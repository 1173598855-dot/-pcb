from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID

from pcbflow.commands import DesignCommand, InstantiateModuleOperation, SchematicObjectRef
from pcbflow.schematic.cst import CstAtom, CstDocument, CstList, apply_edits, make_atom, make_list, make_string, parse_cst, replace_node
from pcbflow.schematic.diff import ChangeKind, ChangeSelector, CommandAttribution, build_semantic_diff
from pcbflow.schematic.modules import ModuleCatalogPort, ModuleRevision, ModuleRevisionNotFoundError, derive_module_uuid
from pcbflow.schematic.semantic import KicadSemanticError, ParsedSchematic, Point, SchematicDocument, inspect_schematic, object_ref_key, parse_schematic


class DesignCommandUnsupportedError(ValueError):
    code = "DESIGN_COMMAND_UNSUPPORTED"

    def __init__(self, operation_type: str) -> None:
        super().__init__(f"{self.code}: {operation_type}")


@dataclass(frozen=True, slots=True)
class AdapterCapabilityReport:
    adapter_contract: str
    kicad_major: int
    supported_operations: tuple[str, ...]
    module_digests: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CommandResult:
    command_id: str
    operation_type: str
    effects: tuple[ChangeSelector, ...]
    created_files: tuple[str, ...]
    provenance_digests: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ApplyResult:
    modified_files: tuple[str, ...]
    command_results: tuple[CommandResult, ...]
    after: SchematicDocument
    capability_report: AdapterCapabilityReport


class SchematicAdapter(Protocol):
    def inspect(self, project: Path) -> SchematicDocument: ...

    def apply(self, project: Path, commands: tuple[DesignCommand, ...]) -> ApplyResult: ...


class CstSchematicAdapter:
    _ADAPTER_CONTRACT = "pcbflow.schematic.cst.v1"

    def __init__(self, module_catalog: ModuleCatalogPort | None) -> None:
        self._module_catalog = module_catalog

    def inspect(self, project: Path) -> SchematicDocument:
        return inspect_schematic(project)

    def apply(self, project: Path, commands: tuple[DesignCommand, ...]) -> ApplyResult:
        if not commands:
            raise ValueError("at least one design command is required")
        if self._module_catalog is None and any(
            command.operation.type
            in ("schematic.instantiate_module", "schematic.assign_footprint")
            for command in commands
        ):
            raise ModuleRevisionNotFoundError("MODULE_CATALOG_NOT_CONFIGURED")
        for command in commands:
            if not isinstance(command.operation, InstantiateModuleOperation):
                raise DesignCommandUnsupportedError(command.operation.type)
        if len(commands) != 1:
            raise ValueError("Task 13 supports exactly one instantiate command per apply")
        command = commands[0]
        assert self._module_catalog is not None
        project_root = _checked_project(project)
        before = parse_schematic(project_root)
        revision = self._module_catalog.get(command.operation.payload.module_revision_id)
        result, after, modified = self._instantiate(project_root, before, command, revision)
        return ApplyResult(
            modified_files=modified,
            command_results=(result,),
            after=after,
            capability_report=AdapterCapabilityReport(
                adapter_contract=self._ADAPTER_CONTRACT,
                kicad_major=9,
                supported_operations=("schematic.instantiate_module",),
                module_digests=(revision.manifest_digest,),
            ),
        )

    def _instantiate(
        self,
        project: Path,
        before: ParsedSchematic,
        command: DesignCommand,
        revision: ModuleRevision,
    ) -> tuple[CommandResult, SchematicDocument, tuple[str, ...]]:
        operation = command.operation
        assert isinstance(operation, InstantiateModuleOperation)
        payload = operation.payload
        before_document = before.document
        manifest = revision.manifest
        if manifest.kicad_major != 9 or manifest.adapter_contract != self._ADAPTER_CONTRACT:
            raise ValueError("module adapter contract is unsupported")
        if payload.placement_slot != "auto":
            raise DesignCommandUnsupportedError("placement_slot")
        _validate_bindings(manifest.parameters, manifest.ports, payload.parameter_bindings, payload.port_bindings)
        target_file, target_document = _target_document(project, before_document, payload.target_sheet_ref)
        child_relative = f"generated/{payload.instance_name.lower()}-{command.command_id}.kicad_sch"
        child_path = target_file.parent / Path(child_relative)
        _validate_new_child_path(project, target_file.parent, child_path)
        sheet_uuid = derive_module_uuid(
            command.project_id, command.batch_id, command.command_id, revision.manifest_digest, f"sheet:{payload.instance_name}"
        )
        child_bytes = _render_child(revision, command, payload.parameter_bindings)
        inserted_nodes = _make_sheet_nodes(
            payload.instance_name,
            child_relative,
            sheet_uuid,
            _placement(target_document),
            command,
            revision,
            manifest.ports,
            payload.port_bindings,
            before,
        )
        root_bytes = apply_edits(
            target_document,
            (_insert_into_root(target_document, inserted_nodes),),
        )
        old_root_bytes = target_file.read_bytes()
        created = False
        root_changed = False
        created_directory = False
        try:
            created_directory = _ensure_generated_directory(child_path.parent)
            _atomic_create(child_path, child_bytes)
            created = True
            _atomic_replace(target_file, root_bytes)
            root_changed = True
            after = inspect_schematic(project)
            _validate_port_connectivity(
                after,
                sheet_uuid,
                command,
                revision,
                manifest.ports,
                payload.port_bindings,
            )
            effects = _selectors_for_changes(before_document, after)
            attribution = CommandAttribution(
                command_id=command.command_id,
                requirement_ids=command.provenance.requirement_ids,
                risk=command.risk,
                selectors=effects,
            )
            # This asserts that every observed semantic change has one exact selector.
            build_semantic_diff(before_document, after, (attribution,))
        except Exception:
            if root_changed:
                _atomic_replace(target_file, old_root_bytes)
            if created:
                child_path.unlink(missing_ok=True)
            if created_directory:
                child_path.parent.rmdir()
            raise
        modified = tuple(
            sorted(
                (
                    target_file.relative_to(project).as_posix(),
                    child_path.relative_to(project).as_posix(),
                )
            )
        )
        return (
            CommandResult(
                command_id=command.command_id,
                operation_type=operation.type,
                effects=effects,
                created_files=(child_path.relative_to(project).as_posix(),),
                provenance_digests=(revision.manifest_digest, manifest.template.digest),
            ),
            after,
            modified,
        )


def _checked_project(project: Path) -> Path:
    root = Path(project)
    if root.is_symlink() or _is_reparse_point(root) or not root.is_dir():
        raise KicadSemanticError("project must be a real directory")
    return root.resolve(strict=True)


def _validate_bindings(parameters, ports, parameter_bindings, port_bindings) -> None:
    unknown_parameters = set(parameter_bindings) - set(parameters)
    missing_parameters = {name for name, entry in parameters.items() if entry.required} - set(parameter_bindings)
    if unknown_parameters or missing_parameters:
        raise ValueError("module parameter bindings do not match manifest")
    if set(port_bindings) != set(ports):
        raise ValueError("module port bindings do not match manifest")


def _target_document(project: Path, document: SchematicDocument, target: SchematicObjectRef) -> tuple[Path, CstDocument]:
    if target.kind != "sheet" or target.pin_number is not None:
        raise ValueError("module target must be a sheet")
    matches = [sheet for sheet in document.sheets if sheet.ref == target]
    if len(matches) != 1:
        raise KicadSemanticError("module target sheet was not found")
    target_sheet = matches[0]
    if sum(sheet.file_name == target_sheet.file_name for sheet in document.sheets) != 1:
        raise KicadSemanticError("module target sheet file is reused")
    target_path = project / target_sheet.file_name
    if not target_path.is_file() or target_path.is_symlink() or _is_reparse_point(target_path):
        raise KicadSemanticError("module target sheet is unsafe")
    source = target_path.read_bytes()
    return target_path, parse_cst(source)


def _validate_new_child_path(project: Path, target_directory: Path, child: Path) -> None:
    if not child.is_relative_to(project) or child.suffix != ".kicad_sch" or child.exists():
        raise ValueError("generated child path is invalid or already exists")
    parent = child.parent
    if parent.parent != target_directory or parent.name != "generated":
        raise KicadSemanticError("generated child path is unsafe")
    if parent.exists() and (parent.is_symlink() or _is_reparse_point(parent) or not parent.is_dir()):
        raise KicadSemanticError("generated directory is unsafe")
    if target_directory.is_symlink() or _is_reparse_point(target_directory):
        raise KicadSemanticError("generated directory is unsafe")


def _ensure_generated_directory(path: Path) -> bool:
    if path.exists():
        return False
    created = False
    try:
        path.mkdir(mode=0o700)
        created = True
        if path.is_symlink() or _is_reparse_point(path) or not path.is_dir():
            raise KicadSemanticError("generated directory is unsafe")
        return True
    except Exception:
        if created:
            path.rmdir()
        raise


def _render_child(revision: ModuleRevision, command: DesignCommand, parameter_bindings: dict[str, str]) -> bytes:
    document = parse_cst(revision.template_bytes)
    if document.root.head != "kicad_sch":
        raise ValueError("module template is not a KiCad schematic")
    edits = []
    for atom, local_uuid in _uuid_atoms(document.root):
        if local_uuid not in revision.manifest.uuid_bindings:
            raise ValueError("module template UUID binding was not verified")
        edits.append(
            replace_node(
                atom,
                make_atom(
                    derive_module_uuid(command.project_id, command.batch_id, command.command_id, revision.manifest_digest, local_uuid)
                ),
            )
        )
    if not edits:
        raise ValueError("module template contains no UUIDs")
    for name, entry in revision.manifest.parameters.items():
        if name not in parameter_bindings:
            continue
        values = _symbol_property_values(document.root, entry.property_name)
        if len(values) != 1:
            raise ValueError("module parameter property is ambiguous")
        edits.append(replace_node(values[0], make_string(parameter_bindings[name])))
    return apply_edits(document, tuple(edits))


def _uuid_atoms(node: CstList):
    if node.head == "uuid" and len(node.items) == 2 and isinstance(node.items[1], CstAtom):
        value = node.items[1].value
        try:
            yield node.items[1], str(UUID(value))
        except ValueError as error:
            raise ValueError("module template UUID is invalid") from error
    for item in node.items:
        if isinstance(item, CstList):
            yield from _uuid_atoms(item)


def _symbol_property_values(root: CstList, name: str) -> tuple[CstAtom, ...]:
    values: list[CstAtom] = []
    for symbol in root.find_children("symbol"):
        for property_node in symbol.find_children("property"):
            if len(property_node.items) >= 3 and property_node.atom_text(1) == name:
                value = property_node.items[2]
                if isinstance(value, CstAtom):
                    values.append(value)
    return tuple(values)


def _placement(document: CstDocument) -> Point:
    occupied = {_node_point(node) for node in document.root.find_children("sheet")}
    for y in range(25, 1001, 25):
        for x in range(25, 1001, 25):
            point = Point(float(x), float(y))
            if point not in occupied:
                return point
    raise ValueError("no free module placement slot")


def _node_point(node: CstList) -> Point:
    at = node.find_children("at")
    if len(at) != 1:
        raise KicadSemanticError("sheet is missing placement")
    return Point(float(at[0].atom_text(1)), float(at[0].atom_text(2)))


def _make_sheet_nodes(
    instance_name,
    child_relative,
    sheet_uuid,
    placement,
    command,
    revision,
    ports,
    port_bindings,
    before,
):
    children = [
        make_atom("sheet"),
        make_list(make_atom("at"), make_atom(_number_text(placement.x)), make_atom(_number_text(placement.y))),
        make_list(make_atom("size"), make_atom("50"), make_atom("25")),
        make_list(make_atom("uuid"), make_atom(sheet_uuid)),
        make_list(make_atom("property"), make_string("Sheetname"), make_string(instance_name)),
        make_list(make_atom("property"), make_string("Sheetfile"), make_string(child_relative)),
    ]
    labels: list[CstList] = []
    wires: list[CstList] = []
    for index, name in enumerate(sorted(ports)):
        position = _binding_point(before, port_bindings[name])
        port_position = Point(placement.x + 50, placement.y + 5 + index * 5)
        port_uuid = derive_module_uuid(command.project_id, command.batch_id, command.command_id, revision.manifest_digest, f"sheet-pin:{instance_name}:{name}")
        label_uuid = derive_module_uuid(command.project_id, command.batch_id, command.command_id, revision.manifest_digest, f"sheet-label:{instance_name}:{name}")
        wire_uuid = derive_module_uuid(command.project_id, command.batch_id, command.command_id, revision.manifest_digest, f"sheet-wire:{instance_name}:{name}")
        children.append(
            make_list(
                make_atom("pin"), make_string(name), make_atom(ports[name]),
                make_list(make_atom("at"), make_atom(_number_text(port_position.x)), make_atom(_number_text(port_position.y)), make_atom("0")),
                make_list(make_atom("uuid"), make_atom(port_uuid)),
            )
        )
        # Labels are root siblings in KiCad and are inserted only for explicit bindings.
        labels.append(
            make_list(
                make_atom("label"), make_string(name),
                make_list(make_atom("at"), make_atom(_number_text(position.x)), make_atom(_number_text(position.y)), make_atom("0")),
                make_list(make_atom("uuid"), make_atom(label_uuid)),
            )
        )
        wires.append(
            make_list(
                make_atom("wire"),
                make_list(
                    make_atom("pts"),
                    make_list(make_atom("xy"), make_atom(_number_text(position.x)), make_atom(_number_text(position.y))),
                    make_list(make_atom("xy"), make_atom(_number_text(port_position.x)), make_atom(_number_text(port_position.y))),
                ),
                make_list(make_atom("uuid"), make_atom(wire_uuid)),
            )
        )
    return (make_list(*children), *labels, *wires)


def _binding_point(parsed: ParsedSchematic, target: SchematicObjectRef) -> Point:
    document = parsed.document
    if target.kind == "pin":
        for symbol in document.symbols:
            for pin in symbol.pins:
                if pin.ref == target:
                    return pin.position
    if target.kind == "hierarchical_port":
        for sheet in document.sheets:
            for port in sheet.ports:
                if port.ref == target:
                    return port.position
    raise KicadSemanticError("module port binding target must be a pin or hierarchical port")


def _validate_port_connectivity(
    after: SchematicDocument,
    sheet_uuid: str,
    command: DesignCommand,
    revision: ModuleRevision,
    ports,
    bindings,
) -> None:
    if not ports:
        return
    sheet = next(
        (item for item in after.sheets if item.ref.object_uuid == sheet_uuid),
        None,
    )
    if sheet is None:
        raise KicadSemanticError("instantiated sheet was not found")
    child_pins = tuple(
        pin
        for symbol in after.symbols
        if symbol.ref.sheet_uuid == sheet_uuid
        for pin in symbol.pins
    )
    for name in sorted(ports):
        port = next((item for item in sheet.ports if item.name == name), None)
        label = next(
            (
                item
                for item in after.labels
                if item.name == name
                and item.scope == "hierarchical"
                and item.ref.sheet_uuid == sheet_uuid
            ),
            None,
        )
        parent_label_uuid = derive_module_uuid(
            command.project_id,
            command.batch_id,
            command.command_id,
            revision.manifest_digest,
            f"sheet-label:{command.operation.payload.instance_name}:{name}",
        )
        parent_label = next(
            (item for item in after.labels if item.ref.object_uuid == parent_label_uuid),
            None,
        )
        if port is None or label is None or parent_label is None:
            raise KicadSemanticError("module port endpoint was not created")
        if not any(
            {object_ref_key(label.ref), object_ref_key(pin.ref)} <= set(net.members)
            for pin in child_pins
            for net in after.nets
        ):
            raise KicadSemanticError("module child port is not connected to a pin")
        target = bindings[name]
        if not any(
            {
                object_ref_key(target),
                object_ref_key(parent_label.ref),
                object_ref_key(port.ref),
            } <= set(net.members)
            for net in after.nets
        ):
            raise KicadSemanticError("module parent port is not connected to target")


def _number_text(value: float) -> str:
    return str(int(value)) if value.is_integer() else str(value)


def _insert_into_root(document: CstDocument, nodes: tuple[CstList, ...]):
    if document.root.head != "kicad_sch":
        raise KicadSemanticError("module target is not a KiCad schematic")
    from pcbflow.schematic.cst import insert_before_close

    return insert_before_close(document.root, nodes, indent=2)


def _is_reparse_point(path: Path) -> bool:
    try:
        return bool(os.lstat(path).st_file_attributes & 0x400)
    except (AttributeError, OSError):
        return False


def _atomic_create(path: Path, data: bytes) -> None:
    if path.exists():
        raise FileExistsError(path)
    _atomic_write(path, data, replace=False)


def _atomic_replace(path: Path, data: bytes) -> None:
    _atomic_write(path, data, replace=True)


def _atomic_write(path: Path, data: bytes, *, replace: bool) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if not replace and path.exists():
            raise FileExistsError(path)
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _selectors_for_changes(before: SchematicDocument, after: SchematicDocument) -> tuple[ChangeSelector, ...]:
    selectors: list[ChangeSelector] = []
    selectors.extend(_added_removed_selectors(ChangeKind.SHEET_ADDED, ChangeKind.SHEET_REMOVED, before.sheets, after.sheets))
    selectors.extend(_added_removed_selectors(ChangeKind.SYMBOL_ADDED, ChangeKind.SYMBOL_REMOVED, before.symbols, after.symbols))
    selectors.extend(_symbol_update_selectors(before, after))
    selectors.extend(_added_removed_selectors(ChangeKind.LABEL_ADDED, ChangeKind.LABEL_REMOVED, before.labels, after.labels))
    before_nets = {object_ref_key(item.ref): item for item in before.nets}
    after_nets = {object_ref_key(item.ref): item for item in after.nets}
    for key in sorted(set(before_nets) | set(after_nets)):
        if before_nets.get(key) != after_nets.get(key):
            item = after_nets.get(key) or before_nets[key]
            selectors.append(ChangeSelector(ChangeKind.NET_CONNECTIVITY_CHANGED, item.ref, None))
    return tuple(sorted(selectors, key=lambda item: (item.kind.value, object_ref_key(item.subject_ref), item.field or "")))


def _added_removed_selectors(added, removed, before_items, after_items):
    before_index = {object_ref_key(item.ref): item for item in before_items}
    after_index = {object_ref_key(item.ref): item for item in after_items}
    return [
        ChangeSelector(added, after_index[key].ref, None) for key in sorted(set(after_index) - set(before_index))
    ] + [
        ChangeSelector(removed, before_index[key].ref, None) for key in sorted(set(before_index) - set(after_index))
    ]


def _symbol_update_selectors(before: SchematicDocument, after: SchematicDocument) -> list[ChangeSelector]:
    before_index = {object_ref_key(item.ref): item for item in before.symbols}
    after_index = {object_ref_key(item.ref): item for item in after.symbols}
    selectors: list[ChangeSelector] = []
    for key in sorted(set(before_index) & set(after_index)):
        old, new = before_index[key], after_index[key]
        if old.footprint != new.footprint:
            selectors.append(ChangeSelector(ChangeKind.FOOTPRINT_CHANGED, new.ref, "Footprint"))
        old_properties = {item.name: item.value for item in old.properties}
        new_properties = {item.name: item.value for item in new.properties}
        for name in sorted(set(old_properties) | set(new_properties)):
            if name != "Footprint" and old_properties.get(name) != new_properties.get(name):
                selectors.append(ChangeSelector(ChangeKind.SYMBOL_PROPERTY_CHANGED, new.ref, name))
    return selectors

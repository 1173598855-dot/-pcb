from __future__ import annotations

import os
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid5

from pcbflow.commands import (
    AddLabelOperation,
    AssignFootprintOperation,
    DesignCommand,
    InstantiateModuleOperation,
    SetPropertyOperation,
    SchematicObjectRef,
)
from pcbflow.observability import MetricName, Metrics
from pcbflow.kicad_compatibility import profile_for_major
from pcbflow.schematic.cst import (
    CstAtom,
    CstDocument,
    CstEdit,
    CstList,
    apply_edits,
    insert_before_close,
    make_atom,
    make_list,
    make_string,
    parse_cst,
    replace_node,
)
from pcbflow.schematic.diff import ChangeKind, ChangeSelector, CommandAttribution, build_semantic_diff
from pcbflow.schematic.modules import (
    ModuleCatalogPort,
    ModuleRevision,
    ModuleRevisionNotFoundError,
    derive_module_uuid,
)
from pcbflow.schematic.semantic import (
    KicadSemanticError,
    ParsedSchematic,
    Point,
    SchematicDocument,
    inspect_schematic,
    object_ref_key,
    parse_schematic,
)


class UnsupportedDesignCommandError(ValueError):
    code = "DESIGN_COMMAND_UNSUPPORTED"

    def __init__(self, operation_type: str) -> None:
        super().__init__(f"{self.code}: {operation_type}")


DesignCommandUnsupportedError = UnsupportedDesignCommandError


class PropertyWriteNotAllowedError(ValueError):
    code = "PROPERTY_WRITE_NOT_ALLOWED"

    def __init__(self, property_name: str) -> None:
        super().__init__(f"{self.code}: {property_name}")


class LabelTargetError(ValueError):
    code = "LABEL_TARGET_ERROR"

    def __init__(self, detail: str) -> None:
        super().__init__(f"{self.code}: {detail}")


PROPERTY_ALLOWLIST = frozenset({"Reference", "Value", "Description"})
USER_PROPERTY_PREFIX = "User."
LABEL_TARGET_KINDS = frozenset({"pin", "hierarchical_port", "wire_endpoint", "net"})


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
    def inspect(self, project: Path, *, kicad_major: int = 9) -> SchematicDocument: ...

    def apply(
        self,
        project: Path,
        commands: tuple[DesignCommand, ...],
        *,
        kicad_major: int = 9,
    ) -> ApplyResult: ...


class CstSchematicAdapter:
    _ADAPTER_CONTRACT = "pcbflow.schematic.cst.v1"

    def __init__(
        self,
        module_catalog: ModuleCatalogPort | None,
        *,
        metrics: Metrics | None = None,
        monotonic=time.monotonic,
    ) -> None:
        self._module_catalog = module_catalog
        self._metrics = metrics
        self._monotonic = monotonic

    def inspect(self, project: Path, *, kicad_major: int = 9) -> SchematicDocument:
        profile = profile_for_major(kicad_major)
        if profile is None:
            raise ValueError(f"unsupported KiCad major: {kicad_major}")
        started = self._monotonic()
        try:
            return replace(
                inspect_schematic(
                    project,
                    accepted_versions=profile.schematic_format_versions,
                ),
                kicad_major=kicad_major,
            )
        finally:
            if self._metrics is not None:
                self._metrics.observe(
                    MetricName.SCHEMATIC_PARSE_SECONDS,
                    max(0.0, self._monotonic() - started),
                )

    def apply(
        self,
        project: Path,
        commands: tuple[DesignCommand, ...],
        *,
        kicad_major: int = 9,
    ) -> ApplyResult:
        profile = profile_for_major(kicad_major)
        if profile is None:
            raise ValueError(f"unsupported KiCad major: {kicad_major}")
        try:
            result = self._apply(project, commands, kicad_major=kicad_major)
        except Exception:
            if self._metrics is not None:
                self._metrics.increment(
                    MetricName.ADAPTER_EXECUTION_TOTAL,
                    labels={"contract": self._ADAPTER_CONTRACT, "result": "failed"},
                )
            raise
        if self._metrics is not None:
            self._metrics.increment(
                MetricName.ADAPTER_EXECUTION_TOTAL,
                labels={"contract": self._ADAPTER_CONTRACT, "result": "pass"},
            )
        return result

    def _apply(
        self,
        project: Path,
        commands: tuple[DesignCommand, ...],
        *,
        kicad_major: int,
    ) -> ApplyResult:
        if not commands:
            raise ValueError("at least one design command is required")
        if self._module_catalog is None and any(
            command.operation.type
            in ("schematic.instantiate_module", "schematic.assign_footprint")
            for command in commands
        ):
            raise ModuleRevisionNotFoundError("MODULE_CATALOG_NOT_CONFIGURED")
        if self._module_catalog is None and any(
            isinstance(command.operation, (SetPropertyOperation, AddLabelOperation))
            for command in commands
        ):
            raise UnsupportedDesignCommandError(commands[0].operation.type)
        project_root = _checked_project(project)
        if all(isinstance(command.operation, InstantiateModuleOperation) for command in commands):
            if len(commands) != 1:
                raise ValueError("Task 13 supports exactly one instantiate command per apply")
            command = commands[0]
            assert self._module_catalog is not None
            profile = profile_for_major(kicad_major)
            assert profile is not None
            before = parse_schematic(
                project_root,
                accepted_versions=profile.schematic_format_versions,
            )
            revision = self._module_catalog.get(command.operation.payload.module_revision_id)
            result, after, modified = self._instantiate(
                project_root, before, command, revision, kicad_major=kicad_major
            )
            return ApplyResult(
                modified_files=modified,
                command_results=(result,),
                after=after,
                capability_report=AdapterCapabilityReport(
                    adapter_contract=self._ADAPTER_CONTRACT,
                    kicad_major=kicad_major,
                    supported_operations=("schematic.instantiate_module",),
                    module_digests=(revision.manifest_digest,),
                ),
            )
        supported = (
            SetPropertyOperation,
            AssignFootprintOperation,
            AddLabelOperation,
        )
        for command in commands:
            if not isinstance(command.operation, supported):
                raise UnsupportedDesignCommandError(command.operation.type)
        if self._module_catalog is None and any(
            isinstance(command.operation, AssignFootprintOperation)
            for command in commands
        ):
            raise ModuleRevisionNotFoundError("MODULE_CATALOG_NOT_CONFIGURED")
        profile = profile_for_major(kicad_major)
        assert profile is not None
        before = parse_schematic(
            project_root,
            accepted_versions=profile.schematic_format_versions,
        )
        results, after, modified, module_digests = self._apply_controlled_operations(
            project_root, before, commands, kicad_major=kicad_major
        )
        return ApplyResult(
            modified_files=modified,
            command_results=results,
            after=after,
            capability_report=AdapterCapabilityReport(
                adapter_contract=self._ADAPTER_CONTRACT,
                kicad_major=kicad_major,
                supported_operations=tuple(sorted({command.operation.type for command in commands})),
                module_digests=module_digests,
            ),
        )

    def _apply_controlled_operations(
        self,
        project: Path,
        before: ParsedSchematic,
        commands: tuple[DesignCommand, ...],
        *,
        kicad_major: int,
    ) -> tuple[tuple[CommandResult, ...], SchematicDocument, tuple[str, ...], tuple[str, ...]]:
        return _apply_controlled_operations(
            project,
            before,
            commands,
            self._module_catalog,
            kicad_major=kicad_major,
        )

    def _instantiate(
        self,
        project: Path,
        before: ParsedSchematic,
        command: DesignCommand,
        revision: ModuleRevision,
        *,
        kicad_major: int,
    ) -> tuple[CommandResult, SchematicDocument, tuple[str, ...]]:
        operation = command.operation
        assert isinstance(operation, InstantiateModuleOperation)
        payload = operation.payload
        before_document = before.document
        manifest = revision.manifest
        if kicad_major not in manifest.kicad_majors or manifest.adapter_contract != self._ADAPTER_CONTRACT:
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
            profile = profile_for_major(kicad_major)
            assert profile is not None
            after = inspect_schematic(
                project,
                accepted_versions=profile.schematic_format_versions,
            )
            after = replace(after, kicad_major=kicad_major)
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


_LABEL_UUID_NAMESPACE = UUID("d1e1f7db-0f24-59d4-b3fd-0c7f13f0d9a1")


def _apply_controlled_operations(
    project: Path,
    before: ParsedSchematic,
    commands: tuple[DesignCommand, ...],
    module_catalog: ModuleCatalogPort | None,
    *,
    kicad_major: int,
) -> tuple[tuple[CommandResult, ...], SchematicDocument, tuple[str, ...], tuple[str, ...]]:
    edits_by_file: dict[Path, list[CstEdit]] = {}
    inserted_by_file: dict[Path, list[CstList]] = {}
    original_bytes: dict[Path, bytes] = {}
    specs: list[tuple[DesignCommand, ChangeSelector, str | None, str | None]] = []
    module_digests: list[str] = []
    pending_bindings: dict[tuple[str, str], str | None] = {}
    reference_values = {
        object_ref_key(symbol.ref): symbol.reference for symbol in before.document.symbols
    }

    def add_edit(path: Path, edit: CstEdit) -> None:
        edits = edits_by_file.setdefault(path, [])
        for previous in edits:
            if edit.start < previous.end and previous.start < edit.end:
                raise ValueError("CST edits overlap")
        edits.append(edit)
        original_bytes.setdefault(path, path.read_bytes())

    for command in commands:
        operation = command.operation
        if isinstance(operation, SetPropertyOperation):
            payload = operation.payload
            symbol = _resolve_symbol(before.document, payload.subject_ref)
            location = before.location(symbol.ref)
            property_nodes = {
                node.atom_text(1): node for node in location.node.find_children("property")
            }
            name = payload.property_name
            if name == "Footprint" or name in {"uuid", "UUID"} or name.startswith("ki_"):
                raise PropertyWriteNotAllowedError(name)
            if name not in PROPERTY_ALLOWLIST and not name.startswith(USER_PROPERTY_PREFIX):
                raise PropertyWriteNotAllowedError(name)
            current = next(
                (item.value for item in symbol.properties if item.name == name), None
            )
            if payload.expected_old_value is not None and current != payload.expected_old_value:
                raise ValueError(f"property value mismatch: {name}")
            if name == "Reference":
                for key, value in reference_values.items():
                    if key != object_ref_key(symbol.ref) and value == payload.value:
                        raise ValueError(f"reference already exists: {payload.value}")
                reference_values[object_ref_key(symbol.ref)] = payload.value
            existing = property_nodes.get(name)
            if existing is None:
                add_edit(
                    location.file_path,
                    insert_before_close(
                        location.node,
                        (make_list(make_atom("property"), make_string(name), make_string(payload.value)),),
                        indent=4,
                    ),
                )
            else:
                replacement_items = list(existing.items)
                if len(replacement_items) < 3:
                    raise KicadSemanticError("malformed symbol property")
                replacement_items[2] = make_string(payload.value)
                add_edit(location.file_path, replace_node(existing, make_list(*replacement_items)))
            selector = ChangeSelector(ChangeKind.SYMBOL_PROPERTY_CHANGED, symbol.ref, name)
            specs.append((command, selector, None, None))
            continue

        if isinstance(operation, AssignFootprintOperation):
            if module_catalog is None:
                raise ModuleRevisionNotFoundError("MODULE_CATALOG_NOT_CONFIGURED")
            payload = operation.payload
            symbol = _resolve_symbol(before.document, payload.subject_ref)
            footprint = module_catalog.get_footprint(payload.footprint_revision_id)
            location = before.location(symbol.ref)
            property_nodes = {
                node.atom_text(1): node for node in location.node.find_children("property")
            }
            existing = property_nodes.get("Footprint")
            if existing is None:
                add_edit(
                    location.file_path,
                    insert_before_close(
                        location.node,
                        (make_list(make_atom("property"), make_string("Footprint"), make_string(footprint.library_id)),),
                        indent=4,
                    ),
                )
            else:
                replacement_items = list(existing.items)
                if len(replacement_items) < 3:
                    raise KicadSemanticError("malformed symbol property")
                replacement_items[2] = make_string(footprint.library_id)
                add_edit(location.file_path, replace_node(existing, make_list(*replacement_items)))
            selector = ChangeSelector(ChangeKind.FOOTPRINT_CHANGED, symbol.ref, "Footprint")
            specs.append((command, selector, footprint.digest, None))
            module_digests.append(footprint.digest)
            continue

        if isinstance(operation, AddLabelOperation):
            payload = operation.payload
            if payload.target_ref.kind not in LABEL_TARGET_KINDS:
                raise LabelTargetError(payload.target_ref.kind)
            path, position, target_net = _label_target(before, payload.target_ref)
            _check_label_binding(before.document, payload.name, payload.scope, target_net)
            binding_key = (payload.name, payload.scope)
            if binding_key in pending_bindings:
                pending_net = pending_bindings[binding_key]
                if pending_net != target_net and (
                    pending_net is not None or target_net is not None
                ):
                    raise LabelTargetError("label name/scope is already bound to another net")
            pending_bindings[binding_key] = target_net
            label_uuid = _label_uuid(command, payload.target_ref, payload.name, payload.scope)
            head = {
                "local": "label",
                "global": "global_label",
                "hierarchical": "hierarchical_label",
            }[payload.scope]
            nodes = [make_atom(head), make_string(payload.name)]
            if payload.scope != "local":
                nodes.append(make_list(make_atom("shape"), make_atom("input")))
            nodes.extend(
                (
                    make_list(
                        make_atom("at"),
                        make_atom(_number_text(position.x)),
                        make_atom(_number_text(position.y)),
                        make_atom("0"),
                    ),
                    make_list(make_atom("uuid"), make_atom(label_uuid)),
                )
            )
            inserted_by_file.setdefault(path, []).append(make_list(*nodes))
            original_bytes.setdefault(path, path.read_bytes())
            selector = ChangeSelector(
                ChangeKind.LABEL_ADDED,
                SchematicObjectRef(
                    kind="label", sheet_uuid=payload.target_ref.sheet_uuid,
                    object_uuid=label_uuid, pin_number=None,
                ),
                None,
            )
            specs.append((command, selector, None, target_net))
            continue

        raise UnsupportedDesignCommandError(operation.type)

    for path, nodes in inserted_by_file.items():
        document = _location_for_path(before, path).document
        add_edit(path, insert_before_close(document.root, tuple(nodes), indent=2))
    changed_files: dict[Path, bytes] = {}
    try:
        for path, edits in edits_by_file.items():
            document = parse_cst(original_bytes[path])
            changed_files[path] = apply_edits(document, tuple(edits))
            _atomic_replace(path, changed_files[path])
        profile = profile_for_major(kicad_major)
        assert profile is not None
        after = parse_schematic(
            project,
            accepted_versions=profile.schematic_format_versions,
        ).document
        after = replace(after, kicad_major=kicad_major)
        before_nets = {object_ref_key(item.ref): item for item in before.document.nets}
        after_nets = {object_ref_key(item.ref): item for item in after.nets}
        changed_net_keys = {
            key
            for key in set(before_nets) | set(after_nets)
            if before_nets.get(key) != after_nets.get(key)
        }
        effects_by_command: dict[str, list[ChangeSelector]] = {
            command.command_id: [selector] for command, selector, _, _ in specs
        }
        for net_key in sorted(changed_net_keys):
            candidates = [
                command.command_id
                for command, selector, _, target_net in specs
                if target_net == net_key
                or (
                    selector.kind is ChangeKind.LABEL_ADDED
                    and any(
                        object_ref_key(selector.subject_ref) in net.members
                        for net in (after_nets.get(net_key), before_nets.get(net_key))
                        if net is not None
                    )
                )
            ]
            if len(candidates) == 1:
                net_ref = (after_nets.get(net_key) or before_nets[net_key]).ref
                effects_by_command[candidates[0]].append(
                    ChangeSelector(ChangeKind.NET_CONNECTIVITY_CHANGED, net_ref, None)
                )
        attributions = tuple(
            CommandAttribution(
                command_id=command.command_id,
                requirement_ids=command.provenance.requirement_ids,
                risk=command.risk,
                selectors=tuple(effects_by_command[command.command_id]),
            )
            for command, _, _, _ in specs
        )
        build_semantic_diff(before.document, after, attributions)
    except Exception:
        for path, data in original_bytes.items():
            try:
                _atomic_replace(path, data)
            except OSError:
                pass
        raise
    command_results = tuple(
        CommandResult(
            command_id=command.command_id,
            operation_type=command.operation.type,
            effects=tuple(effects_by_command[command.command_id]),
            created_files=(),
            provenance_digests=tuple(
                digest for command_, _, digest, _ in specs if command_ is command and digest is not None
            ),
        )
        for command, _, _, _ in specs
    )
    modified = tuple(sorted(path.relative_to(project).as_posix() for path in original_bytes))
    return command_results, after, modified, tuple(sorted(set(module_digests)))


def _resolve_symbol(document: SchematicDocument, reference: SchematicObjectRef):
    if reference.kind != "symbol":
        raise KicadSemanticError("operation target must be a symbol")
    matches = [symbol for symbol in document.symbols if symbol.ref == reference]
    if len(matches) != 1:
        raise KicadSemanticError("symbol target was not found")
    return matches[0]


def _location_for_path(parsed: ParsedSchematic, path: Path):
    for location in parsed.locations.values():
        if location.file_path == path:
            return location
    raise KicadSemanticError(f"schematic file location was not found: {path}")


def _label_target(
    parsed: ParsedSchematic, reference: SchematicObjectRef
) -> tuple[Path, Point, str | None]:
    key = object_ref_key(reference)
    if reference.kind == "pin":
        for symbol in parsed.document.symbols:
            for pin in symbol.pins:
                if pin.ref == reference or (
                    pin.symbol_ref.sheet_uuid == reference.sheet_uuid
                    and pin.symbol_ref.object_uuid == reference.object_uuid
                    and pin.number == reference.pin_number
                ):
                    return parsed.location(pin.ref).file_path, pin.position, _net_for_ref(
                        parsed.document, object_ref_key(pin.ref)
                    )
    elif reference.kind == "hierarchical_port":
        for sheet in parsed.document.sheets:
            for port in sheet.ports:
                if port.ref == reference:
                    return parsed.location(port.ref).file_path, port.position, _net_for_ref(parsed.document, key)
    elif reference.kind == "net":
        for net in parsed.document.nets:
            if net.ref == reference:
                location = parsed.location(net.ref)
                return location.file_path, _net_position(parsed, net, location), key
    elif reference.kind == "wire_endpoint":
        for location in _unique_locations(parsed):
            for wire in location.document.root.find_children("wire"):
                if _optional_uuid(wire) != reference.object_uuid:
                    continue
                points = _wire_points(wire)
                if len(points) < 2:
                    break
                endpoint = reference.pin_number
                if endpoint is None:
                    raise LabelTargetError("wire endpoint must identify start/end or 1/2")
                if endpoint in {"1", "start"}:
                    point = points[0]
                elif endpoint in {"2", "end"}:
                    point = points[-1]
                else:
                    raise LabelTargetError("wire endpoint must be start/end or 1/2")
                return location.file_path, point, _net_for_ref(parsed.document, f"wire:{reference.sheet_uuid}:{reference.object_uuid}")
    raise LabelTargetError("target does not resolve to an existing semantic position")


def _unique_locations(parsed: ParsedSchematic):
    seen: set[tuple[Path, int, int]] = set()
    for location in parsed.locations.values():
        identity = (location.file_path, location.document.root.start, location.document.root.end)
        if identity in seen:
            continue
        seen.add(identity)
        yield location


def _node_position(node: CstList) -> Point:
    at = node.find_children("at")
    if at:
        return Point(float(at[0].atom_text(1)), float(at[0].atom_text(2)))
    points = _wire_points(node)
    if points:
        return points[0]
    raise LabelTargetError("target has no semantic position")


def _net_position(parsed: ParsedSchematic, net, location) -> Point:
    try:
        return _node_position(location.node)
    except LabelTargetError:
        pass
    for member in net.members:
        if member.startswith(("pin:", "hierarchical_port:", "label:")):
            for candidate in (
                *parsed.document.symbols,
                *(port for sheet in parsed.document.sheets for port in sheet.ports),
                *parsed.document.labels,
            ):
                references = [candidate.ref]
                if hasattr(candidate, "pins"):
                    references.extend(pin.ref for pin in candidate.pins)
                for reference in references:
                    if object_ref_key(reference) != member:
                        continue
                    if reference.kind == "pin":
                        return next(
                            pin.position
                            for symbol in parsed.document.symbols
                            for pin in symbol.pins
                            if pin.ref == reference
                        )
                    if reference.kind == "hierarchical_port":
                        return next(
                            port.position
                            for sheet in parsed.document.sheets
                            for port in sheet.ports
                            if port.ref == reference
                        )
                    if reference.kind == "label":
                        return next(label.position for label in parsed.document.labels if label.ref == reference)
        if member.startswith("wire:"):
            _, sheet_uuid, wire_uuid = member.split(":", 2)
            expected_file = _sheet_file(parsed, sheet_uuid)
            matches: list[Point] = []
            for candidate in _unique_locations(parsed):
                if expected_file is not None and candidate.file_path.resolve() != expected_file:
                    continue
                for wire in candidate.document.root.find_children("wire"):
                    if _optional_uuid(wire) == wire_uuid:
                        points = _wire_points(wire)
                        if points:
                            matches.append(points[0])
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise LabelTargetError("known net wire endpoint is ambiguous")
    raise LabelTargetError("known net has no concrete semantic endpoint")


def _sheet_file(parsed: ParsedSchematic, sheet_uuid: str) -> Path | None:
    for sheet in parsed.document.sheets:
        if sheet.ref.object_uuid != sheet_uuid:
            continue
        path = parsed.sheet_files.get(object_ref_key(sheet.ref))
        if path is not None:
            return path.resolve()
        parent_location = parsed.location(sheet.ref)
        return (parent_location.file_path.parent / sheet.file_name).resolve()
    return None


def _wire_points(node: CstList) -> tuple[Point, ...]:
    points_nodes = node.find_children("pts")
    if not points_nodes:
        return ()
    return tuple(
        Point(float(xy.atom_text(1)), float(xy.atom_text(2)))
        for xy in points_nodes[0].find_children("xy")
    )


def _optional_uuid(node: CstList) -> str | None:
    uuid_nodes = node.find_children("uuid")
    if not uuid_nodes or len(uuid_nodes[0].items) < 2:
        return None
    return uuid_nodes[0].atom_text(1)


def _net_for_ref(document: SchematicDocument, key: str) -> str | None:
    for net in document.nets:
        if key in net.members:
            return object_ref_key(net.ref)
    return None


def _check_label_binding(
    document: SchematicDocument, name: str, scope: str, target_net: str | None
) -> None:
    for label in document.labels:
        if label.name != name or label.scope != scope:
            continue
        existing_net = _net_for_ref(document, object_ref_key(label.ref))
        if existing_net != target_net and (existing_net is not None or target_net is not None):
            raise LabelTargetError("label name/scope is already bound to another net")


def _label_uuid(
    command: DesignCommand,
    target: SchematicObjectRef,
    name: str,
    scope: str,
) -> str:
    value = "\x1f".join(
        (
            command.project_id,
            command.batch_id,
            command.command_id,
            object_ref_key(target),
            name,
            scope,
        )
    )
    return str(uuid5(_LABEL_UUID_NAMESPACE, value))


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

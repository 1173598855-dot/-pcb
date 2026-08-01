from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from pcbflow.commands import SchematicObjectRef
from pcbflow.schematic.cst import CstDocument, CstList, CstNode, parse_cst


@dataclass(frozen=True, slots=True)
class Point:
    x: float
    y: float


@dataclass(frozen=True, slots=True)
class SymbolProperty:
    name: str
    value: str


@dataclass(frozen=True, slots=True)
class HierarchicalPort:
    ref: SchematicObjectRef
    name: str
    direction: str
    position: Point


@dataclass(frozen=True, slots=True)
class Sheet:
    ref: SchematicObjectRef
    name: str
    file_name: str
    parent_sheet_uuid: str | None
    ports: tuple[HierarchicalPort, ...]


@dataclass(frozen=True, slots=True)
class PinReference:
    ref: SchematicObjectRef
    symbol_ref: SchematicObjectRef
    number: str
    name: str
    position: Point


@dataclass(frozen=True, slots=True)
class FootprintAssignment:
    symbol_ref: SchematicObjectRef
    library_id: str


@dataclass(frozen=True, slots=True)
class Symbol:
    ref: SchematicObjectRef
    library_id: str
    reference: str
    value: str
    unit: int
    position: Point
    properties: tuple[SymbolProperty, ...]
    footprint: str | None
    pins: tuple[PinReference, ...]


@dataclass(frozen=True, slots=True)
class Label:
    ref: SchematicObjectRef
    name: str
    scope: str
    target_ref: SchematicObjectRef | None
    position: Point


@dataclass(frozen=True, slots=True)
class NetConnectivity:
    ref: SchematicObjectRef
    name: str | None
    members: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SchematicDocument:
    kicad_major: int
    root_file: str
    root_sheet_ref: SchematicObjectRef
    sheets: tuple[Sheet, ...]
    symbols: tuple[Symbol, ...]
    labels: tuple[Label, ...]
    nets: tuple[NetConnectivity, ...]
    footprints: tuple[FootprintAssignment, ...]


@dataclass(frozen=True, slots=True)
class CstLocation:
    file_path: Path
    document: CstDocument
    node: CstList


@dataclass(frozen=True, slots=True)
class ParsedSchematic:
    document: SchematicDocument
    locations: dict[str, CstLocation]
    sheet_files: dict[str, Path]
    aliases: dict[str, tuple[SchematicObjectRef, ...]]
    property_aliases: dict[str, tuple[SchematicObjectRef, ...]]

    def location(self, reference: SchematicObjectRef) -> CstLocation:
        try:
            return self.locations[object_ref_key(reference)]
        except KeyError as error:
            raise SemanticObjectNotFoundError(object_ref_key(reference)) from error

    def aliases_for(
        self, reference: SchematicObjectRef
    ) -> tuple[SchematicObjectRef, ...]:
        key = object_ref_key(reference)
        self.location(reference)
        try:
            return self.aliases[key]
        except KeyError as error:
            raise SemanticObjectNotFoundError(key) from error

    def aliases_for_property(
        self, reference: SchematicObjectRef, name: str
    ) -> tuple[SchematicObjectRef, ...]:
        key = _property_location_key(reference, name)
        try:
            return self.property_aliases[key]
        except KeyError as error:
            raise SemanticObjectNotFoundError(key) from error


class KicadSemanticError(ValueError):
    pass


class SemanticObjectNotFoundError(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class _FileRecord:
    path: Path
    relative_path: str
    cst: CstDocument
    sheet_uuid: str


@dataclass(frozen=True, slots=True)
class _SheetLink:
    parent_path: Path
    child_path: Path
    node: CstList


@dataclass(frozen=True, slots=True)
class _SheetInstance:
    record: _FileRecord
    ref: SchematicObjectRef
    node: CstList
    parent_sheet_uuid: str | None
    location_record: _FileRecord


@dataclass(frozen=True, slots=True)
class _LibraryPin:
    name: str
    position: Point


_WirePrimitive = tuple[CstList, str | None, tuple[Point, ...]]
_JunctionPrimitive = tuple[CstList, str | None, Point]
_GraphLabel = tuple[SchematicObjectRef, CstList, Point, str]
_GraphObject = tuple[SchematicObjectRef, CstList, Point]


def object_ref_key(reference: SchematicObjectRef) -> str:
    pin = "" if reference.pin_number is None else f":{reference.pin_number}"
    return (
        f"{reference.kind}:{reference.sheet_uuid}:"
        f"{reference.object_uuid}{pin}"
    )


def inspect_schematic(project: Path) -> SchematicDocument:
    return parse_schematic(project).document


def parse_schematic(project: Path) -> ParsedSchematic:
    project_root = Path(project)
    if not project_root.is_dir():
        raise KicadSemanticError("project must be a directory")
    if project_root.is_symlink():
        raise KicadSemanticError("project directory must not be a link")
    project_root = project_root.resolve()

    paths = _schematic_paths(project_root)
    records = {path: _parse_file(path, project_root) for path in paths}
    links = _resolve_hierarchy(records, project_root)
    root_paths = sorted(
        set(records) - {link.child_path for link in links},
        key=lambda path: path.as_posix(),
    )
    if len(root_paths) != 1:
        raise KicadSemanticError("schematic hierarchy requires exactly one root")
    root_path = root_paths[0]
    instances = _sheet_instances(root_path, records, links)

    locations: dict[str, CstLocation] = {}
    sheet_files: dict[str, Path] = {}
    sheets: list[Sheet] = []
    symbols: list[Symbol] = []
    labels: list[Label] = []
    nets: list[NetConnectivity] = []
    footprints: list[FootprintAssignment] = []

    for instance in instances:
        record = instance.record
        sheet_ref = instance.ref
        sheet_node = instance.node
        parent_sheet_uuid = instance.parent_sheet_uuid
        if parent_sheet_uuid is None:
            parent_sheet_uuid = None
            name = record.path.stem
            ports: tuple[HierarchicalPort, ...] = ()
        else:
            name = _sheet_name(sheet_node, record.path.stem)
            ports = _extract_ports(
                sheet_node, instance.location_record, parent_sheet_uuid, locations
            )
        locations[object_ref_key(sheet_ref)] = CstLocation(
            file_path=instance.location_record.path,
            document=instance.location_record.cst,
            node=sheet_node,
        )
        sheet_files[object_ref_key(sheet_ref)] = record.path
        sheets.append(
            Sheet(
                ref=sheet_ref,
                name=name,
                file_name=record.relative_path,
                parent_sheet_uuid=parent_sheet_uuid,
                ports=ports,
            )
        )
        file_symbols, file_footprints = _extract_symbols(
            record, sheet_ref.object_uuid, locations
        )
        symbols.extend(file_symbols)
        footprints.extend(file_footprints)
        labels.extend(_extract_labels(record, sheet_ref.object_uuid, locations))
        nets.extend(_extract_nets(record, sheet_ref.object_uuid, locations))

    root_record = records[root_path]
    root_sheet_ref = instances[0].ref
    document = SchematicDocument(
        kicad_major=9,
        root_file=root_record.relative_path,
        root_sheet_ref=root_sheet_ref,
        sheets=tuple(sorted(sheets, key=lambda item: object_ref_key(item.ref))),
        symbols=tuple(sorted(symbols, key=lambda item: object_ref_key(item.ref))),
        labels=tuple(sorted(labels, key=lambda item: object_ref_key(item.ref))),
        nets=tuple(sorted(nets, key=lambda item: object_ref_key(item.ref))),
        footprints=tuple(
            sorted(footprints, key=lambda item: object_ref_key(item.symbol_ref))
        ),
    )
    return ParsedSchematic(
        document=document,
        locations=locations,
        sheet_files=sheet_files,
        aliases=_build_aliases(document, locations),
        property_aliases=_build_property_aliases(document, locations),
    )


def _schematic_paths(project_root: Path) -> tuple[Path, ...]:
    paths: list[Path] = []
    for path in project_root.rglob("*.kicad_sch"):
        if path.is_symlink():
            raise KicadSemanticError(f"schematic file must not be a link: {path}")
        resolved = path.resolve()
        if not resolved.is_relative_to(project_root):
            raise KicadSemanticError(f"schematic file escapes project: {path}")
        paths.append(resolved)
    if not paths:
        raise KicadSemanticError("project contains no .kicad_sch files")
    return tuple(sorted(set(paths), key=lambda path: path.as_posix()))


def _parse_file(path: Path, project_root: Path) -> _FileRecord:
    try:
        cst = parse_cst(path.read_bytes())
    except OSError as error:
        raise KicadSemanticError(f"cannot read schematic file: {path}") from error
    if cst.root.head != "kicad_sch":
        raise KicadSemanticError(f"not a KiCad schematic: {path}")
    version = _required_child(cst.root, "version", path).atom_text(1)
    if version != "20250114":
        raise KicadSemanticError(f"unsupported KiCad schematic version: {version}")
    sheet_uuid = _node_uuid(cst.root, path, "root sheet")
    return _FileRecord(
        path=path,
        relative_path=path.relative_to(project_root).as_posix(),
        cst=cst,
        sheet_uuid=sheet_uuid,
    )


def _resolve_hierarchy(
    records: dict[Path, _FileRecord], project_root: Path
) -> tuple[_SheetLink, ...]:
    links: list[_SheetLink] = []
    for record in records.values():
        for sheet_node in record.cst.root.find_children("sheet"):
            file_name = _property_value(sheet_node, "Sheetfile", required=True)
            assert file_name is not None
            child_path = _child_path(record.path, file_name, project_root)
            if child_path not in records:
                raise KicadSemanticError(
                    f"hierarchical sheet is not a project schematic: {file_name}"
                )
            links.append(
                _SheetLink(
                    parent_path=record.path,
                    child_path=child_path,
                    node=sheet_node,
                )
            )
    return tuple(links)


def _child_path(parent: Path, file_name: str, project_root: Path) -> Path:
    candidate = Path(file_name)
    if (
        candidate.is_absolute()
        or ".." in candidate.parts
        or candidate.suffix != ".kicad_sch"
    ):
        raise KicadSemanticError(f"invalid hierarchical sheet file: {file_name}")
    candidate = parent.parent / candidate
    if not candidate.exists():
        raise KicadSemanticError(f"hierarchical sheet file does not exist: {file_name}")
    resolved = candidate.resolve()
    if not resolved.is_relative_to(project_root):
        raise KicadSemanticError(f"hierarchical sheet escapes project: {file_name}")
    relative = candidate.relative_to(project_root)
    current = project_root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise KicadSemanticError(f"hierarchical sheet file must not be a link: {file_name}")
    return resolved


def _sheet_instances(
    root: Path,
    records: dict[Path, _FileRecord],
    links: tuple[_SheetLink, ...],
) -> tuple[_SheetInstance, ...]:
    links_by_parent: dict[Path, list[_SheetLink]] = {path: [] for path in records}
    for link in links:
        links_by_parent[link.parent_path].append(link)
    root_record = records[root]
    root_ref = _reference("sheet", root_record.sheet_uuid, root_record.sheet_uuid)
    instances: list[_SheetInstance] = [
        _SheetInstance(
            record=root_record,
            ref=root_ref,
            node=root_record.cst.root,
            parent_sheet_uuid=None,
            location_record=root_record,
        )
    ]
    reachable: set[Path] = set()
    active: set[Path] = set()

    def visit(instance: _SheetInstance) -> None:
        path = instance.record.path
        if path in active:
            raise KicadSemanticError("schematic hierarchy contains a cycle")
        active.add(path)
        reachable.add(path)
        for link in links_by_parent[path]:
            child_record = records[link.child_path]
            child_uuid = _node_uuid(link.node, instance.record.path, "hierarchical sheet")
            child_instance = _SheetInstance(
                record=child_record,
                ref=_reference("sheet", instance.ref.object_uuid, child_uuid),
                node=link.node,
                parent_sheet_uuid=instance.ref.object_uuid,
                location_record=instance.record,
            )
            instances.append(child_instance)
            visit(child_instance)
        active.remove(path)

    visit(instances[0])
    if reachable != set(records):
        raise KicadSemanticError("schematic hierarchy is not connected to its root")
    return tuple(instances)


def _extract_ports(
    sheet_node: CstList,
    record: _FileRecord,
    parent_sheet_uuid: str,
    locations: dict[str, CstLocation],
) -> tuple[HierarchicalPort, ...]:
    ports: list[HierarchicalPort] = []
    for node in sheet_node.find_children("pin"):
        name = _atom(node, 1, record.path, "hierarchical port name")
        direction = _atom(node, 2, record.path, "hierarchical port direction")
        port_uuid = _node_uuid(node, record.path, "hierarchical port")
        ref = _reference("hierarchical_port", parent_sheet_uuid, port_uuid)
        locations[object_ref_key(ref)] = CstLocation(record.path, record.cst, node)
        ports.append(
            HierarchicalPort(
                ref=ref,
                name=name,
                direction=direction,
                position=_point(node, record.path),
            )
        )
    return tuple(sorted(ports, key=lambda item: object_ref_key(item.ref)))


def _extract_symbols(
    record: _FileRecord, sheet_uuid: str, locations: dict[str, CstLocation]
) -> tuple[tuple[Symbol, ...], tuple[FootprintAssignment, ...]]:
    symbols: list[Symbol] = []
    footprints: list[FootprintAssignment] = []
    for node in record.cst.root.find_children("symbol"):
        symbol_uuid = _node_uuid(node, record.path, "symbol")
        ref = _reference("symbol", sheet_uuid, symbol_uuid)
        locations[object_ref_key(ref)] = CstLocation(record.path, record.cst, node)
        properties: list[SymbolProperty] = []
        property_values: dict[str, str] = {}
        for property_node in node.find_children("property"):
            name = _atom(property_node, 1, record.path, "property name")
            value = _atom(property_node, 2, record.path, "property value")
            if name in property_values:
                raise KicadSemanticError(f"duplicate symbol property: {name}")
            property_values[name] = value
            properties.append(SymbolProperty(name=name, value=value))
            locations[_property_location_key(ref, name)] = CstLocation(
                record.path, record.cst, property_node
            )
        reference = _required_property(property_values, "Reference", record.path)
        value = _required_property(property_values, "Value", record.path)
        footprint = property_values.get("Footprint") or None
        pins: list[PinReference] = []
        for pin_node, number, pin_name, pin_position in _instance_pin_geometry(
            record, node
        ):
            pin_uuid = _node_uuid(pin_node, record.path, "pin")
            pin_ref = _reference("pin", sheet_uuid, pin_uuid, number)
            locations[object_ref_key(pin_ref)] = CstLocation(
                record.path, record.cst, pin_node
            )
            pins.append(
                PinReference(
                    ref=pin_ref,
                    symbol_ref=ref,
                    number=number,
                    name=pin_name,
                    position=pin_position,
                )
            )
        symbol = Symbol(
            ref=ref,
            library_id=_atom(_required_child(node, "lib_id", record.path), 1, record.path, "library id"),
            reference=reference,
            value=value,
            unit=_integer(_atom(_required_child(node, "unit", record.path), 1, record.path, "unit"), record.path, "unit"),
            position=_point(node, record.path),
            properties=tuple(sorted(properties, key=lambda item: (item.name, item.value))),
            footprint=footprint,
            pins=tuple(sorted(pins, key=lambda item: object_ref_key(item.ref))),
        )
        symbols.append(symbol)
        if footprint is not None:
            footprints.append(FootprintAssignment(symbol_ref=ref, library_id=footprint))
    return (
        tuple(sorted(symbols, key=lambda item: object_ref_key(item.ref))),
        tuple(sorted(footprints, key=lambda item: object_ref_key(item.symbol_ref))),
    )


def _extract_labels(
    record: _FileRecord, sheet_uuid: str, locations: dict[str, CstLocation]
) -> tuple[Label, ...]:
    labels: list[Label] = []
    for head, scope in (
        ("label", "local"),
        ("global_label", "global"),
        ("hierarchical_label", "hierarchical"),
    ):
        for node in record.cst.root.find_children(head):
            label_uuid = _node_uuid(node, record.path, "label")
            ref = _reference("label", sheet_uuid, label_uuid)
            locations[object_ref_key(ref)] = CstLocation(record.path, record.cst, node)
            labels.append(
                Label(
                    ref=ref,
                    name=_atom(node, 1, record.path, "label name"),
                    scope=scope,
                    target_ref=None,
                    position=_point(node, record.path),
                )
            )
    return tuple(sorted(labels, key=lambda item: object_ref_key(item.ref)))


def _extract_nets(
    record: _FileRecord, sheet_uuid: str, locations: dict[str, CstLocation]
) -> tuple[NetConnectivity, ...]:
    nets = _wire_nets(record, sheet_uuid, locations)
    for node in record.cst.root.find_children("net"):
        net_uuid = _node_uuid(node, record.path, "net")
        ref = _reference("net", sheet_uuid, net_uuid)
        locations[object_ref_key(ref)] = CstLocation(record.path, record.cst, node)
        members_node = _child(node, "members")
        members = () if members_node is None else tuple(
            _atom(members_node, index, record.path, "net member")
            for index in range(1, len(members_node.items))
        )
        name = _atom(node, 1, record.path, "net name") if len(node.items) > 1 else None
        nets.append(NetConnectivity(ref=ref, name=name, members=tuple(sorted(members))))
    net_keys = [object_ref_key(net.ref) for net in nets]
    if len(set(net_keys)) != len(net_keys):
        raise KicadSemanticError("duplicate net identity")
    return tuple(sorted(nets, key=lambda item: object_ref_key(item.ref)))


def _wire_nets(
    record: _FileRecord, sheet_uuid: str, locations: dict[str, CstLocation]
) -> list[NetConnectivity]:
    wires = _wire_primitives(record)
    if not wires:
        return []
    junctions = _junction_primitives(record)
    components = _wire_components(wires, junctions)
    labels = _graph_labels(record, sheet_uuid)
    pins = _graph_pins(record, sheet_uuid)
    ports = _graph_ports(record, sheet_uuid)
    return [
        _component_net(
            record,
            sheet_uuid,
            wires,
            indexes,
            junctions,
            labels,
            pins,
            ports,
            locations,
        )
        for indexes in components
    ]


def _wire_primitives(
    record: _FileRecord,
) -> tuple[_WirePrimitive, ...]:
    return tuple(
        (
            node,
            _optional_node_uuid(node, record.path, "wire"),
            _wire_points(node, record.path),
        )
        for node in record.cst.root.find_children("wire")
    )


def _junction_primitives(
    record: _FileRecord,
) -> tuple[_JunctionPrimitive, ...]:
    return tuple(
        (node, _optional_node_uuid(node, record.path, "junction"), _point(node, record.path))
        for node in record.cst.root.find_children("junction")
    )


def _wire_components(
    wires: tuple[_WirePrimitive, ...],
    junctions: tuple[_JunctionPrimitive, ...],
) -> tuple[tuple[int, ...], ...]:
    parents = list(range(len(wires)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def join(indexes: list[int]) -> None:
        for index in indexes[1:]:
            left, right = find(indexes[0]), find(index)
            if left != right:
                parents[right] = left

    endpoints: dict[Point, list[int]] = {}
    for index, (_, _, points) in enumerate(wires):
        for point in points:
            endpoints.setdefault(point, []).append(index)
    for indexes in endpoints.values():
        join(indexes)
    for _, _, point in junctions:
        join(
            [
                index
                for index, (_, _, points) in enumerate(wires)
                if _point_on_wire(point, points)
            ]
        )
    components: dict[int, list[int]] = {}
    for index in range(len(wires)):
        components.setdefault(find(index), []).append(index)
    return tuple(tuple(indexes) for indexes in components.values())


def _component_net(
    record: _FileRecord,
    sheet_uuid: str,
    wires: tuple[_WirePrimitive, ...],
    indexes: tuple[int, ...],
    junctions: tuple[_JunctionPrimitive, ...],
    labels: tuple[_GraphLabel, ...],
    pins: tuple[_GraphObject, ...],
    ports: tuple[_GraphObject, ...],
    locations: dict[str, CstLocation],
) -> NetConnectivity:
    component_wires = tuple(wires[index][2] for index in indexes)

    def contains(point: Point) -> bool:
        return any(_point_on_wire(point, wire_points) for wire_points in component_wires)

    component_junctions = tuple(junction for junction in junctions if contains(junction[2]))
    component_labels = tuple(label for label in labels if contains(label[2]))
    component_pins = tuple(pin for pin in pins if contains(pin[2]))
    component_ports = tuple(port for port in ports if contains(port[2]))
    anchor_uuid, anchor_node = _component_anchor(
        wires,
        indexes,
        component_junctions,
        component_labels,
        component_pins,
        component_ports,
    )
    ref = _reference("net", sheet_uuid, anchor_uuid)
    locations[object_ref_key(ref)] = CstLocation(record.path, record.cst, anchor_node)
    return NetConnectivity(
        ref=ref,
        name=_component_name(component_labels),
        members=_component_members(
            sheet_uuid,
            wires,
            indexes,
            component_junctions,
            component_labels,
            component_pins,
            component_ports,
        ),
    )


def _component_anchor(
    wires: tuple[_WirePrimitive, ...],
    indexes: tuple[int, ...],
    junctions: tuple[_JunctionPrimitive, ...],
    labels: tuple[_GraphLabel, ...],
    pins: tuple[_GraphObject, ...],
    ports: tuple[_GraphObject, ...],
) -> tuple[str, CstList]:
    candidates = [
        (uuid, node)
        for index in indexes
        for node, uuid, _ in (wires[index],)
        if uuid is not None
    ]
    candidates.extend((uuid, node) for node, uuid, _ in junctions if uuid is not None)
    candidates.extend((reference.object_uuid, node) for reference, node, _, _ in labels)
    if not candidates:
        candidates.extend((reference.object_uuid, node) for reference, node, _ in pins)
        candidates.extend((reference.object_uuid, node) for reference, node, _ in ports)
    if not candidates:
        raise KicadSemanticError("wire connectivity component has no UUID anchor")
    return min(candidates, key=lambda item: item[0])


def _component_name(
    labels: tuple[_GraphLabel, ...],
) -> str | None:
    names = sorted(name for _, _, _, name in labels)
    return names[0] if names else None


def _component_members(
    sheet_uuid: str,
    wires: tuple[_WirePrimitive, ...],
    indexes: tuple[int, ...],
    junctions: tuple[_JunctionPrimitive, ...],
    labels: tuple[_GraphLabel, ...],
    pins: tuple[_GraphObject, ...],
    ports: tuple[_GraphObject, ...],
) -> tuple[str, ...]:
    members = [
        f"wire:{sheet_uuid}:{uuid}"
        for index in indexes
        for _, uuid, _ in (wires[index],)
        if uuid is not None
    ]
    members.extend(f"junction:{sheet_uuid}:{uuid}" for _, uuid, _ in junctions if uuid)
    members.extend(object_ref_key(reference) for reference, _, _, _ in labels)
    members.extend(object_ref_key(reference) for reference, _, _ in pins)
    members.extend(object_ref_key(reference) for reference, _, _ in ports)
    return tuple(sorted(members))


def _graph_labels(
    record: _FileRecord, sheet_uuid: str
) -> tuple[_GraphLabel, ...]:
    labels: list[_GraphLabel] = []
    for head in ("label", "global_label", "hierarchical_label"):
        for node in record.cst.root.find_children(head):
            uuid = _node_uuid(node, record.path, "label")
            labels.append(
                (
                    _reference("label", sheet_uuid, uuid),
                    node,
                    _point(node, record.path),
                    _atom(node, 1, record.path, "label name"),
                )
            )
    return tuple(labels)


def _graph_pins(
    record: _FileRecord, sheet_uuid: str
) -> tuple[_GraphObject, ...]:
    pins: list[_GraphObject] = []
    for symbol_node in record.cst.root.find_children("symbol"):
        for pin_node, number, _, position in _instance_pin_geometry(record, symbol_node):
            uuid = _node_uuid(pin_node, record.path, "pin")
            pins.append(
                (
                    _reference("pin", sheet_uuid, uuid, number),
                    pin_node,
                    position,
                )
            )
    return tuple(pins)


def _instance_pin_geometry(
    record: _FileRecord, symbol_node: CstList
) -> tuple[tuple[CstList, str, str, Point], ...]:
    instance_pins = symbol_node.find_children("pin")
    if not instance_pins:
        return ()
    lib_id = _atom(
        _required_child(symbol_node, "lib_id", record.path),
        1,
        record.path,
        "library id",
    )
    unit = _integer(
        _atom(
            _required_child(symbol_node, "unit", record.path),
            1,
            record.path,
            "unit",
        ),
        record.path,
        "unit",
    )
    definitions = _library_pin_definitions(record, lib_id, unit)
    seen_numbers: set[str] = set()
    pins: list[tuple[CstList, str, str, Point]] = []
    for pin_node in instance_pins:
        number = _atom(pin_node, 1, record.path, "pin number")
        if number in seen_numbers:
            raise KicadSemanticError(f"duplicate instance pin {number}: {record.path}")
        seen_numbers.add(number)
        try:
            definition = definitions[number]
        except KeyError as error:
            raise KicadSemanticError(
                f"missing library pin {number} for {lib_id}: {record.path}"
            ) from error
        pins.append(
            (
                pin_node,
                number,
                definition.name,
                _transform_pin_point(definition.position, symbol_node, record.path),
            )
        )
    return tuple(pins)


def _library_pin_definitions(
    record: _FileRecord, lib_id: str, unit: int
) -> dict[str, _LibraryPin]:
    library_root = _library_symbol(record, lib_id)
    nested_symbols = _nested_symbol_nodes(library_root)
    unit_nodes = tuple(
        node
        for node in nested_symbols
        if _library_symbol_unit(node, lib_id) == unit
    )
    has_unit_specific_definitions = any(
        _library_symbol_unit(node, lib_id) is not None for node in nested_symbols
    )
    source_nodes = unit_nodes if has_unit_specific_definitions else (library_root,)
    definitions: dict[str, _LibraryPin] = {}
    for source in source_nodes:
        for pin_node in source.find_children("pin"):
            number = _atom(
                _required_child(pin_node, "number", record.path),
                1,
                record.path,
                "library pin number",
            )
            if number in definitions:
                raise KicadSemanticError(
                    f"duplicate library pin {number} for {lib_id}: {record.path}"
                )
            name_node = _child(pin_node, "name")
            name = (
                number
                if name_node is None
                else _atom(name_node, 1, record.path, "library pin name")
            )
            definitions[number] = _LibraryPin(
                name=name,
                position=_point(pin_node, record.path),
            )
    if not definitions:
        raise KicadSemanticError(
            f"missing pin definitions for {lib_id} unit {unit}: {record.path}"
        )
    return definitions


def _library_symbol(record: _FileRecord, lib_id: str) -> CstList:
    lib_symbols = _required_child(record.cst.root, "lib_symbols", record.path)
    matches = [
        node
        for node in lib_symbols.find_children("symbol")
        if _atom(node, 1, record.path, "library symbol name") == lib_id
    ]
    if len(matches) != 1:
        raise KicadSemanticError(
            f"expected one library symbol {lib_id}: {record.path}"
        )
    return matches[0]


def _nested_symbol_nodes(node: CstList) -> tuple[CstList, ...]:
    nodes: list[CstList] = [node]
    for child in node.find_children("symbol"):
        nodes.extend(_nested_symbol_nodes(child))
    return tuple(nodes)


def _library_symbol_unit(node: CstList, lib_id: str) -> int | None:
    name = _atom(node, 1, Path("<library>"), "library symbol name")
    if name == lib_id:
        return None
    parts = name.rsplit("_", 2)
    if len(parts) != 3 or parts[0] != lib_id:
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


def _transform_pin_point(local: Point, symbol_node: CstList, path: Path) -> Point:
    at = _required_child(symbol_node, "at", path)
    origin = Point(
        x=_number(_atom(at, 1, path, "symbol x coordinate"), path, "symbol x coordinate"),
        y=_number(_atom(at, 2, path, "symbol y coordinate"), path, "symbol y coordinate"),
    )
    angle = (
        0.0
        if len(at.items) < 4
        else _number(_atom(at, 3, path, "symbol rotation"), path, "symbol rotation")
    )
    x, y = local.x, local.y
    mirror = _child(symbol_node, "mirror")
    if mirror is not None:
        axis = _atom(mirror, 1, path, "mirror axis")
        if axis == "x":
            y = -y
        elif axis == "y":
            x = -x
        else:
            raise KicadSemanticError(f"unsupported mirror axis {axis}: {path}")
    radians = math.radians(angle)
    cosine = math.cos(radians)
    sine = math.sin(radians)
    return Point(
        x=_normal_coordinate(origin.x + x * cosine - y * sine),
        y=_normal_coordinate(origin.y + x * sine + y * cosine),
    )


def _normal_coordinate(value: float) -> float:
    return 0.0 if math.isclose(value, 0.0, abs_tol=1e-12) else round(value, 12)


def _graph_ports(
    record: _FileRecord, sheet_uuid: str
) -> tuple[_GraphObject, ...]:
    ports: list[_GraphObject] = []
    for sheet_node in record.cst.root.find_children("sheet"):
        for port_node in sheet_node.find_children("pin"):
            uuid = _node_uuid(port_node, record.path, "hierarchical port")
            ports.append(
                (
                    _reference("hierarchical_port", sheet_uuid, uuid),
                    port_node,
                    _point(port_node, record.path),
                )
            )
    return tuple(ports)


def _wire_points(node: CstList, path: Path) -> tuple[Point, ...]:
    points = _required_child(node, "pts", path).find_children("xy")
    if len(points) < 2:
        raise KicadSemanticError(f"wire requires at least two points: {path}")
    return tuple(
        Point(
            x=_number(_atom(point, 1, path, "wire x coordinate"), path, "wire x coordinate"),
            y=_number(_atom(point, 2, path, "wire y coordinate"), path, "wire y coordinate"),
        )
        for point in points
    )


def _point_on_wire(point: Point, wire_points: tuple[Point, ...]) -> bool:
    return any(
        _point_on_segment(point, start, end)
        for start, end in zip(wire_points, wire_points[1:])
    )


def _point_on_segment(point: Point, start: Point, end: Point) -> bool:
    tolerance = 1e-9
    cross = (point.x - start.x) * (end.y - start.y) - (point.y - start.y) * (end.x - start.x)
    if abs(cross) > tolerance:
        return False
    return (
        min(start.x, end.x) - tolerance <= point.x <= max(start.x, end.x) + tolerance
        and min(start.y, end.y) - tolerance <= point.y <= max(start.y, end.y) + tolerance
    )


def _sheet_name(node: CstList, default: str) -> str:
    return _property_value(node, "Sheetname", required=False) or default


def _property_value(node: CstList, name: str, required: bool) -> str | None:
    matching = [
        child
        for child in node.find_children("property")
        if len(child.items) >= 3 and _is_atom(child.items[1], name)
    ]
    if len(matching) > 1:
        raise KicadSemanticError(f"duplicate property: {name}")
    if not matching:
        if required:
            raise KicadSemanticError(f"missing property: {name}")
        return None
    return _atom(matching[0], 2, Path("<schematic>"), f"property {name}")


def _required_property(values: dict[str, str], name: str, path: Path) -> str:
    try:
        return values[name]
    except KeyError as error:
        raise KicadSemanticError(f"missing symbol property {name}: {path}") from error


def _required_child(node: CstList, head: str, path: Path) -> CstList:
    child = _child(node, head)
    if child is None:
        raise KicadSemanticError(f"missing {head}: {path}")
    return child


def _child(node: CstList, head: str) -> CstList | None:
    children = node.find_children(head)
    if len(children) > 1:
        raise KicadSemanticError(f"duplicate {head}")
    return children[0] if children else None


def _node_uuid(node: CstList, path: Path, description: str) -> str:
    uuid_node = _required_child(node, "uuid", path)
    value = _atom(uuid_node, 1, path, f"{description} uuid")
    try:
        canonical = str(UUID(value))
    except ValueError as error:
        raise KicadSemanticError(f"invalid {description} uuid: {path}") from error
    if canonical != value.lower():
        raise KicadSemanticError(f"non-canonical {description} uuid: {path}")
    return canonical


def _optional_node_uuid(node: CstList, path: Path, description: str) -> str | None:
    uuid_node = _child(node, "uuid")
    if uuid_node is None:
        return None
    value = _atom(uuid_node, 1, path, f"{description} uuid")
    try:
        canonical = str(UUID(value))
    except ValueError as error:
        raise KicadSemanticError(f"invalid {description} uuid: {path}") from error
    if canonical != value.lower():
        raise KicadSemanticError(f"non-canonical {description} uuid: {path}")
    return canonical


def _reference(
    kind: str, sheet_uuid: str, object_uuid: str, pin_number: str | None = None
) -> SchematicObjectRef:
    return SchematicObjectRef(
        kind=kind,
        sheet_uuid=sheet_uuid,
        object_uuid=object_uuid,
        pin_number=pin_number,
    )


def _point(node: CstList, path: Path) -> Point:
    at = _required_child(node, "at", path)
    x = _number(_atom(at, 1, path, "x coordinate"), path, "x coordinate")
    y = _number(_atom(at, 2, path, "y coordinate"), path, "y coordinate")
    return Point(x=x, y=y)


def _number(value: str, path: Path, description: str) -> float:
    try:
        number = float(value)
    except ValueError as error:
        raise KicadSemanticError(f"invalid {description}: {path}") from error
    if not math.isfinite(number):
        raise KicadSemanticError(f"non-finite {description}: {path}")
    return number


def _integer(value: str, path: Path, description: str) -> int:
    try:
        return int(value)
    except ValueError as error:
        raise KicadSemanticError(f"invalid {description}: {path}") from error


def _atom(node: CstList, index: int, path: Path, description: str) -> str:
    try:
        return node.atom_text(index)
    except (IndexError, ValueError) as error:
        raise KicadSemanticError(f"missing {description}: {path}") from error


def _is_atom(node: CstNode, value: str) -> bool:
    return getattr(node, "value", None) == value


def _property_location_key(reference: SchematicObjectRef, name: str) -> str:
    return f"{object_ref_key(reference)}:property:{name}"


def _build_aliases(
    document: SchematicDocument, locations: dict[str, CstLocation]
) -> dict[str, tuple[SchematicObjectRef, ...]]:
    references: list[SchematicObjectRef] = [document.root_sheet_ref]
    references.extend(sheet.ref for sheet in document.sheets)
    references.extend(port.ref for sheet in document.sheets for port in sheet.ports)
    references.extend(symbol.ref for symbol in document.symbols)
    references.extend(pin.ref for symbol in document.symbols for pin in symbol.pins)
    references.extend(label.ref for label in document.labels)
    references.extend(net.ref for net in document.nets)
    groups: dict[tuple[Path, int, int], list[SchematicObjectRef]] = {}
    for reference in references:
        key = object_ref_key(reference)
        location = locations.get(key)
        if location is None:
            raise KicadSemanticError(f"missing location for semantic object: {key}")
        groups.setdefault(
            (location.file_path, location.node.start, location.node.end), []
        ).append(reference)
    aliases: dict[str, tuple[SchematicObjectRef, ...]] = {}
    for group in groups.values():
        stable_group = tuple(sorted(set(group), key=object_ref_key))
        for reference in stable_group:
            aliases[object_ref_key(reference)] = stable_group
    return aliases


def _build_property_aliases(
    document: SchematicDocument, locations: dict[str, CstLocation]
) -> dict[str, tuple[SchematicObjectRef, ...]]:
    groups: dict[tuple[Path, int, int], list[tuple[str, SchematicObjectRef]]] = {}
    for symbol in document.symbols:
        for property_ in symbol.properties:
            key = _property_location_key(symbol.ref, property_.name)
            location = locations.get(key)
            if location is None:
                raise KicadSemanticError(f"missing property location: {key}")
            groups.setdefault(
                (location.file_path, location.node.start, location.node.end), []
            ).append((key, symbol.ref))
    aliases: dict[str, tuple[SchematicObjectRef, ...]] = {}
    for group in groups.values():
        references = tuple(
            reference
            for _, reference in sorted(group, key=lambda item: object_ref_key(item[1]))
        )
        for key, _ in group:
            aliases[key] = references
    return aliases

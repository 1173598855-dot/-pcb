from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Self

from pcbflow.canonical import canonical_json_bytes, sha256_digest

JsonObject = dict[str, object]


def _require_exact_fields(
    value: object, expected: frozenset[str], context: str
) -> JsonObject:
    if type(value) is not dict:
        raise ValueError(f"{context} must be an object")
    keys = frozenset(value)
    unexpected = sorted(keys - expected)
    if unexpected:
        raise ValueError(f"{context} has unexpected fields: {', '.join(unexpected)}")
    missing = sorted(expected - keys)
    if missing:
        raise ValueError(f"{context} is missing fields: {', '.join(missing)}")
    return value


def _require_list(value: object, context: str) -> list[object]:
    if type(value) is not list:
        raise ValueError(f"{context} must be an array")
    return value


def _require_string(value: object, context: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{context} must be a non-empty canonical string")
    return value


def _require_bool(value: object, context: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{context} must be a boolean")
    return value


def _require_int(
    value: object,
    context: str,
    *,
    minimum: int | None = None,
    micrometres: bool = False,
) -> int:
    if type(value) is not int:
        unit = " integer micrometres" if micrometres else "n integer"
        raise ValueError(f"{context} must be a{unit}")
    if minimum is not None and value < minimum:
        raise ValueError(f"{context} must be at least {minimum}")
    return value


def _decode_json(data: bytes, context: str) -> JsonObject:
    if type(data) is not bytes:
        raise TypeError(f"{context} JSON input must be bytes")

    def unique_object(pairs: list[tuple[str, object]]) -> JsonObject:
        result: JsonObject = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(data, object_pairs_hook=unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid {context} JSON") from error
    if type(value) is not dict:
        raise ValueError(f"{context} must be a JSON object")
    return value


def _freeze_json(value: object) -> object:
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise ValueError("opaque payload keys must be strings")
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if type(value) is list:
        return tuple(_freeze_json(item) for item in value)
    if value is None or type(value) in {str, int, bool}:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise ValueError("opaque payload must contain only canonical JSON values")


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _canonicalize_object_lists(
    value: JsonObject, fields: tuple[str, ...]
) -> JsonObject:
    canonical = dict(value)
    for field in fields:
        collection = canonical[field]
        if type(collection) is not list:
            raise TypeError(f"canonical collection {field} must be a list")
        canonical[field] = sorted(
            collection,
            key=lambda item: item["id"] if type(item) is dict else "",
        )
    return canonical


class BoardObjectId(str):
    __slots__ = ()

    def __new__(cls, value: str) -> Self:
        _require_string(value, "board object id")
        if len(value) > 128:
            raise ValueError("board object id is too long")
        return str.__new__(cls, value)

    @property
    def value(self) -> str:
        return str(self)


def _object_id(value: object, context: str) -> BoardObjectId:
    return BoardObjectId(_require_string(value, context))


@dataclass(frozen=True, slots=True)
class PointUm:
    x: int
    y: int

    def __post_init__(self) -> None:
        _require_int(self.x, "point x", micrometres=True)
        _require_int(self.y, "point y", micrometres=True)


@dataclass(frozen=True, slots=True)
class RectUm:
    x: int
    y: int
    width: int
    height: int

    def __post_init__(self) -> None:
        _require_int(self.x, "rectangle x", micrometres=True)
        _require_int(self.y, "rectangle y", micrometres=True)
        _require_int(self.width, "rectangle width", minimum=1, micrometres=True)
        _require_int(self.height, "rectangle height", minimum=1, micrometres=True)

    @property
    def area_um2(self) -> int:
        return self.width * self.height

    def contains(self, point: PointUm) -> bool:
        return (
            self.x <= point.x <= self.x + self.width
            and self.y <= point.y <= self.y + self.height
        )

    def intersects(self, other: RectUm) -> bool:
        if not isinstance(other, RectUm):
            raise TypeError("rectangle intersection requires RectUm")
        return not (
            self.x + self.width < other.x
            or other.x + other.width < self.x
            or self.y + self.height < other.y
            or other.y + other.height < self.y
        )


@dataclass(frozen=True, slots=True)
class NetClass:
    id: BoardObjectId
    min_width_um: int
    clearance_um: int
    min_via_diameter_um: int
    min_via_hole_um: int

    def __post_init__(self) -> None:
        if not isinstance(self.id, BoardObjectId):
            raise TypeError("net class id must be a BoardObjectId")
        _require_int(self.min_width_um, "minimum width", minimum=1, micrometres=True)
        _require_int(self.clearance_um, "clearance", minimum=0, micrometres=True)
        _require_int(
            self.min_via_diameter_um,
            "minimum via diameter",
            minimum=1,
            micrometres=True,
        )
        _require_int(
            self.min_via_hole_um,
            "minimum via hole",
            minimum=1,
            micrometres=True,
        )
        if self.min_via_hole_um >= self.min_via_diameter_um:
            raise ValueError("minimum via hole must be smaller than via diameter")


@dataclass(frozen=True, slots=True)
class Net:
    id: BoardObjectId
    net_class: BoardObjectId

    def __post_init__(self) -> None:
        if not isinstance(self.id, BoardObjectId) or not isinstance(
            self.net_class, BoardObjectId
        ):
            raise TypeError("net ids must be BoardObjectId values")


@dataclass(frozen=True, slots=True)
class Keepout:
    id: BoardObjectId
    kind: str
    bounds: RectUm
    layers: tuple[str, ...]
    prohibited: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.id, BoardObjectId) or not isinstance(self.bounds, RectUm):
            raise TypeError("keepout id and bounds have invalid types")
        _require_string(self.kind, "keepout kind")
        _validate_string_tuple(self.layers, "keepout layers")
        _validate_string_tuple(self.prohibited, "keepout prohibitions")


@dataclass(frozen=True, slots=True)
class Footprint:
    id: BoardObjectId
    position: PointUm
    width_um: int
    height_um: int
    layer: str
    pad_ids: tuple[BoardObjectId, ...]
    keepout_ids: tuple[BoardObjectId, ...]
    placement_lock: bool

    def __post_init__(self) -> None:
        if not isinstance(self.id, BoardObjectId) or not isinstance(
            self.position, PointUm
        ):
            raise TypeError("footprint id and position have invalid types")
        _require_int(self.width_um, "footprint width", minimum=1, micrometres=True)
        _require_int(self.height_um, "footprint height", minimum=1, micrometres=True)
        _require_string(self.layer, "footprint layer")
        _validate_id_tuple(self.pad_ids, "footprint pad ids")
        _validate_id_tuple(self.keepout_ids, "footprint keepout ids")
        _require_bool(self.placement_lock, "footprint placement lock")


@dataclass(frozen=True, slots=True)
class ThermalPolicy:
    """A locked copper connection policy carried by its target pad."""

    pad_id: BoardObjectId
    net_id: BoardObjectId
    layers: tuple[str, ...]
    style: str
    spoke_count: int
    spoke_width_um: int
    gap_um: int
    locked: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.pad_id, BoardObjectId) or not isinstance(
            self.net_id, BoardObjectId
        ):
            raise TypeError("thermal policy ids must be BoardObjectId values")
        _validate_string_tuple(self.layers, "thermal policy layers")
        if self.style not in {"thermal_relief", "solid"}:
            raise ValueError("thermal policy style is unsupported")
        if type(self.spoke_count) is not int or self.spoke_count < 1:
            raise ValueError("thermal policy spoke count must be at least 1")
        if type(self.spoke_width_um) is not int or self.spoke_width_um < 1:
            raise ValueError("thermal policy spoke width must be positive")
        if type(self.gap_um) is not int or self.gap_um < 0:
            raise ValueError("thermal policy gap must be non-negative")
        if type(self.locked) is not bool:
            raise TypeError("thermal policy lock must be a boolean")


@dataclass(frozen=True, slots=True)
class Pad:
    id: BoardObjectId
    footprint_id: BoardObjectId
    net_id: BoardObjectId | None
    position: PointUm
    size_x_um: int
    size_y_um: int
    hole_diameter_um: int
    layers: tuple[str, ...]
    thermal_policy: ThermalPolicy | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.id, BoardObjectId) or not isinstance(
            self.footprint_id, BoardObjectId
        ):
            raise TypeError("pad ids must be BoardObjectId values")
        if self.net_id is not None and not isinstance(self.net_id, BoardObjectId):
            raise TypeError("pad net id must be a BoardObjectId or None")
        if not isinstance(self.position, PointUm):
            raise TypeError("pad position must be PointUm")
        _require_int(self.size_x_um, "pad x size", minimum=1, micrometres=True)
        _require_int(self.size_y_um, "pad y size", minimum=1, micrometres=True)
        _require_int(self.hole_diameter_um, "pad hole", minimum=0, micrometres=True)
        if self.hole_diameter_um > min(self.size_x_um, self.size_y_um):
            raise ValueError("pad hole cannot exceed pad size")
        _validate_string_tuple(self.layers, "pad layers")
        if self.thermal_policy is not None:
            if not isinstance(self.thermal_policy, ThermalPolicy):
                raise TypeError("pad thermal policy must be a ThermalPolicy or None")
            if self.thermal_policy.pad_id != self.id:
                raise ValueError("thermal policy must target its containing pad")
            if self.net_id != self.thermal_policy.net_id:
                raise ValueError("thermal policy net must match its containing pad")
            if not set(self.thermal_policy.layers) <= set(self.layers):
                raise ValueError("thermal policy layers must belong to its containing pad")
            if not self.thermal_policy.locked:
                raise ValueError("pad thermal policy must be locked")


@dataclass(frozen=True, slots=True)
class RouteSegment:
    id: BoardObjectId
    net_id: BoardObjectId
    start: PointUm
    end: PointUm
    width_um: int
    layer: str
    route_lock: bool

    def __post_init__(self) -> None:
        if not isinstance(self.id, BoardObjectId) or not isinstance(
            self.net_id, BoardObjectId
        ):
            raise TypeError("route ids must be BoardObjectId values")
        if not isinstance(self.start, PointUm) or not isinstance(self.end, PointUm):
            raise TypeError("route endpoints must be PointUm")
        if self.start == self.end:
            raise ValueError("route segment endpoints must differ")
        _require_int(self.width_um, "route width", minimum=1, micrometres=True)
        _require_string(self.layer, "route layer")
        _require_bool(self.route_lock, "route lock")

    @property
    def angle_degrees(self) -> int:
        """The undirected segment orientation, stable modulo 180 degrees."""
        angle = math.degrees(math.atan2(self.end.y - self.start.y, self.end.x - self.start.x))
        return round(angle) % 180


@dataclass(frozen=True, slots=True)
class Via:
    id: BoardObjectId
    net_id: BoardObjectId
    position: PointUm
    diameter_um: int
    hole_diameter_um: int
    layers: tuple[str, ...]
    route_lock: bool

    def __post_init__(self) -> None:
        if not isinstance(self.id, BoardObjectId) or not isinstance(
            self.net_id, BoardObjectId
        ):
            raise TypeError("via ids must be BoardObjectId values")
        if not isinstance(self.position, PointUm):
            raise TypeError("via position must be PointUm")
        _require_int(self.diameter_um, "via diameter", minimum=1, micrometres=True)
        _require_int(self.hole_diameter_um, "via hole", minimum=1, micrometres=True)
        if self.hole_diameter_um >= self.diameter_um:
            raise ValueError("via hole must be smaller than via diameter")
        _validate_string_tuple(self.layers, "via layers")
        _require_bool(self.route_lock, "via route lock")


@dataclass(frozen=True, slots=True)
class CopperZone:
    id: BoardObjectId
    net_id: BoardObjectId
    layer: str
    bounds: RectUm
    clearance_um: int
    route_lock: bool

    def __post_init__(self) -> None:
        if not isinstance(self.id, BoardObjectId) or not isinstance(
            self.net_id, BoardObjectId
        ):
            raise TypeError("copper zone ids must be BoardObjectId values")
        _require_string(self.layer, "copper zone layer")
        if not isinstance(self.bounds, RectUm):
            raise TypeError("copper zone bounds must be RectUm")
        _require_int(self.clearance_um, "zone clearance", minimum=0, micrometres=True)
        _require_bool(self.route_lock, "copper zone route lock")


@dataclass(frozen=True, slots=True)
class OpaqueNode:
    id: BoardObjectId
    native_type: str
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        if not isinstance(self.id, BoardObjectId):
            raise TypeError("opaque node id must be a BoardObjectId")
        _require_string(self.native_type, "opaque native type")
        if not isinstance(self.payload, Mapping):
            raise TypeError("opaque payload must be a mapping")
        object.__setattr__(self, "payload", _freeze_json(dict(self.payload)))


@dataclass(frozen=True, slots=True)
class BoardSnapshot:
    schema_version: str
    profile_id: str
    copper_oz: int
    outline: tuple[PointUm, ...]
    layers: tuple[str, ...]
    net_classes: tuple[NetClass, ...]
    nets: tuple[Net, ...]
    keepouts: tuple[Keepout, ...]
    footprints: tuple[Footprint, ...]
    pads: tuple[Pad, ...]
    routes: tuple[RouteSegment, ...]
    vias: tuple[Via, ...]
    copper_zones: tuple[CopperZone, ...]
    opaque_nodes: tuple[OpaqueNode, ...]

    def __post_init__(self) -> None:
        if self.schema_version != "1.0":
            raise ValueError("unsupported BoardIR schema version")
        _require_string(self.profile_id, "board profile id")
        _require_int(self.copper_oz, "copper weight", minimum=1)
        if type(self.outline) is not tuple or len(self.outline) < 4:
            raise ValueError("board outline requires at least four points")
        if any(not isinstance(point, PointUm) for point in self.outline):
            raise TypeError("board outline points must be PointUm")
        _validate_string_tuple(self.layers, "board layers")
        self._validate_collections()
        self._validate_native_ids()
        self._validate_geometry()
        self._validate_references()

    @classmethod
    def load_json(cls, data: bytes) -> Self:
        value = _require_exact_fields(
            _decode_json(data, "BoardIR"),
            frozenset(
                {
                    "schema_version",
                    "profile_id",
                    "copper_oz",
                    "outline",
                    "layers",
                    "net_classes",
                    "nets",
                    "keepouts",
                    "footprints",
                    "pads",
                    "routes",
                    "vias",
                    "copper_zones",
                    "opaque_nodes",
                }
            ),
            "BoardIR snapshot",
        )
        return cls(
            schema_version=_require_string(value["schema_version"], "schema version"),
            profile_id=_require_string(value["profile_id"], "profile id"),
            copper_oz=_require_int(value["copper_oz"], "copper weight", minimum=1),
            outline=tuple(
                _parse_point(item, "outline point")
                for item in _require_list(value["outline"], "outline")
            ),
            layers=_parse_string_tuple(value["layers"], "layers"),
            net_classes=tuple(
                _parse_net_class(item)
                for item in _require_list(value["net_classes"], "net classes")
            ),
            nets=tuple(
                _parse_net(item) for item in _require_list(value["nets"], "nets")
            ),
            keepouts=tuple(
                _parse_keepout(item)
                for item in _require_list(value["keepouts"], "keepouts")
            ),
            footprints=tuple(
                _parse_footprint(item)
                for item in _require_list(value["footprints"], "footprints")
            ),
            pads=tuple(
                _parse_pad(item) for item in _require_list(value["pads"], "pads")
            ),
            routes=tuple(
                _parse_route(item)
                for item in _require_list(value["routes"], "routes")
            ),
            vias=tuple(
                _parse_via(item) for item in _require_list(value["vias"], "vias")
            ),
            copper_zones=tuple(
                _parse_copper_zone(item)
                for item in _require_list(value["copper_zones"], "copper zones")
            ),
            opaque_nodes=tuple(
                _parse_opaque_node(item)
                for item in _require_list(value["opaque_nodes"], "opaque nodes")
            ),
        )

    @property
    def board_size_um(self) -> tuple[int, int]:
        min_x, min_y, max_x, max_y = self._bounds()
        return max_x - min_x, max_y - min_y

    def footprint(self, object_id: str | BoardObjectId) -> Footprint:
        return _find_by_id(self.footprints, object_id, "footprint")

    def net(self, object_id: str | BoardObjectId) -> Net:
        return _find_by_id(self.nets, object_id, "net")

    def net_class(self, object_id: str | BoardObjectId) -> NetClass:
        return _find_by_id(self.net_classes, object_id, "net class")

    def keepout(self, object_id: str | BoardObjectId) -> Keepout:
        return _find_by_id(self.keepouts, object_id, "keepout")

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(
            _canonicalize_object_lists(
                self.to_canonical_dict(),
                (
                    "net_classes",
                    "nets",
                    "keepouts",
                    "footprints",
                    "pads",
                    "routes",
                    "vias",
                    "copper_zones",
                    "opaque_nodes",
                ),
            )
        )

    def canonical_digest(self) -> str:
        return sha256_digest(self.canonical_bytes())

    def validate_proposed(self, proposed: BoardSnapshot) -> None:
        from pcbflow.board.validation import validate_proposed_snapshot

        validate_proposed_snapshot(self, proposed)

    def to_canonical_dict(self) -> JsonObject:
        return {
            "schema_version": self.schema_version,
            "profile_id": self.profile_id,
            "copper_oz": self.copper_oz,
            "outline": [_point_dict(item) for item in self.outline],
            "layers": list(self.layers),
            "net_classes": [_net_class_dict(item) for item in self.net_classes],
            "nets": [
                {"id": str(item.id), "net_class": str(item.net_class)}
                for item in self.nets
            ],
            "keepouts": [
                {
                    "id": str(item.id),
                    "kind": item.kind,
                    "bounds": _rect_dict(item.bounds),
                    "layers": list(item.layers),
                    "prohibited": list(item.prohibited),
                }
                for item in self.keepouts
            ],
            "footprints": [
                {
                    "id": str(item.id),
                    "position": _point_dict(item.position),
                    "width_um": item.width_um,
                    "height_um": item.height_um,
                    "layer": item.layer,
                    "pad_ids": [str(value) for value in item.pad_ids],
                    "keepout_ids": [str(value) for value in item.keepout_ids],
                    "placement_lock": item.placement_lock,
                }
                for item in self.footprints
            ],
            "pads": [_pad_dict(item) for item in self.pads],
            "routes": [
                {
                    "id": str(item.id),
                    "net_id": str(item.net_id),
                    "start": _point_dict(item.start),
                    "end": _point_dict(item.end),
                    "width_um": item.width_um,
                    "layer": item.layer,
                    "route_lock": item.route_lock,
                }
                for item in self.routes
            ],
            "vias": [
                {
                    "id": str(item.id),
                    "net_id": str(item.net_id),
                    "position": _point_dict(item.position),
                    "diameter_um": item.diameter_um,
                    "hole_diameter_um": item.hole_diameter_um,
                    "layers": list(item.layers),
                    "route_lock": item.route_lock,
                }
                for item in self.vias
            ],
            "copper_zones": [
                {
                    "id": str(item.id),
                    "net_id": str(item.net_id),
                    "layer": item.layer,
                    "bounds": _rect_dict(item.bounds),
                    "clearance_um": item.clearance_um,
                    "route_lock": item.route_lock,
                }
                for item in self.copper_zones
            ],
            "opaque_nodes": [
                {
                    "id": str(item.id),
                    "native_type": item.native_type,
                    "payload": _thaw_json(item.payload),
                }
                for item in self.opaque_nodes
            ],
        }

    def _bounds(self) -> tuple[int, int, int, int]:
        xs = [point.x for point in self.outline]
        ys = [point.y for point in self.outline]
        return min(xs), min(ys), max(xs), max(ys)

    def _validate_collections(self) -> None:
        typed_collections: tuple[tuple[object, type[object], str], ...] = (
            (self.net_classes, NetClass, "net classes"),
            (self.nets, Net, "nets"),
            (self.keepouts, Keepout, "keepouts"),
            (self.footprints, Footprint, "footprints"),
            (self.pads, Pad, "pads"),
            (self.routes, RouteSegment, "routes"),
            (self.vias, Via, "vias"),
            (self.copper_zones, CopperZone, "copper zones"),
            (self.opaque_nodes, OpaqueNode, "opaque nodes"),
        )
        for collection, expected_type, context in typed_collections:
            if type(collection) is not tuple or any(
                not isinstance(item, expected_type) for item in collection
            ):
                raise TypeError(f"{context} must be a tuple of {expected_type.__name__}")

    def _validate_native_ids(self) -> None:
        seen: set[BoardObjectId] = set()
        for collection in (
            self.net_classes,
            self.nets,
            self.keepouts,
            self.footprints,
            self.pads,
            self.routes,
            self.vias,
            self.copper_zones,
            self.opaque_nodes,
        ):
            for item in collection:
                if item.id in seen:
                    raise ValueError(f"duplicate native id: {item.id}")
                seen.add(item.id)

    def _validate_geometry(self) -> None:
        if not _is_simple_outline(self.outline):
            raise ValueError("board outline must be a simple polygon with positive area")

        def point_inside(point: PointUm, subject: BoardObjectId | str) -> None:
            if not _point_inside_outline(point, self.outline):
                raise ValueError(f"coordinate outside board outline: {subject}")

        def rect_inside(rect: RectUm, subject: BoardObjectId | str) -> None:
            if not _rect_inside_outline(rect, self.outline):
                raise ValueError(f"coordinate outside board outline: {subject}")

        for keepout in self.keepouts:
            rect_inside(keepout.bounds, keepout.id)
        for footprint in self.footprints:
            rect_inside(_footprint_bounds(footprint), footprint.id)
        for pad in self.pads:
            point_inside(pad.position, pad.id)
        for route in self.routes:
            if not _segment_inside_outline(route.start, route.end, self.outline):
                raise ValueError(f"coordinate outside board outline: {route.id}")
        for via in self.vias:
            point_inside(via.position, via.id)
        for zone in self.copper_zones:
            rect_inside(zone.bounds, zone.id)

    def _validate_references(self) -> None:
        layers = set(self.layers)
        net_class_ids = {item.id for item in self.net_classes}
        net_ids = {item.id for item in self.nets}
        keepout_ids = {item.id for item in self.keepouts}
        footprint_ids = {item.id for item in self.footprints}
        pad_by_id = {item.id: item for item in self.pads}

        def check_layers(values: tuple[str, ...], subject: BoardObjectId) -> None:
            invalid = set(values) - layers
            if invalid:
                raise ValueError(f"invalid layer for {subject}: {min(invalid)}")

        for net in self.nets:
            if net.net_class not in net_class_ids:
                raise ValueError(f"unknown net class for {net.id}: {net.net_class}")
        for keepout in self.keepouts:
            check_layers(keepout.layers, keepout.id)
        for footprint in self.footprints:
            check_layers((footprint.layer,), footprint.id)
            if not set(footprint.keepout_ids) <= keepout_ids:
                raise ValueError(f"unknown keepout referenced by {footprint.id}")
            for pad_id in footprint.pad_ids:
                pad = pad_by_id.get(pad_id)
                if pad is None or pad.footprint_id != footprint.id:
                    raise ValueError(f"invalid pad reference for {footprint.id}: {pad_id}")
        declared_pad_ids = {
            pad_id for footprint in self.footprints for pad_id in footprint.pad_ids
        }
        if declared_pad_ids != set(pad_by_id):
            raise ValueError("every pad must belong to exactly one footprint")
        for pad in self.pads:
            if pad.footprint_id not in footprint_ids:
                raise ValueError(f"unknown footprint for pad {pad.id}")
            if pad.net_id is not None and pad.net_id not in net_ids:
                raise ValueError(f"unknown net for pad {pad.id}")
            check_layers(pad.layers, pad.id)
        for route in self.routes:
            if route.net_id not in net_ids:
                raise ValueError(f"unknown net for route {route.id}")
            check_layers((route.layer,), route.id)
        for via in self.vias:
            if via.net_id not in net_ids:
                raise ValueError(f"unknown net for via {via.id}")
            check_layers(via.layers, via.id)
        for zone in self.copper_zones:
            if zone.net_id not in net_ids:
                raise ValueError(f"unknown net for copper zone {zone.id}")
            check_layers((zone.layer,), zone.id)


def _is_simple_outline(outline: tuple[PointUm, ...]) -> bool:
    if len(set(outline)) != len(outline):
        return False
    if sum(
        left.x * right.y - right.x * left.y
        for left, right in _polygon_edges(outline)
    ) == 0:
        return False
    edges = _polygon_edges(outline)
    for index, (left, right) in enumerate(edges):
        for other_index, (other_left, other_right) in enumerate(
            edges[index + 1 :], index + 1
        ):
            if (other_index - index) in {1, len(edges) - 1}:
                continue
            if _segments_intersect(left, right, other_left, other_right):
                return False
    return True


def _polygon_edges(
    outline: tuple[PointUm, ...],
) -> tuple[tuple[PointUm, PointUm], ...]:
    return tuple(zip(outline, outline[1:] + outline[:1], strict=True))


def _point_inside_outline(point: PointUm, outline: tuple[PointUm, ...]) -> bool:
    inside = False
    for left, right in _polygon_edges(outline):
        if _point_on_segment(point, left, right):
            return True
        if (left.y > point.y) != (right.y > point.y):
            cross = (right.x - left.x) * (point.y - left.y) - (point.x - left.x) * (
                right.y - left.y
            )
            if (cross > 0) == (right.y > left.y):
                inside = not inside
    return inside


def _segment_inside_outline(
    start: PointUm, end: PointUm, outline: tuple[PointUm, ...]
) -> bool:
    if not _point_inside_outline(start, outline) or not _point_inside_outline(
        end, outline
    ):
        return False
    return not any(
        _segments_intersect(start, end, left, right)
        and not (
            _point_on_segment(start, left, right)
            or _point_on_segment(end, left, right)
        )
        for left, right in _polygon_edges(outline)
    )


def _rect_inside_outline(rect: RectUm, outline: tuple[PointUm, ...]) -> bool:
    corners = (
        PointUm(rect.x, rect.y),
        PointUm(rect.x + rect.width, rect.y),
        PointUm(rect.x + rect.width, rect.y + rect.height),
        PointUm(rect.x, rect.y + rect.height),
    )
    return all(_point_inside_outline(point, outline) for point in corners) and all(
        _segment_inside_outline(left, right, outline)
        for left, right in zip(corners, corners[1:] + corners[:1], strict=True)
    )


def _footprint_bounds(footprint: Footprint) -> RectUm:
    half_width = (footprint.width_um + 1) // 2
    half_height = (footprint.height_um + 1) // 2
    return RectUm(
        footprint.position.x - half_width,
        footprint.position.y - half_height,
        2 * half_width,
        2 * half_height,
    )


def _point_on_segment(point: PointUm, left: PointUm, right: PointUm) -> bool:
    return (
        (right.x - left.x) * (point.y - left.y)
        == (right.y - left.y) * (point.x - left.x)
        and min(left.x, right.x) <= point.x <= max(left.x, right.x)
        and min(left.y, right.y) <= point.y <= max(left.y, right.y)
    )


def _segments_intersect(
    left: PointUm,
    right: PointUm,
    other_left: PointUm,
    other_right: PointUm,
) -> bool:
    def cross(origin: PointUm, first: PointUm, second: PointUm) -> int:
        return (first.x - origin.x) * (second.y - origin.y) - (first.y - origin.y) * (
            second.x - origin.x
        )

    first, second = (
        cross(left, right, other_left),
        cross(left, right, other_right),
    )
    third, fourth = (
        cross(other_left, other_right, left),
        cross(other_left, other_right, right),
    )
    return (
        (first == 0 and _point_on_segment(other_left, left, right))
        or (second == 0 and _point_on_segment(other_right, left, right))
        or (third == 0 and _point_on_segment(left, other_left, other_right))
        or (fourth == 0 and _point_on_segment(right, other_left, other_right))
        or ((first > 0) != (second > 0) and (third > 0) != (fourth > 0))
    )


def _validate_string_tuple(values: tuple[str, ...], context: str) -> None:
    if type(values) is not tuple or not values:
        raise ValueError(f"{context} must be a non-empty tuple")
    if any(type(item) is not str or not item or item != item.strip() for item in values):
        raise ValueError(f"{context} must contain canonical strings")
    if len(values) != len(set(values)):
        raise ValueError(f"{context} must be unique")


def _validate_id_tuple(values: tuple[BoardObjectId, ...], context: str) -> None:
    if type(values) is not tuple or any(
        not isinstance(item, BoardObjectId) for item in values
    ):
        raise TypeError(f"{context} must contain BoardObjectId values")
    if len(values) != len(set(values)):
        raise ValueError(f"{context} must be unique")


def _parse_string_tuple(value: object, context: str) -> tuple[str, ...]:
    return tuple(
        _require_string(item, f"{context} item")
        for item in _require_list(value, context)
    )


def _parse_id_tuple(value: object, context: str) -> tuple[BoardObjectId, ...]:
    return tuple(
        _object_id(item, f"{context} item") for item in _require_list(value, context)
    )


def _parse_point(value: object, context: str) -> PointUm:
    item = _require_exact_fields(value, frozenset({"x", "y"}), context)
    return PointUm(
        _require_int(item["x"], f"{context} x", micrometres=True),
        _require_int(item["y"], f"{context} y", micrometres=True),
    )


def _parse_rect(value: object, context: str) -> RectUm:
    item = _require_exact_fields(
        value, frozenset({"x", "y", "width", "height"}), context
    )
    return RectUm(
        x=_require_int(item["x"], f"{context} x", micrometres=True),
        y=_require_int(item["y"], f"{context} y", micrometres=True),
        width=_require_int(
            item["width"], f"{context} width", minimum=1, micrometres=True
        ),
        height=_require_int(
            item["height"], f"{context} height", minimum=1, micrometres=True
        ),
    )


def _parse_net_class(value: object) -> NetClass:
    item = _require_exact_fields(
        value,
        frozenset(
            {
                "id",
                "min_width_um",
                "clearance_um",
                "min_via_diameter_um",
                "min_via_hole_um",
            }
        ),
        "net class",
    )
    return NetClass(
        id=_object_id(item["id"], "net class id"),
        min_width_um=_require_int(
            item["min_width_um"], "minimum width", minimum=1, micrometres=True
        ),
        clearance_um=_require_int(
            item["clearance_um"], "clearance", minimum=0, micrometres=True
        ),
        min_via_diameter_um=_require_int(
            item["min_via_diameter_um"],
            "minimum via diameter",
            minimum=1,
            micrometres=True,
        ),
        min_via_hole_um=_require_int(
            item["min_via_hole_um"],
            "minimum via hole",
            minimum=1,
            micrometres=True,
        ),
    )


def _parse_net(value: object) -> Net:
    item = _require_exact_fields(value, frozenset({"id", "net_class"}), "net")
    return Net(
        id=_object_id(item["id"], "net id"),
        net_class=_object_id(item["net_class"], "net class reference"),
    )


def _parse_keepout(value: object) -> Keepout:
    item = _require_exact_fields(
        value,
        frozenset({"id", "kind", "bounds", "layers", "prohibited"}),
        "keepout",
    )
    return Keepout(
        id=_object_id(item["id"], "keepout id"),
        kind=_require_string(item["kind"], "keepout kind"),
        bounds=_parse_rect(item["bounds"], "keepout bounds"),
        layers=_parse_string_tuple(item["layers"], "keepout layers"),
        prohibited=_parse_string_tuple(item["prohibited"], "keepout prohibitions"),
    )


def _parse_footprint(value: object) -> Footprint:
    item = _require_exact_fields(
        value,
        frozenset(
            {
                "id",
                "position",
                "width_um",
                "height_um",
                "layer",
                "pad_ids",
                "keepout_ids",
                "placement_lock",
            }
        ),
        "footprint",
    )
    return Footprint(
        id=_object_id(item["id"], "footprint id"),
        position=_parse_point(item["position"], "footprint position"),
        width_um=_require_int(
            item["width_um"], "footprint width", minimum=1, micrometres=True
        ),
        height_um=_require_int(
            item["height_um"], "footprint height", minimum=1, micrometres=True
        ),
        layer=_require_string(item["layer"], "footprint layer"),
        pad_ids=_parse_id_tuple(item["pad_ids"], "footprint pad ids"),
        keepout_ids=_parse_id_tuple(item["keepout_ids"], "footprint keepout ids"),
        placement_lock=_require_bool(item["placement_lock"], "placement lock"),
    )


def _parse_pad(value: object) -> Pad:
    expected = frozenset(
        {
            "id",
            "footprint_id",
            "net_id",
            "position",
            "size_x_um",
            "size_y_um",
            "hole_diameter_um",
            "layers",
        }
    )
    raw = value if type(value) is dict else None
    item = _require_exact_fields(
        value,
        expected | ({"thermal_policy"} if raw is not None and "thermal_policy" in raw else set()),
        "pad",
    )
    raw_net_id = item["net_id"]
    raw_thermal_policy = item.get("thermal_policy")
    return Pad(
        id=_object_id(item["id"], "pad id"),
        footprint_id=_object_id(item["footprint_id"], "pad footprint id"),
        net_id=None if raw_net_id is None else _object_id(raw_net_id, "pad net id"),
        position=_parse_point(item["position"], "pad position"),
        size_x_um=_require_int(
            item["size_x_um"], "pad x size", minimum=1, micrometres=True
        ),
        size_y_um=_require_int(
            item["size_y_um"], "pad y size", minimum=1, micrometres=True
        ),
        hole_diameter_um=_require_int(
            item["hole_diameter_um"], "pad hole", minimum=0, micrometres=True
        ),
        layers=_parse_string_tuple(item["layers"], "pad layers"),
        thermal_policy=(
            None
            if raw_thermal_policy is None
            else _parse_thermal_policy(raw_thermal_policy)
        ),
    )


def _parse_thermal_policy(value: object) -> ThermalPolicy:
    item = _require_exact_fields(
        value,
        frozenset(
            {
                "pad_id",
                "net_id",
                "layers",
                "style",
                "spoke_count",
                "spoke_width_um",
                "gap_um",
                "locked",
            }
        ),
        "thermal policy",
    )
    return ThermalPolicy(
        pad_id=_object_id(item["pad_id"], "thermal policy pad id"),
        net_id=_object_id(item["net_id"], "thermal policy net id"),
        layers=_parse_string_tuple(item["layers"], "thermal policy layers"),
        style=_require_string(item["style"], "thermal policy style"),
        spoke_count=_require_int(
            item["spoke_count"], "thermal policy spoke count", minimum=1
        ),
        spoke_width_um=_require_int(
            item["spoke_width_um"],
            "thermal policy spoke width",
            minimum=1,
            micrometres=True,
        ),
        gap_um=_require_int(
            item["gap_um"], "thermal policy gap", minimum=0, micrometres=True
        ),
        locked=_require_bool(item["locked"], "thermal policy lock"),
    )


def _parse_route(value: object) -> RouteSegment:
    item = _require_exact_fields(
        value,
        frozenset(
            {"id", "net_id", "start", "end", "width_um", "layer", "route_lock"}
        ),
        "route",
    )
    return RouteSegment(
        id=_object_id(item["id"], "route id"),
        net_id=_object_id(item["net_id"], "route net id"),
        start=_parse_point(item["start"], "route start"),
        end=_parse_point(item["end"], "route end"),
        width_um=_require_int(
            item["width_um"], "route width", minimum=1, micrometres=True
        ),
        layer=_require_string(item["layer"], "route layer"),
        route_lock=_require_bool(item["route_lock"], "route lock"),
    )


def _parse_via(value: object) -> Via:
    item = _require_exact_fields(
        value,
        frozenset(
            {
                "id",
                "net_id",
                "position",
                "diameter_um",
                "hole_diameter_um",
                "layers",
                "route_lock",
            }
        ),
        "via",
    )
    return Via(
        id=_object_id(item["id"], "via id"),
        net_id=_object_id(item["net_id"], "via net id"),
        position=_parse_point(item["position"], "via position"),
        diameter_um=_require_int(
            item["diameter_um"], "via diameter", minimum=1, micrometres=True
        ),
        hole_diameter_um=_require_int(
            item["hole_diameter_um"], "via hole", minimum=1, micrometres=True
        ),
        layers=_parse_string_tuple(item["layers"], "via layers"),
        route_lock=_require_bool(item["route_lock"], "via route lock"),
    )


def _parse_copper_zone(value: object) -> CopperZone:
    item = _require_exact_fields(
        value,
        frozenset(
            {"id", "net_id", "layer", "bounds", "clearance_um", "route_lock"}
        ),
        "copper zone",
    )
    return CopperZone(
        id=_object_id(item["id"], "copper zone id"),
        net_id=_object_id(item["net_id"], "copper zone net id"),
        layer=_require_string(item["layer"], "copper zone layer"),
        bounds=_parse_rect(item["bounds"], "copper zone bounds"),
        clearance_um=_require_int(
            item["clearance_um"], "zone clearance", minimum=0, micrometres=True
        ),
        route_lock=_require_bool(item["route_lock"], "copper zone route lock"),
    )


def _parse_opaque_node(value: object) -> OpaqueNode:
    item = _require_exact_fields(
        value, frozenset({"id", "native_type", "payload"}), "opaque node"
    )
    payload = item["payload"]
    if type(payload) is not dict:
        raise ValueError("opaque payload must be an object")
    return OpaqueNode(
        id=_object_id(item["id"], "opaque node id"),
        native_type=_require_string(item["native_type"], "opaque native type"),
        payload=payload,
    )


def _point_dict(point: PointUm) -> JsonObject:
    return {"x": point.x, "y": point.y}


def _rect_dict(rect: RectUm) -> JsonObject:
    return {"x": rect.x, "y": rect.y, "width": rect.width, "height": rect.height}


def _net_class_dict(net_class: NetClass) -> JsonObject:
    return {
        "id": str(net_class.id),
        "min_width_um": net_class.min_width_um,
        "clearance_um": net_class.clearance_um,
        "min_via_diameter_um": net_class.min_via_diameter_um,
        "min_via_hole_um": net_class.min_via_hole_um,
    }


def _pad_dict(pad: Pad) -> JsonObject:
    value: JsonObject = {
        "id": str(pad.id),
        "footprint_id": str(pad.footprint_id),
        "net_id": None if pad.net_id is None else str(pad.net_id),
        "position": _point_dict(pad.position),
        "size_x_um": pad.size_x_um,
        "size_y_um": pad.size_y_um,
        "hole_diameter_um": pad.hole_diameter_um,
        "layers": list(pad.layers),
    }
    if pad.thermal_policy is not None:
        value["thermal_policy"] = _thermal_policy_dict(pad.thermal_policy)
    return value


def _thermal_policy_dict(policy: ThermalPolicy) -> JsonObject:
    return {
        "pad_id": str(policy.pad_id),
        "net_id": str(policy.net_id),
        "layers": list(policy.layers),
        "style": policy.style,
        "spoke_count": policy.spoke_count,
        "spoke_width_um": policy.spoke_width_um,
        "gap_um": policy.gap_um,
        "locked": policy.locked,
    }


def _find_by_id(items: tuple[Any, ...], value: str | BoardObjectId, kind: str) -> Any:
    object_id = value if isinstance(value, BoardObjectId) else BoardObjectId(value)
    for item in items:
        if item.id == object_id:
            return item
    raise KeyError(f"unknown {kind}: {object_id}")


__all__ = [
    "BoardObjectId",
    "BoardSnapshot",
    "CopperZone",
    "Footprint",
    "Keepout",
    "Net",
    "NetClass",
    "OpaqueNode",
    "Pad",
    "PointUm",
    "RectUm",
    "RouteSegment",
    "Via",
]

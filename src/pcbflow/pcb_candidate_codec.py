"""BoardOperation serialization codec shared by the store and release paths.

Verbatim extraction from the historical pcb_candidates monolith; behavior is
unchanged. The pcbflow.pcb_candidates module now re-exports from here.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pcbflow.board.ir import (
    BoardObjectId,
    CopperZone,
    PointUm,
    RectUm,
    RouteSegment,
    Via,
)
from pcbflow.board.operations import (
    AddGroundStitching,
    BoardOperation,
    CreateCopperZones,
    FootprintPlacement,
    LockBoardObjects,
    PlaceFootprints,
    RouteNets,
    ThermalPolicy,
)
from pcbflow.pcb_candidate_validation import _canonical


def _operation_payload(operation: BoardOperation) -> dict[str, Any]:
    """Persist the concrete operation type with its canonical dataclass fields."""
    value = _canonical(operation)
    if type(value) is not dict:
        raise TypeError("board operation did not produce an object payload")
    return {"operation_type": operation.operation_type, **value}


def deserialize_board_operations(
    values: Sequence[dict[str, Any]],
) -> tuple[BoardOperation, ...]:
    """Reconstruct the only typed BoardOperation forms accepted by V1."""
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise ValueError("persisted board operations must be a sequence")
    return tuple(_operation_from_payload(item) for item in values)


def _operation_from_payload(value: object) -> BoardOperation:
    raw = _mapping(value, "persisted board operation")
    operation_type = _string(raw.get("operation_type"), "operation type")
    common = _operation_common(raw)
    if operation_type == PlaceFootprints.operation_type:
        _expect_keys(raw, set(common) | {"operation_type", "placements"}, operation_type)
        return PlaceFootprints(
            **common,
            placements=tuple(
                _placement(item) for item in _list(raw["placements"], "placements")
            ),
        )
    if operation_type == RouteNets.operation_type:
        _expect_keys(
            raw,
            set(common)
            | {"operation_type", "net_ids", "segments", "vias", "removed_route_ids"},
            operation_type,
        )
        return RouteNets(
            **common,
            net_ids=_ids(raw["net_ids"], "route net ids"),
            segments=tuple(_route(item) for item in _list(raw["segments"], "segments")),
            vias=tuple(_via(item) for item in _list(raw["vias"], "vias")),
            removed_route_ids=_ids(raw["removed_route_ids"], "removed route ids"),
        )
    if operation_type == CreateCopperZones.operation_type:
        _expect_keys(
            raw,
            set(common) | {"operation_type", "zone_ids", "zones", "thermal_policies"},
            operation_type,
        )
        return CreateCopperZones(
            **common,
            zone_ids=_ids(raw["zone_ids"], "copper zone ids"),
            zones=tuple(_zone(item) for item in _list(raw["zones"], "copper zones")),
            thermal_policies=tuple(
                _thermal_policy(item)
                for item in _list(raw["thermal_policies"], "thermal policies")
            ),
        )
    if operation_type == AddGroundStitching.operation_type:
        _expect_keys(raw, set(common) | {"operation_type", "via_ids", "vias"}, operation_type)
        return AddGroundStitching(
            **common,
            via_ids=_ids(raw["via_ids"], "ground stitching via ids"),
            vias=tuple(_via(item) for item in _list(raw["vias"], "ground stitching vias")),
        )
    if operation_type == LockBoardObjects.operation_type:
        _expect_keys(
            raw,
            set(common) | {"operation_type", "placement_ids", "route_ids"},
            operation_type,
        )
        return LockBoardObjects(
            **common,
            placement_ids=_ids(raw["placement_ids"], "placement lock ids"),
            route_ids=_ids(raw["route_ids"], "route lock ids"),
        )
    raise ValueError(f"unsupported persisted board operation: {operation_type}")


def _operation_common(raw: dict[str, object]) -> dict[str, Any]:
    return {
        "project_id": _string(raw.get("project_id"), "operation project id"),
        "baseline_revision": _string(
            raw.get("baseline_revision"), "operation baseline revision"
        ),
        "risk": _string(raw.get("risk"), "operation risk"),
        "rulepack_digest": _string(
            raw.get("rulepack_digest"), "operation rulepack digest"
        ),
        "target_object_ids": _ids(
            raw.get("target_object_ids"), "operation target object ids"
        ),
        "idempotency_key": _string(
            raw.get("idempotency_key"), "operation idempotency key"
        ),
        "expected_snapshot_digest": _string(
            raw.get("expected_snapshot_digest"), "operation expected snapshot digest"
        ),
    }


def _placement(value: object) -> FootprintPlacement:
    raw = _mapping(value, "footprint placement")
    _expect_keys(raw, {"footprint_id", "position", "layer"}, "footprint placement")
    return FootprintPlacement(
        BoardObjectId(_string(raw["footprint_id"], "footprint id")),
        _point(raw["position"], "footprint position"),
        _string(raw["layer"], "footprint layer"),
    )


def _route(value: object) -> RouteSegment:
    raw = _mapping(value, "route segment")
    _expect_keys(
        raw,
        {"id", "net_id", "start", "end", "width_um", "layer", "route_lock"},
        "route segment",
    )
    return RouteSegment(
        BoardObjectId(_string(raw["id"], "route id")),
        BoardObjectId(_string(raw["net_id"], "route net id")),
        _point(raw["start"], "route start"),
        _point(raw["end"], "route end"),
        _integer(raw["width_um"], "route width"),
        _string(raw["layer"], "route layer"),
        _boolean(raw["route_lock"], "route lock"),
    )


def _via(value: object) -> Via:
    raw = _mapping(value, "via")
    _expect_keys(
        raw,
        {"id", "net_id", "position", "diameter_um", "hole_diameter_um", "layers", "route_lock"},
        "via",
    )
    return Via(
        BoardObjectId(_string(raw["id"], "via id")),
        BoardObjectId(_string(raw["net_id"], "via net id")),
        _point(raw["position"], "via position"),
        _integer(raw["diameter_um"], "via diameter"),
        _integer(raw["hole_diameter_um"], "via hole diameter"),
        _strings(raw["layers"], "via layers"),
        _boolean(raw["route_lock"], "via route lock"),
    )


def _zone(value: object) -> CopperZone:
    raw = _mapping(value, "copper zone")
    _expect_keys(
        raw,
        {"id", "net_id", "layer", "bounds", "clearance_um", "route_lock"},
        "copper zone",
    )
    return CopperZone(
        BoardObjectId(_string(raw["id"], "copper zone id")),
        BoardObjectId(_string(raw["net_id"], "copper zone net id")),
        _string(raw["layer"], "copper zone layer"),
        _rect(raw["bounds"], "copper zone bounds"),
        _integer(raw["clearance_um"], "copper zone clearance"),
        _boolean(raw["route_lock"], "copper zone route lock"),
    )


def _thermal_policy(value: object) -> ThermalPolicy:
    raw = _mapping(value, "thermal policy")
    _expect_keys(
        raw,
        {
            "pad_id",
            "net_id",
            "layers",
            "style",
            "spoke_count",
            "spoke_width_um",
            "gap_um",
            "locked",
        },
        "thermal policy",
    )
    return ThermalPolicy(
        pad_id=BoardObjectId(_string(raw["pad_id"], "thermal pad id")),
        net_id=BoardObjectId(_string(raw["net_id"], "thermal net id")),
        layers=_strings(raw["layers"], "thermal layers"),
        style=_string(raw["style"], "thermal style"),
        spoke_count=_integer(raw["spoke_count"], "thermal spoke count"),
        spoke_width_um=_integer(raw["spoke_width_um"], "thermal spoke width"),
        gap_um=_integer(raw["gap_um"], "thermal gap"),
        locked=_boolean(raw["locked"], "thermal lock"),
    )


def _point(value: object, context: str) -> PointUm:
    raw = _mapping(value, context)
    _expect_keys(raw, {"x", "y"}, context)
    return PointUm(_integer(raw["x"], f"{context} x"), _integer(raw["y"], f"{context} y"))


def _rect(value: object, context: str) -> RectUm:
    raw = _mapping(value, context)
    _expect_keys(raw, {"x", "y", "width", "height"}, context)
    return RectUm(
        _integer(raw["x"], f"{context} x"),
        _integer(raw["y"], f"{context} y"),
        _integer(raw["width"], f"{context} width"),
        _integer(raw["height"], f"{context} height"),
    )


def _ids(value: object, context: str) -> tuple[BoardObjectId, ...]:
    return tuple(BoardObjectId(_string(item, context)) for item in _list(value, context))


def _strings(value: object, context: str) -> tuple[str, ...]:
    return tuple(_string(item, context) for item in _list(value, context))


def _mapping(value: object, context: str) -> dict[str, object]:
    if type(value) is not dict or any(type(key) is not str for key in value):
        raise ValueError(f"{context} must be an object")
    return value


def _list(value: object, context: str) -> list[object]:
    if type(value) is not list:
        raise ValueError(f"{context} must be an array")
    return value


def _string(value: object, context: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{context} must be a non-empty canonical string")
    return value


def _integer(value: object, context: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{context} must be an integer")
    return value


def _boolean(value: object, context: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{context} must be a boolean")
    return value


def _expect_keys(raw: dict[str, object], expected: set[str], context: str) -> None:
    actual = set(raw)
    if actual != expected:
        raise ValueError(
            f"{context} has invalid fields: "
            + ", ".join(sorted(actual ^ expected))
        )

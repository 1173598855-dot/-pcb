"""Codec round-trip and rejection tests for persisted board operations.

The production path persists ``_operation_payload`` dicts as JSON and rebuilds
typed operations with ``deserialize_board_operations`` on read, so the
round-trip tests go through a real JSON encoding rather than trusting the
in-memory dict.
"""

from __future__ import annotations

import json

import pytest

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
    CreateCopperZones,
    FootprintPlacement,
    LockBoardObjects,
    PlaceFootprints,
    RouteNets,
    ThermalPolicy,
)
from pcbflow.pcb_candidate_codec import (
    _operation_payload,
    deserialize_board_operations,
)

_DIGEST = "sha256:" + "a" * 64


def _common(**overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "project_id": "prj_board",
        "baseline_revision": "git:" + "b" * 40,
        "risk": "low",
        "rulepack_digest": _DIGEST,
        "target_object_ids": (BoardObjectId("FP1"),),
        "idempotency_key": "op-1",
        "expected_snapshot_digest": _DIGEST,
    }
    fields.update(overrides)
    return fields


def _segment(route_id: str = "R1", **overrides: object) -> RouteSegment:
    fields: dict[str, object] = {
        "id": BoardObjectId(route_id),
        "net_id": BoardObjectId("GND"),
        "start": PointUm(0, 0),
        "end": PointUm(1000, 0),
        "width_um": 200,
        "layer": "F.Cu",
        "route_lock": False,
    }
    fields.update(overrides)
    return RouteSegment(**fields)


def _via(via_id: str = "V1", net_id: str = "GND") -> Via:
    return Via(
        id=BoardObjectId(via_id),
        net_id=BoardObjectId(net_id),
        position=PointUm(1000, 0),
        diameter_um=600,
        hole_diameter_um=300,
        layers=("F.Cu", "B.Cu"),
        route_lock=False,
    )


def _zone(zone_id: str = "Z1") -> CopperZone:
    return CopperZone(
        id=BoardObjectId(zone_id),
        net_id=BoardObjectId("GND"),
        layer="F.Cu",
        bounds=RectUm(0, 0, 1000, 1000),
        clearance_um=200,
        route_lock=False,
    )


def _thermal_policy() -> ThermalPolicy:
    return ThermalPolicy(
        pad_id=BoardObjectId("PAD1"),
        net_id=BoardObjectId("GND"),
        layers=("F.Cu",),
        style="thermal_relief",
        spoke_count=4,
        spoke_width_um=200,
        gap_um=200,
        locked=True,
    )


def _operation_samples() -> dict[str, object]:
    return {
        "board.place_footprints": PlaceFootprints(
            **_common(),
            placements=(
                FootprintPlacement(BoardObjectId("FP1"), PointUm(0, 0), "F.Cu"),
            ),
        ),
        "board.route_nets": RouteNets(
            **_common(),
            net_ids=(BoardObjectId("GND"),),
            segments=(_segment(),),
            vias=(_via(),),
            removed_route_ids=(),
        ),
        "board.create_copper_zones": CreateCopperZones(
            **_common(),
            zone_ids=(BoardObjectId("Z1"),),
            zones=(_zone(),),
            thermal_policies=(_thermal_policy(),),
        ),
        "board.add_ground_stitching": AddGroundStitching(
            **_common(),
            via_ids=(BoardObjectId("V1"),),
            vias=(_via(),),
        ),
        "board.lock_board_objects": LockBoardObjects(
            **_common(),
            placement_ids=(BoardObjectId("FP1"),),
            route_ids=(BoardObjectId("R1"),),
        ),
    }


@pytest.mark.parametrize("operation_type", sorted(_operation_samples()))
def test_round_trip_restores_every_supported_operation(operation_type: str) -> None:
    operation = _operation_samples()[operation_type]

    payload = json.loads(json.dumps(_operation_payload(operation)))
    restored = deserialize_board_operations([payload])[0]

    assert restored == operation


def test_payload_of_non_dataclass_operation_is_rejected() -> None:
    with pytest.raises(TypeError, match="object payload"):
        _operation_payload(BoardObjectId("FP1"))


def test_deserialize_rejects_non_sequence_input() -> None:
    with pytest.raises(ValueError, match="must be a sequence"):
        deserialize_board_operations('{"operation_type": "board.route_nets"}')
    with pytest.raises(ValueError, match="must be a sequence"):
        deserialize_board_operations(None)  # type: ignore[arg-type]


def test_deserialize_rejects_unknown_operation_type() -> None:
    with pytest.raises(ValueError, match="unsupported persisted board operation"):
        deserialize_board_operations([_route_payload(operation_type="board.explode")])


def test_deserialize_rejects_non_object_operation() -> None:
    with pytest.raises(ValueError, match="must be an object"):
        deserialize_board_operations([[1, 2]])


def test_deserialize_rejects_dict_with_non_string_keys() -> None:
    with pytest.raises(ValueError, match="must be an object"):
        deserialize_board_operations([{1: 2}])


def test_deserialize_rejects_extra_field() -> None:
    with pytest.raises(ValueError, match="bogus"):
        deserialize_board_operations([_route_payload(bogus=1)])


def test_deserialize_rejects_missing_field() -> None:
    payload = _route_payload()
    del payload["vias"]

    with pytest.raises(ValueError, match="vias"):
        deserialize_board_operations([payload])


def test_deserialize_rejects_blank_common_string() -> None:
    with pytest.raises(ValueError, match="non-empty canonical string"):
        deserialize_board_operations([_route_payload(project_id="  ")])


def test_deserialize_rejects_non_list_collection() -> None:
    with pytest.raises(ValueError, match="segments must be an array"):
        deserialize_board_operations([_route_payload(segments={})])


def test_deserialize_rejects_non_object_point() -> None:
    with pytest.raises(ValueError, match="route start must be an object"):
        deserialize_board_operations([_route_payload(segments=[_segment_payload(start=[0, 0])])])


def test_deserialize_rejects_non_integer_width() -> None:
    payload = _route_payload(segments=[_segment_payload(width_um="200")])

    with pytest.raises(ValueError, match="route width must be an integer"):
        deserialize_board_operations([payload])


def test_deserialize_rejects_non_boolean_route_lock() -> None:
    payload = _route_payload(segments=[_segment_payload(route_lock=1)])

    with pytest.raises(ValueError, match="route lock must be a boolean"):
        deserialize_board_operations([payload])


def _route_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "operation_type": "board.route_nets",
        "project_id": "prj_board",
        "baseline_revision": "git:" + "b" * 40,
        "risk": "low",
        "rulepack_digest": _DIGEST,
        "target_object_ids": [],
        "idempotency_key": "op-1",
        "expected_snapshot_digest": _DIGEST,
        "net_ids": ["GND"],
        "segments": [],
        "vias": [],
        "removed_route_ids": [],
    }
    payload.update(overrides)
    return payload


def _segment_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": "R1",
        "net_id": "GND",
        "start": {"x": 0, "y": 0},
        "end": {"x": 1000, "y": 0},
        "width_um": 200,
        "layer": "F.Cu",
        "route_lock": False,
    }
    payload.update(overrides)
    return payload

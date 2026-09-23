from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pcbflow.board import (
    AddGroundStitching,
    BoardObjectId,
    BoardSnapshot,
    CreateCopperZones,
    LockBoardObjects,
    OpaqueNode,
    Pad,
    PlaceFootprints,
    PointUm,
    RouteNets,
    ThermalPolicy,
)

FIXTURE = (
    Path(__file__).parents[1]
    / "fixtures"
    / "boardir"
    / "stm32-environment-controller-2l-v1.json"
)


def _fixture_value() -> dict[str, object]:
    return json.loads(FIXTURE.read_bytes())


def _load(value: dict[str, object]) -> BoardSnapshot:
    return BoardSnapshot.load_json(json.dumps(value).encode("utf-8"))


def _concave_board_value() -> dict[str, object]:
    value = _fixture_value()
    value["outline"] = [
        {"x": 0, "y": 0},
        {"x": 100_000, "y": 0},
        {"x": 100_000, "y": 80_000},
        {"x": 60_000, "y": 80_000},
        {"x": 60_000, "y": 40_000},
        {"x": 0, "y": 40_000},
    ]
    for name in (
        "net_classes",
        "nets",
        "keepouts",
        "footprints",
        "pads",
        "routes",
        "vias",
        "copper_zones",
        "opaque_nodes",
    ):
        value[name] = []
    return value


def test_stm32_fixture_loads_with_fixed_identity_and_integer_geometry() -> None:
    snapshot = BoardSnapshot.load_json(FIXTURE.read_bytes())

    assert snapshot.board_size_um == (100_000, 80_000)
    assert snapshot.footprint("U_WIFI").keepout_ids == ("ko_esp_antenna",)
    assert snapshot.footprint("U_MCU").placement_lock is False
    assert snapshot.net("GND").net_class == "ground"
    assert {str(item.id) for item in snapshot.footprints} >= {
        "J_USB_C",
        "U_MCU",
        "U_WIFI",
        "OLED1",
        "K1",
        "K2",
        "Q1",
        "Q2",
        "J_RELAY_OUT",
        "J_MOS_OUT",
        "J_SWD",
        "J_UART",
        "MH1",
        "MH2",
        "MH3",
        "MH4",
    }


def test_snapshot_digest_is_canonical_and_has_a_fixed_regression_value() -> None:
    value = _fixture_value()
    reordered = dict(reversed(tuple(value.items())))
    left = _load(value)
    right = _load(reordered)

    assert left.canonical_digest() == right.canonical_digest()
    assert left.canonical_digest() == (
        "sha256:888129a2afd5261c5cb5f308ec1b7346eb07498f8b592a268aa98072a59910d5"
    )
    assert left.canonical_bytes() == right.canonical_bytes()
    assert left.canonical_digest() == (
        "sha256:" + hashlib.sha256(left.canonical_bytes()).hexdigest()
    )
    assert left.to_canonical_dict() == value


def test_snapshot_digest_is_stable_when_object_collections_are_reordered() -> None:
    value = _fixture_value()
    reordered = dict(value)
    for field in (
        "net_classes",
        "nets",
        "keepouts",
        "footprints",
        "pads",
        "routes",
        "vias",
        "copper_zones",
        "opaque_nodes",
    ):
        reordered[field] = list(reversed(value[field]))  # type: ignore[index]

    left = _load(value)
    right = _load(reordered)

    assert left.canonical_digest() == right.canonical_digest()
    assert left.canonical_bytes() == right.canonical_bytes()


def test_snapshot_retains_locked_thermal_policy_on_a_pad() -> None:
    value = _fixture_value()
    pad = next(item for item in value["pads"] if item["id"] == "pad_MH1")  # type: ignore[index]
    pad["net_id"] = "GND"
    pad["thermal_policy"] = {
        "pad_id": "pad_MH1",
        "net_id": "GND",
        "layers": ["F.Cu", "B.Cu"],
        "style": "thermal_relief",
        "spoke_count": 4,
        "spoke_width_um": 203,
        "gap_um": 254,
        "locked": True,
    }

    snapshot = _load(value)

    applied = next(
        item
        for item in snapshot.to_canonical_dict()["pads"]
        if item["id"] == "pad_MH1"
    )
    assert applied["thermal_policy"] == pad["thermal_policy"]
    assert snapshot.canonical_digest() != _load(_fixture_value()).canonical_digest()


@pytest.mark.parametrize(
    ("overrides", "message"),
    (
        ({"pad_id": "pad_MH1"}, "ids"),
        ({"style": "unsupported"}, "style"),
        ({"spoke_count": 0}, "spoke count"),
        ({"spoke_width_um": 0}, "spoke width"),
        ({"gap_um": -1}, "gap"),
        ({"locked": 1}, "lock"),
    ),
)
def test_thermal_policy_rejects_invalid_locked_connection_fields(
    overrides: dict[str, object], message: str
) -> None:
    values: dict[str, object] = {
        "pad_id": BoardObjectId("pad_MH1"),
        "net_id": BoardObjectId("GND"),
        "layers": ("F.Cu", "B.Cu"),
        "style": "thermal_relief",
        "spoke_count": 4,
        "spoke_width_um": 203,
        "gap_um": 254,
        "locked": True,
    }

    with pytest.raises((TypeError, ValueError), match=message):
        ThermalPolicy(**(values | overrides))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("policy_overrides", "message"),
    (
        ({"pad_id": BoardObjectId("pad_other")}, "containing pad"),
        ({"net_id": BoardObjectId("5V")}, "containing pad"),
        ({"layers": ("B.Cu",)}, "containing pad"),
        ({"locked": False}, "must be locked"),
    ),
)
def test_pad_rejects_a_thermal_policy_outside_its_locked_binding(
    policy_overrides: dict[str, object], message: str
) -> None:
    values: dict[str, object] = {
        "pad_id": BoardObjectId("pad_MH1"),
        "net_id": BoardObjectId("GND"),
        "layers": ("F.Cu",),
        "style": "thermal_relief",
        "spoke_count": 4,
        "spoke_width_um": 203,
        "gap_um": 254,
        "locked": True,
    }
    policy = ThermalPolicy(**(values | policy_overrides))  # type: ignore[arg-type]

    with pytest.raises(ValueError, match=message):
        Pad(
            id=BoardObjectId("pad_MH1"),
            footprint_id=BoardObjectId("MH1"),
            net_id=BoardObjectId("GND"),
            position=PointUm(4_000, 4_000),
            size_x_um=5_000,
            size_y_um=5_000,
            hole_diameter_um=3_200,
            layers=("F.Cu",),
            thermal_policy=policy,
        )


def test_opaque_node_rejects_non_string_payload_keys() -> None:
    with pytest.raises(ValueError, match="opaque payload keys must be strings"):
        OpaqueNode(
            BoardObjectId("opaque-test"),
            "native.test",
            {1: "must not be coerced"},  # type: ignore[dict-item]
        )


def test_snapshot_rejects_duplicate_native_ids_across_object_types() -> None:
    value = _fixture_value()
    value["vias"][0]["id"] = value["routes"][0]["id"]  # type: ignore[index]

    with pytest.raises(ValueError, match="duplicate native id"):
        _load(value)


def test_opaque_payload_preserves_finite_native_numbers_without_interpreting_them() -> None:
    value = _fixture_value()
    value["opaque_nodes"][0]["payload"]["native_scale"] = 1.25  # type: ignore[index]

    snapshot = _load(value)

    assert snapshot.to_canonical_dict()["opaque_nodes"][0]["payload"]["native_scale"] == 1.25  # type: ignore[index]


def test_snapshot_rejects_unknown_fields_and_invalid_layers() -> None:
    extra = _fixture_value()
    extra["native_archive"] = "must not be guessed"
    invalid_layer = _fixture_value()
    invalid_layer["routes"][0]["layer"] = "In1.Cu"  # type: ignore[index]

    with pytest.raises(ValueError, match="unexpected fields"):
        _load(extra)
    with pytest.raises(ValueError, match="invalid layer"):
        _load(invalid_layer)


@given(
    x=st.one_of(
        st.integers(min_value=-1_000_000, max_value=-1),
        st.integers(min_value=100_001, max_value=1_000_000),
    )
)
def test_snapshot_rejects_footprint_coordinates_outside_outline(x: int) -> None:
    value = _fixture_value()
    value["footprints"][1]["position"]["x"] = x  # type: ignore[index]

    with pytest.raises(ValueError, match="outside board outline"):
        _load(value)


def test_snapshot_rejects_self_intersecting_outline() -> None:
    value = _fixture_value()
    value["outline"] = [
        {"x": 0, "y": 0},
        {"x": 100_000, "y": 80_000},
        {"x": 100_000, "y": 0},
        {"x": 0, "y": 80_000},
    ]

    with pytest.raises(ValueError, match="simple"):
        _load(value)


def test_snapshot_rejects_nonzero_area_self_intersecting_outline() -> None:
    value = _fixture_value()
    value["outline"] = [
        {"x": 0, "y": 0},
        {"x": 100_000, "y": 80_000},
        {"x": 100_000, "y": 0},
        {"x": 0, "y": 48_000},
    ]

    with pytest.raises(ValueError, match="simple"):
        _load(value)


def test_snapshot_accepts_footprint_and_route_inside_concave_outline() -> None:
    value = _concave_board_value()
    value["footprints"] = [
        {
            "id": "U_INSIDE",
            "position": {"x": 80_000, "y": 60_000},
            "width_um": 1_000,
            "height_um": 1_000,
            "layer": "F.Cu",
            "pad_ids": [],
            "keepout_ids": [],
            "placement_lock": False,
        }
    ]
    value["net_classes"] = [
        {
            "id": "signal",
            "min_width_um": 203,
            "clearance_um": 203,
            "min_via_diameter_um": 762,
            "min_via_hole_um": 381,
        }
    ]
    value["nets"] = [{"id": "NET_TEST", "net_class": "signal"}]
    value["routes"] = [
        {
            "id": "route_inside",
            "net_id": "NET_TEST",
            "start": {"x": 70_000, "y": 50_000},
            "end": {"x": 90_000, "y": 70_000},
            "width_um": 203,
            "layer": "F.Cu",
            "route_lock": False,
        }
    ]

    snapshot = _load(value)

    assert snapshot.footprint("U_INSIDE").position == PointUm(80_000, 60_000)
    assert snapshot.routes[0].id == BoardObjectId("route_inside")


def test_snapshot_rejects_footprint_in_concave_outline_cutout() -> None:
    value = _concave_board_value()
    value["footprints"] = [
        {
            "id": "U_CUTOUT",
            "position": {"x": 30_000, "y": 60_000},
            "width_um": 1_000,
            "height_um": 1_000,
            "layer": "F.Cu",
            "pad_ids": [],
            "keepout_ids": [],
            "placement_lock": False,
        }
    ]

    with pytest.raises(ValueError, match="outside board outline: U_CUTOUT"):
        _load(value)


def test_snapshot_rejects_route_crossing_concave_outline_cutout() -> None:
    value = _concave_board_value()
    value["net_classes"] = [
        {
            "id": "signal",
            "min_width_um": 203,
            "clearance_um": 203,
            "min_via_diameter_um": 762,
            "min_via_hole_um": 381,
        }
    ]
    value["nets"] = [{"id": "NET_TEST", "net_class": "signal"}]
    value["routes"] = [
        {
            "id": "route_cutout",
            "net_id": "NET_TEST",
            "start": {"x": 10_000, "y": 30_000},
            "end": {"x": 90_000, "y": 70_000},
            "width_um": 203,
            "layer": "F.Cu",
            "route_lock": False,
        }
    ]

    with pytest.raises(ValueError, match="outside board outline: route_cutout"):
        _load(value)


@given(value=st.sampled_from((True, False, 1.5, "1000", None)))
def test_point_rejects_non_integer_micrometres(value: object) -> None:
    with pytest.raises((TypeError, ValueError), match="integer micrometres"):
        PointUm(value, 0)  # type: ignore[arg-type]


def test_board_models_and_operations_are_frozen_typed_values() -> None:
    point = PointUm(1, 2)
    with pytest.raises(FrozenInstanceError):
        point.x = 3  # type: ignore[misc]

    common = {
        "project_id": "prj_controller",
        "baseline_revision": "git:" + "1" * 40,
        "risk": "medium",
        "rulepack_digest": "sha256:" + "2" * 64,
        "target_object_ids": (BoardObjectId("U_MCU"),),
        "idempotency_key": "board-op-1",
        "expected_snapshot_digest": "sha256:" + "3" * 64,
    }
    operations = (
        PlaceFootprints(**common),
        RouteNets(**common),
        CreateCopperZones(**common),
        AddGroundStitching(**common),
        LockBoardObjects(**common),
    )

    assert [item.operation_type for item in operations] == [
        "board.place_footprints",
        "board.route_nets",
        "board.create_copper_zones",
        "board.add_ground_stitching",
        "board.lock_board_objects",
    ]
    assert all(item.target_object_ids == (BoardObjectId("U_MCU"),) for item in operations)

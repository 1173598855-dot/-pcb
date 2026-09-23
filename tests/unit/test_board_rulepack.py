from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from pcbflow.board import ManufacturingRulePack

RULEPACK = (
    Path(__file__).parents[1]
    / "fixtures"
    / "boardir"
    / "stm32-environment-controller-2l-rulepack.json"
)
RULEPACK_4L = (
    Path(__file__).parents[1]
    / "fixtures"
    / "boardir"
    / "stm32-environment-controller-4l-rulepack.json"
)


def _value() -> dict[str, object]:
    value = json.loads(RULEPACK.read_bytes())
    value["layer_names"] = ["F.Cu", "B.Cu"]
    return value


def _load(value: dict[str, object]) -> ManufacturingRulePack:
    return ManufacturingRulePack.load_json(json.dumps(value).encode("utf-8"))


def test_rulepack_loads_exact_v1_integer_net_class_rules() -> None:
    rulepack = ManufacturingRulePack.load_json(RULEPACK.read_bytes())

    assert rulepack.profile_id == "stm32-environment-controller-2l-v1"
    assert rulepack.layer_count == 2
    assert rulepack.layer_names == ("F.Cu", "B.Cu")
    assert rulepack.copper_oz == 1
    assert rulepack.max_board_size_um == (100_000, 80_000)
    assert rulepack.net_class("signal").min_width_um == 203
    assert rulepack.net_class("logic_power").min_width_um == 508
    assert rulepack.net_class("load_power").min_width_um == 2032
    assert rulepack.routing.grid_step_um == 1_000
    assert rulepack.routing.max_vias_per_net == 2
    assert rulepack.copper.edge_clearance_um == 203
    assert rulepack.copper.stitching_pitch_um == 10_000
    assert rulepack.copper.max_stitching_vias == 16
    assert rulepack.copper.thermal_spoke_count == 4
    assert [str(item.id) for item in rulepack.net_classes] == [
        "signal",
        "quiet_signal",
        "logic_power",
        "load_power",
        "ground",
    ]


def test_rulepack_loads_exact_v1_4_layer_stack() -> None:
    rulepack = ManufacturingRulePack.load_json(RULEPACK_4L.read_bytes())

    assert rulepack.profile_id == "stm32-environment-controller-4l-v1"
    assert rulepack.layer_count == 4
    assert rulepack.layer_names == ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")


def test_rulepack_digest_is_stable_when_net_classes_are_reordered() -> None:
    value = _value()
    reordered = dict(value)
    reordered["net_classes"] = list(reversed(value["net_classes"]))  # type: ignore[index]

    left = _load(value)
    right = _load(reordered)

    assert left.canonical_digest() == right.canonical_digest()
    assert left.canonical_digest() == (
        "sha256:21db03a63f180acdb87737c79a017906cbabae147178709bbd414a7783975301"
    )
    assert left.canonical_bytes() == right.canonical_bytes()
    assert left.canonical_digest() == (
        "sha256:" + hashlib.sha256(left.canonical_bytes()).hexdigest()
    )


def test_routing_policy_is_strict_and_contributes_to_digest() -> None:
    value = _value()
    changed = _value()
    changed["routing"]["max_retry_rounds"] = 2  # type: ignore[index]
    invalid = _value()
    invalid["routing"]["grid_step_um"] = 0  # type: ignore[index]
    extra = _value()
    extra["routing"]["hidden_retry_limit"] = 3  # type: ignore[index]

    assert _load(value).canonical_digest() != _load(changed).canonical_digest()
    with pytest.raises(ValueError, match="grid step"):
        _load(invalid)
    with pytest.raises(ValueError, match="unexpected fields"):
        _load(extra)


def test_copper_policy_is_strict_and_contributes_to_digest() -> None:
    value = _value()
    changed = _value()
    changed["copper"]["stitching_pitch_um"] = 12_000  # type: ignore[index]
    invalid = _value()
    invalid["copper"]["thermal_spoke_count"] = 0  # type: ignore[index]
    extra = _value()
    extra["copper"]["hidden_island_policy"] = "drop"  # type: ignore[index]

    assert _load(value).canonical_digest() != _load(changed).canonical_digest()
    with pytest.raises(ValueError, match="thermal spoke count"):
        _load(invalid)
    with pytest.raises(ValueError, match="unexpected fields"):
        _load(extra)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("high_voltage", True),
        ("rf_impedance_controlled", True),
        ("high_speed_differential", True),
        ("blind_buried_vias", True),
        ("unrestricted_full_board_routing", True),
    ],
)
def test_v1_rulepack_rejects_out_of_scope_requests(field: str, value: bool) -> None:
    payload = _value()
    payload["scope"][field] = value  # type: ignore[index]

    with pytest.raises(ValueError, match="unsupported V1 scope"):
        _load(payload)


def test_v1_rulepack_rejects_invalid_layer_count_and_wrong_layer_names() -> None:
    three_layer = _value()
    three_layer["layer_count"] = 3

    wrong_names_2l = _value()
    wrong_names_2l["layer_names"] = ["F.Cu", "In1.Cu", "B.Cu"]

    wrong_names_4l = json.loads(RULEPACK_4L.read_bytes())
    wrong_names_4l["layer_names"] = ["F.Cu", "B.Cu", "In1.Cu", "In2.Cu"]

    fractional = _value()
    fractional["max_board_size_um"]["width"] = 100_000.0  # type: ignore[index]

    with pytest.raises(ValueError, match="rule pack supports 2, 4 copper layers"):
        _load(three_layer)
    with pytest.raises(ValueError, match="layer names"):
        _load(wrong_names_2l)
    with pytest.raises(ValueError, match="layer names"):
        _load(wrong_names_4l)
    with pytest.raises(ValueError, match="integer micrometres"):
        _load(fractional)


def test_rulepack_rejects_unknown_fields_and_duplicate_net_classes() -> None:
    unknown = _value()
    unknown["relax_drc"] = True
    duplicate = _value()
    duplicate["net_classes"][1]["id"] = "signal"  # type: ignore[index]

    with pytest.raises(ValueError, match="unexpected fields"):
        _load(unknown)
    with pytest.raises(ValueError, match="duplicate net class"):
        _load(duplicate)

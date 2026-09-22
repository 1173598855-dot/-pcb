from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from pcbflow.board import (
    BoardObjectId,
    BoardRuleChecker,
    BoardSnapshot,
    CopperPlanner,
    CreateCopperZones,
    FixtureBoardAdapter,
    ManufacturingRulePack,
)


FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "boardir"


def test_copper_operations_replay_and_pass_boardir_validation(tmp_path: Path) -> None:
    snapshot = BoardSnapshot.load_json(
        (FIXTURE_ROOT / "stm32-environment-controller-2l-v1.json").read_bytes()
    )
    rulepack = ManufacturingRulePack.load_json(
        (FIXTURE_ROOT / "stm32-environment-controller-2l-rulepack.json").read_bytes()
    )
    result = CopperPlanner().plan(snapshot, rulepack)

    assert result.operations
    source = tmp_path / "source"
    source.mkdir()
    (source / "expected-boardir.json").write_bytes(
        json.dumps(snapshot.to_canonical_dict(), separators=(",", ":")).encode()
    )
    candidate = FixtureBoardAdapter().create_candidate(source, tmp_path / "candidate")

    applied = FixtureBoardAdapter().apply_operations(candidate, result.operations, snapshot)

    assert all(zone in applied.copper_zones for zone in result.zones)
    assert all(via in applied.vias for via in result.vias)
    assert BoardRuleChecker().check(applied, rulepack) == ()


def test_copper_replay_persists_locked_thermal_policy_on_ground_pad(
    tmp_path: Path,
) -> None:
    snapshot = BoardSnapshot.load_json(
        (FIXTURE_ROOT / "stm32-environment-controller-2l-v1.json").read_bytes()
    )
    snapshot = replace(
        snapshot,
        pads=tuple(
            replace(item, net_id=BoardObjectId("GND"))
            if item.id == BoardObjectId("pad_MH1")
            else item
            for item in snapshot.pads
        ),
    )
    rulepack = ManufacturingRulePack.load_json(
        (FIXTURE_ROOT / "stm32-environment-controller-2l-rulepack.json").read_bytes()
    )
    result = CopperPlanner().plan(snapshot, rulepack)
    policy = next(
        item for item in result.thermal_policies if item.pad_id == BoardObjectId("pad_MH1")
    )

    source = tmp_path / "source"
    source.mkdir()
    (source / "expected-boardir.json").write_bytes(
        json.dumps(snapshot.to_canonical_dict(), separators=(",", ":")).encode()
    )
    candidate = FixtureBoardAdapter().create_candidate(source, tmp_path / "candidate")

    applied = FixtureBoardAdapter().apply_operations(candidate, result.operations, snapshot)

    applied_pad = next(
        item
        for item in applied.to_canonical_dict()["pads"]
        if item["id"] == "pad_MH1"
    )
    assert applied_pad.get("thermal_policy") == {
        "pad_id": str(policy.pad_id),
        "net_id": str(policy.net_id),
        "layers": list(policy.layers),
        "style": policy.style,
        "spoke_count": policy.spoke_count,
        "spoke_width_um": policy.spoke_width_um,
        "gap_um": policy.gap_um,
        "locked": policy.locked,
    }

    zones = next(
        item for item in result.operations if isinstance(item, CreateCopperZones)
    )
    overwrite = replace(
        zones,
        expected_snapshot_digest=applied.canonical_digest(),
        target_object_ids=(policy.pad_id,),
        idempotency_key="overwrite-locked-thermal",
        zone_ids=(),
        zones=(),
        thermal_policies=(replace(policy, gap_um=policy.gap_um + 1),),
    )
    with pytest.raises(ValueError, match="thermal policy is locked"):
        FixtureBoardAdapter().apply_operations(candidate, (overwrite,), applied)

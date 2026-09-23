from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from pcbflow.board import (
    BoardObjectId,
    BoardRuleChecker,
    BoardSnapshot,
    Keepout,
    ManufacturingRulePack,
    RectUm,
)
from pcbflow.board.fixture_adapter import FixtureBoardAdapter
from pcbflow.board.placement import DoubledRectUm, PlacementSolver, score_layout

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "boardir"


def test_placement_operation_round_trips_through_boardir_adapter(tmp_path: Path) -> None:
    snapshot = BoardSnapshot.load_json(
        (FIXTURE_ROOT / "stm32-environment-controller-2l-v1.json").read_bytes()
    )
    rulepack = ManufacturingRulePack.load_json(
        (FIXTURE_ROOT / "stm32-environment-controller-2l-rulepack.json").read_bytes()
    )
    result = PlacementSolver().solve(snapshot, rulepack, seed=13, starts=3, iterations=30)
    source = tmp_path / "source"
    source.mkdir()
    (source / "expected-boardir.json").write_bytes(
        json.dumps(snapshot.to_canonical_dict()).encode("utf-8")
    )
    adapter = FixtureBoardAdapter()
    candidate = adapter.create_candidate(source, tmp_path / "candidate")

    applied = adapter.apply_operations(candidate, result.operations, snapshot)

    assert BoardRuleChecker().check(applied, rulepack) == ()
    for locked in (item for item in snapshot.footprints if item.placement_lock):
        assert applied.footprint(locked.id) == locked


def test_adapter_moves_pads_with_footprints_and_preserves_applied_score(tmp_path: Path) -> None:
    snapshot = BoardSnapshot.load_json(
        (FIXTURE_ROOT / "stm32-environment-controller-2l-v1.json").read_bytes()
    )
    rulepack = ManufacturingRulePack.load_json(
        (FIXTURE_ROOT / "stm32-environment-controller-2l-rulepack.json").read_bytes()
    )
    k2 = snapshot.footprint("K2")
    overlapping = replace(
        snapshot,
        footprints=tuple(
            replace(item, position=k2.position) if str(item.id) == "K1" else item
            for item in snapshot.footprints
        ),
    )
    result = PlacementSolver().solve(overlapping, rulepack, seed=31, starts=1, iterations=0)
    source = tmp_path / "source"
    source.mkdir()
    (source / "expected-boardir.json").write_bytes(
        json.dumps(overlapping.to_canonical_dict()).encode("utf-8")
    )
    adapter = FixtureBoardAdapter()
    candidate = adapter.create_candidate(source, tmp_path / "candidate")

    applied = adapter.apply_operations(candidate, result.operations, overlapping)

    for pad in applied.pads:
        before_pad = next(item for item in overlapping.pads if item.id == pad.id)
        before_footprint = overlapping.footprint(pad.footprint_id)
        after_footprint = applied.footprint(pad.footprint_id)
        assert (
            pad.position.x - after_footprint.position.x,
            pad.position.y - after_footprint.position.y,
        ) == (
            before_pad.position.x - before_footprint.position.x,
            before_pad.position.y - before_footprint.position.y,
        )
    applied_positions = {str(item.id): item.position for item in applied.footprints}
    assert score_layout(applied, applied_positions) == result.score


def test_solver_never_emits_an_odd_sized_footprint_that_adapter_cannot_reconstruct(tmp_path: Path) -> None:
    snapshot = BoardSnapshot.load_json(
        (FIXTURE_ROOT / "stm32-environment-controller-2l-v1.json").read_bytes()
    )
    rulepack = ManufacturingRulePack.load_json(
        (FIXTURE_ROOT / "stm32-environment-controller-2l-rulepack.json").read_bytes()
    )
    odd_at_strict_edge = replace(
        snapshot,
        footprints=tuple(
            replace(item, width_um=14_001, keepout_ids=())
            if str(item.id) == "K1"
            else replace(item, layer="B.Cu", placement_lock=True)
            for item in snapshot.footprints
        ),
        keepouts=snapshot.keepouts
        + (
            Keepout(
                BoardObjectId("ko_force_k1_to_odd_edge"),
                "mechanical",
                RectUm(14_002, 0, 85_998, 80_000),
                ("F.Cu",),
                ("footprint",),
            ),
        ),
    )
    source = tmp_path / "source"
    source.mkdir()
    (source / "expected-boardir.json").write_bytes(
        json.dumps(odd_at_strict_edge.to_canonical_dict()).encode("utf-8")
    )
    candidate = FixtureBoardAdapter().create_candidate(source, tmp_path / "candidate")

    result = PlacementSolver().solve(odd_at_strict_edge, rulepack, seed=41, starts=1, iterations=0)

    assert result.findings == ()
    assert result.placements_by_id["K1"].position.x == 7_001
    bounds = result.footprint_bounds("K1")
    assert isinstance(bounds, DoubledRectUm)
    assert not bounds.intersects(odd_at_strict_edge.keepout("ko_force_k1_to_odd_edge").bounds)
    assert (bounds.x, bounds.x + bounds.width) == (1, 28_003)
    assert FixtureBoardAdapter().apply_operations(candidate, result.operations, odd_at_strict_edge).footprint("K1") == replace(
        odd_at_strict_edge.footprint("K1"), position=result.placements_by_id["K1"].position
    )

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from pcbflow.board import (
    BoardObjectId,
    BoardSnapshot,
    DoubledRectUm,
    Footprint,
    Keepout,
    ManufacturingRulePack,
    PointUm,
    RectUm,
)
from pcbflow.board.placement import (
    PlacementSolver,
    connector_access_penalty,
    thermal_cluster_penalty,
)

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "boardir"


@pytest.fixture
def stm32_board() -> BoardSnapshot:
    return BoardSnapshot.load_json(
        (FIXTURE_ROOT / "stm32-environment-controller-2l-v1.json").read_bytes()
    )


@pytest.fixture
def rulepack() -> ManufacturingRulePack:
    return ManufacturingRulePack.load_json(
        (FIXTURE_ROOT / "stm32-environment-controller-2l-rulepack.json").read_bytes()
    )


def test_solver_is_feasible_deterministic_and_retains_complete_evidence(
    stm32_board: BoardSnapshot, rulepack: ManufacturingRulePack
) -> None:
    solver = PlacementSolver()
    result = solver.solve(stm32_board, rulepack, seed=7, starts=4, iterations=40)

    assert result.findings == ()
    assert result.operations[0].placements_by_id["J_USB_C"].position == stm32_board.footprint("J_USB_C").position
    assert result.operations[0].placements_by_id["U_WIFI"].position == stm32_board.footprint("U_WIFI").position
    assert result.evidence.objective_version == "placement-v1"
    assert result.evidence.seed == 7
    assert len(result.evidence.start_scores) == 4
    assert result.evidence.chosen_score == result.score
    assert result.evidence.snapshot_digest == stm32_board.canonical_digest()
    assert result.evidence.rulepack_digest == rulepack.canonical_digest()
    assert result.evidence.placement_digest.startswith("sha256:")
    assert result.evidence.capability_summary == "capability_unbound"
    assert solver.solve(stm32_board, rulepack, seed=7, starts=4, iterations=40) == result


def test_solver_enforces_antenna_mechanical_quiet_and_crystal_hard_regions(
    stm32_board: BoardSnapshot, rulepack: ManufacturingRulePack
) -> None:
    result = PlacementSolver().solve(stm32_board, rulepack, seed=3, starts=2, iterations=20)

    for footprint in stm32_board.footprints:
        bounds = result.footprint_bounds(footprint.id)
        for keepout in stm32_board.keepouts:
            if "footprint" not in keepout.prohibited or keepout.id in footprint.keepout_ids:
                continue
            assert not bounds.intersects(keepout.bounds)
    assert result.footprint_bounds("U_MCU").intersects(
        stm32_board.keepout("ko_crystal_near_field").bounds
    )
    assert not result.footprint_bounds("OLED1").intersects(
        stm32_board.keepout("ko_power_zone").bounds
    )


def test_solver_avoids_courtyard_overlap_and_never_moves_any_lock(
    stm32_board: BoardSnapshot, rulepack: ManufacturingRulePack
) -> None:
    result = PlacementSolver().solve(stm32_board, rulepack, seed=11, starts=3, iterations=25)

    for footprint in stm32_board.footprints:
        placement = result.operations[0].placements_by_id[str(footprint.id)]
        if footprint.placement_lock:
            assert placement.position == footprint.position
            assert placement.layer == footprint.layer
    bounds = [result.footprint_bounds(item.id) for item in stm32_board.footprints]
    assert all(
        not left.intersects(right)
        for index, left in enumerate(bounds)
        for right in bounds[index + 1 :]
    )


def test_solver_repairs_an_initial_unlocked_courtyard_overlap(
    stm32_board: BoardSnapshot, rulepack: ManufacturingRulePack
) -> None:
    k2 = stm32_board.footprint("K2")
    overlapping = replace(
        stm32_board,
        footprints=tuple(
            replace(item, position=k2.position) if str(item.id) == "K1" else item
            for item in stm32_board.footprints
        ),
    )

    result = PlacementSolver().solve(overlapping, rulepack, seed=17, starts=2, iterations=20)

    assert result.findings == ()
    assert result.footprint_bounds("K1").intersects(result.footprint_bounds("K2")) is False
    assert (
        result.operations[0].placements_by_id["K1"].position,
        result.operations[0].placements_by_id["K2"].position,
    ) != (k2.position, k2.position)


def test_cp_sat_rejects_different_size_edge_touching_courtyards_at_zero_iterations(
    stm32_board: BoardSnapshot, rulepack: ManufacturingRulePack
) -> None:
    edge_touching = replace(
        stm32_board,
        footprints=tuple(
            replace(item, position=PointUm(23_000, 68_000), width_um=12_000, placement_lock=True)
            if str(item.id) == "K1"
            else replace(item, placement_lock=True)
            if str(item.id) == "K2"
            else item
            for item in stm32_board.footprints
        ),
    )

    result = PlacementSolver().solve(edge_touching, rulepack, seed=19, starts=1, iterations=0)

    assert result.operations == ()
    assert result.findings[0].rule_id == "PCB_PLACEMENT_INFEASIBLE"


def test_doubled_placement_bounds_type_is_available_from_board_package() -> None:
    assert DoubledRectUm(0, 0, 2, 2).intersects(RectUm(1, 0, 1, 1))


def test_multi_start_evidence_records_distinct_hard_feasible_initial_layouts(
    stm32_board: BoardSnapshot, rulepack: ManufacturingRulePack
) -> None:
    result = PlacementSolver().solve(stm32_board, rulepack, seed=23, starts=4, iterations=0)

    assert len(result.evidence.seed_runs) == 4
    assert len({item.start_placement_digest for item in result.evidence.seed_runs}) == 4
    assert tuple(item.start_score for item in result.evidence.seed_runs) == result.evidence.start_scores
    assert all(item.start_score >= 0 for item in result.evidence.seed_runs)


def test_multi_start_evidence_reports_exhaustion_instead_of_repeating_a_locked_layout(
    stm32_board: BoardSnapshot, rulepack: ManufacturingRulePack
) -> None:
    all_locked = replace(
        stm32_board,
        footprints=tuple(replace(item, placement_lock=True) for item in stm32_board.footprints),
    )

    result = PlacementSolver().solve(all_locked, rulepack, seed=27, starts=4, iterations=10)

    assert result.evidence.starts == 4
    assert result.evidence.actual_starts == 1
    assert result.evidence.starts_exhausted is True
    assert len(result.evidence.seed_runs) == 1


def test_operation_key_includes_outcome_defining_search_parameters(
    stm32_board: BoardSnapshot, rulepack: ManufacturingRulePack
) -> None:
    short = PlacementSolver().solve(stm32_board, rulepack, seed=29, starts=1, iterations=0)
    long = PlacementSolver().solve(stm32_board, rulepack, seed=29, starts=2, iterations=0)

    assert short.operations[0].idempotency_key != long.operations[0].idempotency_key


def test_cp_sat_allows_required_displacement_larger_than_200_mm(
    stm32_board: BoardSnapshot, rulepack: ManufacturingRulePack
) -> None:
    far_relocation = replace(
        stm32_board,
        outline=(
            PointUm(0, 0),
            PointUm(500_000, 0),
            PointUm(500_000, 80_000),
            PointUm(0, 80_000),
        ),
        footprints=tuple(
            replace(footprint, keepout_ids=())
            if str(footprint.id) == "K1"
            else replace(footprint, layer="B.Cu", placement_lock=True)
            for footprint in stm32_board.footprints
        ),
        keepouts=stm32_board.keepouts
        + (
            Keepout(
                BoardObjectId("ko_k1_far_relocation"),
                "mechanical",
                RectUm(0, 0, 250_000, 80_000),
                ("F.Cu",),
                ("footprint",),
            ),
        ),
    )

    result = PlacementSolver().solve(far_relocation, rulepack, seed=37, starts=1, iterations=0)

    assert result.findings == ()
    assert abs(result.placements_by_id["K1"].position.x - far_relocation.footprint("K1").position.x) > 200_000


def test_thermal_and_connector_objectives_contrast_legal_boardir_positions(
    stm32_board: BoardSnapshot,
    rulepack: ManufacturingRulePack,
) -> None:
    base = {str(item.id): item.position for item in stm32_board.footprints}
    thermal_close = dict(base)
    thermal_close["Q2"] = PointUm(65_000, 68_000)
    thermal_far = dict(base)
    thermal_far["Q2"] = PointUm(70_000, 58_000)
    connector_edge = dict(base)
    connector_edge["J_SWD"] = PointUm(15_000, 30_000)
    connector_inner = dict(base)
    connector_inner["J_SWD"] = PointUm(25_000, 30_000)

    for positions in (thermal_close, thermal_far, connector_edge, connector_inner):
        locked = replace(
            stm32_board,
            footprints=tuple(
                replace(item, position=positions[str(item.id)], placement_lock=True)
                for item in stm32_board.footprints
            ),
        )
        assert PlacementSolver().solve(locked, rulepack, seed=1, starts=1, iterations=0).findings == ()

    assert thermal_cluster_penalty(stm32_board, thermal_far) < thermal_cluster_penalty(stm32_board, thermal_close)
    assert connector_access_penalty(stm32_board, connector_edge) < connector_access_penalty(stm32_board, connector_inner)


def test_nonzero_iteration_evidence_freezes_cp_sat_start_before_refinement(
    stm32_board: BoardSnapshot, rulepack: ManufacturingRulePack
) -> None:
    zero = PlacementSolver().solve(stm32_board, rulepack, seed=43, starts=1, iterations=0)
    refined = PlacementSolver().solve(stm32_board, rulepack, seed=43, starts=1, iterations=40)

    assert refined.evidence.seed_runs[0].start_placement_digest == zero.evidence.seed_runs[0].start_placement_digest
    assert refined.evidence.seed_runs[0].start_score == zero.evidence.seed_runs[0].start_score
    assert refined.evidence.seed_runs[0].refined_placement_digest == refined.evidence.placement_digest
    assert refined.evidence.seed_runs[0].start_placement_digest != refined.evidence.seed_runs[0].refined_placement_digest


def test_cp_sat_no_good_finds_second_layout_requiring_two_footprints_to_swap_together(
    stm32_board: BoardSnapshot, rulepack: ManufacturingRulePack
) -> None:
    swapped = BoardSnapshot(
        schema_version="1.0", profile_id=rulepack.profile_id, copper_oz=1,
        outline=(PointUm(0, 0), PointUm(30_000, 0), PointUm(30_000, 10_000), PointUm(0, 10_000)),
        layers=("F.Cu",), net_classes=(), nets=(),
        keepouts=(
            Keepout(
                BoardObjectId("ko_middle"),
                "mechanical",
                RectUm(10_001, 0, 9_998, 10_000),
                ("F.Cu",),
                ("footprint",),
            ),
        ),
        footprints=(
            Footprint(BoardObjectId("A"), PointUm(5_000, 5_000), 10_000, 10_000, "F.Cu", (), (), False),
            Footprint(BoardObjectId("B"), PointUm(25_000, 5_000), 10_000, 10_000, "F.Cu", (), (), False),
        ),
        pads=(), routes=(), vias=(), copper_zones=(), opaque_nodes=(),
    )
    result = PlacementSolver().solve(swapped, rulepack, seed=47, starts=2, iterations=0)
    locked_left_right = replace(
        swapped,
        footprints=(
            replace(swapped.footprint("A"), placement_lock=True),
            replace(swapped.footprint("B"), placement_lock=True),
        ),
    )
    locked_right_left = replace(
        locked_left_right,
        footprints=(
            replace(locked_left_right.footprint("A"), position=PointUm(25_000, 5_000)),
            replace(locked_left_right.footprint("B"), position=PointUm(5_000, 5_000)),
        ),
    )
    locked_same_left = replace(
        locked_left_right,
        footprints=(
            locked_left_right.footprint("A"),
            replace(locked_left_right.footprint("B"), position=PointUm(5_000, 5_000)),
        ),
    )
    expected = {
        PlacementSolver().solve(locked_left_right, rulepack, seed=47, starts=1, iterations=0).evidence.placement_digest,
        PlacementSolver().solve(locked_right_left, rulepack, seed=47, starts=1, iterations=0).evidence.placement_digest,
    }
    assert PlacementSolver().solve(locked_same_left, rulepack, seed=47, starts=1, iterations=0).findings[0].rule_id == "PCB_PLACEMENT_INFEASIBLE"
    runs = result.evidence.seed_runs
    assert len(runs) == 2
    assert result.evidence.actual_starts == 2 and not result.evidence.starts_exhausted
    assert {item.start_placement_digest for item in runs} == expected


def test_solver_reports_a_stable_finding_when_no_legal_region_exists(
    stm32_board: BoardSnapshot, rulepack: ManufacturingRulePack
) -> None:
    blocked = replace(
        stm32_board,
        keepouts=stm32_board.keepouts
        + (
            Keepout(
                BoardObjectId("ko_no_placement"),
                "mechanical",
                RectUm(0, 0, 100_000, 80_000),
                ("F.Cu", "B.Cu"),
                ("footprint",),
            ),
        ),
    )

    first = PlacementSolver().solve(blocked, rulepack, seed=5, starts=2, iterations=10)
    second = PlacementSolver().solve(blocked, rulepack, seed=5, starts=2, iterations=10)

    assert first.operations == ()
    assert first.findings[0].rule_id == "PCB_PLACEMENT_NO_LEGAL_REGION"
    assert first.findings == second.findings
    assert first.evidence == second.evidence

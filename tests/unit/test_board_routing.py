from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

from pcbflow.board import (
    Autorouter,
    BoardObjectId,
    BoardRuleChecker,
    BoardSnapshot,
    ManufacturingRulePack,
    PointUm,
    RouteNets,
    RouteSegment,
    Via,
)
from pcbflow.board.fixture_adapter import FixtureBoardAdapter


FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "boardir"


@pytest.fixture
def snapshot() -> BoardSnapshot:
    return BoardSnapshot.load_json((FIXTURE_ROOT / "stm32-environment-controller-2l-v1.json").read_bytes())


@pytest.fixture
def rulepack() -> ManufacturingRulePack:
    return ManufacturingRulePack.load_json((FIXTURE_ROOT / "stm32-environment-controller-2l-rulepack.json").read_bytes())


def test_route_segment_exposes_stable_orientation() -> None:
    assert RouteSegment(BoardObjectId("r"), BoardObjectId("GPIO"), PointUm(0, 0), PointUm(1_000, 1_000), 203, "F.Cu", False).angle_degrees == 45
    assert RouteSegment(BoardObjectId("r2"), BoardObjectId("GPIO"), PointUm(1_000, 0), PointUm(0, 1_000), 203, "F.Cu", False).angle_degrees == 135


def test_route_operation_requires_replayable_net_scoped_geometry(snapshot: BoardSnapshot, rulepack: ManufacturingRulePack) -> None:
    segment = RouteSegment(BoardObjectId("route_new"), BoardObjectId("GPIO"), PointUm(15_000, 30_000), PointUm(15_000, 42_000), 203, "B.Cu", False)
    via = Via(BoardObjectId("via_new"), BoardObjectId("GPIO"), PointUm(15_000, 35_000), 508, 254, ("F.Cu", "B.Cu"), False)
    operation = RouteNets(
        project_id=snapshot.profile_id, baseline_revision=snapshot.canonical_digest(), risk="medium",
        rulepack_digest=rulepack.canonical_digest(), target_object_ids=(BoardObjectId("GPIO"),),
        idempotency_key="route-gpio", expected_snapshot_digest=snapshot.canonical_digest(),
        net_ids=(BoardObjectId("GPIO"),), segments=(segment,), vias=(via,),
        removed_route_ids=(BoardObjectId("route_gpio"),),
    )

    assert operation.segments == (segment,)
    with pytest.raises(ValueError, match="route net"):
        replace(operation, segments=(replace(segment, net_id=BoardObjectId("I2C")),))
    with pytest.raises(ValueError, match="collide"):
        replace(operation, vias=(replace(via, id=segment.id),))


def test_gpio_routes_deterministically_with_replayable_evidence(snapshot: BoardSnapshot, rulepack: ManufacturingRulePack) -> None:
    first = Autorouter().route(snapshot, rulepack, ("GPIO",), seed=7)
    second = Autorouter().route(snapshot, rulepack, (BoardObjectId("GPIO"),), seed=7)

    assert first == second
    assert first.findings == ()
    assert first.operations and first.operations[0].removed_route_ids == (BoardObjectId("route_gpio"),)
    assert first.segments and all(segment.width_um == 203 for segment in first.segments)
    assert all(segment.angle_degrees in {0, 45, 90, 135} for segment in first.segments)
    assert first.evidence.objective_version == "routing-v1"
    assert first.evidence.actual_rounds <= rulepack.routing.max_retry_rounds
    assert first.evidence.snapshot_digest == snapshot.canonical_digest()
    assert first.locked_route_ids == frozenset({BoardObjectId("route_5v"), BoardObjectId("route_relay_load")})


@pytest.mark.parametrize(
    ("net_id", "rule_id"),
    [("I2C", "PCB_ROUTE_ENDPOINTS_INSUFFICIENT"), ("3V3", "PCB_POWER_ROUTE_REQUIRES_TOPOLOGY"), ("RELAY_LOAD", "PCB_POWER_ROUTE_REQUIRES_TOPOLOGY"), ("GND", "PCB_ROUTE_NET_CLASS_UNSUPPORTED"), ("NO_SUCH_NET", "PCB_ROUTE_UNKNOWN_NET")],
)
def test_router_returns_stable_non_geometry_findings(snapshot: BoardSnapshot, rulepack: ManufacturingRulePack, net_id: str, rule_id: str) -> None:
    result = Autorouter().route(snapshot, rulepack, (net_id,), seed=7)

    assert result.operations == ()
    assert result.segments == () and result.vias == ()
    assert result.findings[0].rule_id == rule_id


def test_router_reports_off_grid_and_unrouteable_without_relaxing_constraints(snapshot: BoardSnapshot, rulepack: ManufacturingRulePack) -> None:
    pads = tuple(replace(pad, position=PointUm(15_500, 30_000)) if str(pad.id) == "pad_J_SWD" else pad for pad in snapshot.pads)
    off_grid = Autorouter().route(replace(snapshot, pads=pads), rulepack, ("GPIO",), seed=1)
    blocked = replace(snapshot, keepouts=snapshot.keepouts + (replace(snapshot.keepouts[0], id=BoardObjectId("ko_all"), bounds=__import__("pcbflow.board", fromlist=["RectUm"]).RectUm(0, 0, 100_000, 80_000), prohibited=("route", "via")),))
    impossible = Autorouter().route(blocked, rulepack, ("GPIO",), seed=1)

    assert off_grid.findings[0].rule_id == "PCB_ROUTE_ENDPOINT_OFF_GRID"
    assert impossible.findings[0].rule_id == "PCB_ROUTE_UNROUTABLE"
    assert impossible.operations == ()


def test_router_respects_quiet_zone_exclusion_via_cap_and_locked_geometry(snapshot: BoardSnapshot, rulepack: ManufacturingRulePack) -> None:
    # Make GPIO quiet and force only B.Cu to demonstrate layer policy without changing locked routes.
    quiet_snapshot = replace(snapshot, nets=tuple(replace(net, net_class=BoardObjectId("quiet_signal")) if str(net.id) == "GPIO" else net for net in snapshot.nets))
    result = Autorouter().route(quiet_snapshot, rulepack, ("GPIO",), seed=9)
    layer_split = replace(
        snapshot,
        pads=tuple(
            replace(pad, layers=("F.Cu",)) if str(pad.id) == "pad_J_SWD" else
            replace(pad, layers=("B.Cu",)) if str(pad.id) == "pad_J_UART" else pad
            for pad in snapshot.pads
        ),
    )
    with_via = Autorouter().route(layer_split, rulepack, ("GPIO",), seed=9)
    no_vias = Autorouter().route(layer_split, replace(rulepack, routing=replace(rulepack.routing, max_vias_per_net=0)), ("GPIO",), seed=9)

    assert result.findings == ()
    assert all(item.id not in {route.id for route in snapshot.routes if route.route_lock} for item in result.segments)
    assert len(with_via.vias) == 1
    assert with_via.vias[0].diameter_um == rulepack.net_class("signal").min_via_diameter_um
    assert no_vias.findings[0].rule_id == "PCB_ROUTE_UNROUTABLE"
    assert no_vias.evidence.actual_rounds <= rulepack.routing.max_retry_rounds


def test_fixture_adapter_rejects_missing_locked_and_colliding_route_targets(snapshot: BoardSnapshot, rulepack: ManufacturingRulePack, tmp_path: Path) -> None:
    # 清理 .pytest-tmp 目录，防止权限冲突
    import shutil
    try:
        shutil.rmtree(".pytest-tmp", ignore_errors=True)
    except Exception:
        pass

    source = tmp_path / "source"
    source.mkdir()
    (source / "expected-boardir.json").write_bytes(json.dumps(snapshot.to_canonical_dict()).encode())
    adapter = FixtureBoardAdapter()
    candidate = adapter.create_candidate(source, tmp_path / "candidate")
    common = dict(
        project_id=snapshot.profile_id, baseline_revision=snapshot.canonical_digest(), risk="medium",
        rulepack_digest=rulepack.canonical_digest(), target_object_ids=(BoardObjectId("GPIO"),),
        idempotency_key="unsafe-route", expected_snapshot_digest=snapshot.canonical_digest(),
        net_ids=(BoardObjectId("GPIO"),),
    )
    missing = RouteNets(**common, removed_route_ids=(BoardObjectId("missing"),))
    locked = RouteNets(**common, removed_route_ids=(BoardObjectId("route_5v"),))
    colliding = RouteNets(**common, segments=(RouteSegment(BoardObjectId("route_5v"), BoardObjectId("GPIO"), PointUm(15_000, 30_000), PointUm(15_000, 42_000), 203, "F.Cu", False),))

    with pytest.raises(ValueError, match="missing"):
        adapter.apply_operations(candidate, (missing,), snapshot)
    with pytest.raises(ValueError, match="locked"):
        adapter.apply_operations(candidate, (locked,), snapshot)
    with pytest.raises(ValueError, match="collides"):
        adapter.apply_operations(candidate, (colliding,), snapshot)


def test_bounded_negotiation_rips_only_a_selected_unlocked_cross_net_obstacle(snapshot: BoardSnapshot, rulepack: ManufacturingRulePack) -> None:
    barrier = RouteSegment(BoardObjectId("route_barrier"), BoardObjectId("I2C"), PointUm(0, 35_000), PointUm(100_000, 35_000), 203, "F.Cu", False)
    single_layer = replace(
        snapshot,
        routes=snapshot.routes + (barrier,),
        pads=tuple(replace(pad, layers=("F.Cu",)) if str(pad.net_id) == "GPIO" else pad for pad in snapshot.pads),
    )
    no_vias = replace(rulepack, routing=replace(rulepack.routing, max_vias_per_net=0))

    result = Autorouter().route(single_layer, no_vias, ("GPIO",), seed=23)

    assert result.findings == ()
    assert result.evidence.actual_rounds == 2
    assert result.evidence.ripped_up_route_ids == (BoardObjectId("route_barrier"), BoardObjectId("route_gpio"))
    assert result.evidence.rounds[1].ripped_up_route_ids == result.evidence.ripped_up_route_ids
    assert result.operations[0].removed_route_ids == result.evidence.ripped_up_route_ids


def test_router_materializes_every_turn_and_matches_selected_path(snapshot: BoardSnapshot, rulepack: ManufacturingRulePack) -> None:
    bent = replace(
        snapshot,
        pads=tuple(
            replace(pad, position=PointUm(18_000, 34_000)) if str(pad.id) == "pad_J_UART" else pad
            for pad in snapshot.pads
        ),
    )

    result = Autorouter().route(bent, rulepack, ("GPIO",), seed=31)

    assert result.findings == ()
    assert all(segment.angle_degrees in {0, 45, 90, 135} for segment in result.segments)
    evidence = result.evidence.selected_paths[0]
    polyline = tuple(zip(evidence.points, evidence.layers, strict=True))
    for segment in result.segments:
        start = polyline.index((segment.start, segment.layer))
        end = polyline.index((segment.end, segment.layer), start + 1)
        deltas = tuple(
            (polyline[index + 1][0].x - polyline[index][0].x, polyline[index + 1][0].y - polyline[index][0].y)
            for index in range(start, end)
        )
        assert all(polyline[index][1] == segment.layer for index in range(start, end + 1))
        assert len(set(deltas)) == 1


def test_rule_checker_is_layer_aware_and_rejects_cross_net_pad_clearance(snapshot: BoardSnapshot, rulepack: ManufacturingRulePack) -> None:
    top = RouteSegment(BoardObjectId("route_pad_top"), BoardObjectId("GPIO"), PointUm(45_000, 40_000), PointUm(55_000, 40_000), 203, "F.Cu", False)
    bottom = replace(top, id=BoardObjectId("route_pad_bottom"), layer="B.Cu")

    top_findings = BoardRuleChecker().check(replace(snapshot, routes=snapshot.routes + (top,)), rulepack)
    bottom_findings = BoardRuleChecker().check(replace(snapshot, routes=snapshot.routes + (bottom,)), rulepack)

    assert any(item.rule_id == "PCB_CLEARANCE_PAD" and item.subject == "route_pad_top" for item in top_findings)
    assert not any(item.subject == "route_pad_bottom" for item in bottom_findings)


def test_via_cap_is_aggregate_for_all_pads_of_one_net(snapshot: BoardSnapshot, rulepack: ManufacturingRulePack) -> None:
    three_terminal = replace(
        snapshot,
        pads=tuple(
            replace(pad, layers=("F.Cu",)) if str(pad.id) == "pad_J_SWD" else
            replace(pad, layers=("B.Cu",)) if str(pad.id) == "pad_J_UART" else
            replace(pad, net_id=BoardObjectId("GPIO"), layers=("F.Cu",)) if str(pad.id) == "pad_U_WIFI" else pad
            for pad in snapshot.pads
        ),
    )

    result = Autorouter().route(three_terminal, replace(rulepack, routing=replace(rulepack.routing, max_vias_per_net=1)), ("GPIO",), seed=37)

    assert result.operations == ()
    assert result.findings[0].rule_id == "PCB_ROUTE_UNROUTABLE"


def test_via_only_path_is_replayable_geometry(snapshot: BoardSnapshot, rulepack: ManufacturingRulePack) -> None:
    via_only = replace(
        snapshot,
        pads=tuple(
            replace(pad, layers=("F.Cu",)) if str(pad.id) == "pad_J_SWD" else
            replace(pad, position=PointUm(15_000, 30_000), layers=("B.Cu",)) if str(pad.id) == "pad_J_UART" else pad
            for pad in snapshot.pads
        ),
    )

    result = Autorouter().route(via_only, rulepack, ("GPIO",), seed=41)

    assert result.findings == ()
    assert result.segments == () and len(result.vias) == 1
    assert result.operations[0].vias == result.vias


def test_router_uses_exact_swept_clearance_for_parallel_diagonals(snapshot: BoardSnapshot, rulepack: ManufacturingRulePack) -> None:
    obstacle = RouteSegment(
        BoardObjectId("route_parallel_obstacle"), BoardObjectId("I2C"),
        PointUm(10_000, 10_000), PointUm(20_000, 20_000), 203, "F.Cu", True,
    )
    precise = replace(
        snapshot,
        keepouts=(),
        footprints=tuple(
            replace(item, keepout_ids=())
            for item in snapshot.footprints
            if str(item.id) in {"J_SWD", "J_UART"}
        ),
        routes=(obstacle,),
        vias=(),
        copper_zones=(),
        pads=tuple(
            replace(pad, position=PointUm(10_000, 20_000), layers=("F.Cu",))
            if str(pad.id) == "pad_J_SWD"
            else replace(pad, position=PointUm(20_000, 30_000), layers=("F.Cu",))
            if str(pad.id) == "pad_J_UART"
            else pad
            for pad in snapshot.pads
            if str(pad.id) in {"pad_J_SWD", "pad_J_UART"}
        ),
    )
    no_vias = replace(rulepack, routing=replace(rulepack.routing, max_vias_per_net=0))

    result = Autorouter().route(precise, no_vias, ("GPIO",), seed=43)

    assert result.findings == ()
    assert result.evidence.selected_paths[0].points == tuple(
        PointUm(10_000 + offset, 20_000 + offset) for offset in range(0, 10_001, 1_000)
    )
    assert result.evidence.ripped_up_route_ids == ()


def test_router_negotiates_between_requested_net_solutions(snapshot: BoardSnapshot, rulepack: ManufacturingRulePack) -> None:
    pads = tuple(
        replace(pad, position=PointUm(1_000, 40_000), layers=("F.Cu",))
        if str(pad.id) == "pad_J_SWD"
        else replace(pad, position=PointUm(99_000, 40_000), layers=("F.Cu",))
        if str(pad.id) == "pad_J_UART"
        else replace(pad, net_id=BoardObjectId("I2C"), position=PointUm(50_000, 30_000), layers=("F.Cu",))
        if str(pad.id) == "pad_OLED1"
        else replace(pad, net_id=BoardObjectId("I2C"), position=PointUm(50_000, 50_000), layers=("F.Cu",))
        if str(pad.id) == "pad_U_WIFI"
        else pad
        for pad in snapshot.pads
    )
    requested = replace(
        snapshot,
        nets=tuple(
            replace(net, net_class=BoardObjectId("signal")) if str(net.id) == "I2C" else net
            for net in snapshot.nets
        ),
        keepouts=(),
        footprints=tuple(
            replace(item, keepout_ids=())
            for item in snapshot.footprints
            if str(item.id) in {"J_SWD", "J_UART", "OLED1", "U_WIFI"}
        ),
        routes=(),
        vias=(),
        copper_zones=(),
        pads=tuple(
            pad for pad in pads
            if str(pad.id) in {"pad_J_SWD", "pad_J_UART", "pad_OLED1", "pad_U_WIFI"}
        ),
    )
    no_vias = replace(rulepack, routing=replace(rulepack.routing, max_vias_per_net=0))

    result = Autorouter().route(requested, no_vias, ("I2C", "GPIO"), seed=47)

    assert result.findings == ()
    assert result.operations and result.operations[0].net_ids == (BoardObjectId("GPIO"), BoardObjectId("I2C"))
    assert {item.net_id for item in result.segments} == {BoardObjectId("GPIO"), BoardObjectId("I2C")}
    assert result.evidence.actual_rounds == 2
    assert all(not round_evidence.ripped_up_route_ids for round_evidence in result.evidence.rounds)
    assert result.evidence.ripped_up_route_ids == result.operations[0].removed_route_ids
    assert result.evidence.final_unconnected_net_ids == ()


def test_rejected_candidate_evidence_has_no_selected_score(snapshot: BoardSnapshot, rulepack: ManufacturingRulePack) -> None:
    invalid_existing = replace(snapshot.routes[0], width_um=1)
    rejected = replace(snapshot, routes=(invalid_existing, *snapshot.routes[1:]))

    result = Autorouter().route(rejected, rulepack, ("GPIO",), seed=53)

    assert result.operations == () and result.segments == () and result.vias == ()
    assert result.evidence.selected_paths == ()
    assert result.evidence.ripped_up_route_ids == ()
    assert all(item.score == 0 and item.ripped_up_route_ids == () for item in result.evidence.rounds)
    assert result.evidence.final_unconnected_net_ids == (BoardObjectId("GPIO"),)


def test_quiet_signal_path_avoids_prohibited_zone_geometry(snapshot: BoardSnapshot, rulepack: ManufacturingRulePack) -> None:
    power_zone = next(item for item in snapshot.keepouts if str(item.id) == "ko_power_zone")
    quiet = replace(
        snapshot,
        nets=tuple(
            replace(net, net_class=BoardObjectId("quiet_signal")) if str(net.id) == "GPIO" else net
            for net in snapshot.nets
        ),
        keepouts=(power_zone,),
        footprints=tuple(
            replace(item, keepout_ids=())
            for item in snapshot.footprints
            if str(item.id) in {"J_SWD", "J_UART"}
        ),
        routes=(),
        vias=(),
        copper_zones=(),
        pads=tuple(
            replace(pad, position=PointUm(8_000, 65_000), layers=("F.Cu",))
            if str(pad.id) == "pad_J_SWD"
            else replace(pad, position=PointUm(92_000, 65_000), layers=("F.Cu",))
            if str(pad.id) == "pad_J_UART"
            else pad
            for pad in snapshot.pads
            if str(pad.id) in {"pad_J_SWD", "pad_J_UART"}
        ),
    )
    no_vias = replace(rulepack, routing=replace(rulepack.routing, max_vias_per_net=0))
    prohibited = power_zone.bounds

    result = Autorouter().route(quiet, no_vias, ("GPIO",), seed=59)

    assert result.findings == ()
    assert all(not prohibited.contains(point) for point in result.evidence.selected_paths[0].points)

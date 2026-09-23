from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from pcbflow.board import (
    BoardObjectId,
    BoardRuleChecker,
    BoardSnapshot,
    CopperPlanner,
    Keepout,
    ManufacturingRulePack,
    PointUm,
    RectUm,
    RouteSegment,
)
from pcbflow.board.copper import _is_axis_aligned_rectangle

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "boardir"


def _snapshot() -> BoardSnapshot:
    return BoardSnapshot.load_json(
        (FIXTURE_ROOT / "stm32-environment-controller-2l-v1.json").read_bytes()
    )


def _rulepack() -> ManufacturingRulePack:
    return ManufacturingRulePack.load_json(
        (FIXTURE_ROOT / "stm32-environment-controller-2l-rulepack.json").read_bytes()
    )


def _snapshot_4l() -> BoardSnapshot:
    return BoardSnapshot.load_json(
        (FIXTURE_ROOT / "stm32-environment-controller-4l-v1.json").read_bytes()
    )


def _rulepack_4l() -> ManufacturingRulePack:
    return ManufacturingRulePack.load_json(
        (FIXTURE_ROOT / "stm32-environment-controller-4l-rulepack.json").read_bytes()
    )


def _snapshot_with_ground_pad() -> BoardSnapshot:
    snapshot = _snapshot()
    pads = tuple(
        replace(item, net_id=BoardObjectId("GND"))
        if item.id == BoardObjectId("pad_MH1")
        else item
        for item in snapshot.pads
    )
    return replace(snapshot, pads=pads)


def _snapshot_with_ground_route() -> BoardSnapshot:
    snapshot = _snapshot()
    ground_route = RouteSegment(
        id=BoardObjectId("route_gnd_anchor"),
        net_id=BoardObjectId("GND"),
        start=PointUm(60_000, 12_000),
        end=PointUm(70_000, 12_000),
        width_um=508,
        layer="F.Cu",
        route_lock=True,
    )
    return replace(
        snapshot,
        routes=snapshot.routes + (ground_route,),
        copper_zones=(),
    )


def _snapshot_with_copper_barrier() -> BoardSnapshot:
    snapshot = _snapshot()
    barrier = Keepout(
        id=BoardObjectId("ko_test_copper_barrier"),
        kind="mechanical",
        bounds=RectUm(48_000, 0, 4_000, 80_000),
        layers=("F.Cu", "B.Cu"),
        prohibited=("copper_zone", "via"),
    )
    return replace(snapshot, copper_zones=(), keepouts=snapshot.keepouts + (barrier,))


def test_copper_plan_excludes_keepouts_and_emits_both_ground_layers() -> None:
    snapshot = _snapshot()
    rulepack = _rulepack()

    result = CopperPlanner().plan(snapshot, rulepack)

    assert {zone.layer for zone in result.zones} == {"F.Cu", "B.Cu"}
    assert all(zone.net_id == BoardObjectId("GND") for zone in result.zones)
    for zone in result.zones:
        assert all(
            not (
                "copper_zone" in keepout.prohibited
                and zone.layer in keepout.layers
                and zone.bounds.intersects(keepout.bounds)
            )
            for keepout in snapshot.keepouts
        )
        assert zone.bounds.x >= rulepack.copper.edge_clearance_um
        assert zone.bounds.y >= rulepack.copper.edge_clearance_um
    assert result.evidence.snapshot_digest == snapshot.canonical_digest()
    assert result.evidence.rulepack_digest == rulepack.canonical_digest()


def test_copper_plan_is_deterministic_for_identical_inputs() -> None:
    snapshot = _snapshot()
    rulepack = _rulepack()

    first = CopperPlanner().plan(snapshot, rulepack)
    second = CopperPlanner().plan(snapshot, rulepack)

    assert first == second


def test_copper_plan_selects_rulepack_thermal_relief_for_ground_pads() -> None:
    snapshot = _snapshot_with_ground_pad()
    rulepack = _rulepack()

    result = CopperPlanner().plan(snapshot, rulepack)

    policy = next(
        item for item in result.thermal_policies if item.pad_id == BoardObjectId("pad_MH1")
    )
    assert policy.style == "thermal_relief"
    assert policy.locked is True
    assert policy.spoke_count == rulepack.copper.thermal_spoke_count
    assert policy.spoke_width_um == rulepack.copper.thermal_spoke_width_um
    assert policy.gap_um == rulepack.copper.thermal_gap_um
    assert result.evidence.thermal_policy_pad_ids == (policy.pad_id,)
    assert len(result.evidence.thermal_policy_digests) == 1
    assert result.evidence.thermal_policy_digests[0].startswith("sha256:")


def test_copper_plan_does_not_emit_unapplied_thermal_policies_without_zones() -> None:
    snapshot = replace(_snapshot_with_ground_pad(), vias=(), copper_zones=())

    result = CopperPlanner().plan(snapshot, _rulepack())

    assert result.zones == ()
    assert result.operations == ()
    assert result.thermal_policies == ()
    assert result.evidence.thermal_policy_pad_ids == ()
    assert result.evidence.thermal_policy_digests == ()


def test_copper_plan_anchors_tiles_to_ground_routes() -> None:
    result = CopperPlanner().plan(_snapshot_with_ground_route(), _rulepack())

    assert any(zone.layer == "F.Cu" for zone in result.zones)


def test_stitching_vias_use_continuous_ground_and_avoid_quiet_or_edge_regions() -> None:
    snapshot = _snapshot()
    rulepack = _rulepack()

    result = CopperPlanner().plan(snapshot, rulepack)

    assert result.stitching_policy.net_id == BoardObjectId("GND")
    assert len(result.vias) <= rulepack.copper.max_stitching_vias
    ground_rule = rulepack.net_class("ground")
    for via in result.vias:
        assert via.net_id == BoardObjectId("GND")
        assert via.diameter_um >= ground_rule.min_via_diameter_um
        assert via.hole_diameter_um >= ground_rule.min_via_hole_um
        assert via.position.x >= rulepack.copper.edge_clearance_um + via.diameter_um // 2
        assert via.position.y >= rulepack.copper.edge_clearance_um + via.diameter_um // 2
        assert all(
            not (
                keepout.kind in {"esp_antenna", "quiet_zone", "crystal_near_field"}
                and keepout.bounds.contains(via.position)
            )
            for keepout in snapshot.keepouts
        )
        assert any(
            zone.layer == "F.Cu"
            and zone.bounds.x <= via.position.x - via.diameter_um // 2
            and zone.bounds.y <= via.position.y - via.diameter_um // 2
            and zone.bounds.x + zone.bounds.width >= via.position.x + via.diameter_um // 2
            and zone.bounds.y + zone.bounds.height >= via.position.y + via.diameter_um // 2
            for zone in snapshot.copper_zones + result.zones
        )
        assert any(
            zone.layer == "B.Cu"
            and zone.bounds.x <= via.position.x - via.diameter_um // 2
            and zone.bounds.y <= via.position.y - via.diameter_um // 2
            and zone.bounds.x + zone.bounds.width >= via.position.x + via.diameter_um // 2
            and zone.bounds.y + zone.bounds.height >= via.position.y + via.diameter_um // 2
            for zone in snapshot.copper_zones + result.zones
        )


def test_copper_plan_reports_a_discarded_island_without_submitting_it() -> None:
    snapshot = _snapshot_with_copper_barrier()
    rulepack = _rulepack()

    result = CopperPlanner().plan(snapshot, rulepack)

    assert any(item.rule_id == "PCB.COPPER.ISLAND" for item in result.findings)
    assert all(zone.bounds.x + zone.bounds.width <= 48_000 or zone.bounds.x >= 52_000 for zone in result.zones)


def test_copper_plan_rejects_existing_disconnected_ground_zone() -> None:
    snapshot = _snapshot()
    disconnected = replace(
        snapshot.copper_zones[0],
        id=BoardObjectId("zone_gnd_disconnected"),
        bounds=RectUm(70_000, 30_000, 10_000, 10_000),
    )
    snapshot = replace(snapshot, copper_zones=snapshot.copper_zones + (disconnected,))

    result = CopperPlanner().plan(snapshot, _rulepack())

    assert result.operations == ()
    assert any(item.rule_id == "PCB.COPPER.CONNECTIVITY" for item in result.findings)


def test_copper_plan_fails_closed_for_non_rectangular_outline() -> None:
    snapshot = _snapshot()
    outline = (
        PointUm(0, 0),
        PointUm(100_000, 0),
        PointUm(100_000, 80_000),
        PointUm(50_000, 80_000),
        PointUm(50_000, 90_000),
        PointUm(0, 80_000),
    )

    result = CopperPlanner().plan(replace(snapshot, outline=outline), _rulepack())

    assert result.operations == ()
    assert any(item.rule_id == "PCB.COPPER.OUTLINE_UNSUPPORTED" for item in result.findings)


def test_copper_rectangle_detection_rejects_self_intersecting_outline() -> None:
    outline = (
        PointUm(0, 0),
        PointUm(100_000, 80_000),
        PointUm(100_000, 0),
        PointUm(0, 80_000),
    )

    assert _is_axis_aligned_rectangle(outline) is False


def test_board_checker_rejects_copper_zone_clearance_and_edge_violations() -> None:
    snapshot = _snapshot()
    foreign = replace(
        snapshot.copper_zones[0],
        id=BoardObjectId("zone_gnd_bad_clearance"),
        bounds=RectUm(14_000, 28_000, 4_000, 4_000),
    )
    edge = replace(
        snapshot.copper_zones[1],
        id=BoardObjectId("zone_gnd_bad_edge"),
        bounds=RectUm(0, 10_000, 5_000, 5_000),
    )
    findings = BoardRuleChecker().check(
        replace(snapshot, copper_zones=snapshot.copper_zones + (foreign, edge)), _rulepack()
    )

    assert any(item.rule_id == "PCB.COPPER.CLEARANCE" and item.subject == "zone_gnd_bad_clearance" for item in findings)
    assert any(item.rule_id == "PCB.COPPER.EDGE" and item.subject == "zone_gnd_bad_edge" for item in findings)


def test_board_checker_rejects_copper_in_sensitive_quiet_keepout() -> None:
    snapshot = _snapshot()
    quiet = snapshot.keepout("ko_adc_i2c_quiet")
    bad = replace(
        snapshot.copper_zones[0],
        id=BoardObjectId("zone_gnd_bad_quiet"),
        bounds=quiet.bounds,
    )

    findings = BoardRuleChecker().check(
        replace(snapshot, copper_zones=snapshot.copper_zones + (bad,)), _rulepack()
    )

    assert any(
        item.rule_id == "PCB_KEEPOUT_COPPER_ZONE" and item.subject == "zone_gnd_bad_quiet"
        for item in findings
    )


def test_four_layer_copper_plan_pours_every_layer_and_stitches_the_stack() -> None:
    snapshot = _snapshot_4l()
    rulepack = _rulepack_4l()

    assert BoardRuleChecker().check(snapshot, rulepack) == ()

    result = CopperPlanner().plan(snapshot, rulepack)

    assert {zone.layer for zone in result.zones} == {
        "F.Cu",
        "In1.Cu",
        "In2.Cu",
        "B.Cu",
    }
    assert result.vias, "four-layer board must still get ground stitching vias"
    # Stitching vias must span every ground layer the point is covered by,
    # which on this fixture includes the inner layers.
    assert all(via.net_id == BoardObjectId("GND") for via in result.vias)
    assert any(len(via.layers) == 4 for via in result.vias)
    for via in result.vias:
        assert 2 <= len(via.layers) <= 4
        assert set(via.layers) <= set(snapshot.layers)


def test_four_layer_copper_plan_is_deterministic() -> None:
    snapshot = _snapshot_4l()
    rulepack = _rulepack_4l()

    assert CopperPlanner().plan(snapshot, rulepack) == CopperPlanner().plan(
        snapshot, rulepack
    )

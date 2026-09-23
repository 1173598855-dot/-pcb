from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from pcbflow.board import (
    BoardObjectId,
    BoardRuleChecker,
    BoardSnapshot,
    BoardWriteRejectedError,
    ManufacturingRulePack,
    PointUm,
    RouteSegment,
    Via,
    validate_proposed_snapshot,
)

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "boardir"


def _snapshot() -> BoardSnapshot:
    return BoardSnapshot.load_json(
        (FIXTURE_ROOT / "stm32-environment-controller-2l-v1.json").read_bytes()
    )


def _rulepack() -> ManufacturingRulePack:
    return ManufacturingRulePack.load_json(
        (FIXTURE_ROOT / "stm32-environment-controller-2l-rulepack.json").read_bytes()
    )


def test_valid_stm32_fixture_has_no_board_rule_findings() -> None:
    assert BoardRuleChecker().check(_snapshot(), _rulepack()) == ()


def test_route_inside_esp_antenna_keepout_has_stable_finding() -> None:
    snapshot = _snapshot()
    route = replace(
        snapshot.routes[0],
        id=BoardObjectId("route_in_esp_antenna"),
        start=PointUm(90_000, 10_000),
        end=PointUm(95_000, 10_000),
        route_lock=False,
    )
    proposed = replace(snapshot, routes=snapshot.routes + (route,))

    findings = BoardRuleChecker().check(proposed, _rulepack())

    assert any(
        item.rule_id == "PCB_KEEPOUT_ROUTE"
        and item.subject == "route_in_esp_antenna"
        and "ko_esp_antenna" in item.message
        for item in findings
    )


def test_power_trace_thinner_than_net_class_has_stable_finding() -> None:
    snapshot = _snapshot()
    routes = tuple(
        replace(item, width_um=508)
        if item.id == BoardObjectId("route_relay_load")
        else item
        for item in snapshot.routes
    )

    findings = BoardRuleChecker().check(replace(snapshot, routes=routes), _rulepack())

    assert any(
        item.rule_id == "PCB_TRACE_WIDTH_BELOW_MINIMUM"
        and item.subject == "route_relay_load"
        and "2032" in item.message
        for item in findings
    )


def test_unknown_snapshot_net_class_has_stable_finding() -> None:
    snapshot = _snapshot()
    original_class = snapshot.net_classes[0]
    unsupported_class = replace(
        original_class,
        id=BoardObjectId("vendor_signal"),
    )
    nets = tuple(
        replace(item, net_class=unsupported_class.id)
        if item.net_class == original_class.id
        else item
        for item in snapshot.nets
    )
    proposed = replace(
        snapshot,
        net_classes=(unsupported_class, *snapshot.net_classes[1:]),
        nets=nets,
    )

    findings = BoardRuleChecker().check(proposed, _rulepack())

    assert any(
        item.rule_id == "PCB_NET_CLASS_UNSUPPORTED"
        and item.subject == "vendor_signal"
        for item in findings
    )


def test_rule_checker_reports_same_layer_route_and_via_clearance() -> None:
    snapshot = _snapshot()
    crossing = RouteSegment(
        BoardObjectId("route_crossing"), BoardObjectId("GPIO"),
        PointUm(20_000, 3_000), PointUm(20_000, 5_000), 203, "F.Cu", False,
    )
    through_via = RouteSegment(
        BoardObjectId("route_through_via"), BoardObjectId("GPIO"),
        PointUm(25_000, 25_000), PointUm(35_000, 25_000), 203, "F.Cu", False,
    )

    findings = BoardRuleChecker().check(
        replace(snapshot, routes=snapshot.routes + (crossing, through_via)), _rulepack()
    )

    assert any(item.rule_id == "PCB_CLEARANCE_ROUTE" and item.subject == "route_crossing" for item in findings)
    assert any(item.rule_id == "PCB_CLEARANCE_VIA" and item.subject == "route_through_via" for item in findings)


def test_rule_checker_reports_via_to_pad_and_via_clearance() -> None:
    snapshot = _snapshot()
    pad_via = Via(
        BoardObjectId("via_at_foreign_pad"), BoardObjectId("GPIO"), PointUm(50_000, 40_000),
        508, 254, ("F.Cu", "B.Cu"), False,
    )
    via_via = Via(
        BoardObjectId("via_near_foreign_via"), BoardObjectId("GPIO"), PointUm(30_000, 25_000),
        508, 254, ("F.Cu", "B.Cu"), False,
    )

    findings = BoardRuleChecker().check(
        replace(snapshot, vias=snapshot.vias + (pad_via, via_via)), _rulepack()
    )

    assert any(item.rule_id == "PCB_CLEARANCE_PAD" and item.subject == "via_at_foreign_pad" for item in findings)
    assert any(item.rule_id == "PCB_CLEARANCE_VIA" and "via_near_foreign_via" in item.message for item in findings)


def test_rule_checker_reports_disconnected_multi_terminal_route() -> None:
    snapshot = _snapshot()
    first = RouteSegment(
        BoardObjectId("route_gpio_first_island"), BoardObjectId("GPIO"),
        PointUm(15_000, 30_000), PointUm(20_000, 30_000), 203, "B.Cu", False,
    )
    second = RouteSegment(
        BoardObjectId("route_gpio_second_island"), BoardObjectId("GPIO"),
        PointUm(15_000, 42_000), PointUm(20_000, 42_000), 203, "B.Cu", False,
    )
    routes = tuple(item for item in snapshot.routes if str(item.id) != "route_gpio") + (first, second)

    findings = BoardRuleChecker().check(replace(snapshot, routes=routes), _rulepack())

    assert any(item.rule_id == "PCB_ROUTE_DISCONNECTED" and item.subject == "GPIO" for item in findings)


def test_connectivity_requires_route_layer_to_touch_each_pad() -> None:
    original = _snapshot()
    pads = tuple(
        replace(item, layers=("F.Cu",))
        if item.id == BoardObjectId("pad_J_SWD")
        else replace(item, layers=("B.Cu",))
        if item.id == BoardObjectId("pad_J_UART")
        else item
        for item in original.pads
    )
    route = RouteSegment(
        BoardObjectId("route_gpio_wrong_layer"),
        BoardObjectId("GPIO"),
        PointUm(15_000, 30_000),
        PointUm(15_000, 42_000),
        203,
        "F.Cu",
        False,
    )
    routes = (route, replace(route, id=BoardObjectId("route_gpio_other_layer"), layer="B.Cu"))

    findings = BoardRuleChecker().check(
        replace(original, pads=pads, routes=routes), _rulepack()
    )

    assert any(
        item.rule_id == "PCB_ROUTE_DISCONNECTED" and item.subject == "GPIO"
        for item in findings
    )


def test_proposed_write_rejects_locked_footprint_move() -> None:
    original = _snapshot()
    footprints = tuple(
        replace(item, position=PointUm(item.position.x + 1, item.position.y))
        if item.id == BoardObjectId("J_USB_C")
        else item
        for item in original.footprints
    )

    with pytest.raises(BoardWriteRejectedError, match="locked footprint moved: J_USB_C"):
        validate_proposed_snapshot(original, replace(original, footprints=footprints))


def test_proposed_write_rejects_locked_route_change() -> None:
    original = _snapshot()
    routes = tuple(
        replace(item, width_um=item.width_um + 1)
        if item.id == BoardObjectId("route_5v")
        else item
        for item in original.routes
    )

    with pytest.raises(BoardWriteRejectedError, match="locked route changed: route_5v"):
        validate_proposed_snapshot(original, replace(original, routes=routes))


def test_proposed_write_rejects_opaque_node_omission_or_rewrite() -> None:
    original = _snapshot()

    with pytest.raises(BoardWriteRejectedError, match="opaque node omitted"):
        validate_proposed_snapshot(original, replace(original, opaque_nodes=()))

    rewritten = replace(original.opaque_nodes[0], native_type="lceda.rewritten")
    with pytest.raises(BoardWriteRejectedError, match="opaque node changed"):
        validate_proposed_snapshot(
            original,
            replace(original, opaque_nodes=(rewritten,)),
        )

from __future__ import annotations

from collections.abc import Iterable

from pcbflow.domain import NormalizedFinding

from .ir import (
    BoardObjectId,
    BoardSnapshot,
    CopperZone,
    Footprint,
    Keepout,
    NetClass,
    Pad,
    PointUm,
    RectUm,
    RouteSegment,
    Via,
)
from . import geometry
from .rulepack import ManufacturingRulePack


class BoardWriteRejectedError(ValueError):
    """A proposed BoardIR write would violate preserved native constraints."""


class BoardRuleChecker:
    def check(
        self, snapshot: BoardSnapshot, rulepack: ManufacturingRulePack
    ) -> tuple[NormalizedFinding, ...]:
        findings: list[NormalizedFinding] = []
        if snapshot.profile_id != rulepack.profile_id:
            findings.append(
                _finding(
                    "PCB_PROFILE_MISMATCH",
                    snapshot.profile_id,
                    f"snapshot requires rule pack {snapshot.profile_id}",
                )
            )
        if len(snapshot.layers) != rulepack.layer_count:
            findings.append(
                _finding(
                    "PCB_LAYER_COUNT_UNSUPPORTED",
                    snapshot.profile_id,
                    f"expected {rulepack.layer_count} copper layers",
                )
            )
        if snapshot.copper_oz != rulepack.copper_oz:
            findings.append(
                _finding(
                    "PCB_COPPER_WEIGHT_MISMATCH",
                    snapshot.profile_id,
                    f"expected {rulepack.copper_oz} oz copper",
                )
            )
        width, height = snapshot.board_size_um
        if width > rulepack.max_board_width_um or height > rulepack.max_board_height_um:
            findings.append(
                _finding(
                    "PCB_BOARD_SIZE_EXCEEDED",
                    snapshot.profile_id,
                    "board outline exceeds the V1 rule pack maximum",
                )
            )

        net_classes = {item.id: item for item in rulepack.net_classes}
        nets = {item.id: item for item in snapshot.nets}
        for board_class in snapshot.net_classes:
            rule = net_classes.get(board_class.id)
            if rule is None:
                findings.append(
                    _finding(
                        "PCB_NET_CLASS_UNSUPPORTED",
                        board_class.id,
                        f"net class {board_class.id} is absent from the rule pack",
                    )
                )
                continue
            findings.extend(_check_net_class(board_class, rule))
        for route in snapshot.routes:
            net = nets[route.net_id]
            rule = net_classes.get(net.net_class)
            if rule is None:
                continue
            if route.width_um < rule.min_width_um:
                findings.append(
                    _finding(
                        "PCB_TRACE_WIDTH_BELOW_MINIMUM",
                        route.id,
                        f"route width {route.width_um} um is below {rule.min_width_um} um",
                    )
                )
            findings.extend(_route_keepout_findings(route, net.net_class, snapshot.keepouts))
            findings.extend(_route_clearance_findings(route, snapshot, rulepack))
        for via in snapshot.vias:
            net = nets[via.net_id]
            rule = net_classes.get(net.net_class)
            if rule is None:
                continue
            if via.diameter_um < rule.min_via_diameter_um:
                findings.append(
                    _finding(
                        "PCB_VIA_DIAMETER_BELOW_MINIMUM",
                        via.id,
                        f"via diameter {via.diameter_um} um is below {rule.min_via_diameter_um} um",
                    )
                )
            if via.hole_diameter_um < rule.min_via_hole_um:
                findings.append(
                    _finding(
                        "PCB_VIA_HOLE_BELOW_MINIMUM",
                        via.id,
                        f"via hole {via.hole_diameter_um} um is below {rule.min_via_hole_um} um",
                    )
                )
            for keepout in snapshot.keepouts:
                if (
                    ("via" in keepout.prohibited
                    or (via.net_id == BoardObjectId("GND") and keepout.kind in {"esp_antenna", "quiet_zone", "crystal_near_field", "sensitive_analog"}))
                    and set(via.layers) & set(keepout.layers)
                    and keepout.bounds.contains(via.position)
                ):
                    findings.append(
                        _finding(
                            "PCB.COPPER.KEEPOUT" if via.net_id == BoardObjectId("GND") else "PCB_KEEPOUT_VIA",
                            via.id,
                            f"via intersects hard keepout {keepout.id}",
                        )
                    )
            findings.extend(_via_edge_findings(via, snapshot, rulepack))
        for zone in snapshot.copper_zones:
            net = nets[zone.net_id]
            rule = net_classes.get(net.net_class)
            if rule is None:
                continue
            if zone.clearance_um < rule.clearance_um:
                findings.append(
                    _finding(
                        "PCB_ZONE_CLEARANCE_BELOW_MINIMUM",
                        zone.id,
                        f"zone clearance {zone.clearance_um} um is below {rule.clearance_um} um",
                    )
                )
            findings.extend(_zone_keepout_findings(zone, snapshot.keepouts))
            findings.extend(_zone_edge_findings(zone, snapshot, rulepack))
            findings.extend(_zone_clearance_findings(zone, snapshot, rulepack))
        for footprint in snapshot.footprints:
            findings.extend(_footprint_keepout_findings(footprint, snapshot.keepouts))
        findings.extend(_route_route_clearance_findings(snapshot, rulepack))
        findings.extend(_route_via_clearance_findings(snapshot, rulepack))
        findings.extend(_via_pad_clearance_findings(snapshot, rulepack))
        findings.extend(_via_via_clearance_findings(snapshot, rulepack))
        findings.extend(_connectivity_findings(snapshot, rulepack))
        findings.extend(_copper_connectivity_findings(snapshot, rulepack))

        return tuple(
            sorted(
                findings,
                key=lambda item: (item.rule_id, item.subject, item.message, item.severity),
            )
        )


def validate_proposed_snapshot(
    original: BoardSnapshot, proposed: BoardSnapshot
) -> None:
    proposed_footprints = {item.id: item for item in proposed.footprints}
    for item in original.footprints:
        if not item.placement_lock:
            continue
        candidate = proposed_footprints.get(item.id)
        if (
            candidate is None
            or not candidate.placement_lock
            or candidate.position != item.position
            or candidate.layer != item.layer
        ):
            raise BoardWriteRejectedError(f"locked footprint moved: {item.id}")

    for collection_name in ("routes", "vias", "copper_zones"):
        original_items = getattr(original, collection_name)
        proposed_items = {item.id: item for item in getattr(proposed, collection_name)}
        for item in original_items:
            if not item.route_lock:
                continue
            if proposed_items.get(item.id) != item:
                kind = "route" if collection_name == "routes" else collection_name[:-1]
                raise BoardWriteRejectedError(f"locked {kind} changed: {item.id}")

    proposed_opaque = {item.id: item for item in proposed.opaque_nodes}
    for item in original.opaque_nodes:
        candidate = proposed_opaque.get(item.id)
        if candidate is None:
            raise BoardWriteRejectedError(f"opaque node omitted: {item.id}")
        if candidate != item:
            raise BoardWriteRejectedError(f"opaque node changed: {item.id}")


def _check_net_class(
    board_class: NetClass, rule: NetClass
) -> Iterable[NormalizedFinding]:
    values = (
        ("min_width_um", board_class.min_width_um, rule.min_width_um),
        ("clearance_um", board_class.clearance_um, rule.clearance_um),
        (
            "min_via_diameter_um",
            board_class.min_via_diameter_um,
            rule.min_via_diameter_um,
        ),
        ("min_via_hole_um", board_class.min_via_hole_um, rule.min_via_hole_um),
    )
    for name, actual, minimum in values:
        if actual < minimum:
            yield _finding(
                "PCB_NET_CLASS_BELOW_RULEPACK",
                board_class.id,
                f"{name} {actual} um is below rule pack minimum {minimum} um",
            )


def _route_keepout_findings(
    route: RouteSegment,
    net_class_id: BoardObjectId,
    keepouts: tuple[Keepout, ...],
) -> Iterable[NormalizedFinding]:
    for keepout in keepouts:
        if route.layer not in keepout.layers:
            continue
        prohibited = set(keepout.prohibited)
        applies = (
            "route" in prohibited
            or "unrelated_route" in prohibited
            or (
                "load_power_route" in prohibited
                and str(net_class_id) == "load_power"
            )
            or (
                "quiet_signal_route" in prohibited
                and str(net_class_id) == "quiet_signal"
            )
        )
        if applies and _segment_rect_within(route.start, route.end, keepout.bounds, route.width_um // 2):
            yield _finding(
                "PCB_KEEPOUT_ROUTE",
                route.id,
                f"route intersects hard keepout {keepout.id}",
            )


def _route_clearance_findings(
    route: RouteSegment, snapshot: BoardSnapshot, rulepack: ManufacturingRulePack
) -> Iterable[NormalizedFinding]:
    route_clearance = rulepack.net_class(snapshot.net(route.net_id).net_class).clearance_um
    for pad in snapshot.pads:
        if pad.net_id == route.net_id or route.layer not in pad.layers:
            continue
        pad_clearance = _net_clearance(rulepack, snapshot, pad.net_id)
        if _segment_rect_within(route.start, route.end, _pad_bounds(pad), route_clearance + pad_clearance + route.width_um // 2):
            yield _finding("PCB_CLEARANCE_PAD", route.id, f"route violates pad clearance at {pad.id}")


def _route_route_clearance_findings(snapshot: BoardSnapshot, rulepack: ManufacturingRulePack) -> Iterable[NormalizedFinding]:
    for index, route in enumerate(snapshot.routes):
        for other in snapshot.routes[index + 1:]:
            if route.net_id == other.net_id or route.layer != other.layer:
                continue
            required = route.width_um // 2 + other.width_um // 2 + _net_clearance(rulepack, snapshot, route.net_id) + _net_clearance(rulepack, snapshot, other.net_id)
            if _segments_within(route.start, route.end, other.start, other.end, required):
                yield _finding("PCB_CLEARANCE_ROUTE", route.id, f"route clearance violated by {other.id}")
                yield _finding("PCB_CLEARANCE_ROUTE", other.id, f"route clearance violated by {route.id}")


def _route_via_clearance_findings(snapshot: BoardSnapshot, rulepack: ManufacturingRulePack) -> Iterable[NormalizedFinding]:
    for route in snapshot.routes:
        for via in snapshot.vias:
            if route.net_id == via.net_id or route.layer not in via.layers:
                continue
            required = route.width_um // 2 + via.diameter_um // 2 + _net_clearance(rulepack, snapshot, route.net_id) + _net_clearance(rulepack, snapshot, via.net_id)
            if _point_segment_within(via.position, route.start, route.end, required):
                yield _finding("PCB_CLEARANCE_VIA", route.id, f"route clearance violated by {via.id}")


def _via_pad_clearance_findings(snapshot: BoardSnapshot, rulepack: ManufacturingRulePack) -> Iterable[NormalizedFinding]:
    for via in snapshot.vias:
        for pad in snapshot.pads:
            if pad.net_id == via.net_id or not set(via.layers) & set(pad.layers):
                continue
            required = via.diameter_um // 2 + _net_clearance(rulepack, snapshot, via.net_id) + _net_clearance(rulepack, snapshot, pad.net_id)
            if _point_rect_within(via.position, _pad_bounds(pad), required):
                yield _finding("PCB_CLEARANCE_PAD", via.id, f"via clearance violated by {pad.id}")


def _via_via_clearance_findings(snapshot: BoardSnapshot, rulepack: ManufacturingRulePack) -> Iterable[NormalizedFinding]:
    for index, via in enumerate(snapshot.vias):
        for other in snapshot.vias[index + 1:]:
            if via.net_id == other.net_id or not set(via.layers) & set(other.layers):
                continue
            required = via.diameter_um // 2 + other.diameter_um // 2 + _net_clearance(rulepack, snapshot, via.net_id) + _net_clearance(rulepack, snapshot, other.net_id)
            if _point_distance_within(via.position, other.position, required):
                yield _finding("PCB_CLEARANCE_VIA", via.id, f"via clearance violated by {other.id}")


def _connectivity_findings(snapshot: BoardSnapshot, rulepack: ManufacturingRulePack) -> Iterable[NormalizedFinding]:
    for net in snapshot.nets:
        pads = tuple(item for item in snapshot.pads if item.net_id == net.id)
        routes = tuple(item for item in snapshot.routes if item.net_id == net.id)
        vias = tuple(item for item in snapshot.vias if item.net_id == net.id)
        if len(pads) < 2 or not routes and not vias:
            continue
        entities: tuple[object, ...] = pads + routes + vias
        parent = list(range(len(entities)))

        def root(item: int) -> int:
            while parent[item] != item:
                parent[item] = parent[parent[item]]
                item = parent[item]
            return item

        def union(left: int, right: int) -> None:
            left, right = root(left), root(right)
            if left != right:
                parent[right] = left

        for left_index, left in enumerate(entities):
            for right_index, right in enumerate(entities[left_index + 1:], left_index + 1):
                if _conductive_contact(left, right, rulepack, snapshot):
                    union(left_index, right_index)
        touched = []
        for index, pad in enumerate(pads):
            if any(_pad_touches(pad, item, rulepack, snapshot) for item in routes + vias):
                touched.append(index)
        if len(touched) >= 2 and len({root(index) for index in touched}) > 1:
            yield _finding("PCB_ROUTE_DISCONNECTED", net.id, "multi-terminal route geometry is disconnected")


def _zone_keepout_findings(
    zone: CopperZone, keepouts: tuple[Keepout, ...]
) -> Iterable[NormalizedFinding]:
    for keepout in keepouts:
        if (
            ("copper_zone" in keepout.prohibited or keepout.kind in {"esp_antenna", "quiet_zone", "crystal_near_field", "sensitive_analog"})
            and zone.layer in keepout.layers
            and _rects_intersect(zone.bounds, keepout.bounds)
        ):
            yield _finding(
                "PCB_KEEPOUT_COPPER_ZONE",
                zone.id,
                f"copper zone intersects hard keepout {keepout.id}",
            )


def _zone_edge_findings(
    zone: CopperZone, snapshot: BoardSnapshot, rulepack: ManufacturingRulePack
) -> Iterable[NormalizedFinding]:
    edge = rulepack.copper.edge_clearance_um
    min_x, min_y = min(item.x for item in snapshot.outline), min(item.y for item in snapshot.outline)
    max_x, max_y = max(item.x for item in snapshot.outline), max(item.y for item in snapshot.outline)
    if (
        zone.bounds.x < min_x + edge
        or zone.bounds.y < min_y + edge
        or zone.bounds.x + zone.bounds.width > max_x - edge
        or zone.bounds.y + zone.bounds.height > max_y - edge
    ):
        yield _finding(
            "PCB.COPPER.EDGE",
            zone.id,
            f"copper zone violates board edge clearance of {edge} um",
        )


def _via_edge_findings(
    via: Via, snapshot: BoardSnapshot, rulepack: ManufacturingRulePack
) -> Iterable[NormalizedFinding]:
    if via.net_id != BoardObjectId("GND"):
        return
    edge = rulepack.copper.edge_clearance_um + via.diameter_um // 2
    min_x, min_y = min(item.x for item in snapshot.outline), min(item.y for item in snapshot.outline)
    max_x, max_y = max(item.x for item in snapshot.outline), max(item.y for item in snapshot.outline)
    if (
        via.position.x < min_x + edge
        or via.position.y < min_y + edge
        or via.position.x > max_x - edge
        or via.position.y > max_y - edge
    ):
        yield _finding("PCB.COPPER.EDGE", via.id, "ground stitching via violates board edge clearance")


def _zone_clearance_findings(
    zone: CopperZone, snapshot: BoardSnapshot, rulepack: ManufacturingRulePack
) -> Iterable[NormalizedFinding]:
    for pad in snapshot.pads:
        if pad.net_id == zone.net_id or zone.layer not in pad.layers:
            continue
        required = zone.clearance_um + _net_clearance(rulepack, snapshot, pad.net_id)
        if _rects_within(zone.bounds, _pad_bounds(pad), required):
            yield _finding("PCB.COPPER.CLEARANCE", zone.id, f"copper zone violates pad clearance at {pad.id}")
    for route in snapshot.routes:
        if route.net_id == zone.net_id or route.layer != zone.layer:
            continue
        required = zone.clearance_um + _net_clearance(rulepack, snapshot, route.net_id) + route.width_um // 2
        if _segment_rect_within(route.start, route.end, zone.bounds, required):
            yield _finding("PCB.COPPER.CLEARANCE", zone.id, f"copper zone violates route clearance at {route.id}")
    for via in snapshot.vias:
        if via.net_id == zone.net_id or zone.layer not in via.layers:
            continue
        required = zone.clearance_um + _net_clearance(rulepack, snapshot, via.net_id) + via.diameter_um // 2
        if _point_rect_within(via.position, zone.bounds, required):
            yield _finding("PCB.COPPER.CLEARANCE", zone.id, f"copper zone violates via clearance at {via.id}")
    for other in snapshot.copper_zones:
        if other.id == zone.id or other.net_id == zone.net_id or other.layer != zone.layer:
            continue
        required = zone.clearance_um + other.clearance_um
        if _rects_within(zone.bounds, other.bounds, required):
            yield _finding("PCB.COPPER.CLEARANCE", zone.id, f"copper zone violates zone clearance at {other.id}")


def _copper_connectivity_findings(
    snapshot: BoardSnapshot, rulepack: ManufacturingRulePack
) -> Iterable[NormalizedFinding]:
    zones = tuple(item for item in snapshot.copper_zones if item.net_id == BoardObjectId("GND"))
    if not zones:
        return
    entities: tuple[object, ...] = tuple(item for item in snapshot.pads if item.net_id == BoardObjectId("GND")) + tuple(
        item for item in snapshot.routes if item.net_id == BoardObjectId("GND")
    ) + tuple(item for item in snapshot.vias if item.net_id == BoardObjectId("GND")) + zones
    parent = list(range(len(entities)))

    def root(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left, right = root(left), root(right)
        if left != right:
            parent[right] = left

    for left_index, left in enumerate(entities):
        for right_index, right in enumerate(entities[left_index + 1:], left_index + 1):
            if _conductive_contact(left, right, rulepack, snapshot):
                union(left_index, right_index)
    anchor_roots = {
        root(index)
        for index, entity in enumerate(entities)
        if not isinstance(entity, CopperZone)
    }
    for index, zone in enumerate(zones):
        if root(len(entities) - len(zones) + index) not in anchor_roots:
            yield _finding(
                "PCB.COPPER.CONNECTIVITY",
                zone.id,
                "GND copper zone is not connected to a conductive GND anchor",
            )


def _footprint_keepout_findings(
    footprint: Footprint, keepouts: tuple[Keepout, ...]
) -> Iterable[NormalizedFinding]:
    bounds = RectUm(
        footprint.position.x - footprint.width_um // 2,
        footprint.position.y - footprint.height_um // 2,
        footprint.width_um,
        footprint.height_um,
    )
    allowed = set(footprint.keepout_ids)
    for keepout in keepouts:
        if (
            "footprint" in keepout.prohibited
            and footprint.layer in keepout.layers
            and keepout.id not in allowed
            and _rects_intersect(bounds, keepout.bounds)
        ):
            yield _finding(
                "PCB_KEEPOUT_FOOTPRINT",
                footprint.id,
                f"footprint intersects hard keepout {keepout.id}",
            )


def _finding(
    rule_id: str, subject: str | BoardObjectId, message: str
) -> NormalizedFinding:
    return NormalizedFinding(
        rule_id=rule_id,
        severity="error",
        subject=str(subject),
        message=message,
    )


def _rects_intersect(left: RectUm, right: RectUm) -> bool:
    return not (
        left.x + left.width < right.x
        or right.x + right.width < left.x
        or left.y + left.height < right.y
        or right.y + right.height < left.y
    )


def _rects_within(left: RectUm, right: RectUm, distance: int) -> bool:
    return not (
        left.x + left.width + distance < right.x
        or right.x + right.width + distance < left.x
        or left.y + left.height + distance < right.y
        or right.y + right.height + distance < left.y
    )


def _net_clearance(rulepack: ManufacturingRulePack, snapshot: BoardSnapshot, net_id: BoardObjectId | None) -> int:
    return geometry.net_clearance(rulepack, snapshot, net_id)


def _pad_bounds(pad: Pad) -> RectUm:
    return geometry.pad_bounds(pad)


def _point_distance_within(point: PointUm, other: PointUm, distance: int) -> bool:
    return geometry.point_distance_within(point, other, distance)


def _point_segment_within(point: PointUm, start: PointUm, end: PointUm, distance: int) -> bool:
    return geometry.point_segment_within(point, start, end, distance)


def _segments_within(a: PointUm, b: PointUm, c: PointUm, d: PointUm, distance: int) -> bool:
    return geometry.segments_within(a, b, c, d, distance)


def _point_rect_within(point: PointUm, rect: RectUm, distance: int) -> bool:
    return geometry.point_rect_within(point, rect, distance)


def _segment_rect_within(start: PointUm, end: PointUm, rect: RectUm, distance: int = 0) -> bool:
    return geometry.segment_rect_within(start, end, rect, distance)


def _pad_touches(pad: Pad, entity: object, rulepack: ManufacturingRulePack, snapshot: BoardSnapshot) -> bool:
    if isinstance(entity, Pad):
        return False
    if isinstance(entity, RouteSegment):
        if entity.layer not in pad.layers:
            return False
        return _segment_rect_within(entity.start, entity.end, _pad_bounds(pad), entity.width_um // 2)
    if isinstance(entity, CopperZone):
        return entity.net_id == pad.net_id and entity.layer in pad.layers and _point_rect_within(pad.position, entity.bounds, max(pad.size_x_um, pad.size_y_um) // 2)
    if not set(entity.layers) & set(pad.layers):
        return False
    return _point_rect_within(entity.position, _pad_bounds(pad), entity.diameter_um // 2)


def _conductive_contact(left: object, right: object, rulepack: ManufacturingRulePack, snapshot: BoardSnapshot) -> bool:
    if isinstance(left, Pad):
        return _pad_touches(left, right, rulepack, snapshot)
    if isinstance(right, Pad):
        return _pad_touches(right, left, rulepack, snapshot)
    if isinstance(left, RouteSegment) and isinstance(right, RouteSegment):
        return left.layer == right.layer and _segments_within(left.start, left.end, right.start, right.end, left.width_um // 2 + right.width_um // 2)
    if isinstance(left, RouteSegment) and isinstance(right, Via):
        return left.layer in right.layers and _point_segment_within(right.position, left.start, left.end, left.width_um // 2 + right.diameter_um // 2)
    if isinstance(left, Via) and isinstance(right, RouteSegment):
        return right.layer in left.layers and _point_segment_within(left.position, right.start, right.end, left.diameter_um // 2 + right.width_um // 2)
    if isinstance(left, Via) and isinstance(right, Via):
        return bool(set(left.layers) & set(right.layers)) and _point_distance_within(left.position, right.position, left.diameter_um // 2 + right.diameter_um // 2)
    if isinstance(left, CopperZone) and isinstance(right, Pad):
        return right.net_id == left.net_id and left.layer in right.layers and _point_rect_within(right.position, left.bounds, max(right.size_x_um, right.size_y_um) // 2)
    if isinstance(left, Pad) and isinstance(right, CopperZone):
        return _conductive_contact(right, left, rulepack, snapshot)
    if isinstance(left, CopperZone) and isinstance(right, RouteSegment):
        return right.net_id == left.net_id and right.layer == left.layer and _segment_rect_within(right.start, right.end, left.bounds, right.width_um // 2)
    if isinstance(left, RouteSegment) and isinstance(right, CopperZone):
        return _conductive_contact(right, left, rulepack, snapshot)
    if isinstance(left, CopperZone) and isinstance(right, Via):
        return right.net_id == left.net_id and left.layer in right.layers and _point_rect_within(right.position, left.bounds, right.diameter_um // 2)
    if isinstance(left, Via) and isinstance(right, CopperZone):
        return _conductive_contact(right, left, rulepack, snapshot)
    if isinstance(left, CopperZone) and isinstance(right, CopperZone):
        return left.net_id == right.net_id and left.layer == right.layer and _rects_intersect(left.bounds, right.bounds)
    return False


def _segment_intersects_rect(start: PointUm, end: PointUm, rect: RectUm) -> bool:
    return geometry.segment_intersects_rect(start, end, rect)


def _segments_intersect(a: PointUm, b: PointUm, c: PointUm, d: PointUm) -> bool:
    return geometry.segments_intersect(a, b, c, d)


__all__ = [
    "BoardRuleChecker",
    "BoardWriteRejectedError",
    "validate_proposed_snapshot",
]

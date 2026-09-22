from __future__ import annotations

import hashlib
import heapq
from dataclasses import dataclass, replace

from pcbflow.canonical import canonical_json_bytes
from pcbflow.domain import NormalizedFinding

from .ir import BoardObjectId, BoardSnapshot, Net, PointUm, RectUm, RouteSegment, Via
from .operations import RouteNets
from .rulepack import ManufacturingRulePack
from .validation import BoardRuleChecker


_OBJECTIVE_VERSION = "routing-v1"


@dataclass(frozen=True, slots=True)
class RoutePathEvidence:
    net_id: BoardObjectId
    points: tuple[PointUm, ...]
    layers: tuple[str, ...]
    score: int
    via_count: int


@dataclass(frozen=True, slots=True)
class RoutingRoundEvidence:
    round_index: int
    requested_net_ids: tuple[BoardObjectId, ...]
    score: int
    ripped_up_route_ids: tuple[BoardObjectId, ...]


@dataclass(frozen=True, slots=True)
class RoutingEvidence:
    objective_version: str
    input_digest: str
    snapshot_digest: str
    rulepack_digest: str
    seed: int
    selected_paths: tuple[RoutePathEvidence, ...]
    ripped_up_route_ids: tuple[BoardObjectId, ...]
    rounds: tuple[RoutingRoundEvidence, ...]
    actual_rounds: int
    final_unconnected_net_ids: tuple[BoardObjectId, ...]


@dataclass(frozen=True, slots=True)
class RoutingResult:
    operations: tuple[RouteNets, ...]
    segments: tuple[RouteSegment, ...]
    vias: tuple[Via, ...]
    findings: tuple[NormalizedFinding, ...]
    evidence: RoutingEvidence
    locked_route_ids: frozenset[BoardObjectId]


_Solution = tuple[list[RouteSegment], list[Via], list[tuple[PointUm, str]], int]


class Autorouter:
    """A deliberately narrow deterministic two-layer BoardIR router."""

    def route(
        self,
        snapshot: BoardSnapshot,
        rulepack: ManufacturingRulePack,
        net_ids: tuple[str | BoardObjectId, ...],
        *,
        seed: int,
    ) -> RoutingResult:
        if type(seed) is not int or seed < 0:
            raise ValueError("routing seed must be a non-negative integer")
        if type(net_ids) is not tuple:
            raise TypeError("route net ids must be a tuple")
        requested = tuple(sorted({BoardObjectId(str(item)) for item in net_ids}, key=str))
        snapshot_digest = snapshot.canonical_digest()
        rulepack_digest = rulepack.canonical_digest()
        locked = frozenset(item.id for item in snapshot.routes if item.route_lock)
        original_unlocked_route_ids = frozenset(
            item.id for item in snapshot.routes if not item.route_lock
        )
        findings: list[NormalizedFinding] = []
        paths: list[RoutePathEvidence] = []
        removed_original: set[BoardObjectId] = set()
        evidence_removed: set[BoardObjectId] = set()
        unconnected: set[BoardObjectId] = set()
        nets = {item.id: item for item in snapshot.nets}
        congestion: dict[tuple[int, int, str], int] = {}
        routable: dict[BoardObjectId, tuple[Net, tuple[object, ...]]] = {}
        routing_snapshot = snapshot
        solutions: dict[BoardObjectId, _Solution] = {}

        def score_now() -> int:
            return sum(item[3] for item in solutions.values())

        def install(net_id: BoardObjectId, value: _Solution) -> None:
            nonlocal routing_snapshot
            old = solutions.pop(net_id, None)
            old_route_ids: set[BoardObjectId] = set()
            old_via_ids: set[BoardObjectId] = set()
            if old is not None:
                old_route_ids = {item.id for item in old[0]}
                old_via_ids = {item.id for item in old[1]}
                routing_snapshot = replace(
                    routing_snapshot,
                    routes=tuple(item for item in routing_snapshot.routes if item.id not in old_route_ids),
                    vias=tuple(item for item in routing_snapshot.vias if item.id not in old_via_ids),
                )
            own_existing = tuple(
                item for item in routing_snapshot.routes
                if item.net_id == net_id and not item.route_lock
            )
            removed_baseline_ids = {
                item.id for item in own_existing if item.id in original_unlocked_route_ids
            }
            removed_original.update(removed_baseline_ids)
            evidence_removed.update(removed_baseline_ids)
            routing_snapshot = replace(
                routing_snapshot,
                routes=tuple(item for item in routing_snapshot.routes if item.id not in {item.id for item in own_existing}) + tuple(value[0]),
                vias=routing_snapshot.vias + tuple(value[1]),
            )
            solutions[net_id] = value
            _record_congestion(congestion, value[2])

        # Negotiation is intentionally bounded.  The initial round replaces only
        # existing unlocked geometry on a selected requested net; no other net is
        # silently brought into scope.
        for net_id in requested:
            net = nets.get(net_id)
            if net is None:
                findings.append(_finding("PCB_ROUTE_UNKNOWN_NET", net_id, f"unknown route request {net_id}"))
                unconnected.add(net_id)
                continue
            net_class = str(net.net_class)
            pads = tuple(sorted((item for item in snapshot.pads if item.net_id == net_id), key=lambda item: str(item.id)))
            if net_class in {"logic_power", "load_power"}:
                findings.append(_finding("PCB_POWER_ROUTE_REQUIRES_TOPOLOGY", net_id, "power nets require topology planning, not narrow signal routing"))
                unconnected.add(net_id)
                continue
            if net_class not in {"signal", "quiet_signal"}:
                findings.append(_finding("PCB_ROUTE_NET_CLASS_UNSUPPORTED", net_id, f"net class {net_class} is not a signal-router target"))
                unconnected.add(net_id)
                continue
            if len(pads) < 2:
                findings.append(_finding("PCB_ROUTE_ENDPOINTS_INSUFFICIENT", net_id, "routing requires at least two pads"))
                unconnected.add(net_id)
                continue
            if any(not _on_grid(item.position, rulepack.routing.grid_step_um) for item in pads):
                findings.append(_finding("PCB_ROUTE_ENDPOINT_OFF_GRID", net_id, "routing endpoints must lie on the routing grid"))
                unconnected.add(net_id)
                continue
            routable[net_id] = (net, pads)
            route_segments, route_vias, path, score = _route_net(routing_snapshot, rulepack, net, pads, congestion)
            if path is None:
                findings.append(_finding("PCB_ROUTE_UNROUTABLE", net_id, "no legal constrained route exists"))
                unconnected.add(net_id)
                continue
            paths.append(RoutePathEvidence(net_id, tuple(point for point, _ in path), tuple(layer for _, layer in path), score, len(route_vias)))
            install(net_id, (route_segments, route_vias, path, score))

        # A failed request is retried only after a single explicit, unlocked,
        # cross-net obstacle has demonstrably made a legal path available.  This
        # keeps negotiation bounded and avoids silently expanding the request set.
        rounds: list[RoutingRoundEvidence] = [
            RoutingRoundEvidence(1, requested, score_now(), tuple(sorted(evidence_removed, key=str)))
        ] if requested else []
        unresolved = [item for item in requested if item in unconnected and item in routable]
        for round_index in range(2, rulepack.routing.max_retry_rounds + 1):
            if not unresolved:
                break
            progress = False
            ripped_this_round: list[BoardObjectId] = []
            for net_id in tuple(sorted(unresolved, key=str)):
                net, pads = routable[net_id]
                candidate_groups: list[tuple[BoardObjectId | None, tuple[BoardObjectId, ...]]] = []
                for candidate_net, candidate in sorted(solutions.items(), key=lambda item: str(item[0])):
                    if candidate_net != net_id:
                        candidate_groups.append((candidate_net, tuple(item.id for item in candidate[0])))
                for obstacle in tuple(item for item in routing_snapshot.routes if not item.route_lock and item.net_id not in requested):
                    candidate_groups.append((None, (obstacle.id,)))
                candidate_groups.sort(key=lambda item: tuple(str(value) for value in item[1]))
                for candidate_net, obstacle_ids in candidate_groups:
                    obstacle_set = set(obstacle_ids)
                    trial = replace(
                        routing_snapshot,
                        routes=tuple(item for item in routing_snapshot.routes if item.id not in obstacle_set),
                        vias=tuple(item for item in routing_snapshot.vias if item.id not in obstacle_set),
                    )
                    route_segments, route_vias, path, score = _route_net(trial, rulepack, net, pads, congestion)
                    if path is None:
                        continue
                    paths.append(RoutePathEvidence(net_id, tuple(point for point, _ in path), tuple(layer for _, layer in path), score, len(route_vias)))
                    removed_baseline_ids = obstacle_set & original_unlocked_route_ids
                    evidence_removed.update(removed_baseline_ids)
                    removed_original.update(removed_baseline_ids)
                    ripped_this_round.extend(sorted(removed_baseline_ids, key=str))
                    if candidate_net is not None:
                        solutions.pop(candidate_net, None)
                        unresolved.append(candidate_net)
                    routing_snapshot = trial
                    own_existing_ids = tuple(
                        item.id for item in routing_snapshot.routes
                        if item.net_id == net_id
                        and not item.route_lock
                        and item.id in original_unlocked_route_ids
                    )
                    ripped_this_round.extend(sorted(own_existing_ids, key=str))
                    install(net_id, (route_segments, route_vias, path, score))
                    findings = [item for item in findings if not (item.rule_id == "PCB_ROUTE_UNROUTABLE" and item.subject == str(net_id))]
                    unconnected.discard(net_id)
                    if candidate_net is not None:
                        unconnected.add(candidate_net)
                        if not any(item.rule_id == "PCB_ROUTE_UNROUTABLE" and item.subject == str(candidate_net) for item in findings):
                            findings.append(_finding("PCB_ROUTE_UNROUTABLE", candidate_net, "no legal constrained route exists"))
                    unresolved.remove(net_id)
                    if candidate_net is not None:
                        candidate_info = routable[candidate_net]
                        reroute_segments, reroute_vias, reroute_path, reroute_score = _route_net(
                            routing_snapshot, rulepack, candidate_info[0], candidate_info[1], congestion
                        )
                        if reroute_path is not None:
                            paths.append(RoutePathEvidence(
                                candidate_net,
                                tuple(point for point, _ in reroute_path),
                                tuple(layer for _, layer in reroute_path),
                                reroute_score,
                                len(reroute_vias),
                            ))
                            install(candidate_net, (reroute_segments, reroute_vias, reroute_path, reroute_score))
                            unconnected.discard(candidate_net)
                            unresolved.remove(candidate_net)
                            findings = [item for item in findings if not (item.rule_id == "PCB_ROUTE_UNROUTABLE" and item.subject == str(candidate_net))]
                    progress = True
                    break
            rounds.append(RoutingRoundEvidence(round_index, requested, score_now(), tuple(sorted(set(ripped_this_round), key=str))))
            if not progress:
                break

        segments = [item for net_id in sorted(solutions, key=str) for item in solutions[net_id][0]]
        vias = [item for net_id in sorted(solutions, key=str) for item in solutions[net_id][1]]
        removed = tuple(sorted(removed_original, key=str))         
        findings = sorted(findings, key=lambda item: (item.rule_id, item.subject, item.message, item.severity))
        actual_rounds = len(rounds)
        evidence = RoutingEvidence(
            _OBJECTIVE_VERSION, snapshot_digest, snapshot_digest, rulepack_digest, seed,
            tuple(paths), tuple(sorted(evidence_removed, key=str)), tuple(rounds), actual_rounds,
            tuple(sorted(unconnected, key=str)),
        )
        if not segments and not vias:
            return RoutingResult((), (), (), tuple(findings), evidence, locked)
        operation = RouteNets(
            project_id=snapshot.profile_id,
            baseline_revision=snapshot_digest,
            risk="medium",
            rulepack_digest=rulepack_digest,
            target_object_ids=tuple(sorted({*requested, *removed}, key=str)),
            idempotency_key=_operation_key(snapshot_digest, rulepack_digest, seed, requested, paths, list(removed)),
            expected_snapshot_digest=snapshot_digest,
            net_ids=tuple(sorted({item.net_id for item in segments + vias}, key=str)),
            segments=tuple(segments), vias=tuple(vias), removed_route_ids=removed,
        )
        proposed = replace(
            snapshot,
            routes=tuple(item for item in snapshot.routes if item.id not in set(removed)) + tuple(segments),
            vias=snapshot.vias + tuple(vias),
        )
        geometry_findings = BoardRuleChecker().check(proposed, rulepack)
        if geometry_findings:
            all_generated_nets = {item.net_id for item in segments + vias}
            failed = tuple(sorted(
                list(findings) + [_finding("PCB_ROUTE_UNROUTABLE", item, "proposed route violates BoardIR rules") for item in sorted(all_generated_nets, key=str)],
                key=lambda item: (item.rule_id, item.subject, item.message, item.severity),
            ))
            rejected_evidence = replace(
                evidence,
                selected_paths=(),
                ripped_up_route_ids=(),
                rounds=tuple(replace(item, score=0, ripped_up_route_ids=()) for item in evidence.rounds),
                final_unconnected_net_ids=tuple(sorted({*evidence.final_unconnected_net_ids, *all_generated_nets}, key=str)),
            )
            return RoutingResult((), (), (), failed, rejected_evidence, locked)
        return RoutingResult((operation,), tuple(segments), tuple(vias), tuple(findings), evidence, locked)


def _route_net(snapshot: BoardSnapshot, rulepack: ManufacturingRulePack, net: Net, pads: tuple[object, ...], congestion: dict[tuple[int, int, str], int]) -> tuple[list[RouteSegment], list[Via], list[tuple[PointUm, str]] | None, int]:
    # Connect each deterministic pad pair.  This creates one connected tree for
    # the V1 star-free fixture without inventing a whole-board net expansion.
    paths: list[list[tuple[PointUm, str]]] = []
    score = 0
    remaining_vias = rulepack.routing.max_vias_per_net
    for start, target in zip(pads, pads[1:]):
        path, path_score = _astar(snapshot, rulepack, net, start, target, congestion, remaining_vias)  # type: ignore[arg-type]
        if path is None:
            return [], [], None, 0
        remaining_vias -= sum(1 for left, right in zip(path, path[1:]) if left[1] != right[1])
        paths.append(path)
        score += path_score
    board_class = snapshot.net_class(net.net_class)
    base = f"route-{str(net.id).lower()}"
    used = {str(item.id) for group in (snapshot.routes, snapshot.vias) for item in group}
    segments: list[RouteSegment] = []
    vias: list[Via] = []
    segment_index = 1
    via_index = 1
    for path in paths:
        run_start = path[0]
        previous = path[0]
        direction: tuple[int, int] | None = None
        for current in path[1:]:
            current_direction = (current[0].x - previous[0].x, current[0].y - previous[0].y)
            if current[1] != previous[1] or (direction is not None and current_direction != direction):
                if run_start[0] != previous[0]:
                    identifier = _fresh_id(f"{base}-{segment_index}", used)
                    segment_index += 1
                    segments.append(RouteSegment(identifier, net.id, run_start[0], previous[0], board_class.min_width_um, previous[1], False))
                if current[1] != previous[1]:
                    identifier = _fresh_id(f"via-{str(net.id).lower()}-{via_index}", used)
                    via_index += 1
                    vias.append(Via(identifier, net.id, previous[0], board_class.min_via_diameter_um, board_class.min_via_hole_um, tuple(snapshot.layers), False))
                    run_start = current
                    direction = None
                else:
                    run_start = previous
                    direction = current_direction
            else:
                direction = current_direction
            previous = current
        if run_start[0] != previous[0]:
            identifier = _fresh_id(f"{base}-{segment_index}", used)
            segment_index += 1
            segments.append(RouteSegment(identifier, net.id, run_start[0], previous[0], board_class.min_width_um, previous[1], False))
    flattened = [item for path in paths for item in path]
    return segments, vias, flattened, score


def _astar(snapshot: BoardSnapshot, rulepack: ManufacturingRulePack, net: Net, start: object, target: object, congestion: dict[tuple[int, int, str], int], max_vias: int) -> tuple[list[tuple[PointUm, str]] | None, int]:
    step = rulepack.routing.grid_step_um
    start_point, target_point = start.position, target.position  # type: ignore[attr-defined]
    starts = tuple((start_point, layer) for layer in snapshot.layers if layer in start.layers)  # type: ignore[attr-defined]
    goals = {(target_point.x, target_point.y, layer) for layer in snapshot.layers if layer in target.layers}  # type: ignore[attr-defined]
    if not starts or not goals:
        return None, 0
    endpoint_footprints = {str(start.footprint_id), str(target.footprint_id)}  # type: ignore[attr-defined]
    queue: list[tuple[int, int, int, int, int, str]] = []
    previous: dict[tuple[int, int, str, int], tuple[int, int, str, int] | None] = {}
    best: dict[tuple[int, int, str, int], int] = {}
    for point, layer in starts:
        key = (point.x, point.y, layer, 0)
        previous[key] = None
        best[key] = 0
        heapq.heappush(queue, (_heuristic(point, target_point), 0, 0, point.y, point.x, layer))
    directions = tuple((dx, dy) for dy in (-1, 0, 1) for dx in (-1, 0, 1) if dx or dy)
    winner: tuple[int, int, str, int] | None = None
    while queue:
        _, cost, via_count, y, x, layer = heapq.heappop(queue)
        key = (x, y, layer, via_count)
        if best.get(key) != cost:
            continue
        if (x, y, layer) in goals:
            winner = key
            break
        for dx, dy in directions:
            point = PointUm(x + dx * step, y + dy * step)
            if _blocked(snapshot, rulepack, net, point, layer, endpoint_footprints) or _edge_blocked(snapshot, rulepack, net, PointUm(x, y), point, layer, endpoint_footprints):
                continue
            move_cost = 1414 if dx and dy else 1000
            candidate = cost + move_cost + _penalty(snapshot, net, point, layer, step, congestion)
            next_key = (point.x, point.y, layer, via_count)
            if candidate < best.get(next_key, 10**18):
                best[next_key] = candidate
                previous[next_key] = key
                heapq.heappush(queue, (candidate + _heuristic(point, target_point), candidate, via_count, point.y, point.x, layer))
        if via_count < max_vias:
            for next_layer in snapshot.layers:
                if next_layer == layer or any(
                    _blocked(
                        snapshot, rulepack, net, PointUm(x, y), via_layer, endpoint_footprints,
                        rulepack.net_class(net.net_class).min_via_diameter_um // 2,
                    )
                    for via_layer in snapshot.layers
                ):
                    continue
                next_key = (x, y, next_layer, via_count + 1)
                candidate = cost + 25_000
                if candidate < best.get(next_key, 10**18):
                    best[next_key] = candidate
                    previous[next_key] = key
                    heapq.heappush(queue, (candidate + _heuristic(PointUm(x, y), target_point), candidate, via_count + 1, y, x, next_layer))
    if winner is None:
        return None, 0
    values: list[tuple[PointUm, str]] = []
    current: tuple[int, int, str, int] | None = winner
    while current is not None:
        values.append((PointUm(current[0], current[1]), current[2]))
        current = previous[current]
    values.reverse()
    return values, best[winner]


def _blocked(
    snapshot: BoardSnapshot,
    rulepack: ManufacturingRulePack,
    net: Net,
    point: PointUm,
    layer: str,
    endpoint_footprints: set[str],
    copper_radius_um: int | None = None,
) -> bool:
    min_x, min_y = min(item.x for item in snapshot.outline), min(item.y for item in snapshot.outline)
    max_x, max_y = max(item.x for item in snapshot.outline), max(item.y for item in snapshot.outline)
    edge = rulepack.routing.edge_clearance_um
    if point.x < min_x + edge or point.x > max_x - edge or point.y < min_y + edge or point.y > max_y - edge:
        return True
    board_class = rulepack.net_class(net.net_class)
    net_class = str(net.net_class)
    radius = board_class.min_width_um // 2 if copper_radius_um is None else copper_radius_um
    for keepout in snapshot.keepouts:
        if layer not in keepout.layers:
            continue
        prohibited = set(keepout.prohibited)
        applies = "route" in prohibited or "unrelated_route" in prohibited or (net_class == "quiet_signal" and "quiet_signal_route" in prohibited)
        if applies and _point_rect_within(point, keepout.bounds, radius):
            return True
    for footprint in snapshot.footprints:
        if footprint.layer == layer and str(footprint.id) not in endpoint_footprints:
            bounds = RectUm(footprint.position.x - footprint.width_um // 2, footprint.position.y - footprint.height_um // 2, footprint.width_um, footprint.height_um)
            if _point_rect_within(point, bounds, radius):
                return True
    clearance = board_class.clearance_um
    for pad in snapshot.pads:
        if pad.net_id == net.id or layer not in pad.layers:
            continue
        other_clearance = _net_clearance(rulepack, snapshot, pad.net_id)
        if _point_rect_within(point, _pad_bounds(pad), radius + clearance + other_clearance):
            return True
    for route in snapshot.routes:
        if route.net_id != net.id and route.layer == layer and _point_segment_within(
            point, route.start, route.end, radius + route.width_um // 2 + clearance + _net_clearance(rulepack, snapshot, route.net_id)
        ):
            return True
    for via in snapshot.vias:
        if via.net_id != net.id and layer in via.layers and _point_distance_within(
            point, via.position, radius + via.diameter_um // 2 + clearance + _net_clearance(rulepack, snapshot, via.net_id)
        ):
            return True
    for zone in snapshot.copper_zones:
        if zone.net_id != net.id and zone.route_lock and zone.layer == layer and _point_rect_within(
            point, zone.bounds, radius + clearance + max(zone.clearance_um, _net_clearance(rulepack, snapshot, zone.net_id))
        ):
            return True
    return False


def _edge_blocked(
    snapshot: BoardSnapshot,
    rulepack: ManufacturingRulePack,
    net: Net,
    start: PointUm,
    end: PointUm,
    layer: str,
    endpoint_footprints: set[str],
) -> bool:
    board_class = rulepack.net_class(net.net_class)
    clearance = board_class.clearance_um
    radius = board_class.min_width_um // 2
    for keepout in snapshot.keepouts:
        prohibited = set(keepout.prohibited)
        applies = "route" in prohibited or "unrelated_route" in prohibited or (str(net.net_class) == "quiet_signal" and "quiet_signal_route" in prohibited)
        if layer in keepout.layers and applies and _segment_rect_within(start, end, keepout.bounds, radius):
            return True
    for footprint in snapshot.footprints:
        if footprint.layer == layer and str(footprint.id) not in endpoint_footprints:
            bounds = RectUm(footprint.position.x - footprint.width_um // 2, footprint.position.y - footprint.height_um // 2, footprint.width_um, footprint.height_um)
            if _segment_rect_within(start, end, bounds, radius):
                return True
    for pad in snapshot.pads:
        if pad.net_id == net.id or layer not in pad.layers:
            continue
        other_clearance = _net_clearance(rulepack, snapshot, pad.net_id)
        if _segment_rect_within(start, end, _pad_bounds(pad), radius + clearance + other_clearance):
            return True
    for route in snapshot.routes:
        if route.net_id != net.id and route.layer == layer and _segments_within(
            start, end, route.start, route.end, radius + route.width_um // 2 + clearance + _net_clearance(rulepack, snapshot, route.net_id)
        ):
            return True
    for via in snapshot.vias:
        if via.net_id != net.id and layer in via.layers and _point_segment_within(
            via.position, start, end, radius + via.diameter_um // 2 + clearance + _net_clearance(rulepack, snapshot, via.net_id)
        ):
            return True
    for zone in snapshot.copper_zones:
        if zone.net_id != net.id and zone.route_lock and zone.layer == layer and _segment_rect_within(
            start, end, zone.bounds, radius + clearance + max(zone.clearance_um, _net_clearance(rulepack, snapshot, zone.net_id))
        ):
            return True
    return False


def _penalty(snapshot: BoardSnapshot, net: Net, point: PointUm, layer: str, step: int, congestion: dict[tuple[int, int, str], int]) -> int:
    # Keep this score explicit: negotiation evidence must never hide these costs.
    quiet = sum(2_000 for item in snapshot.keepouts if item.kind == "quiet_zone" and layer in item.layers and item.bounds.contains(point))
    proximity = sum(200 for item in snapshot.keepouts if layer in item.layers and _expanded(item.bounds, step).contains(point))
    gnd_disruption = sum(500 for item in snapshot.copper_zones if item.net_id == BoardObjectId("GND") and item.route_lock and item.layer == layer and item.bounds.contains(point))
    historical_congestion = 1_000 * congestion.get((point.x, point.y, layer), 0)
    return quiet + proximity + gnd_disruption + historical_congestion


def _record_congestion(congestion: dict[tuple[int, int, str], int], path: list[tuple[PointUm, str]]) -> None:
    for point, layer in path:
        key = (point.x, point.y, layer)
        congestion[key] = congestion.get(key, 0) + 1


def _on_grid(point: PointUm, step: int) -> bool:
    return point.x % step == 0 and point.y % step == 0


def _heuristic(point: PointUm, target: PointUm) -> int:
    return 1000 * max(abs(point.x - target.x), abs(point.y - target.y)) // 1000


def _expanded(rect: RectUm, amount: int) -> RectUm:
    return RectUm(rect.x - amount, rect.y - amount, rect.width + 2 * amount, rect.height + 2 * amount)


def _point_near_segment(point: PointUm, start: PointUm, end: PointUm, clearance: int) -> bool:
    return _point_segment_within(point, start, end, clearance)


def _pad_bounds(pad: object) -> RectUm:
    return RectUm(pad.position.x - pad.size_x_um // 2, pad.position.y - pad.size_y_um // 2, pad.size_x_um, pad.size_y_um)  # type: ignore[attr-defined]


def _segment_bounds(route: RouteSegment) -> RectUm:
    return RectUm(min(route.start.x, route.end.x), min(route.start.y, route.end.y), max(1, abs(route.end.x - route.start.x)), max(1, abs(route.end.y - route.start.y)))


def _net_clearance(rulepack: ManufacturingRulePack, snapshot: BoardSnapshot, net_id: BoardObjectId | None) -> int:
    if net_id is None:
        return 0
    try:
        return rulepack.net_class(snapshot.net(net_id).net_class).clearance_um
    except KeyError:
        return 0


def _point_distance_within(point: PointUm, other: PointUm, distance: int) -> bool:
    dx, dy = point.x - other.x, point.y - other.y
    return dx * dx + dy * dy <= distance * distance


def _point_segment_within(point: PointUm, start: PointUm, end: PointUm, distance: int) -> bool:
    dx, dy = end.x - start.x, end.y - start.y
    if point.x < min(start.x, end.x) - distance or point.x > max(start.x, end.x) + distance or point.y < min(start.y, end.y) - distance or point.y > max(start.y, end.y) + distance:
        return False
    px, py = point.x - start.x, point.y - start.y
    length_sq = dx * dx + dy * dy
    if length_sq == 0:
        return _point_distance_within(point, start, distance)
    projection = px * dx + py * dy
    if projection <= 0:
        return _point_distance_within(point, start, distance)
    if projection >= length_sq:
        return _point_distance_within(point, end, distance)
    cross = px * dy - py * dx
    return cross * cross <= distance * distance * length_sq


def _segments_within(a: PointUm, b: PointUm, c: PointUm, d: PointUm, distance: int) -> bool:
    if max(min(a.x, b.x), min(c.x, d.x)) > min(max(a.x, b.x), max(c.x, d.x)) + distance:
        return False
    if max(min(a.y, b.y), min(c.y, d.y)) > min(max(a.y, b.y), max(c.y, d.y)) + distance:
        return False
    if _segments_intersect(a, b, c, d):
        return True
    return (
        _point_segment_within(a, c, d, distance)
        or _point_segment_within(b, c, d, distance)
        or _point_segment_within(c, a, b, distance)
        or _point_segment_within(d, a, b, distance)
    )


def _point_rect_within(point: PointUm, rect: RectUm, distance: int) -> bool:
    dx = max(rect.x - point.x, 0, point.x - (rect.x + rect.width))
    dy = max(rect.y - point.y, 0, point.y - (rect.y + rect.height))
    return dx * dx + dy * dy <= distance * distance


def _segment_rect_within(start: PointUm, end: PointUm, rect: RectUm, distance: int = 0) -> bool:
    if max(rect.x - distance, min(start.x, end.x)) > min(rect.x + rect.width + distance, max(start.x, end.x)):
        return False
    if max(rect.y - distance, min(start.y, end.y)) > min(rect.y + rect.height + distance, max(start.y, end.y)):
        return False
    if _segment_intersects_rect(start, end, rect):
        return True
    corners = (
        PointUm(rect.x, rect.y),
        PointUm(rect.x + rect.width, rect.y),
        PointUm(rect.x + rect.width, rect.y + rect.height),
        PointUm(rect.x, rect.y + rect.height),
    )
    if any(_point_segment_within(corner, start, end, distance) for corner in corners):
        return True
    return any(
        _point_segment_within(start, left, right, distance)
        or _point_segment_within(end, left, right, distance)
        for left, right in zip(corners, corners[1:] + corners[:1], strict=True)
    )


def _segment_intersects_rect(start: PointUm, end: PointUm, rect: RectUm) -> bool:
    if rect.contains(start) or rect.contains(end):
        return True
    corners = (PointUm(rect.x, rect.y), PointUm(rect.x + rect.width, rect.y), PointUm(rect.x + rect.width, rect.y + rect.height), PointUm(rect.x, rect.y + rect.height))
    return any(_segments_intersect(start, end, left, right) for left, right in zip(corners, corners[1:] + corners[:1], strict=True))


def _segments_intersect(a: PointUm, b: PointUm, c: PointUm, d: PointUm) -> bool:
    def cross(p: PointUm, q: PointUm, r: PointUm) -> int:
        return (q.x - p.x) * (r.y - p.y) - (q.y - p.y) * (r.x - p.x)
    def between(p: PointUm, q: PointUm, r: PointUm) -> bool:
        return min(p.x, r.x) <= q.x <= max(p.x, r.x) and min(p.y, r.y) <= q.y <= max(p.y, r.y)
    first, second, third, fourth = cross(a, b, c), cross(a, b, d), cross(c, d, a), cross(c, d, b)
    return (first == 0 and between(a, c, b)) or (second == 0 and between(a, d, b)) or (third == 0 and between(c, a, d)) or (fourth == 0 and between(c, b, d)) or ((first > 0) != (second > 0) and (third > 0) != (fourth > 0))


def _fresh_id(value: str, used: set[str]) -> BoardObjectId:
    index, candidate = 1, value
    while candidate in used:
        index += 1
        candidate = f"{value}-{index}"
    used.add(candidate)
    return BoardObjectId(candidate)


def _operation_key(snapshot_digest: str, rulepack_digest: str, seed: int, requested: tuple[BoardObjectId, ...], paths: list[RoutePathEvidence], removed: list[BoardObjectId]) -> str:
    payload = {"objective_version": _OBJECTIVE_VERSION, "snapshot_digest": snapshot_digest, "rulepack_digest": rulepack_digest, "seed": seed, "requested": [str(item) for item in requested], "paths": [{"net": str(item.net_id), "score": item.score, "points": [(point.x, point.y, layer) for point, layer in zip(item.points, item.layers, strict=True)]} for item in paths], "removed": [str(item) for item in removed]}
    return "routing-" + hashlib.sha256(canonical_json_bytes(payload)).hexdigest()[:32]


def _finding(rule_id: str, subject: BoardObjectId, message: str) -> NormalizedFinding:
    return NormalizedFinding(rule_id, "error", str(subject), message)


__all__ = ["Autorouter", "RoutePathEvidence", "RoutingEvidence", "RoutingResult", "RoutingRoundEvidence"]

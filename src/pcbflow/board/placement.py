from __future__ import annotations

import hashlib
import math
import random
from collections.abc import Mapping
from dataclasses import dataclass

from ortools.sat.python import cp_model

from pcbflow.canonical import canonical_json_bytes
from pcbflow.domain import NormalizedFinding

from .ir import BoardObjectId, BoardSnapshot, Footprint, PointUm, RectUm
from .operations import FootprintPlacement, PlaceFootprints
from .rulepack import ManufacturingRulePack

_GRID_UM = 5_000
_OBJECTIVE_VERSION = "placement-v1"
_CAPABILITY_SUMMARY = "capability_unbound"


@dataclass(frozen=True, slots=True)
class DoubledRectUm:
    x: int
    y: int
    width: int
    height: int

    def intersects(self, other: DoubledRectUm | RectUm) -> bool:
        if isinstance(other, RectUm):
            other = _doubled_rect(other)
        if not isinstance(other, DoubledRectUm):
            raise TypeError("rectangle intersection requires DoubledRectUm or RectUm")
        return not (
            self.x + self.width < other.x
            or other.x + other.width < self.x
            or self.y + self.height < other.y
            or other.y + other.height < self.y
        )


@dataclass(frozen=True, slots=True)
class PlacementSeedEvidence:
    seed: int
    start_score: int
    refined_score: int
    start_placement_digest: str
    refined_placement_digest: str


@dataclass(frozen=True, slots=True)
class PlacementEvidence:
    objective_version: str
    snapshot_digest: str
    rulepack_digest: str
    capability_summary: str
    seed: int
    starts: int
    actual_starts: int
    starts_exhausted: bool
    iterations: int
    start_scores: tuple[int, ...]
    chosen_score: int
    placement_digest: str
    seed_runs: tuple[PlacementSeedEvidence, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "objective_version": self.objective_version,
            "snapshot_digest": self.snapshot_digest,
            "rulepack_digest": self.rulepack_digest,
            "capability_summary": self.capability_summary,
            "seed": self.seed,
            "starts": self.starts,
            "actual_starts": self.actual_starts,
            "starts_exhausted": self.starts_exhausted,
            "iterations": self.iterations,
            "start_scores": list(self.start_scores),
            "chosen_score": self.chosen_score,
            "placement_digest": self.placement_digest,
            "seed_runs": [
                {
                    "seed": item.seed,
                    "start_score": item.start_score,
                    "refined_score": item.refined_score,
                    "start_placement_digest": item.start_placement_digest,
                    "refined_placement_digest": item.refined_placement_digest,
                }
                for item in self.seed_runs
            ],
        }


@dataclass(frozen=True, slots=True)
class PlacementResult:
    operations: tuple[PlaceFootprints, ...]
    score: int
    evidence: PlacementEvidence
    findings: tuple[NormalizedFinding, ...]
    _footprints: tuple[Footprint, ...]

    def footprint_bounds(self, object_id: str | BoardObjectId) -> DoubledRectUm:
        identifier = str(object_id)
        placements = (
            self.operations[0].placements_by_id if self.operations else {}
        )
        placement = placements.get(identifier)
        footprint = next(
            (item for item in self._footprints if str(item.id) == identifier), None
        )
        if placement is None or footprint is None:
            raise KeyError(f"placement footprint not found: {identifier}")
        return _doubled_bounds(footprint, placement.position)

    @property
    def placements_by_id(self) -> Mapping[str, FootprintPlacement]:
        return self.operations[0].placements_by_id if self.operations else {}


class PlacementSolver:
    """Solve only hard-legal cells, then deterministically refine that solution."""

    def solve(
        self,
        snapshot: BoardSnapshot,
        rulepack: ManufacturingRulePack,
        *,
        seed: int,
        starts: int,
        iterations: int,
    ) -> PlacementResult:
        _validate_inputs(seed=seed, starts=starts, iterations=iterations)
        snapshot_digest = snapshot.canonical_digest()
        rulepack_digest = rulepack.canonical_digest()
        if snapshot.profile_id != rulepack.profile_id:
            return self._infeasible(
                snapshot,
                snapshot_digest,
                rulepack_digest,
                seed,
                starts,
                iterations,
                "PCB_PLACEMENT_RULEPACK_MISMATCH",
                snapshot.profile_id,
                "placement requires a rule pack for the BoardIR profile",
            )

        cells = {
            str(footprint.id): _legal_cells(snapshot, footprint)
            for footprint in snapshot.footprints
        }
        missing = sorted(key for key, values in cells.items() if not values)
        if missing:
            return self._infeasible(
                snapshot,
                snapshot_digest,
                rulepack_digest,
                seed,
                starts,
                iterations,
                "PCB_PLACEMENT_NO_LEGAL_REGION",
                missing[0],
                f"footprint {missing[0]} has no hard-legal placement cell",
            )

        initial = _solve_cp_sat(snapshot, cells, seed)
        if initial is None:
            return self._infeasible(
                snapshot,
                snapshot_digest,
                rulepack_digest,
                seed,
                starts,
                iterations,
                "PCB_PLACEMENT_INFEASIBLE",
                snapshot.profile_id,
                "hard placement constraints have no jointly feasible solution",
            )

        best, score, start_scores, runs = _refine(
            snapshot, cells, initial, seed=seed, starts=starts, iterations=iterations
        )
        placements = tuple(
            FootprintPlacement(footprint.id, best[str(footprint.id)], footprint.layer)
            for footprint in snapshot.footprints
        )
        digest = _placement_digest(best)
        idempotency_digest = hashlib.sha256(
            canonical_json_bytes(
                {
                    "objective_version": _OBJECTIVE_VERSION,
                    "snapshot_digest": snapshot_digest,
                    "rulepack_digest": rulepack_digest,
                    "seed": seed,
                    "starts": starts,
                    "iterations": iterations,
                    "placement_digest": digest,
                }
            )
        ).hexdigest()
        operation = PlaceFootprints(
            project_id=snapshot.profile_id,
            baseline_revision=snapshot_digest,
            risk="high",
            rulepack_digest=rulepack_digest,
            target_object_ids=tuple(item.id for item in snapshot.footprints),
            idempotency_key=f"placement-{idempotency_digest[:32]}",
            expected_snapshot_digest=snapshot_digest,
            placements=placements,
        )
        return PlacementResult(
            operations=(operation,),
            score=score,
            evidence=PlacementEvidence(
                objective_version=_OBJECTIVE_VERSION,
                snapshot_digest=snapshot_digest,
                rulepack_digest=rulepack_digest,
                capability_summary=_CAPABILITY_SUMMARY,
                seed=seed,
                starts=starts,
                actual_starts=len(runs),
                starts_exhausted=len(runs) < starts,
                iterations=iterations,
                start_scores=start_scores,
                chosen_score=score,
                placement_digest=digest,
                seed_runs=runs,
            ),
            findings=(),
            _footprints=snapshot.footprints,
        )

    @staticmethod
    def _infeasible(
        snapshot: BoardSnapshot,
        snapshot_digest: str,
        rulepack_digest: str,
        seed: int,
        starts: int,
        iterations: int,
        rule_id: str,
        subject: str,
        message: str,
    ) -> PlacementResult:
        finding = NormalizedFinding(rule_id, "error", subject, message)
        evidence = PlacementEvidence(
            objective_version=_OBJECTIVE_VERSION,
            snapshot_digest=snapshot_digest,
            rulepack_digest=rulepack_digest,
            capability_summary=_CAPABILITY_SUMMARY,
            seed=seed,
            starts=starts,
            actual_starts=0,
            starts_exhausted=False,
            iterations=iterations,
            start_scores=(),
            chosen_score=0,
            placement_digest=_placement_digest(()),
            seed_runs=(),
        )
        return PlacementResult((), 0, evidence, (finding,), snapshot.footprints)


def score_layout(
    snapshot: BoardSnapshot, placements: Mapping[str, FootprintPlacement | PointUm]
) -> int:
    return (
        100 * weighted_ratsnest_length(snapshot, placements)
        + 250 * critical_net_length(snapshot, placements)
        + 400 * power_loop_area(snapshot, placements)
        + 500 * quiet_zone_noise_penalty(snapshot, placements)
        + 150 * thermal_cluster_penalty(snapshot, placements)
        + 75 * connector_access_penalty(snapshot, placements)
    )


def weighted_ratsnest_length(
    snapshot: BoardSnapshot, placements: Mapping[str, FootprintPlacement | PointUm]
) -> int:
    net_points = _net_points(snapshot, placements)
    classes = {str(net.id): str(net.net_class) for net in snapshot.nets}
    weights = {"load_power": 4, "logic_power": 3, "quiet_signal": 2}
    return sum(
        weights.get(classes[net_id], 1)
        * _spanning_manhattan_length(points)
        for net_id, points in net_points.items()
    )


def critical_net_length(
    snapshot: BoardSnapshot, placements: Mapping[str, FootprintPlacement | PointUm]
) -> int:
    classes = {str(net.id): str(net.net_class) for net in snapshot.nets}
    return sum(
        _spanning_manhattan_length(points)
        for net_id, points in _net_points(snapshot, placements).items()
        if classes[net_id] in {"quiet_signal", "logic_power"}
    )


def power_loop_area(
    snapshot: BoardSnapshot, placements: Mapping[str, FootprintPlacement | PointUm]
) -> int:
    classes = {str(net.id): str(net.net_class) for net in snapshot.nets}
    power = [
        point
        for net_id, points in _net_points(snapshot, placements).items()
        if classes[net_id] == "load_power"
        for point in points
    ]
    if len(power) < 2:
        return 0
    return ((max(point.x for point in power) - min(point.x for point in power))
            * (max(point.y for point in power) - min(point.y for point in power))) // 1_000


def quiet_zone_noise_penalty(
    snapshot: BoardSnapshot, placements: Mapping[str, FootprintPlacement | PointUm]
) -> int:
    quiet_ids = _footprints_with_net_class(snapshot, "quiet_signal")
    power_zones = [item for item in snapshot.keepouts if item.kind == "power_zone"]
    footprints = {str(item.id): item for item in snapshot.footprints}
    return sum(
        1
        for identifier in quiet_ids
        if any(
            _doubled_bounds(footprints[identifier], _point(placements[identifier])).intersects(
                _doubled_rect(zone.bounds)
            )
            for zone in power_zones
        )
    )


def thermal_cluster_penalty(
    snapshot: BoardSnapshot, placements: Mapping[str, FootprintPlacement | PointUm]
) -> int:
    load_nets = {str(net.id) for net in snapshot.nets if str(net.net_class) == "load_power"}
    thermal = sorted({str(pad.footprint_id) for pad in snapshot.pads if pad.net_id and str(pad.net_id) in load_nets})
    threshold = min(snapshot.board_size_um) // 4
    points = {identifier: _point(placements[identifier]) for identifier in thermal}
    return sum(max(0, threshold - abs(points[left].x - points[right].x) - abs(points[left].y - points[right].y))
               for index, left in enumerate(thermal) for right in thermal[index + 1:])


def connector_access_penalty(
    snapshot: BoardSnapshot, placements: Mapping[str, FootprintPlacement | PointUm]
) -> int:
    min_x, min_y, max_x, max_y = _outline_bounds(snapshot)
    footprints = {str(item.id): item for item in snapshot.footprints}
    total = 0
    for identifier, footprint in footprints.items():
        if not identifier.startswith("J_"):
            continue
        bounds = _doubled_bounds(footprint, _point(placements[identifier]))
        total += min(bounds.x - 2 * min_x, bounds.y - 2 * min_y, 2 * max_x - (bounds.x + bounds.width), 2 * max_y - (bounds.y + bounds.height))
    return total


def _validate_inputs(*, seed: int, starts: int, iterations: int) -> None:
    if type(seed) is not int or seed < 0:
        raise ValueError("seed must be a non-negative integer")
    if type(starts) is not int or starts < 1:
        raise ValueError("starts must be a positive integer")
    if type(iterations) is not int or iterations < 0:
        raise ValueError("iterations must be a non-negative integer")


def _legal_cells(snapshot: BoardSnapshot, footprint: Footprint) -> tuple[PointUm, ...]:
    if footprint.placement_lock:
        return (footprint.position,) if _is_legal(snapshot, footprint, footprint.position) else ()
    min_x, min_y, max_x, max_y = _outline_bounds(snapshot)
    min_center_x = (2 * min_x + footprint.width_um + 1) // 2
    min_center_y = (2 * min_y + footprint.height_um + 1) // 2
    max_center_x = (2 * max_x - footprint.width_um) // 2
    max_center_y = (2 * max_y - footprint.height_um) // 2
    values = {footprint.position}
    for x in range(min_center_x, max_center_x + 1, _GRID_UM):
        for y in range(min_center_y, max_center_y + 1, _GRID_UM):
            values.add(PointUm(x, y))
    return tuple(sorted((item for item in values if _is_legal(snapshot, footprint, item)), key=lambda item: (item.x, item.y)))


def _is_legal(snapshot: BoardSnapshot, footprint: Footprint, position: PointUm) -> bool:
    bounds = _doubled_bounds(footprint, position)
    min_x, min_y, max_x, max_y = _outline_bounds(snapshot)
    if (
        bounds.x < 2 * min_x
        or bounds.y < 2 * min_y
        or bounds.x + bounds.width > 2 * max_x
        or bounds.y + bounds.height > 2 * max_y
    ):
        return False
    allowed = {str(item) for item in footprint.keepout_ids}
    for keepout in snapshot.keepouts:
        if footprint.layer not in keepout.layers:
            continue
        intersects = bounds.intersects(_doubled_rect(keepout.bounds))
        if "footprint" in keepout.prohibited and str(keepout.id) not in allowed and intersects:
            return False
        if keepout.kind in {"crystal_near_field", "power_zone"} and str(keepout.id) in allowed and not intersects:
            return False
    quiet_signal_footprint = str(footprint.id) in _footprints_with_net_class(
        snapshot, "quiet_signal"
    )
    if not quiet_signal_footprint:
        return True
    return not any(
        bounds.intersects(_doubled_rect(item.bounds))
        for item in snapshot.keepouts
        if item.kind == "power_zone"
    )
    return True


def _solve_cp_sat(
    snapshot: BoardSnapshot, cells: Mapping[str, tuple[PointUm, ...]], seed: int,
    excluded: set[tuple[tuple[str, int, int], ...]] | None = None,
) -> dict[str, PointUm] | None:
    model = cp_model.CpModel()
    choices: dict[str, cp_model.IntVar] = {}
    candidate_sets: dict[str, tuple[PointUm, ...]] = {}
    x_intervals: list[cp_model.IntervalVar] = []
    y_intervals: list[cp_model.IntervalVar] = []
    displacements: list[cp_model.IntVar] = []
    for footprint in snapshot.footprints:
        identifier = str(footprint.id)
        candidates = cells[identifier]
        choice = model.new_int_var(0, len(candidates) - 1, f"choice_{identifier}")
        lefts = [2 * item.x - footprint.width_um for item in candidates]
        tops = [2 * item.y - footprint.height_um for item in candidates]
        x = model.new_int_var(min(lefts), max(lefts), f"left_{identifier}")
        y = model.new_int_var(min(tops), max(tops), f"top_{identifier}")
        model.add_element(choice, lefts, x)
        model.add_element(choice, tops, y)
        end_x = model.new_int_var(
            min(lefts) + 2 * footprint.width_um + 1,
            max(lefts) + 2 * footprint.width_um + 1,
            f"end_x_{identifier}",
        )
        end_y = model.new_int_var(
            min(tops) + 2 * footprint.height_um + 1,
            max(tops) + 2 * footprint.height_um + 1,
            f"end_y_{identifier}",
        )
        model.add(end_x == x + 2 * footprint.width_um + 1)
        model.add(end_y == y + 2 * footprint.height_um + 1)
        x_intervals.append(model.new_interval_var(x, 2 * footprint.width_um + 1, end_x, f"ix_{identifier}"))
        y_intervals.append(model.new_interval_var(y, 2 * footprint.height_um + 1, end_y, f"iy_{identifier}"))
        if not footprint.placement_lock:
            original_left = 2 * footprint.position.x - footprint.width_um
            original_top = 2 * footprint.position.y - footprint.height_um
            dx = model.new_int_var(
                0,
                max(abs(left - original_left) for left in lefts),
                f"dx_{identifier}",
            )
            dy = model.new_int_var(
                0,
                max(abs(top - original_top) for top in tops),
                f"dy_{identifier}",
            )
            model.add_abs_equality(dx, x - original_left)
            model.add_abs_equality(dy, y - original_top)
            displacements.extend((dx, dy))
        choices[identifier] = choice
        candidate_sets[identifier] = candidates
    for key in sorted(excluded or ()):
        positions = {identifier: PointUm(x, y) for identifier, x, y in key}
        model.add_forbidden_assignments(
            list(choices.values()),
            [[candidate_sets[identifier].index(positions[identifier]) for identifier in choices]],
        )
    model.add_no_overlap_2d(x_intervals, y_intervals)
    if displacements:
        model.minimize(sum(displacements))
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = seed
    solver.parameters.randomize_search = False
    status = solver.Solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None
    return {
        identifier: candidate_sets[identifier][solver.Value(choices[identifier])]
        for identifier in candidate_sets
    }


def _refine(
    snapshot: BoardSnapshot,
    cells: Mapping[str, tuple[PointUm, ...]],
    initial: Mapping[str, PointUm],
    *,
    seed: int,
    starts: int,
    iterations: int,
) -> tuple[dict[str, PointUm], int, tuple[int, ...], tuple[PlacementSeedEvidence, ...]]:
    movable = [str(item.id) for item in snapshot.footprints if not item.placement_lock]
    best = dict(initial)
    best_score = score_layout(snapshot, best)
    start_scores: list[int] = []
    runs: list[PlacementSeedEvidence] = []
    used_starts: set[tuple[tuple[str, int, int], ...]] = set()
    for index in range(starts):
        run_seed = seed + index
        randomizer = random.Random(run_seed)
        current = dict(initial) if index == 0 else _solve_cp_sat(snapshot, cells, run_seed, used_starts)
        if current is None:
            break
        current_score = score_layout(snapshot, current)
        start_digest = _placement_digest(current)
        start_scores.append(current_score)
        used_starts.add(_placement_key(current))
        for step in range(iterations):
            if not movable:
                break
            identifier = randomizer.choice(movable)
            candidate = randomizer.choice(cells[identifier])
            if candidate == current[identifier]:
                continue
            proposed = dict(current)
            proposed[identifier] = candidate
            if _has_overlap(snapshot, proposed):
                continue
            proposed_score = score_layout(snapshot, proposed)
            temperature = max(1.0, 1000.0 * (1.0 - step / max(1, iterations)))
            if proposed_score <= current_score or randomizer.random() < math.exp((current_score - proposed_score) / temperature):
                current, current_score = proposed, proposed_score
        runs.append(
            PlacementSeedEvidence(
                run_seed,
                start_scores[-1],
                current_score,
                start_digest,
                _placement_digest(current),
            )
        )
        if (current_score, _placement_key(current)) < (best_score, _placement_key(best)):
            best, best_score = current, current_score
    return best, best_score, tuple(start_scores), tuple(runs)


def _has_overlap(snapshot: BoardSnapshot, placements: Mapping[str, PointUm]) -> bool:
    values = [
        _doubled_bounds(footprint, placements[str(footprint.id)])
        for footprint in snapshot.footprints
    ]
    return any(
        left.intersects(right)
        for index, left in enumerate(values)
        for right in values[index + 1 :]
    )


def _doubled_bounds(footprint: Footprint, position: PointUm) -> DoubledRectUm:
    return DoubledRectUm(
        2 * position.x - footprint.width_um,
        2 * position.y - footprint.height_um,
        2 * footprint.width_um,
        2 * footprint.height_um,
    )


def _doubled_rect(bounds: RectUm) -> DoubledRectUm:
    return DoubledRectUm(2 * bounds.x, 2 * bounds.y, 2 * bounds.width, 2 * bounds.height)


def _outline_bounds(snapshot: BoardSnapshot) -> tuple[int, int, int, int]:
    return (
        min(item.x for item in snapshot.outline),
        min(item.y for item in snapshot.outline),
        max(item.x for item in snapshot.outline),
        max(item.y for item in snapshot.outline),
    )


def _point(value: FootprintPlacement | PointUm) -> PointUm:
    return value.position if isinstance(value, FootprintPlacement) else value


def _net_points(
    snapshot: BoardSnapshot, placements: Mapping[str, FootprintPlacement | PointUm]
) -> dict[str, list[PointUm]]:
    footprints = {str(item.id): item for item in snapshot.footprints}
    result: dict[str, list[PointUm]] = {}
    for pad in snapshot.pads:
        if pad.net_id is None:
            continue
        footprint = footprints[str(pad.footprint_id)]
        position = _point(placements[str(footprint.id)])
        point = PointUm(
            pad.position.x + position.x - footprint.position.x,
            pad.position.y + position.y - footprint.position.y,
        )
        result.setdefault(str(pad.net_id), []).append(point)
    return result


def _footprints_with_net_class(snapshot: BoardSnapshot, net_class: str) -> set[str]:
    net_classes = {str(net.id): str(net.net_class) for net in snapshot.nets}
    return {
        str(pad.footprint_id)
        for pad in snapshot.pads
        if pad.net_id is not None and net_classes[str(pad.net_id)] == net_class
    }


def _spanning_manhattan_length(points: list[PointUm]) -> int:
    if len(points) < 2:
        return 0
    remaining = sorted(points, key=lambda item: (item.x, item.y))
    connected = [remaining.pop(0)]
    total = 0
    while remaining:
        distance, _, point = min(
            (
                abs(candidate.x - origin.x) + abs(candidate.y - origin.y),
                index,
                candidate,
            )
            for index, candidate in enumerate(remaining)
            for origin in connected
        )
        total += distance
        connected.append(point)
        remaining.remove(point)
    return total


def _placement_digest(placements: tuple[FootprintPlacement, ...] | Mapping[str, PointUm]) -> str:
    if isinstance(placements, Mapping):
        values = [
            {"id": identifier, "x": point.x, "y": point.y}
            for identifier, point in sorted(placements.items())
        ]
    else:
        values = [
            {"id": str(item.footprint_id), "x": item.position.x, "y": item.position.y, "layer": item.layer}
            for item in placements
        ]
    return "sha256:" + hashlib.sha256(canonical_json_bytes(values)).hexdigest()


def _placement_key(placements: Mapping[str, PointUm]) -> tuple[tuple[str, int, int], ...]:
    return tuple((identifier, point.x, point.y) for identifier, point in sorted(placements.items()))


__all__ = [
    "DoubledRectUm",
    "PlacementEvidence",
    "PlacementResult",
    "PlacementSeedEvidence",
    "PlacementSolver",
    "connector_access_penalty",
    "critical_net_length",
    "power_loop_area",
    "quiet_zone_noise_penalty",
    "score_layout",
    "thermal_cluster_penalty",
    "weighted_ratsnest_length",
]

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from typing import Iterable

from pcbflow.canonical import canonical_json_bytes
from pcbflow.domain import NormalizedFinding

from .ir import (
    BoardObjectId,
    BoardSnapshot,
    CopperZone,
    Keepout,
    Pad,
    PointUm,
    RectUm,
    RouteSegment,
    Via,
)
from .operations import AddGroundStitching, CreateCopperZones, ThermalPolicy
from .rulepack import CopperPolicy, ManufacturingRulePack
from .validation import BoardRuleChecker


_GND = BoardObjectId("GND")
_OBJECTIVE_VERSION = "copper-v1-rect-tile-1"
_SENSITIVE_KEEP_OUT_KINDS = frozenset(
    {"esp_antenna", "quiet_zone", "crystal_near_field", "sensitive_analog"}
)
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class ExcludedRegion:
    region_id: str
    layer: str
    bounds: RectUm
    reason: str

    def __post_init__(self) -> None:
        if type(self.region_id) is not str or not self.region_id or self.region_id != self.region_id.strip():
            raise ValueError("excluded region id must be canonical")
        if type(self.layer) is not str or not self.layer or self.layer != self.layer.strip():
            raise ValueError("excluded region layer must be canonical")
        if not isinstance(self.bounds, RectUm):
            raise TypeError("excluded region bounds must be RectUm")
        if type(self.reason) is not str or not self.reason or self.reason != self.reason.strip():
            raise ValueError("excluded region reason must be canonical")


@dataclass(frozen=True, slots=True)
class StitchingPolicy:
    net_id: BoardObjectId
    pitch_um: int
    max_vias: int
    edge_clearance_um: int
    via_diameter_um: int
    via_hole_diameter_um: int
    layers: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.net_id != _GND:
            raise ValueError("stitching policy must target GND")
        if type(self.pitch_um) is not int or self.pitch_um < 1:
            raise ValueError("stitching pitch must be positive")
        if type(self.max_vias) is not int or self.max_vias < 0:
            raise ValueError("maximum stitching vias must be non-negative")
        if type(self.edge_clearance_um) is not int or self.edge_clearance_um < 0:
            raise ValueError("stitching edge clearance must be non-negative")
        if type(self.via_diameter_um) is not int or self.via_diameter_um < 1:
            raise ValueError("stitching via diameter must be positive")
        if type(self.via_hole_diameter_um) is not int or self.via_hole_diameter_um < 1:
            raise ValueError("stitching via hole must be positive")
        if self.via_hole_diameter_um >= self.via_diameter_um:
            raise ValueError("stitching via hole must be smaller than diameter")
        _validate_string_tuple(self.layers, "stitching policy layers")


@dataclass(frozen=True, slots=True)
class CopperEvidence:
    objective_version: str
    snapshot_digest: str
    rulepack_digest: str
    representation: str
    excluded_regions: tuple[ExcludedRegion, ...]
    zone_ids: tuple[BoardObjectId, ...]
    zone_digests: tuple[str, ...]
    thermal_policy_pad_ids: tuple[BoardObjectId, ...]
    thermal_policy_digests: tuple[str, ...]
    island_ids: tuple[str, ...]
    stitching_via_ids: tuple[BoardObjectId, ...]
    via_coordinates: tuple[PointUm, ...]
    connectivity: tuple[tuple[str, bool], ...]
    post_fill_finding_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.objective_version) is not str or not self.objective_version:
            raise ValueError("copper evidence objective version must be canonical")
        for name, digest in (("snapshot", self.snapshot_digest), ("rulepack", self.rulepack_digest)):
            if type(digest) is not str or _DIGEST.fullmatch(digest) is None:
                raise ValueError(f"copper evidence {name} digest is invalid")
        if self.representation != "rectangular_tiles_v1":
            raise ValueError("unsupported copper evidence representation")
        if type(self.excluded_regions) is not tuple or any(not isinstance(item, ExcludedRegion) for item in self.excluded_regions):
            raise TypeError("copper evidence excluded regions must be a tuple")
        if type(self.zone_ids) is not tuple or any(not isinstance(item, BoardObjectId) for item in self.zone_ids):
            raise TypeError("copper evidence zone ids must be a tuple")
        if type(self.zone_digests) is not tuple or any(_DIGEST.fullmatch(item) is None for item in self.zone_digests):
            raise ValueError("copper evidence zone digests are invalid")
        if len(self.zone_ids) != len(self.zone_digests):
            raise ValueError("copper evidence zone ids and digests must align")
        if type(self.thermal_policy_pad_ids) is not tuple or any(
            not isinstance(item, BoardObjectId) for item in self.thermal_policy_pad_ids
        ):
            raise TypeError("copper evidence thermal policy pad ids must be a tuple")
        if type(self.thermal_policy_digests) is not tuple or any(
            _DIGEST.fullmatch(item) is None for item in self.thermal_policy_digests
        ):
            raise ValueError("copper evidence thermal policy digests are invalid")
        if len(self.thermal_policy_pad_ids) != len(self.thermal_policy_digests):
            raise ValueError("copper evidence thermal policies must align")
        if type(self.via_coordinates) is not tuple or any(not isinstance(item, PointUm) for item in self.via_coordinates):
            raise TypeError("copper evidence via coordinates must be a tuple")


@dataclass(frozen=True, slots=True)
class CopperResult:
    operations: tuple[CreateCopperZones | AddGroundStitching, ...]
    zones: tuple[CopperZone, ...]
    vias: tuple[Via, ...]
    thermal_policies: tuple[ThermalPolicy, ...]
    stitching_policy: StitchingPolicy | None
    findings: tuple[NormalizedFinding, ...]
    evidence: CopperEvidence


@dataclass(frozen=True, slots=True)
class _Tile:
    layer: str
    bounds: RectUm


class CopperPlanner:
    """Plan deterministic, clipped rectangular GND surfaces for BoardIR V1.

    BoardIR V1 intentionally stores rectangles rather than polygon paths. The
    planner therefore emits a set of safe rectangular tiles. A tile is only
    retained when its same-layer component is anchored to existing GND
    geometry; unanchored and undersized tiles become explicit findings.
    """

    def plan(
        self, snapshot: BoardSnapshot, rulepack: ManufacturingRulePack
    ) -> CopperResult:
        snapshot_digest = snapshot.canonical_digest()
        rulepack_digest = rulepack.canonical_digest()
        try:
            gnd = snapshot.net(_GND)
            ground_rule = rulepack.net_class(gnd.net_class)
        except KeyError:
            finding = _finding(
                "PCB.COPPER.GND_MISSING",
                "GND",
                "snapshot has no GND net or ground net class",
            )
            return self._empty_result(snapshot_digest, rulepack_digest, (finding,))

        baseline_copper_findings = tuple(
            item
            for item in BoardRuleChecker().check(snapshot, rulepack)
            if item.rule_id.startswith("PCB.COPPER.") and item.severity == "error"
        )
        if baseline_copper_findings:
            return self._empty_result(snapshot_digest, rulepack_digest, baseline_copper_findings)

        if not _is_axis_aligned_rectangle(snapshot.outline):
            finding = _finding(
                "PCB.COPPER.OUTLINE_UNSUPPORTED",
                snapshot.profile_id,
                "V1 rectangular copper tiles require an axis-aligned rectangular outline",
            )
            return self._empty_result(snapshot_digest, rulepack_digest, (finding,))

        board = _board_rect(snapshot, rulepack.copper.edge_clearance_um)
        if board is None:
            finding = _finding(
                "PCB.COPPER.BOARD_TOO_SMALL",
                snapshot.profile_id,
                "board edge clearance leaves no copper area",
            )
            return self._empty_result(snapshot_digest, rulepack_digest, (finding,))

        excluded: list[ExcludedRegion] = []
        tiles_by_layer: dict[str, list[_Tile]] = {}
        island_ids: list[str] = []
        zones: list[CopperZone] = []
        used_ids = _all_object_ids(snapshot)

        for layer in snapshot.layers:
            regions = _excluded_regions(snapshot, rulepack, board, layer, ground_rule.clearance_um)
            excluded.extend(regions)
            remaining = [board]
            for region in regions:
                remaining = _subtract_all(remaining, region.bounds)
            tiles = [_Tile(layer, item) for item in remaining]
            anchored, unanchored = _anchored_tiles(tiles, snapshot)
            for tile in unanchored:
                island_ids.append(_island_id(tile))
            tiles_by_layer[layer] = anchored
            for tile in anchored:
                if tile.bounds.area_um2 < rulepack.copper.min_island_area_um2:
                    island_ids.append(_island_id(tile))
                    continue
                identifier = _next_id(
                    f"copper_gnd_{_layer_token(layer)}_", used_ids, len(zones) + 1
                )
                used_ids.add(identifier)
                zones.append(
                    CopperZone(
                        id=identifier,
                        net_id=_GND,
                        layer=layer,
                        bounds=tile.bounds,
                        clearance_um=ground_rule.clearance_um,
                        route_lock=False,
                    )
                )

        island_ids = sorted(set(island_ids))
        island_findings = tuple(
            _finding(
                "PCB.COPPER.ISLAND",
                island_id,
                "discarded unanchored or undersized copper tile",
                severity="warning",
            )
            for island_id in island_ids
        )

        thermal_policies = (
            tuple(
                pad.thermal_policy
                if pad.thermal_policy is not None
                else _thermal_policy(pad, rulepack.copper, snapshot.layers)
                for pad in snapshot.pads
                if pad.net_id == _GND
            )
            if zones
            else ()
        )
        stitching_policy = StitchingPolicy(
            net_id=_GND,
            pitch_um=rulepack.copper.stitching_pitch_um,
            max_vias=rulepack.copper.max_stitching_vias,
            edge_clearance_um=rulepack.copper.edge_clearance_um,
            via_diameter_um=ground_rule.min_via_diameter_um,
            via_hole_diameter_um=ground_rule.min_via_hole_um,
            layers=tuple(snapshot.layers),
        )
        vias = _stitching_vias(
            snapshot,
            rulepack,
            stitching_policy,
            tuple(zones),
            used_ids,
        )

        thermal_policy_by_pad = {
            item.pad_id: item for item in thermal_policies
        }
        proposed = replace(
            snapshot,
            copper_zones=snapshot.copper_zones + tuple(zones),
            vias=snapshot.vias + tuple(vias),
            pads=tuple(
                replace(pad, thermal_policy=thermal_policy_by_pad[pad.id])
                if pad.id in thermal_policy_by_pad
                else pad
                for pad in snapshot.pads
            ),
        )
        post_fill_findings = BoardRuleChecker().check(proposed, rulepack)
        findings = tuple(
            sorted(
                island_findings + post_fill_findings,
                key=lambda item: (item.rule_id, item.subject, item.message, item.severity),
            )
        )
        blocking = any(item.severity == "error" for item in findings)
        if blocking:
            operations: tuple[CreateCopperZones | AddGroundStitching, ...] = ()
            output_zones: tuple[CopperZone, ...] = ()
            output_vias: tuple[Via, ...] = ()
            output_thermal_policies: tuple[ThermalPolicy, ...] = ()
        else:
            output_zones = tuple(zones)
            output_vias = tuple(vias)
            output_thermal_policies = thermal_policies
            operations_list: list[CreateCopperZones | AddGroundStitching] = []
            if zones:
                zone_ids = tuple(item.id for item in zones)
                operations_list.append(
                    CreateCopperZones(
                        project_id=snapshot.profile_id,
                        baseline_revision=snapshot_digest,
                        risk="medium",
                        rulepack_digest=rulepack_digest,
                        target_object_ids=zone_ids
                        + tuple(item.pad_id for item in thermal_policies),
                        idempotency_key=_operation_key("zones", snapshot_digest, rulepack_digest, zone_ids),
                        expected_snapshot_digest=snapshot_digest,
                        zone_ids=zone_ids,
                        zones=tuple(zones),
                        thermal_policies=thermal_policies,
                    )
                )
            if vias:
                via_ids = tuple(item.id for item in vias)
                operations_list.append(
                    AddGroundStitching(
                        project_id=snapshot.profile_id,
                        baseline_revision=snapshot_digest,
                        risk="medium",
                        rulepack_digest=rulepack_digest,
                        target_object_ids=via_ids,
                        idempotency_key=_operation_key("stitching", snapshot_digest, rulepack_digest, via_ids),
                        expected_snapshot_digest=snapshot_digest,
                        via_ids=via_ids,
                        vias=tuple(vias),
                    )
                )
            operations = tuple(operations_list)

        evidence = CopperEvidence(
            objective_version=_OBJECTIVE_VERSION,
            snapshot_digest=snapshot_digest,
            rulepack_digest=rulepack_digest,
            representation="rectangular_tiles_v1",
            excluded_regions=tuple(sorted(excluded, key=lambda item: (item.layer, item.region_id))),
            zone_ids=tuple(item.id for item in output_zones),
            zone_digests=tuple(_zone_digest(item) for item in output_zones),
            thermal_policy_pad_ids=tuple(
                item.pad_id for item in output_thermal_policies
            ),
            thermal_policy_digests=tuple(
                _thermal_policy_digest(item) for item in output_thermal_policies
            ),
            island_ids=tuple(island_ids),
            stitching_via_ids=tuple(item.id for item in output_vias),
            via_coordinates=tuple(item.position for item in output_vias),
            connectivity=tuple(
                sorted(
                    (str(item.id), not any(
                        finding.rule_id == "PCB.COPPER.CONNECTIVITY"
                        and finding.subject == str(item.id)
                        for finding in post_fill_findings
                    ))
                    for item in output_zones
                )
            ),
            post_fill_finding_ids=tuple(
                sorted({item.rule_id for item in post_fill_findings})
            ),
        )
        return CopperResult(
            operations=operations,
            zones=output_zones,
            vias=output_vias,
            thermal_policies=output_thermal_policies,
            stitching_policy=stitching_policy,
            findings=findings,
            evidence=evidence,
        )

    @staticmethod
    def _empty_result(
        snapshot_digest: str,
        rulepack_digest: str,
        findings: tuple[NormalizedFinding, ...],
    ) -> CopperResult:
        return CopperResult(
            operations=(),
            zones=(),
            vias=(),
            thermal_policies=(),
            stitching_policy=None,
            findings=findings,
            evidence=CopperEvidence(
                objective_version=_OBJECTIVE_VERSION,
                snapshot_digest=snapshot_digest,
                rulepack_digest=rulepack_digest,
                representation="rectangular_tiles_v1",
                excluded_regions=(),
                zone_ids=(),
                zone_digests=(),
                thermal_policy_pad_ids=(),
                thermal_policy_digests=(),
                island_ids=(),
                stitching_via_ids=(),
                via_coordinates=(),
                connectivity=(),
                post_fill_finding_ids=tuple(sorted(item.rule_id for item in findings)),
            ),
        )


def _board_rect(snapshot: BoardSnapshot, edge_clearance_um: int) -> RectUm | None:
    min_x = min(item.x for item in snapshot.outline)
    min_y = min(item.y for item in snapshot.outline)
    max_x = max(item.x for item in snapshot.outline)
    max_y = max(item.y for item in snapshot.outline)
    width = max_x - min_x - 2 * edge_clearance_um
    height = max_y - min_y - 2 * edge_clearance_um
    if width < 1 or height < 1:
        return None
    return RectUm(min_x + edge_clearance_um, min_y + edge_clearance_um, width, height)


def _is_axis_aligned_rectangle(outline: tuple[PointUm, ...]) -> bool:
    if len(outline) != 4:
        return False
    min_x = min(item.x for item in outline)
    min_y = min(item.y for item in outline)
    max_x = max(item.x for item in outline)
    max_y = max(item.y for item in outline)
    corners = {
        (item.x, item.y)
        for item in outline
    }
    return corners == {
        (min_x, min_y),
        (max_x, min_y),
        (max_x, max_y),
        (min_x, max_y),
    } and all(
        left.x == right.x or left.y == right.y
        for left, right in zip(outline, outline[1:] + outline[:1], strict=True)
    )


def _excluded_regions(
    snapshot: BoardSnapshot,
    rulepack: ManufacturingRulePack,
    board: RectUm,
    layer: str,
    ground_clearance_um: int,
) -> list[ExcludedRegion]:
    regions: list[ExcludedRegion] = []
    for keepout in snapshot.keepouts:
        if layer not in keepout.layers or not _keepout_applies_to_copper(keepout):
            continue
        bounds = _expanded_clipped(
            keepout.bounds,
            ground_clearance_um + 1,
            board,
        )
        if bounds is not None:
            regions.append(ExcludedRegion(f"{keepout.id}:{layer}", layer, bounds, "keepout"))

    for pad in snapshot.pads:
        if pad.net_id == _GND or layer not in pad.layers:
            continue
        other_clearance = _net_clearance(rulepack, snapshot, pad.net_id)
        bounds = _expanded_clipped(
            _pad_bounds(pad),
            ground_clearance_um + other_clearance + 1,
            board,
        )
        if bounds is not None:
            regions.append(ExcludedRegion(f"{pad.id}:{layer}", layer, bounds, "pad_clearance"))

    for route in snapshot.routes:
        if route.net_id == _GND or route.layer != layer:
            continue
        other_clearance = _net_clearance(rulepack, snapshot, route.net_id)
        bounds = _expanded_clipped(
            _segment_bounds(route.start, route.end, route.width_um // 2),
            ground_clearance_um + other_clearance + 1,
            board,
        )
        if bounds is not None:
            regions.append(ExcludedRegion(f"{route.id}:{layer}", layer, bounds, "route_clearance"))

    for via in snapshot.vias:
        if via.net_id == _GND or layer not in via.layers:
            continue
        other_clearance = _net_clearance(rulepack, snapshot, via.net_id)
        bounds = _expanded_clipped(
            _point_bounds(via.position, via.diameter_um // 2),
            ground_clearance_um + other_clearance + 1,
            board,
        )
        if bounds is not None:
            regions.append(ExcludedRegion(f"{via.id}:{layer}", layer, bounds, "via_clearance"))

    for zone in snapshot.copper_zones:
        if zone.layer != layer:
            continue
        if zone.net_id == _GND:
            bounds = _expanded_clipped(zone.bounds, 0, board)
            reason = "existing_gnd_zone"
        else:
            bounds = _expanded_clipped(
                zone.bounds,
                ground_clearance_um + zone.clearance_um + 1,
                board,
            )
            reason = "zone_clearance"
        if bounds is not None:
            regions.append(ExcludedRegion(f"{zone.id}:{layer}", layer, bounds, reason))
    return sorted(regions, key=lambda item: (item.layer, item.bounds.x, item.bounds.y, item.region_id))


def _keepout_applies_to_copper(keepout: Keepout) -> bool:
    return "copper_zone" in keepout.prohibited or keepout.kind in _SENSITIVE_KEEP_OUT_KINDS


def _subtract_all(rects: list[RectUm], cut: RectUm) -> list[RectUm]:
    result: list[RectUm] = []
    for rect in rects:
        result.extend(_subtract(rect, cut))
    return sorted(result, key=lambda item: (item.x, item.y, item.width, item.height))


def _subtract(rect: RectUm, cut: RectUm) -> list[RectUm]:
    left = max(rect.x, cut.x)
    bottom = max(rect.y, cut.y)
    right = min(rect.x + rect.width, cut.x + cut.width)
    top = min(rect.y + rect.height, cut.y + cut.height)
    if left >= right or bottom >= top:
        return [rect]
    pieces: list[RectUm] = []
    if left > rect.x:
        pieces.append(RectUm(rect.x, rect.y, left - rect.x, rect.height))
    if right < rect.x + rect.width:
        pieces.append(RectUm(right, rect.y, rect.x + rect.width - right, rect.height))
    if bottom > rect.y:
        pieces.append(RectUm(left, rect.y, right - left, bottom - rect.y))
    if top < rect.y + rect.height:
        pieces.append(RectUm(left, top, right - left, rect.y + rect.height - top))
    return pieces


def _anchored_tiles(
    tiles: list[_Tile], snapshot: BoardSnapshot
) -> tuple[list[_Tile], list[_Tile]]:
    if not tiles:
        return [], []
    parent = list(range(len(tiles)))

    def root(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left, right = root(left), root(right)
        if left != right:
            parent[right] = left

    for index, tile in enumerate(tiles):
        for other_index in range(index):
            other = tiles[other_index]
            if tile.layer == other.layer and _rects_connected(tile.bounds, other.bounds):
                union(index, other_index)

    anchored_roots: set[int] = set()
    for index, tile in enumerate(tiles):
        if any(_tile_touches_entity(tile, entity) for entity in _ground_entities(snapshot)):
            anchored_roots.add(root(index))
    return (
        [tile for index, tile in enumerate(tiles) if root(index) in anchored_roots],
        [tile for index, tile in enumerate(tiles) if root(index) not in anchored_roots],
    )


def _ground_entities(snapshot: BoardSnapshot) -> tuple[Pad | RouteSegment | Via | CopperZone, ...]:
    return tuple(item for item in snapshot.pads if item.net_id == _GND) + tuple(
        item for item in snapshot.routes if item.net_id == _GND
    ) + tuple(item for item in snapshot.vias if item.net_id == _GND) + tuple(
        item for item in snapshot.copper_zones if item.net_id == _GND
    )


def _tile_touches_entity(tile: _Tile, entity: Pad | RouteSegment | Via | CopperZone) -> bool:
    if isinstance(entity, CopperZone):
        return entity.layer == tile.layer and _rects_connected(tile.bounds, entity.bounds)
    if isinstance(entity, Pad):
        return tile.layer in entity.layers and _point_rect_within(
            entity.position, tile.bounds, max(entity.size_x_um, entity.size_y_um) // 2
        )
    if isinstance(entity, RouteSegment):
        return entity.layer == tile.layer and _segment_rect_within(
            entity.start, entity.end, tile.bounds, entity.width_um // 2
        )
    return tile.layer in entity.layers and _point_rect_within(
        entity.position, tile.bounds, entity.diameter_um // 2
    )


def _stitching_vias(
    snapshot: BoardSnapshot,
    rulepack: ManufacturingRulePack,
    policy: StitchingPolicy,
    zones: tuple[CopperZone, ...],
    used_ids: set[BoardObjectId],
) -> list[Via]:
    board = _board_rect(snapshot, policy.edge_clearance_um)
    if board is None or policy.max_vias == 0:
        return []
    all_zones = snapshot.copper_zones + zones
    step = policy.pitch_um
    candidates: list[PointUm] = []
    x = board.x + step // 2
    while x < board.x + board.width:
        y = board.y + step // 2
        while y < board.y + board.height:
            candidates.append(PointUm(x, y))
            y += step
        x += step
    candidates.sort(key=lambda item: (item.y, item.x))
    result: list[Via] = []
    via_radius = policy.via_diameter_um // 2
    for point in candidates:
        if len(result) >= policy.max_vias:
            break
        if not all(
            any(
                zone.net_id == _GND
                and zone.layer == layer
                and _rect_contains_circle(zone.bounds, point, via_radius)
                for zone in all_zones
            )
            for layer in policy.layers
        ):
            continue
        if _stitching_point_blocked(point, snapshot, rulepack, policy, result):
            continue
        identifier = _next_id("via_copper_stitch_", used_ids, len(result) + 1)
        used_ids.add(identifier)
        result.append(
            Via(
                id=identifier,
                net_id=_GND,
                position=point,
                diameter_um=policy.via_diameter_um,
                hole_diameter_um=policy.via_hole_diameter_um,
                layers=policy.layers,
                route_lock=False,
            )
        )
    return result


def _stitching_point_blocked(
    point: PointUm,
    snapshot: BoardSnapshot,
    rulepack: ManufacturingRulePack,
    policy: StitchingPolicy,
    generated: list[Via],
) -> bool:
    ground_radius = policy.via_diameter_um // 2
    for keepout in snapshot.keepouts:
        if not set(policy.layers) & set(keepout.layers):
            continue
        if (
            "via" in keepout.prohibited
            or keepout.kind in _SENSITIVE_KEEP_OUT_KINDS
        ) and _point_rect_within(point, keepout.bounds, ground_radius + policy.edge_clearance_um):
            return True
    min_x = min(item.x for item in snapshot.outline)
    min_y = min(item.y for item in snapshot.outline)
    max_x = max(item.x for item in snapshot.outline)
    max_y = max(item.y for item in snapshot.outline)
    if (
        point.x < min_x + policy.edge_clearance_um + ground_radius
        or point.x > max_x - policy.edge_clearance_um - ground_radius
        or point.y < min_y + policy.edge_clearance_um + ground_radius
        or point.y > max_y - policy.edge_clearance_um - ground_radius
    ):
        return True
    for pad in snapshot.pads:
        if pad.net_id == _GND or not set(policy.layers) & set(pad.layers):
            continue
        required = ground_radius + max(pad.size_x_um, pad.size_y_um) // 2 + policy.edge_clearance_um + _net_clearance(rulepack, snapshot, pad.net_id)
        if _point_rect_within(point, _pad_bounds(pad), required):
            return True
    for route in snapshot.routes:
        if route.net_id == _GND or route.layer not in policy.layers:
            continue
        required = ground_radius + route.width_um // 2 + policy.edge_clearance_um + _net_clearance(rulepack, snapshot, route.net_id)
        if _point_segment_within(point, route.start, route.end, required):
            return True
    for via in snapshot.vias + tuple(generated):
        if via.net_id == _GND or not set(policy.layers) & set(via.layers):
            continue
        required = ground_radius + via.diameter_um // 2 + policy.edge_clearance_um + _net_clearance(rulepack, snapshot, via.net_id)
        if _point_distance_within(point, via.position, required):
            return True
    return False


def _thermal_policy(
    pad: Pad, policy: CopperPolicy, layers: tuple[str, ...]
) -> ThermalPolicy:
    return ThermalPolicy(
        pad_id=pad.id,
        net_id=_GND,
        layers=tuple(layer for layer in layers if layer in pad.layers),
        style="thermal_relief",
        spoke_count=policy.thermal_spoke_count,
        spoke_width_um=policy.thermal_spoke_width_um,
        gap_um=policy.thermal_gap_um,
    )


def _all_object_ids(snapshot: BoardSnapshot) -> set[BoardObjectId]:
    return {
        item.id
        for collection in (
            snapshot.net_classes,
            snapshot.nets,
            snapshot.keepouts,
            snapshot.footprints,
            snapshot.pads,
            snapshot.routes,
            snapshot.vias,
            snapshot.copper_zones,
            snapshot.opaque_nodes,
        )
        for item in collection
    }


def _next_id(prefix: str, used: set[BoardObjectId], ordinal: int) -> BoardObjectId:
    candidate = BoardObjectId(f"{prefix}{ordinal:03d}")
    while candidate in used:
        ordinal += 1
        candidate = BoardObjectId(f"{prefix}{ordinal:03d}")
    return candidate


def _operation_key(
    kind: str,
    snapshot_digest: str,
    rulepack_digest: str,
    identifiers: tuple[BoardObjectId, ...],
) -> str:
    payload = {
        "objective_version": _OBJECTIVE_VERSION,
        "kind": kind,
        "snapshot_digest": snapshot_digest,
        "rulepack_digest": rulepack_digest,
        "identifiers": [str(item) for item in identifiers],
    }
    return f"copper-{kind}-" + hashlib.sha256(canonical_json_bytes(payload)).hexdigest()[:32]


def _zone_digest(zone: CopperZone) -> str:
    payload = {
        "id": str(zone.id),
        "net_id": str(zone.net_id),
        "layer": zone.layer,
        "bounds": {
            "x": zone.bounds.x,
            "y": zone.bounds.y,
            "width": zone.bounds.width,
            "height": zone.bounds.height,
        },
        "clearance_um": zone.clearance_um,
    }
    return "sha256:" + hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _thermal_policy_digest(policy: ThermalPolicy) -> str:
    payload = {
        "pad_id": str(policy.pad_id),
        "net_id": str(policy.net_id),
        "layers": list(policy.layers),
        "style": policy.style,
        "spoke_count": policy.spoke_count,
        "spoke_width_um": policy.spoke_width_um,
        "gap_um": policy.gap_um,
        "locked": policy.locked,
    }
    return "sha256:" + hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _layer_token(layer: str) -> str:
    return "f" if layer == "F.Cu" else "b" if layer == "B.Cu" else layer.lower().replace(".", "_")


def _island_id(tile: _Tile) -> str:
    return f"{tile.layer}:{tile.bounds.x}:{tile.bounds.y}:{tile.bounds.width}:{tile.bounds.height}"


def _expanded_clipped(rect: RectUm, amount: int, clip: RectUm) -> RectUm | None:
    left = max(clip.x, rect.x - amount)
    bottom = max(clip.y, rect.y - amount)
    right = min(clip.x + clip.width, rect.x + rect.width + amount)
    top = min(clip.y + clip.height, rect.y + rect.height + amount)
    if left >= right or bottom >= top:
        return None
    return RectUm(left, bottom, right - left, top - bottom)


def _segment_bounds(start: PointUm, end: PointUm, radius: int) -> RectUm:
    left = min(start.x, end.x) - radius
    bottom = min(start.y, end.y) - radius
    return RectUm(left, bottom, max(start.x, end.x) - left + radius, max(start.y, end.y) - bottom + radius)


def _point_bounds(point: PointUm, radius: int) -> RectUm:
    return RectUm(point.x - radius, point.y - radius, max(1, 2 * radius), max(1, 2 * radius))


def _pad_bounds(pad: Pad) -> RectUm:
    return RectUm(
        pad.position.x - pad.size_x_um // 2,
        pad.position.y - pad.size_y_um // 2,
        pad.size_x_um,
        pad.size_y_um,
    )


def _net_clearance(
    rulepack: ManufacturingRulePack,
    snapshot: BoardSnapshot,
    net_id: BoardObjectId | None,
) -> int:
    if net_id is None:
        return 0
    try:
        return rulepack.net_class(snapshot.net(net_id).net_class).clearance_um
    except KeyError:
        return 0


def _rects_connected(left: RectUm, right: RectUm) -> bool:
    x_overlap = min(left.x + left.width, right.x + right.width) - max(left.x, right.x)
    y_overlap = min(left.y + left.height, right.y + right.height) - max(left.y, right.y)
    return (x_overlap >= 0 and y_overlap > 0) or (x_overlap > 0 and y_overlap >= 0)


def _point_rect_within(point: PointUm, rect: RectUm, distance: int) -> bool:
    dx = max(rect.x - point.x, 0, point.x - (rect.x + rect.width))
    dy = max(rect.y - point.y, 0, point.y - (rect.y + rect.height))
    return dx * dx + dy * dy <= distance * distance


def _segment_rect_within(
    start: PointUm, end: PointUm, rect: RectUm, distance: int = 0
) -> bool:
    if max(rect.x - distance, min(start.x, end.x)) > min(
        rect.x + rect.width + distance, max(start.x, end.x)
    ):
        return False
    if max(rect.y - distance, min(start.y, end.y)) > min(
        rect.y + rect.height + distance, max(start.y, end.y)
    ):
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
    corners = (
        PointUm(rect.x, rect.y),
        PointUm(rect.x + rect.width, rect.y),
        PointUm(rect.x + rect.width, rect.y + rect.height),
        PointUm(rect.x, rect.y + rect.height),
    )
    return any(
        _segments_intersect(start, end, left, right)
        for left, right in zip(corners, corners[1:] + corners[:1], strict=True)
    )


def _segments_intersect(a: PointUm, b: PointUm, c: PointUm, d: PointUm) -> bool:
    def cross(p: PointUm, q: PointUm, r: PointUm) -> int:
        return (q.x - p.x) * (r.y - p.y) - (q.y - p.y) * (r.x - p.x)

    def between(p: PointUm, q: PointUm, r: PointUm) -> bool:
        return min(p.x, r.x) <= q.x <= max(p.x, r.x) and min(p.y, r.y) <= q.y <= max(p.y, r.y)

    first, second = cross(a, b, c), cross(a, b, d)
    third, fourth = cross(c, d, a), cross(c, d, b)
    return (
        (first == 0 and between(a, c, b))
        or (second == 0 and between(a, d, b))
        or (third == 0 and between(c, a, d))
        or (fourth == 0 and between(c, b, d))
        or ((first > 0) != (second > 0) and (third > 0) != (fourth > 0))
    )


def _rect_contains_circle(rect: RectUm, point: PointUm, radius: int) -> bool:
    return (
        point.x - radius >= rect.x
        and point.y - radius >= rect.y
        and point.x + radius <= rect.x + rect.width
        and point.y + radius <= rect.y + rect.height
    )


def _point_distance_within(point: PointUm, other: PointUm, distance: int) -> bool:
    dx, dy = point.x - other.x, point.y - other.y
    return dx * dx + dy * dy <= distance * distance


def _point_segment_within(point: PointUm, start: PointUm, end: PointUm, distance: int) -> bool:
    dx, dy = end.x - start.x, end.y - start.y
    if point.x < min(start.x, end.x) - distance or point.x > max(start.x, end.x) + distance:
        return False
    if point.y < min(start.y, end.y) - distance or point.y > max(start.y, end.y) + distance:
        return False
    length_sq = dx * dx + dy * dy
    if length_sq == 0:
        return _point_distance_within(point, start, distance)
    projection = (point.x - start.x) * dx + (point.y - start.y) * dy
    if projection <= 0:
        return _point_distance_within(point, start, distance)
    if projection >= length_sq:
        return _point_distance_within(point, end, distance)
    cross = (point.x - start.x) * dy - (point.y - start.y) * dx
    return cross * cross <= distance * distance * length_sq


def _finding(
    rule_id: str,
    subject: str | BoardObjectId,
    message: str,
    *,
    severity: str = "error",
) -> NormalizedFinding:
    return NormalizedFinding(
        rule_id=rule_id,
        severity=severity,
        subject=str(subject),
        message=message,
    )


def _validate_string_tuple(values: tuple[str, ...], context: str) -> None:
    if type(values) is not tuple or not values:
        raise ValueError(f"{context} must be a non-empty tuple")
    if any(type(item) is not str or not item or item != item.strip() for item in values):
        raise ValueError(f"{context} must contain canonical strings")
    if len(values) != len(set(values)):
        raise ValueError(f"{context} must be unique")


__all__ = [
    "CopperEvidence",
    "CopperPlanner",
    "CopperResult",
    "ExcludedRegion",
    "StitchingPolicy",
    "ThermalPolicy",
]

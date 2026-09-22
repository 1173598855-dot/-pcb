"""Shared axis-aligned segment/rectangle geometry predicates.

These helpers were previously duplicated verbatim in ``copper.py``,
``routing.py`` and ``validation.py``. They operate on integer micrometre
coordinates, so every comparison must stay in exact integer arithmetic;
``segments_intersect`` deliberately keeps the raw cross-product form
(instead of a normalised orientation sign) to avoid changing overflow or
comparison behaviour for large coordinates.
"""

from __future__ import annotations

from .ir import BoardObjectId, BoardSnapshot, Pad, PointUm, RectUm
from .rulepack import ManufacturingRulePack


def pad_bounds(pad: Pad) -> RectUm:
    return RectUm(
        pad.position.x - pad.size_x_um // 2,
        pad.position.y - pad.size_y_um // 2,
        pad.size_x_um,
        pad.size_y_um,
    )


def net_clearance(
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


def point_distance_within(point: PointUm, other: PointUm, distance: int) -> bool:
    """True when two points are within ``distance`` (inclusive)."""
    dx, dy = point.x - other.x, point.y - other.y
    return dx * dx + dy * dy <= distance * distance


def point_segment_within(
    point: PointUm, start: PointUm, end: PointUm, distance: int
) -> bool:
    """True when ``point`` lies within ``distance`` of the ``start``-``end`` segment."""
    dx, dy = end.x - start.x, end.y - start.y
    if (
        point.x < min(start.x, end.x) - distance
        or point.x > max(start.x, end.x) + distance
        or point.y < min(start.y, end.y) - distance
        or point.y > max(start.y, end.y) + distance
    ):
        return False
    px, py = point.x - start.x, point.y - start.y
    length_sq = dx * dx + dy * dy
    if length_sq == 0:
        return point_distance_within(point, start, distance)
    projection = px * dx + py * dy
    if projection <= 0:
        return point_distance_within(point, start, distance)
    if projection >= length_sq:
        return point_distance_within(point, end, distance)
    cross = px * dy - py * dx
    return cross * cross <= distance * distance * length_sq


def segments_intersect(
    a: PointUm, b: PointUm, c: PointUm, d: PointUm
) -> bool:
    """True when segment ``a``-``b`` intersects segment ``c``-``d`` (touching counts)."""

    def cross(p: PointUm, q: PointUm, r: PointUm) -> int:
        return (q.x - p.x) * (r.y - p.y) - (q.y - p.y) * (r.x - p.x)

    def between(p: PointUm, q: PointUm, r: PointUm) -> bool:
        return (
            min(p.x, r.x) <= q.x <= max(p.x, r.x)
            and min(p.y, r.y) <= q.y <= max(p.y, r.y)
        )

    first, second = cross(a, b, c), cross(a, b, d)
    third, fourth = cross(c, d, a), cross(c, d, b)
    return (
        (first == 0 and between(a, c, b))
        or (second == 0 and between(a, d, b))
        or (third == 0 and between(c, a, d))
        or (fourth == 0 and between(c, b, d))
        or ((first > 0) != (second > 0) and (third > 0) != (fourth > 0))
    )


def _rect_corners(rect: RectUm) -> tuple[PointUm, PointUm, PointUm, PointUm]:
    return (
        PointUm(rect.x, rect.y),
        PointUm(rect.x + rect.width, rect.y),
        PointUm(rect.x + rect.width, rect.y + rect.height),
        PointUm(rect.x, rect.y + rect.height),
    )


def segment_intersects_rect(start: PointUm, end: PointUm, rect: RectUm) -> bool:
    """True when segment ``start``-``end`` crosses or touches ``rect``."""
    if rect.contains(start) or rect.contains(end):
        return True
    corners = _rect_corners(rect)
    return any(
        segments_intersect(start, end, left, right)
        for left, right in zip(corners, corners[1:] + corners[:1], strict=True)
    )


def point_rect_within(point: PointUm, rect: RectUm, distance: int) -> bool:
    """True when ``point`` lies within ``distance`` of axis-aligned ``rect``."""
    dx = max(rect.x - point.x, 0, point.x - (rect.x + rect.width))
    dy = max(rect.y - point.y, 0, point.y - (rect.y + rect.height))
    return dx * dx + dy * dy <= distance * distance


def rect_contains_circle(rect: RectUm, point: PointUm, radius: int) -> bool:
    """True when a circle of ``radius`` at ``point`` fits inside ``rect``."""
    return (
        point.x - radius >= rect.x
        and point.y - radius >= rect.y
        and point.x + radius <= rect.x + rect.width
        and point.y + radius <= rect.y + rect.height
    )


def segments_within(
    a: PointUm, b: PointUm, c: PointUm, d: PointUm, distance: int
) -> bool:
    """True when segment ``a``-``b`` and segment ``c``-``d`` are within ``distance``."""
    if max(min(a.x, b.x), min(c.x, d.x)) > min(max(a.x, b.x), max(c.x, d.x)) + distance:
        return False
    if max(min(a.y, b.y), min(c.y, d.y)) > min(max(a.y, b.y), max(c.y, d.y)) + distance:
        return False
    if segments_intersect(a, b, c, d):
        return True
    return (
        point_segment_within(a, c, d, distance)
        or point_segment_within(b, c, d, distance)
        or point_segment_within(c, a, b, distance)
        or point_segment_within(d, a, b, distance)
    )


def segment_rect_within(
    start: PointUm, end: PointUm, rect: RectUm, distance: int = 0
) -> bool:
    """True when the ``start``-``end`` segment stays within ``distance`` of ``rect``."""
    if max(rect.x - distance, min(start.x, end.x)) > min(
        rect.x + rect.width + distance, max(start.x, end.x)
    ):
        return False
    if max(rect.y - distance, min(start.y, end.y)) > min(
        rect.y + rect.height + distance, max(start.y, end.y)
    ):
        return False
    if segment_intersects_rect(start, end, rect):
        return True
    corners = _rect_corners(rect)
    if any(
        point_segment_within(corner, start, end, distance) for corner in corners
    ):
        return True
    return any(
        point_segment_within(start, left, right, distance)
        or point_segment_within(end, left, right, distance)
        for left, right in zip(corners, corners[1:] + corners[:1], strict=True)
    )


__all__ = [
    "net_clearance",
    "pad_bounds",
    "point_distance_within",
    "point_rect_within",
    "point_segment_within",
    "rect_contains_circle",
    "segment_intersects_rect",
    "segment_rect_within",
    "segments_intersect",
    "segments_within",
]

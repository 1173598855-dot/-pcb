from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import ClassVar, Mapping

from .ir import BoardObjectId, CopperZone, PointUm, RouteSegment, ThermalPolicy, Via


_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class FootprintPlacement:
    footprint_id: BoardObjectId
    position: PointUm
    layer: str

    def __post_init__(self) -> None:
        if not isinstance(self.footprint_id, BoardObjectId):
            raise TypeError("footprint placement id must be a BoardObjectId")
        if not isinstance(self.position, PointUm):
            raise TypeError("footprint placement position must be PointUm")
        if type(self.layer) is not str or not self.layer or self.layer != self.layer.strip():
            raise ValueError("footprint placement layer must be a canonical string")


@dataclass(frozen=True, slots=True, kw_only=True)
class BoardOperation:
    project_id: str
    baseline_revision: str
    risk: str
    rulepack_digest: str
    target_object_ids: tuple[BoardObjectId, ...]
    idempotency_key: str
    expected_snapshot_digest: str

    operation_type: ClassVar[str] = "board.operation"

    def __post_init__(self) -> None:
        for name, value in (
            ("project id", self.project_id),
            ("baseline revision", self.baseline_revision),
            ("idempotency key", self.idempotency_key),
        ):
            if type(value) is not str or not value or value != value.strip():
                raise ValueError(f"{name} must be a non-empty canonical string")
        if self.risk not in {"low", "medium", "high"}:
            raise ValueError("board operation risk must be low, medium, or high")
        for name, digest in (
            ("rule pack digest", self.rulepack_digest),
            ("expected snapshot digest", self.expected_snapshot_digest),
        ):
            if type(digest) is not str or _DIGEST.fullmatch(digest) is None:
                raise ValueError(f"{name} must be a canonical SHA-256 digest")
        _validate_ids(self.target_object_ids, "target object ids")


@dataclass(frozen=True, slots=True, kw_only=True)
class PlaceFootprints(BoardOperation):
    placements: tuple[FootprintPlacement, ...] = ()
    operation_type: ClassVar[str] = "board.place_footprints"

    def __post_init__(self) -> None:
        super(PlaceFootprints, self).__post_init__()
        if type(self.placements) is not tuple or any(
            not isinstance(item, FootprintPlacement) for item in self.placements
        ):
            raise TypeError("placements must be a tuple of FootprintPlacement")
        if len({item.footprint_id for item in self.placements}) != len(self.placements):
            raise ValueError("placements must contain each footprint at most once")

    @property
    def placements_by_id(self) -> Mapping[str, FootprintPlacement]:
        """Read-only lookup that keeps placement consumers out of tuple scans."""
        return MappingProxyType(
            {str(item.footprint_id): item for item in self.placements}
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class RouteNets(BoardOperation):
    net_ids: tuple[BoardObjectId, ...] = ()
    segments: tuple[RouteSegment, ...] = ()
    vias: tuple[Via, ...] = ()
    removed_route_ids: tuple[BoardObjectId, ...] = ()
    operation_type: ClassVar[str] = "board.route_nets"

    def __post_init__(self) -> None:
        super(RouteNets, self).__post_init__()
        _validate_ids(self.net_ids, "route net ids")
        _validate_ids(self.removed_route_ids, "removed route ids")
        if type(self.segments) is not tuple or any(not isinstance(item, RouteSegment) for item in self.segments):
            raise TypeError("route segments must be a tuple of RouteSegment")
        if type(self.vias) is not tuple or any(not isinstance(item, Via) for item in self.vias):
            raise TypeError("route vias must be a tuple of Via")
        net_ids = set(self.net_ids)
        if any(item.net_id not in net_ids for item in self.segments + self.vias):
            raise ValueError("route net geometry must belong to a requested route net")
        new_ids = tuple(item.id for item in self.segments + self.vias)
        if len(new_ids) != len(set(new_ids)) or set(new_ids) & set(self.removed_route_ids):
            raise ValueError("route identifiers collide")


@dataclass(frozen=True, slots=True, kw_only=True)
class CreateCopperZones(BoardOperation):
    zone_ids: tuple[BoardObjectId, ...] = ()
    zones: tuple[CopperZone, ...] = ()
    thermal_policies: tuple[ThermalPolicy, ...] = ()
    operation_type: ClassVar[str] = "board.create_copper_zones"

    def __post_init__(self) -> None:
        super(CreateCopperZones, self).__post_init__()
        _validate_ids(self.zone_ids, "copper zone ids")
        if type(self.zones) is not tuple or any(not isinstance(item, CopperZone) for item in self.zones):
            raise TypeError("copper zones must be a tuple of CopperZone")
        if self.zones and tuple(item.id for item in self.zones) != self.zone_ids:
            raise ValueError("copper zone ids must match zone geometry")
        if type(self.thermal_policies) is not tuple or any(not isinstance(item, ThermalPolicy) for item in self.thermal_policies):
            raise TypeError("thermal policies must be a tuple of ThermalPolicy")
        _validate_ids(tuple(item.pad_id for item in self.thermal_policies), "thermal policy pad ids")


@dataclass(frozen=True, slots=True, kw_only=True)
class AddGroundStitching(BoardOperation):
    via_ids: tuple[BoardObjectId, ...] = ()
    vias: tuple[Via, ...] = ()
    operation_type: ClassVar[str] = "board.add_ground_stitching"

    def __post_init__(self) -> None:
        super(AddGroundStitching, self).__post_init__()
        _validate_ids(self.via_ids, "ground stitching via ids")
        if type(self.vias) is not tuple or any(not isinstance(item, Via) for item in self.vias):
            raise TypeError("ground stitching vias must be a tuple of Via")
        if self.vias and tuple(item.id for item in self.vias) != self.via_ids:
            raise ValueError("ground stitching via ids must match via geometry")
        if any(item.net_id != BoardObjectId("GND") for item in self.vias):
            raise ValueError("ground stitching vias must belong to GND")


@dataclass(frozen=True, slots=True, kw_only=True)
class LockBoardObjects(BoardOperation):
    placement_ids: tuple[BoardObjectId, ...] = ()
    route_ids: tuple[BoardObjectId, ...] = ()
    operation_type: ClassVar[str] = "board.lock_board_objects"

    def __post_init__(self) -> None:
        super(LockBoardObjects, self).__post_init__()
        _validate_ids(self.placement_ids, "placement lock ids")
        _validate_ids(self.route_ids, "route lock ids")


def _validate_ids(values: tuple[BoardObjectId, ...], context: str) -> None:
    if type(values) is not tuple or any(
        not isinstance(item, BoardObjectId) for item in values
    ):
        raise TypeError(f"{context} must be a tuple of BoardObjectId")
    if len(values) != len(set(values)):
        raise ValueError(f"{context} must be unique")


__all__ = [
    "AddGroundStitching",
    "BoardOperation",
    "CreateCopperZones",
    "FootprintPlacement",
    "LockBoardObjects",
    "PlaceFootprints",
    "RouteNets",
    "ThermalPolicy",
]


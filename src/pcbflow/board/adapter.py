from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from pcbflow.domain import ValidationReport
from pcbflow.eda import EdaCapability

from .ir import BoardSnapshot
from .operations import BoardOperation
from .rulepack import ManufacturingRulePack


@dataclass(frozen=True, slots=True)
class CandidateWorkspace:
    path: Path
    output_dir: Path


@dataclass(frozen=True, slots=True)
class BoardSemanticDiff:
    changed_object_ids: tuple[str, ...]
    unexpected_object_ids: tuple[str, ...]

    @property
    def is_empty(self) -> bool:
        return not self.changed_object_ids and not self.unexpected_object_ids


@dataclass(frozen=True, slots=True)
class ReleaseArtifacts:
    files: tuple[tuple[str, Path], ...]


class BoardSemanticMismatchError(ValueError):
    def __init__(self, message: str, diff: BoardSemanticDiff | None = None) -> None:
        super().__init__(message)
        self.diff = diff


class UnsupportedEdaOperationError(ValueError):
    pass


class PcbEdaAdapter(Protocol):
    def probe(self) -> EdaCapability: ...
    def load_snapshot(self, project_dir: Path) -> BoardSnapshot: ...
    def create_candidate(self, source_dir: Path, destination_dir: Path) -> CandidateWorkspace: ...
    def apply_operations(
        self,
        candidate: CandidateWorkspace,
        operations: tuple[BoardOperation, ...],
        expected_snapshot: BoardSnapshot,
    ) -> BoardSnapshot: ...
    def run_drc(self, candidate: CandidateWorkspace) -> tuple[ValidationReport, ...]: ...
    def export_release(self, candidate: CandidateWorkspace, rulepack: ManufacturingRulePack) -> ReleaseArtifacts: ...


def semantic_diff(before: BoardSnapshot, after: BoardSnapshot) -> BoardSemanticDiff:
    """Compare every native object, including opaque nodes, by stable native id."""
    before_objects = _objects(before)
    after_objects = _objects(after)
    changed = tuple(sorted(str(object_id) for object_id in before_objects.keys() & after_objects.keys() if before_objects[object_id] != after_objects[object_id]))
    unexpected = tuple(sorted(str(object_id) for object_id in before_objects.keys() ^ after_objects.keys()))
    before_root = before.to_canonical_dict()
    after_root = after.to_canonical_dict()
    collections = {
        "net_classes", "nets", "keepouts", "footprints", "pads", "routes",
        "vias", "copper_zones", "opaque_nodes",
    }
    if any(before_root[key] != after_root[key] for key in before_root.keys() - collections):
        unexpected = tuple(sorted(set(unexpected) | {"<board>"}))
    return BoardSemanticDiff(changed, unexpected)


def _objects(snapshot: BoardSnapshot) -> dict[str, object]:
    result: dict[str, object] = {}
    for name in (
        "net_classes",
        "nets",
        "keepouts",
        "footprints",
        "pads",
        "routes",
        "vias",
        "copper_zones",
        "opaque_nodes",
    ):
        for item in getattr(snapshot, name):
            result[str(item.id)] = _canonical_item(snapshot, name, item.id)
    return result


def _canonical_item(snapshot: BoardSnapshot, collection: str, object_id: str) -> object:
    values = cast("list[dict[str, object]]", snapshot.to_canonical_dict()[collection])
    return next(item for item in values if item["id"] == object_id)


def object_locks(snapshot: BoardSnapshot) -> dict[str, bool]:
    result: dict[str, bool] = {}
    for item in snapshot.footprints:
        result[str(item.id)] = item.placement_lock
    for collection in (snapshot.routes, snapshot.vias, snapshot.copper_zones):
        for route_item in collection:
            result[str(route_item.id)] = route_item.route_lock
    return result


def all_opaque(snapshot: BoardSnapshot) -> dict[str, object]:
    return {str(item.id): (item.native_type, item.payload) for item in snapshot.opaque_nodes}


__all__ = [
    "BoardSemanticDiff",
    "BoardSemanticMismatchError",
    "CandidateWorkspace",
    "PcbEdaAdapter",
    "ReleaseArtifacts",
    "UnsupportedEdaOperationError",
    "all_opaque",
    "object_locks",
    "semantic_diff",
]

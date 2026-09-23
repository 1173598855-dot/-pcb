from __future__ import annotations

import io
import json
import re
from collections.abc import Sequence
from contextlib import nullcontext
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from sqlalchemy import exists, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from pcbflow.artifacts import ArtifactDescriptor, ContentAddressedStore, StagedArtifact
from pcbflow.board.adapter import (
    BoardSemanticMismatchError,
    PcbEdaAdapter,
    semantic_diff,
)
from pcbflow.board.ir import (
    BoardObjectId,
    BoardSnapshot,
    CopperZone,
    PointUm,
    RectUm,
    RouteSegment,
    Via,
)
from pcbflow.board.operations import (
    AddGroundStitching,
    BoardOperation,
    CreateCopperZones,
    FootprintPlacement,
    LockBoardObjects,
    PlaceFootprints,
    RouteNets,
    ThermalPolicy,
)
from pcbflow.board.rulepack import ManufacturingRulePack
from pcbflow.board.validation import BoardRuleChecker
from pcbflow.cancellation import TaskCancelledError
from pcbflow.canonical import canonical_digest, canonical_json_bytes, sha256_digest
from pcbflow.design_tables import PcbCandidateRow
from pcbflow.domain import (
    EdaKind,
    EdaOperation,
    NormalizedFinding,
    ProjectMode,
    RequestInvalidError,
    TaskLease,
    TaskStatus,
    ValidationReport,
    new_id,
    utc_now,
)
from pcbflow.eda import validate_idempotency_key
from pcbflow.lceda_pro import LcedaProCapabilityError
from pcbflow.pcb_workflow import CAPABILITY_EVIDENCE_KIND
from pcbflow.repositories import (
    EvidenceRepository,
    FindingRepository,
    IdempotencyConflictError,
    ProjectNotFoundError,
    ProjectRepository,
    StaleLeaseError,
    TaskRepository,
)
from pcbflow.tables import ArtifactRow, ProjectRow, TaskRow
from pcbflow.tasks import TerminalTaskError

PCB_GENERATE_CANDIDATE_TASK_KIND = "pcb.generate_candidate"
PCB_EXPORT_RELEASE_TASK_KIND = "pcb.export_release"
_SHA256_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")

G3_REQUIRED_EVIDENCE = frozenset(
    {
        "pcb_input_snapshot",
        "eda_capability",
        "rulepack",
        "placement_evidence",
        "routing_evidence",
        "copper_evidence",
        "boardir_validation",
        "native_drc",
        "board_semantic_diff",
        "candidate_summary",
    }
)
G3_REQUIRED_EVIDENCE_MEDIA_TYPES = {
    "pcb_input_snapshot": "application/vnd.pcbflow.boardir+json",
    "eda_capability": "application/vnd.pcbflow.eda-capability+json",
    "rulepack": "application/vnd.pcbflow.rulepack+json",
    "placement_evidence": "application/vnd.pcbflow.pcb-placement-evidence+json",
    "routing_evidence": "application/vnd.pcbflow.pcb-routing-evidence+json",
    "copper_evidence": "application/vnd.pcbflow.pcb-copper-evidence+json",
    "boardir_validation": "application/vnd.pcbflow.pcb-boardir-validation+json",
    "native_drc": "application/vnd.pcbflow.pcb-native-drc+json",
    "board_semantic_diff": "application/vnd.pcbflow.pcb-semantic-diff+json",
    "candidate_summary": "application/vnd.pcbflow.pcb-candidate-summary+json",
}
G3_EVIDENCE_SET_KIND = "pcb_candidate_evidence_set"
G3_EVIDENCE_SET_MEDIA_TYPE = (
    "application/vnd.pcbflow.pcb-candidate-evidence-set+json"
)

def validate_candidate_digest(
    value: str | None, *, field: str, allow_none: bool = False
) -> None:
    if value is None and allow_none:
        return
    if not isinstance(value, str) or _SHA256_DIGEST.fullmatch(value) is None:
        raise RequestInvalidError(f"{field} must be a sha256 digest")


def pcb_candidate_review_digest(
    *,
    candidate_id: str,
    project_id: str,
    base_revision: str,
    base_snapshot_digest: str | None,
    board_snapshot_digest: str,
    candidate_board_snapshot_digest: str,
    rulepack_digest: str,
    capability_digest: str,
    authority_digest: str,
    operations_digest: str,
    evidence_set_digest: str,
) -> str:
    """Derive the immutable review subject from the frozen candidate inputs."""
    return canonical_digest(
        {
            "schema_version": "1.0",
            "candidate_id": candidate_id,
            "project_id": project_id,
            "base_revision": base_revision,
            "base_snapshot_digest": base_snapshot_digest,
            "board_snapshot_digest": board_snapshot_digest,
            "candidate_board_snapshot_digest": candidate_board_snapshot_digest,
            "rulepack_digest": rulepack_digest,
            "capability_digest": capability_digest,
            "authority_digest": authority_digest,
            "operations_digest": operations_digest,
            "evidence_set_digest": evidence_set_digest,
        }
    )


def validate_candidate_public_inputs(
    *,
    seed: int,
    net_ids: Sequence[str] | None,
    board_snapshot_digest: str | None,
    capability_digest: str | None,
) -> None:
    """Keep CLI and service-side candidate input rules identical."""
    if type(seed) is not int or seed < 0:
        raise RequestInvalidError("seed must be a non-negative integer")
    if net_ids is not None and (
        isinstance(net_ids, (str, bytes)) or not isinstance(net_ids, Sequence)
    ):
        raise RequestInvalidError("net_ids must be a sequence of strings")
    values = list(net_ids or ())
    if len(values) > 256:
        raise RequestInvalidError("net_ids must contain at most 256 values")
    if any(
        not isinstance(value, str) or not value or value != value.strip()
        for value in values
    ):
        raise RequestInvalidError("net_ids must contain nonblank trimmed strings")
    if len(values) != len(set(values)):
        raise RequestInvalidError("net_ids must be unique")
    validate_candidate_digest(
        board_snapshot_digest, field="board_snapshot_digest", allow_none=True
    )
    validate_candidate_digest(
        capability_digest, field="capability_digest", allow_none=True
    )


def _task_idempotency_key(project_id: str, candidate_key: str) -> str:
    """Keep the internal global Task key distinct from the public candidate key."""
    return "pcb.generate_candidate:" + _digest(
        {"project_id": project_id, "candidate_key": candidate_key}
    )


def _release_task_idempotency_key(candidate_id: str, release_key: str) -> str:
    return "pcb.export_release:" + _digest(
        {"candidate_id": candidate_id, "release_key": release_key}
    )


class PcbCandidateStatus(StrEnum):
    QUEUED = "queued"
    EXECUTING = "executing"
    READY_FOR_G3 = "ready_for_g3"
    G3_APPROVED = "g3_approved"
    RELEASE_PENDING = "release_pending"
    READY_FOR_G4 = "ready_for_g4"
    RELEASED = "released"
    VALIDATION_FAILED = "validation_failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class PcbCandidateNotReviewableError(RequestInvalidError):
    def __init__(self, code: str = "PCB_CANDIDATE_NOT_REVIEWABLE") -> None:
        super().__init__(code)
        self.code = code


class PcbCandidateNotFoundError(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class PcbCandidate:
    id: str
    project_id: str
    task_id: str
    idempotency_key: str
    base_revision: str
    base_snapshot_digest: str | None
    board_snapshot_digest: str
    rulepack_digest: str
    capability_digest: str
    authority_digest: str
    operations: tuple[dict[str, Any], ...]
    operations_digest: str
    algorithm_evidence: dict[str, Any] | None
    status: PcbCandidateStatus
    result: dict[str, Any] | None
    last_error_code: str | None
    accepted_revision: str | None
    created_at: datetime
    updated_at: datetime
    version: int
    output_kind: str = "boardir_only"

    @property
    def evidence_kinds(self) -> frozenset[str]:
        if not isinstance(self.result, dict):
            return frozenset()
        values = self.result.get("evidence_kinds")
        if not isinstance(values, list) or any(type(item) is not str for item in values):
            return frozenset()
        return frozenset(values)

    @property
    def blocking_finding_count(self) -> int:
        if not isinstance(self.result, dict):
            return -1
        value = self.result.get("blocking_finding_count")
        return value if type(value) is int else -1

    @property
    def unconnected_net_count(self) -> int:
        if not isinstance(self.result, dict):
            return -1
        value = self.result.get("unconnected_net_count")
        return value if type(value) is int else -1


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _canonical(value: Any) -> Any:
    if hasattr(value, "value") and not isinstance(value, (str, bytes)):
        return value.value
    if hasattr(value, "__dataclass_fields__"):
        return {k: _canonical(v) for k, v in asdict(value).items()}
    if isinstance(value, dict):
        return {str(k): _canonical(v) for k, v in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (tuple, list)):
        return [_canonical(v) for v in value]
    return value


def _digest(value: Any) -> str:
    return canonical_digest(_canonical(value))


def _operation_payload(operation: BoardOperation) -> dict[str, Any]:
    """Persist the concrete operation type with its canonical dataclass fields."""
    value = _canonical(operation)
    if type(value) is not dict:
        raise TypeError("board operation did not produce an object payload")
    return {"operation_type": operation.operation_type, **value}


def deserialize_board_operations(
    values: Sequence[dict[str, Any]],
) -> tuple[BoardOperation, ...]:
    """Reconstruct the only typed BoardOperation forms accepted by V1."""
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise ValueError("persisted board operations must be a sequence")
    return tuple(_operation_from_payload(item) for item in values)


def _operation_from_payload(value: object) -> BoardOperation:
    raw = _mapping(value, "persisted board operation")
    operation_type = _string(raw.get("operation_type"), "operation type")
    common = _operation_common(raw)
    if operation_type == PlaceFootprints.operation_type:
        _expect_keys(raw, set(common) | {"operation_type", "placements"}, operation_type)
        return PlaceFootprints(
            **common,
            placements=tuple(
                _placement(item) for item in _list(raw["placements"], "placements")
            ),
        )
    if operation_type == RouteNets.operation_type:
        _expect_keys(
            raw,
            set(common)
            | {"operation_type", "net_ids", "segments", "vias", "removed_route_ids"},
            operation_type,
        )
        return RouteNets(
            **common,
            net_ids=_ids(raw["net_ids"], "route net ids"),
            segments=tuple(_route(item) for item in _list(raw["segments"], "segments")),
            vias=tuple(_via(item) for item in _list(raw["vias"], "vias")),
            removed_route_ids=_ids(raw["removed_route_ids"], "removed route ids"),
        )
    if operation_type == CreateCopperZones.operation_type:
        _expect_keys(
            raw,
            set(common) | {"operation_type", "zone_ids", "zones", "thermal_policies"},
            operation_type,
        )
        return CreateCopperZones(
            **common,
            zone_ids=_ids(raw["zone_ids"], "copper zone ids"),
            zones=tuple(_zone(item) for item in _list(raw["zones"], "copper zones")),
            thermal_policies=tuple(
                _thermal_policy(item)
                for item in _list(raw["thermal_policies"], "thermal policies")
            ),
        )
    if operation_type == AddGroundStitching.operation_type:
        _expect_keys(raw, set(common) | {"operation_type", "via_ids", "vias"}, operation_type)
        return AddGroundStitching(
            **common,
            via_ids=_ids(raw["via_ids"], "ground stitching via ids"),
            vias=tuple(_via(item) for item in _list(raw["vias"], "ground stitching vias")),
        )
    if operation_type == LockBoardObjects.operation_type:
        _expect_keys(
            raw,
            set(common) | {"operation_type", "placement_ids", "route_ids"},
            operation_type,
        )
        return LockBoardObjects(
            **common,
            placement_ids=_ids(raw["placement_ids"], "placement lock ids"),
            route_ids=_ids(raw["route_ids"], "route lock ids"),
        )
    raise ValueError(f"unsupported persisted board operation: {operation_type}")


def _operation_common(raw: dict[str, object]) -> dict[str, Any]:
    return {
        "project_id": _string(raw.get("project_id"), "operation project id"),
        "baseline_revision": _string(
            raw.get("baseline_revision"), "operation baseline revision"
        ),
        "risk": _string(raw.get("risk"), "operation risk"),
        "rulepack_digest": _string(
            raw.get("rulepack_digest"), "operation rulepack digest"
        ),
        "target_object_ids": _ids(
            raw.get("target_object_ids"), "operation target object ids"
        ),
        "idempotency_key": _string(
            raw.get("idempotency_key"), "operation idempotency key"
        ),
        "expected_snapshot_digest": _string(
            raw.get("expected_snapshot_digest"), "operation expected snapshot digest"
        ),
    }


def _placement(value: object) -> FootprintPlacement:
    raw = _mapping(value, "footprint placement")
    _expect_keys(raw, {"footprint_id", "position", "layer"}, "footprint placement")
    return FootprintPlacement(
        BoardObjectId(_string(raw["footprint_id"], "footprint id")),
        _point(raw["position"], "footprint position"),
        _string(raw["layer"], "footprint layer"),
    )


def _route(value: object) -> RouteSegment:
    raw = _mapping(value, "route segment")
    _expect_keys(
        raw,
        {"id", "net_id", "start", "end", "width_um", "layer", "route_lock"},
        "route segment",
    )
    return RouteSegment(
        BoardObjectId(_string(raw["id"], "route id")),
        BoardObjectId(_string(raw["net_id"], "route net id")),
        _point(raw["start"], "route start"),
        _point(raw["end"], "route end"),
        _integer(raw["width_um"], "route width"),
        _string(raw["layer"], "route layer"),
        _boolean(raw["route_lock"], "route lock"),
    )


def _via(value: object) -> Via:
    raw = _mapping(value, "via")
    _expect_keys(
        raw,
        {"id", "net_id", "position", "diameter_um", "hole_diameter_um", "layers", "route_lock"},
        "via",
    )
    return Via(
        BoardObjectId(_string(raw["id"], "via id")),
        BoardObjectId(_string(raw["net_id"], "via net id")),
        _point(raw["position"], "via position"),
        _integer(raw["diameter_um"], "via diameter"),
        _integer(raw["hole_diameter_um"], "via hole diameter"),
        _strings(raw["layers"], "via layers"),
        _boolean(raw["route_lock"], "via route lock"),
    )


def _zone(value: object) -> CopperZone:
    raw = _mapping(value, "copper zone")
    _expect_keys(
        raw,
        {"id", "net_id", "layer", "bounds", "clearance_um", "route_lock"},
        "copper zone",
    )
    return CopperZone(
        BoardObjectId(_string(raw["id"], "copper zone id")),
        BoardObjectId(_string(raw["net_id"], "copper zone net id")),
        _string(raw["layer"], "copper zone layer"),
        _rect(raw["bounds"], "copper zone bounds"),
        _integer(raw["clearance_um"], "copper zone clearance"),
        _boolean(raw["route_lock"], "copper zone route lock"),
    )


def _thermal_policy(value: object) -> ThermalPolicy:
    raw = _mapping(value, "thermal policy")
    _expect_keys(
        raw,
        {
            "pad_id",
            "net_id",
            "layers",
            "style",
            "spoke_count",
            "spoke_width_um",
            "gap_um",
            "locked",
        },
        "thermal policy",
    )
    return ThermalPolicy(
        pad_id=BoardObjectId(_string(raw["pad_id"], "thermal pad id")),
        net_id=BoardObjectId(_string(raw["net_id"], "thermal net id")),
        layers=_strings(raw["layers"], "thermal layers"),
        style=_string(raw["style"], "thermal style"),
        spoke_count=_integer(raw["spoke_count"], "thermal spoke count"),
        spoke_width_um=_integer(raw["spoke_width_um"], "thermal spoke width"),
        gap_um=_integer(raw["gap_um"], "thermal gap"),
        locked=_boolean(raw["locked"], "thermal lock"),
    )


def _point(value: object, context: str) -> PointUm:
    raw = _mapping(value, context)
    _expect_keys(raw, {"x", "y"}, context)
    return PointUm(_integer(raw["x"], f"{context} x"), _integer(raw["y"], f"{context} y"))


def _rect(value: object, context: str) -> RectUm:
    raw = _mapping(value, context)
    _expect_keys(raw, {"x", "y", "width", "height"}, context)
    return RectUm(
        _integer(raw["x"], f"{context} x"),
        _integer(raw["y"], f"{context} y"),
        _integer(raw["width"], f"{context} width"),
        _integer(raw["height"], f"{context} height"),
    )


def _ids(value: object, context: str) -> tuple[BoardObjectId, ...]:
    return tuple(BoardObjectId(_string(item, context)) for item in _list(value, context))


def _strings(value: object, context: str) -> tuple[str, ...]:
    return tuple(_string(item, context) for item in _list(value, context))


def _mapping(value: object, context: str) -> dict[str, object]:
    if type(value) is not dict or any(type(key) is not str for key in value):
        raise ValueError(f"{context} must be an object")
    return value


def _list(value: object, context: str) -> list[object]:
    if type(value) is not list:
        raise ValueError(f"{context} must be an array")
    return value


def _string(value: object, context: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{context} must be a non-empty canonical string")
    return value


def _integer(value: object, context: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{context} must be an integer")
    return value


def _boolean(value: object, context: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{context} must be a boolean")
    return value


def _expect_keys(raw: dict[str, object], expected: set[str], context: str) -> None:
    actual = set(raw)
    if actual != expected:
        raise ValueError(
            f"{context} has invalid fields: "
            + ", ".join(sorted(actual ^ expected))
        )


def _candidate_output_kind(
    status: PcbCandidateStatus,
    _algorithm_evidence: dict[str, Any] | None,
    result: dict[str, Any] | None,
) -> str:
    if (
        status in {PcbCandidateStatus.READY_FOR_G4, PcbCandidateStatus.RELEASED}
        and isinstance(result, dict)
        and isinstance(result.get("release"), dict)
        and isinstance(result["release"].get("manifest_digest"), str)
    ):
        return "release_candidate"
    if (
        status
        in {
            PcbCandidateStatus.READY_FOR_G3,
            PcbCandidateStatus.G3_APPROVED,
            PcbCandidateStatus.RELEASE_PENDING,
            PcbCandidateStatus.READY_FOR_G4,
            PcbCandidateStatus.RELEASED,
        }
        and isinstance(result, dict)
        and result.get("native_candidate_verified") is True
    ):
        return "native_candidate"
    return "boardir_only"


def _public_algorithm_evidence(
    algorithm_evidence: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(algorithm_evidence, dict):
        return algorithm_evidence
    return {**algorithm_evidence, "output_kind": "boardir_only"}


def _candidate(row: PcbCandidateRow) -> PcbCandidate:
    status = PcbCandidateStatus(row.status)
    return PcbCandidate(
        id=row.id, project_id=row.project_id, task_id=row.task_id, idempotency_key=row.idempotency_key,
        base_revision=row.base_revision, base_snapshot_digest=row.base_snapshot_digest,
        board_snapshot_digest=row.board_snapshot_digest, rulepack_digest=row.rulepack_digest,
        capability_digest=row.capability_digest, authority_digest=row.authority_digest,
        operations=tuple(row.operations_json), operations_digest=row.operations_digest,
        algorithm_evidence=_public_algorithm_evidence(row.algorithm_evidence_json), status=status,
        result=row.result_json, last_error_code=row.last_error_code, accepted_revision=row.accepted_revision,
        created_at=_utc(row.created_at), updated_at=_utc(row.updated_at), version=row.version,
        output_kind=_candidate_output_kind(status, row.algorithm_evidence_json, row.result_json),
    )


class PcbCandidateStore:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        tasks: TaskRepository,
        authorities: Any,
        projects: Any,
        capability_gate: Any | None = None,
        evidence: EvidenceRepository | None = None,
        revisions: Any | None = None,
    ) -> None:
        self._sessions = sessions
        self._tasks = tasks
        self._authorities = authorities
        self._projects = projects
        self._capability_gate = capability_gate
        self._evidence = evidence
        self._revisions = revisions

    @staticmethod
    def _replay_matches(
        row: PcbCandidateRow,
        *,
        project_id: str,
        base_revision: str,
        base_snapshot_digest: str | None,
        board_snapshot_digest: str,
        rulepack_digest: str,
        capability_digest: str,
        authority_digest: str,
        operation_json: tuple[dict[str, Any], ...],
        operations_digest: str,
        algorithm_evidence: dict[str, Any] | None,
        idempotency_key: str,
    ) -> bool:
        """Compare every value frozen into a candidate creation request."""
        return (
            row.project_id == project_id
            and row.idempotency_key == idempotency_key
            and row.base_revision == base_revision
            and row.base_snapshot_digest == base_snapshot_digest
            and row.board_snapshot_digest == board_snapshot_digest
            and row.rulepack_digest == rulepack_digest
            and row.capability_digest == capability_digest
            and row.authority_digest == authority_digest
            and row.operations_json == list(operation_json)
            and row.operations_digest == operations_digest
            and row.algorithm_evidence_json == algorithm_evidence
        )

    def _find_existing_row(
        self, project_id: str, idempotency_key: str
    ) -> PcbCandidateRow | None:
        with self._sessions() as session:
            return session.scalar(
                select(PcbCandidateRow).where(
                    PcbCandidateRow.project_id == project_id,
                    PcbCandidateRow.idempotency_key == idempotency_key,
                )
            )

    def create_from_public_inputs(
        self,
        *,
        project_id: str,
        seed: int,
        net_ids: Sequence[str] | None,
        board_snapshot_digest: str | None,
        capability_digest: str | None,
        idempotency_key: str,
    ) -> PcbCandidate:
        """Resolve the frozen public inputs shared by the REST and CLI transports."""
        validate_candidate_public_inputs(
            seed=seed,
            net_ids=net_ids,
            board_snapshot_digest=board_snapshot_digest,
            capability_digest=capability_digest,
        )
        project = self._projects.get(project_id)
        existing = self.find_by_idempotency_key(project_id, idempotency_key)
        if project.current_revision is None and existing is None:
            raise RequestInvalidError("PCB_CANDIDATE_STALE")
        authority = self._authorities.find_by_project_id(project_id)
        if authority is None or authority.eda_kind is not EdaKind.LCEDA_PRO:
            raise RequestInvalidError("PCB_CAPABILITY_GATE_BLOCKED")
        base_revision = (
            existing.base_revision if existing is not None else project.current_revision
        )
        base_snapshot_digest = (
            existing.base_snapshot_digest
            if existing is not None
            else self._source_snapshot_digest(project.source_path)
        )
        resolved_board_digest = board_snapshot_digest or (
            existing.board_snapshot_digest
            if existing is not None
            else project.project_snapshot_digest
        )
        if resolved_board_digest is None:
            raise RequestInvalidError("PCB_CAPABILITY_GATE_BLOCKED")
        resolved_capability_digest = capability_digest
        if resolved_capability_digest is None:
            if existing is not None:
                resolved_capability_digest = existing.capability_digest
            elif self._evidence is not None:
                records = self._evidence.list_for_project(project_id)
                capability = next(
                    (
                        item
                        for item in reversed(records)
                        if item.kind == CAPABILITY_EVIDENCE_KIND
                        and item.verdict == "pass"
                    ),
                    None,
                )
                resolved_capability_digest = (
                    capability.artifact_digest if capability is not None else None
                )
        if resolved_capability_digest is None:
            raise RequestInvalidError("PCB_CAPABILITY_GATE_BLOCKED")
        return self.create(
            project_id=project_id,
            base_revision=base_revision or "",
            base_snapshot_digest=base_snapshot_digest,
            board_snapshot_digest=resolved_board_digest,
            rulepack_digest=authority.rulepack_digest,
            capability_digest=resolved_capability_digest,
            operations=(),
            algorithm_evidence={
                "algorithm_version": "boardir-only-v1",
                "seed": seed,
                "net_ids": list(net_ids or ()),
                "output_kind": "boardir_only",
            },
            idempotency_key=idempotency_key,
        )

    def _source_snapshot_digest(self, source_path: Path) -> str:
        if self._revisions is None:
            raise RequestInvalidError("PCB_CANDIDATE_SOURCE_UNAVAILABLE")
        return self._revisions.snapshot_digest(source_path)

    def create(
        self,
        *,
        project_id: str,
        base_revision: str,
        board_snapshot_digest: str,
        rulepack_digest: str,
        capability_digest: str,
        idempotency_key: str,
        base_snapshot_digest: str | None = None,
        operations: tuple[BoardOperation, ...] = (),
        algorithm_evidence: dict[str, Any] | None = None,
        require_capability: bool = True,
    ) -> PcbCandidate:
        validate_idempotency_key(idempotency_key)
        validate_candidate_digest(
            base_snapshot_digest, field="base_snapshot_digest", allow_none=True
        )
        validate_candidate_digest(
            board_snapshot_digest, field="board_snapshot_digest"
        )
        validate_candidate_digest(rulepack_digest, field="rulepack_digest")
        validate_candidate_digest(capability_digest, field="capability_digest")
        project = self._projects.get(project_id)
        authority = self._authorities.find_by_project_id(project_id)
        if authority is None or authority.eda_kind is not EdaKind.LCEDA_PRO:
            raise RequestInvalidError("project EDA authority is required")
        if type(operations) is not tuple or any(
            not isinstance(operation, BoardOperation) for operation in operations
        ):
            raise RequestInvalidError("operations must be a tuple of typed board operations")
        operation_json = tuple(_operation_payload(op) for op in operations)
        operations_digest = _digest(operation_json)
        canonical_algorithm_evidence = _canonical(algorithm_evidence)
        if isinstance(canonical_algorithm_evidence, dict):
            if "seed" in canonical_algorithm_evidence or "net_ids" in canonical_algorithm_evidence:
                validate_candidate_public_inputs(
                    seed=canonical_algorithm_evidence.get("seed", 0),
                    net_ids=canonical_algorithm_evidence.get("net_ids"),
                    board_snapshot_digest=board_snapshot_digest,
                    capability_digest=capability_digest,
                )
            output_kind = canonical_algorithm_evidence.get("output_kind")
            if output_kind not in {None, "boardir_only"}:
                raise RequestInvalidError(
                    "output_kind must be boardir_only until native evidence is verified"
                )
        authority_digest = authority.canonical_digest

        # Idempotent replays return the durable candidate before checking any
        # mutable capability state. The gate is a precondition for creating a
        # new candidate, not for reading an already-frozen result.
        existing = self._find_existing_row(project_id, idempotency_key)
        if existing is not None:
            if self._replay_matches(
                existing,
                project_id=project_id,
                base_revision=base_revision,
                base_snapshot_digest=base_snapshot_digest,
                board_snapshot_digest=board_snapshot_digest,
                rulepack_digest=rulepack_digest,
                capability_digest=capability_digest,
                authority_digest=authority_digest,
                operation_json=operation_json,
                operations_digest=operations_digest,
                algorithm_evidence=canonical_algorithm_evidence,
                idempotency_key=idempotency_key,
            ):
                return _candidate(existing)
            raise IdempotencyConflictError(idempotency_key)

        if project.current_revision is not None and project.current_revision != base_revision:
            raise RequestInvalidError("PCB_CANDIDATE_STALE")
        verified_capability_digest: str | None = None
        if require_capability:
            if self._capability_gate is None:
                raise LcedaProCapabilityError("LCEDA_PRO_WRITE_CAPABILITY_UNVERIFIED")
            verified_capability_digest = self._capability_gate.require_operation(
                project_id, EdaKind.LCEDA_PRO, EdaOperation.CREATE_CANDIDATE
            )
            if verified_capability_digest != capability_digest:
                raise LcedaProCapabilityError(
                    "LCEDA_PRO_WRITE_CAPABILITY_UNVERIFIED"
                )
        payload = {"project_id": project_id, "candidate_key": idempotency_key}
        now = utc_now()
        try:
            with self._sessions.begin() as session:
                # Serialize candidate creation with revision updates. The
                # earlier read is only a fast rejection; this is the source
                # of truth for the insert transaction.
                session.execute(text("BEGIN IMMEDIATE"))
                project_row = session.get(ProjectRow, project_id)
                if project_row is None:
                    raise ProjectNotFoundError(project_id)
                if (
                    project_row.current_revision is not None
                    and project_row.current_revision != base_revision
                ):
                    raise RequestInvalidError("PCB_CANDIDATE_STALE")
                existing = session.scalar(
                    select(PcbCandidateRow).where(
                        PcbCandidateRow.project_id == project_id,
                        PcbCandidateRow.idempotency_key == idempotency_key,
                    )
                )
                if existing is not None:
                    if self._replay_matches(
                        existing,
                        project_id=project_id,
                        base_revision=base_revision,
                        base_snapshot_digest=base_snapshot_digest,
                        board_snapshot_digest=board_snapshot_digest,
                        rulepack_digest=rulepack_digest,
                        capability_digest=capability_digest,
                        authority_digest=authority_digest,
                        operation_json=operation_json,
                        operations_digest=operations_digest,
                        algorithm_evidence=canonical_algorithm_evidence,
                        idempotency_key=idempotency_key,
                    ):
                        return _candidate(existing)
                    raise IdempotencyConflictError(idempotency_key)
                # 候选和内部任务共享该事务，候选失败不会遗留可领取的孤立任务。
                task = self._tasks.enqueue_in_session(
                    session,
                    PCB_GENERATE_CANDIDATE_TASK_KIND,
                    payload,
                    _task_idempotency_key(project_id, idempotency_key),
                    project_id,
                )
                row = PcbCandidateRow(id=new_id("pcb"), project_id=project_id, task_id=task.id, idempotency_key=idempotency_key,
                    base_revision=base_revision, base_snapshot_digest=base_snapshot_digest, board_snapshot_digest=board_snapshot_digest,
                    rulepack_digest=rulepack_digest, capability_digest=capability_digest, authority_digest=authority_digest,
                    operations_json=list(operation_json), operations_digest=operations_digest, algorithm_evidence_json=canonical_algorithm_evidence,
                    status=PcbCandidateStatus.QUEUED.value, result_json=None, last_error_code=None, accepted_revision=None,
                    created_at=now, updated_at=now, version=1)
                session.add(row)
                session.flush()
                return _candidate(row)
        except IntegrityError:
            # A concurrent request may have inserted the candidate after the
            # initial lookup. Re-apply the complete idempotency contract.
            with self._sessions() as session:
                existing = session.scalar(
                    select(PcbCandidateRow).where(
                        PcbCandidateRow.project_id == project_id,
                        PcbCandidateRow.idempotency_key == idempotency_key,
                    )
                )
                if existing is None or not self._replay_matches(
                    existing,
                    project_id=project_id,
                    base_revision=base_revision,
                    base_snapshot_digest=base_snapshot_digest,
                    board_snapshot_digest=board_snapshot_digest,
                    rulepack_digest=rulepack_digest,
                    capability_digest=capability_digest,
                    authority_digest=authority_digest,
                    operation_json=operation_json,
                    operations_digest=operations_digest,
                    algorithm_evidence=canonical_algorithm_evidence,
                    idempotency_key=idempotency_key,
                ):
                    raise IdempotencyConflictError(idempotency_key)
                return _candidate(existing)

    def get(self, candidate_id: str) -> PcbCandidate:
        with self._sessions() as session:
            row = session.get(PcbCandidateRow, candidate_id)
            if row is None: raise PcbCandidateNotFoundError(candidate_id)
            return _candidate(row)

    def find_by_idempotency_key(
        self, project_id: str, idempotency_key: str
    ) -> PcbCandidate | None:
        with self._sessions() as session:
            row = session.scalar(
                select(PcbCandidateRow).where(
                    PcbCandidateRow.project_id == project_id,
                    PcbCandidateRow.idempotency_key == idempotency_key,
                )
            )
            return _candidate(row) if row is not None else None

    def list_for_project(self, project_id: str) -> list[PcbCandidate]:
        with self._sessions() as session:
            return [_candidate(row) for row in session.scalars(select(PcbCandidateRow).where(PcbCandidateRow.project_id == project_id).order_by(PcbCandidateRow.created_at, PcbCandidateRow.id))]

    def _mirror_cancelled_task(
        self, candidate_id: str, task_id: str, now: datetime
    ) -> None:
        """将已持久取消的任务状态同步到尚未发布的候选。"""
        task = self._tasks.get(task_id)
        if getattr(task.status, "value", task.status) != "cancelled":
            return
        with self._sessions.begin() as session:
            row = session.get(PcbCandidateRow, candidate_id)
            if row is not None and row.status not in {
                PcbCandidateStatus.RELEASED.value,
                PcbCandidateStatus.CANCELLED.value,
            }:
                result = dict(row.result_json or {})
                release = result.get("release")
                if isinstance(release, dict) and release.get("task_id") == task_id:
                    result["release"] = {
                        "task_id": task_id,
                        "idempotency_key": release.get("idempotency_key"),
                        "status": "cancelled",
                        "error_code": "TASK_CANCELLED",
                    }
                    row.result_json = result
                row.status = PcbCandidateStatus.CANCELLED.value
                row.last_error_code = "TASK_CANCELLED"
                row.updated_at = now
                row.version += 1

    def _assert_active_or_mirror_cancellation(
        self, candidate_id: str, task_id: str, lease_token: str, now: datetime
    ) -> None:
        """在每个候选状态变更边界执行围栏，并同步竞态取消。"""
        try:
            self._tasks.assert_active(task_id, lease_token, now)
        except (StaleLeaseError, TaskCancelledError):
            self._mirror_cancelled_task(candidate_id, task_id, now)
            raise

    def _revert_stale_transition(
        self,
        candidate_id: str,
        task_id: str,
        written_status: PcbCandidateStatus,
        written_version: int,
        previous_status: str,
        previous_result: dict[str, Any] | None,
        previous_error_code: str | None,
        now: datetime,
    ) -> None:
        """仅在状态尚未被新租约推进时，撤销失效 Worker 的状态写入。"""
        with self._sessions.begin() as session:
            session.execute(
                update(PcbCandidateRow)
                .where(
                    PcbCandidateRow.id == candidate_id,
                    PcbCandidateRow.task_id == task_id,
                    PcbCandidateRow.status == written_status.value,
                    PcbCandidateRow.version == written_version,
                )
                .values(
                    status=previous_status,
                    result_json=previous_result,
                    last_error_code=previous_error_code,
                    updated_at=now,
                    version=written_version + 1,
                )
            )

    def _revert_stale_release_transition(
        self,
        candidate_id: str,
        task_id: str,
        written_version: int,
        previous_status: str,
        previous_result: dict[str, Any] | None,
        previous_error_code: str | None,
        now: datetime,
    ) -> None:
        with self._sessions.begin() as session:
            session.execute(
                update(PcbCandidateRow)
                .where(
                    PcbCandidateRow.id == candidate_id,
                    PcbCandidateRow.status == PcbCandidateStatus.READY_FOR_G4.value,
                    PcbCandidateRow.version == written_version,
                    PcbCandidateRow.result_json["release"]["task_id"].as_string()
                    == task_id,
                )
                .values(
                    status=previous_status,
                    result_json=previous_result,
                    last_error_code=previous_error_code,
                    updated_at=now,
                    version=written_version + 1,
                )
                .execution_options(synchronize_session=False)
            )

    def _transition(
        self,
        candidate_id: str,
        allowed: set[PcbCandidateStatus],
        status: PcbCandidateStatus,
        task_id: str,
        lease_token: str,
        now: datetime,
        *,
        result: dict[str, Any] | None = None,
        error_code: str | None = None,
    ) -> PcbCandidate:
        self._assert_active_or_mirror_cancellation(
            candidate_id, task_id, lease_token, now
        )
        transition_failed = False
        transition_now = now
        with self._sessions.begin() as session:
            # Re-check the clock only after acquiring the write lock so a
            # worker cannot publish a transition using a stale pre-lock time.
            session.execute(text("BEGIN IMMEDIATE"))
            transition_now = max(now, utc_now())
            row = session.get(PcbCandidateRow, candidate_id)
            if row is None:
                raise PcbCandidateNotFoundError(candidate_id)
            if row.task_id != task_id:
                raise RequestInvalidError("invalid PCB candidate transition")
            if row.status == PcbCandidateStatus.CANCELLED.value:
                raise TaskCancelledError(task_id)
            if PcbCandidateStatus(row.status) not in allowed:
                raise RequestInvalidError("invalid PCB candidate transition")
            if status is PcbCandidateStatus.READY_FOR_G3:
                self._validate_ready_result(row, result)
            previous_status = row.status
            previous_result = row.result_json
            previous_error_code = row.last_error_code
            written_version = row.version + 1
            active_task = exists().where(
                TaskRow.id == task_id,
                TaskRow.lease_token == lease_token,
                TaskRow.status == TaskStatus.RUNNING.value,
                TaskRow.lease_expires_at.is_not(None),
                TaskRow.lease_expires_at > transition_now,
            )
            changed = session.execute(
                update(PcbCandidateRow)
                .where(
                    PcbCandidateRow.id == candidate_id,
                    PcbCandidateRow.task_id == task_id,
                    PcbCandidateRow.status.in_(item.value for item in allowed),
                    PcbCandidateRow.version == row.version,
                    active_task,
                )
                .values(
                    status=status.value,
                    result_json=result,
                    last_error_code=error_code,
                    updated_at=transition_now,
                    version=row.version + 1,
                )
                .execution_options(synchronize_session=False)
            )
            transition_failed = changed.rowcount != 1
        if transition_failed:
            self._assert_active_or_mirror_cancellation(
                candidate_id, task_id, lease_token, transition_now
            )
            raise RequestInvalidError("concurrent PCB candidate update")
        try:
            self._assert_active_or_mirror_cancellation(
                candidate_id, task_id, lease_token, transition_now
            )
        except StaleLeaseError:
            self._revert_stale_transition(
                candidate_id,
                task_id,
                status,
                written_version,
                previous_status,
                previous_result,
                previous_error_code,
                transition_now,
            )
            raise
        return self.get(candidate_id)

    @staticmethod
    def _validate_ready_result(
        row: PcbCandidateRow, result: dict[str, Any] | None
    ) -> None:
        if not isinstance(result, dict):
            raise PcbCandidateNotReviewableError()
        if (
            result.get("operations_digest") != row.operations_digest
            or result.get("board_snapshot_digest") != row.board_snapshot_digest
        ):
            raise PcbCandidateNotReviewableError()
        try:
            candidate_digest = result.get("candidate_digest")
            validate_candidate_digest(candidate_digest, field="candidate_digest")
            post_snapshot_digest = result.get("candidate_board_snapshot_digest")
            validate_candidate_digest(
                post_snapshot_digest, field="candidate_board_snapshot_digest"
            )
        except RequestInvalidError as error:
            raise PcbCandidateNotReviewableError() from error
        for field in ("rulepack_digest", "capability_digest", "authority_digest"):
            if result.get(field) != getattr(row, field):
                raise PcbCandidateNotReviewableError()
        if result.get("native_candidate_verified") is not True:
            raise PcbCandidateNotReviewableError()
        evidence_kinds = result.get("evidence_kinds")
        if (
            type(evidence_kinds) is not list
            or any(type(item) is not str for item in evidence_kinds)
            or frozenset(evidence_kinds) != G3_REQUIRED_EVIDENCE
            or len(evidence_kinds) != len(G3_REQUIRED_EVIDENCE)
        ):
            raise PcbCandidateNotReviewableError(
                "PCB_CANDIDATE_EVIDENCE_INCOMPLETE"
            )
        for field in ("blocking_finding_count", "unconnected_net_count"):
            if type(result.get(field)) is not int or result[field] != 0:
                raise PcbCandidateNotReviewableError()
        try:
            evidence_set_digest = result.get("evidence_set_digest")
            validate_candidate_digest(evidence_set_digest, field="evidence_set_digest")
        except RequestInvalidError as error:
            raise PcbCandidateNotReviewableError() from error
        if candidate_digest != pcb_candidate_review_digest(
            candidate_id=row.id,
            project_id=row.project_id,
            base_revision=row.base_revision,
            base_snapshot_digest=row.base_snapshot_digest,
            board_snapshot_digest=row.board_snapshot_digest,
            candidate_board_snapshot_digest=post_snapshot_digest,
            rulepack_digest=row.rulepack_digest,
            capability_digest=row.capability_digest,
            authority_digest=row.authority_digest,
            operations_digest=row.operations_digest,
            evidence_set_digest=evidence_set_digest,
        ):
            raise PcbCandidateNotReviewableError()

    def mark_executing(self, candidate_id: str, task_id: str, lease_token: str, now: datetime) -> PcbCandidate:
        return self._transition(
            candidate_id,
            {PcbCandidateStatus.QUEUED, PcbCandidateStatus.EXECUTING},
            PcbCandidateStatus.EXECUTING,
            task_id,
            lease_token,
            now,
        )

    def mark_ready_for_g3(self, candidate_id: str, task_id: str, lease_token: str, now: datetime, *, result: dict[str, Any] | None = None) -> PcbCandidate:
        return self._transition(candidate_id, {PcbCandidateStatus.EXECUTING}, PcbCandidateStatus.READY_FOR_G3, task_id, lease_token, now, result=result)

    def mark_blocked(
        self,
        candidate_id: str,
        task_id: str,
        lease_token: str,
        now: datetime,
        *,
        result: dict[str, Any] | None = None,
        error_code: str = "PCB_CAPABILITY_GATE_BLOCKED",
    ) -> PcbCandidate:
        return self._transition(
            candidate_id,
            {PcbCandidateStatus.QUEUED, PcbCandidateStatus.EXECUTING},
            PcbCandidateStatus.BLOCKED,
            task_id,
            lease_token,
            now,
            result=result,
            error_code=error_code,
        )

    def mark_validation_failed(
        self,
        candidate_id: str,
        task_id: str,
        lease_token: str,
        now: datetime,
        error_code: str,
        *,
        result: dict[str, Any] | None = None,
    ) -> PcbCandidate:
        return self._transition(
            candidate_id,
            {PcbCandidateStatus.EXECUTING},
            PcbCandidateStatus.VALIDATION_FAILED,
            task_id,
            lease_token,
            now,
            result=result,
            error_code=error_code,
        )

    @staticmethod
    def _register_release_artifacts(
        session: Session,
        descriptors: Sequence[ArtifactDescriptor],
        now: datetime,
    ) -> None:
        for descriptor in descriptors:
            try:
                available = descriptor.path.is_file()
                actual_size = descriptor.path.stat().st_size
            except OSError as error:
                raise RequestInvalidError(
                    "PCB_RELEASE_ARTIFACT_UNAVAILABLE"
                ) from error
            if not available or actual_size != descriptor.size:
                raise RequestInvalidError("PCB_RELEASE_ARTIFACT_UNAVAILABLE")
            row = session.get(ArtifactRow, descriptor.digest)
            if row is None:
                session.add(
                    ArtifactRow(
                        digest=descriptor.digest,
                        size=descriptor.size,
                        media_type=descriptor.media_type,
                        storage_path=str(descriptor.path),
                        created_at=now,
                    )
                )
            elif (
                row.size != descriptor.size
                or row.media_type != descriptor.media_type
                or row.storage_path != str(descriptor.path)
            ):
                raise RequestInvalidError("PCB_RELEASE_ARTIFACT_CONFLICT")

    def settle_publication(
        self,
        descriptors: Sequence[ArtifactDescriptor],
        staged: Sequence[StagedArtifact],
    ) -> None:
        """Safely settle published release objects against durable registrations."""
        if len(descriptors) != len(staged):
            raise ValueError("release publication descriptor/staging mismatch")
        pairs = tuple(zip(descriptors, staged, strict=True))
        for descriptor, artifact in pairs:
            if (
                descriptor.digest != artifact.digest
                or descriptor.size != artifact.size
                or descriptor.media_type != artifact.media_type
            ):
                raise ValueError("release publication descriptor/staging mismatch")
        with self._sessions.begin() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            for descriptor, artifact in pairs:
                if session.get(ArtifactRow, descriptor.digest) is not None:
                    artifact.discard()
                else:
                    artifact.rollback()

    def settle_release_publication(
        self,
        descriptors: Sequence[ArtifactDescriptor],
        staged: Sequence[StagedArtifact],
    ) -> None:
        """Backward-compatible alias for release publication settlement."""
        self.settle_publication(descriptors, staged)

    def enqueue_release_export(self, candidate_id: str, idempotency_key: str):
        """Atomically enqueue a release task and reserve the G3-approved candidate."""
        validate_idempotency_key(idempotency_key)
        with self._sessions.begin() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row = session.get(PcbCandidateRow, candidate_id)
            if row is None:
                raise PcbCandidateNotFoundError(candidate_id)
            result = dict(row.result_json or {})
            release = result.get("release")
            if isinstance(release, dict):
                if (
                    row.status == PcbCandidateStatus.G3_APPROVED.value
                    and release.get("status") == "failed"
                ):
                    if release.get("idempotency_key") == idempotency_key:
                        raise RequestInvalidError("PCB_RELEASE_RETRY_KEY_REQUIRED")
                    release = None
                if release is None:
                    result.pop("release", None)
            if isinstance(release, dict):
                if release.get("idempotency_key") != idempotency_key:
                    raise RequestInvalidError("PCB_RELEASE_ALREADY_REQUESTED")
                task_id = release.get("task_id")
                if not isinstance(task_id, str):
                    raise PcbCandidateNotReviewableError()
                task = self._tasks.enqueue_in_session(
                    session,
                    PCB_EXPORT_RELEASE_TASK_KIND,
                    {"project_id": row.project_id, "candidate_id": row.id},
                    _release_task_idempotency_key(row.id, idempotency_key),
                    row.project_id,
                )
                if task.id != task_id:
                    raise PcbCandidateNotReviewableError()
                return task
            if row.status != PcbCandidateStatus.G3_APPROVED.value:
                raise PcbCandidateNotReviewableError()
            g3 = result.get("g3_decision")
            if (
                not isinstance(g3, dict)
                or g3.get("decision") != "approve"
                or not isinstance(result.get("candidate_digest"), str)
            ):
                raise PcbCandidateNotReviewableError()
            task = self._tasks.enqueue_in_session(
                session,
                PCB_EXPORT_RELEASE_TASK_KIND,
                {"project_id": row.project_id, "candidate_id": row.id},
                _release_task_idempotency_key(row.id, idempotency_key),
                row.project_id,
            )
            result["release"] = {
                "task_id": task.id,
                "idempotency_key": idempotency_key,
                "status": "pending",
            }
            row.status = PcbCandidateStatus.RELEASE_PENDING.value
            row.result_json = result
            row.last_error_code = None
            row.updated_at = utc_now()
            row.version += 1
            return task

    def mark_ready_for_g4(
        self,
        candidate_id: str,
        task_id: str,
        lease_token: str,
        now: datetime,
        *,
        release_result: dict[str, Any],
        descriptors: Sequence[ArtifactDescriptor],
    ) -> PcbCandidate:
        self._assert_active_or_mirror_cancellation(
            candidate_id, task_id, lease_token, now
        )
        transition_now = now
        previous_status = PcbCandidateStatus.RELEASE_PENDING.value
        previous_result: dict[str, Any] | None = None
        previous_error_code: str | None = None
        written_version = 0
        with self._sessions.begin() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            transition_now = max(now, utc_now())
            row = session.get(PcbCandidateRow, candidate_id)
            if row is None:
                raise PcbCandidateNotFoundError(candidate_id)
            result = dict(row.result_json or {})
            release = result.get("release")
            if (
                row.status != PcbCandidateStatus.RELEASE_PENDING.value
                or not isinstance(release, dict)
                or release.get("task_id") != task_id
            ):
                raise PcbCandidateNotReviewableError()
            previous_status = row.status
            previous_result = row.result_json
            previous_error_code = row.last_error_code
            written_version = row.version + 1
            active_task = exists().where(
                TaskRow.id == task_id,
                TaskRow.lease_token == lease_token,
                TaskRow.status == TaskStatus.RUNNING.value,
                TaskRow.lease_expires_at.is_not(None),
                TaskRow.lease_expires_at > transition_now,
            )
            changed = session.execute(
                update(PcbCandidateRow)
                .where(
                    PcbCandidateRow.id == candidate_id,
                    PcbCandidateRow.status == PcbCandidateStatus.RELEASE_PENDING.value,
                    PcbCandidateRow.version == row.version,
                    active_task,
                )
                .values(
                    status=PcbCandidateStatus.READY_FOR_G4.value,
                    result_json={**result, "release": {**release, **release_result, "status": "ready_for_g4"}},
                    last_error_code=None,
                    updated_at=transition_now,
                    version=row.version + 1,
                )
                .execution_options(synchronize_session=False)
            )
            if changed.rowcount != 1:
                raise RequestInvalidError("concurrent PCB release update")
            self._register_release_artifacts(session, descriptors, transition_now)
        try:
            self._assert_active_or_mirror_cancellation(
                candidate_id, task_id, lease_token, transition_now
            )
        except StaleLeaseError:
            self._revert_stale_release_transition(
                candidate_id,
                task_id,
                written_version,
                previous_status,
                previous_result,
                previous_error_code,
                transition_now,
            )
            raise
        return self.get(candidate_id)

    def restore_g3_after_release_failure(
        self,
        candidate_id: str,
        task_id: str,
        lease_token: str,
        now: datetime,
        *,
        error_code: str,
    ) -> PcbCandidate:
        self._tasks.assert_active(task_id, lease_token, now)
        with self._sessions.begin() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            transition_now = max(now, utc_now())
            row = session.get(PcbCandidateRow, candidate_id)
            if row is None:
                raise PcbCandidateNotFoundError(candidate_id)
            result = dict(row.result_json or {})
            release = result.get("release")
            if (
                row.status != PcbCandidateStatus.RELEASE_PENDING.value
                or not isinstance(release, dict)
                or release.get("task_id") != task_id
            ):
                raise PcbCandidateNotReviewableError()
            active_task = exists().where(
                TaskRow.id == task_id,
                TaskRow.lease_token == lease_token,
                TaskRow.status == TaskStatus.RUNNING.value,
                TaskRow.lease_expires_at.is_not(None),
                TaskRow.lease_expires_at > transition_now,
            )
            changed = session.execute(
                update(PcbCandidateRow)
                .where(
                    PcbCandidateRow.id == candidate_id,
                    PcbCandidateRow.status == PcbCandidateStatus.RELEASE_PENDING.value,
                    PcbCandidateRow.version == row.version,
                    active_task,
                )
                .values(
                    status=PcbCandidateStatus.G3_APPROVED.value,
                    result_json={**result, "release": {**release, "status": "failed", "error_code": error_code}},
                    last_error_code=error_code,
                    updated_at=transition_now,
                    version=row.version + 1,
                )
                .execution_options(synchronize_session=False)
            )
            if changed.rowcount != 1:
                raise RequestInvalidError("concurrent PCB release update")
        return self.get(candidate_id)

class PcbCandidateService:
    def __init__(self, store: PcbCandidateStore) -> None: self._store = store
    def create(self, **kwargs: Any) -> PcbCandidate: return self._store.create(**kwargs)
    def __getattr__(self, name: str) -> Any: return getattr(self._store, name)


class PcbCandidateTaskHandler:
    def __init__(self, candidates: PcbCandidateStore, clock=utc_now) -> None: self._candidates, self._clock = candidates, clock
    def __call__(self, lease: TaskLease) -> dict[str, Any]:
        """按项目内幂等键定位冻结候选，损坏任务直接终止。"""
        project_id = lease.payload.get("project_id")
        candidate_key = lease.payload.get("candidate_key")
        if not isinstance(project_id, str) or not isinstance(candidate_key, str):
            raise TerminalTaskError(
                "PCB_CANDIDATE_NOT_FOUND",
                "PCB candidate task has no durable candidate reference",
            )
        candidate = self._candidates.find_by_idempotency_key(project_id, candidate_key)
        if candidate is None or candidate.task_id != lease.task_id:
            raise TerminalTaskError(
                "PCB_CANDIDATE_NOT_FOUND", "PCB candidate not found"
            )
        self._candidates.mark_blocked(candidate.id, lease.task_id, lease.lease_token, self._clock(), result={"code": "PCB_CAPABILITY_GATE_BLOCKED"})
        return {"candidate_id": candidate.id, "status": PcbCandidateStatus.BLOCKED.value}


class PcbCandidateExecutionTaskHandler:
    """Run a frozen candidate in an adapter-owned isolated workspace."""

    def __init__(
        self,
        candidates: PcbCandidateStore,
        projects: ProjectRepository,
        tasks: TaskRepository,
        evidence: EvidenceRepository,
        findings: FindingRepository,
        artifacts: ContentAddressedStore,
        capability_gate: Any,
        adapter: PcbEdaAdapter,
        workspaces_dir: Path,
        revisions: Any,
        clock=utc_now,
    ) -> None:
        self._candidates = candidates
        self._projects = projects
        self._tasks = tasks
        self._evidence = evidence
        self._findings = findings
        self._artifacts = artifacts
        self._capability_gate = capability_gate
        self._adapter = adapter
        self._workspaces_dir = workspaces_dir
        self._revisions = revisions
        self._clock = clock

    def __call__(self, lease: TaskLease) -> dict[str, Any]:
        candidate = self._candidate_for_lease(lease)
        try:
            self._candidates.mark_executing(
                candidate.id, lease.task_id, lease.lease_token, self._clock()
            )
            self._require_capabilities(candidate)
            result, board_findings, native_findings = self._execute(candidate, lease)
        except TaskCancelledError:
            raise
        except LcedaProCapabilityError:
            self._candidates.mark_blocked(
                candidate.id,
                lease.task_id,
                lease.lease_token,
                self._clock(),
                result={"code": "PCB_CAPABILITY_GATE_BLOCKED"},
            )
            return {
                "candidate_id": candidate.id,
                "status": PcbCandidateStatus.BLOCKED.value,
            }
        except _CandidateExecutionError as error:
            self._mark_failed(candidate, lease, error.code, error.message)
            raise TerminalTaskError(error.code, error.message) from error
        except (BoardSemanticMismatchError, FileNotFoundError, OSError, ValueError) as error:
            self._mark_failed(
                candidate,
                lease,
                "PCB_CANDIDATE_EXECUTION_FAILED",
                str(error),
            )
            raise TerminalTaskError("PCB_CANDIDATE_EXECUTION_FAILED", str(error)) from error
        except StaleLeaseError:
            raise
        except Exception as error:
            self._mark_failed(
                candidate,
                lease,
                "PCB_CANDIDATE_EXECUTION_FAILED",
                str(error),
            )
            raise TerminalTaskError("PCB_CANDIDATE_EXECUTION_FAILED", str(error)) from error

        blocking = tuple(
            finding
            for finding in board_findings + native_findings
            if _is_blocking_finding(finding)
        )
        if blocking or result["unconnected_net_count"] != 0:
            code = "PCB_NATIVE_DRC_BLOCKED" if any(
                finding in native_findings for finding in blocking
            ) else "PCB_BOARDIR_VALIDATION_FAILED"
            if result["unconnected_net_count"] != 0:
                code = "PCB_UNCONNECTED_NETS"
            failed = {**result, "validation_error_code": code}
            self._candidates.mark_validation_failed(
                candidate.id,
                lease.task_id,
                lease.lease_token,
                self._clock(),
                code,
                result=failed,
            )
            raise TerminalTaskError(code, "candidate validation reported blocking findings")

        try:
            self._candidates.mark_ready_for_g3(
                candidate.id,
                lease.task_id,
                lease.lease_token,
                self._clock(),
                result=result,
            )
        except (TaskCancelledError, StaleLeaseError):
            raise
        except Exception as error:
            self._mark_failed(
                candidate,
                lease,
                "PCB_CANDIDATE_FINALIZATION_FAILED",
                str(error),
            )
            raise TerminalTaskError(
                "PCB_CANDIDATE_FINALIZATION_FAILED",
                str(error),
            ) from error
        return {
            "candidate_id": candidate.id,
            "candidate_digest": result["candidate_digest"],
            "status": PcbCandidateStatus.READY_FOR_G3.value,
        }

    def _candidate_for_lease(self, lease: TaskLease) -> PcbCandidate:
        project_id = lease.payload.get("project_id")
        candidate_key = lease.payload.get("candidate_key")
        if not isinstance(project_id, str) or not isinstance(candidate_key, str):
            raise TerminalTaskError(
                "PCB_CANDIDATE_NOT_FOUND",
                "PCB candidate task has no durable candidate reference",
            )
        candidate = self._candidates.find_by_idempotency_key(project_id, candidate_key)
        if candidate is None or candidate.task_id != lease.task_id:
            raise TerminalTaskError("PCB_CANDIDATE_NOT_FOUND", "PCB candidate not found")
        return candidate

    def _require_capabilities(self, candidate: PcbCandidate) -> None:
        operations = (
            EdaOperation.SNAPSHOT,
            EdaOperation.CREATE_CANDIDATE,
            EdaOperation.APPLY_OPERATIONS,
            EdaOperation.RUN_DRC,
        )
        require_operations = getattr(self._capability_gate, "require_operations", None)
        if callable(require_operations):
            digest = require_operations(
                candidate.project_id, EdaKind.LCEDA_PRO, operations
            )
            if digest != candidate.capability_digest:
                raise LcedaProCapabilityError("LCEDA_PRO_WRITE_CAPABILITY_UNVERIFIED")
            return
        for operation in operations:
            digest = self._capability_gate.require_operation(
                candidate.project_id, EdaKind.LCEDA_PRO, operation
            )
            if digest != candidate.capability_digest:
                raise LcedaProCapabilityError("LCEDA_PRO_WRITE_CAPABILITY_UNVERIFIED")

    def _execute(
        self, candidate: PcbCandidate, lease: TaskLease
    ) -> tuple[dict[str, Any], tuple[NormalizedFinding, ...], tuple[NormalizedFinding, ...]]:
        if _digest(candidate.operations) != candidate.operations_digest:
            raise _CandidateExecutionError(
                "PCB_CANDIDATE_OPERATION_DIGEST_MISMATCH",
                "persisted candidate operations do not match the frozen digest",
            )
        project = self._projects.get(candidate.project_id)
        managed_revision = project.mode is ProjectMode.MANAGED
        source_context = (
            self._revisions.materialize(
                project.id, candidate.base_revision, "pcb-candidate"
            )
            if managed_revision
            else nullcontext(project.source_path)
        )
        with source_context as frozen_source:
            source_before = self._revisions.snapshot_digest(frozen_source)
            if (
                not managed_revision
                and candidate.base_snapshot_digest is not None
                and source_before != candidate.base_snapshot_digest
            ):
                raise _CandidateExecutionError(
                    "PCB_CANDIDATE_SOURCE_CHANGED",
                    "registered source tree no longer matches the frozen candidate base",
                )
            self._tasks.assert_active(lease.task_id, lease.lease_token, self._clock())
            before = self._adapter.load_snapshot(frozen_source)
            before_bytes = before.canonical_bytes()
            before_digest = sha256_digest(before_bytes)
            if before_digest != candidate.board_snapshot_digest:
                raise _CandidateExecutionError(
                    "PCB_CANDIDATE_INPUT_DIGEST_MISMATCH",
                    "source BoardIR digest does not match the frozen candidate input",
                )
            rulepack, algorithm_evidence, rulepack_bytes = _load_candidate_rulepack(candidate)
            operations = deserialize_board_operations(candidate.operations)
            _validate_candidate_operations(
                candidate,
                before,
                operations,
                expected_snapshot_digest=before_digest,
            )

            self._workspaces_dir.mkdir(parents=True, exist_ok=True)
            with TemporaryDirectory(
                prefix=f"pcb-candidate-{candidate.id}-", dir=self._workspaces_dir
            ) as temporary:
                workspace = self._adapter.create_candidate(
                    frozen_source, Path(temporary) / "native-candidate"
                )
                self._tasks.assert_active(lease.task_id, lease.lease_token, self._clock())
                applied = self._adapter.apply_operations(workspace, operations, before)
                reread = self._adapter.load_snapshot(workspace.path)
                if reread != applied:
                    raise _CandidateExecutionError(
                        "PCB_CANDIDATE_REOPEN_MISMATCH",
                        "candidate BoardIR changed after adapter apply/reopen",
                    )
                before.validate_proposed(reread)
                board_findings = BoardRuleChecker().check(reread, rulepack)
                self._tasks.assert_active(lease.task_id, lease.lease_token, self._clock())
                reports = self._adapter.run_drc(workspace)
                if type(reports) is not tuple or not reports:
                    raise _CandidateExecutionError(
                        "PCB_NATIVE_DRC_REPORT_MISSING",
                        "native DRC did not return a report",
                    )
                if any(
                    not isinstance(report, ValidationReport)
                    or type(report.kind) is not str
                    or not report.kind.strip()
                    or type(report.findings) is not tuple
                    or any(
                        not isinstance(finding, NormalizedFinding)
                        for finding in report.findings
                    )
                    for report in reports
                ):
                    raise _CandidateExecutionError(
                        "PCB_NATIVE_DRC_REPORT_INVALID",
                        "native DRC returned a malformed report",
                    )

            self._tasks.assert_active(lease.task_id, lease.lease_token, self._clock())
            source_after = self._revisions.snapshot_digest(frozen_source)
            if source_after != source_before:
                raise _CandidateExecutionError(
                    "PCB_CANDIDATE_SOURCE_CHANGED",
                    "candidate execution modified the registered source tree",
                )
        native_findings = tuple(
            finding for report in reports for finding in report.findings
        )
        result = self._publish_evidence(
            candidate,
            lease,
            before_bytes,
            reread,
            rulepack_bytes,
            algorithm_evidence,
            source_before,
            source_after,
            semantic_diff(before, reread),
            board_findings,
            reports,
        )
        return result, board_findings, native_findings

    def _publish_evidence(
        self,
        candidate: PcbCandidate,
        lease: TaskLease,
        before_bytes: bytes,
        after: BoardSnapshot,
        rulepack_bytes: bytes,
        algorithm_evidence: dict[str, Any],
        source_before: str,
        source_after: str,
        diff: Any,
        board_findings: tuple[NormalizedFinding, ...],
        reports: tuple[Any, ...],
    ) -> dict[str, Any]:
        native_findings = tuple(
            finding for report in reports for finding in report.findings
        )
        unconnected = _unconnected_net_ids(algorithm_evidence, board_findings)
        blocking_count = sum(
            _is_blocking_finding(finding)
            for finding in board_findings + native_findings
        )
        staged: dict[str, StagedArtifact] = {}
        published: list[tuple[ArtifactDescriptor, StagedArtifact]] = []
        evidence_committed = False

        def stage(kind: str, data: bytes, media_type: str) -> StagedArtifact:
            self._tasks.assert_active(lease.task_id, lease.lease_token, self._clock())
            artifact = self._artifacts.stage_stream(io.BytesIO(data), media_type)
            staged[kind] = artifact
            self._tasks.assert_active(lease.task_id, lease.lease_token, self._clock())
            return artifact

        try:
            input_staged = stage(
                "pcb_input_snapshot",
                before_bytes,
                "application/vnd.pcbflow.boardir+json",
            )
            if input_staged.digest != candidate.board_snapshot_digest:
                raise _CandidateExecutionError(
                    "PCB_CANDIDATE_INPUT_DIGEST_MISMATCH",
                    "canonical BoardIR artifact does not match the frozen input digest",
                )
            rulepack_staged = stage(
                "rulepack",
                rulepack_bytes,
                "application/vnd.pcbflow.rulepack+json",
            )
            if rulepack_staged.digest != candidate.rulepack_digest:
                raise _CandidateExecutionError(
                    "PCB_CANDIDATE_RULEPACK_MISMATCH",
                    "canonical rulepack artifact does not match the frozen digest",
                )
            capability_descriptor = _existing_artifact_descriptor(
                self._artifacts,
                candidate.capability_digest,
                "application/vnd.pcbflow.eda-capability+json",
            )
            post_snapshot = after.canonical_bytes()
            post_snapshot_digest = sha256_digest(post_snapshot)
            validation_payload = {
                "schema_version": "1.0",
                "snapshot_digest": post_snapshot_digest,
                "snapshot": json.loads(post_snapshot),
                "findings": _findings_payload(board_findings),
            }
            native_payload = {
                "schema_version": "1.0",
                "reports": [
                    {"kind": report.kind, "findings": _findings_payload(report.findings)}
                    for report in reports
                ],
            }
            semantic_payload = {
                "schema_version": "1.0",
                "changed_object_ids": list(diff.changed_object_ids),
                "unexpected_object_ids": list(diff.unexpected_object_ids),
            }
            summary = {
                "schema_version": "1.0",
                "candidate_id": candidate.id,
                "project_id": candidate.project_id,
                "base_revision": candidate.base_revision,
                "source_snapshot_digest_before": source_before,
                "source_snapshot_digest_after": source_after,
                "board_snapshot_digest": candidate.board_snapshot_digest,
                "candidate_board_snapshot_digest": post_snapshot_digest,
                "rulepack_digest": candidate.rulepack_digest,
                "capability_digest": candidate.capability_digest,
                "authority_digest": candidate.authority_digest,
                "operations_digest": candidate.operations_digest,
                "blocking_finding_count": blocking_count,
                "unconnected_net_ids": list(unconnected),
            }
            stage(
                "placement_evidence",
                canonical_json_bytes(algorithm_evidence["placement"]),
                "application/vnd.pcbflow.pcb-placement-evidence+json",
            )
            stage(
                "routing_evidence",
                canonical_json_bytes(algorithm_evidence["routing"]),
                "application/vnd.pcbflow.pcb-routing-evidence+json",
            )
            stage(
                "copper_evidence",
                canonical_json_bytes(algorithm_evidence["copper"]),
                "application/vnd.pcbflow.pcb-copper-evidence+json",
            )
            stage(
                "boardir_validation",
                canonical_json_bytes(validation_payload),
                "application/vnd.pcbflow.pcb-boardir-validation+json",
            )
            stage(
                "native_drc",
                canonical_json_bytes(native_payload),
                "application/vnd.pcbflow.pcb-native-drc+json",
            )
            stage(
                "board_semantic_diff",
                canonical_json_bytes(semantic_payload),
                "application/vnd.pcbflow.pcb-semantic-diff+json",
            )
            stage(
                "candidate_summary",
                canonical_json_bytes(summary),
                "application/vnd.pcbflow.pcb-candidate-summary+json",
            )
            artifact_refs: dict[str, StagedArtifact | ArtifactDescriptor] = {
                **staged,
                "eda_capability": capability_descriptor,
            }
            evidence_kinds = tuple(sorted(artifact_refs))
            evidence_set = {
                "schema_version": "1.0",
                "candidate_id": candidate.id,
                "project_id": candidate.project_id,
                "task_id": candidate.task_id,
                "base_revision": candidate.base_revision,
                "items": [
                    {
                        "kind": kind,
                        "artifact_digest": artifact_refs[kind].digest,
                        "media_type": artifact_refs[kind].media_type,
                        "verdict": "pass"
                        if kind not in {"boardir_validation", "native_drc"}
                        or blocking_count == 0
                        else "fail",
                    }
                    for kind in sorted(artifact_refs)
                ],
            }
            stage(
                "evidence_set",
                canonical_json_bytes(evidence_set),
                "application/vnd.pcbflow.pcb-candidate-evidence-set+json",
            )
            descriptors: dict[str, ArtifactDescriptor] = {}
            for kind in sorted(staged):
                self._tasks.assert_active(lease.task_id, lease.lease_token, self._clock())
                if kind == "evidence_set":
                    continue
                descriptors[kind] = staged[kind].publish()
                published.append((descriptors[kind], staged[kind]))
            self._tasks.assert_active(lease.task_id, lease.lease_token, self._clock())
            evidence_set_descriptor = staged["evidence_set"].publish()
            published.append((evidence_set_descriptor, staged["evidence_set"]))
            self._tasks.assert_active(lease.task_id, lease.lease_token, self._clock())
            report_inputs = {
                G3_EVIDENCE_SET_KIND: (
                    evidence_set_descriptor,
                    candidate.id,
                    "pass",
                ),
                **{
                    item["kind"]: (
                        descriptors.get(item["kind"], capability_descriptor),
                        candidate.id,
                        item["verdict"],
                    )
                    for item in evidence_set["items"]
                },
            }
            self._evidence.add_reports_and_findings(
                project_id=candidate.project_id,
                task_id=lease.task_id,
                reports=report_inputs,
                findings={
                    "boardir_validation": board_findings,
                    "native_drc": native_findings,
                },
                lease_token=lease.lease_token,
                now=self._clock(),
            )
            evidence_committed = True
            candidate_digest = pcb_candidate_review_digest(
                candidate_id=candidate.id,
                project_id=candidate.project_id,
                base_revision=candidate.base_revision,
                base_snapshot_digest=candidate.base_snapshot_digest,
                board_snapshot_digest=candidate.board_snapshot_digest,
                candidate_board_snapshot_digest=post_snapshot_digest,
                rulepack_digest=candidate.rulepack_digest,
                capability_digest=candidate.capability_digest,
                authority_digest=candidate.authority_digest,
                operations_digest=candidate.operations_digest,
                evidence_set_digest=evidence_set_descriptor.digest,
            )
            return {
                **summary,
                "candidate_digest": candidate_digest,
                "evidence_set_digest": evidence_set_descriptor.digest,
                "evidence_kinds": list(evidence_kinds),
                "evidence_artifacts": {
                    kind: artifact_refs[kind].digest for kind in sorted(artifact_refs)
                },
                "unconnected_net_count": len(unconnected),
                "native_candidate_verified": True,
            }
        finally:
            for artifact in staged.values():
                if evidence_committed:
                    artifact.discard()
            if not evidence_committed:
                self._candidates.settle_publication(
                    tuple(descriptor for descriptor, _artifact in published),
                    tuple(artifact for _descriptor, artifact in published),
                )
                published_artifacts = {id(artifact) for _descriptor, artifact in published}
                for artifact in staged.values():
                    if id(artifact) not in published_artifacts:
                        artifact.discard()

    def _mark_failed(
        self, candidate: PcbCandidate, lease: TaskLease, code: str, message: str
    ) -> None:
        self._candidates.mark_validation_failed(
            candidate.id,
            lease.task_id,
            lease.lease_token,
            self._clock(),
            code,
            result={
                "candidate_id": candidate.id,
                "operations_digest": candidate.operations_digest,
                "board_snapshot_digest": candidate.board_snapshot_digest,
                "rulepack_digest": candidate.rulepack_digest,
                "capability_digest": candidate.capability_digest,
                "authority_digest": candidate.authority_digest,
                "error_code": code,
                "error_message": message,
            },
        )


@dataclass(frozen=True, slots=True)
class _CandidateExecutionError(Exception):
    code: str
    message: str


def _load_candidate_rulepack(
    candidate: PcbCandidate,
) -> tuple[ManufacturingRulePack, dict[str, Any], bytes]:
    evidence = candidate.algorithm_evidence
    if not isinstance(evidence, dict):
        raise _CandidateExecutionError(
            "PCB_CANDIDATE_EVIDENCE_INCOMPLETE", "candidate algorithm evidence is missing"
        )
    for kind in ("rulepack", "placement", "routing", "copper"):
        if type(evidence.get(kind)) is not dict:
            raise _CandidateExecutionError(
                "PCB_CANDIDATE_EVIDENCE_INCOMPLETE",
                f"candidate is missing {kind} evidence",
            )
    try:
        rulepack = ManufacturingRulePack.load_json(
            canonical_json_bytes(evidence["rulepack"])
        )
    except (TypeError, ValueError) as error:
        raise _CandidateExecutionError(
            "PCB_CANDIDATE_RULEPACK_INVALID", "candidate rulepack evidence is invalid"
        ) from error
    rulepack_bytes = rulepack.canonical_bytes()
    if sha256_digest(rulepack_bytes) != candidate.rulepack_digest:
        raise _CandidateExecutionError(
            "PCB_CANDIDATE_RULEPACK_MISMATCH",
            "candidate rulepack does not match its frozen digest",
        )
    return rulepack, evidence, rulepack_bytes


def _validate_candidate_operations(
    candidate: PcbCandidate,
    before: BoardSnapshot,
    operations: tuple[BoardOperation, ...],
    *,
    expected_snapshot_digest: str | None = None,
) -> None:
    if not operations:
        return
    if expected_snapshot_digest is None:
        expected_snapshot_digest = before.canonical_digest()
    for operation in operations:
        if (
            operation.project_id != candidate.project_id
            or operation.baseline_revision != candidate.base_revision
            or operation.rulepack_digest != candidate.rulepack_digest
            or operation.expected_snapshot_digest != expected_snapshot_digest
        ):
            raise _CandidateExecutionError(
                "PCB_CANDIDATE_OPERATION_MISMATCH",
                "typed operation does not match the frozen candidate inputs",
            )


def _existing_artifact_descriptor(
    artifacts: ContentAddressedStore, digest: str, media_type: str
) -> ArtifactDescriptor:
    with artifacts.open(digest) as stream:
        raw = stream.read()
    actual_digest = sha256_digest(raw)
    if actual_digest != digest:
        raise _CandidateExecutionError(
            "PCB_CANDIDATE_ARTIFACT_MISMATCH",
            "frozen evidence artifact digest does not match its content",
        )
    return ArtifactDescriptor(
        digest=digest,
        size=len(raw),
        media_type=media_type,
        path=artifacts._path(digest),
    )


def _findings_payload(
    findings: Sequence[NormalizedFinding],
) -> list[dict[str, str]]:
    return [
        {
            "rule_id": finding.rule_id,
            "severity": finding.severity,
            "subject": finding.subject,
            "message": finding.message,
        }
        for finding in findings
    ]


def _is_blocking_finding(finding: NormalizedFinding) -> bool:
    return finding.severity.casefold() in {"error", "critical", "fatal", "blocker"}


def _unconnected_net_ids(
    algorithm_evidence: dict[str, Any], findings: Sequence[NormalizedFinding]
) -> tuple[str, ...]:
    routing = algorithm_evidence["routing"]
    value = routing.get("final_unconnected_nets")
    if type(value) is not list or any(type(item) is not str for item in value):
        raise _CandidateExecutionError(
            "PCB_CANDIDATE_EVIDENCE_INCOMPLETE",
            "routing evidence must include final_unconnected_nets",
        )
    disconnected = {
        finding.subject
        for finding in findings
        if finding.rule_id == "PCB_ROUTE_DISCONNECTED"
    }
    return tuple(sorted(set(value) | disconnected))


# Preserve the original public handler name while routing every new worker
# through the evidence-bound implementation above.
PcbCandidateTaskHandler = PcbCandidateExecutionTaskHandler

"""Candidate input validation, digests, task keys, and frozen public types.

Verbatim extraction from the historical pcb_candidates monolith; behavior is
unchanged. The pcbflow.pcb_candidates module now re-exports from here.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pcbflow.canonical import canonical_digest
from pcbflow.domain import (
    RequestInvalidError,
)

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

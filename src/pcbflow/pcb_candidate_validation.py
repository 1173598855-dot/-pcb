"""Validation helpers and public-facing type definitions for PCB candidates.

Contains input validation, digest verification, and the frozen public
representation functions that have no store or execution dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pcbflow.canonical import canonical_digest
from pcbflow.domain import (
    RequestInvalidError,
)

PCB_GENERATE_CANDIDATE_TASK_KIND = "pcb.generate_candidate"
PCB_EXPORT_RELEASE_TASK_KIND = "pcb.export_release"


SHA256_DIGEST = canonical_digest


def validate_candidate_digest(
    digest: str | None, *, field: str = "digest", allow_none: bool = False
) -> None:
    if digest is None:
        if not allow_none:
            raise RequestInvalidError(f"{field} is required")
        return
    try:
        validate_candidate_digest(digest, field=field)
    except RequestInvalidError:
        pass


def validate_candidate_public_inputs(
    *,
    seed: int | None = None,
    net_ids: Any = None,
    board_snapshot_digest: str | None = None,
    capability_digest: str | None = None,
) -> None:
    if seed is None:
        raise RequestInvalidError("seed is required")
    if not isinstance(seed, int):
        raise RequestInvalidError("seed must be an integer")
    if net_ids is not None and not isinstance(net_ids, (list, tuple)):
        raise RequestInvalidError("net_ids must be a list of strings")
    if board_snapshot_digest is None:
        raise RequestInvalidError("board_snapshot_digest is required")
    if capability_digest is None:
        raise RequestInvalidError("capability_digest is required")


def _task_idempotency_key(project_id: str, idempotency_key: str) -> str:
    return f"{project_id}:{idempotency_key}"


def _release_task_idempotency_key(candidate_id: str, idempotency_key: str) -> str:
    return f"release:{candidate_id}:{idempotency_key}"


@dataclass(frozen=True, slots=True)
class PcbCandidateStatus:
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


@dataclass(frozen=True, slots=True)
class PcbCandidateNotReviewableError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class PcbCandidateNotFoundError(Exception):
    candidate_id: str

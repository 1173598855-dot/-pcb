from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from pcbflow.commands import load_command_batch
from pcbflow.domain import ProjectMode
from pcbflow.repositories import (
    IdempotencyConflictError,
    ProjectRepository,
    RevisionConflictError,
)
from pcbflow.requirement_store import RequirementStore
from pcbflow.requirements import RequirementSetStatus

if TYPE_CHECKING:
    from pcbflow.proposal_store import ProposalStore


DESIGN_PROPOSAL_TASK_KIND = "design.execute_proposal"


class ProposalStatus(StrEnum):
    QUEUED = "queued"
    EXECUTING = "executing"
    VALIDATION_FAILED = "validation_failed"
    READY_FOR_REVIEW = "ready_for_review"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class ChangeProposal:
    id: str
    project_id: str
    command_batch_id: str
    task_id: str
    status: ProposalStatus
    candidate_revision: str | None
    candidate_snapshot_digest: str | None
    review_digest: str | None
    semantic_diff_digest: str | None
    evidence_set_digest: str | None
    result: dict[str, object] | None
    last_error_code: str | None
    created_at: datetime
    updated_at: datetime
    version: int


class ProjectNotManagedError(RuntimeError):
    pass


class ProposalService:
    def __init__(
        self,
        projects: ProjectRepository,
        requirements: RequirementStore,
        store: ProposalStore,
    ) -> None:
        self._projects = projects
        self._requirements = requirements
        self._store = store

    def create(self, data: bytes, idempotency_key: str) -> ChangeProposal:
        batch = load_command_batch(data)
        if batch.idempotency_key != idempotency_key:
            raise IdempotencyConflictError(idempotency_key)
        existing = self._store.find_existing(batch)
        if existing is not None:
            return existing
        project = self._projects.get(batch.project_id)
        if project.mode is not ProjectMode.MANAGED:
            raise ProjectNotManagedError(project.id)
        if project.current_revision != batch.base_revision:
            raise RevisionConflictError(batch.base_revision, project.current_revision)
        requirement_set = self._requirements.get(batch.requirement_set_id)
        if (
            requirement_set.project_id != project.id
            or requirement_set.status is not RequirementSetStatus.FROZEN
            or project.active_requirement_set_id != requirement_set.id
        ):
            raise ValueError("batch must use the active frozen requirement set")
        return self._store.create_queued(batch)

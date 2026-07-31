from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def utc_now() -> datetime:
    return datetime.now(UTC)


class ProjectMode(StrEnum):
    REGISTERED = "registered"
    MANAGED = "managed"


@dataclass(frozen=True, slots=True)
class Project:
    id: str
    name: str
    source_path: Path
    created_at: datetime
    mode: ProjectMode
    managed_repo_key: str | None
    current_revision: str | None
    project_snapshot_digest: str | None
    active_requirement_set_id: str | None
    adoption_idempotency_key: str | None
    adoption_input_digest: str | None
    managed_at: datetime | None
    version: int


@dataclass(frozen=True, slots=True)
class ProjectRevision:
    id: str
    project_id: str
    revision: str
    parent_revision: str | None
    snapshot_digest: str
    requirement_set_id: str | None
    command_batch_id: str | None
    created_at: datetime


class TaskStatus(StrEnum):
    QUEUED = "queued"
    LEASED = "leased"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    RETRY_WAIT = "retry_wait"
    FAILED_TERMINAL = "failed_terminal"


@dataclass(frozen=True, slots=True)
class Task:
    id: str
    project_id: str | None
    kind: str
    payload: dict[str, Any]
    result: dict[str, Any] | None
    status: TaskStatus
    attempt_count: int
    last_error_code: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class TaskLease:
    task_id: str
    kind: str
    payload: dict[str, Any]
    lease_token: str
    lease_expires_at: datetime
    attempt_number: int


@dataclass(frozen=True, slots=True)
class NormalizedFinding:
    rule_id: str
    severity: str
    subject: str
    message: str


@dataclass(frozen=True, slots=True)
class ValidationReport:
    kind: str
    findings: tuple[NormalizedFinding, ...]


@dataclass(frozen=True, slots=True)
class Evidence:
    id: str
    project_id: str
    task_id: str
    kind: str
    artifact_digest: str
    subject: str
    verdict: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Finding:
    id: str
    project_id: str
    task_id: str
    evidence_id: str
    rule_id: str
    severity: str
    subject: str
    message: str
    status: str
    created_at: datetime

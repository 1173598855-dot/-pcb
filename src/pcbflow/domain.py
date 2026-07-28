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


@dataclass(frozen=True, slots=True)
class Project:
    id: str
    name: str
    source_path: Path
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

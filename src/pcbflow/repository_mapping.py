"""Row-to-domain mapping helpers shared by the repository classes.

These helpers translate SQLAlchemy rows into the frozen domain value
objects. They live apart from :mod:`pcbflow.repositories` so the mapping
rules stay readable next to the schema they read, and are re-exported
from :mod:`pcbflow.repositories` for compatibility.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from pcbflow.cancellation import TaskCancelledError
from pcbflow.domain import (
    Evidence,
    Finding,
    Project,
    ProjectMode,
    Task,
    TaskStatus,
)
from pcbflow.repository_errors import StaleLeaseError
from pcbflow.tables import EvidenceRow, FindingRow, ProjectRow, TaskRow


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def assert_active_fence(
    session: Session, task_id: str, lease_token: str, now: datetime
) -> None:
    cancelled = session.scalar(
        select(TaskRow.id).where(
            TaskRow.id == task_id,
            TaskRow.status == TaskStatus.CANCELLED.value,
        )
    )
    if cancelled is not None:
        raise TaskCancelledError(task_id)
    active = session.scalar(
        select(TaskRow.id).where(
            TaskRow.id == task_id,
            TaskRow.lease_token == lease_token,
            TaskRow.status == TaskStatus.RUNNING.value,
            TaskRow.lease_expires_at.is_not(None),
            TaskRow.lease_expires_at > now,
        )
    )
    if active is None:
        raise StaleLeaseError(task_id)


def project(row: ProjectRow) -> Project:
    return Project(
        id=row.id,
        name=row.name,
        source_path=Path(row.source_path),
        created_at=utc(row.created_at),
        mode=ProjectMode(row.mode),
        managed_repo_key=row.managed_repo_key,
        current_revision=row.current_revision,
        project_snapshot_digest=row.project_snapshot_digest,
        active_requirement_set_id=row.active_requirement_set_id,
        adoption_idempotency_key=row.adoption_idempotency_key,
        adoption_input_digest=row.adoption_input_digest,
        managed_at=utc(row.managed_at) if row.managed_at is not None else None,
        version=row.version,
    )


def task(row: TaskRow) -> Task:
    return Task(
        id=row.id,
        project_id=row.project_id,
        kind=row.kind,
        payload=dict(row.payload_json),
        result=dict(row.result_json) if row.result_json is not None else None,
        status=TaskStatus(row.status),
        attempt_count=row.attempt_count,
        last_error_code=row.last_error_code,
        cancelled_at=(
            utc(row.cancelled_at) if row.cancelled_at is not None else None
        ),
        cancellation_reason=row.cancellation_reason,
        created_at=utc(row.created_at),
        updated_at=utc(row.updated_at),
    )


def evidence(row: EvidenceRow) -> Evidence:
    return Evidence(
        id=row.id,
        project_id=row.project_id,
        task_id=row.task_id,
        kind=row.kind,
        artifact_digest=row.artifact_digest,
        subject=row.subject,
        verdict=row.verdict,
        created_at=utc(row.created_at),
    )


def finding(row: FindingRow) -> Finding:
    return Finding(
        id=row.id,
        project_id=row.project_id,
        task_id=row.task_id,
        evidence_id=row.evidence_id,
        rule_id=row.rule_id,
        severity=row.severity,
        subject=row.subject,
        message=row.message,
        status=row.status,
        created_at=utc(row.created_at),
    )


__all__ = [
    "assert_active_fence",
    "evidence",
    "finding",
    "project",
    "task",
    "utc",
]

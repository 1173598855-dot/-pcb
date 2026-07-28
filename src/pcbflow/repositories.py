from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from pcbflow.domain import Project, Task, TaskLease, TaskStatus, new_id, utc_now
from pcbflow.tables import ProjectRow, TaskAttemptRow, TaskRow


class ProjectNotFoundError(LookupError):
    pass


class IdempotencyConflictError(RuntimeError):
    pass


class TaskNotFoundError(LookupError):
    pass


class StaleLeaseError(RuntimeError):
    pass


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _project(row: ProjectRow) -> Project:
    return Project(
        id=row.id,
        name=row.name,
        source_path=Path(row.source_path),
        created_at=_utc(row.created_at),
    )


def _task(row: TaskRow) -> Task:
    return Task(
        id=row.id,
        project_id=row.project_id,
        kind=row.kind,
        payload=dict(row.payload_json),
        result=dict(row.result_json) if row.result_json is not None else None,
        status=TaskStatus(row.status),
        attempt_count=row.attempt_count,
        last_error_code=row.last_error_code,
        created_at=_utc(row.created_at),
        updated_at=_utc(row.updated_at),
    )


class ProjectRepository:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def create(self, name: str, source_path: Path, idempotency_key: str) -> Project:
        resolved = source_path.resolve(strict=True)
        if not resolved.is_dir():
            raise ValueError("project source must be a directory")
        with self._sessions.begin() as session:
            row = session.scalar(
                select(ProjectRow).where(ProjectRow.idempotency_key == idempotency_key)
            )
            if row is not None:
                if row.name != name or Path(row.source_path) != resolved:
                    raise IdempotencyConflictError(idempotency_key)
                return _project(row)
            row = ProjectRow(
                id=new_id("prj"),
                name=name,
                source_path=str(resolved),
                idempotency_key=idempotency_key,
                created_at=utc_now(),
            )
            session.add(row)
        return _project(row)

    def get(self, project_id: str) -> Project:
        with self._sessions() as session:
            row = session.get(ProjectRow, project_id)
            if row is None:
                raise ProjectNotFoundError(project_id)
            return _project(row)

    def list(self) -> list[Project]:
        with self._sessions() as session:
            rows = session.scalars(
                select(ProjectRow).order_by(ProjectRow.created_at, ProjectRow.id)
            ).all()
            return [_project(row) for row in rows]


class TaskRepository:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def enqueue(
        self,
        kind: str,
        payload: dict[str, Any],
        idempotency_key: str,
        project_id: str | None,
    ) -> Task:
        json.dumps(payload, sort_keys=True, allow_nan=False)
        with self._sessions.begin() as session:
            row = session.scalar(
                select(TaskRow).where(TaskRow.idempotency_key == idempotency_key)
            )
            if row is not None:
                if (
                    row.kind != kind
                    or row.project_id != project_id
                    or row.payload_json != payload
                ):
                    raise IdempotencyConflictError(idempotency_key)
                return _task(row)
            now = utc_now()
            row = TaskRow(
                id=new_id("tsk"),
                project_id=project_id,
                kind=kind,
                payload_json=payload,
                result_json=None,
                status=TaskStatus.QUEUED.value,
                idempotency_key=idempotency_key,
                lease_owner=None,
                lease_token=None,
                lease_expires_at=None,
                last_error_code=None,
                attempt_count=0,
                created_at=now,
                updated_at=now,
                version=1,
            )
            session.add(row)
        return _task(row)

    def get(self, task_id: str) -> Task:
        with self._sessions() as session:
            row = session.get(TaskRow, task_id)
            if row is None:
                raise TaskNotFoundError(task_id)
            return _task(row)

    @staticmethod
    def _claimable(now: datetime):
        return or_(
            TaskRow.status.in_(
                [TaskStatus.QUEUED.value, TaskStatus.RETRY_WAIT.value]
            ),
            and_(
                TaskRow.status.in_(
                    [TaskStatus.LEASED.value, TaskStatus.RUNNING.value]
                ),
                TaskRow.lease_expires_at.is_not(None),
                TaskRow.lease_expires_at <= now,
            ),
        )

    def claim_next(
        self,
        worker_id: str,
        now: datetime,
        lease_seconds: int,
    ) -> TaskLease | None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")

        for _attempt in range(3):
            with self._sessions.begin() as session:
                row = session.scalar(
                    select(TaskRow)
                    .where(self._claimable(now))
                    .order_by(TaskRow.created_at, TaskRow.id)
                    .limit(1)
                )
                if row is None:
                    return None
                old_version = row.version
                attempt_number = row.attempt_count + 1
                lease_token = uuid4().hex
                lease_expires_at = now + timedelta(seconds=lease_seconds)
                claimed = session.execute(
                    update(TaskRow)
                    .where(
                        TaskRow.id == row.id,
                        TaskRow.version == old_version,
                        self._claimable(now),
                    )
                    .values(
                        status=TaskStatus.LEASED.value,
                        lease_owner=worker_id,
                        lease_token=lease_token,
                        lease_expires_at=lease_expires_at,
                        attempt_count=attempt_number,
                        updated_at=now,
                        version=old_version + 1,
                    )
                    .execution_options(synchronize_session=False)
                )
                if claimed.rowcount != 1:
                    continue
                session.execute(
                    update(TaskAttemptRow)
                    .where(
                        TaskAttemptRow.task_id == row.id,
                        TaskAttemptRow.finished_at.is_(None),
                    )
                    .values(finished_at=now, outcome="lease_expired")
                )
                session.add(
                    TaskAttemptRow(
                        id=new_id("att"),
                        task_id=row.id,
                        attempt_number=attempt_number,
                        lease_token=lease_token,
                        started_at=now,
                        finished_at=None,
                        outcome=None,
                        error_code=None,
                    )
                )
                return TaskLease(
                    task_id=row.id,
                    kind=row.kind,
                    payload=dict(row.payload_json),
                    lease_token=lease_token,
                    lease_expires_at=lease_expires_at,
                    attempt_number=attempt_number,
                )
        return None

    def start(self, task_id: str, lease_token: str) -> None:
        self._transition_with_lease(
            task_id,
            lease_token,
            [TaskStatus.LEASED],
            status=TaskStatus.RUNNING.value,
            updated_at=utc_now(),
        )

    def complete(
        self,
        task_id: str,
        lease_token: str,
        result: dict[str, Any],
    ) -> None:
        json.dumps(result, sort_keys=True, allow_nan=False)
        now = utc_now()
        self._finish_attempt(
            task_id,
            lease_token,
            now,
            "succeeded",
            None,
            status=TaskStatus.SUCCEEDED.value,
            result_json=result,
            last_error_code=None,
        )

    def fail(
        self,
        task_id: str,
        lease_token: str,
        error_code: str,
        retryable: bool,
    ) -> None:
        now = utc_now()
        status = (
            TaskStatus.RETRY_WAIT.value
            if retryable
            else TaskStatus.FAILED_TERMINAL.value
        )
        self._finish_attempt(
            task_id,
            lease_token,
            now,
            "retryable_failure" if retryable else "terminal_failure",
            error_code,
            status=status,
            result_json=None,
            last_error_code=error_code,
        )

    def _transition_with_lease(
        self,
        task_id: str,
        lease_token: str,
        allowed: list[TaskStatus],
        **values: Any,
    ) -> None:
        with self._sessions.begin() as session:
            changed = session.execute(
                update(TaskRow)
                .where(
                    TaskRow.id == task_id,
                    TaskRow.lease_token == lease_token,
                    TaskRow.status.in_([status.value for status in allowed]),
                )
                .values(**values, version=TaskRow.version + 1)
                .execution_options(synchronize_session=False)
            )
            if changed.rowcount != 1:
                raise StaleLeaseError(task_id)

    def _finish_attempt(
        self,
        task_id: str,
        lease_token: str,
        now: datetime,
        outcome: str,
        error_code: str | None,
        **values: Any,
    ) -> None:
        with self._sessions.begin() as session:
            changed = session.execute(
                update(TaskRow)
                .where(
                    TaskRow.id == task_id,
                    TaskRow.lease_token == lease_token,
                    TaskRow.status.in_(
                        [TaskStatus.LEASED.value, TaskStatus.RUNNING.value]
                    ),
                )
                .values(
                    **values,
                    lease_owner=None,
                    lease_token=None,
                    lease_expires_at=None,
                    updated_at=now,
                    version=TaskRow.version + 1,
                )
                .execution_options(synchronize_session=False)
            )
            if changed.rowcount != 1:
                raise StaleLeaseError(task_id)
            session.execute(
                update(TaskAttemptRow)
                .where(
                    TaskAttemptRow.task_id == task_id,
                    TaskAttemptRow.lease_token == lease_token,
                    TaskAttemptRow.finished_at.is_(None),
                )
                .values(finished_at=now, outcome=outcome, error_code=error_code)
            )

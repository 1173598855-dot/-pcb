from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import CursorResult, and_, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from pcbflow.artifacts import ArtifactDescriptor, StagedArtifact
from pcbflow.design_tables import (
    OutboxEventRow,
    PcbCandidateRow,
    ProjectEdaAuthorityRow,
    ProjectRevisionRow,
)
from pcbflow.domain import (
    Evidence,
    Finding,
    NormalizedFinding,
    Project,
    ProjectMode,
    RequestInvalidError,
    Task,
    TaskLease,
    TaskStatus,
    new_id,
    utc_now,
)
from pcbflow.eda import (
    EdaAuthorityConflictError,
    ProjectEdaAuthorityInput,
    authority_digest,
    registration_input_digest,
    validate_authority_input,
    validate_idempotency_key,
)
from pcbflow.observability import audit_payload
from pcbflow.repository_errors import (
    EvidenceConflictError,
    IdempotencyConflictError,
    ProjectNotFoundError,
    RevisionConflictError,
    StaleLeaseError,
    TaskNotCancellableError,
    TaskNotFoundError,
)
from pcbflow.repository_mapping import (
    assert_active_fence as _assert_active_fence,
)
from pcbflow.repository_mapping import (
    evidence as _evidence,
)
from pcbflow.repository_mapping import (
    finding as _finding,
)
from pcbflow.repository_mapping import (
    project as _project,
)
from pcbflow.repository_mapping import (
    task as _task,
)
from pcbflow.tables import (
    ArtifactRow,
    EvidenceRow,
    FindingRow,
    ProjectRow,
    TaskAttemptRow,
    TaskRow,
)
from pcbflow.workspaces import assert_supported_entry


class ProjectRepository:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def create(self, name: str, source_path: Path, idempotency_key: str) -> Project:
        project, _created = self.create_with_status(
            name, source_path, idempotency_key
        )
        return project

    def create_with_status(
        self,
        name: str,
        source_path: Path,
        idempotency_key: str,
        authority: ProjectEdaAuthorityInput | None = None,
    ) -> tuple[Project, bool]:
        assert_supported_entry(source_path)
        resolved = source_path.resolve(strict=True)
        if not resolved.is_dir():
            raise ValueError("project source must be a directory")
        validate_idempotency_key(idempotency_key)
        if authority is not None:
            validate_authority_input(authority)
        try:
            with self._sessions.begin() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                row = session.scalar(
                    select(ProjectRow).where(
                        ProjectRow.idempotency_key == idempotency_key
                    )
                )
                if row is not None:
                    if row.name != name or Path(row.source_path) != resolved:
                        raise IdempotencyConflictError(idempotency_key)
                    self._assert_registration_replay(
                        row, name, resolved, authority, idempotency_key
                    )
                    return _project(row), False
                row = ProjectRow(
                    id=new_id("prj"),
                    name=name,
                    source_path=str(resolved),
                    idempotency_key=idempotency_key,
                    registration_input_digest=registration_input_digest(
                        name, resolved, authority
                    ),
                    created_at=utc_now(),
                )
                session.add(row)
                session.flush()
                if authority is not None:
                    session.add(
                        ProjectEdaAuthorityRow(
                            project_id=row.id,
                            eda_kind=authority.eda_kind.value,
                            eda_profile_id=authority.eda_profile_id,
                            board_profile_id=authority.board_profile_id,
                            rulepack_digest=authority.rulepack_digest,
                            idempotency_key=idempotency_key,
                            canonical_digest=authority_digest(row.id, authority),
                            created_at=utc_now(),
                        )
                    )
                    session.flush()
            return _project(row), True
        except IntegrityError as error:
            # A concurrent creator may win between the read and the insert.
            # Re-read the durable row and apply the same idempotency contract.
            with self._sessions() as session:
                row = session.scalar(
                    select(ProjectRow).where(
                        ProjectRow.idempotency_key == idempotency_key
                    )
                )
                if row is None:
                    raise
                if row.name != name or Path(row.source_path) != resolved:
                    raise IdempotencyConflictError(idempotency_key) from error
                self._assert_registration_replay(
                    row, name, resolved, authority, idempotency_key
                )
                return _project(row), False

    @staticmethod
    def _assert_registration_replay(
        project: ProjectRow,
        name: str,
        source_path: Path,
        authority: ProjectEdaAuthorityInput | None,
        idempotency_key: str,
    ) -> None:
        if project.registration_input_digest is None:
            raise IdempotencyConflictError(idempotency_key)
        if project.registration_input_digest != registration_input_digest(
            name, source_path, authority
        ):
            raise EdaAuthorityConflictError(
                "project creation replay changed the EDA authority tuple"
            )

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

    def find_by_adoption_key(self, idempotency_key: str) -> Project | None:
        with self._sessions() as session:
            row = session.scalar(
                select(ProjectRow).where(
                    ProjectRow.adoption_idempotency_key == idempotency_key
                )
            )
            return _project(row) if row is not None else None

    @staticmethod
    def _replay_adoption(
        row: ProjectRow,
        *,
        project_id: str,
        repo_key: str,
        revision: str,
        snapshot_digest: str,
        adoption_idempotency_key: str,
        adoption_input_digest: str,
    ) -> Project:
        if row.id != project_id:
            raise IdempotencyConflictError(adoption_idempotency_key)
        if row.adoption_idempotency_key == adoption_idempotency_key:
            if (
                row.mode == ProjectMode.MANAGED.value
                and row.managed_repo_key == repo_key
                and row.current_revision == revision
                and row.project_snapshot_digest == snapshot_digest
                and row.adoption_input_digest == adoption_input_digest
            ):
                return _project(row)
            raise IdempotencyConflictError(adoption_idempotency_key)
        if (
            row.mode == ProjectMode.MANAGED.value
            and row.adoption_input_digest == adoption_input_digest
        ):
            return _project(row)
        raise IdempotencyConflictError(adoption_idempotency_key)

    def mark_managed(
        self,
        project_id: str,
        *,
        repo_key: str,
        revision: str,
        snapshot_digest: str,
        adoption_idempotency_key: str,
        adoption_input_digest: str,
        source_head: str | None,
        expected_version: int,
    ) -> Project:
        try:
            with self._sessions.begin() as session:
                keyed = session.scalar(
                    select(ProjectRow).where(
                        ProjectRow.adoption_idempotency_key
                        == adoption_idempotency_key
                    )
                )
                if keyed is not None:
                    return self._replay_adoption(
                        keyed,
                        project_id=project_id,
                        repo_key=repo_key,
                        revision=revision,
                        snapshot_digest=snapshot_digest,
                        adoption_idempotency_key=adoption_idempotency_key,
                        adoption_input_digest=adoption_input_digest,
                    )

                row = session.get(ProjectRow, project_id)
                if row is None:
                    raise ProjectNotFoundError(project_id)
                if row.mode == ProjectMode.MANAGED.value:
                    return self._replay_adoption(
                        row,
                        project_id=project_id,
                        repo_key=repo_key,
                        revision=revision,
                        snapshot_digest=snapshot_digest,
                        adoption_idempotency_key=adoption_idempotency_key,
                        adoption_input_digest=adoption_input_digest,
                    )

                now = utc_now()
                changed = cast(
                    "CursorResult[Any]",
                    session.execute(
                    update(ProjectRow)
                    .where(
                        ProjectRow.id == project_id,
                        ProjectRow.mode == ProjectMode.REGISTERED.value,
                        ProjectRow.version == expected_version,
                    )
                    .values(
                        mode=ProjectMode.MANAGED.value,
                        managed_repo_key=repo_key,
                        current_revision=revision,
                        project_snapshot_digest=snapshot_digest,
                        adoption_idempotency_key=adoption_idempotency_key,
                        adoption_input_digest=adoption_input_digest,
                        managed_at=now,
                        version=ProjectRow.version + 1,
                    )
                    .execution_options(synchronize_session=False)
                ))
                if changed.rowcount != 1:
                    session.expire_all()
                    actual = session.get(ProjectRow, project_id)
                    if actual is None:
                        raise ProjectNotFoundError(project_id)
                    if actual.mode == ProjectMode.MANAGED.value:
                        return self._replay_adoption(
                            actual,
                            project_id=project_id,
                            repo_key=repo_key,
                            revision=revision,
                            snapshot_digest=snapshot_digest,
                            adoption_idempotency_key=adoption_idempotency_key,
                            adoption_input_digest=adoption_input_digest,
                        )
                    raise RevisionConflictError(
                        row.current_revision, actual.current_revision
                    )

                adopted_payload = audit_payload(
                    actor_type="service",
                    actor_id="pcbflow",
                    action="project.adopt",
                    object_type="project",
                    object_id=project_id,
                    before_digest=None,
                    after_digest=snapshot_digest,
                    result="adopted",
                )
                adopted_payload.update(
                    {
                        "project_id": project_id,
                        "revision": revision,
                        "snapshot_digest": snapshot_digest,
                        "adoption_input_digest": adoption_input_digest,
                        "source_head": source_head,
                    }
                )
                session.add(
                    ProjectRevisionRow(
                        id=new_id("rev"),
                        project_id=project_id,
                        revision=revision,
                        parent_revision=None,
                        snapshot_digest=snapshot_digest,
                        requirement_set_id=None,
                        command_batch_id=None,
                        created_at=now,
                    )
                )
                session.add(
                    OutboxEventRow(
                        id=new_id("evt"),
                        aggregate_type="project",
                        aggregate_id=project_id,
                        event_type="project.adopted",
                        payload_json=adopted_payload,
                        created_at=now,
                        processed_at=None,
                        attempt_count=0,
                        last_error_code=None,
                    )
                )
                session.flush()
                session.expire_all()
                managed = session.get(ProjectRow, project_id)
                assert managed is not None
                return _project(managed)
        except IntegrityError:
            with self._sessions() as session:
                keyed = session.scalar(
                    select(ProjectRow).where(
                        ProjectRow.adoption_idempotency_key
                        == adoption_idempotency_key
                    )
                )
                if keyed is None:
                    raise
                return self._replay_adoption(
                    keyed,
                    project_id=project_id,
                    repo_key=repo_key,
                    revision=revision,
                    snapshot_digest=snapshot_digest,
                    adoption_idempotency_key=adoption_idempotency_key,
                    adoption_input_digest=adoption_input_digest,
                )

    def compare_and_set_revision(
        self,
        project_id: str,
        *,
        expected_revision: str,
        new_revision: str,
        snapshot_digest: str,
        expected_version: int,
    ) -> Project:
        with self._sessions.begin() as session:
            changed = cast(
                "CursorResult[Any]",
                session.execute(
                update(ProjectRow)
                .where(
                    ProjectRow.id == project_id,
                    ProjectRow.version == expected_version,
                    ProjectRow.current_revision == expected_revision,
                )
                .values(
                    current_revision=new_revision,
                    project_snapshot_digest=snapshot_digest,
                    version=ProjectRow.version + 1,
                )
                .execution_options(synchronize_session=False)
            ))
            if changed.rowcount != 1:
                actual = session.get(ProjectRow, project_id)
                if actual is None:
                    raise ProjectNotFoundError(project_id)
                raise RevisionConflictError(expected_revision, actual.current_revision)
            session.expire_all()
            updated = session.get(ProjectRow, project_id)
            assert updated is not None
            return _project(updated)


class TaskRepository:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        max_attempts: int = 5,
        retry_base_seconds: int = 5,
        retry_max_delay_seconds: int = 300,
    ) -> None:
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        if retry_base_seconds <= 0:
            raise ValueError("retry_base_seconds must be positive")
        if retry_max_delay_seconds <= 0:
            raise ValueError("retry_max_delay_seconds must be positive")
        if retry_base_seconds > retry_max_delay_seconds:
            raise ValueError("retry base delay must not exceed retry maximum delay")
        self._sessions = sessions
        self._max_attempts = max_attempts
        self._retry_base_seconds = retry_base_seconds
        self._retry_max_delay_seconds = retry_max_delay_seconds

    def enqueue(
        self,
        kind: str,
        payload: dict[str, Any],
        idempotency_key: str,
        project_id: str | None,
    ) -> Task:
        try:
            with self._sessions.begin() as session:
                return self.enqueue_in_session(
                    session, kind, payload, idempotency_key, project_id
                )
        except IntegrityError as error:
            with self._sessions() as session:
                row = session.scalar(
                    select(TaskRow).where(TaskRow.idempotency_key == idempotency_key)
                )
                if row is None:
                    raise
                if (
                    row.kind != kind
                    or row.project_id != project_id
                    or row.payload_json != payload
                ):
                    raise IdempotencyConflictError(idempotency_key) from error
                return _task(row)

    def enqueue_in_session(
        self,
        session: Session,
        kind: str,
        payload: dict[str, Any],
        idempotency_key: str,
        project_id: str | None,
    ) -> Task:
        """在调用方事务中创建任务，供需要原子写入的工作流复用。"""
        json.dumps(payload, sort_keys=True, allow_nan=False)
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
            next_attempt_at=None,
            last_error_code=None,
            cancelled_at=None,
            cancellation_reason=None,
            attempt_count=0,
            created_at=now,
            updated_at=now,
            version=1,
        )
        session.add(row)
        # 先落库任务以满足候选表的外键，同时仍由外层事务统一提交。
        session.flush()
        return _task(row)

    def get(self, task_id: str) -> Task:
        with self._sessions() as session:
            row = session.get(TaskRow, task_id)
            if row is None:
                raise TaskNotFoundError(task_id)
            return _task(row)

    def is_cancelled(self, task_id: str) -> bool:
        with self._sessions() as session:
            status = session.scalar(
                select(TaskRow.status).where(TaskRow.id == task_id)
            )
        return status == TaskStatus.CANCELLED.value

    def cancel(self, task_id: str, reason: str, now: datetime) -> Task:
        cancellation_reason = reason.strip()
        if not cancellation_reason:
            raise RequestInvalidError("cancellation reason must not be empty")
        if len(cancellation_reason) > 1000:
            raise RequestInvalidError("cancellation reason must not exceed 1000 characters")
        cancellable = {
            TaskStatus.QUEUED.value,
            TaskStatus.RETRY_WAIT.value,
            TaskStatus.LEASED.value,
            TaskStatus.RUNNING.value,
        }
        for _attempt in range(3):
            with self._sessions.begin() as session:
                row = session.get(TaskRow, task_id)
                if row is None:
                    raise TaskNotFoundError(task_id)
                if row.status == TaskStatus.CANCELLED.value:
                    self._mirror_cancelled_candidate(session, task_id, now)
                    return _task(row)
                if row.status not in cancellable:
                    raise TaskNotCancellableError(task_id, row.status)
                lease_token = row.lease_token
                changed = cast(
                    "CursorResult[Any]",
                    session.execute(
                    update(TaskRow)
                    .where(
                        TaskRow.id == task_id,
                        TaskRow.version == row.version,
                        TaskRow.status.in_(cancellable),
                    )
                    .values(
                        status=TaskStatus.CANCELLED.value,
                        result_json=None,
                        lease_owner=None,
                        lease_token=None,
                        lease_expires_at=None,
                        next_attempt_at=None,
                        last_error_code="TASK_CANCELLED",
                        cancelled_at=now,
                        cancellation_reason=cancellation_reason,
                        updated_at=now,
                        version=row.version + 1,
                    )
                    .execution_options(synchronize_session=False)
                ))
                if changed.rowcount != 1:
                    continue
                if lease_token is not None:
                    session.execute(
                        update(TaskAttemptRow)
                        .where(
                            TaskAttemptRow.task_id == task_id,
                            TaskAttemptRow.lease_token == lease_token,
                            TaskAttemptRow.finished_at.is_(None),
                        )
                        .values(
                            finished_at=now,
                            outcome="cancelled",
                            error_code="TASK_CANCELLED",
                        )
                    )
                self._mirror_cancelled_candidate(session, task_id, now)
            return self.get(task_id)
        raise TaskNotCancellableError(task_id, "concurrent_update")

    @staticmethod
    def _mirror_cancelled_candidate(
        session: Session, task_id: str, now: datetime
    ) -> None:
        """Keep a durable PCB candidate terminal when its task is cancelled."""
        session.execute(
            update(PcbCandidateRow)
            .where(
                PcbCandidateRow.task_id == task_id,
                PcbCandidateRow.status.not_in(
                    {"released", "cancelled"}
                ),
            )
            .values(
                status="cancelled",
                last_error_code="TASK_CANCELLED",
                updated_at=now,
                version=PcbCandidateRow.version + 1,
            )
            .execution_options(synchronize_session=False)
        )
        active_release_statuses = {"release_pending", "ready_for_g4"}
        release_candidates = session.scalars(
            select(PcbCandidateRow).where(
                PcbCandidateRow.status.in_(active_release_statuses)
            )
        ).all()
        for candidate in release_candidates:
            result = dict(candidate.result_json or {})
            release = result.get("release")
            if not isinstance(release, dict) or release.get("task_id") != task_id:
                continue
            result["release"] = {
                "task_id": task_id,
                "idempotency_key": release.get("idempotency_key"),
                "status": "cancelled",
                "error_code": "TASK_CANCELLED",
            }
            session.execute(
                update(PcbCandidateRow)
                .where(
                    PcbCandidateRow.id == candidate.id,
                    PcbCandidateRow.status.in_(active_release_statuses),
                    PcbCandidateRow.version == candidate.version,
                )
                .values(
                    status="cancelled",
                    result_json=result,
                    last_error_code="TASK_CANCELLED",
                    updated_at=now,
                    version=candidate.version + 1,
                )
                .execution_options(synchronize_session=False)
            )

    @staticmethod
    def _claimable(now: datetime):
        return or_(
            TaskRow.status == TaskStatus.QUEUED.value,
            and_(
                TaskRow.status == TaskStatus.RETRY_WAIT.value,
                TaskRow.next_attempt_at.is_not(None),
                TaskRow.next_attempt_at <= now,
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
                claimed = cast(
                    "CursorResult[Any]",
                    session.execute(
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
                        next_attempt_at=None,
                        attempt_count=attempt_number,
                        updated_at=now,
                        version=old_version + 1,
                    )
                    .execution_options(synchronize_session=False)
                ))
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

    def start(self, task_id: str, lease_token: str, now: datetime) -> None:
        self._transition_with_lease(
            task_id,
            lease_token,
            [TaskStatus.LEASED],
            now,
            status=TaskStatus.RUNNING.value,
            updated_at=now,
        )

    def renew(
        self,
        task_id: str,
        lease_token: str,
        now: datetime,
        lease_seconds: int,
    ) -> datetime:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        lease_expires_at = now + timedelta(seconds=lease_seconds)
        with self._sessions.begin() as session:
            changed = cast(
                "CursorResult[Any]",
                session.execute(
                update(TaskRow)
                .where(
                    TaskRow.id == task_id,
                    TaskRow.lease_token == lease_token,
                    TaskRow.status.in_(
                        [TaskStatus.LEASED.value, TaskStatus.RUNNING.value]
                    ),
                    TaskRow.lease_expires_at.is_not(None),
                    TaskRow.lease_expires_at > now,
                )
                .values(
                    lease_expires_at=lease_expires_at,
                    updated_at=now,
                    version=TaskRow.version + 1,
                )
                .execution_options(synchronize_session=False)
            ))
            if changed.rowcount != 1:
                raise StaleLeaseError(task_id)
        return lease_expires_at

    def complete(
        self,
        task_id: str,
        lease_token: str,
        result: dict[str, Any],
        now: datetime,
    ) -> None:
        json.dumps(result, sort_keys=True, allow_nan=False)
        self._finish_attempt(
            task_id,
            lease_token,
            now,
            "succeeded",
            None,
            status=TaskStatus.SUCCEEDED.value,
            result_json=result,
            last_error_code=None,
            next_attempt_at=None,
        )

    def _retry_delay_seconds(self, attempt_count: int) -> int:
        return min(
            self._retry_max_delay_seconds,
            self._retry_base_seconds * (2 ** max(0, attempt_count - 1)),
        )

    def fail(
        self,
        task_id: str,
        lease_token: str,
        error_code: str,
        retryable: bool,
        now: datetime,
    ) -> None:
        with self._sessions() as session:
            attempt_count = session.scalar(
                select(TaskRow.attempt_count).where(
                    TaskRow.id == task_id,
                    TaskRow.lease_token == lease_token,
                )
            )
        if attempt_count is None:
            raise StaleLeaseError(task_id)
        retry_later = retryable and attempt_count < self._max_attempts
        next_attempt_at = (
            now + timedelta(seconds=self._retry_delay_seconds(attempt_count))
            if retry_later
            else None
        )
        self._finish_attempt(
            task_id,
            lease_token,
            now,
            "retryable_failure" if retryable else "terminal_failure",
            error_code,
            status=(
                TaskStatus.RETRY_WAIT.value
                if retry_later
                else TaskStatus.FAILED_TERMINAL.value
            ),
            result_json=None,
            last_error_code=error_code,
            next_attempt_at=next_attempt_at,
        )

    def _transition_with_lease(
        self,
        task_id: str,
        lease_token: str,
        allowed: list[TaskStatus],
        now: datetime,
        **values: Any,
    ) -> None:
        with self._sessions.begin() as session:
            changed = cast(
                "CursorResult[Any]",
                session.execute(
                update(TaskRow)
                .where(
                    TaskRow.id == task_id,
                    TaskRow.lease_token == lease_token,
                    TaskRow.status.in_([status.value for status in allowed]),
                    TaskRow.lease_expires_at.is_not(None),
                    TaskRow.lease_expires_at > now,
                )
                .values(**values, version=TaskRow.version + 1)
                .execution_options(synchronize_session=False)
            ))
            if changed.rowcount != 1:
                raise StaleLeaseError(task_id)

    def assert_active(self, task_id: str, lease_token: str, now: datetime) -> None:
        with self._sessions() as session:
            _assert_active_fence(session, task_id, lease_token, now)

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
            changed = cast(
                "CursorResult[Any]",
                session.execute(
                update(TaskRow)
                .where(
                    TaskRow.id == task_id,
                    TaskRow.lease_token == lease_token,
                    TaskRow.status.in_(
                        [TaskStatus.LEASED.value, TaskStatus.RUNNING.value]
                    ),
                    TaskRow.lease_expires_at.is_not(None),
                    TaskRow.lease_expires_at > now,
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
            ))
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


class EvidenceRepository:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def add_reports_and_findings(
        self,
        *,
        project_id: str,
        task_id: str,
        reports: Mapping[str, tuple[ArtifactDescriptor, str, str]],
        findings: Mapping[str, tuple[NormalizedFinding, ...]],
        lease_token: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Evidence]:
        """Persist a complete evidence batch under one lease-fenced transaction."""
        if not reports:
            raise EvidenceConflictError("evidence batch must contain a report")
        timestamp = now or utc_now()
        rows: dict[str, EvidenceRow] = {}
        with self._sessions.begin() as session:
            # Serialize publication with cancellation and lease takeover. The
            # fence is checked again before any durable row is written.
            session.execute(text("BEGIN IMMEDIATE"))
            fence_now = max(timestamp, utc_now())
            if lease_token is not None:
                _assert_active_fence(session, task_id, lease_token, fence_now)
            for kind, (descriptor, subject, verdict) in reports.items():
                artifact = session.get(ArtifactRow, descriptor.digest)
                if artifact is None:
                    session.add(
                        ArtifactRow(
                            digest=descriptor.digest,
                            size=descriptor.size,
                            media_type=descriptor.media_type,
                            storage_path=str(descriptor.path),
                            created_at=timestamp,
                        )
                    )
                elif (
                    artifact.size != descriptor.size
                    or artifact.media_type != descriptor.media_type
                    or Path(artifact.storage_path) != descriptor.path
                ):
                    raise EvidenceConflictError(descriptor.digest)

                row = session.scalar(
                    select(EvidenceRow).where(
                        EvidenceRow.task_id == task_id,
                        EvidenceRow.kind == kind,
                    )
                )
                if row is not None:
                    if (
                        row.project_id != project_id
                        or row.artifact_digest != descriptor.digest
                        or row.subject != subject
                        or row.verdict != verdict
                    ):
                        raise EvidenceConflictError(f"{task_id}:{kind}")
                else:
                    row = EvidenceRow(
                        id=new_id("evd"),
                        project_id=project_id,
                        task_id=task_id,
                        kind=kind,
                        artifact_digest=descriptor.digest,
                        subject=subject,
                        verdict=verdict,
                        created_at=timestamp,
                    )
                    session.add(row)
                rows[kind] = row

            session.flush()
            for kind, finding_values in findings.items():
                evidence = rows.get(kind)
                if evidence is None:
                    raise EvidenceConflictError(f"{task_id}:{kind}")
                for finding in finding_values:
                    finding_row = session.scalar(
                        select(FindingRow).where(
                            FindingRow.evidence_id == evidence.id,
                            FindingRow.rule_id == finding.rule_id,
                            FindingRow.subject == finding.subject,
                            FindingRow.message == finding.message,
                        )
                    )
                    if finding_row is None:
                        session.add(
                            FindingRow(
                                id=new_id("fnd"),
                                project_id=project_id,
                                task_id=task_id,
                                evidence_id=evidence.id,
                                rule_id=finding.rule_id,
                                severity=finding.severity,
                                subject=finding.subject,
                                message=finding.message,
                                status="open",
                                created_at=timestamp,
                            )
                        )
                    elif finding_row.severity != finding.severity:
                        raise EvidenceConflictError(finding.rule_id)
            session.flush()
            return {kind: _evidence(row) for kind, row in rows.items()}

    def settle_publication(
        self,
        descriptors: Sequence[ArtifactDescriptor],
        staged: Sequence[StagedArtifact],
    ) -> None:
        """Safely settle published evidence objects against durable rows."""
        if len(descriptors) != len(staged):
            raise ValueError("evidence publication descriptor/staging mismatch")
        pairs = tuple(zip(descriptors, staged, strict=True))
        for descriptor, artifact in pairs:
            if (
                descriptor.digest != artifact.digest
                or descriptor.size != artifact.size
                or descriptor.media_type != artifact.media_type
            ):
                raise ValueError("evidence publication descriptor/staging mismatch")
        with self._sessions.begin() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            for descriptor, artifact in pairs:
                if session.get(ArtifactRow, descriptor.digest) is not None:
                    artifact.discard()
                else:
                    artifact.rollback()

    def add_report(
        self,
        project_id: str,
        task_id: str,
        descriptor: ArtifactDescriptor,
        kind: str,
        subject: str,
        verdict: str,
        *,
        lease_token: str | None = None,
        now: datetime | None = None,
    ) -> Evidence:
        with self._sessions.begin() as session:
            if lease_token is not None:
                _assert_active_fence(
                    session, task_id, lease_token, now or utc_now()
                )
            artifact = session.get(ArtifactRow, descriptor.digest)
            if artifact is None:
                session.add(
                    ArtifactRow(
                        digest=descriptor.digest,
                        size=descriptor.size,
                        media_type=descriptor.media_type,
                        storage_path=str(descriptor.path),
                        created_at=utc_now(),
                    )
                )
            elif (
                artifact.size != descriptor.size
                or artifact.media_type != descriptor.media_type
                or Path(artifact.storage_path) != descriptor.path
            ):
                raise EvidenceConflictError(descriptor.digest)

            row = session.scalar(
                select(EvidenceRow).where(
                    EvidenceRow.task_id == task_id,
                    EvidenceRow.kind == kind,
                )
            )
            if row is not None:
                if (
                    row.project_id != project_id
                    or row.artifact_digest != descriptor.digest
                    or row.subject != subject
                    or row.verdict != verdict
                ):
                    raise EvidenceConflictError(f"{task_id}:{kind}")
                return _evidence(row)
            row = EvidenceRow(
                id=new_id("evd"),
                project_id=project_id,
                task_id=task_id,
                kind=kind,
                artifact_digest=descriptor.digest,
                subject=subject,
                verdict=verdict,
                created_at=utc_now(),
            )
            session.add(row)
        return _evidence(row)

    def list_for_project(self, project_id: str) -> list[Evidence]:
        with self._sessions() as session:
            rows = session.scalars(
                select(EvidenceRow)
                .where(EvidenceRow.project_id == project_id)
                .order_by(EvidenceRow.created_at, EvidenceRow.id)
            ).all()
            return [_evidence(row) for row in rows]

    def artifact_media_type(self, digest: str) -> str | None:
        with self._sessions() as session:
            row = session.get(ArtifactRow, digest)
            return row.media_type if row is not None else None

    def evidence_set_matches_registered_artifacts(
        self,
        *,
        project_id: str,
        task_id: str,
        subject: str,
        evidence_set_kind: str,
        evidence_set_digest: str,
        evidence_set_media_type: str,
        items: Mapping[str, tuple[str, str, str]],
    ) -> bool:
        """Verify the durable evidence rows and artifact media types for a set."""
        required_kinds = set(items) | {evidence_set_kind}
        with self._sessions() as session:
            rows = session.scalars(
                select(EvidenceRow).where(
                    EvidenceRow.project_id == project_id,
                    EvidenceRow.task_id == task_id,
                )
            ).all()
            by_kind = {row.kind: row for row in rows}
            if set(by_kind) != required_kinds or len(rows) != len(required_kinds):
                return False
            evidence_set_row = by_kind[evidence_set_kind]
            evidence_set_artifact = session.get(
                ArtifactRow, evidence_set_row.artifact_digest
            )
            if (
                evidence_set_row.artifact_digest != evidence_set_digest
                or evidence_set_row.subject != subject
                or evidence_set_row.verdict != "pass"
                or evidence_set_artifact is None
                or evidence_set_artifact.media_type != evidence_set_media_type
            ):
                return False
            for kind, (digest, media_type, verdict) in items.items():
                row = by_kind[kind]
                artifact = session.get(ArtifactRow, row.artifact_digest)
                if (
                    row.artifact_digest != digest
                    or row.subject != subject
                    or row.verdict != verdict
                    or artifact is None
                    or artifact.media_type != media_type
                ):
                    return False
        return True


class FindingRepository:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def add_many(
        self,
        project_id: str,
        task_id: str,
        evidence_id: str,
        findings: tuple[NormalizedFinding, ...],
        *,
        lease_token: str | None = None,
        now: datetime | None = None,
    ) -> list[Finding]:
        result: list[Finding] = []
        with self._sessions.begin() as session:
            if lease_token is not None:
                _assert_active_fence(
                    session, task_id, lease_token, now or utc_now()
                )
            evidence = session.get(EvidenceRow, evidence_id)
            if (
                evidence is None
                or evidence.project_id != project_id
                or evidence.task_id != task_id
            ):
                raise EvidenceConflictError(evidence_id)
            for finding in findings:
                row = session.scalar(
                    select(FindingRow).where(
                        FindingRow.evidence_id == evidence_id,
                        FindingRow.rule_id == finding.rule_id,
                        FindingRow.subject == finding.subject,
                        FindingRow.message == finding.message,
                    )
                )
                if row is None:
                    row = FindingRow(
                        id=new_id("fnd"),
                        project_id=project_id,
                        task_id=task_id,
                        evidence_id=evidence_id,
                        rule_id=finding.rule_id,
                        severity=finding.severity,
                        subject=finding.subject,
                        message=finding.message,
                        status="open",
                        created_at=utc_now(),
                    )
                    session.add(row)
                elif row.severity != finding.severity:
                    raise EvidenceConflictError(finding.rule_id)
                result.append(_finding(row))
        return result

    def list_for_project(self, project_id: str) -> list[Finding]:
        with self._sessions() as session:
            rows = session.scalars(
                select(FindingRow)
                .where(FindingRow.project_id == project_id)
                .order_by(FindingRow.created_at, FindingRow.id)
            ).all()
            return [_finding(row) for row in rows]

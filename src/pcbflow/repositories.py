from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from pcbflow.artifacts import ArtifactDescriptor
from pcbflow.design_tables import OutboxEventRow, ProjectRevisionRow
from pcbflow.domain import (
    Evidence,
    Finding,
    NormalizedFinding,
    Project,
    ProjectMode,
    Task,
    TaskLease,
    TaskStatus,
    new_id,
    utc_now,
)
from pcbflow.observability import audit_payload
from pcbflow.tables import (
    ArtifactRow,
    EvidenceRow,
    FindingRow,
    ProjectRow,
    TaskAttemptRow,
    TaskRow,
)


class ProjectNotFoundError(LookupError):
    pass


class IdempotencyConflictError(RuntimeError):
    pass


class RevisionConflictError(RuntimeError):
    def __init__(self, expected: str | None, actual: str | None) -> None:
        super().__init__(f"expected {expected}, found {actual}")
        self.expected = expected
        self.actual = actual


class TaskNotFoundError(LookupError):
    pass


class StaleLeaseError(RuntimeError):
    pass


class EvidenceConflictError(RuntimeError):
    pass


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _project(row: ProjectRow) -> Project:
    return Project(
        id=row.id,
        name=row.name,
        source_path=Path(row.source_path),
        created_at=_utc(row.created_at),
        mode=ProjectMode(row.mode),
        managed_repo_key=row.managed_repo_key,
        current_revision=row.current_revision,
        project_snapshot_digest=row.project_snapshot_digest,
        active_requirement_set_id=row.active_requirement_set_id,
        adoption_idempotency_key=row.adoption_idempotency_key,
        adoption_input_digest=row.adoption_input_digest,
        managed_at=_utc(row.managed_at) if row.managed_at is not None else None,
        version=row.version,
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


def _evidence(row: EvidenceRow) -> Evidence:
    return Evidence(
        id=row.id,
        project_id=row.project_id,
        task_id=row.task_id,
        kind=row.kind,
        artifact_digest=row.artifact_digest,
        subject=row.subject,
        verdict=row.verdict,
        created_at=_utc(row.created_at),
    )


def _finding(row: FindingRow) -> Finding:
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
        created_at=_utc(row.created_at),
    )


class ProjectRepository:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def create(self, name: str, source_path: Path, idempotency_key: str) -> Project:
        project, _created = self.create_with_status(
            name, source_path, idempotency_key
        )
        return project

    def create_with_status(
        self, name: str, source_path: Path, idempotency_key: str
    ) -> tuple[Project, bool]:
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
                return _project(row), False
            row = ProjectRow(
                id=new_id("prj"),
                name=name,
                source_path=str(resolved),
                idempotency_key=idempotency_key,
                created_at=utc_now(),
            )
            session.add(row)
        return _project(row), True

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
                changed = session.execute(
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
                )
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
            changed = session.execute(
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
            )
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

    def start(self, task_id: str, lease_token: str, now: datetime) -> None:
        self._transition_with_lease(
            task_id,
            lease_token,
            [TaskStatus.LEASED],
            now,
            status=TaskStatus.RUNNING.value,
            updated_at=now,
        )

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
        )

    def fail(
        self,
        task_id: str,
        lease_token: str,
        error_code: str,
        retryable: bool,
        now: datetime,
    ) -> None:
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
        now: datetime,
        **values: Any,
    ) -> None:
        with self._sessions.begin() as session:
            changed = session.execute(
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
            )
            if changed.rowcount != 1:
                raise StaleLeaseError(task_id)

    def assert_active(self, task_id: str, lease_token: str, now: datetime) -> None:
        with self._sessions() as session:
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


class EvidenceRepository:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def add_report(
        self,
        project_id: str,
        task_id: str,
        descriptor: ArtifactDescriptor,
        kind: str,
        subject: str,
        verdict: str,
    ) -> Evidence:
        with self._sessions.begin() as session:
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


class FindingRepository:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def add_many(
        self,
        project_id: str,
        task_id: str,
        evidence_id: str,
        findings: tuple[NormalizedFinding, ...],
    ) -> list[Finding]:
        result: list[Finding] = []
        with self._sessions.begin() as session:
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

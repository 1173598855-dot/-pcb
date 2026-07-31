from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import select, text, update
from sqlalchemy.orm import Session, sessionmaker

from pcbflow.canonical import canonical_digest, canonical_json_bytes
from pcbflow.commands import CommandBatch, command_batch_digest
from pcbflow.design_tables import (
    ChangeProposalRow,
    DesignCommandBatchRow,
    DesignCommandRow,
    OutboxEventRow,
)
from pcbflow.domain import TaskStatus, new_id, utc_now
from pcbflow.proposals import (
    DESIGN_PROPOSAL_TASK_KIND,
    ChangeProposal,
    ProposalStatus,
    READY_EVIDENCE_KINDS,
    READY_EVIDENCE_MEDIA_TYPES,
)
from pcbflow.repositories import IdempotencyConflictError, StaleLeaseError, EvidenceConflictError
from pcbflow.tables import ArtifactRow, EvidenceRow, TaskRow
from pcbflow.proposals import EvidenceRegistration, EvidenceSet


class CommandBatchNotFoundError(LookupError):
    pass


class ProposalNotFoundError(LookupError):
    pass


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _proposal(row: ChangeProposalRow) -> ChangeProposal:
    return ChangeProposal(
        id=row.id,
        project_id=row.project_id,
        command_batch_id=row.command_batch_id,
        task_id=row.task_id,
        status=ProposalStatus(row.status),
        candidate_revision=row.candidate_revision,
        candidate_snapshot_digest=row.candidate_snapshot_digest,
        review_digest=row.review_digest,
        semantic_diff_digest=row.semantic_diff_digest,
        evidence_set_digest=row.evidence_set_digest,
        result=dict(row.result_json) if row.result_json is not None else None,
        last_error_code=row.last_error_code,
        created_at=_utc(row.created_at),
        updated_at=_utc(row.updated_at),
        version=row.version,
    )


def _batch(row: DesignCommandBatchRow) -> CommandBatch:
    value = {
        "schema_version": "1.0",
        "batch_id": row.id,
        "project_id": row.project_id,
        "base_revision": row.base_revision,
        "requirement_set_id": row.requirement_set_id,
        "idempotency_key": row.idempotency_key,
        "actor": row.actor_json,
        "intent": row.intent,
        "risk": row.risk,
        "commands": row.commands_json,
    }
    try:
        batch = CommandBatch.model_validate_json(
            canonical_json_bytes(value), strict=True
        )
    except ValueError as error:
        raise RuntimeError("stored command batch is invalid") from error
    if command_batch_digest(batch) != row.canonical_digest:
        raise RuntimeError("stored command batch digest mismatch")
    return batch


class CommandBatchStore:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def get(self, batch_id: str) -> CommandBatch:
        with self._sessions() as session:
            row = session.get(DesignCommandBatchRow, batch_id)
            if row is None:
                raise CommandBatchNotFoundError(batch_id)
            return _batch(row)

    def created_at(self, batch_id: str) -> datetime:
        with self._sessions() as session:
            row = session.get(DesignCommandBatchRow, batch_id)
            if row is None:
                raise CommandBatchNotFoundError(batch_id)
            return _utc(row.created_at)


class ProposalStore:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._sessions = sessions
        self._clock = clock

    @staticmethod
    def _existing(
        session: Session, batch: CommandBatch, batch_json: dict[str, object], digest: str
    ) -> ChangeProposal | None:
        existing = session.scalar(
            select(DesignCommandBatchRow).where(
                DesignCommandBatchRow.project_id == batch.project_id,
                DesignCommandBatchRow.idempotency_key == batch.idempotency_key,
            )
        )
        if existing is None:
            return None
        if (
            existing.canonical_digest != digest
            or existing.commands_json != batch_json["commands"]
        ):
            raise IdempotencyConflictError(batch.idempotency_key)
        proposal = session.scalar(
            select(ChangeProposalRow).where(
                ChangeProposalRow.command_batch_id == existing.id
            )
        )
        if proposal is None:
            raise RuntimeError("command batch exists without proposal")
        return _proposal(proposal)

    def get(self, proposal_id: str) -> ChangeProposal:
        with self._sessions() as session:
            row = session.get(ChangeProposalRow, proposal_id)
            if row is None:
                raise ProposalNotFoundError(proposal_id)
            return _proposal(row)

    def find_existing(self, batch: CommandBatch) -> ChangeProposal | None:
        batch_json = batch.model_dump(mode="json")
        digest = command_batch_digest(batch)
        with self._sessions() as session:
            return self._existing(session, batch, batch_json, digest)

    def create_queued(self, batch: CommandBatch) -> ChangeProposal:
        batch_json = batch.model_dump(mode="json")
        digest = command_batch_digest(batch)
        with self._sessions.begin() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            existing = self._existing(session, batch, batch_json, digest)
            if existing is not None:
                return existing

            now = self._clock()
            batch_row = DesignCommandBatchRow(
                id=batch.batch_id,
                project_id=batch.project_id,
                requirement_set_id=batch.requirement_set_id,
                base_revision=batch.base_revision,
                idempotency_key=batch.idempotency_key,
                actor_json=batch.actor.model_dump(mode="json"),
                intent=batch.intent,
                risk=batch.risk.value,
                commands_json=batch_json["commands"],
                canonical_digest=digest,
                created_at=now,
            )
            session.add(batch_row)
            task_id = new_id("tsk")
            proposal_id = new_id("prop")
            session.add(
                TaskRow(
                    id=task_id,
                    project_id=batch.project_id,
                    kind=DESIGN_PROPOSAL_TASK_KIND,
                    payload_json={
                        "proposal_id": proposal_id,
                        "command_batch_id": batch.batch_id,
                        "project_id": batch.project_id,
                    },
                    result_json=None,
                    status=TaskStatus.QUEUED.value,
                    idempotency_key=f"proposal:{batch.project_id}:{batch.idempotency_key}",
                    lease_owner=None,
                    lease_token=None,
                    lease_expires_at=None,
                    last_error_code=None,
                    attempt_count=0,
                    created_at=now,
                    updated_at=now,
                    version=1,
                )
            )
            session.flush()
            for ordinal, command in enumerate(batch.commands):
                command_json = command.model_dump(mode="json")
                session.add(
                    DesignCommandRow(
                        id=command.command_id,
                        batch_id=batch.batch_id,
                        project_id=batch.project_id,
                        ordinal=ordinal,
                        idempotency_key=command.idempotency_key,
                        operation_type=command.operation.type,
                        payload_json=command_json,
                        canonical_digest=canonical_digest(command_json),
                    )
                )

            proposal = ChangeProposalRow(
                id=proposal_id,
                project_id=batch.project_id,
                command_batch_id=batch.batch_id,
                task_id=task_id,
                status=ProposalStatus.QUEUED.value,
                candidate_revision=None,
                candidate_snapshot_digest=None,
                review_digest=None,
                semantic_diff_digest=None,
                evidence_set_digest=None,
                result_json=None,
                last_error_code=None,
                created_at=now,
                updated_at=now,
                version=1,
            )
            session.add(proposal)
            session.add(
                OutboxEventRow(
                    id=new_id("evt"),
                    aggregate_type="change_proposal",
                    aggregate_id=proposal_id,
                    event_type="proposal.queued",
                    payload_json={
                        "project_id": batch.project_id,
                        "command_batch_id": batch.batch_id,
                        "task_id": task_id,
                    },
                    created_at=now,
                    processed_at=None,
                    attempt_count=0,
                    last_error_code=None,
                )
            )
            session.flush()
            return _proposal(proposal)

    @staticmethod
    def _assert_active_fence(session: Session, task_id: str, lease_token: str, now: datetime) -> None:
        active = session.scalar(select(TaskRow.id).where(TaskRow.id == task_id,
            TaskRow.lease_token == lease_token, TaskRow.status == TaskStatus.RUNNING.value,
            TaskRow.lease_expires_at.is_not(None), TaskRow.lease_expires_at > now))
        if active is None:
            raise StaleLeaseError(task_id)

    @staticmethod
    def _register_evidence(session: Session, proposal: ChangeProposalRow,
                           registrations: tuple[EvidenceRegistration, ...], now: datetime,
                           subject: str) -> None:
        for registration in registrations:
            descriptor, item = registration.descriptor, registration.item
            artifact = session.get(ArtifactRow, descriptor.digest)
            if artifact is None:
                session.add(ArtifactRow(digest=descriptor.digest, size=descriptor.size,
                    media_type=descriptor.media_type, storage_path=str(descriptor.path), created_at=now))
            elif (artifact.size, artifact.media_type, artifact.storage_path) != (descriptor.size, descriptor.media_type, str(descriptor.path)):
                raise EvidenceConflictError(descriptor.digest)
            existing = session.scalar(select(EvidenceRow).where(EvidenceRow.task_id == proposal.task_id, EvidenceRow.kind == item.kind))
            if existing is None:
                session.add(EvidenceRow(id=new_id("evd"), project_id=proposal.project_id,
                    task_id=proposal.task_id, kind=item.kind, artifact_digest=item.artifact_digest,
                    subject=subject, verdict=item.verdict, created_at=now))
            elif (existing.project_id, existing.artifact_digest, existing.subject, existing.verdict) != (proposal.project_id, item.artifact_digest, subject, item.verdict):
                raise EvidenceConflictError(f"{proposal.task_id}:{item.kind}")

    def begin_execution(self, proposal_id: str, task_id: str, lease_token: str, now: datetime) -> ChangeProposal:
        with self._sessions.begin() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            self._assert_active_fence(session, task_id, lease_token, now)
            row = session.get(ChangeProposalRow, proposal_id)
            if row is None or row.task_id != task_id:
                raise ProposalNotFoundError(proposal_id)
            if row.status not in (ProposalStatus.QUEUED.value, ProposalStatus.EXECUTING.value):
                return _proposal(row)
            changed = session.execute(update(ChangeProposalRow).where(ChangeProposalRow.id == proposal_id,
                ChangeProposalRow.task_id == task_id, ChangeProposalRow.version == row.version,
                ChangeProposalRow.status.in_([ProposalStatus.QUEUED.value, ProposalStatus.EXECUTING.value])).values(
                    status=ProposalStatus.EXECUTING.value, updated_at=now, version=ChangeProposalRow.version + 1))
            if changed.rowcount != 1:
                raise StaleLeaseError(task_id)
            session.expire_all(); refreshed = session.get(ChangeProposalRow, proposal_id); assert refreshed is not None
            return _proposal(refreshed)

    def mark_validation_failed(self, proposal_id: str, task_id: str, lease_token: str, now: datetime,
                               error_code: str, semantic_diff_digest: str | None,
                               evidence_set_digest: str, result: dict[str, object],
                               evidence: tuple[EvidenceRegistration, ...]) -> ChangeProposal:
        with self._sessions.begin() as session:
            session.execute(text("BEGIN IMMEDIATE")); self._assert_active_fence(session, task_id, lease_token, now)
            row = session.get(ChangeProposalRow, proposal_id)
            if row is None or row.task_id != task_id: raise ProposalNotFoundError(proposal_id)
            if row.status != ProposalStatus.VALIDATION_FAILED.value:
                if row.status not in (ProposalStatus.EXECUTING.value, ProposalStatus.QUEUED.value): raise StaleLeaseError(task_id)
                changed = session.execute(update(ChangeProposalRow).where(ChangeProposalRow.id == proposal_id, ChangeProposalRow.version == row.version).values(
                    status=ProposalStatus.VALIDATION_FAILED.value, last_error_code=error_code,
                    semantic_diff_digest=semantic_diff_digest, evidence_set_digest=evidence_set_digest,
                    result_json=result, updated_at=now, version=ChangeProposalRow.version + 1))
                if changed.rowcount != 1: raise StaleLeaseError(task_id)
            self._register_evidence(session, row, evidence, now, proposal_id)
            session.add(OutboxEventRow(id=new_id("evt"), aggregate_type="change_proposal", aggregate_id=proposal_id,
                event_type="proposal.validation_failed", payload_json={"project_id": row.project_id, "task_id": task_id, "error_code": error_code}, created_at=now, processed_at=None, attempt_count=0, last_error_code=None))
            session.expire_all(); refreshed = session.get(ChangeProposalRow, proposal_id); assert refreshed is not None
            return _proposal(refreshed)

    def mark_ready(self, proposal_id: str, task_id: str, lease_token: str, now: datetime,
                   candidate_revision: str, candidate_snapshot_digest: str, review_digest: str,
                   semantic_diff_digest: str, evidence_set_digest: str, result: dict[str, object],
                   evidence: tuple[EvidenceRegistration, ...]) -> ChangeProposal:
        with self._sessions.begin() as session:
            session.execute(text("BEGIN IMMEDIATE")); self._assert_active_fence(session, task_id, lease_token, now)
            row = session.get(ChangeProposalRow, proposal_id)
            if row is None or row.task_id != task_id: raise ProposalNotFoundError(proposal_id)
            if row.status == ProposalStatus.READY_FOR_REVIEW.value: return _proposal(row)
            if row.status != ProposalStatus.EXECUTING.value: raise StaleLeaseError(task_id)
            kinds = {r.item.kind for r in evidence}
            if len(evidence) != len(READY_EVIDENCE_KINDS) or kinds != READY_EVIDENCE_KINDS:
                raise ValueError("incomplete proposal evidence")
            evidence_set_registration = next(r for r in evidence if r.item.kind == "proposal_evidence_set")
            if (evidence_set_registration.descriptor.digest != evidence_set_digest
                    or evidence_set_registration.item.verdict != "pass"
                    or evidence_set_registration.item.media_type != "application/json"):
                raise ValueError("proposal evidence set digest mismatch")
            try:
                evidence_set = EvidenceSet.model_validate_json(
                    evidence_set_registration.descriptor.path.read_bytes(), strict=True
                )
            except Exception as error:
                raise ValueError("invalid proposal evidence set") from error
            batch_row = session.get(DesignCommandBatchRow, row.command_batch_id)
            if (batch_row is None or evidence_set.project_id != row.project_id
                    or evidence_set.task_id != row.task_id or evidence_set.proposal_id != row.id
                    or evidence_set.base_revision != batch_row.base_revision
                    or evidence_set.candidate_revision != candidate_revision):
                raise ValueError("proposal evidence set binding mismatch")
            evidence_items = {item.kind: item for item in evidence_set.artifacts}
            registrations = {item.item.kind: item for item in evidence}
            if (len(evidence_set.artifacts) != len(READY_EVIDENCE_MEDIA_TYPES)
                    or len(evidence_items) != len(READY_EVIDENCE_MEDIA_TYPES)
                    or set(evidence_items) != set(READY_EVIDENCE_MEDIA_TYPES)
                    or any(
                        item.media_type != READY_EVIDENCE_MEDIA_TYPES[kind]
                        or item.verdict != "pass"
                        or registrations[kind].item != item
                        for kind, item in evidence_items.items()
                    )):
                raise ValueError("invalid proposal evidence set contents")
            self._register_evidence(session, row, evidence, now, f"{proposal_id}@{candidate_revision}")
            changed = session.execute(update(ChangeProposalRow).where(ChangeProposalRow.id == proposal_id, ChangeProposalRow.task_id == task_id, ChangeProposalRow.version == row.version).values(
                status=ProposalStatus.READY_FOR_REVIEW.value, candidate_revision=candidate_revision,
                candidate_snapshot_digest=candidate_snapshot_digest, review_digest=review_digest,
                semantic_diff_digest=semantic_diff_digest, evidence_set_digest=evidence_set_digest,
                result_json=result, updated_at=now, version=ChangeProposalRow.version + 1))
            if changed.rowcount != 1: raise StaleLeaseError(task_id)
            session.add(OutboxEventRow(id=new_id("evt"), aggregate_type="change_proposal", aggregate_id=proposal_id,
                event_type="proposal.ready_for_review", payload_json={"project_id": row.project_id, "task_id": task_id, "candidate_revision": candidate_revision}, created_at=now, processed_at=None, attempt_count=0, last_error_code=None))
            session.expire_all(); refreshed = session.get(ChangeProposalRow, proposal_id); assert refreshed is not None
            return _proposal(refreshed)

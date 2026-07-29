from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import select, text
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
)
from pcbflow.repositories import IdempotencyConflictError
from pcbflow.tables import TaskRow


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

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import and_, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from pcbflow.artifacts import ArtifactDescriptor
from pcbflow.canonical import canonical_digest, canonical_json_bytes
from pcbflow.commands import CommandBatch, command_batch_digest
from pcbflow.design_tables import (
    ChangeProposalRow,
    DesignCommandBatchRow,
    DesignCommandRow,
    GateDecisionRow,
    OutboxEventRow,
    ProjectRevisionRow,
)
from pcbflow.domain import RequestInvalidError, TaskStatus, new_id, utc_now
from pcbflow.observability import audit_payload, ensure_trace_id
from pcbflow.proposals import (
    DESIGN_PROPOSAL_TASK_KIND,
    READY_EVIDENCE_KINDS,
    READY_EVIDENCE_MEDIA_TYPES,
    ChangeProposal,
    EvidenceRegistration,
    EvidenceSet,
    ProposalStatus,
)
from pcbflow.repositories import (
    EvidenceConflictError,
    IdempotencyConflictError,
    RevisionConflictError,
    StaleLeaseError,
)
from pcbflow.tables import ArtifactRow, EvidenceRow, ProjectRow, TaskRow


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

    def list_for_project(self, project_id: str) -> tuple[ChangeProposal, ...]:
        with self._sessions() as session:
            rows = session.scalars(
                select(ChangeProposalRow).where(
                    ChangeProposalRow.project_id == project_id
                )
            ).all()
            return tuple(_proposal(row) for row in rows)

    def evidence_items_match_registered_artifacts(self, items) -> bool:
        with self._sessions() as session:
            for item in items:
                artifact = session.get(ArtifactRow, item.artifact_digest)
                if artifact is None or artifact.media_type != item.media_type:
                    return False
        return True

    def decision_for_key(self, project_id: str, idempotency_key: str) -> dict[str, str] | None:
        with self._sessions() as session:
            row = session.scalar(select(GateDecisionRow).where(
                GateDecisionRow.project_id == project_id,
                GateDecisionRow.idempotency_key == idempotency_key,
            ))
            if row is None:
                return None
            return {
                "subject_id": row.subject_id,
                "subject_digest": row.subject_digest,
                "base_revision": row.base_revision,
                "decision": row.decision,
                "actor_type": row.actor_type,
                "actor_id": row.actor_id,
                "comment": row.comment,
            }

    def find_existing(self, batch: CommandBatch) -> ChangeProposal | None:
        batch_json = batch.model_dump(mode="json")
        digest = command_batch_digest(batch)
        with self._sessions() as session:
            return self._existing(session, batch, batch_json, digest)

    @staticmethod
    def _conflicting_command_key(session: Session, batch: CommandBatch) -> str | None:
        # Two IN lists plus project_id stay below SQLite's guaranteed 999 binds.
        chunk_size = 400
        for start in range(0, len(batch.commands), chunk_size):
            commands = batch.commands[start : start + chunk_size]
            keys = [command.idempotency_key for command in commands]
            command_ids = [command.command_id for command in commands]
            rows = session.scalars(
                select(DesignCommandRow).where(
                    or_(
                        and_(
                            DesignCommandRow.project_id == batch.project_id,
                            DesignCommandRow.idempotency_key.in_(keys),
                        ),
                        DesignCommandRow.id.in_(command_ids),
                    )
                )
            ).all()
            existing_keys = {
                row.idempotency_key
                for row in rows
                if row.project_id == batch.project_id
            }
            existing_ids = {row.id for row in rows}
            for command in commands:
                if (
                    command.idempotency_key in existing_keys
                    or command.command_id in existing_ids
                ):
                    return command.idempotency_key
        return None

    def create_queued(self, batch: CommandBatch) -> ChangeProposal:
        try:
            return self._create_queued(batch)
        except IntegrityError:
            # Unique-key races are expected at this boundary.  Re-read the
            # winner and preserve the domain-level idempotency contract.
            batch_json = batch.model_dump(mode="json")
            digest = command_batch_digest(batch)
            with self._sessions() as session:
                existing = self._existing(session, batch, batch_json, digest)
                if existing is not None:
                    return existing
                conflict_key = self._conflicting_command_key(session, batch)
                if conflict_key is not None:
                    raise IdempotencyConflictError(conflict_key)
            raise

    def _create_queued(self, batch: CommandBatch) -> ChangeProposal:
        batch_json = batch.model_dump(mode="json")
        digest = command_batch_digest(batch)
        with self._sessions.begin() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            existing = self._existing(session, batch, batch_json, digest)
            if existing is not None:
                return existing

            conflict_key = self._conflicting_command_key(session, batch)
            if conflict_key is not None:
                raise IdempotencyConflictError(conflict_key)

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
                        "trace_id": ensure_trace_id(),
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
            queued_payload = audit_payload(
                actor_type=batch.actor.type,
                actor_id=batch.actor.id,
                action="proposal.create",
                object_type="change_proposal",
                object_id=proposal_id,
                before_digest=None,
                after_digest=digest,
                result="queued",
            )
            queued_payload.update(
                {
                    "project_id": batch.project_id,
                    "command_batch_id": batch.batch_id,
                    "task_id": task_id,
                }
            )
            session.add(
                OutboxEventRow(
                    id=new_id("evt"),
                    aggregate_type="change_proposal",
                    aggregate_id=proposal_id,
                    event_type="proposal.queued",
                    payload_json=queued_payload,
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
            batch = session.get(DesignCommandBatchRow, row.command_batch_id)
            payload = audit_payload(
                actor_type="service",
                actor_id="pcbflow",
                action="proposal.validate",
                object_type="change_proposal",
                object_id=proposal_id,
                before_digest=batch.canonical_digest if batch else None,
                after_digest=evidence_set_digest,
                result="failed",
            )
            payload.update({"project_id": row.project_id, "task_id": task_id, "error_code": error_code})
            session.add(OutboxEventRow(id=new_id("evt"), aggregate_type="change_proposal", aggregate_id=proposal_id,
                event_type="proposal.validation_failed", payload_json=payload, created_at=now, processed_at=None, attempt_count=0, last_error_code=None))
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
                    or evidence_set_registration.item.media_type != evidence_set_registration.descriptor.media_type):
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
                        not item.media_type
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
            payload = audit_payload(
                actor_type="service",
                actor_id="pcbflow",
                action="proposal.execute",
                object_type="change_proposal",
                object_id=proposal_id,
                before_digest=batch_row.canonical_digest,
                after_digest=review_digest,
                result="ready_for_review",
            )
            payload.update({"project_id": row.project_id, "task_id": task_id, "candidate_revision": candidate_revision})
            session.add(OutboxEventRow(id=new_id("evt"), aggregate_type="change_proposal", aggregate_id=proposal_id,
                event_type="proposal.ready_for_review", payload_json=payload, created_at=now, processed_at=None, attempt_count=0, last_error_code=None))
            session.expire_all(); refreshed = session.get(ChangeProposalRow, proposal_id); assert refreshed is not None
            return _proposal(refreshed)

    @staticmethod
    def _decision_matches(
        row: GateDecisionRow,
        *,
        subject_id: str,
        subject_digest: str,
        base_revision: str,
        idempotency_key: str,
        decision: str,
        actor_type: str,
        actor_id: str,
        comment: str,
    ) -> bool:
        return (
            row.gate == "DESIGN_CHANGE"
            and row.subject_type == "change_proposal"
            and row.subject_id == subject_id
            and row.subject_digest == subject_digest
            and row.base_revision == base_revision
            and row.idempotency_key == idempotency_key
            and row.decision == decision
            and row.actor_type == actor_type
            and row.actor_id == actor_id
            and row.comment == comment
        )

    @staticmethod
    def _register_decision_artifact(
        session: Session,
        descriptor: ArtifactDescriptor,
        created_at: datetime,
    ) -> None:
        artifact = session.get(ArtifactRow, descriptor.digest)
        if artifact is None:
            session.add(ArtifactRow(
                digest=descriptor.digest,
                size=descriptor.size,
                media_type=descriptor.media_type,
                storage_path=str(descriptor.path),
                created_at=created_at,
            ))
        elif (artifact.size, artifact.media_type, artifact.storage_path) != (
            descriptor.size, descriptor.media_type, str(descriptor.path)
        ):
            raise EvidenceConflictError(descriptor.digest)

    def decide_accept(
        self,
        *,
        proposal_id: str,
        candidate_revision: str,
        base_revision: str,
        expected_project_version: int,
        candidate_snapshot_digest: str,
        subject_digest: str,
        idempotency_key: str,
        actor_type: str,
        actor_id: str,
        comment: str,
        approval_artifact: ArtifactDescriptor,
        now: datetime,
    ) -> ChangeProposal:
        stale = False
        stale_expected: str | None = None
        with self._sessions.begin() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row = session.get(ChangeProposalRow, proposal_id)
            if row is None:
                raise ProposalNotFoundError(proposal_id)
            existing = session.scalar(select(GateDecisionRow).where(
                GateDecisionRow.project_id == row.project_id,
                GateDecisionRow.idempotency_key == idempotency_key,
            ))
            if existing is not None:
                if (
                    existing.decision == "stale"
                    and self._decision_matches(
                        existing, subject_id=proposal_id, subject_digest=subject_digest,
                        base_revision=base_revision, idempotency_key=idempotency_key,
                        decision="stale", actor_type=actor_type, actor_id=actor_id,
                        comment=comment,
                    )
                ):
                    project = session.get(ProjectRow, row.project_id)
                    raise RevisionConflictError(
                        base_revision,
                        project.current_revision if project is not None else None,
                    )
                if not self._decision_matches(
                    existing, subject_id=proposal_id, subject_digest=subject_digest,
                    base_revision=base_revision, idempotency_key=idempotency_key,
                    decision="approve", actor_type=actor_type, actor_id=actor_id,
                    comment=comment,
                ):
                    raise IdempotencyConflictError(idempotency_key)
                return _proposal(row)
            if row.status != ProposalStatus.READY_FOR_REVIEW.value:
                raise RequestInvalidError("candidate is not reviewable")
            project = session.get(ProjectRow, row.project_id)
            batch = session.get(DesignCommandBatchRow, row.command_batch_id)
            if project is None or batch is None:
                raise ProposalNotFoundError(proposal_id)
            if project.current_revision != base_revision:
                session.add(GateDecisionRow(
                    id=new_id("gdec"), project_id=row.project_id,
                    gate="DESIGN_CHANGE", subject_type="change_proposal",
                    subject_id=proposal_id, subject_digest=subject_digest,
                    base_revision=base_revision, idempotency_key=idempotency_key,
                    decision="stale", actor_type=actor_type, actor_id=actor_id,
                    comment=comment, created_at=now,
                ))
                row.status = ProposalStatus.STALE.value
                row.updated_at = now
                row.version += 1
                stale_payload = audit_payload(
                    actor_type=actor_type,
                    actor_id=actor_id,
                    action="proposal.accept",
                    object_type="change_proposal",
                    object_id=proposal_id,
                    before_digest=subject_digest,
                    after_digest=None,
                    result="stale",
                )
                stale_payload.update({"project_id": row.project_id, "proposal_id": proposal_id, "base_revision": base_revision, "current_revision": project.current_revision})
                session.add(OutboxEventRow(
                    id=new_id("evt"), aggregate_type="change_proposal",
                    aggregate_id=proposal_id, event_type="proposal.stale",
                    payload_json=stale_payload,
                    created_at=now, processed_at=None, attempt_count=0,
                    last_error_code=None,
                ))
                stale = True
                stale_expected = project.current_revision
            else:
                self._register_decision_artifact(session, approval_artifact, now)
                session.add(GateDecisionRow(
                    id=new_id("gdec"), project_id=row.project_id,
                    gate="DESIGN_CHANGE", subject_type="change_proposal",
                    subject_id=proposal_id, subject_digest=subject_digest,
                    base_revision=base_revision, idempotency_key=idempotency_key,
                    decision="approve", actor_type=actor_type, actor_id=actor_id,
                    comment=comment, created_at=now,
                ))
                session.add(EvidenceRow(
                    id=new_id("evd"), project_id=row.project_id, task_id=row.task_id,
                    kind="approval_signature", artifact_digest=approval_artifact.digest,
                    subject=f"{proposal_id}@{candidate_revision}", verdict="pass",
                    created_at=now,
                ))
                changed = session.execute(update(ProjectRow).where(
                    ProjectRow.id == row.project_id,
                    ProjectRow.current_revision == base_revision,
                    ProjectRow.version == expected_project_version,
                ).values(current_revision=candidate_revision,
                         project_snapshot_digest=candidate_snapshot_digest,
                         version=ProjectRow.version + 1))
                if changed.rowcount != 1:
                    session.expire_all()
                    actual = session.get(ProjectRow, row.project_id)
                    raise RevisionConflictError(
                        base_revision,
                        actual.current_revision if actual is not None else None,
                    )
                session.add(ProjectRevisionRow(
                    id=new_id("rev"), project_id=row.project_id,
                    revision=candidate_revision, parent_revision=base_revision,
                    snapshot_digest=candidate_snapshot_digest,
                    requirement_set_id=batch.requirement_set_id,
                    command_batch_id=batch.id, created_at=now,
                ))
                row.status = ProposalStatus.ACCEPTED.value
                row.updated_at = now
                row.version += 1
                accepted_payload = audit_payload(
                    actor_type=actor_type,
                    actor_id=actor_id,
                    action="proposal.accept",
                    object_type="change_proposal",
                    object_id=proposal_id,
                    before_digest=subject_digest,
                    after_digest=candidate_snapshot_digest,
                    result="accepted",
                )
                accepted_payload.update({"project_id": row.project_id, "proposal_id": proposal_id, "revision": candidate_revision, "snapshot_digest": candidate_snapshot_digest, "decision": "approve"})
                session.add(OutboxEventRow(
                    id=new_id("evt"), aggregate_type="project",
                    aggregate_id=row.project_id, event_type="project.revision.accepted",
                    payload_json=accepted_payload,
                    created_at=now, processed_at=None, attempt_count=0,
                    last_error_code=None,
                ))
            session.flush()
            result = _proposal(row)
        if stale:
            raise RevisionConflictError(base_revision, stale_expected)
        return result

    def decide_reject(
        self,
        *,
        proposal_id: str,
        base_revision: str,
        subject_digest: str,
        idempotency_key: str,
        actor_type: str,
        actor_id: str,
        comment: str,
        rejection_artifact: ArtifactDescriptor,
        now: datetime,
    ) -> ChangeProposal:
        with self._sessions.begin() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row = session.get(ChangeProposalRow, proposal_id)
            if row is None:
                raise ProposalNotFoundError(proposal_id)
            existing = session.scalar(select(GateDecisionRow).where(
                GateDecisionRow.project_id == row.project_id,
                GateDecisionRow.idempotency_key == idempotency_key,
            ))
            if existing is not None:
                if not self._decision_matches(
                    existing, subject_id=proposal_id, subject_digest=subject_digest,
                    base_revision=base_revision, idempotency_key=idempotency_key,
                    decision="reject", actor_type=actor_type, actor_id=actor_id,
                    comment=comment,
                ):
                    raise IdempotencyConflictError(idempotency_key)
                return _proposal(row)
            if row.status != ProposalStatus.READY_FOR_REVIEW.value:
                raise RequestInvalidError("candidate is not reviewable")
            self._register_decision_artifact(session, rejection_artifact, now)
            session.add(GateDecisionRow(
                id=new_id("gdec"), project_id=row.project_id,
                gate="DESIGN_CHANGE", subject_type="change_proposal",
                subject_id=proposal_id, subject_digest=subject_digest,
                base_revision=base_revision, idempotency_key=idempotency_key,
                decision="reject", actor_type=actor_type, actor_id=actor_id,
                comment=comment, created_at=now,
            ))
            session.add(EvidenceRow(
                id=new_id("evd"), project_id=row.project_id, task_id=row.task_id,
                kind="rejection_decision", artifact_digest=rejection_artifact.digest,
                subject=proposal_id, verdict="pass", created_at=now,
            ))
            row.status = ProposalStatus.REJECTED.value
            row.updated_at = now
            row.version += 1
            rejected_payload = audit_payload(
                actor_type=actor_type,
                actor_id=actor_id,
                action="proposal.reject",
                object_type="change_proposal",
                object_id=proposal_id,
                before_digest=subject_digest,
                after_digest=None,
                    result="rejected",
                )
            rejected_payload.update({"project_id": row.project_id, "proposal_id": proposal_id, "decision": "reject"})
            session.add(OutboxEventRow(
                id=new_id("evt"), aggregate_type="change_proposal",
                aggregate_id=proposal_id, event_type="proposal.rejected",
                payload_json=rejected_payload,
                created_at=now, processed_at=None, attempt_count=0,
                last_error_code=None,
            ))
            session.flush()
            return _proposal(row)

    def accept(self, **kwargs) -> ChangeProposal:
        """Compatibility name for the single acceptance transaction."""
        return self.decide_accept(**kwargs)

    def reject(self, **kwargs) -> ChangeProposal:
        """Compatibility name for the single rejection transaction."""
        return self.decide_reject(**kwargs)

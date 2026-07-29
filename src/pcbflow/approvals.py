from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from pcbflow.design_tables import (
    GateDecisionRow,
    OutboxEventRow,
    ProjectRevisionRow,
    RequirementSetRow,
)
from pcbflow.domain import new_id, utc_now
from pcbflow.repositories import (
    IdempotencyConflictError,
    ProjectNotFoundError,
    ProjectRepository,
    RevisionConflictError,
)
from pcbflow.requirement_store import (
    RequirementSetNotFoundError,
    RequirementStore,
    _requirement_set,
)
from pcbflow.requirements import RequirementSet, RequirementSetStatus
from pcbflow.revisions import RevisionReconciler
from pcbflow.tables import ProjectRow


class ApprovalDigestMismatchError(RuntimeError):
    def __init__(self, expected: str, actual: str) -> None:
        super().__init__(f"expected subject digest {expected}, found {actual}")
        self.expected = expected
        self.actual = actual


@dataclass(frozen=True, slots=True)
class GateDecision:
    id: str
    project_id: str
    gate: str
    subject_type: str
    subject_id: str
    subject_digest: str
    base_revision: str
    decision: str
    actor_type: str
    actor_id: str
    comment: str
    created_at: datetime


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _decision(row: GateDecisionRow) -> GateDecision:
    return GateDecision(
        id=row.id,
        project_id=row.project_id,
        gate=row.gate,
        subject_type=row.subject_type,
        subject_id=row.subject_id,
        subject_digest=row.subject_digest,
        base_revision=row.base_revision,
        decision=row.decision,
        actor_type=row.actor_type,
        actor_id=row.actor_id,
        comment=row.comment,
        created_at=_utc(row.created_at),
    )


class GateDecisionStore:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @staticmethod
    def _replay(
        row: GateDecisionRow,
        *,
        gate: str,
        subject_type: str,
        subject_id: str,
        subject_digest: str,
        base_revision: str,
        idempotency_key: str,
        decision: str,
        actor_type: str,
        actor_id: str,
        comment: str,
    ) -> GateDecision:
        if row.subject_digest != subject_digest:
            raise ApprovalDigestMismatchError(row.subject_digest, subject_digest)
        if (
            row.gate != gate
            or row.subject_type != subject_type
            or row.subject_id != subject_id
            or row.base_revision != base_revision
            or row.idempotency_key != idempotency_key
            or row.decision != decision
            or row.actor_type != actor_type
            or row.actor_id != actor_id
            or row.comment != comment
        ):
            raise IdempotencyConflictError(idempotency_key)
        return _decision(row)

    def find_by_key(
        self, project_id: str, idempotency_key: str
    ) -> GateDecision | None:
        with self._sessions() as session:
            row = session.scalar(
                select(GateDecisionRow).where(
                    GateDecisionRow.project_id == project_id,
                    GateDecisionRow.idempotency_key == idempotency_key,
                )
            )
            return _decision(row) if row is not None else None

    def add(
        self,
        *,
        project_id: str,
        gate: str,
        subject_type: str,
        subject_id: str,
        subject_digest: str,
        base_revision: str,
        idempotency_key: str,
        decision: str,
        actor_type: str,
        actor_id: str,
        comment: str,
    ) -> GateDecision:
        if not idempotency_key:
            raise ValueError("idempotency key must not be empty")
        if decision not in {"approve", "reject"}:
            raise ValueError("unsupported gate decision")
        values = {
            "gate": gate,
            "subject_type": subject_type,
            "subject_id": subject_id,
            "subject_digest": subject_digest,
            "base_revision": base_revision,
            "idempotency_key": idempotency_key,
            "decision": decision,
            "actor_type": actor_type,
            "actor_id": actor_id,
            "comment": comment,
        }
        try:
            with self._sessions.begin() as session:
                existing = session.scalar(
                    select(GateDecisionRow).where(
                        GateDecisionRow.project_id == project_id,
                        GateDecisionRow.idempotency_key == idempotency_key,
                    )
                )
                if existing is not None:
                    return self._replay(existing, **values)
                row = GateDecisionRow(
                    id=new_id("gdec"),
                    project_id=project_id,
                    created_at=utc_now(),
                    **values,
                )
                session.add(row)
                session.flush()
                result = _decision(row)
            return result
        except IntegrityError:
            with self._sessions() as session:
                existing = session.scalar(
                    select(GateDecisionRow).where(
                        GateDecisionRow.project_id == project_id,
                        GateDecisionRow.idempotency_key == idempotency_key,
                    )
                )
                if existing is None:
                    raise
                return self._replay(existing, **values)

    def decide_g1(
        self,
        *,
        project_id: str,
        requirement_set_id: str,
        subject_digest: str,
        base_revision: str,
        candidate_revision: str,
        candidate_snapshot_digest: str,
        expected_project_version: int,
        idempotency_key: str,
        decision: str,
        actor_type: str,
        actor_id: str,
        comment: str,
    ) -> RequirementSet:
        if not idempotency_key:
            raise ValueError("idempotency key must not be empty")
        if decision not in {"approve", "reject"}:
            raise ValueError("unsupported gate decision")
        replay_values = {
            "gate": "G1",
            "subject_type": "requirement_set",
            "subject_id": requirement_set_id,
            "subject_digest": subject_digest,
            "base_revision": base_revision,
            "idempotency_key": idempotency_key,
            "decision": decision,
            "actor_type": actor_type,
            "actor_id": actor_id,
            "comment": comment,
        }
        try:
            with self._sessions.begin() as session:
                existing = session.scalar(
                    select(GateDecisionRow).where(
                        GateDecisionRow.project_id == project_id,
                        GateDecisionRow.idempotency_key == idempotency_key,
                    )
                )
                if existing is not None:
                    self._replay(existing, **replay_values)
                    replayed = session.get(RequirementSetRow, requirement_set_id)
                    if replayed is None:
                        raise RequirementSetNotFoundError(requirement_set_id)
                    return _requirement_set(replayed)

                requirement_row = session.get(RequirementSetRow, requirement_set_id)
                if requirement_row is None:
                    raise RequirementSetNotFoundError(requirement_set_id)
                requirement_set = _requirement_set(requirement_row)
                if requirement_set.project_id != project_id:
                    raise IdempotencyConflictError(idempotency_key)
                actual_digest = requirement_set.subject_digest()
                if actual_digest != subject_digest:
                    raise ApprovalDigestMismatchError(actual_digest, subject_digest)
                if (
                    requirement_set.base_revision != base_revision
                    or requirement_set.candidate_revision != candidate_revision
                    or requirement_set.candidate_snapshot_digest
                    != candidate_snapshot_digest
                ):
                    raise IdempotencyConflictError(idempotency_key)
                if requirement_row.status != RequirementSetStatus.PENDING_APPROVAL.value:
                    raise ValueError("requirement set is not pending approval")

                project = session.get(ProjectRow, project_id)
                if project is None:
                    raise ProjectNotFoundError(project_id)
                now = utc_now()
                decision_row = GateDecisionRow(
                    id=new_id("gdec"),
                    project_id=project_id,
                    created_at=now,
                    **replay_values,
                )
                session.add(decision_row)

                if decision == "approve":
                    if (
                        project.version != expected_project_version
                        or project.current_revision != base_revision
                    ):
                        raise RevisionConflictError(
                            base_revision, project.current_revision
                        )
                    if project.active_requirement_set_id is not None:
                        superseded = session.get(
                            RequirementSetRow, project.active_requirement_set_id
                        )
                        if superseded is None:
                            raise RuntimeError("active requirement set is missing")
                        if superseded.id != requirement_set_id:
                            if superseded.status != RequirementSetStatus.FROZEN.value:
                                raise RuntimeError("active requirement set is not frozen")
                            superseded.status = RequirementSetStatus.SUPERSEDED.value

                    changed = session.execute(
                        update(ProjectRow)
                        .where(
                            ProjectRow.id == project_id,
                            ProjectRow.version == expected_project_version,
                            ProjectRow.current_revision == base_revision,
                        )
                        .values(
                            current_revision=candidate_revision,
                            project_snapshot_digest=candidate_snapshot_digest,
                            active_requirement_set_id=requirement_set_id,
                            version=ProjectRow.version + 1,
                        )
                        .execution_options(synchronize_session=False)
                    )
                    if changed.rowcount != 1:
                        session.expire_all()
                        actual = session.get(ProjectRow, project_id)
                        if actual is None:
                            raise ProjectNotFoundError(project_id)
                        raise RevisionConflictError(
                            base_revision, actual.current_revision
                        )
                    requirement_row.status = RequirementSetStatus.FROZEN.value
                    requirement_row.frozen_revision = candidate_revision
                    requirement_row.frozen_at = now
                    session.add(
                        ProjectRevisionRow(
                            id=new_id("rev"),
                            project_id=project_id,
                            revision=candidate_revision,
                            parent_revision=base_revision,
                            snapshot_digest=candidate_snapshot_digest,
                            requirement_set_id=requirement_set_id,
                            command_batch_id=None,
                            created_at=now,
                        )
                    )
                    event_type = "project.revision.accepted"
                    event_payload = {
                        "project_id": project_id,
                        "gate": "G1",
                        "requirement_set_id": requirement_set_id,
                        "subject_digest": subject_digest,
                        "base_revision": base_revision,
                        "revision": candidate_revision,
                        "snapshot_digest": candidate_snapshot_digest,
                        "decision": decision,
                        "actor": {"type": actor_type, "id": actor_id},
                        "comment": comment,
                    }
                else:
                    requirement_row.status = RequirementSetStatus.REJECTED.value
                    event_type = "gate.decided"
                    event_payload = {
                        "project_id": project_id,
                        "gate": "G1",
                        "requirement_set_id": requirement_set_id,
                        "subject_digest": subject_digest,
                        "base_revision": base_revision,
                        "decision": decision,
                        "actor": {"type": actor_type, "id": actor_id},
                        "comment": comment,
                    }

                session.add(
                    OutboxEventRow(
                        id=new_id("evt"),
                        aggregate_type="project",
                        aggregate_id=project_id,
                        event_type=event_type,
                        payload_json=event_payload,
                        created_at=now,
                        processed_at=None,
                        attempt_count=0,
                        last_error_code=None,
                    )
                )
                session.flush()
                result = _requirement_set(requirement_row)
            return result
        except IntegrityError:
            with self._sessions() as session:
                existing = session.scalar(
                    select(GateDecisionRow).where(
                        GateDecisionRow.project_id == project_id,
                        GateDecisionRow.idempotency_key == idempotency_key,
                    )
                )
                if existing is None:
                    raise
                self._replay(existing, **replay_values)
                requirement_row = session.get(RequirementSetRow, requirement_set_id)
                if requirement_row is None:
                    raise RequirementSetNotFoundError(requirement_set_id)
                return _requirement_set(requirement_row)


class ApprovalService:
    def __init__(
        self,
        requirements: RequirementStore,
        projects: ProjectRepository,
        decisions: GateDecisionStore,
        reconciler: RevisionReconciler,
    ) -> None:
        self._requirements = requirements
        self._projects = projects
        self._decisions = decisions
        self._reconciler = reconciler

    def decide_g1(
        self,
        *,
        requirement_set_id: str,
        subject_digest: str,
        decision: str,
        actor_type: str,
        actor_id: str,
        comment: str,
        idempotency_key: str,
    ) -> RequirementSet:
        requirement_set = self._requirements.get(requirement_set_id)
        actual_digest = requirement_set.subject_digest()
        if actual_digest != subject_digest:
            raise ApprovalDigestMismatchError(actual_digest, subject_digest)
        if requirement_set.candidate_revision is None:
            raise ValueError("requirement set has no candidate revision")
        if requirement_set.candidate_snapshot_digest is None:
            raise ValueError("requirement set has no candidate snapshot")
        project = self._projects.get(requirement_set.project_id)
        result = self._decisions.decide_g1(
            project_id=project.id,
            requirement_set_id=requirement_set.id,
            subject_digest=subject_digest,
            base_revision=requirement_set.base_revision,
            candidate_revision=requirement_set.candidate_revision,
            candidate_snapshot_digest=requirement_set.candidate_snapshot_digest,
            expected_project_version=project.version,
            idempotency_key=idempotency_key,
            decision=decision,
            actor_type=actor_type,
            actor_id=actor_id,
            comment=comment,
        )
        self._reconciler.run_once()
        return result

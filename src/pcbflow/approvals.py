from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from pcbflow.design_tables import GateDecisionRow
from pcbflow.domain import new_id, utc_now
from pcbflow.repositories import IdempotencyConflictError


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

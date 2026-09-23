from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from pcbflow.artifacts import ArtifactDescriptor, ContentAddressedStore
from pcbflow.canonical import canonical_json_bytes
from pcbflow.design_tables import (
    GateDecisionRow,
    OutboxEventRow,
    PcbCandidateRow,
    ProjectRevisionRow,
    RequirementSetRow,
)
from pcbflow.domain import RequestInvalidError, new_id, utc_now
from pcbflow.eda import validate_idempotency_key
from pcbflow.observability import MetricName, Metrics, audit_payload
from pcbflow.pcb_candidates import (
    G3_EVIDENCE_SET_KIND,
    G3_EVIDENCE_SET_MEDIA_TYPE,
    G3_REQUIRED_EVIDENCE,
    G3_REQUIRED_EVIDENCE_MEDIA_TYPES,
    PcbCandidate,
    PcbCandidateNotReviewableError,
    PcbCandidateStatus,
    PcbCandidateStore,
    pcb_candidate_review_digest,
    validate_candidate_digest,
)
from pcbflow.repositories import (
    EvidenceRepository,
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
from pcbflow.tables import ArtifactRow, ProjectRow

_G1_APPROVAL_MEDIA_TYPE = "application/vnd.pcbflow.g1-approval+json"
_G3_APPROVAL_MEDIA_TYPE = "application/vnd.pcbflow.g3-approval+json"


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
    def __init__(
        self,
        sessions: sessionmaker[Session],
        artifacts: ContentAddressedStore | None = None,
        metrics: Metrics | None = None,
    ) -> None:
        self._sessions = sessions
        self._artifacts = artifacts
        self._metrics = metrics

    @staticmethod
    def _register_artifact(
        session: Session, descriptor: ArtifactDescriptor, created_at: datetime
    ) -> None:
        row = session.get(ArtifactRow, descriptor.digest)
        if row is None:
            session.add(
                ArtifactRow(
                    digest=descriptor.digest,
                    size=descriptor.size,
                    media_type=descriptor.media_type,
                    storage_path=str(descriptor.path),
                    created_at=created_at,
                )
            )
            return
        if (
            row.size != descriptor.size
            or row.media_type != descriptor.media_type
            or row.storage_path != str(descriptor.path)
        ):
            raise RuntimeError(f"artifact descriptor conflict: {descriptor.digest}")

    def _create_g1_approval_artifact(
        self,
        *,
        decision_id: str,
        project: ProjectRow,
        requirement_set: RequirementSet,
        subject_digest: str,
        decision: str,
        actor_type: str,
        actor_id: str,
        comment: str,
        created_at: datetime,
    ) -> ArtifactDescriptor:
        if self._artifacts is None:
            raise RuntimeError("G1 approval artifacts require a content-addressed store")
        record = {
            "schema_version": "1.0",
            "gate": "G1",
            "gate_decision_id": decision_id,
            "project_id": project.id,
            "requirement_set_id": requirement_set.id,
            "base_revision": requirement_set.base_revision,
            "base_snapshot_digest": project.project_snapshot_digest,
            "candidate_revision": requirement_set.candidate_revision,
            "candidate_snapshot_digest": requirement_set.candidate_snapshot_digest,
            "subject_digest": subject_digest,
            "decision": decision,
            "actor": {"type": actor_type, "id": actor_id},
            "comment": comment,
            "created_at": created_at.isoformat().replace("+00:00", "Z"),
        }
        return self._artifacts.put_bytes(
            canonical_json_bytes(record), _G1_APPROVAL_MEDIA_TYPE
        )

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
                session.execute(text("BEGIN IMMEDIATE"))
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
                    raise RequestInvalidError(
                        "requirement set is not pending approval"
                    )

                project = session.get(ProjectRow, project_id)
                if project is None:
                    raise ProjectNotFoundError(project_id)
                if decision == "approve" and (
                    project.version != expected_project_version
                    or project.current_revision != base_revision
                ):
                    if self._metrics is not None:
                        self._metrics.increment(MetricName.PROJECT_REVISION_CONFLICT_TOTAL)
                    raise RevisionConflictError(base_revision, project.current_revision)
                now = utc_now()
                decision_id = new_id("gdec")
                approval_artifact = self._create_g1_approval_artifact(
                    decision_id=decision_id,
                    project=project,
                    requirement_set=requirement_set,
                    subject_digest=subject_digest,
                    decision=decision,
                    actor_type=actor_type,
                    actor_id=actor_id,
                    comment=comment,
                    created_at=now,
                )
                self._register_artifact(session, approval_artifact, now)
                decision_row = GateDecisionRow(
                    id=decision_id,
                    project_id=project_id,
                    created_at=now,
                    **replay_values,
                )
                session.add(decision_row)

                if decision == "approve":
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
                        if self._metrics is not None:
                            self._metrics.increment(MetricName.PROJECT_REVISION_CONFLICT_TOTAL)
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
                    event_payload = audit_payload(
                        actor_type=actor_type,
                        actor_id=actor_id,
                        action="approval.g1",
                        object_type="requirement_set",
                        object_id=requirement_set_id,
                        before_digest=subject_digest,
                        after_digest=candidate_snapshot_digest,
                        result="accepted",
                    )
                    event_payload.update({
                        "project_id": project_id,
                        "gate": "G1",
                        "requirement_set_id": requirement_set_id,
                        "subject_digest": subject_digest,
                        "base_revision": base_revision,
                        "revision": candidate_revision,
                        "snapshot_digest": candidate_snapshot_digest,
                        "decision": decision,
                        "gate_decision_id": decision_id,
                        "approval_artifact_digest": approval_artifact.digest,
                        "actor": {"type": actor_type, "id": actor_id},
                        "comment": comment,
                    })
                else:
                    requirement_row.status = RequirementSetStatus.REJECTED.value
                    event_type = "gate.decided"
                    event_payload = audit_payload(
                        actor_type=actor_type,
                        actor_id=actor_id,
                        action="approval.g1",
                        object_type="requirement_set",
                        object_id=requirement_set_id,
                        before_digest=subject_digest,
                        after_digest=None,
                        result="rejected",
                    )
                    event_payload.update({
                        "project_id": project_id,
                        "gate": "G1",
                        "requirement_set_id": requirement_set_id,
                        "subject_digest": subject_digest,
                        "base_revision": base_revision,
                        "decision": decision,
                        "gate_decision_id": decision_id,
                        "approval_artifact_digest": approval_artifact.digest,
                        "actor": {"type": actor_type, "id": actor_id},
                        "comment": comment,
                    })

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
        reconciler: RevisionReconciler | None = None,
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
            raise RequestInvalidError("requirement set has no candidate revision")
        if requirement_set.candidate_snapshot_digest is None:
            raise RequestInvalidError("requirement set has no candidate snapshot")
        project = self._projects.get(requirement_set.project_id)
        if (
            decision == "approve"
            and self._reconciler is not None
            and self._decisions.find_by_key(project.id, idempotency_key) is None
        ):
            self._reconciler.assert_writable(project.id)
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
        if decision == "approve" and self._reconciler is not None:
            self._reconciler.run_once()
        return result


class PcbApprovalService:
    """Bind G3 decisions to the frozen PCB candidate evidence set."""

    def __init__(
        self,
        candidates: PcbCandidateStore,
        decisions: GateDecisionStore,
        artifacts: ContentAddressedStore,
        sessions: sessionmaker[Session],
    ) -> None:
        self._candidates = candidates
        self._decisions = decisions
        self._artifacts = artifacts
        self._sessions = sessions
        self._evidence = EvidenceRepository(sessions)

    def decide_g3(
        self,
        *,
        candidate_id: str,
        candidate_digest: str,
        idempotency_key: str,
        actor_id: str,
        decision: str,
        comment: str,
    ) -> PcbCandidate:
        validate_idempotency_key(idempotency_key)
        validate_candidate_digest(candidate_digest, field="candidate_digest")
        if decision not in {"approve", "reject"}:
            raise RequestInvalidError("unsupported G3 decision")
        if type(actor_id) is not str or not actor_id.strip():
            raise RequestInvalidError("actor_id must not be blank")
        if type(comment) is not str:
            raise RequestInvalidError("comment must be a string")
        candidate = self._candidates.get(candidate_id)
        normalized_actor_id = actor_id.strip()
        expected_digest = self._expected_candidate_digest(candidate)
        stored_digest = (
            candidate.result.get("candidate_digest")
            if isinstance(candidate.result, dict)
            else None
        )
        if expected_digest is None or stored_digest != expected_digest:
            raise PcbCandidateNotReviewableError()
        if candidate_digest != expected_digest:
            raise ApprovalDigestMismatchError(expected_digest, candidate_digest)
        replay_values = {
            "gate": "G3_PCB",
            "subject_type": "pcb_candidate",
            "subject_id": candidate.id,
            "subject_digest": candidate_digest,
            "base_revision": candidate.base_revision,
            "idempotency_key": idempotency_key,
            "decision": decision,
            "actor_type": "human",
            "actor_id": normalized_actor_id,
            "comment": comment,
        }
        existing = self._decisions.find_by_key(candidate.project_id, idempotency_key)
        if existing is None and not self._eligible(candidate):
            raise PcbCandidateNotReviewableError()
        now = utc_now()
        with self._sessions.begin() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row = session.get(PcbCandidateRow, candidate.id)
            if row is None:
                raise PcbCandidateNotReviewableError()
            gate_row = session.scalar(
                select(GateDecisionRow).where(
                    GateDecisionRow.project_id == candidate.project_id,
                    GateDecisionRow.idempotency_key == idempotency_key,
                )
            )
            if gate_row is not None:
                gate = GateDecisionStore._replay(gate_row, **replay_values)
                approval = self._approval_artifact(
                    candidate,
                    gate_decision_id=gate.id,
                    created_at=gate.created_at,
                    decision=decision,
                    actor_id=normalized_actor_id,
                    comment=comment,
                )
                expected_g3_decision = self._g3_decision_payload(
                    gate.id,
                    idempotency_key,
                    candidate_digest,
                    decision,
                    normalized_actor_id,
                    comment,
                    approval.digest,
                )
                if (
                    not isinstance(row.result_json, dict)
                    or row.result_json.get("g3_decision") != expected_g3_decision
                    or row.status
                    != (
                        PcbCandidateStatus.G3_APPROVED.value
                        if decision == "approve"
                        else PcbCandidateStatus.VALIDATION_FAILED.value
                    )
                ):
                    raise PcbCandidateNotReviewableError()
                GateDecisionStore._register_artifact(session, approval, now)
            else:
                if (
                    row.status != PcbCandidateStatus.READY_FOR_G3.value
                    or row.version != candidate.version
                    or row.result_json != candidate.result
                ):
                    raise PcbCandidateNotReviewableError()
                # Re-read the complete evidence contract after taking the
                # decision lock so a concurrent tamper cannot use a stale
                # preflight result.
                if not self._eligible(candidate):
                    raise PcbCandidateNotReviewableError()
                gate_id = new_id("gdec")
                approval = self._approval_artifact(
                    candidate,
                    gate_decision_id=gate_id,
                    created_at=now,
                    decision=decision,
                    actor_id=normalized_actor_id,
                    comment=comment,
                )
                GateDecisionStore._register_artifact(session, approval, now)
                session.add(
                    GateDecisionRow(
                        id=gate_id,
                        project_id=candidate.project_id,
                        created_at=now,
                        **replay_values,
                    )
                )
                result = dict(row.result_json)
                result["g3_decision"] = self._g3_decision_payload(
                    gate_id,
                    idempotency_key,
                    candidate_digest,
                    decision,
                    normalized_actor_id,
                    comment,
                    approval.digest,
                )
                row.status = (
                    PcbCandidateStatus.G3_APPROVED.value
                    if decision == "approve"
                    else PcbCandidateStatus.VALIDATION_FAILED.value
                )
                row.result_json = result
                row.last_error_code = None if decision == "approve" else "G3_REJECTED"
                row.updated_at = now
                row.version += 1
        return self._candidates.get(candidate.id)

    def _approval_artifact(
        self,
        candidate: PcbCandidate,
        *,
        gate_decision_id: str,
        created_at: datetime,
        decision: str,
        actor_id: str,
        comment: str,
    ) -> ArtifactDescriptor:
        return self._artifacts.put_bytes(
            canonical_json_bytes(
                {
                    "schema_version": "1.0",
                    "gate": "G3_PCB",
                    "gate_decision_id": gate_decision_id,
                    "project_id": candidate.project_id,
                    "candidate_id": candidate.id,
                    "candidate_digest": candidate.result["candidate_digest"],
                    "base_revision": candidate.base_revision,
                    "decision": decision,
                    "actor": {"type": "human", "id": actor_id},
                    "comment": comment,
                    "created_at": created_at.isoformat().replace("+00:00", "Z"),
                }
            ),
            _G3_APPROVAL_MEDIA_TYPE,
        )

    @staticmethod
    def _g3_decision_payload(
        gate_decision_id: str,
        idempotency_key: str,
        candidate_digest: str,
        decision: str,
        actor_id: str,
        comment: str,
        approval_artifact_digest: str,
    ) -> dict[str, str]:
        return {
            "gate_decision_id": gate_decision_id,
            "idempotency_key": idempotency_key,
            "subject_digest": candidate_digest,
            "decision": decision,
            "actor_id": actor_id,
            "comment": comment,
            "approval_artifact_digest": approval_artifact_digest,
        }

    def _eligible(self, candidate: PcbCandidate) -> bool:
        if (
            candidate.status is not PcbCandidateStatus.READY_FOR_G3
            or candidate.blocking_finding_count != 0
            or candidate.unconnected_net_count != 0
            or candidate.evidence_kinds != G3_REQUIRED_EVIDENCE
            or not isinstance(candidate.result, dict)
            or candidate.result.get("native_candidate_verified") is not True
        ):
            return False
        expected_digest = self._expected_candidate_digest(candidate)
        if expected_digest is None or candidate.result.get("candidate_digest") != expected_digest:
            return False
        return self._verify_evidence_set(candidate)

    @staticmethod
    def _expected_candidate_digest(candidate: PcbCandidate) -> str | None:
        if not isinstance(candidate.result, dict):
            return None
        post_snapshot_digest = candidate.result.get("candidate_board_snapshot_digest")
        evidence_set_digest = candidate.result.get("evidence_set_digest")
        try:
            validate_candidate_digest(
                post_snapshot_digest, field="candidate_board_snapshot_digest"
            )
            validate_candidate_digest(evidence_set_digest, field="evidence_set_digest")
        except RequestInvalidError:
            return None
        return pcb_candidate_review_digest(
            candidate_id=candidate.id,
            project_id=candidate.project_id,
            base_revision=candidate.base_revision,
            base_snapshot_digest=candidate.base_snapshot_digest,
            board_snapshot_digest=candidate.board_snapshot_digest,
            candidate_board_snapshot_digest=post_snapshot_digest,
            rulepack_digest=candidate.rulepack_digest,
            capability_digest=candidate.capability_digest,
            authority_digest=candidate.authority_digest,
            operations_digest=candidate.operations_digest,
            evidence_set_digest=evidence_set_digest,
        )

    def _verify_evidence_set(self, candidate: PcbCandidate) -> bool:
        assert isinstance(candidate.result, dict)
        evidence_set_digest = candidate.result.get("evidence_set_digest")
        artifacts = candidate.result.get("evidence_artifacts")
        if not isinstance(evidence_set_digest, str) or type(artifacts) is not dict:
            return False
        try:
            if not self._artifacts.verify(evidence_set_digest):
                return False
            with self._artifacts.open(evidence_set_digest) as stream:
                raw = stream.read()
            payload = json.loads(raw.decode("utf-8"))
            if raw != canonical_json_bytes(payload):
                return False
            if not isinstance(payload, dict) or set(payload) != {
                "schema_version",
                "candidate_id",
                "project_id",
                "task_id",
                "base_revision",
                "items",
            }:
                return False
            if (
                payload["schema_version"] != "1.0"
                or payload["candidate_id"] != candidate.id
                or payload["project_id"] != candidate.project_id
                or payload["task_id"] != candidate.task_id
                or payload["base_revision"] != candidate.base_revision
                or type(payload["items"]) is not list
            ):
                return False
            items: dict[str, dict[str, Any]] = {}
            for item in payload["items"]:
                if type(item) is not dict or set(item) != {
                    "kind",
                    "artifact_digest",
                    "media_type",
                    "verdict",
                }:
                    return False
                kind = item["kind"]
                if type(kind) is not str or kind in items:
                    return False
                if (
                    item["verdict"] != "pass"
                    or type(item["artifact_digest"]) is not str
                    or type(item["media_type"]) is not str
                ):
                    return False
                items[kind] = item
            if (
                set(items) != G3_REQUIRED_EVIDENCE
                or set(artifacts) != G3_REQUIRED_EVIDENCE
            ):
                return False
            if (
                artifacts["pcb_input_snapshot"] != candidate.board_snapshot_digest
                or artifacts["eda_capability"] != candidate.capability_digest
                or artifacts["rulepack"] != candidate.rulepack_digest
            ):
                return False
            if any(
                item["media_type"] != G3_REQUIRED_EVIDENCE_MEDIA_TYPES[kind]
                for kind, item in items.items()
            ):
                return False
            native_payload = self._read_canonical_json(
                items["native_drc"]["artifact_digest"]
            )
            if not self._valid_native_drc_payload(native_payload):
                return False
            validation_payload = self._read_canonical_json(
                items["boardir_validation"]["artifact_digest"]
            )
            if not self._valid_boardir_validation_payload(validation_payload, candidate):
                return False
            summary_payload = self._read_canonical_json(
                items["candidate_summary"]["artifact_digest"]
            )
            if not self._valid_candidate_summary_payload(summary_payload, candidate):
                return False
            registered_items = {
                kind: (
                    item["artifact_digest"],
                    item["media_type"],
                    item["verdict"],
                )
                for kind, item in items.items()
            }
            return (
                all(
                    artifacts[kind] == item["artifact_digest"]
                    and self._artifacts.verify(item["artifact_digest"])
                    for kind, item in items.items()
                )
                and self._evidence.evidence_set_matches_registered_artifacts(
                    project_id=candidate.project_id,
                    task_id=candidate.task_id,
                    subject=candidate.id,
                    evidence_set_kind=G3_EVIDENCE_SET_KIND,
                    evidence_set_digest=evidence_set_digest,
                    evidence_set_media_type=G3_EVIDENCE_SET_MEDIA_TYPE,
                    items=registered_items,
                )
            )
        except (OSError, UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError):
            return False

    def _read_canonical_json(self, digest: str) -> Any | None:
        try:
            with self._artifacts.open(digest) as stream:
                raw = stream.read()
            payload = json.loads(raw.decode("utf-8"))
            if raw != canonical_json_bytes(payload):
                return None
            return payload
        except (OSError, UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError):
            return None

    @staticmethod
    def _valid_native_drc_payload(payload: Any) -> bool:
        if (
            type(payload) is not dict
            or set(payload) != {"schema_version", "reports"}
            or payload["schema_version"] != "1.0"
            or type(payload["reports"]) is not list
            or not payload["reports"]
        ):
            return False
        for report in payload["reports"]:
            if (
                type(report) is not dict
                or set(report) != {"kind", "findings"}
                or type(report["kind"]) is not str
                or not report["kind"].strip()
                or type(report["findings"]) is not list
            ):
                return False
            for finding in report["findings"]:
                if (
                    type(finding) is not dict
                    or set(finding)
                    != {"rule_id", "severity", "subject", "message"}
                    or any(type(finding[field]) is not str for field in finding)
                    or finding["severity"].casefold()
                    in {"error", "critical", "fatal", "blocker"}
                ):
                    return False
        return True

    @staticmethod
    def _valid_boardir_validation_payload(
        payload: Any, candidate: PcbCandidate
    ) -> bool:
        if (
            type(payload) is not dict
            or set(payload) != {"schema_version", "snapshot_digest", "snapshot", "findings"}
            or payload["schema_version"] != "1.0"
            or type(payload["snapshot_digest"]) is not str
            or type(payload["snapshot"]) is not dict
            or type(payload["findings"]) is not list
            or not isinstance(candidate.result, dict)
            or payload["snapshot_digest"]
            != candidate.result.get("candidate_board_snapshot_digest")
        ):
            return False
        actual_digest = "sha256:" + hashlib.sha256(
            canonical_json_bytes(payload["snapshot"])
        ).hexdigest()
        if actual_digest != payload["snapshot_digest"]:
            return False
        for finding in payload["findings"]:
            if (
                type(finding) is not dict
                or set(finding) != {"rule_id", "severity", "subject", "message"}
                or any(type(finding[field]) is not str for field in finding)
                or finding["severity"].casefold()
                in {"error", "critical", "fatal", "blocker"}
            ):
                return False
        return True

    @staticmethod
    def _valid_candidate_summary_payload(
        payload: Any, candidate: PcbCandidate
    ) -> bool:
        if not isinstance(candidate.result, dict) or type(payload) is not dict:
            return False
        expected_keys = {
            "schema_version",
            "candidate_id",
            "project_id",
            "base_revision",
            "source_snapshot_digest_before",
            "source_snapshot_digest_after",
            "board_snapshot_digest",
            "candidate_board_snapshot_digest",
            "rulepack_digest",
            "capability_digest",
            "authority_digest",
            "operations_digest",
            "blocking_finding_count",
            "unconnected_net_ids",
        }
        if set(payload) != expected_keys or payload["schema_version"] != "1.0":
            return False
        expected = {
            "candidate_id": candidate.id,
            "project_id": candidate.project_id,
            "base_revision": candidate.base_revision,
            "board_snapshot_digest": candidate.board_snapshot_digest,
            "candidate_board_snapshot_digest": candidate.result.get(
                "candidate_board_snapshot_digest"
            ),
            "rulepack_digest": candidate.rulepack_digest,
            "capability_digest": candidate.capability_digest,
            "authority_digest": candidate.authority_digest,
            "operations_digest": candidate.operations_digest,
            "blocking_finding_count": 0,
            "unconnected_net_ids": [],
        }
        if any(payload[key] != value for key, value in expected.items()):
            return False
        base_snapshot_digest = candidate.base_snapshot_digest
        if base_snapshot_digest is not None and (
            payload["source_snapshot_digest_before"] != base_snapshot_digest
            or payload["source_snapshot_digest_after"] != base_snapshot_digest
        ):
            return False
        return all(
            type(payload[key]) is str
            for key in (
                "source_snapshot_digest_before",
                "source_snapshot_digest_after",
            )
        )

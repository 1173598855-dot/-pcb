"""Durable PCB candidate lifecycle: creation, replay, transitions, release.

Verbatim extraction from the historical pcb_candidates monolith; behavior is
unchanged. The pcbflow.pcb_candidates module now re-exports from here.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import exists, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from pcbflow.artifacts import ArtifactDescriptor, StagedArtifact
from pcbflow.board.operations import (
    BoardOperation,
)
from pcbflow.cancellation import TaskCancelledError
from pcbflow.design_tables import PcbCandidateRow
from pcbflow.domain import (
    EdaKind,
    EdaOperation,
    RequestInvalidError,
    TaskStatus,
    new_id,
    utc_now,
)
from pcbflow.eda import validate_idempotency_key
from pcbflow.lceda_pro import LcedaProCapabilityError
from pcbflow.pcb_candidate_codec import _operation_payload
from pcbflow.pcb_candidate_validation import (
    G3_REQUIRED_EVIDENCE,
    PCB_EXPORT_RELEASE_TASK_KIND,
    PCB_GENERATE_CANDIDATE_TASK_KIND,
    PcbCandidate,
    PcbCandidateNotFoundError,
    PcbCandidateNotReviewableError,
    PcbCandidateStatus,
    _canonical,
    _digest,
    _release_task_idempotency_key,
    _task_idempotency_key,
    _utc,
    pcb_candidate_review_digest,
    validate_candidate_digest,
    validate_candidate_public_inputs,
)
from pcbflow.pcb_workflow import CAPABILITY_EVIDENCE_KIND
from pcbflow.repositories import (
    EvidenceRepository,
    IdempotencyConflictError,
    ProjectNotFoundError,
    StaleLeaseError,
    TaskRepository,
)
from pcbflow.tables import ArtifactRow, ProjectRow, TaskRow


def _candidate_output_kind(
    status: PcbCandidateStatus,
    _algorithm_evidence: dict[str, Any] | None,
    result: dict[str, Any] | None,
) -> str:
    if (
        status in {PcbCandidateStatus.READY_FOR_G4, PcbCandidateStatus.RELEASED}
        and isinstance(result, dict)
        and isinstance(result.get("release"), dict)
        and isinstance(result["release"].get("manifest_digest"), str)
    ):
        return "release_candidate"
    if (
        status
        in {
            PcbCandidateStatus.READY_FOR_G3,
            PcbCandidateStatus.G3_APPROVED,
            PcbCandidateStatus.RELEASE_PENDING,
            PcbCandidateStatus.READY_FOR_G4,
            PcbCandidateStatus.RELEASED,
        }
        and isinstance(result, dict)
        and result.get("native_candidate_verified") is True
    ):
        return "native_candidate"
    return "boardir_only"


def _public_algorithm_evidence(
    algorithm_evidence: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(algorithm_evidence, dict):
        return algorithm_evidence
    return {**algorithm_evidence, "output_kind": "boardir_only"}


def _candidate(row: PcbCandidateRow) -> PcbCandidate:
    status = PcbCandidateStatus(row.status)
    return PcbCandidate(
        id=row.id, project_id=row.project_id, task_id=row.task_id, idempotency_key=row.idempotency_key,
        base_revision=row.base_revision, base_snapshot_digest=row.base_snapshot_digest,
        board_snapshot_digest=row.board_snapshot_digest, rulepack_digest=row.rulepack_digest,
        capability_digest=row.capability_digest, authority_digest=row.authority_digest,
        operations=tuple(row.operations_json), operations_digest=row.operations_digest,
        algorithm_evidence=_public_algorithm_evidence(row.algorithm_evidence_json), status=status,
        result=row.result_json, last_error_code=row.last_error_code, accepted_revision=row.accepted_revision,
        created_at=_utc(row.created_at), updated_at=_utc(row.updated_at), version=row.version,
        output_kind=_candidate_output_kind(status, row.algorithm_evidence_json, row.result_json),
    )


class PcbCandidateStore:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        tasks: TaskRepository,
        authorities: Any,
        projects: Any,
        capability_gate: Any | None = None,
        evidence: EvidenceRepository | None = None,
        revisions: Any | None = None,
    ) -> None:
        self._sessions = sessions
        self._tasks = tasks
        self._authorities = authorities
        self._projects = projects
        self._capability_gate = capability_gate
        self._evidence = evidence
        self._revisions = revisions

    @staticmethod
    def _replay_matches(
        row: PcbCandidateRow,
        *,
        project_id: str,
        base_revision: str,
        base_snapshot_digest: str | None,
        board_snapshot_digest: str,
        rulepack_digest: str,
        capability_digest: str,
        authority_digest: str,
        operation_json: tuple[dict[str, Any], ...],
        operations_digest: str,
        algorithm_evidence: dict[str, Any] | None,
        idempotency_key: str,
    ) -> bool:
        """Compare every value frozen into a candidate creation request."""
        return (
            row.project_id == project_id
            and row.idempotency_key == idempotency_key
            and row.base_revision == base_revision
            and row.base_snapshot_digest == base_snapshot_digest
            and row.board_snapshot_digest == board_snapshot_digest
            and row.rulepack_digest == rulepack_digest
            and row.capability_digest == capability_digest
            and row.authority_digest == authority_digest
            and row.operations_json == list(operation_json)
            and row.operations_digest == operations_digest
            and row.algorithm_evidence_json == algorithm_evidence
        )

    def _find_existing_row(
        self, project_id: str, idempotency_key: str
    ) -> PcbCandidateRow | None:
        with self._sessions() as session:
            return session.scalar(
                select(PcbCandidateRow).where(
                    PcbCandidateRow.project_id == project_id,
                    PcbCandidateRow.idempotency_key == idempotency_key,
                )
            )

    def create_from_public_inputs(
        self,
        *,
        project_id: str,
        seed: int,
        net_ids: Sequence[str] | None,
        board_snapshot_digest: str | None,
        capability_digest: str | None,
        idempotency_key: str,
    ) -> PcbCandidate:
        """Resolve the frozen public inputs shared by the REST and CLI transports."""
        validate_candidate_public_inputs(
            seed=seed,
            net_ids=net_ids,
            board_snapshot_digest=board_snapshot_digest,
            capability_digest=capability_digest,
        )
        project = self._projects.get(project_id)
        existing = self.find_by_idempotency_key(project_id, idempotency_key)
        if project.current_revision is None and existing is None:
            raise RequestInvalidError("PCB_CANDIDATE_STALE")
        authority = self._authorities.find_by_project_id(project_id)
        if authority is None or authority.eda_kind is not EdaKind.LCEDA_PRO:
            raise RequestInvalidError("PCB_CAPABILITY_GATE_BLOCKED")
        base_revision = (
            existing.base_revision if existing is not None else project.current_revision
        )
        base_snapshot_digest = (
            existing.base_snapshot_digest
            if existing is not None
            else self._source_snapshot_digest(project.source_path)
        )
        resolved_board_digest = board_snapshot_digest or (
            existing.board_snapshot_digest
            if existing is not None
            else project.project_snapshot_digest
        )
        if resolved_board_digest is None:
            raise RequestInvalidError("PCB_CAPABILITY_GATE_BLOCKED")
        resolved_capability_digest = capability_digest
        if resolved_capability_digest is None:
            if existing is not None:
                resolved_capability_digest = existing.capability_digest
            elif self._evidence is not None:
                records = self._evidence.list_for_project(project_id)
                capability = next(
                    (
                        item
                        for item in reversed(records)
                        if item.kind == CAPABILITY_EVIDENCE_KIND
                        and item.verdict == "pass"
                    ),
                    None,
                )
                resolved_capability_digest = (
                    capability.artifact_digest if capability is not None else None
                )
        if resolved_capability_digest is None:
            raise RequestInvalidError("PCB_CAPABILITY_GATE_BLOCKED")
        return self.create(
            project_id=project_id,
            base_revision=base_revision or "",
            base_snapshot_digest=base_snapshot_digest,
            board_snapshot_digest=resolved_board_digest,
            rulepack_digest=authority.rulepack_digest,
            capability_digest=resolved_capability_digest,
            operations=(),
            algorithm_evidence={
                "algorithm_version": "boardir-only-v1",
                "seed": seed,
                "net_ids": list(net_ids or ()),
                "output_kind": "boardir_only",
            },
            idempotency_key=idempotency_key,
        )

    def _source_snapshot_digest(self, source_path: Path) -> str:
        if self._revisions is None:
            raise RequestInvalidError("PCB_CANDIDATE_SOURCE_UNAVAILABLE")
        return self._revisions.snapshot_digest(source_path)

    def create(
        self,
        *,
        project_id: str,
        base_revision: str,
        board_snapshot_digest: str,
        rulepack_digest: str,
        capability_digest: str,
        idempotency_key: str,
        base_snapshot_digest: str | None = None,
        operations: tuple[BoardOperation, ...] = (),
        algorithm_evidence: dict[str, Any] | None = None,
        require_capability: bool = True,
    ) -> PcbCandidate:
        validate_idempotency_key(idempotency_key)
        validate_candidate_digest(
            base_snapshot_digest, field="base_snapshot_digest", allow_none=True
        )
        validate_candidate_digest(
            board_snapshot_digest, field="board_snapshot_digest"
        )
        validate_candidate_digest(rulepack_digest, field="rulepack_digest")
        validate_candidate_digest(capability_digest, field="capability_digest")
        project = self._projects.get(project_id)
        authority = self._authorities.find_by_project_id(project_id)
        if authority is None or authority.eda_kind is not EdaKind.LCEDA_PRO:
            raise RequestInvalidError("project EDA authority is required")
        if type(operations) is not tuple or any(
            not isinstance(operation, BoardOperation) for operation in operations
        ):
            raise RequestInvalidError("operations must be a tuple of typed board operations")
        operation_json = tuple(_operation_payload(op) for op in operations)
        operations_digest = _digest(operation_json)
        canonical_algorithm_evidence = _canonical(algorithm_evidence)
        if isinstance(canonical_algorithm_evidence, dict):
            if "seed" in canonical_algorithm_evidence or "net_ids" in canonical_algorithm_evidence:
                validate_candidate_public_inputs(
                    seed=canonical_algorithm_evidence.get("seed", 0),
                    net_ids=canonical_algorithm_evidence.get("net_ids"),
                    board_snapshot_digest=board_snapshot_digest,
                    capability_digest=capability_digest,
                )
            output_kind = canonical_algorithm_evidence.get("output_kind")
            if output_kind not in {None, "boardir_only"}:
                raise RequestInvalidError(
                    "output_kind must be boardir_only until native evidence is verified"
                )
        authority_digest = authority.canonical_digest

        # Idempotent replays return the durable candidate before checking any
        # mutable capability state. The gate is a precondition for creating a
        # new candidate, not for reading an already-frozen result.
        existing = self._find_existing_row(project_id, idempotency_key)
        if existing is not None:
            if self._replay_matches(
                existing,
                project_id=project_id,
                base_revision=base_revision,
                base_snapshot_digest=base_snapshot_digest,
                board_snapshot_digest=board_snapshot_digest,
                rulepack_digest=rulepack_digest,
                capability_digest=capability_digest,
                authority_digest=authority_digest,
                operation_json=operation_json,
                operations_digest=operations_digest,
                algorithm_evidence=canonical_algorithm_evidence,
                idempotency_key=idempotency_key,
            ):
                return _candidate(existing)
            raise IdempotencyConflictError(idempotency_key)

        if project.current_revision is not None and project.current_revision != base_revision:
            raise RequestInvalidError("PCB_CANDIDATE_STALE")
        verified_capability_digest: str | None = None
        if require_capability:
            if self._capability_gate is None:
                raise LcedaProCapabilityError("LCEDA_PRO_WRITE_CAPABILITY_UNVERIFIED")
            verified_capability_digest = self._capability_gate.require_operation(
                project_id, EdaKind.LCEDA_PRO, EdaOperation.CREATE_CANDIDATE
            )
            if verified_capability_digest != capability_digest:
                raise LcedaProCapabilityError(
                    "LCEDA_PRO_WRITE_CAPABILITY_UNVERIFIED"
                )
        payload = {"project_id": project_id, "candidate_key": idempotency_key}
        now = utc_now()
        try:
            with self._sessions.begin() as session:
                # Serialize candidate creation with revision updates. The
                # earlier read is only a fast rejection; this is the source
                # of truth for the insert transaction.
                session.execute(text("BEGIN IMMEDIATE"))
                project_row = session.get(ProjectRow, project_id)
                if project_row is None:
                    raise ProjectNotFoundError(project_id)
                if (
                    project_row.current_revision is not None
                    and project_row.current_revision != base_revision
                ):
                    raise RequestInvalidError("PCB_CANDIDATE_STALE")
                existing = session.scalar(
                    select(PcbCandidateRow).where(
                        PcbCandidateRow.project_id == project_id,
                        PcbCandidateRow.idempotency_key == idempotency_key,
                    )
                )
                if existing is not None:
                    if self._replay_matches(
                        existing,
                        project_id=project_id,
                        base_revision=base_revision,
                        base_snapshot_digest=base_snapshot_digest,
                        board_snapshot_digest=board_snapshot_digest,
                        rulepack_digest=rulepack_digest,
                        capability_digest=capability_digest,
                        authority_digest=authority_digest,
                        operation_json=operation_json,
                        operations_digest=operations_digest,
                        algorithm_evidence=canonical_algorithm_evidence,
                        idempotency_key=idempotency_key,
                    ):
                        return _candidate(existing)
                    raise IdempotencyConflictError(idempotency_key)
                # 候选和内部任务共享该事务，候选失败不会遗留可领取的孤立任务。
                task = self._tasks.enqueue_in_session(
                    session,
                    PCB_GENERATE_CANDIDATE_TASK_KIND,
                    payload,
                    _task_idempotency_key(project_id, idempotency_key),
                    project_id,
                )
                row = PcbCandidateRow(id=new_id("pcb"), project_id=project_id, task_id=task.id, idempotency_key=idempotency_key,
                    base_revision=base_revision, base_snapshot_digest=base_snapshot_digest, board_snapshot_digest=board_snapshot_digest,
                    rulepack_digest=rulepack_digest, capability_digest=capability_digest, authority_digest=authority_digest,
                    operations_json=list(operation_json), operations_digest=operations_digest, algorithm_evidence_json=canonical_algorithm_evidence,
                    status=PcbCandidateStatus.QUEUED.value, result_json=None, last_error_code=None, accepted_revision=None,
                    created_at=now, updated_at=now, version=1)
                session.add(row)
                session.flush()
                return _candidate(row)
        except IntegrityError:
            # A concurrent request may have inserted the candidate after the
            # initial lookup. Re-apply the complete idempotency contract.
            with self._sessions() as session:
                existing = session.scalar(
                    select(PcbCandidateRow).where(
                        PcbCandidateRow.project_id == project_id,
                        PcbCandidateRow.idempotency_key == idempotency_key,
                    )
                )
                if existing is None or not self._replay_matches(
                    existing,
                    project_id=project_id,
                    base_revision=base_revision,
                    base_snapshot_digest=base_snapshot_digest,
                    board_snapshot_digest=board_snapshot_digest,
                    rulepack_digest=rulepack_digest,
                    capability_digest=capability_digest,
                    authority_digest=authority_digest,
                    operation_json=operation_json,
                    operations_digest=operations_digest,
                    algorithm_evidence=canonical_algorithm_evidence,
                    idempotency_key=idempotency_key,
                ):
                    raise IdempotencyConflictError(idempotency_key)
                return _candidate(existing)

    def get(self, candidate_id: str) -> PcbCandidate:
        with self._sessions() as session:
            row = session.get(PcbCandidateRow, candidate_id)
            if row is None: raise PcbCandidateNotFoundError(candidate_id)
            return _candidate(row)

    def find_by_idempotency_key(
        self, project_id: str, idempotency_key: str
    ) -> PcbCandidate | None:
        with self._sessions() as session:
            row = session.scalar(
                select(PcbCandidateRow).where(
                    PcbCandidateRow.project_id == project_id,
                    PcbCandidateRow.idempotency_key == idempotency_key,
                )
            )
            return _candidate(row) if row is not None else None

    def list_for_project(self, project_id: str) -> list[PcbCandidate]:
        with self._sessions() as session:
            return [_candidate(row) for row in session.scalars(select(PcbCandidateRow).where(PcbCandidateRow.project_id == project_id).order_by(PcbCandidateRow.created_at, PcbCandidateRow.id))]

    def _mirror_cancelled_task(
        self, candidate_id: str, task_id: str, now: datetime
    ) -> None:
        """将已持久取消的任务状态同步到尚未发布的候选。"""
        task = self._tasks.get(task_id)
        if getattr(task.status, "value", task.status) != "cancelled":
            return
        with self._sessions.begin() as session:
            row = session.get(PcbCandidateRow, candidate_id)
            if row is not None and row.status not in {
                PcbCandidateStatus.RELEASED.value,
                PcbCandidateStatus.CANCELLED.value,
            }:
                result = dict(row.result_json or {})
                release = result.get("release")
                if isinstance(release, dict) and release.get("task_id") == task_id:
                    result["release"] = {
                        "task_id": task_id,
                        "idempotency_key": release.get("idempotency_key"),
                        "status": "cancelled",
                        "error_code": "TASK_CANCELLED",
                    }
                    row.result_json = result
                row.status = PcbCandidateStatus.CANCELLED.value
                row.last_error_code = "TASK_CANCELLED"
                row.updated_at = now
                row.version += 1

    def _assert_active_or_mirror_cancellation(
        self, candidate_id: str, task_id: str, lease_token: str, now: datetime
    ) -> None:
        """在每个候选状态变更边界执行围栏，并同步竞态取消。"""
        try:
            self._tasks.assert_active(task_id, lease_token, now)
        except (StaleLeaseError, TaskCancelledError):
            self._mirror_cancelled_task(candidate_id, task_id, now)
            raise

    def _revert_stale_transition(
        self,
        candidate_id: str,
        task_id: str,
        written_status: PcbCandidateStatus,
        written_version: int,
        previous_status: str,
        previous_result: dict[str, Any] | None,
        previous_error_code: str | None,
        now: datetime,
    ) -> None:
        """仅在状态尚未被新租约推进时，撤销失效 Worker 的状态写入。"""
        with self._sessions.begin() as session:
            session.execute(
                update(PcbCandidateRow)
                .where(
                    PcbCandidateRow.id == candidate_id,
                    PcbCandidateRow.task_id == task_id,
                    PcbCandidateRow.status == written_status.value,
                    PcbCandidateRow.version == written_version,
                )
                .values(
                    status=previous_status,
                    result_json=previous_result,
                    last_error_code=previous_error_code,
                    updated_at=now,
                    version=written_version + 1,
                )
            )

    def _revert_stale_release_transition(
        self,
        candidate_id: str,
        task_id: str,
        written_version: int,
        previous_status: str,
        previous_result: dict[str, Any] | None,
        previous_error_code: str | None,
        now: datetime,
    ) -> None:
        with self._sessions.begin() as session:
            session.execute(
                update(PcbCandidateRow)
                .where(
                    PcbCandidateRow.id == candidate_id,
                    PcbCandidateRow.status == PcbCandidateStatus.READY_FOR_G4.value,
                    PcbCandidateRow.version == written_version,
                    PcbCandidateRow.result_json["release"]["task_id"].as_string()
                    == task_id,
                )
                .values(
                    status=previous_status,
                    result_json=previous_result,
                    last_error_code=previous_error_code,
                    updated_at=now,
                    version=written_version + 1,
                )
                .execution_options(synchronize_session=False)
            )

    def _transition(
        self,
        candidate_id: str,
        allowed: set[PcbCandidateStatus],
        status: PcbCandidateStatus,
        task_id: str,
        lease_token: str,
        now: datetime,
        *,
        result: dict[str, Any] | None = None,
        error_code: str | None = None,
    ) -> PcbCandidate:
        self._assert_active_or_mirror_cancellation(
            candidate_id, task_id, lease_token, now
        )
        transition_failed = False
        transition_now = now
        with self._sessions.begin() as session:
            # Re-check the clock only after acquiring the write lock so a
            # worker cannot publish a transition using a stale pre-lock time.
            session.execute(text("BEGIN IMMEDIATE"))
            transition_now = max(now, utc_now())
            row = session.get(PcbCandidateRow, candidate_id)
            if row is None:
                raise PcbCandidateNotFoundError(candidate_id)
            if row.task_id != task_id:
                raise RequestInvalidError("invalid PCB candidate transition")
            if row.status == PcbCandidateStatus.CANCELLED.value:
                raise TaskCancelledError(task_id)
            if PcbCandidateStatus(row.status) not in allowed:
                raise RequestInvalidError("invalid PCB candidate transition")
            if status is PcbCandidateStatus.READY_FOR_G3:
                self._validate_ready_result(row, result)
            previous_status = row.status
            previous_result = row.result_json
            previous_error_code = row.last_error_code
            written_version = row.version + 1
            active_task = exists().where(
                TaskRow.id == task_id,
                TaskRow.lease_token == lease_token,
                TaskRow.status == TaskStatus.RUNNING.value,
                TaskRow.lease_expires_at.is_not(None),
                TaskRow.lease_expires_at > transition_now,
            )
            changed = session.execute(
                update(PcbCandidateRow)
                .where(
                    PcbCandidateRow.id == candidate_id,
                    PcbCandidateRow.task_id == task_id,
                    PcbCandidateRow.status.in_(item.value for item in allowed),
                    PcbCandidateRow.version == row.version,
                    active_task,
                )
                .values(
                    status=status.value,
                    result_json=result,
                    last_error_code=error_code,
                    updated_at=transition_now,
                    version=row.version + 1,
                )
                .execution_options(synchronize_session=False)
            )
            transition_failed = changed.rowcount != 1
        if transition_failed:
            self._assert_active_or_mirror_cancellation(
                candidate_id, task_id, lease_token, transition_now
            )
            raise RequestInvalidError("concurrent PCB candidate update")
        try:
            self._assert_active_or_mirror_cancellation(
                candidate_id, task_id, lease_token, transition_now
            )
        except StaleLeaseError:
            self._revert_stale_transition(
                candidate_id,
                task_id,
                status,
                written_version,
                previous_status,
                previous_result,
                previous_error_code,
                transition_now,
            )
            raise
        return self.get(candidate_id)

    @staticmethod
    def _validate_ready_result(
        row: PcbCandidateRow, result: dict[str, Any] | None
    ) -> None:
        if not isinstance(result, dict):
            raise PcbCandidateNotReviewableError()
        if (
            result.get("operations_digest") != row.operations_digest
            or result.get("board_snapshot_digest") != row.board_snapshot_digest
        ):
            raise PcbCandidateNotReviewableError()
        try:
            candidate_digest = result.get("candidate_digest")
            validate_candidate_digest(candidate_digest, field="candidate_digest")
            post_snapshot_digest = result.get("candidate_board_snapshot_digest")
            validate_candidate_digest(
                post_snapshot_digest, field="candidate_board_snapshot_digest"
            )
        except RequestInvalidError as error:
            raise PcbCandidateNotReviewableError() from error
        for field in ("rulepack_digest", "capability_digest", "authority_digest"):
            if result.get(field) != getattr(row, field):
                raise PcbCandidateNotReviewableError()
        if result.get("native_candidate_verified") is not True:
            raise PcbCandidateNotReviewableError()
        evidence_kinds = result.get("evidence_kinds")
        if (
            type(evidence_kinds) is not list
            or any(type(item) is not str for item in evidence_kinds)
            or frozenset(evidence_kinds) != G3_REQUIRED_EVIDENCE
            or len(evidence_kinds) != len(G3_REQUIRED_EVIDENCE)
        ):
            raise PcbCandidateNotReviewableError(
                "PCB_CANDIDATE_EVIDENCE_INCOMPLETE"
            )
        for field in ("blocking_finding_count", "unconnected_net_count"):
            if type(result.get(field)) is not int or result[field] != 0:
                raise PcbCandidateNotReviewableError()
        try:
            evidence_set_digest = result.get("evidence_set_digest")
            validate_candidate_digest(evidence_set_digest, field="evidence_set_digest")
        except RequestInvalidError as error:
            raise PcbCandidateNotReviewableError() from error
        if candidate_digest != pcb_candidate_review_digest(
            candidate_id=row.id,
            project_id=row.project_id,
            base_revision=row.base_revision,
            base_snapshot_digest=row.base_snapshot_digest,
            board_snapshot_digest=row.board_snapshot_digest,
            candidate_board_snapshot_digest=post_snapshot_digest,
            rulepack_digest=row.rulepack_digest,
            capability_digest=row.capability_digest,
            authority_digest=row.authority_digest,
            operations_digest=row.operations_digest,
            evidence_set_digest=evidence_set_digest,
        ):
            raise PcbCandidateNotReviewableError()

    def mark_executing(self, candidate_id: str, task_id: str, lease_token: str, now: datetime) -> PcbCandidate:
        return self._transition(
            candidate_id,
            {PcbCandidateStatus.QUEUED, PcbCandidateStatus.EXECUTING},
            PcbCandidateStatus.EXECUTING,
            task_id,
            lease_token,
            now,
        )

    def mark_ready_for_g3(self, candidate_id: str, task_id: str, lease_token: str, now: datetime, *, result: dict[str, Any] | None = None) -> PcbCandidate:
        return self._transition(candidate_id, {PcbCandidateStatus.EXECUTING}, PcbCandidateStatus.READY_FOR_G3, task_id, lease_token, now, result=result)

    def mark_blocked(
        self,
        candidate_id: str,
        task_id: str,
        lease_token: str,
        now: datetime,
        *,
        result: dict[str, Any] | None = None,
        error_code: str = "PCB_CAPABILITY_GATE_BLOCKED",
    ) -> PcbCandidate:
        return self._transition(
            candidate_id,
            {PcbCandidateStatus.QUEUED, PcbCandidateStatus.EXECUTING},
            PcbCandidateStatus.BLOCKED,
            task_id,
            lease_token,
            now,
            result=result,
            error_code=error_code,
        )

    def mark_validation_failed(
        self,
        candidate_id: str,
        task_id: str,
        lease_token: str,
        now: datetime,
        error_code: str,
        *,
        result: dict[str, Any] | None = None,
    ) -> PcbCandidate:
        return self._transition(
            candidate_id,
            {PcbCandidateStatus.EXECUTING},
            PcbCandidateStatus.VALIDATION_FAILED,
            task_id,
            lease_token,
            now,
            result=result,
            error_code=error_code,
        )

    @staticmethod
    def _register_release_artifacts(
        session: Session,
        descriptors: Sequence[ArtifactDescriptor],
        now: datetime,
    ) -> None:
        for descriptor in descriptors:
            try:
                available = descriptor.path.is_file()
                actual_size = descriptor.path.stat().st_size
            except OSError as error:
                raise RequestInvalidError(
                    "PCB_RELEASE_ARTIFACT_UNAVAILABLE"
                ) from error
            if not available or actual_size != descriptor.size:
                raise RequestInvalidError("PCB_RELEASE_ARTIFACT_UNAVAILABLE")
            row = session.get(ArtifactRow, descriptor.digest)
            if row is None:
                session.add(
                    ArtifactRow(
                        digest=descriptor.digest,
                        size=descriptor.size,
                        media_type=descriptor.media_type,
                        storage_path=str(descriptor.path),
                        created_at=now,
                    )
                )
            elif (
                row.size != descriptor.size
                or row.media_type != descriptor.media_type
                or row.storage_path != str(descriptor.path)
            ):
                raise RequestInvalidError("PCB_RELEASE_ARTIFACT_CONFLICT")

    def settle_publication(
        self,
        descriptors: Sequence[ArtifactDescriptor],
        staged: Sequence[StagedArtifact],
    ) -> None:
        """Safely settle published release objects against durable registrations."""
        if len(descriptors) != len(staged):
            raise ValueError("release publication descriptor/staging mismatch")
        pairs = tuple(zip(descriptors, staged, strict=True))
        for descriptor, artifact in pairs:
            if (
                descriptor.digest != artifact.digest
                or descriptor.size != artifact.size
                or descriptor.media_type != artifact.media_type
            ):
                raise ValueError("release publication descriptor/staging mismatch")
        with self._sessions.begin() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            for descriptor, artifact in pairs:
                if session.get(ArtifactRow, descriptor.digest) is not None:
                    artifact.discard()
                else:
                    artifact.rollback()

    def settle_release_publication(
        self,
        descriptors: Sequence[ArtifactDescriptor],
        staged: Sequence[StagedArtifact],
    ) -> None:
        """Backward-compatible alias for release publication settlement."""
        self.settle_publication(descriptors, staged)

    def enqueue_release_export(self, candidate_id: str, idempotency_key: str):
        """Atomically enqueue a release task and reserve the G3-approved candidate."""
        validate_idempotency_key(idempotency_key)
        with self._sessions.begin() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row = session.get(PcbCandidateRow, candidate_id)
            if row is None:
                raise PcbCandidateNotFoundError(candidate_id)
            result = dict(row.result_json or {})
            release = result.get("release")
            if isinstance(release, dict):
                if (
                    row.status == PcbCandidateStatus.G3_APPROVED.value
                    and release.get("status") == "failed"
                ):
                    if release.get("idempotency_key") == idempotency_key:
                        raise RequestInvalidError("PCB_RELEASE_RETRY_KEY_REQUIRED")
                    release = None
                if release is None:
                    result.pop("release", None)
            if isinstance(release, dict):
                if release.get("idempotency_key") != idempotency_key:
                    raise RequestInvalidError("PCB_RELEASE_ALREADY_REQUESTED")
                task_id = release.get("task_id")
                if not isinstance(task_id, str):
                    raise PcbCandidateNotReviewableError()
                task = self._tasks.enqueue_in_session(
                    session,
                    PCB_EXPORT_RELEASE_TASK_KIND,
                    {"project_id": row.project_id, "candidate_id": row.id},
                    _release_task_idempotency_key(row.id, idempotency_key),
                    row.project_id,
                )
                if task.id != task_id:
                    raise PcbCandidateNotReviewableError()
                return task
            if row.status != PcbCandidateStatus.G3_APPROVED.value:
                raise PcbCandidateNotReviewableError()
            g3 = result.get("g3_decision")
            if (
                not isinstance(g3, dict)
                or g3.get("decision") != "approve"
                or not isinstance(result.get("candidate_digest"), str)
            ):
                raise PcbCandidateNotReviewableError()
            task = self._tasks.enqueue_in_session(
                session,
                PCB_EXPORT_RELEASE_TASK_KIND,
                {"project_id": row.project_id, "candidate_id": row.id},
                _release_task_idempotency_key(row.id, idempotency_key),
                row.project_id,
            )
            result["release"] = {
                "task_id": task.id,
                "idempotency_key": idempotency_key,
                "status": "pending",
            }
            row.status = PcbCandidateStatus.RELEASE_PENDING.value
            row.result_json = result
            row.last_error_code = None
            row.updated_at = utc_now()
            row.version += 1
            return task

    def mark_ready_for_g4(
        self,
        candidate_id: str,
        task_id: str,
        lease_token: str,
        now: datetime,
        *,
        release_result: dict[str, Any],
        descriptors: Sequence[ArtifactDescriptor],
    ) -> PcbCandidate:
        self._assert_active_or_mirror_cancellation(
            candidate_id, task_id, lease_token, now
        )
        transition_now = now
        previous_status = PcbCandidateStatus.RELEASE_PENDING.value
        previous_result: dict[str, Any] | None = None
        previous_error_code: str | None = None
        written_version = 0
        with self._sessions.begin() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            transition_now = max(now, utc_now())
            row = session.get(PcbCandidateRow, candidate_id)
            if row is None:
                raise PcbCandidateNotFoundError(candidate_id)
            result = dict(row.result_json or {})
            release = result.get("release")
            if (
                row.status != PcbCandidateStatus.RELEASE_PENDING.value
                or not isinstance(release, dict)
                or release.get("task_id") != task_id
            ):
                raise PcbCandidateNotReviewableError()
            previous_status = row.status
            previous_result = row.result_json
            previous_error_code = row.last_error_code
            written_version = row.version + 1
            active_task = exists().where(
                TaskRow.id == task_id,
                TaskRow.lease_token == lease_token,
                TaskRow.status == TaskStatus.RUNNING.value,
                TaskRow.lease_expires_at.is_not(None),
                TaskRow.lease_expires_at > transition_now,
            )
            changed = session.execute(
                update(PcbCandidateRow)
                .where(
                    PcbCandidateRow.id == candidate_id,
                    PcbCandidateRow.status == PcbCandidateStatus.RELEASE_PENDING.value,
                    PcbCandidateRow.version == row.version,
                    active_task,
                )
                .values(
                    status=PcbCandidateStatus.READY_FOR_G4.value,
                    result_json={**result, "release": {**release, **release_result, "status": "ready_for_g4"}},
                    last_error_code=None,
                    updated_at=transition_now,
                    version=row.version + 1,
                )
                .execution_options(synchronize_session=False)
            )
            if changed.rowcount != 1:
                raise RequestInvalidError("concurrent PCB release update")
            self._register_release_artifacts(session, descriptors, transition_now)
        try:
            self._assert_active_or_mirror_cancellation(
                candidate_id, task_id, lease_token, transition_now
            )
        except StaleLeaseError:
            self._revert_stale_release_transition(
                candidate_id,
                task_id,
                written_version,
                previous_status,
                previous_result,
                previous_error_code,
                transition_now,
            )
            raise
        return self.get(candidate_id)

    def restore_g3_after_release_failure(
        self,
        candidate_id: str,
        task_id: str,
        lease_token: str,
        now: datetime,
        *,
        error_code: str,
    ) -> PcbCandidate:
        self._tasks.assert_active(task_id, lease_token, now)
        with self._sessions.begin() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            transition_now = max(now, utc_now())
            row = session.get(PcbCandidateRow, candidate_id)
            if row is None:
                raise PcbCandidateNotFoundError(candidate_id)
            result = dict(row.result_json or {})
            release = result.get("release")
            if (
                row.status != PcbCandidateStatus.RELEASE_PENDING.value
                or not isinstance(release, dict)
                or release.get("task_id") != task_id
            ):
                raise PcbCandidateNotReviewableError()
            active_task = exists().where(
                TaskRow.id == task_id,
                TaskRow.lease_token == lease_token,
                TaskRow.status == TaskStatus.RUNNING.value,
                TaskRow.lease_expires_at.is_not(None),
                TaskRow.lease_expires_at > transition_now,
            )
            changed = session.execute(
                update(PcbCandidateRow)
                .where(
                    PcbCandidateRow.id == candidate_id,
                    PcbCandidateRow.status == PcbCandidateStatus.RELEASE_PENDING.value,
                    PcbCandidateRow.version == row.version,
                    active_task,
                )
                .values(
                    status=PcbCandidateStatus.G3_APPROVED.value,
                    result_json={**result, "release": {**release, "status": "failed", "error_code": error_code}},
                    last_error_code=error_code,
                    updated_at=transition_now,
                    version=row.version + 1,
                )
                .execution_options(synchronize_session=False)
            )
            if changed.rowcount != 1:
                raise RequestInvalidError("concurrent PCB release update")
        return self.get(candidate_id)

class PcbCandidateService:
    def __init__(self, store: PcbCandidateStore) -> None: self._store = store
    def create(self, **kwargs: Any) -> PcbCandidate: return self._store.create(**kwargs)
    def __getattr__(self, name: str) -> Any: return getattr(self._store, name)

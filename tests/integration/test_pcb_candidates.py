from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from pcbflow.board import BoardObjectId, PlaceFootprints, PointUm
from pcbflow.board.operations import FootprintPlacement
from pcbflow.cancellation import TaskCancelledError
from pcbflow.design_tables import PcbCandidateRow
from pcbflow.domain import (
    EdaKind,
    RequestInvalidError,
    TaskStatus,
    utc_now,
)
from pcbflow.eda import ProjectEdaAuthorityInput
from pcbflow.lceda_pro import LcedaProCapabilityError
from pcbflow.pcb_candidates import (
    PCB_GENERATE_CANDIDATE_TASK_KIND,
    PcbCandidateNotReviewableError,
    PcbCandidateStatus,
)
from pcbflow.repositories import IdempotencyConflictError, StaleLeaseError
from pcbflow.tables import TaskRow

_DIGEST = "sha256:" + "a" * 64


@pytest.fixture
def lceda_project(container, tmp_path: Path):
    source = tmp_path / "lceda-project"
    source.mkdir()
    (source / "board.kicad_pcb").write_text("fixture", encoding="utf-8")
    project = container.projects.create("LCEDA board", source, "pcb-project")
    container.eda_authorities.configure(
        project.id,
        ProjectEdaAuthorityInput(
            eda_kind=EdaKind.LCEDA_PRO,
            eda_profile_id="lceda-pro-v1",
            board_profile_id="stm32-environment-controller-2l-v1",
            rulepack_digest=_DIGEST,
        ),
        "pcb-authority",
    )
    return container.projects.get(project.id)


def _operation(project_id: str, key: str = "operation-1") -> PlaceFootprints:
    return PlaceFootprints(
        project_id=project_id,
        baseline_revision="git:" + "b" * 40,
        risk="low",
        rulepack_digest=_DIGEST,
        target_object_ids=(BoardObjectId("U_MCU"),),
        idempotency_key=key,
        expected_snapshot_digest="sha256:" + "c" * 64,
        placements=(
            FootprintPlacement(
                BoardObjectId("U_MCU"), PointUm(50_000, 40_000), "F.Cu"
            ),
        ),
    )


def test_candidate_creation_freezes_inputs_and_is_idempotent(container, lceda_project):
    candidate = container.pcb_candidates.create(
        project_id=lceda_project.id,
        base_revision="git:" + "b" * 40,
        base_snapshot_digest="sha256:" + "d" * 64,
        board_snapshot_digest="sha256:" + "c" * 64,
        rulepack_digest=_DIGEST,
        capability_digest="sha256:" + "e" * 64,
        operations=(_operation(lceda_project.id),),
        algorithm_evidence={"objective_version": "placement-v1", "seed": 7},
        idempotency_key="candidate-1",
        require_capability=False,
    )

    replay = container.pcb_candidates.create(
        project_id=lceda_project.id,
        base_revision="git:" + "b" * 40,
        base_snapshot_digest="sha256:" + "d" * 64,
        board_snapshot_digest="sha256:" + "c" * 64,
        rulepack_digest=_DIGEST,
        capability_digest="sha256:" + "e" * 64,
        operations=(_operation(lceda_project.id),),
        algorithm_evidence={"seed": 7, "objective_version": "placement-v1"},
        idempotency_key="candidate-1",
        require_capability=False,
    )

    assert replay == candidate
    assert candidate.status is PcbCandidateStatus.QUEUED
    assert candidate.authority_digest
    assert candidate.operations_digest.startswith("sha256:")
    assert candidate.task_id
    assert candidate.accepted_revision is None


def test_candidate_creation_fails_closed_when_capability_is_not_verified(
    container, lceda_project
):
    with pytest.raises(LcedaProCapabilityError) as raised:
        container.pcb_candidates.create(
            project_id=lceda_project.id,
            base_revision="git:" + "b" * 40,
            base_snapshot_digest="sha256:" + "d" * 64,
            board_snapshot_digest="sha256:" + "c" * 64,
            rulepack_digest=_DIGEST,
            capability_digest="sha256:" + "e" * 64,
            operations=(_operation(lceda_project.id),),
            idempotency_key="candidate-blocked",
        )
    assert raised.value.code == "LCEDA_PRO_WRITE_CAPABILITY_UNVERIFIED"
    assert container.pcb_candidates.list_for_project(lceda_project.id) == []


def test_public_candidate_freezes_the_external_source_snapshot(
    container, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "public-candidate-source"
    source.mkdir()
    (source / "board.kicad_pcb").write_text("fixture", encoding="utf-8")
    project = container.projects.create("LCEDA board", source, "public-candidate-source")
    container.eda_authorities.configure(
        project.id,
        ProjectEdaAuthorityInput(
            eda_kind=EdaKind.LCEDA_PRO,
            eda_profile_id="lceda-pro-v1",
            board_profile_id="stm32-environment-controller-2l-v1",
            rulepack_digest=_DIGEST,
        ),
        "public-candidate-source-authority",
    )
    managed = container.revisions.adopt(project.id, "public-candidate-source-adopt")
    capability_digest = "sha256:" + "e" * 64
    monkeypatch.setattr(
        container.capability_gate,
        "require_operation",
        lambda *_args: capability_digest,
    )

    candidate = container.pcb_candidates.create_from_public_inputs(
        project_id=managed.id,
        seed=7,
        net_ids=("I2C_SCL",),
        board_snapshot_digest=managed.project_snapshot_digest,
        capability_digest=capability_digest,
        idempotency_key="public-candidate-source",
    )

    assert candidate.base_snapshot_digest == container.revisions.snapshot_digest(source)
    assert candidate.base_snapshot_digest != managed.project_snapshot_digest


def test_candidate_idempotency_keys_are_scoped_to_the_project(
    container, lceda_project, tmp_path: Path
):
    other_source = tmp_path / "other-lceda-project"
    other_source.mkdir()
    other = container.projects.create("Other LCEDA board", other_source, "other-lceda")
    container.eda_authorities.configure(
        other.id,
        ProjectEdaAuthorityInput(
            eda_kind=EdaKind.LCEDA_PRO,
            eda_profile_id="lceda-pro-v1",
            board_profile_id="stm32-environment-controller-2l-v1",
            rulepack_digest=_DIGEST,
        ),
        "other-authority",
    )
    kwargs = {
        "base_revision": "git:" + "b" * 40,
        "base_snapshot_digest": "sha256:" + "d" * 64,
        "board_snapshot_digest": "sha256:" + "c" * 64,
        "rulepack_digest": _DIGEST,
        "capability_digest": "sha256:" + "e" * 64,
        "operations": (),
        "idempotency_key": "same-candidate-key",
        "require_capability": False,
    }

    first = container.pcb_candidates.create(project_id=lceda_project.id, **kwargs)
    second = container.pcb_candidates.create(project_id=other.id, **kwargs)

    assert first.id != second.id
    assert first.task_id != second.task_id


def test_candidate_can_be_found_by_project_and_idempotency_key(
    container, lceda_project
):
    candidate = container.pcb_candidates.create(
        project_id=lceda_project.id,
        base_revision="git:" + "b" * 40,
        base_snapshot_digest="sha256:" + "d" * 64,
        board_snapshot_digest="sha256:" + "c" * 64,
        rulepack_digest=_DIGEST,
        capability_digest="sha256:" + "e" * 64,
        operations=(_operation(lceda_project.id),),
        idempotency_key="candidate-lookup",
        require_capability=False,
    )

    found = container.pcb_candidates.find_by_idempotency_key(
        lceda_project.id, "candidate-lookup"
    )

    assert found == candidate


def test_candidate_replay_rejects_changed_frozen_inputs(container, lceda_project):
    kwargs = {
        "project_id": lceda_project.id,
        "base_revision": "git:" + "b" * 40,
        "base_snapshot_digest": "sha256:" + "d" * 64,
        "board_snapshot_digest": "sha256:" + "c" * 64,
        "rulepack_digest": _DIGEST,
        "capability_digest": "sha256:" + "e" * 64,
        "operations": (_operation(lceda_project.id),),
        "idempotency_key": "candidate-input-conflict",
        "require_capability": False,
    }
    original = container.pcb_candidates.create(**kwargs)
    with pytest.raises(IdempotencyConflictError):
        container.pcb_candidates.create(
            **{**kwargs, "board_snapshot_digest": "sha256:" + "9" * 64}
        )
    assert container.pcb_candidates.get(original.id).board_snapshot_digest == "sha256:" + "c" * 64


def test_candidate_replay_is_allowed_after_capability_gate_changes(container, lceda_project):
    kwargs = {
        "project_id": lceda_project.id,
        "base_revision": "git:" + "b" * 40,
        "base_snapshot_digest": "sha256:" + "d" * 64,
        "board_snapshot_digest": "sha256:" + "c" * 64,
        "rulepack_digest": _DIGEST,
        "capability_digest": "sha256:" + "e" * 64,
        "operations": (_operation(lceda_project.id),),
        "idempotency_key": "candidate-gate-replay",
        "require_capability": False,
    }
    original = container.pcb_candidates.create(**kwargs)
    replay = container.pcb_candidates.create(**{**kwargs, "require_capability": True})
    assert replay == original


def test_candidate_state_transitions_require_active_task_lease(container, lceda_project):
    candidate = container.pcb_candidates.create(
        project_id=lceda_project.id,
        base_revision="git:" + "b" * 40,
        base_snapshot_digest="sha256:" + "d" * 64,
        board_snapshot_digest="sha256:" + "c" * 64,
        rulepack_digest=_DIGEST,
        capability_digest="sha256:" + "e" * 64,
        operations=(_operation(lceda_project.id),),
        idempotency_key="candidate-fence",
        require_capability=False,
    )
    lease = container.tasks.claim_next("candidate-worker", utc_now(), 30)
    assert lease is not None and lease.task_id == candidate.task_id
    container.tasks.start(lease.task_id, lease.lease_token, utc_now())
    container.tasks.cancel(lease.task_id, "operator stopped", utc_now())

    with pytest.raises((StaleLeaseError, RequestInvalidError, TaskCancelledError)):
        container.pcb_candidates.mark_executing(
            candidate.id, lease.task_id, lease.lease_token, utc_now()
        )
    assert container.pcb_candidates.get(candidate.id).status is PcbCandidateStatus.CANCELLED


def test_candidate_transition_mirrors_cancellation_after_state_update(
    container, lceda_project, monkeypatch
):
    """候选状态写入后才到达的取消也必须被镜像为 cancelled。"""
    candidate = container.pcb_candidates.create(
        project_id=lceda_project.id,
        base_revision="git:" + "b" * 40,
        base_snapshot_digest="sha256:" + "d" * 64,
        board_snapshot_digest="sha256:" + "c" * 64,
        rulepack_digest=_DIGEST,
        capability_digest="sha256:" + "e" * 64,
        operations=(_operation(lceda_project.id),),
        idempotency_key="candidate-late-cancellation",
        require_capability=False,
    )
    lease = container.tasks.claim_next("candidate-worker", utc_now(), 30)
    assert lease is not None
    now = utc_now()
    container.tasks.start(lease.task_id, lease.lease_token, now)
    assert_active = container.tasks.assert_active
    checks = 0

    def cancel_after_first_fence(task_id: str, lease_token: str, checked_at) -> None:
        """在首次围栏成功后注入取消，模拟 Worker 与取消命令的竞态。"""
        nonlocal checks
        assert_active(task_id, lease_token, checked_at)
        checks += 1
        if checks == 1:
            container.tasks.cancel(task_id, "operator stopped", checked_at)

    monkeypatch.setattr(container.tasks, "assert_active", cancel_after_first_fence)

    with pytest.raises(TaskCancelledError):
        container.pcb_candidates.mark_executing(
            candidate.id, lease.task_id, lease.lease_token, now
        )

    assert checks == 1
    assert container.pcb_candidates.get(candidate.id).status is PcbCandidateStatus.CANCELLED


def test_candidate_transition_reverts_when_lease_is_lost_after_state_update(
    container, lceda_project, monkeypatch
):
    """第二次围栏发现租约失效时，不得留下已发布的候选状态。"""
    candidate = container.pcb_candidates.create(
        project_id=lceda_project.id,
        base_revision="git:" + "b" * 40,
        base_snapshot_digest="sha256:" + "d" * 64,
        board_snapshot_digest="sha256:" + "c" * 64,
        rulepack_digest=_DIGEST,
        capability_digest="sha256:" + "e" * 64,
        operations=(_operation(lceda_project.id),),
        idempotency_key="candidate-late-lease-loss",
        require_capability=False,
    )
    lease = container.tasks.claim_next("candidate-worker", utc_now(), 30)
    assert lease is not None
    now = utc_now()
    container.tasks.start(lease.task_id, lease.lease_token, now)
    assert_active = container.tasks.assert_active
    checks = 0

    def lose_lease_after_first_fence(task_id: str, lease_token: str, checked_at) -> None:
        """首次围栏成功后模拟租约被其他 Worker 接管。"""
        nonlocal checks
        if checks == 0:
            checks += 1
            assert_active(task_id, lease_token, checked_at)
            return
        raise StaleLeaseError(task_id)

    monkeypatch.setattr(container.tasks, "assert_active", lose_lease_after_first_fence)

    with pytest.raises(StaleLeaseError):
        container.pcb_candidates.mark_executing(
            candidate.id, lease.task_id, lease.lease_token, now
        )

    assert checks == 1
    restored = container.pcb_candidates.get(candidate.id)
    assert restored.status is PcbCandidateStatus.QUEUED
    assert restored.result is None


def test_candidate_transition_uses_time_after_acquiring_database_lock(
    container, lceda_project, monkeypatch
):
    candidate = container.pcb_candidates.create(
        project_id=lceda_project.id,
        base_revision="git:" + "b" * 40,
        base_snapshot_digest="sha256:" + "d" * 64,
        board_snapshot_digest="sha256:" + "c" * 64,
        rulepack_digest=_DIGEST,
        capability_digest="sha256:" + "e" * 64,
        operations=(_operation(lceda_project.id),),
        idempotency_key="candidate-lock-time-fence",
        require_capability=False,
    )
    lease = container.tasks.claim_next("candidate-lock-time-worker", utc_now(), 30)
    assert lease is not None
    started_at = utc_now()
    container.tasks.start(lease.task_id, lease.lease_token, started_at)
    lease_expiry = started_at + timedelta(seconds=1)
    with container.sessions.begin() as session:
        row = session.get(TaskRow, lease.task_id)
        assert row is not None
        row.lease_expires_at = lease_expiry
    monkeypatch.setattr(
        "pcbflow.pcb_candidate_store.utc_now", lambda: lease_expiry + timedelta(seconds=1)
    )

    with pytest.raises(StaleLeaseError):
        container.pcb_candidates.mark_executing(
            candidate.id, lease.task_id, lease.lease_token, started_at
        )

    assert container.pcb_candidates.get(candidate.id).status is PcbCandidateStatus.QUEUED


def test_failed_transition_rollback_restores_previous_error_code(
    container, lceda_project, monkeypatch
):
    candidate = container.pcb_candidates.create(
        project_id=lceda_project.id,
        base_revision="git:" + "b" * 40,
        base_snapshot_digest="sha256:" + "d" * 64,
        board_snapshot_digest="sha256:" + "c" * 64,
        rulepack_digest=_DIGEST,
        capability_digest="sha256:" + "e" * 64,
        operations=(_operation(lceda_project.id),),
        idempotency_key="candidate-late-lease-error-code",
        require_capability=False,
    )
    lease = container.tasks.claim_next("candidate-worker", utc_now(), 30)
    assert lease is not None
    now = utc_now()
    container.tasks.start(lease.task_id, lease.lease_token, now)
    container.pcb_candidates.mark_executing(
        candidate.id, lease.task_id, lease.lease_token, now
    )
    assert_active = container.tasks.assert_active
    checks = 0

    def lose_lease_after_first_fence(task_id: str, lease_token: str, checked_at) -> None:
        nonlocal checks
        if checks == 0:
            checks += 1
            assert_active(task_id, lease_token, checked_at)
            return
        raise StaleLeaseError(task_id)

    monkeypatch.setattr(container.tasks, "assert_active", lose_lease_after_first_fence)

    with pytest.raises(StaleLeaseError):
        container.pcb_candidates.mark_validation_failed(
            candidate.id,
            lease.task_id,
            lease.lease_token,
            now,
            "PCB_TEST_FAILURE",
        )

    restored = container.pcb_candidates.get(candidate.id)
    assert restored.status is PcbCandidateStatus.EXECUTING
    assert restored.last_error_code is None


def test_candidate_status_transition_guards_and_reviewability(container, lceda_project):
    candidate = container.pcb_candidates.create(
        project_id=lceda_project.id,
        base_revision="git:" + "b" * 40,
        base_snapshot_digest="sha256:" + "d" * 64,
        board_snapshot_digest="sha256:" + "c" * 64,
        rulepack_digest=_DIGEST,
        capability_digest="sha256:" + "e" * 64,
        operations=(_operation(lceda_project.id),),
        idempotency_key="candidate-transition",
        require_capability=False,
    )
    lease = container.tasks.claim_next("candidate-worker", utc_now(), 30)
    assert lease is not None
    now = utc_now()
    container.tasks.start(lease.task_id, lease.lease_token, now)
    executing = container.pcb_candidates.mark_executing(
        candidate.id, lease.task_id, lease.lease_token, now
    )
    assert executing.status is PcbCandidateStatus.EXECUTING
    with pytest.raises(PcbCandidateNotReviewableError):
        container.pcb_candidates.mark_ready_for_g3(
            candidate.id,
            lease.task_id,
            lease.lease_token,
            now + timedelta(seconds=1),
            result={
                "candidate_digest": "sha256:" + "f" * 64,
                "operations_digest": candidate.operations_digest,
                "board_snapshot_digest": candidate.board_snapshot_digest,
            },
        )
    assert container.pcb_candidates.get(candidate.id).status is PcbCandidateStatus.EXECUTING


def test_candidate_task_kind_is_public_and_worker_marks_blocked_result(container, lceda_project):
    assert PCB_GENERATE_CANDIDATE_TASK_KIND == "pcb.generate_candidate"
    candidate = container.pcb_candidates.create(
        project_id=lceda_project.id,
        base_revision="git:" + "b" * 40,
        base_snapshot_digest="sha256:" + "d" * 64,
        board_snapshot_digest="sha256:" + "c" * 64,
        rulepack_digest=_DIGEST,
        capability_digest="sha256:" + "e" * 64,
        operations=(_operation(lceda_project.id),),
        idempotency_key="candidate-worker",
        require_capability=False,
    )
    assert container.worker.run_once() is True
    task = container.tasks.get(candidate.task_id)
    assert task.status is TaskStatus.SUCCEEDED
    assert container.pcb_candidates.get(candidate.id).status is PcbCandidateStatus.BLOCKED


def test_candidate_creation_rolls_back_task_when_candidate_insert_fails(
    container, lceda_project
):
    """候选行写入失败时，候选任务必须随同一个事务回滚。"""

    def reject_candidate_flush(session: Session, flush_context, instances) -> None:
        """仅模拟候选持久化阶段的数据库失败。"""
        if any(isinstance(row, PcbCandidateRow) for row in session.new):
            raise RuntimeError("simulated PCB candidate insert failure")

    event.listen(Session, "before_flush", reject_candidate_flush)
    try:
        with pytest.raises(RuntimeError, match="simulated PCB candidate insert failure"):
            container.pcb_candidates.create(
                project_id=lceda_project.id,
                base_revision="git:" + "b" * 40,
                base_snapshot_digest="sha256:" + "d" * 64,
                board_snapshot_digest="sha256:" + "c" * 64,
                rulepack_digest=_DIGEST,
                capability_digest="sha256:" + "e" * 64,
                operations=(_operation(lceda_project.id),),
                idempotency_key="candidate-atomic-rollback",
                require_capability=False,
            )
    finally:
        event.remove(Session, "before_flush", reject_candidate_flush)

    with container.sessions() as session:
        # 同一项目中不应留下没有候选记录的内部任务。
        tasks = session.scalars(
            select(TaskRow).where(TaskRow.project_id == lceda_project.id)
        ).all()
        candidates = session.scalars(
            select(PcbCandidateRow).where(
                PcbCandidateRow.project_id == lceda_project.id
            )
        ).all()
    assert tasks == []
    assert candidates == []


def test_candidate_task_without_durable_candidate_fails_with_stable_error(
    container, lceda_project
):
    """损坏的候选任务必须终止并暴露稳定错误码，不得继续执行。"""
    task = container.tasks.enqueue(
        PCB_GENERATE_CANDIDATE_TASK_KIND,
        {"project_id": lceda_project.id, "candidate_key": "missing-candidate"},
        "candidate-missing-task",
        lceda_project.id,
    )

    assert container.worker.run_once() is True
    failed = container.tasks.get(task.id)
    assert failed.status is TaskStatus.FAILED_TERMINAL
    assert failed.last_error_code == "PCB_CANDIDATE_NOT_FOUND"


def test_cancelling_a_queued_candidate_mirrors_its_terminal_status(
    container, lceda_project
):
    candidate = container.pcb_candidates.create(
        project_id=lceda_project.id,
        base_revision="git:" + "b" * 40,
        base_snapshot_digest="sha256:" + "d" * 64,
        board_snapshot_digest="sha256:" + "c" * 64,
        rulepack_digest=_DIGEST,
        capability_digest="sha256:" + "e" * 64,
        idempotency_key="candidate-queued-cancel",
        require_capability=False,
    )

    container.tasks.cancel(candidate.task_id, "operator stopped", utc_now())

    cancelled = container.pcb_candidates.get(candidate.id)
    assert cancelled.status is PcbCandidateStatus.CANCELLED
    assert cancelled.last_error_code == "TASK_CANCELLED"


def test_candidate_task_with_mismatched_durable_link_fails_terminally(
    container, lceda_project
):
    candidate = container.pcb_candidates.create(
        project_id=lceda_project.id,
        base_revision="git:" + "b" * 40,
        base_snapshot_digest="sha256:" + "d" * 64,
        board_snapshot_digest="sha256:" + "c" * 64,
        rulepack_digest=_DIGEST,
        capability_digest="sha256:" + "e" * 64,
        idempotency_key="candidate-corrupt-link",
        require_capability=False,
    )
    replacement = container.tasks.enqueue(
        PCB_GENERATE_CANDIDATE_TASK_KIND,
        {"project_id": lceda_project.id, "candidate_key": "candidate-corrupt-link"},
        "candidate-corrupt-link-replacement",
        lceda_project.id,
    )
    with container.sessions.begin() as session:
        session.execute(
            PcbCandidateRow.__table__.update()
            .where(PcbCandidateRow.id == candidate.id)
            .values(task_id=replacement.id)
        )

    assert container.worker.run_once() is True
    failed = container.tasks.get(candidate.task_id)
    assert failed.status is TaskStatus.FAILED_TERMINAL
    assert failed.last_error_code == "PCB_CANDIDATE_NOT_FOUND"


def test_ready_for_g3_requires_result_digests_that_match_the_frozen_input(
    container, lceda_project
):
    candidate = container.pcb_candidates.create(
        project_id=lceda_project.id,
        base_revision="git:" + "b" * 40,
        base_snapshot_digest="sha256:" + "d" * 64,
        board_snapshot_digest="sha256:" + "c" * 64,
        rulepack_digest=_DIGEST,
        capability_digest="sha256:" + "e" * 64,
        operations=(_operation(lceda_project.id),),
        idempotency_key="candidate-result-digests",
        require_capability=False,
    )
    lease = container.tasks.claim_next("candidate-worker", utc_now(), 30)
    assert lease is not None
    now = utc_now()
    container.tasks.start(lease.task_id, lease.lease_token, now)
    container.pcb_candidates.mark_executing(
        candidate.id, lease.task_id, lease.lease_token, now
    )

    with pytest.raises(PcbCandidateNotReviewableError):
        container.pcb_candidates.mark_ready_for_g3(
            candidate.id,
            lease.task_id,
            lease.lease_token,
            now + timedelta(seconds=1),
            result={
                "candidate_digest": "sha256:" + "f" * 64,
                "operations_digest": candidate.operations_digest,
                "board_snapshot_digest": "sha256:" + "0" * 64,
            },
        )

    assert container.pcb_candidates.get(candidate.id).status is PcbCandidateStatus.EXECUTING


def test_candidate_view_remains_boardir_only_without_verified_native_evidence(
    container, lceda_project
):
    kwargs = {
        "project_id": lceda_project.id,
        "base_revision": "git:" + "b" * 40,
        "base_snapshot_digest": "sha256:" + "d" * 64,
        "board_snapshot_digest": "sha256:" + "c" * 64,
        "rulepack_digest": _DIGEST,
        "capability_digest": "sha256:" + "e" * 64,
        "idempotency_key": "candidate-output-kind",
        "require_capability": False,
    }
    with pytest.raises(RequestInvalidError):
        container.pcb_candidates.create(
            **kwargs, algorithm_evidence={"output_kind": "native_candidate"}
        )
    candidate = container.pcb_candidates.create(**kwargs)

    assert candidate.output_kind == "boardir_only"


def test_candidate_capability_digest_must_match_the_verified_gate(
    container, lceda_project, monkeypatch
):
    class VerifiedGate:
        def require_operation(self, project_id, eda_kind, operation):
            return "sha256:" + "f" * 64

    monkeypatch.setattr(container.pcb_candidates, "_capability_gate", VerifiedGate())

    with pytest.raises(LcedaProCapabilityError):
        container.pcb_candidates.create(
            project_id=lceda_project.id,
            base_revision="git:" + "b" * 40,
            base_snapshot_digest="sha256:" + "d" * 64,
            board_snapshot_digest="sha256:" + "c" * 64,
            rulepack_digest=_DIGEST,
            capability_digest="sha256:" + "e" * 64,
            idempotency_key="candidate-capability-digest",
        )

    assert container.pcb_candidates.list_for_project(lceda_project.id) == []


def test_candidate_rejects_a_string_instead_of_a_net_id_sequence(
    container, lceda_project
):
    with pytest.raises(RequestInvalidError):
        container.pcb_candidates.create(
            project_id=lceda_project.id,
            base_revision="git:" + "b" * 40,
            base_snapshot_digest="sha256:" + "d" * 64,
            board_snapshot_digest="sha256:" + "c" * 64,
            rulepack_digest=_DIGEST,
            capability_digest="sha256:" + "e" * 64,
            algorithm_evidence={"net_ids": "abcdef"},
            idempotency_key="candidate-invalid-net-id-sequence",
            require_capability=False,
        )

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from pcbflow import worker_service as worker_service_module
from pcbflow.config import Settings
from pcbflow.container import build_container
from pcbflow.domain import TaskLease, TaskStatus, utc_now
from pcbflow.worker_service import TaskExecution, WorkerService, WorkerState


class _FakeTaskCommands:
    """Worker-side double for renewal, claiming, and completion probes."""

    def __init__(self) -> None:
        self.renew_calls: list[str] = []
        self.renew_error: Exception | None = None
        self.claim_error: Exception | None = None
        self.claimed: TaskLease | None = None

    def renew(self, task_id: str, lease_token: str, now, lease_seconds: float):
        self.renew_calls.append(task_id)
        if self.renew_error is not None:
            raise self.renew_error
        return now + timedelta(seconds=lease_seconds)

    def claim_next(self, worker_id: str, now, lease_seconds: float):
        if self.claim_error is not None:
            raise self.claim_error
        return self.claimed

    def get(self, task_id: str):
        return SimpleNamespace(status=TaskStatus.SUCCEEDED, last_error_code=None)


class _FakeWorkerExecutor:
    def __init__(self) -> None:
        self.run_calls: list[str] = []
        self.run_error: Exception | None = None

    def run_claimed(self, lease) -> None:
        self.run_calls.append(lease.task_id)
        if self.run_error is not None:
            raise self.run_error


class _FakeContainer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.tasks = _FakeTaskCommands()
        self.worker = _FakeWorkerExecutor()


def _lease(task_id: str = "task-1") -> TaskLease:
    return TaskLease(
        task_id=task_id,
        kind="probe",
        payload={},
        lease_token=f"lease-{task_id}",
        lease_expires_at=utc_now() + timedelta(seconds=30),
        attempt_number=1,
    )


def test_worker_starts_in_idle_state(tmp_path: Path) -> None:
    settings = Settings.from_env({"PCBFLOW_DATA_DIR": str(tmp_path)})
    container = build_container(settings)
    try:
        worker = WorkerService(container)
        assert worker.state == WorkerState.IDLE
        assert worker.worker_id.startswith("worker_")
        assert worker.active_tasks == {}
        assert worker.completed_count == 0
        assert worker.failed_count == 0
        state = json.loads(
            (settings.data_dir / "worker-state.json").read_text(encoding="utf-8")
        )
        assert state["worker_id"] == worker.worker_id
        assert state["state"] == "IDLE"
        assert state["status"] == "healthy"
    finally:
        container.dispose()


def test_worker_returns_no_work_when_queue_empty(tmp_path: Path) -> None:
    settings = Settings.from_env({"PCBFLOW_DATA_DIR": str(tmp_path)})
    container = build_container(settings)
    try:
        worker = WorkerService(container)

        result = worker.run_one_cycle()

        assert not result.claimed
        assert result.task_id is None
        assert result.duration_seconds >= 0
        assert worker.state == WorkerState.IDLE
    finally:
        container.dispose()


def test_worker_respects_shutdown_signal(tmp_path: Path) -> None:
    settings = Settings.from_env({"PCBFLOW_DATA_DIR": str(tmp_path)})
    container = build_container(settings)
    try:
        worker = WorkerService(container)

        worker.request_shutdown()

        assert worker.shutdown_requested
        assert worker.state == WorkerState.STOPPING
    finally:
        container.dispose()


def test_worker_claims_and_executes_available_task(tmp_path: Path) -> None:
    settings = Settings.from_env({"PCBFLOW_DATA_DIR": str(tmp_path)})
    container = build_container(settings)

    try:
        task = container.tasks.enqueue(
            "worker-service-test-unknown",
            {},
            "test-worker-service-unknown-task",
            None,
        )

        worker = WorkerService(container)
        result = worker.run_one_cycle()

        assert result.claimed
        assert result.task_id == task.id

        processed = container.tasks.get(task.id)
        assert processed.status is TaskStatus.FAILED_TERMINAL
        assert processed.last_error_code == "UNKNOWN_TASK_KIND"
        assert worker.completed_count == 0
        assert worker.failed_count == 1

    finally:
        container.dispose()


def test_worker_treats_cancelled_task_as_resolved(tmp_path: Path) -> None:
    settings = Settings.from_env({"PCBFLOW_DATA_DIR": str(tmp_path)})
    container = build_container(settings)
    worker = WorkerService(container)
    try:
        task = container.tasks.enqueue(
            "worker-service-cancelled",
            {},
            "test-worker-service-cancelled-task",
            None,
        )
        lease = container.tasks.claim_next("worker-a", datetime.now(UTC), 30)
        assert lease is not None and lease.task_id == task.id
        container.tasks.cancel(task.id, "operator requested cancellation", datetime.now(UTC))

        worker._execute_task_in_thread(lease)

        assert container.tasks.get(task.id).status is TaskStatus.CANCELLED
        assert worker.completed_count == 0
        assert worker.failed_count == 0
        assert task.id not in worker.active_tasks
    finally:
        worker.request_shutdown()
        container.dispose()


def test_worker_does_not_claim_when_shutdown_requested(tmp_path: Path) -> None:
    settings = Settings.from_env({"PCBFLOW_DATA_DIR": str(tmp_path)})
    container = build_container(settings)

    try:
        source = tmp_path / "test-project"
        source.mkdir(parents=True, exist_ok=True)
        (source / "test.kicad_sch").write_text("(kicad_sch (version 20230121))")

        project = container.projects.create(
            "Test Project",
            source,
            "test-shutdown-1",
        )

        container.validation.enqueue(project.id, "test-shutdown-1")

        worker = WorkerService(container)
        worker.request_shutdown()

        result = worker.run_one_cycle()

        assert not result.claimed
        assert result.task_id is None

    finally:
        container.dispose()


def test_worker_uses_configured_identifier(tmp_path: Path) -> None:
    settings = Settings.from_env(
        {
            "PCBFLOW_DATA_DIR": str(tmp_path),
            "PCBFLOW_WORKER_ID": "custom-worker",
        }
    )
    container = build_container(settings)
    try:
        worker = WorkerService(container)

        assert worker.worker_id == "custom-worker"
    finally:
        container.dispose()


def test_health_state_write_failure_does_not_break_worker(
    tmp_path: Path, monkeypatch
) -> None:
    settings = Settings.from_env({"PCBFLOW_DATA_DIR": str(tmp_path)})
    container = build_container(settings)

    def explode(path, health):
        raise OSError("state file locked")

    monkeypatch.setattr(worker_service_module, "write_worker_health_state", explode)

    try:
        worker = WorkerService(container)

        worker.request_shutdown()

        assert worker.state == WorkerState.STOPPING
    finally:
        container.dispose()


def test_renewal_loop_survives_renewal_errors(tmp_path: Path, monkeypatch) -> None:
    settings = Settings.from_env(
        {
            "PCBFLOW_DATA_DIR": str(tmp_path),
            "PCBFLOW_WORKER_HEARTBEAT_SECONDS": "0.05",
        }
    )
    container = _FakeContainer(settings)
    renewal_calls: list[int] = []

    def raising_renewal(self):
        renewal_calls.append(1)
        raise RuntimeError("lease renew exploded")

    monkeypatch.setattr(WorkerService, "_renew_active_leases", raising_renewal)

    worker = WorkerService(container)
    try:
        deadline = time.monotonic() + 5.0
        while not renewal_calls and time.monotonic() < deadline:
            time.sleep(0.01)

        assert renewal_calls, "renewal loop must run and survive the error"
        assert not worker._lease_renewal_stop.is_set()
    finally:
        worker.request_shutdown()


def test_renew_active_leases_renews_tasks_past_halfway(tmp_path: Path) -> None:
    settings = Settings.from_env({"PCBFLOW_DATA_DIR": str(tmp_path)})
    container = _FakeContainer(settings)
    worker = WorkerService(container)
    try:
        started = utc_now() - timedelta(seconds=100)
        worker.active_tasks["task-1"] = TaskExecution(
            task_id="task-1",
            lease_token="lease-1",
            started_at=started,
            lease_expires_at=started + timedelta(seconds=180),
        )

        worker._renew_active_leases()

        assert container.tasks.renew_calls == ["task-1"]
        assert worker.active_tasks["task-1"].lease_expires_at > utc_now()
    finally:
        worker.request_shutdown()


def test_renew_active_leases_skips_fresh_tasks_and_logs_failures(
    tmp_path: Path,
) -> None:
    settings = Settings.from_env({"PCBFLOW_DATA_DIR": str(tmp_path)})
    container = _FakeContainer(settings)
    worker = WorkerService(container)
    try:
        worker.active_tasks["fresh"] = TaskExecution(
            task_id="fresh",
            lease_token="lease-fresh",
            started_at=utc_now(),
            lease_expires_at=utc_now() + timedelta(seconds=30),
        )
        worker.active_tasks["broken"] = TaskExecution(
            task_id="broken",
            lease_token="lease-broken",
            started_at=utc_now() - timedelta(seconds=100),
            lease_expires_at=utc_now() + timedelta(seconds=80),
        )
        container.tasks.renew_error = RuntimeError("renew raced cancellation")

        worker._renew_active_leases()

        assert container.tasks.renew_calls == ["broken"]
        assert "fresh" in worker.active_tasks
        assert "broken" in worker.active_tasks
    finally:
        worker.request_shutdown()


def test_execute_task_counts_success(tmp_path: Path) -> None:
    settings = Settings.from_env({"PCBFLOW_DATA_DIR": str(tmp_path)})
    container = _FakeContainer(settings)
    worker = WorkerService(container)
    try:
        worker.active_tasks["task-1"] = TaskExecution(
            task_id="task-1",
            lease_token="lease-1",
            started_at=utc_now(),
            lease_expires_at=utc_now() + timedelta(seconds=30),
        )

        worker._execute_task_in_thread(_lease("task-1"))

        assert container.worker.run_calls == ["task-1"]
        assert worker.completed_count == 1
        assert worker.failed_count == 0
        assert "task-1" not in worker.active_tasks
    finally:
        worker.request_shutdown()


def test_execute_task_counts_executor_exception_as_failure(tmp_path: Path) -> None:
    settings = Settings.from_env({"PCBFLOW_DATA_DIR": str(tmp_path)})
    container = _FakeContainer(settings)
    container.worker.run_error = RuntimeError("runner exploded")
    worker = WorkerService(container)
    try:
        worker._execute_task_in_thread(_lease("task-1"))

        assert worker.failed_count == 1
        assert worker.completed_count == 0
        assert "task-1" not in worker.active_tasks
    finally:
        worker.request_shutdown()


def test_execute_task_reports_unresolved_final_status(tmp_path: Path) -> None:
    settings = Settings.from_env({"PCBFLOW_DATA_DIR": str(tmp_path)})
    container = _FakeContainer(settings)
    worker = WorkerService(container)

    def queued_result(task_id: str):
        return SimpleNamespace(status=TaskStatus.QUEUED, last_error_code=None)

    container.tasks.get = queued_result  # type: ignore[method-assign]
    try:
        worker._execute_task_in_thread(_lease("task-1"))

        assert worker.completed_count == 0
        assert worker.failed_count == 0
    finally:
        worker.request_shutdown()


def test_run_one_cycle_waits_when_no_slot_available(tmp_path: Path) -> None:
    settings = Settings.from_env({"PCBFLOW_DATA_DIR": str(tmp_path)})
    container = build_container(settings)
    worker = WorkerService(container)
    try:
        worker.active_tasks["occupied"] = TaskExecution(
            task_id="occupied",
            lease_token="lease-occupied",
            started_at=utc_now(),
            lease_expires_at=utc_now() + timedelta(seconds=30),
        )

        result = worker.run_one_cycle()

        assert not result.claimed
        assert result.task_id is None
        assert result.duration_seconds >= 0.1
    finally:
        worker.request_shutdown()
        container.dispose()


def test_run_one_cycle_spawns_threads_when_slots_available(tmp_path: Path) -> None:
    settings = Settings.from_env(
        {
            "PCBFLOW_DATA_DIR": str(tmp_path),
            "PCBFLOW_WORKER_SLOTS": "2",
        }
    )
    container = _FakeContainer(settings)
    container.tasks.claimed = _lease("task-parallel")
    worker = WorkerService(container)
    try:
        result = worker.run_one_cycle()

        assert result.claimed
        assert result.task_id == "task-parallel"

        execution = worker.active_tasks.get("task-parallel")
        if execution is not None and execution.thread is not None:
            execution.thread.join(timeout=5.0)

        assert container.worker.run_calls == ["task-parallel"]
        assert worker.completed_count == 1
        assert "task-parallel" not in worker.active_tasks
    finally:
        worker.request_shutdown()


def test_run_one_cycle_propagates_claim_failure(tmp_path: Path) -> None:
    settings = Settings.from_env({"PCBFLOW_DATA_DIR": str(tmp_path)})
    container = _FakeContainer(settings)
    container.tasks.claim_error = RuntimeError("database locked")
    worker = WorkerService(container)
    try:
        with pytest.raises(RuntimeError, match="database locked"):
            worker.run_one_cycle()

        assert worker.failed_count == 0
    finally:
        worker.request_shutdown()


def test_run_one_cycle_counts_failures_after_claim(
    tmp_path: Path, monkeypatch
) -> None:
    settings = Settings.from_env({"PCBFLOW_DATA_DIR": str(tmp_path)})
    container = _FakeContainer(settings)
    container.tasks.claimed = _lease("task-bind")
    worker = WorkerService(container)
    try:

        def explode(task_id: str) -> None:
            raise RuntimeError("log context exploded")

        monkeypatch.setattr(worker_service_module, "bind_log_context", explode)

        with pytest.raises(RuntimeError, match="log context exploded"):
            worker.run_one_cycle()

        assert worker.failed_count == 1
    finally:
        worker.request_shutdown()


def test_shutdown_gracefully_reports_clean_stop(tmp_path: Path) -> None:
    settings = Settings.from_env({"PCBFLOW_DATA_DIR": str(tmp_path)})
    container = build_container(settings)
    worker = WorkerService(container)
    try:
        assert worker.shutdown_gracefully() is True
        assert worker.state == WorkerState.STOPPED
    finally:
        container.dispose()


def test_shutdown_gracefully_times_out_with_active_tasks(
    tmp_path: Path, monkeypatch
) -> None:
    settings = Settings.from_env({"PCBFLOW_DATA_DIR": str(tmp_path)})
    container = build_container(settings)
    worker = WorkerService(container)
    try:
        worker.active_tasks["stuck"] = TaskExecution(
            task_id="stuck",
            lease_token="lease-stuck",
            started_at=utc_now(),
            lease_expires_at=utc_now() + timedelta(seconds=30),
        )

        real_now = datetime.now(UTC)
        now_calls = {"count": 0}

        def fake_now(tz=None):
            now_calls["count"] += 1
            if now_calls["count"] == 1:
                return real_now
            return real_now + timedelta(hours=2)

        monkeypatch.setattr(
            worker_service_module, "datetime", SimpleNamespace(now=fake_now)
        )

        assert worker.shutdown_gracefully() is False
        assert worker.state == WorkerState.STOPPED
    finally:
        worker.request_shutdown()
        container.dispose()

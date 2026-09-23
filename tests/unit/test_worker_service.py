from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from pcbflow.config import Settings
from pcbflow.container import build_container
from pcbflow.domain import TaskStatus
from pcbflow.worker_service import WorkerService, WorkerState


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

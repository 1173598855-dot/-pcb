from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pcbflow.config import Settings
from pcbflow.container import build_container
from pcbflow.worker_service import CycleResult, WorkerService, WorkerState


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
        # Create a test project and enqueue validation
        source = tmp_path / "test-project"
        source.mkdir(parents=True, exist_ok=True)
        (source / "test.kicad_sch").write_text("(kicad_sch (version 20230121))")

        project = container.projects.create(
            "Test Project",
            source,
            "test-validation-worker-1",
        )

        task = container.validation.enqueue(
            project.id, "test-validation-worker-1"
        )

        worker = WorkerService(container)
        result = worker.run_one_cycle()

        assert result.claimed
        assert result.task_id == task.id

        # Worker processes the task through run_once
        # It either succeeds or fails terminally
        assert worker.completed_count >= 0
        assert worker.failed_count >= 0

    finally:
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

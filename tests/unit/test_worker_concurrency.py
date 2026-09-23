"""Tests for worker concurrent task execution and lease renewal."""

import time
from pathlib import Path

from pcbflow.config import Settings
from pcbflow.container import build_container
from pcbflow.worker_service import WorkerService


def test_worker_supports_multiple_slots(tmp_path: Path) -> None:
    """Test that worker can be configured with multiple execution slots."""
    settings = Settings.from_env({
        "PCBFLOW_DATA_DIR": str(tmp_path),
        "PCBFLOW_WORKER_SLOTS": "3",
    })
    container = build_container(settings)
    try:
        worker = WorkerService(container)
        assert worker.container.settings.worker_slots == 3
        assert worker._has_available_slot()

    finally:
        worker.request_shutdown()
        container.dispose()


def test_worker_tracks_available_slots(tmp_path: Path) -> None:
    """Test that worker correctly tracks available execution slots."""
    settings = Settings.from_env({
        "PCBFLOW_DATA_DIR": str(tmp_path),
        "PCBFLOW_WORKER_SLOTS": "2",
    })
    container = build_container(settings)
    try:
        worker = WorkerService(container)

        # Initially all slots available
        assert worker._has_available_slot()
        assert len(worker.active_tasks) == 0

        # Mock an active task
        from datetime import UTC, datetime

        from pcbflow.worker_service import TaskExecution

        execution = TaskExecution(
            task_id="test-task-1",
            lease_token="token-1",
            started_at=datetime.now(UTC),
            lease_expires_at=datetime.now(UTC),
            thread=None,
        )
        worker.active_tasks["test-task-1"] = execution

        # Still one slot available
        assert worker._has_available_slot()
        assert len(worker.active_tasks) == 1

        # Fill second slot
        execution2 = TaskExecution(
            task_id="test-task-2",
            lease_token="token-2",
            started_at=datetime.now(UTC),
            lease_expires_at=datetime.now(UTC),
            thread=None,
        )
        worker.active_tasks["test-task-2"] = execution2

        # No slots available
        assert not worker._has_available_slot()
        assert len(worker.active_tasks) == 2

    finally:
        worker.request_shutdown()
        container.dispose()


def test_worker_with_slots_1_is_default(tmp_path: Path) -> None:
    """Test backward compatibility: default slots=1."""
    settings = Settings.from_env({
        "PCBFLOW_DATA_DIR": str(tmp_path),
    })
    container = build_container(settings)
    try:
        worker = WorkerService(container)
        assert worker.container.settings.worker_slots == 1

    finally:
        worker.request_shutdown()
        container.dispose()


def test_lease_renewal_thread_starts_with_worker(tmp_path: Path) -> None:
    """Test that lease renewal background thread starts with worker."""
    settings = Settings.from_env({"PCBFLOW_DATA_DIR": str(tmp_path)})
    container = build_container(settings)
    try:
        worker = WorkerService(container)

        # Lease renewal thread should be running
        assert worker._lease_renewal_thread is not None
        assert worker._lease_renewal_thread.is_alive()

    finally:
        worker.request_shutdown()
        container.dispose()


def test_lease_renewal_thread_stops_on_shutdown(tmp_path: Path) -> None:
    """Test that lease renewal thread stops gracefully on shutdown."""
    settings = Settings.from_env({"PCBFLOW_DATA_DIR": str(tmp_path)})
    container = build_container(settings)
    try:
        worker = WorkerService(container)
        thread = worker._lease_renewal_thread

        worker.request_shutdown()
        time.sleep(0.5)  # Give thread time to stop

        # Thread should be stopped
        assert not thread.is_alive()

    finally:
        container.dispose()

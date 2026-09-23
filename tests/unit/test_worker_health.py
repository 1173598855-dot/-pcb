"""Tests for worker health check and metrics."""

from datetime import UTC, datetime
from pathlib import Path

from pcbflow.config import Settings
from pcbflow.container import build_container
from pcbflow.worker_health import get_worker_health, get_worker_metrics
from pcbflow.worker_service import WorkerService, WorkerState


def test_get_worker_health_returns_healthy_status(tmp_path: Path) -> None:
    settings = Settings.from_env({"PCBFLOW_DATA_DIR": str(tmp_path)})
    container = build_container(settings)
    try:
        worker = WorkerService(container)
        worker.completed_count = 10
        worker.failed_count = 2

        health = get_worker_health(worker)

        assert health.status == "healthy"
        assert health.worker_id == worker.worker_id
        assert health.state == "IDLE"
        assert health.slots_total == 1
        assert health.slots_active == 0
        assert health.slots_available == 1
        assert health.tasks_completed == 10
        assert health.tasks_failed == 2
        assert health.tasks_active == 0
        assert health.uptime_seconds >= 0
    finally:
        container.dispose()


def test_get_worker_health_returns_degraded_on_high_failure_rate(
    tmp_path: Path,
) -> None:
    settings = Settings.from_env({"PCBFLOW_DATA_DIR": str(tmp_path)})
    container = build_container(settings)
    try:
        worker = WorkerService(container)
        worker.completed_count = 2
        worker.failed_count = 5  # > 50% failure rate

        health = get_worker_health(worker)

        assert health.status == "degraded"
    finally:
        container.dispose()


def test_get_worker_health_returns_unhealthy_when_stopped(tmp_path: Path) -> None:
    settings = Settings.from_env({"PCBFLOW_DATA_DIR": str(tmp_path)})
    container = build_container(settings)
    try:
        worker = WorkerService(container)
        worker.state = WorkerState.STOPPED

        health = get_worker_health(worker)

        assert health.status == "unhealthy"
        assert health.state == "STOPPED"
    finally:
        container.dispose()


def test_get_worker_health_includes_active_tasks(tmp_path: Path) -> None:
    settings = Settings.from_env({"PCBFLOW_DATA_DIR": str(tmp_path)})
    container = build_container(settings)
    try:
        worker = WorkerService(container)
        worker.active_tasks["task-1"] = datetime.now(UTC)
        worker.state = WorkerState.EXECUTING

        health = get_worker_health(worker)

        assert health.slots_active == 1
        assert health.slots_available == 0
        assert health.tasks_active == 1
        assert health.state == "EXECUTING"
    finally:
        container.dispose()


def test_get_worker_metrics_returns_prometheus_format(tmp_path: Path) -> None:
    settings = Settings.from_env({"PCBFLOW_DATA_DIR": str(tmp_path)})
    container = build_container(settings)
    try:
        worker = WorkerService(container)
        worker.completed_count = 42
        worker.failed_count = 3
        worker._backoff_attempts = 2

        metrics = get_worker_metrics(worker)

        assert metrics.worker_uptime_seconds >= 0
        assert metrics.worker_active_slots == 0
        assert metrics.worker_tasks_completed_total == 42
        assert metrics.worker_tasks_failed_total == 3
        assert metrics.worker_backoff_attempts_current == 2
    finally:
        container.dispose()


def test_worker_health_includes_iso8601_timestamp(tmp_path: Path) -> None:
    settings = Settings.from_env({"PCBFLOW_DATA_DIR": str(tmp_path)})
    container = build_container(settings)
    try:
        worker = WorkerService(container)

        health = get_worker_health(worker)

        # Should be valid ISO 8601 format
        parsed = datetime.fromisoformat(health.started_at)
        assert isinstance(parsed, datetime)
        assert parsed.tzinfo is not None  # Should have timezone
    finally:
        container.dispose()

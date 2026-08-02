"""Worker health check and metrics support."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pcbflow.worker_service import WorkerService


@dataclass
class WorkerHealth:
    """Worker health status for monitoring."""

    status: str  # "healthy", "degraded", "unhealthy"
    worker_id: str
    state: str
    started_at: str  # ISO 8601
    uptime_seconds: float
    slots_total: int
    slots_active: int
    slots_available: int
    tasks_completed: int
    tasks_failed: int
    tasks_active: int
    backoff_attempts: int


@dataclass
class WorkerMetrics:
    """Worker metrics for observability."""

    worker_uptime_seconds: float
    worker_active_slots: int
    worker_tasks_completed_total: int
    worker_tasks_failed_total: int
    worker_backoff_attempts_current: int


def get_worker_health(worker: WorkerService) -> WorkerHealth:
    """Generate health check response from worker state."""
    uptime = (datetime.now(UTC) - worker.started_at).total_seconds()

    # Determine health status
    if worker.state.value == "STOPPED":
        status = "unhealthy"
    elif worker.failed_count > worker.completed_count * 0.5 and worker.completed_count > 0:
        status = "degraded"  # More than 50% failure rate
    else:
        status = "healthy"

    return WorkerHealth(
        status=status,
        worker_id=worker.worker_id,
        state=worker.state.value,
        started_at=worker.started_at.isoformat(),
        uptime_seconds=uptime,
        slots_total=worker.container.settings.worker_slots,
        slots_active=len(worker.active_tasks),
        slots_available=worker.container.settings.worker_slots
        - len(worker.active_tasks),
        tasks_completed=worker.completed_count,
        tasks_failed=worker.failed_count,
        tasks_active=len(worker.active_tasks),
        backoff_attempts=worker._backoff_attempts,
    )


def get_worker_metrics(worker: WorkerService) -> WorkerMetrics:
    """Generate Prometheus-compatible metrics from worker state."""
    uptime = (datetime.now(UTC) - worker.started_at).total_seconds()

    return WorkerMetrics(
        worker_uptime_seconds=uptime,
        worker_active_slots=len(worker.active_tasks),
        worker_tasks_completed_total=worker.completed_count,
        worker_tasks_failed_total=worker.failed_count,
        worker_backoff_attempts_current=worker._backoff_attempts,
    )

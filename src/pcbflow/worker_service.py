from __future__ import annotations

import logging
import os
import socket
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import TYPE_CHECKING

from pcbflow.domain import utc_now
from pcbflow.observability import bind_log_context

if TYPE_CHECKING:
    from pcbflow.container import Container

logger = logging.getLogger(__name__)


class WorkerState(Enum):
    IDLE = "IDLE"
    CLAIMING = "CLAIMING"
    EXECUTING = "EXECUTING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"


@dataclass
class CycleResult:
    claimed: bool
    task_id: str | None
    duration_seconds: float


class WorkerService:
    def __init__(self, container: Container) -> None:
        self.container = container
        self.worker_id = self._generate_worker_id()
        self.state = WorkerState.IDLE
        self.started_at = datetime.now(UTC)
        self.shutdown_requested = False
        self.active_tasks: dict[str, datetime] = {}
        self.completed_count = 0
        self.failed_count = 0
        self._backoff_attempts = 0

        logger.info(
            "worker.started worker_id=%s slots=%d",
            self.worker_id,
            container.settings.worker_slots,
        )

    def _generate_worker_id(self) -> str:
        if self.container.settings.worker_id:
            return self.container.settings.worker_id
        hostname = socket.gethostname()
        pid = os.getpid()
        timestamp = int(time.time())
        return f"worker_{hostname}_{pid}_{timestamp}"

    def request_shutdown(self) -> None:
        self.shutdown_requested = True
        self.state = WorkerState.STOPPING
        logger.info("worker.shutdown_requested active_tasks=%d", len(self.active_tasks))

    def shutdown_gracefully(self) -> bool:
        """Wait for active tasks to complete. Returns True if clean, False if forced."""
        self.request_shutdown()

        timeout_at = datetime.now(UTC) + timedelta(
            seconds=self.container.settings.worker_shutdown_timeout_seconds
        )

        while self.active_tasks:
            if datetime.now(UTC) > timeout_at:
                logger.warning(
                    "worker.shutdown_timeout_exceeded active_tasks=%s",
                    list(self.active_tasks.keys()),
                )
                self.state = WorkerState.STOPPED
                return False

            time.sleep(0.5)

        self.state = WorkerState.STOPPED
        return True

    def _backoff_sleep(self) -> None:
        delay = min(
            self.container.settings.worker_poll_seconds * (2**self._backoff_attempts),
            self.container.settings.worker_poll_max_seconds,
        )
        time.sleep(delay)
        self._backoff_attempts += 1

    def _reset_backoff(self) -> None:
        self._backoff_attempts = 0

    def run_one_cycle(self) -> CycleResult:
        if self.shutdown_requested:
            return CycleResult(claimed=False, task_id=None, duration_seconds=0.0)

        start = time.time()
        self.state = WorkerState.CLAIMING

        task = None
        try:
            task = self.container.tasks.claim_next(
                self.worker_id, utc_now(), self.container.settings.task_lease_seconds
            )
            if task is None:
                self._backoff_sleep()
                self.state = WorkerState.IDLE
                return CycleResult(
                    claimed=False, task_id=None, duration_seconds=time.time() - start
                )

            self._reset_backoff()
            self.state = WorkerState.EXECUTING
            bind_log_context(task_id=task.task_id)
            logger.info("task.claimed task_id=%s worker_id=%s", task.task_id, self.worker_id)

            self.active_tasks[task.task_id] = datetime.now(UTC)

            # Execute task using existing task execution logic
            self.container.worker.run_once()

            self.completed_count += 1
            duration = time.time() - start
            logger.info("task.completed task_id=%s duration_seconds=%.2f", task.task_id, duration)

            return CycleResult(claimed=True, task_id=task.task_id, duration_seconds=duration)

        except Exception as error:
            if task:
                self.failed_count += 1
                logger.error(
                    "task.failed task_id=%s error=%s",
                    task.task_id if task else None,
                    str(error),
                )
            raise

        finally:
            if task and task.task_id in self.active_tasks:
                del self.active_tasks[task.task_id]
            self.state = WorkerState.IDLE

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import TYPE_CHECKING

from pcbflow.domain import TaskLease, TaskStatus, utc_now
from pcbflow.observability import bind_log_context
from pcbflow.worker_health import get_worker_health, write_worker_health_state

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


@dataclass
class TaskExecution:
    """Track state for a single executing task."""

    task_id: str
    lease_token: str
    started_at: datetime
    lease_expires_at: datetime
    thread: threading.Thread | None = None


class WorkerService:
    def __init__(self, container: Container) -> None:
        self.container = container
        self.worker_id = self._generate_worker_id()
        self.state = WorkerState.IDLE
        self.started_at = datetime.now(UTC)
        self.shutdown_requested = False
        self.active_tasks: dict[str, TaskExecution] = {}
        self.completed_count = 0
        self.failed_count = 0
        self._backoff_attempts = 0
        self._lease_renewal_thread: threading.Thread | None = None
        self._lease_renewal_stop = threading.Event()

        logger.info(
            "worker.started worker_id=%s slots=%d",
            self.worker_id,
            container.settings.worker_slots,
        )

        # Start lease renewal thread if slots > 0
        if container.settings.worker_slots > 0:
            self._start_lease_renewal_thread()
        self._publish_health_state()

    def _publish_health_state(self) -> None:
        try:
            write_worker_health_state(
                self.container.settings.worker_state_file,
                get_worker_health(self),
            )
        except Exception:
            logger.exception("worker.health_state_write_failed")

    def _generate_worker_id(self) -> str:
        if self.container.settings.worker_id:
            return self.container.settings.worker_id
        hostname = socket.gethostname()
        pid = os.getpid()
        timestamp = int(time.time())
        return f"worker_{hostname}_{pid}_{timestamp}"

    def _start_lease_renewal_thread(self) -> None:
        """Start background thread for renewing task leases."""

        def renewal_loop():
            while not self._lease_renewal_stop.is_set():
                try:
                    self._renew_active_leases()
                except Exception as error:
                    logger.error("lease.renewal_error error=%s", str(error))

                # Sleep for heartbeat interval
                self._lease_renewal_stop.wait(
                    self.container.settings.worker_heartbeat_seconds
                )

        self._lease_renewal_thread = threading.Thread(
            target=renewal_loop, daemon=True, name=f"{self.worker_id}-lease-renewal"
        )
        self._lease_renewal_thread.start()
        logger.info("lease.renewal_thread_started")

    def _renew_active_leases(self) -> None:
        """Renew leases for all active tasks."""
        now = utc_now()
        lease_seconds = self.container.settings.task_lease_seconds

        for task_id, execution in list(self.active_tasks.items()):
            try:
                # Renew if more than halfway to expiration
                halfway_point = execution.started_at + timedelta(
                    seconds=lease_seconds / 2
                )
                if now >= halfway_point:
                    new_expires = self.container.tasks.renew(
                        task_id, execution.lease_token, now, lease_seconds
                    )
                    execution.lease_expires_at = new_expires
                    logger.info(
                        "lease.renewed task_id=%s expires_at=%s",
                        task_id,
                        new_expires.isoformat(),
                    )
            except Exception as error:
                logger.warning(
                    "lease.renewal_failed task_id=%s error=%s", task_id, str(error)
                )

    def _stop_lease_renewal_thread(self) -> None:
        """Stop the lease renewal background thread."""
        if self._lease_renewal_thread:
            self._lease_renewal_stop.set()
            self._lease_renewal_thread.join(timeout=5.0)
            logger.info("lease.renewal_thread_stopped")

    def request_shutdown(self) -> None:
        self.shutdown_requested = True
        self.state = WorkerState.STOPPING
        self._stop_lease_renewal_thread()
        logger.info("worker.shutdown_requested active_tasks=%d", len(self.active_tasks))
        self._publish_health_state()

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
                self._publish_health_state()
                return False

            time.sleep(0.5)

        self.state = WorkerState.STOPPED
        self._publish_health_state()
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

    def _has_available_slot(self) -> bool:
        """Check if there's an available execution slot."""
        return len(self.active_tasks) < self.container.settings.worker_slots

    def _execute_task_in_thread(self, lease: TaskLease) -> None:
        """Execute a task in the current thread (to be called from worker thread)."""
        task_id = lease.task_id
        try:
            self.container.worker.run_claimed(lease)
            processed = self.container.tasks.get(task_id)
            if processed.status is TaskStatus.SUCCEEDED:
                self.completed_count += 1
                logger.info("task.completed task_id=%s", task_id)
            elif processed.status in {
                TaskStatus.RETRY_WAIT,
                TaskStatus.FAILED_TERMINAL,
            }:
                self.failed_count += 1
                logger.warning(
                    "task.failed task_id=%s error_code=%s",
                    task_id,
                    processed.last_error_code,
                )
            elif processed.status is TaskStatus.CANCELLED:
                logger.info("task.cancelled task_id=%s", task_id)
            else:
                logger.warning(
                    "task.execution_unresolved task_id=%s status=%s",
                    task_id,
                    processed.status.value,
                )

        except Exception as error:
            self.failed_count += 1
            logger.error("task.failed task_id=%s error=%s", task_id, str(error))

        finally:
            # Remove from active tasks
            if task_id in self.active_tasks:
                del self.active_tasks[task_id]
            self._publish_health_state()

    def run_one_cycle(self) -> CycleResult:
        """
        Attempt to claim and execute one task.

        With slots=1: synchronous execution (backward compatible).
        With slots>1: spawns threads for concurrent execution.
        """
        if self.shutdown_requested:
            return CycleResult(claimed=False, task_id=None, duration_seconds=0.0)

        # Check if we have capacity
        if not self._has_available_slot():
            time.sleep(0.1)  # Brief wait before retry
            return CycleResult(claimed=False, task_id=None, duration_seconds=0.1)

        start = time.time()
        self.state = WorkerState.CLAIMING

        task = None
        try:
            task = self.container.tasks.claim_next(
                self.worker_id, utc_now(), self.container.settings.task_lease_seconds
            )
            if task is None:
                self._backoff_sleep()
                self.state = WorkerState.IDLE if not self.active_tasks else WorkerState.EXECUTING
                return CycleResult(
                    claimed=False, task_id=None, duration_seconds=time.time() - start
                )

            self._reset_backoff()
            bind_log_context(task_id=task.task_id)
            logger.info("task.claimed task_id=%s worker_id=%s", task.task_id, self.worker_id)

            now = datetime.now(UTC)
            lease_expires = now + timedelta(
                seconds=self.container.settings.task_lease_seconds
            )

            # Register task execution
            execution = TaskExecution(
                task_id=task.task_id,
                lease_token=task.lease_token,
                started_at=now,
                lease_expires_at=lease_expires,
                thread=None,
            )

            # For slots=1, execute synchronously (backward compatible)
            if self.container.settings.worker_slots == 1:
                self.state = WorkerState.EXECUTING
                self.active_tasks[task.task_id] = execution
                self._execute_task_in_thread(task)
                duration = time.time() - start
                self.state = WorkerState.IDLE
                return CycleResult(claimed=True, task_id=task.task_id, duration_seconds=duration)

            # For slots>1, execute in thread
            thread = threading.Thread(
                target=self._execute_task_in_thread,
                args=(task,),
                name=f"task-{task.task_id}",
                daemon=False,
            )
            execution.thread = thread
            self.active_tasks[task.task_id] = execution

            thread.start()
            self.state = WorkerState.EXECUTING

            duration = time.time() - start
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
            if not self.active_tasks:
                self.state = WorkerState.IDLE
            self._publish_health_state()

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from threading import Event, Thread
from typing import Any

from pcbflow.cancellation import TaskCancelledError, task_cancellation_scope
from pcbflow.domain import TaskLease
from pcbflow.observability import bind_log_context, log_event
from pcbflow.repositories import StaleLeaseError, TaskRepository

logger = logging.getLogger(__name__)

TaskHandler = Callable[[TaskLease], dict[str, Any]]


class RetryableTaskError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class TerminalTaskError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class Worker:
    def __init__(
        self,
        repository: TaskRepository,
        worker_id: str,
        handlers: dict[str, TaskHandler],
        clock: Callable[[], datetime],
        lease_seconds: int,
    ) -> None:
        self._repository = repository
        self._worker_id = worker_id
        self._handlers = handlers
        self._clock = clock
        self._lease_seconds = lease_seconds

    def _is_cancelled(self, task_id: str) -> bool:
        is_cancelled = getattr(self._repository, "is_cancelled", None)
        return bool(is_cancelled(task_id)) if is_cancelled is not None else False

    def run_once(self) -> bool:
        lease = self._repository.claim_next(
            self._worker_id, self._clock(), self._lease_seconds
        )
        if lease is None:
            return False
        return self.run_claimed(lease)

    def run_claimed(self, lease: TaskLease) -> bool:
        """Execute a task lease that has already been claimed by a caller."""
        try:
            self._repository.start(lease.task_id, lease.lease_token, self._clock())
        except StaleLeaseError:
            logger.warning(
                "task.lease_lost",
                extra={"task_id": lease.task_id},
            )
            return True
        handler = self._handlers.get(lease.kind)
        if handler is None:
            try:
                self._repository.fail(
                    lease.task_id,
                    lease.lease_token,
                    "UNKNOWN_TASK_KIND",
                    False,
                    self._clock(),
                )
            except StaleLeaseError:
                logger.warning(
                    "task.lease_lost",
                    extra={"task_id": lease.task_id},
                )
            return True
        stop_heartbeat = Event()
        lease_lost = Event()
        heartbeat_interval = max(0.05, min(30.0, self._lease_seconds / 3))

        def heartbeat() -> None:
            while not stop_heartbeat.wait(heartbeat_interval):
                try:
                    self._repository.renew(
                        lease.task_id,
                        lease.lease_token,
                        self._clock(),
                        self._lease_seconds,
                    )
                except StaleLeaseError:
                    lease_lost.set()
                    logger.warning(
                        "task.lease_lost",
                        extra={"task_id": lease.task_id},
                    )
                    return
                except Exception:
                    lease_lost.set()
                    logger.exception(
                        "task.lease_renewal_failed",
                        extra={"task_id": lease.task_id},
                    )
                    # Fail closed: without a successful renewal we cannot
                    # prove that this worker still owns the task lease.
                    return

        heartbeat_thread = Thread(
            target=heartbeat,
            name=f"pcbflow-lease-{lease.task_id}",
            daemon=True,
        )
        heartbeat_thread.start()
        with bind_log_context(
            task_id=lease.task_id,
            project_id=lease.payload.get("project_id"),
            trace_id=lease.payload.get("trace_id"),
        ), task_cancellation_scope(lambda: self._is_cancelled(lease.task_id)):
            try:
                if self._is_cancelled(lease.task_id):
                    raise TaskCancelledError(lease.task_id)
                result = handler(lease)
                if self._is_cancelled(lease.task_id):
                    raise TaskCancelledError(lease.task_id)
            except TaskCancelledError:
                logger.info("task.cancelled", extra={"task_id": lease.task_id})
            except RetryableTaskError as error:
                if not lease_lost.is_set():
                    try:
                        self._repository.fail(
                            lease.task_id,
                            lease.lease_token,
                            error.code,
                            True,
                            self._clock(),
                        )
                    except StaleLeaseError:
                        lease_lost.set()
            except TerminalTaskError as error:
                if not lease_lost.is_set():
                    try:
                        self._repository.fail(
                            lease.task_id,
                            lease.lease_token,
                            error.code,
                            False,
                            self._clock(),
                        )
                    except StaleLeaseError:
                        lease_lost.set()
            except StaleLeaseError:
                lease_lost.set()
                logger.warning(
                    "task.lease_lost",
                    extra={"task_id": lease.task_id},
                )
            except Exception:
                log_event(
                    logger,
                    logging.ERROR,
                    "task.unhandled_error",
                    error_code="UNHANDLED_TASK_ERROR",
                    result="failed",
                )
                if not lease_lost.is_set():
                    try:
                        self._repository.fail(
                            lease.task_id,
                            lease.lease_token,
                            "UNHANDLED_TASK_ERROR",
                            False,
                            self._clock(),
                        )
                    except StaleLeaseError:
                        lease_lost.set()
            else:
                if not lease_lost.is_set():
                    try:
                        self._repository.complete(
                            lease.task_id, lease.lease_token, result, self._clock()
                        )
                    except StaleLeaseError:
                        lease_lost.set()
            finally:
                stop_heartbeat.set()
                heartbeat_thread.join(
                    timeout=max(1.0, min(5.0, heartbeat_interval * 2))
                )
                if heartbeat_thread.is_alive():
                    logger.error(
                        "task.lease_heartbeat_did_not_stop",
                        extra={"task_id": lease.task_id},
                    )
        return True

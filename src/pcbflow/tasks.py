from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from pcbflow.domain import TaskLease
from pcbflow.observability import bind_log_context, log_event
from pcbflow.repositories import TaskRepository

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

    def run_once(self) -> bool:
        lease = self._repository.claim_next(
            self._worker_id, self._clock(), self._lease_seconds
        )
        if lease is None:
            return False
        self._repository.start(lease.task_id, lease.lease_token, self._clock())
        handler = self._handlers.get(lease.kind)
        if handler is None:
            self._repository.fail(
                lease.task_id,
                lease.lease_token,
                "UNKNOWN_TASK_KIND",
                False,
                self._clock(),
            )
            return True
        with bind_log_context(
            task_id=lease.task_id,
            project_id=lease.payload.get("project_id"),
            trace_id=lease.payload.get("trace_id"),
        ):
            try:
                result = handler(lease)
            except RetryableTaskError as error:
                self._repository.fail(
                    lease.task_id, lease.lease_token, error.code, True, self._clock()
                )
            except TerminalTaskError as error:
                self._repository.fail(
                    lease.task_id, lease.lease_token, error.code, False, self._clock()
                )
            except Exception:
                log_event(
                    logger,
                    logging.ERROR,
                    "task.unhandled_error",
                    error_code="UNHANDLED_TASK_ERROR",
                    result="failed",
                )
                self._repository.fail(
                    lease.task_id,
                    lease.lease_token,
                    "UNHANDLED_TASK_ERROR",
                    False,
                    self._clock(),
                )
            else:
                self._repository.complete(
                    lease.task_id, lease.lease_token, result, self._clock()
                )
        return True

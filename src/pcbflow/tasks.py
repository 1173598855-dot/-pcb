from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from pcbflow.domain import TaskLease
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
        self._repository.start(lease.task_id, lease.lease_token)
        handler = self._handlers.get(lease.kind)
        if handler is None:
            self._repository.fail(
                lease.task_id, lease.lease_token, "UNKNOWN_TASK_KIND", False
            )
            return True
        try:
            result = handler(lease)
        except RetryableTaskError as error:
            self._repository.fail(
                lease.task_id, lease.lease_token, error.code, True
            )
        except TerminalTaskError as error:
            self._repository.fail(
                lease.task_id, lease.lease_token, error.code, False
            )
        except Exception:
            logger.exception("Unhandled task error", extra={"task_id": lease.task_id})
            self._repository.fail(
                lease.task_id, lease.lease_token, "UNHANDLED_TASK_ERROR", False
            )
        else:
            self._repository.complete(lease.task_id, lease.lease_token, result)
        return True

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar


class TaskCancelledError(RuntimeError):
    def __init__(self, task_id: str | None = None) -> None:
        message = "task was cancelled" if task_id is None else f"task {task_id} was cancelled"
        super().__init__(message)
        self.task_id = task_id


CancellationChecker = Callable[[], bool]
_cancellation_checker: ContextVar[CancellationChecker | None] = ContextVar(
    "pcbflow_task_cancellation_checker",
    default=None,
)


@contextmanager
def task_cancellation_scope(checker: CancellationChecker) -> Iterator[None]:
    token = _cancellation_checker.set(checker)
    try:
        yield
    finally:
        _cancellation_checker.reset(token)


def current_cancellation_checker() -> CancellationChecker | None:
    return _cancellation_checker.get()

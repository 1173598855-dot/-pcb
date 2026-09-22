"""Repository-level domain errors.

Kept in a separate module so that callers can map persistence failures to
stable error codes without importing the repository implementations. The
names are re-exported from :mod:`pcbflow.repositories` for compatibility.
"""

from __future__ import annotations


class ProjectNotFoundError(LookupError):
    pass


class IdempotencyConflictError(RuntimeError):
    pass


class RevisionConflictError(RuntimeError):
    def __init__(self, expected: str | None, actual: str | None) -> None:
        super().__init__(f"expected {expected}, found {actual}")
        self.expected = expected
        self.actual = actual


class TaskNotFoundError(LookupError):
    pass


class StaleLeaseError(RuntimeError):
    pass


class TaskNotCancellableError(RuntimeError):
    def __init__(self, task_id: str, status: str) -> None:
        super().__init__(f"task {task_id} cannot be cancelled from {status}")
        self.task_id = task_id
        self.status = status


class EvidenceConflictError(RuntimeError):
    pass


__all__ = [
    "EvidenceConflictError",
    "IdempotencyConflictError",
    "ProjectNotFoundError",
    "RevisionConflictError",
    "StaleLeaseError",
    "TaskNotCancellableError",
    "TaskNotFoundError",
]

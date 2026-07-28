from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session, sessionmaker

from pcbflow.domain import TaskStatus
from pcbflow.repositories import (
    IdempotencyConflictError,
    StaleLeaseError,
    TaskRepository,
)
from pcbflow.tasks import RetryableTaskError, TerminalTaskError, Worker

NOW = datetime(2026, 7, 29, 1, 0, tzinfo=UTC)


@pytest.fixture
def task_repository(session_factory: sessionmaker[Session]) -> TaskRepository:
    return TaskRepository(session_factory)


def test_enqueue_is_idempotent(task_repository: TaskRepository) -> None:
    first = task_repository.enqueue(
        "validate", {"probe": True}, "validation:probe:r1", None
    )
    second = task_repository.enqueue(
        "validate", {"probe": True}, "validation:probe:r1", None
    )

    assert first.id == second.id
    assert first.status is TaskStatus.QUEUED


def test_enqueue_rejects_key_reuse_for_different_payload(
    task_repository: TaskRepository,
) -> None:
    task_repository.enqueue("validate", {"revision": 1}, "same-key", None)

    with pytest.raises(IdempotencyConflictError):
        task_repository.enqueue("validate", {"revision": 2}, "same-key", None)


def test_unexpired_lease_cannot_be_claimed_twice(
    task_repository: TaskRepository,
) -> None:
    task_repository.enqueue("validate", {}, "once", None)

    first = task_repository.claim_next("worker-a", NOW, 10)
    second = task_repository.claim_next("worker-b", NOW + timedelta(seconds=9), 10)

    assert first is not None
    assert second is None


def test_expired_lease_is_reclaimed_and_old_token_is_fenced(
    task_repository: TaskRepository,
) -> None:
    task = task_repository.enqueue("validate", {}, "reclaim", None)
    first = task_repository.claim_next("worker-a", NOW, 10)
    second = task_repository.claim_next("worker-b", NOW + timedelta(seconds=11), 10)

    assert first is not None and second is not None
    assert first.task_id == task.id == second.task_id
    assert first.lease_token != second.lease_token
    assert second.attempt_number == 2
    with pytest.raises(StaleLeaseError):
        task_repository.complete(task.id, first.lease_token, {})

    task_repository.start(task.id, second.lease_token)
    task_repository.complete(task.id, second.lease_token, {"evidence": 2})
    completed = task_repository.get(task.id)
    assert completed.status is TaskStatus.SUCCEEDED
    assert completed.result == {"evidence": 2}


def test_worker_completes_registered_handler(
    task_repository: TaskRepository,
) -> None:
    task = task_repository.enqueue("double", {"value": 4}, "double-4", None)
    worker = Worker(
        task_repository,
        "worker-a",
        {
            "double": lambda lease: {
                "value": int(lease.payload["value"]) * 2,
                "task_id": lease.task_id,
            }
        },
        lambda: NOW,
        30,
    )

    assert worker.run_once()
    assert task_repository.get(task.id).status is TaskStatus.SUCCEEDED
    assert task_repository.get(task.id).result == {"value": 8, "task_id": task.id}
    assert not worker.run_once()


def test_worker_marks_retryable_failure(
    task_repository: TaskRepository,
) -> None:
    task = task_repository.enqueue("unstable", {}, "unstable-1", None)

    def fail(_lease):
        raise RetryableTaskError("TOOL_BUSY", "tool is busy")

    worker = Worker(
        task_repository,
        "worker-a",
        {"unstable": fail},
        lambda: NOW,
        30,
    )

    assert worker.run_once()
    failed = task_repository.get(task.id)
    assert failed.status is TaskStatus.RETRY_WAIT
    assert failed.last_error_code == "TOOL_BUSY"


def test_worker_marks_unknown_kind_terminal(
    task_repository: TaskRepository,
) -> None:
    task = task_repository.enqueue("unknown", {}, "unknown-1", None)
    worker = Worker(task_repository, "worker-a", {}, lambda: NOW, 30)

    assert worker.run_once()
    failed = task_repository.get(task.id)
    assert failed.status is TaskStatus.FAILED_TERMINAL
    assert failed.last_error_code == "UNKNOWN_TASK_KIND"


def test_worker_preserves_declared_terminal_error_code(
    task_repository: TaskRepository,
) -> None:
    task = task_repository.enqueue("invalid", {}, "invalid-1", None)

    def fail(_lease):
        raise TerminalTaskError("INVALID_PROJECT", "project cannot be read")

    worker = Worker(
        task_repository,
        "worker-a",
        {"invalid": fail},
        lambda: NOW,
        30,
    )

    assert worker.run_once()
    failed = task_repository.get(task.id)
    assert failed.status is TaskStatus.FAILED_TERMINAL
    assert failed.last_error_code == "INVALID_PROJECT"

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
        task_repository.complete(
            task.id, first.lease_token, {}, NOW + timedelta(seconds=11)
        )

    task_repository.start(task.id, second.lease_token, NOW + timedelta(seconds=11))
    task_repository.complete(
        task.id,
        second.lease_token,
        {"evidence": 2},
        NOW + timedelta(seconds=11),
    )
    completed = task_repository.get(task.id)
    assert completed.status is TaskStatus.SUCCEEDED
    assert completed.result == {"evidence": 2}


def test_expired_token_cannot_transition_or_remain_active(
    session_factory: sessionmaker[Session],
) -> None:
    repository = TaskRepository(session_factory)
    task = repository.enqueue("example", {}, "expired-transition", None)
    claimed_at = datetime(2026, 7, 29, tzinfo=UTC)
    lease = repository.claim_next("worker-a", claimed_at, 1)
    assert lease is not None
    expired_at = claimed_at + timedelta(seconds=1)

    with pytest.raises(StaleLeaseError):
        repository.start(task.id, lease.lease_token, expired_at)

    with pytest.raises(StaleLeaseError):
        repository.complete(task.id, lease.lease_token, {"ok": True}, expired_at)

    with pytest.raises(StaleLeaseError):
        repository.fail(
            task.id,
            lease.lease_token,
            "EXPIRED",
            False,
            expired_at,
        )

    with pytest.raises(StaleLeaseError):
        repository.assert_active(task.id, lease.lease_token, expired_at)


def test_worker_passes_its_clock_to_fenced_transitions(
    task_repository: TaskRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = task_repository.enqueue("double", {"value": 4}, "clocked-double", None)
    calls: list[datetime] = []
    clock = {"now": NOW}
    original_start = task_repository.start
    original_complete = task_repository.complete

    def now() -> datetime:
        current = clock["now"]
        clock["now"] = current + timedelta(seconds=1)
        return current

    def record_start(task_id: str, lease_token: str, now: datetime) -> None:
        calls.append(now)
        original_start(task_id, lease_token, now)

    def record_complete(
        task_id: str, lease_token: str, result: dict[str, object], now: datetime
    ) -> None:
        calls.append(now)
        original_complete(task_id, lease_token, result, now)

    monkeypatch.setattr(task_repository, "start", record_start)
    monkeypatch.setattr(task_repository, "complete", record_complete)
    worker = Worker(
        task_repository,
        "worker-a",
        {"double": lambda lease: {"value": int(lease.payload["value"]) * 2}},
        now,
        30,
    )

    assert worker.run_once()
    assert calls == [NOW + timedelta(seconds=1), NOW + timedelta(seconds=2)]
    assert task_repository.get(task.id).status is TaskStatus.SUCCEEDED


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

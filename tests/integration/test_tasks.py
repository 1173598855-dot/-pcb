import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event, Lock

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from pcbflow.cancellation import TaskCancelledError
from pcbflow.domain import RequestInvalidError, TaskLease, TaskStatus
from pcbflow.repositories import (
    IdempotencyConflictError,
    StaleLeaseError,
    TaskNotCancellableError,
    TaskRepository,
)
from pcbflow.tables import TaskAttemptRow, TaskRow
from pcbflow.tasks import RetryableTaskError, TerminalTaskError, Worker

NOW = datetime(2026, 7, 29, 1, 0, tzinfo=UTC)


class _WorkerRepositoryProbe:
    def __init__(
        self,
        *,
        stale_fail: bool = False,
        stale_complete: bool = False,
    ) -> None:
        self.lease = TaskLease(
            task_id="task-edge",
            kind="edge",
            payload={},
            lease_token="lease-edge",
            lease_expires_at=NOW + timedelta(seconds=30),
            attempt_number=1,
        )
        self.stale_fail = stale_fail
        self.stale_complete = stale_complete
        self.failures: list[tuple[str, bool]] = []
        self.fail_calls: list[tuple[str, str, bool]] = []
        self.complete_calls: list[tuple[str, object]] = []

    def claim_next(self, _worker_id, _now, _lease_seconds):
        lease, self.lease = self.lease, None
        return lease

    @staticmethod
    def start(_task_id, _lease_token, _now) -> None:
        return None

    @staticmethod
    def renew(_task_id, _lease_token, now, lease_seconds):
        return now + timedelta(seconds=lease_seconds)

    def fail(self, task_id, _lease_token, error_code, retryable, _now) -> None:
        self.fail_calls.append((task_id, error_code, retryable))
        if self.stale_fail:
            raise StaleLeaseError(task_id)
        self.failures.append((error_code, retryable))

    def complete(self, task_id, _lease_token, result, _now) -> None:
        self.complete_calls.append((task_id, result))
        if self.stale_complete:
            raise StaleLeaseError(task_id)


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


def test_concurrent_enqueue_replays_the_winning_request(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    barrier = Barrier(2)
    calls_lock = Lock()
    read_calls = 0
    original_add = Session.add

    def synchronize_task_inserts(session, instance, _warn=True):
        nonlocal read_calls
        if isinstance(instance, TaskRow):
            with calls_lock:
                read_calls += 1
                should_wait = read_calls <= 2
            if should_wait:
                barrier.wait(timeout=5)
        return original_add(session, instance, _warn=_warn)

    monkeypatch.setattr(Session, "add", synchronize_task_inserts)
    first_repository = TaskRepository(session_factory)
    second_repository = TaskRepository(session_factory)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            first_repository.enqueue,
            "validate",
            {"probe": True},
            "concurrent-task",
            None,
        )
        second = executor.submit(
            second_repository.enqueue,
            "validate",
            {"probe": True},
            "concurrent-task",
            None,
        )
        first_result = first.result(timeout=10)
        second_result = second.result(timeout=10)

    assert read_calls >= 2
    assert first_result.id == second_result.id


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


def test_renew_extends_a_running_lease_and_preserves_its_fence(
    task_repository: TaskRepository,
) -> None:
    task = task_repository.enqueue("validate", {}, "renew-running", None)
    lease = task_repository.claim_next("worker-a", NOW, 2)
    assert lease is not None
    task_repository.start(task.id, lease.lease_token, NOW)

    renewed_until = task_repository.renew(
        task.id,
        lease.lease_token,
        NOW + timedelta(seconds=1),
        2,
    )

    assert renewed_until == NOW + timedelta(seconds=3)
    task_repository.assert_active(
        task.id,
        lease.lease_token,
        NOW + timedelta(seconds=2),
    )
    with pytest.raises(StaleLeaseError):
        task_repository.renew(
            task.id,
            "expired-token",
            NOW + timedelta(seconds=2),
            2,
        )


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


def test_worker_survives_stale_lease_during_start(
    task_repository: TaskRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = task_repository.enqueue("double", {}, "stale-start", None)
    started = False

    def stale_start(_task_id: str, _lease_token: str, _now: datetime) -> None:
        raise StaleLeaseError(task.id)

    def handler(_lease):
        nonlocal started
        started = True
        return {"ok": True}

    monkeypatch.setattr(task_repository, "start", stale_start)
    worker = Worker(task_repository, "worker-a", {"double": handler}, lambda: NOW, 30)

    assert worker.run_once()
    assert not started
    assert task_repository.get(task.id).status is TaskStatus.LEASED


def test_worker_executes_a_preclaimed_lease(
    task_repository: TaskRepository,
) -> None:
    task = task_repository.enqueue("double", {"value": 4}, "preclaimed-double", None)
    lease = task_repository.claim_next("resident-worker", NOW, 30)
    assert lease is not None
    worker = Worker(
        task_repository,
        "worker-a",
        {"double": lambda claimed: {"value": int(claimed.payload["value"]) * 2}},
        lambda: NOW,
        30,
    )

    assert worker.run_claimed(lease)
    processed = task_repository.get(task.id)
    assert processed.status is TaskStatus.SUCCEEDED
    assert processed.result == {"value": 8}


def test_worker_renews_its_lease_while_a_handler_is_running(
    task_repository: TaskRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = task_repository.enqueue("slow", {}, "renew-worker", None)
    renewed = Event()
    original_renew = task_repository.renew

    def record_renew(
        task_id: str, lease_token: str, now: datetime, lease_seconds: int
    ) -> datetime:
        renewed.set()
        return original_renew(task_id, lease_token, now, lease_seconds)

    monkeypatch.setattr(task_repository, "renew", record_renew)

    def slow_handler(_lease):
        assert renewed.wait(timeout=2)
        return {"ok": True}

    worker = Worker(
        task_repository,
        "worker-a",
        {"slow": slow_handler},
        lambda: NOW,
        1,
    )

    assert worker.run_once()
    assert task_repository.get(task.id).status is TaskStatus.SUCCEEDED


def test_worker_does_not_complete_after_heartbeat_loses_the_lease(
    task_repository: TaskRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = task_repository.enqueue("stale", {}, "stale-heartbeat", None)
    fenced = Event()
    completed = False

    def stale_renew(
        _task_id: str, _lease_token: str, _now: datetime, _lease_seconds: int
    ) -> datetime:
        fenced.set()
        raise StaleLeaseError(task.id)

    original_complete = task_repository.complete

    def record_complete(task_id, lease_token, result, now):
        nonlocal completed
        completed = True
        return original_complete(task_id, lease_token, result, now)

    monkeypatch.setattr(task_repository, "renew", stale_renew)
    monkeypatch.setattr(task_repository, "complete", record_complete)

    def handler(_lease):
        assert fenced.wait(timeout=2)
        return {"ok": True}

    worker = Worker(task_repository, "worker-a", {"stale": handler}, lambda: NOW, 1)

    assert worker.run_once()
    assert not completed


def test_worker_does_not_complete_after_heartbeat_database_failure(
    task_repository: TaskRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_repository.enqueue("db-failure", {}, "db-heartbeat", None)
    failed = Event()
    completed = False

    def failed_renew(
        _task_id: str, _lease_token: str, _now: datetime, _lease_seconds: int
    ) -> datetime:
        failed.set()
        raise RuntimeError("database unavailable")

    original_complete = task_repository.complete

    def record_complete(task_id, lease_token, result, now):
        nonlocal completed
        completed = True
        return original_complete(task_id, lease_token, result, now)

    monkeypatch.setattr(task_repository, "renew", failed_renew)
    monkeypatch.setattr(task_repository, "complete", record_complete)

    def handler(_lease):
        assert failed.wait(timeout=2)
        return {"ok": True}

    worker = Worker(
        task_repository, "worker-a", {"db-failure": handler}, lambda: NOW, 1
    )

    assert worker.run_once()
    assert not completed


def test_worker_does_not_wait_unboundedly_for_a_blocked_heartbeat(
    task_repository: TaskRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_repository.enqueue("blocked", {}, "blocked-heartbeat", None)
    entered = Event()
    release = Event()
    original_renew = task_repository.renew

    def blocked_renew(
        task_id: str, lease_token: str, now: datetime, lease_seconds: int
    ) -> datetime:
        entered.set()
        release.wait(timeout=3)
        return original_renew(task_id, lease_token, now, lease_seconds)

    monkeypatch.setattr(task_repository, "renew", blocked_renew)

    def handler(_lease):
        assert entered.wait(timeout=2)
        return {"ok": True}

    worker = Worker(task_repository, "worker-a", {"blocked": handler}, lambda: NOW, 1)
    started = time.monotonic()
    assert worker.run_once()
    elapsed = time.monotonic() - started
    release.set()

    assert elapsed < 2


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


def test_retry_wait_is_not_claimed_before_its_backoff_expires(
    task_repository: TaskRepository,
) -> None:
    task = task_repository.enqueue("unstable", {}, "deferred-retry", None)
    lease = task_repository.claim_next("worker-a", NOW, 30)

    assert lease is not None
    task_repository.fail(task.id, lease.lease_token, "TOOL_BUSY", True, NOW)
    assert task_repository.claim_next(
        "worker-b", NOW + timedelta(seconds=4), 30
    ) is None

    resumed = task_repository.claim_next(
        "worker-b", NOW + timedelta(seconds=5), 30
    )

    assert resumed is not None
    assert resumed.task_id == task.id


def test_retry_wait_does_not_starve_a_new_queued_task(
    task_repository: TaskRepository,
) -> None:
    first = task_repository.enqueue("unstable", {}, "first-retry", None)
    lease = task_repository.claim_next("worker-a", NOW, 30)

    assert lease is not None
    assert lease.task_id == first.id
    task_repository.fail(first.id, lease.lease_token, "TOOL_BUSY", True, NOW)
    second = task_repository.enqueue("fresh", {}, "fresh-work", None)

    claimed = task_repository.claim_next("worker-b", NOW + timedelta(seconds=1), 30)

    assert claimed is not None
    assert claimed.task_id == second.id


def test_retryable_failure_becomes_terminal_at_the_attempt_limit(
    session_factory: sessionmaker[Session],
) -> None:
    repository = TaskRepository(
        session_factory,
        max_attempts=2,
        retry_base_seconds=5,
        retry_max_delay_seconds=30,
    )
    task = repository.enqueue("unstable", {}, "limited-retry", None)
    first = repository.claim_next("worker-a", NOW, 30)

    assert first is not None
    repository.fail(task.id, first.lease_token, "TOOL_BUSY", True, NOW)
    second = repository.claim_next("worker-a", NOW + timedelta(seconds=5), 30)

    assert second is not None
    repository.fail(
        task.id,
        second.lease_token,
        "TOOL_BUSY",
        True,
        NOW + timedelta(seconds=5),
    )

    assert repository.get(task.id).status is TaskStatus.FAILED_TERMINAL


def test_worker_marks_unknown_kind_terminal(
    task_repository: TaskRepository,
) -> None:
    task = task_repository.enqueue("unknown", {}, "unknown-1", None)
    worker = Worker(task_repository, "worker-a", {}, lambda: NOW, 30)

    assert worker.run_once()
    failed = task_repository.get(task.id)
    assert failed.status is TaskStatus.FAILED_TERMINAL
    assert failed.last_error_code == "UNKNOWN_TASK_KIND"


def test_worker_survives_stale_lease_when_failing_unknown_kind(
    task_repository: TaskRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = task_repository.enqueue("unknown-stale", {}, "unknown-stale", None)

    def stale_fail(
        _task_id: str,
        _lease_token: str,
        _error_code: str,
        _retryable: bool,
        _now: datetime,
    ) -> None:
        raise StaleLeaseError(task.id)

    monkeypatch.setattr(task_repository, "fail", stale_fail)
    worker = Worker(task_repository, "worker-a", {}, lambda: NOW, 30)

    assert worker.run_once()
    assert task_repository.get(task.id).status is TaskStatus.RUNNING


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


@pytest.mark.parametrize(
    "handler_error",
    [
        RetryableTaskError("TOOL_BUSY", "retry later"),
        TerminalTaskError("INVALID_INPUT", "stop retrying"),
    ],
)
def test_worker_tolerates_stale_lease_while_recording_declared_failure(
    handler_error: RuntimeError,
) -> None:
    repository = _WorkerRepositoryProbe(stale_fail=True)

    def fail(_lease):
        raise handler_error

    worker = Worker(repository, "worker-a", {"edge": fail}, lambda: NOW, 30)

    assert worker.run_once()
    assert repository.fail_calls == [
        (
            "task-edge",
            handler_error.code,
            isinstance(handler_error, RetryableTaskError),
        )
    ]
    assert repository.failures == []


@pytest.mark.parametrize(
    (
        "handler_error",
        "stale_fail",
        "stale_complete",
        "expected_failures",
        "expected_fail_call_count",
        "expected_complete_call_count",
    ),
    [
        (StaleLeaseError("task-edge"), False, False, [], 0, 0),
        (
            RuntimeError("unexpected"),
            False,
            False,
            [("UNHANDLED_TASK_ERROR", False)],
            1,
            0,
        ),
        (RuntimeError("unexpected"), True, False, [], 1, 0),
        (None, False, True, [], 0, 1),
    ],
)
def test_worker_fences_unexpected_handler_and_completion_failures(
    handler_error: BaseException | None,
    stale_fail: bool,
    stale_complete: bool,
    expected_failures: list[tuple[str, bool]],
    expected_fail_call_count: int,
    expected_complete_call_count: int,
) -> None:
    repository = _WorkerRepositoryProbe(
        stale_fail=stale_fail,
        stale_complete=stale_complete,
    )

    def handle(_lease):
        if handler_error is not None:
            raise handler_error
        return {"ok": True}

    worker = Worker(repository, "worker-a", {"edge": handle}, lambda: NOW, 30)

    assert worker.run_once()
    assert repository.failures == expected_failures
    assert len(repository.fail_calls) == expected_fail_call_count
    assert len(repository.complete_calls) == expected_complete_call_count
    if expected_complete_call_count:
        assert repository.complete_calls == [("task-edge", {"ok": True})]
    else:
        assert repository.complete_calls == []


def test_cancel_queued_and_retrying_tasks_are_terminal_and_not_claimable(
    task_repository: TaskRepository,
) -> None:
    queued = task_repository.enqueue("queued", {}, "cancel-queued", None)
    cancelled_at = NOW + timedelta(seconds=1)

    cancelled = task_repository.cancel(
        queued.id, "operator requested cancellation", cancelled_at
    )

    assert cancelled.status is TaskStatus.CANCELLED
    assert cancelled.last_error_code == "TASK_CANCELLED"
    assert cancelled.cancelled_at == cancelled_at
    assert cancelled.cancellation_reason == "operator requested cancellation"
    assert cancelled.result is None
    assert task_repository.claim_next("worker-a", NOW + timedelta(days=1), 30) is None

    replayed = task_repository.cancel(
        queued.id, "a later reason must not replace the first", cancelled_at + timedelta(seconds=1)
    )
    assert replayed == cancelled

    retrying = task_repository.enqueue("retry", {}, "cancel-retry", None)
    lease = task_repository.claim_next("worker-a", NOW, 30)
    assert lease is not None and lease.task_id == retrying.id
    task_repository.fail(retrying.id, lease.lease_token, "TOOL_BUSY", True, NOW)

    canceled_retry = task_repository.cancel(
        retrying.id, "operator cancelled retry", NOW + timedelta(seconds=1)
    )
    assert canceled_retry.status is TaskStatus.CANCELLED
    assert task_repository.claim_next("worker-b", NOW + timedelta(days=1), 30) is None


def test_cancel_strips_reason_before_persisting(
    task_repository: TaskRepository,
) -> None:
    task = task_repository.enqueue("queued", {}, "cancel-normalized-reason", None)

    cancelled = task_repository.cancel(
        task.id, "  operator requested cancellation  ", NOW
    )

    assert cancelled.cancellation_reason == "operator requested cancellation"


def test_concurrent_cancels_preserve_one_terminal_snapshot(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = TaskRepository(session_factory)
    task = repository.enqueue("queued", {}, "cancel-concurrent", None)
    barrier = Barrier(2)
    calls_lock = Lock()
    task_reads = 0
    original_get = Session.get

    def synchronize_task_reads(session, entity, ident, *args, **kwargs):
        nonlocal task_reads
        if entity is TaskRow and ident == task.id:
            with calls_lock:
                task_reads += 1
                should_wait = task_reads <= 2
            if should_wait:
                barrier.wait(timeout=5)
        return original_get(session, entity, ident, *args, **kwargs)

    monkeypatch.setattr(Session, "get", synchronize_task_reads)
    first_repository = TaskRepository(session_factory)
    second_repository = TaskRepository(session_factory)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            first_repository.cancel, task.id, "first cancellation", NOW
        )
        second = executor.submit(
            second_repository.cancel, task.id, "second cancellation", NOW
        )
        first_result = first.result(timeout=10)
        second_result = second.result(timeout=10)

    persisted = repository.get(task.id)
    assert task_reads >= 2
    assert first_result == second_result == persisted
    assert persisted.status is TaskStatus.CANCELLED
    assert persisted.cancellation_reason in {"first cancellation", "second cancellation"}


@pytest.mark.parametrize("reason", ["   ", "x" * 1001])
def test_cancel_rejects_invalid_reasons_without_mutating_the_task(
    task_repository: TaskRepository,
    reason: str,
) -> None:
    task = task_repository.enqueue("queued", {}, f"cancel-invalid-{len(reason)}", None)

    with pytest.raises(RequestInvalidError, match="cancellation reason"):
        task_repository.cancel(task.id, reason, NOW)

    assert task_repository.get(task.id).status is TaskStatus.QUEUED


def test_cancelling_a_running_task_closes_its_attempt_and_fences_its_lease(
    task_repository: TaskRepository,
    session_factory: sessionmaker[Session],
) -> None:
    task = task_repository.enqueue("slow", {}, "cancel-running", None)
    lease = task_repository.claim_next("worker-a", NOW, 30)
    assert lease is not None
    task_repository.start(task.id, lease.lease_token, NOW)

    cancelled = task_repository.cancel(
        task.id, "operator requested cancellation", NOW + timedelta(seconds=1)
    )

    assert cancelled.status is TaskStatus.CANCELLED
    assert cancelled.last_error_code == "TASK_CANCELLED"
    assert cancelled.cancellation_reason == "operator requested cancellation"
    assert task_repository.is_cancelled(task.id)
    with pytest.raises(TaskCancelledError):
        task_repository.assert_active(
            task.id, lease.lease_token, NOW + timedelta(seconds=1)
        )
    with pytest.raises(StaleLeaseError):
        task_repository.complete(
            task.id, lease.lease_token, {"published": True}, NOW + timedelta(seconds=1)
        )

    with session_factory() as session:
        attempts = list(
            session.scalars(
                select(TaskAttemptRow).where(TaskAttemptRow.task_id == task.id)
            )
        )
    assert [(attempt.outcome, attempt.error_code) for attempt in attempts] == [
        ("cancelled", "TASK_CANCELLED")
    ]


def test_cancel_rejects_completed_tasks(
    task_repository: TaskRepository,
) -> None:
    task = task_repository.enqueue("done", {}, "cancel-terminal", None)
    lease = task_repository.claim_next("worker-a", NOW, 30)
    assert lease is not None
    task_repository.start(task.id, lease.lease_token, NOW)
    task_repository.complete(task.id, lease.lease_token, {"ok": True}, NOW)

    with pytest.raises(TaskNotCancellableError) as raised:
        task_repository.cancel(task.id, "too late", NOW + timedelta(seconds=1))

    assert raised.value.task_id == task.id
    assert raised.value.status == TaskStatus.SUCCEEDED.value


def test_worker_does_not_publish_a_handler_result_after_cancellation(
    task_repository: TaskRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = task_repository.enqueue("late", {}, "cancel-late-result", None)
    complete_calls = 0
    original_complete = task_repository.complete

    def record_complete(*args, **kwargs) -> None:
        nonlocal complete_calls
        complete_calls += 1
        original_complete(*args, **kwargs)

    monkeypatch.setattr(task_repository, "complete", record_complete)

    def cancel_then_return(_lease: TaskLease) -> dict[str, bool]:
        task_repository.cancel(task.id, "operator requested cancellation", NOW)
        return {"published": True}

    worker = Worker(
        task_repository,
        "worker-a",
        {"late": cancel_then_return},
        lambda: NOW,
        30,
    )

    assert worker.run_once()
    cancelled = task_repository.get(task.id)
    assert cancelled.status is TaskStatus.CANCELLED
    assert cancelled.result is None
    assert complete_calls == 0

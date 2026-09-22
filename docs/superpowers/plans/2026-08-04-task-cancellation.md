# Task Cancellation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Implement the Phase 5B task-cancellation slice: durable terminal
cancellation, cooperative Worker interruption, process-tree cleanup, and
REST/CLI contracts.

**Architecture:** `TaskRepository` owns the only cancellation state
transition. `Worker` installs a context-local read probe while running a
leased handler; `ProcessRunner` consumes that probe without changing the
existing process-port interface. API and CLI are thin adapters over the same
repository operation.

**Tech Stack:** Python 3.12/3.13, SQLAlchemy, Alembic, FastAPI, Typer,
pytest, existing process-tree termination code.

## Global Constraints

- SQLite remains authoritative for task state; do not add an in-memory-only
  cancel path.
- Preserve existing lease fencing and all non-cancelled task transitions.
- A cancellation must never make a task claimable again.
- Reuse `ProcessRunner._terminate_tree()`; process invocation remains
  `shell=False` with argument arrays.
- REST writes require `Idempotency-Key`; strict request models reject unknown
  fields.
- Add tests before each production behavior change and keep the existing
  README and development guide synchronized.

---

### Task 1: Add the Persistent Cancellation State

**Files:**
- Create: `alembic/versions/0006_task_cancellation.py`
- Modify: `src/pcbflow/domain.py`
- Modify: `src/pcbflow/tables.py`
- Modify: `src/pcbflow/repositories.py`
- Modify: `tests/integration/test_tasks.py`
- Modify: `tests/integration/test_migrations.py`

**Interfaces:**
- Produces `TaskStatus.CANCELLED`, `Task.cancelled_at`,
  `Task.cancellation_reason`, and
  `TaskRepository.cancel(task_id: str, reason: str, now: datetime) -> Task`.
- Produces `TaskNotCancellableError` and
  `TaskRepository.is_cancelled(task_id: str) -> bool`.

- [x] **Step 1: Write repository and migration regression tests**

```python
def test_cancelling_a_running_task_closes_its_attempt_and_fences_its_lease(
    task_repository: TaskRepository,
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
    with pytest.raises(TaskCancelledError):
        task_repository.assert_active(task.id, lease.lease_token, NOW + timedelta(seconds=1))
```

Also add queued/retry/repeated/terminal-state cases and update the migration
head/column assertions for `0006_task_cancellation`.

- [x] **Step 2: Run the tests to verify they fail**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration/test_tasks.py tests/integration/test_migrations.py -q
```

Expected: failures because `CANCELLED`, `cancel()`, and the 0006 migration do
not exist.

- [x] **Step 3: Add the minimal schema and repository implementation**

```python
class TaskStatus(StrEnum):
    CANCELLED = "cancelled"

class TaskNotCancellableError(RuntimeError):
    def __init__(self, task_id: str, status: str) -> None:
        super().__init__(f"task {task_id} cannot be cancelled from {status}")
        self.task_id = task_id
        self.status = status

def cancel(self, task_id: str, reason: str, now: datetime) -> Task:
    allowed = {
        TaskStatus.QUEUED.value,
        TaskStatus.RETRY_WAIT.value,
        TaskStatus.LEASED.value,
        TaskStatus.RUNNING.value,
    }
    for _ in range(3):
        with self._sessions.begin() as session:
            row = session.get(TaskRow, task_id)
            if row is None:
                raise TaskNotFoundError(task_id)
            if row.status == TaskStatus.CANCELLED.value:
                return _task(row)
            if row.status not in allowed:
                raise TaskNotCancellableError(task_id, row.status)
            lease_token = row.lease_token
            changed = session.execute(
                update(TaskRow)
                .where(TaskRow.id == task_id, TaskRow.version == row.version)
                .values(
                    status=TaskStatus.CANCELLED.value,
                    result_json=None,
                    lease_owner=None,
                    lease_token=None,
                    lease_expires_at=None,
                    next_attempt_at=None,
                    last_error_code="TASK_CANCELLED",
                    cancelled_at=now,
                    cancellation_reason=reason,
                    updated_at=now,
                    version=row.version + 1,
                )
            )
            if changed.rowcount != 1:
                continue
            if lease_token is not None:
                session.execute(
                    update(TaskAttemptRow)
                    .where(
                        TaskAttemptRow.task_id == task_id,
                        TaskAttemptRow.lease_token == lease_token,
                        TaskAttemptRow.finished_at.is_(None),
                    )
                    .values(
                        finished_at=now,
                        outcome="cancelled",
                        error_code="TASK_CANCELLED",
                    )
                )
        return self.get(task_id)
    raise TaskNotCancellableError(task_id, "concurrent_update")
```

Migration `0006` adds nullable `cancelled_at` and `cancellation_reason` to
the `tasks` table. Do not mutate terminal successful or failed rows.

- [x] **Step 4: Run focused repository and migration tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration/test_tasks.py tests/integration/test_migrations.py -q
```

Expected: all selected tests pass.

### Task 2: Bind Cancellation to Worker and Process Execution

**Files:**
- Create: `src/pcbflow/cancellation.py`
- Modify: `src/pcbflow/tasks.py`
- Modify: `src/pcbflow/process.py`
- Modify: `src/pcbflow/worker_service.py`
- Modify: `tests/unit/test_process.py`
- Modify: `tests/integration/test_tasks.py`

**Interfaces:**
- Produces `TaskCancelledError` and
  `task_cancellation_scope(checker: Callable[[], bool])`.
- `ProcessRunner.run()` raises `TaskCancelledError` only when its current
  scope reports cancellation.
- `Worker.run_claimed()` exits without `complete()` or `fail()` when the
  task became canceled.

- [x] **Step 1: Write process and Worker cancellation tests**

```python
def test_runner_terminates_a_process_when_its_task_is_cancelled(tmp_path: Path) -> None:
    cancelled = Event()
    marker = tmp_path / "child-started"
    result: dict[str, BaseException] = {}

    def run() -> None:
        try:
            with task_cancellation_scope(cancelled.is_set):
                ProcessRunner(1_024).run(
                    [
                        sys.executable,
                        "-c",
                        "from pathlib import Path; import sys, time; "
                        "Path(sys.argv[1]).write_text('started'); time.sleep(30)",
                        str(marker),
                    ],
                    tmp_path,
                    30,
                )
        except BaseException as error:
            result["error"] = error

    thread = Thread(target=run)
    thread.start()
    deadline = time.monotonic() + 2
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert marker.exists()
    cancelled.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert isinstance(result["error"], TaskCancelledError)

def test_worker_preserves_a_cancelled_task_when_a_handler_returns_late(
    task_repository: TaskRepository,
) -> None:
    task = task_repository.enqueue("late", {}, "cancel-late", None)
    entered = Event()
    release = Event()

    def handler(_lease: TaskLease) -> dict[str, bool]:
        entered.set()
        assert release.wait(timeout=2)
        return {"published": True}

    worker = Worker(task_repository, "worker-a", {"late": handler}, lambda: NOW, 30)
    thread = Thread(target=worker.run_once)
    thread.start()
    assert entered.wait(timeout=2)
    task_repository.cancel(task.id, "operator requested cancellation", NOW)
    release.set()
    thread.join(timeout=2)
    assert not thread.is_alive()
    cancelled_task = task_repository.get(task.id)
    assert cancelled_task.status is TaskStatus.CANCELLED
    assert cancelled_task.result is None
```

- [x] **Step 2: Run tests to verify they fail**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_process.py tests/integration/test_tasks.py -q
```

Expected: failures because no cancellation context or cancellation-specific
Worker handling exists.

- [x] **Step 3: Implement context-local polling and Worker handling**

```python
with task_cancellation_scope(lambda: self._repository.is_cancelled(lease.task_id)):
    result = handler(lease)
    if self._repository.is_cancelled(lease.task_id):
        raise TaskCancelledError(lease.task_id)
```

`ProcessRunner` must poll only when a cancellation checker exists, invoke the
existing tree terminator, reap streams, and then raise. `WorkerService` must
recognize `TaskStatus.CANCELLED` as resolved rather than failed.

- [x] **Step 4: Run focused tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_process.py tests/integration/test_tasks.py tests/unit/test_worker_service.py -q
```

Expected: all selected tests pass.

### Task 3: Expose Stable REST and CLI Contracts

**Files:**
- Modify: `src/pcbflow/api.py`
- Modify: `src/pcbflow/cli.py`
- Modify: `tests/e2e/test_api_cli.py`
- Modify: `tests/unit/test_phase2a_edges.py`

**Interfaces:**
- Produces `POST /api/v1/tasks/{task_id}:cancel` with strict
  `{"reason": "operator requested cancellation"}` input and
  `Idempotency-Key`.
- Produces `pcbflow task cancel TASK_ID --reason REASON --idempotency-key KEY`.
- Produces stable `TASK_NOT_CANCELLABLE` errors for non-canceled terminal
  tasks.

- [x] **Step 1: Write public-contract tests**

```python
cancelled = await client.post(
    f"/api/v1/tasks/{task_id}:cancel",
    headers={"Idempotency-Key": "cancel-api-task"},
    json={"reason": "operator requested cancellation"},
)
assert cancelled.status_code == 200
assert cancelled.json()["status"] == "cancelled"

result = runner.invoke(app, [
    "task", "cancel", task_id,
    "--reason", "operator requested cancellation",
    "--idempotency-key", "cancel-cli-task", "--json",
])
assert result.exit_code == 0
```

- [x] **Step 2: Run the contract tests to verify they fail**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/e2e/test_api_cli.py tests/unit/test_phase2a_edges.py -q
```

Expected: failures because the endpoint and CLI subcommand do not exist.

- [x] **Step 3: Add thin API and CLI adapters**

```python
@app.post("/api/v1/tasks/{task_id}:cancel")
def cancel_task(
    task_id: str,
    payload: CancelTaskRequest,
    idempotency_key: Annotated[
        str, Header(alias="Idempotency-Key", min_length=1)
    ],
):
    del idempotency_key
    return jsonable_encoder(services.tasks.cancel(task_id, payload.reason, utc_now()))
```

Add `TaskNotCancellableError` error mapping in both adapters. Keep all state
transition code in `TaskRepository`.

- [x] **Step 4: Run public-contract tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/e2e/test_api_cli.py tests/unit/test_phase2a_edges.py -q
```

Expected: all selected tests pass.

### Task 4: Document and Verify the Slice

**Files:**
- Modify: `README.md`
- Modify: `docs/DEVELOPMENT_GUIDE.md`
- Modify: `docs/superpowers/specs/2026-08-04-task-cancellation-design.md`
- Modify: `docs/superpowers/plans/2026-08-04-task-cancellation.md`

- [x] **Step 1: Document user-facing cancellation behavior**

Add the CLI example, REST endpoint, terminal state, and the guarantee that
cancellation keeps the source project immutable while reusing process-tree
termination.

- [x] **Step 2: Run focused behavior and documentation checks**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_process.py tests/integration/test_tasks.py tests/integration/test_migrations.py tests/e2e/test_api_cli.py -q
rg -n "task cancel|tasks/.+:cancel|cancelled|TASK_CANCELLED" README.md docs/DEVELOPMENT_GUIDE.md
git diff --check
```

Expected: tests pass, documentation contains the implemented contracts, and
the diff check emits no output.

- [x] **Step 3: Run final verification**

Run:

```powershell
.\.venv\Scripts\python.exe -m compileall -q src
.\.venv\Scripts\python.exe -m pytest -q --tb=short -p no:cacheprovider --cov=pcbflow --cov-report=term --cov-fail-under=90 --basetemp .pytest-tmp-phase5b-final
.\.venv\Scripts\python.exe -m alembic -c alembic.ini upgrade head
git diff --check
```

Expected: compile and migration commands exit 0, the full suite has no
failures, coverage is at least 90%, and `git diff --check` emits no output.

## Completion Record

- 2026-08-04: focused cancellation regressions, including concurrent cancel
  snapshot preservation, passed.
- 2026-08-04: `compileall` passed; an isolated SQLite database downgraded to
  base and upgraded to `0006_task_cancellation` successfully.
- 2026-08-04: full suite passed with 534 tests, 2 expected KiCad-tool skips,
  and 90.21% total coverage.

## Plan Self-Review

- Scope matches the documented Phase 5B Worker cancellation and process-tree
  interruption requirements; it does not add a worker pool or distributed
  controller.
- Each durable state, adapter surface, and process interruption behavior has
  a focused failing test before its implementation step.
- The public error, fields, migration revision, and function names are
  consistent with the design specification.

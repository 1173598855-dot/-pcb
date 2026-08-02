# Concurrency and Retry Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve content-addressed artifact integrity under concurrent publication, serialize component artifact registration, and prevent retrying tasks from starving fresh work.

**Architecture:** Artifact publication will use an atomic hard link from a fully fsynced staging file rather than replacing a digest target. Component revision creation will reserve SQLite's write path before its idempotency and artifact reads. Tasks will persist an eligibility timestamp and use bounded exponential backoff with a terminal attempt limit.

**Tech Stack:** Python 3.12/3.13, standard-library `os` and `datetime`, SQLAlchemy 2.x, Alembic, SQLite WAL, pytest.

## Global Constraints

- Preserve artifact digest, media type, descriptor, and conflict contracts.
- A staged object must never overwrite a digest target created concurrently.
- Keep component revision idempotency behavior unchanged for matching and conflicting requests.
- SQLite is the only supported database; take `BEGIN IMMEDIATE` before component catalog reads that lead to writes.
- A task attempt count includes its initial execution. Defaults are 5 total attempts, a 5-second exponential base delay, and a 300-second delay cap.
- Retry eligibility is internal persistence state; do not change REST or CLI task schemas in this increment.
- Every production behavior change starts with a focused failing regression test.

---

## File Structure

| File | Responsibility |
| --- | --- |
| `src/pcbflow/artifacts.py` | Atomic no-clobber staged-object publication. |
| `tests/unit/test_artifacts.py` | Race regression for target creation during publish. |
| `src/pcbflow/component_store.py` | SQLite write reservation before component reads. |
| `tests/integration/test_component_revisions.py` | Reservation-order regression. |
| `src/pcbflow/config.py` | Explicit retry settings and environment parsing. |
| `src/pcbflow/container.py` | Retry policy injection. |
| `src/pcbflow/tables.py` | Retry eligibility column and index declaration. |
| `src/pcbflow/repositories.py` | Claim filtering, backoff, and exhaustion. |
| `alembic/versions/0004_task_retry_schedule.py` | Persistent retry migration. |
| `tests/integration/test_tasks.py` | Retry timing, fairness, and exhaustion behavior. |
| `tests/integration/test_migrations.py` | Migration head and column assertions. |
| `tests/unit/test_config.py` | Retry-policy validation. |

### Task 1: Publish Staged Artifacts Without Overwrite Races

**Files:**
- Modify: `src/pcbflow/artifacts.py`
- Modify: `tests/unit/test_artifacts.py`

**Interfaces:** `StagedArtifact.publish() -> ArtifactDescriptor` remains unchanged. A concurrently created target is accepted only after `_verify_existing(target, staged.digest, staged.size)` succeeds; otherwise it raises existing `ArtifactConflictError`.

- [x] **Step 1: Write the failing target-creation race test**

```python
def test_publish_never_overwrites_a_target_created_during_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ContentAddressedStore(tmp_path / "artifacts")
    staged = store.stage_stream(io.BytesIO(b"trusted bytes"), "application/octet-stream")
    target = store._path(staged.digest)

    def create_conflicting_target(_source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"attacker bytes")
        raise FileExistsError(destination)

    monkeypatch.setattr("pcbflow.artifacts.os.link", create_conflicting_target)
    with pytest.raises(ArtifactConflictError, match=staged.digest):
        staged.publish()
    assert target.read_bytes() == b"attacker bytes"
    staged.discard()
```

- [x] **Step 2: Verify RED**

Run `pytest tests/unit/test_artifacts.py::test_publish_never_overwrites_a_target_created_during_publication -q` with the repository Python command. It must fail because current code uses `os.replace()` and overwrites the simulated target.

- [x] **Step 3: Implement atomic no-clobber publication**

Replace the `target.exists()` / `os.replace()` pair with:

```python
try:
    os.link(staged._temporary_path, target)
except FileExistsError:
    self._verify_existing(target, staged.digest, staged.size)
staged._temporary_path.unlink(missing_ok=True)
staged._temporary_path = None
return ArtifactDescriptor(staged.digest, staged.size, staged.media_type, target)
```

Create `target.parent` first. Keep staging and target below the same artifact root. Make `_verify_existing` convert an `OSError` while opening or reading an existing target into `ArtifactConflictError(digest)`.

- [x] **Step 4: Verify GREEN**

Run `pytest tests/unit/test_artifacts.py -q` with the repository Python command. Existing duplicate staging and corrupt-object tests must remain green.

- [x] **Step 5: Commit**

Run `git add src/pcbflow/artifacts.py tests/unit/test_artifacts.py` followed by `git commit -m "fix: publish staged artifacts without overwrite races"`.

### Task 2: Reserve the Component Catalog Write Path Before Reads

**Files:**
- Modify: `src/pcbflow/component_store.py`
- Modify: `tests/integration/test_component_revisions.py`

**Interfaces:** `ComponentRevisionStore.create()` keeps its public signature and existing replay/conflict behavior. It reserves the SQLite write path before reading `ComponentRevisionRow` or `ArtifactRow`.

- [x] **Step 1: Write the failing ordering test**

Monkeypatch `Session.execute`, record `statement.text == "BEGIN IMMEDIATE"` and catalog `SELECT` SQL, then call `ComponentRevisionStore.create()` with the file's existing valid manifest/artifact helper. Assert the first recorded relevant statement is exactly `BEGIN IMMEDIATE`.

```python
assert statements[0] == "BEGIN IMMEDIATE"
```

- [x] **Step 2: Verify RED**

Run the new test alone. It must fail because the first statement today is the idempotency-key `SELECT`.

- [x] **Step 3: Implement the reservation**

Import `text` and make this the first database action in the existing transaction:

```python
with self._sessions.begin() as session:
    session.execute(text("BEGIN IMMEDIATE"))
    keyed = session.scalar(
        select(ComponentRevisionRow).where(
            ComponentRevisionRow.idempotency_key == idempotency_key
        )
    )
```

Retain the outer `IntegrityError` replay handler and every descriptor-matching rule unchanged.

- [x] **Step 4: Verify GREEN**

Run `pytest tests/integration/test_component_revisions.py tests/unit/test_components.py -q` with the repository Python command.

- [x] **Step 5: Commit**

Run `git add src/pcbflow/component_store.py tests/integration/test_component_revisions.py` followed by `git commit -m "fix: serialize component artifact registration"`.

### Task 3: Schedule Retryable Tasks With Bounded Backoff

**Files:**
- Create: `alembic/versions/0004_task_retry_schedule.py`
- Modify: `src/pcbflow/config.py`
- Modify: `src/pcbflow/container.py`
- Modify: `src/pcbflow/tables.py`
- Modify: `src/pcbflow/repositories.py`
- Modify: `tests/integration/test_tasks.py`
- Modify: `tests/integration/test_migrations.py`
- Modify: `tests/unit/test_config.py`

**Interfaces:** `TaskRepository(..., max_attempts, retry_base_seconds, retry_max_delay_seconds)` receives configured defaults. `fail(..., retryable=True)` persists `next_attempt_at`; `claim_next()` ignores it until due. Exhausted retryable tasks become `FAILED_TERMINAL`.

- [x] **Step 1: Write failing timing, fairness, and exhaustion tests**

Add these deterministic tests using `NOW`:

```python
def test_retry_wait_is_not_claimed_before_its_backoff_expires(task_repository) -> None:
    task = task_repository.enqueue("unstable", {}, "deferred-retry", None)
    lease = task_repository.claim_next("worker-a", NOW, 30)
    assert lease is not None
    task_repository.fail(task.id, lease.lease_token, "TOOL_BUSY", True, NOW)
    assert task_repository.claim_next("worker-b", NOW + timedelta(seconds=4), 30) is None
    resumed = task_repository.claim_next("worker-b", NOW + timedelta(seconds=5), 30)
    assert resumed is not None and resumed.task_id == task.id


def test_retry_wait_does_not_starve_a_new_queued_task(task_repository) -> None:
    first = task_repository.enqueue("unstable", {}, "first-retry", None)
    lease = task_repository.claim_next("worker-a", NOW, 30)
    assert lease is not None and lease.task_id == first.id
    task_repository.fail(first.id, lease.lease_token, "TOOL_BUSY", True, NOW)
    second = task_repository.enqueue("fresh", {}, "fresh-work", None)
    claimed = task_repository.claim_next("worker-b", NOW + timedelta(seconds=1), 30)
    assert claimed is not None and claimed.task_id == second.id


def test_retryable_failure_becomes_terminal_at_the_attempt_limit(session_factory) -> None:
    repository = TaskRepository(session_factory, max_attempts=2, retry_base_seconds=5, retry_max_delay_seconds=30)
    task = repository.enqueue("unstable", {}, "limited-retry", None)
    first = repository.claim_next("worker-a", NOW, 30)
    assert first is not None
    repository.fail(task.id, first.lease_token, "TOOL_BUSY", True, NOW)
    second = repository.claim_next("worker-a", NOW + timedelta(seconds=5), 30)
    assert second is not None
    repository.fail(task.id, second.lease_token, "TOOL_BUSY", True, NOW + timedelta(seconds=5))
    assert repository.get(task.id).status is TaskStatus.FAILED_TERMINAL
```

Add config tests for non-positive policy values and base delay above maximum. Update migration tests for the new head and `next_attempt_at` column.

- [x] **Step 2: Verify RED**

Run `pytest tests/integration/test_tasks.py -q` with the repository Python command. The timing and fairness tests must fail because retry rows are currently immediately claimable; the policy constructor must also fail before it exists.

- [x] **Step 3: Add settings, migration, and model state**

Append these Settings fields after existing API settings to preserve legacy positional construction:

```python
task_retry_max_attempts: int = 5
task_retry_base_seconds: int = 5
task_retry_max_delay_seconds: int = 300
```

Validate all are positive and reject base delay above cap. Parse `PCBFLOW_TASK_RETRY_MAX_ATTEMPTS`, `PCBFLOW_TASK_RETRY_BASE_SECONDS`, and `PCBFLOW_TASK_RETRY_MAX_DELAY_SECONDS`; pass them into `TaskRepository` by keyword.

Add nullable `TaskRow.next_attempt_at` and a `status, next_attempt_at, created_at` claim index. Create migration `0004_task_retry_schedule` from `0003_component_revision_catalog`: add the column, set it to `updated_at` for existing `retry_wait` rows, create `ix_tasks_claim_ready`, and reverse these operations in downgrade.

- [x] **Step 4: Implement eligibility, backoff, and exhaustion**

Use this capped delay helper:

```python
def _retry_delay_seconds(self, attempt_count: int) -> int:
    return min(
        self._retry_max_delay_seconds,
        self._retry_base_seconds * (2 ** max(0, attempt_count - 1)),
    )
```

Make `_claimable(now)` allow queued rows immediately, retry rows only when `next_attempt_at` is non-null and at or before `now`, and retain the existing expired-lease branch. Clear eligibility on claim, completion, and terminal failure. In `fail`, a retryable task with `attempt_count < max_attempts` receives `RETRY_WAIT` plus `now + timedelta(seconds=delay)`; otherwise write `FAILED_TERMINAL` in the same transaction that finishes the attempt.

- [x] **Step 5: Verify GREEN**

Run `pytest tests/integration/test_tasks.py tests/integration/test_migrations.py tests/unit/test_config.py -q` with the repository Python command.

- [x] **Step 6: Commit**

Run `git add alembic/versions/0004_task_retry_schedule.py src/pcbflow/config.py src/pcbflow/container.py src/pcbflow/tables.py src/pcbflow/repositories.py tests/integration/test_tasks.py tests/integration/test_migrations.py tests/unit/test_config.py` followed by `git commit -m "fix: schedule retryable tasks with bounded backoff"`.

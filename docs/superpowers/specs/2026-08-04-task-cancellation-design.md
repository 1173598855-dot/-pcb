# Task Cancellation Design

**Date:** 2026-08-04  
**Status:** Implemented and verified  
**Scope:** Phase 5B task cancellation and external process interruption

## Context

Phase 5A provides durable task leases, resident Workers, fencing, lease
renewal, and process-tree cleanup when a process times out. The next defined
Worker capability in the development guide is Phase 5B: a caller must be able
to cancel work that is queued, waiting to retry, leased, or running. The
implementation must preserve the existing SQLite authority boundary and must
not allow a canceled Worker to publish a result, candidate revision, or other
fenced state after cancellation.

## Goals

- Expose one stable cancellation operation through REST and the CLI.
- Persist cancellation as a durable terminal task state with its reason and
  timestamp.
- Stop an active external process tree promptly after its task is canceled.
- Preserve fencing: any late completion, failure, renewal, or handler-side
  `assert_active` call from the old lease must not mutate the canceled task.
- Record a canceled active attempt as a completed `cancelled` attempt.
- Keep the existing behavior unchanged for task execution without a
  cancellation request.

## Non-goals

- Killing arbitrary Python threads or application code that does not reach a
  cancellation boundary.
- Canceling accepted proposals, changing historical evidence, or deleting
  Git objects. Existing reconciliation remains responsible for recovering
  incomplete candidate projections.
- A distributed Worker control plane, priority scheduling, rate limits, or
  per-kind concurrency quotas.

## Alternatives Considered

### 1. Immediate terminal cancellation (selected)

`queued`, `retry_wait`, `leased`, and `running` tasks transition directly to
`cancelled`. The transaction clears the lease, closes an active attempt, and
prevents future claims. A running Worker observes the durable terminal state,
terminates its external process tree, and cannot publish a late result because
the existing lease fence no longer matches.

This is selected because it remains safe if the Worker crashes after a client
requests cancellation. No later Worker can accidentally reclaim the task.

### 2. `cancel_requested` intermediate state

A Worker would acknowledge the request and eventually transition to
`cancelled`. This would make user-visible progress more detailed, but a
crashed Worker could leave the task indefinitely in an intermediate state or
require new recovery rules and a watchdog. It is deferred until there is a
separate multi-Worker control-plane design.

### 3. In-memory cancellation only

A resident Worker could keep `threading.Event` objects for its active tasks.
This cannot cancel queued work, does not survive a process restart, and gives
other Workers no authoritative view of the request. It is rejected.

## State Model

Add one terminal state:

```text
queued      ──cancel──> cancelled
retry_wait  ──cancel──> cancelled
leased      ──cancel──> cancelled
running     ──cancel──> cancelled
```

`succeeded`, `failed_terminal`, and `cancelled` are terminal. A request to
cancel `succeeded` or `failed_terminal` returns `TASK_NOT_CANCELLABLE`.
Repeating a cancel request for an already canceled task returns the existing
task snapshot without changing its original reason or timestamp.

The cancellation transaction sets:

- `status = "cancelled"`
- `last_error_code = "TASK_CANCELLED"`
- `cancellation_reason` and `cancelled_at`
- `result_json = NULL`, `next_attempt_at = NULL`, and all lease columns to
  `NULL`

For a leased or running task, the open `TaskAttemptRow` is completed in the
same transaction with `outcome = "cancelled"` and
`error_code = "TASK_CANCELLED"`. No new attempt is created and a canceled task
is not claimable.

## Repository and Worker Behavior

`TaskRepository.cancel(task_id, reason, now)` performs a compare-and-swap
update over the cancellable states. It retries a small bounded number of times
when a concurrent Worker changes the row, then returns the terminal result or
the stable cancellation conflict. `TaskRepository.is_cancelled(task_id)` is a
read-only signal for the active Worker. `assert_active()` distinguishes a
canceled task from an expired or replaced lease by raising
`TaskCancelledError`; other fence failures remain `StaleLeaseError`.

`Worker.run_claimed()` binds a task-local cancellation probe around the
handler. It catches `TaskCancelledError` before generic task failures, records
an informational cancellation event, and deliberately does not call
`complete()` or `fail()`. It also checks the probe after a handler returns,
so a late successful return cannot overwrite cancellation.

`WorkerService` treats `cancelled` as a resolved task, not a failed task. It
removes the active execution normally and leaves the durable task state as the
source of truth.

## External Process Interruption

Introduce a small context-local cancellation scope. `Worker.run_claimed()`
installs its repository probe only in the thread that executes that task.
`ProcessRunner.run()` remains API-compatible; when a probe is present it
waits in short bounded intervals. If the probe reports cancellation, it calls
the existing `_terminate_tree()` routine, reaps stdout/stderr readers, and
raises `TaskCancelledError`.

On Windows this reuses `taskkill /PID <pid> /T /F`; on POSIX it reuses the
process-group `SIGKILL` path. A canceled process never returns a partial
`ProcessResult`. No cancellation scope means the existing single `wait()`
behavior remains unchanged.

## REST and CLI Contract

REST:

```http
POST /api/v1/tasks/{task_id}:cancel
Idempotency-Key: cancel-task-123
Content-Type: application/json

{"reason":"operator stopped this validation"}
```

The strict body accepts only `reason`, with a non-empty value capped at 1,000
characters. A successful or repeated cancellation returns HTTP 200 and the
serialized task. Missing tasks retain `TASK_NOT_FOUND` (404); cancellation of
another terminal state returns `TASK_NOT_CANCELLABLE` (409). The endpoint is a
normal mutating API call, so existing remote authentication middleware and the
required `Idempotency-Key` header apply.

CLI:

```powershell
pcbflow task cancel <task-id> --reason "operator stopped this validation" --idempotency-key cancel-task-123 --json
```

The CLI uses the same repository operation and error codes. The idempotency
key is accepted to keep CLI and REST write contracts aligned; cancellation is
convergent and therefore repeating it returns the persisted canceled task.

## Persistence and Compatibility

Alembic migration `0006_task_cancellation` adds nullable
`cancelled_at` and `cancellation_reason` columns to `tasks`. The current
SQLAlchemy table definition and `Task` domain value expose both fields. Old
rows read as `None`; no data backfill or migration of existing terminal
statuses is required.

## Validation Plan

- Repository tests cover queued, retry-wait, leased, running, repeated, and
  non-cancellable terminal states; they also prove leases and attempts are
  fenced after cancellation.
- Process tests prove a cancellation probe stops a sleeping child before its
  normal timeout and raises `TaskCancelledError`.
- Worker integration proves a cancellation signal cannot become an
  `UNHANDLED_TASK_ERROR` or overwrite the durable canceled state.
- API/CLI tests prove the public syntax, strict body validation, stable error
  code, and persisted task representation.
- Migration tests verify the 0006 head and new task columns.
- Final verification runs focused tests, the full test suite, coverage gate,
  compilation, migration upgrade, and `git diff --check`.

## Safety Properties

1. SQLite is authoritative for cancellation; an in-memory Worker state is
   only a delivery mechanism.
2. Cancellation clears the lease before the handler can publish a late
   result, preserving fencing.
3. The source project is never modified by cancellation. Validation
   workspaces remain temporary and proposal work is subject to existing
   reconciliation rules.
4. Process creation remains argument-array based with `shell=False`; only
   the already established tree-termination implementation is reused.

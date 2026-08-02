# Resident Worker Design Specification

**Date:** 2026-08-03  
**Status:** Draft  
**Phase:** 5A

## 1. Overview

This specification defines a resident worker service that continuously claims and executes tasks from the PCBFlow queue. The resident worker replaces the current `--once` mode with a production-grade service that supports graceful shutdown, health checks, concurrent task execution, and operational monitoring.

## 2. Goals

1. **Continuous Operation**: Run indefinitely until explicitly stopped or encountering an unrecoverable error
2. **Graceful Shutdown**: Complete in-progress tasks before exiting on SIGTERM/SIGINT
3. **Health Monitoring**: Expose worker status and task metrics
4. **Backpressure Handling**: Respect queue emptiness with exponential backoff
5. **Concurrency Control**: Support configurable concurrent task slots
6. **Lease Renewal**: Extend task leases during long-running operations
7. **Observability**: Structured logging with worker lifecycle events

## 3. Non-Goals (Future Phases)

- **Task cancellation**: Interrupting running tasks (Phase 5B)
- **Distributed coordination**: Multi-worker leader election (Phase 5C)
- **Dynamic scaling**: Auto-adjusting worker count (Phase 5D)
- **Task prioritization**: Priority queues (Phase 5E)
- **Web UI**: Real-time worker dashboard (Phase 5)

## 4. Architecture

### 4.1 Worker Lifecycle

```
STARTING → IDLE → CLAIMING → EXECUTING → IDLE
                      ↓            ↓
                   STOPPING ← STOPPING
                      ↓            ↓
                    STOPPED ← STOPPED
```

### 4.2 State Transitions

- **STARTING**: Initialize container, verify database, register worker ID
- **IDLE**: Wait for next poll cycle or shutdown signal
- **CLAIMING**: Attempt to claim a task from the queue
- **EXECUTING**: Run task handler, renew lease periodically
- **STOPPING**: Finish current tasks, reject new claims
- **STOPPED**: All tasks complete, resources released

### 4.3 Core Components

```python
class ResidentWorker:
    worker_id: str
    started_at: datetime
    state: WorkerState
    slots: int
    active_tasks: dict[str, TaskExecution]
    shutdown_requested: bool
    
class TaskExecution:
    task_id: str
    claimed_at: datetime
    last_heartbeat: datetime
    lease_token: str
```

## 5. Configuration

### 5.1 Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PCBFLOW_WORKER_SLOTS` | 1 | Max concurrent tasks |
| `PCBFLOW_WORKER_POLL_SECONDS` | 5 | Idle poll interval |
| `PCBFLOW_WORKER_POLL_MAX_SECONDS` | 60 | Max backoff interval |
| `PCBFLOW_WORKER_HEARTBEAT_SECONDS` | 30 | Lease renewal interval |
| `PCBFLOW_WORKER_SHUTDOWN_TIMEOUT_SECONDS` | 300 | Max graceful shutdown wait |
| `PCBFLOW_WORKER_ID` | auto | Worker identifier |

### 5.2 Validation Rules

- `slots` must be >= 1 and <= 10
- `poll_seconds` must be >= 1 and <= `poll_max_seconds`
- `heartbeat_seconds` must be < `task_lease_seconds` / 2
- `shutdown_timeout` must be >= 60 and <= 600

## 6. Behavior Specification

### 6.1 Startup

1. Generate worker ID: `worker_{hostname}_{pid}_{timestamp}`
2. Log: `worker.started` with worker_id, slots, and configuration
3. Register signal handlers for SIGTERM and SIGINT
4. Verify database connectivity
5. Transition to IDLE

### 6.2 Main Loop

```python
while not shutdown_requested:
    if active_tasks.count < slots:
        task = try_claim_task()
        if task:
            spawn_task_executor(task)
            continue
        else:
            backoff()
    else:
        await_slot_available()
    
    renew_active_leases()
    
    if shutdown_requested and no_active_tasks:
        break
```

### 6.3 Task Claiming

1. Query database for claimable tasks (respecting retry eligibility)
2. Attempt to claim with new lease token
3. If successful, spawn executor thread/coroutine
4. If no tasks, increment backoff counter
5. If task already claimed (race), retry immediately

### 6.4 Task Execution

1. Log: `task.started` with task_id, worker_id, lease_token
2. Spawn heartbeat timer (renew lease every N seconds)
3. Invoke task handler from existing registry
4. On completion:
   - Log: `task.completed` with result, duration
   - Update task status in database
   - Release slot
5. On exception:
   - Log: `task.failed` with error, traceback
   - Mark task failed/retryable
   - Release slot
6. Cancel heartbeat timer

### 6.5 Lease Renewal

```python
def renew_lease(task_id: str, lease_token: str) -> bool:
    try:
        result = task_store.renew_lease(task_id, lease_token, extend_seconds=60)
        if result:
            log.debug("lease.renewed", task_id=task_id)
            return True
        else:
            log.warning("lease.expired", task_id=task_id)
            return False
    except Exception as error:
        log.error("lease.renewal_failed", task_id=task_id, error=str(error))
        return False
```

If renewal fails, the executor should:
1. Stop work immediately
2. Not publish results
3. Release slot
4. Log terminal error

### 6.6 Graceful Shutdown

On receiving SIGTERM or SIGINT:

1. Set `shutdown_requested = True`
2. Log: `worker.shutdown_requested`
3. Stop claiming new tasks
4. Wait for active tasks to complete (up to shutdown timeout)
5. If timeout exceeded:
   - Log: `worker.shutdown_timeout_exceeded`
   - Force exit with remaining task IDs logged
6. Log: `worker.stopped` with total_tasks, duration
7. Exit with code 0 (graceful) or 1 (forced)

### 6.7 Backoff Strategy

```python
class ExponentialBackoff:
    base_seconds: int = 5
    max_seconds: int = 60
    attempts: int = 0
    
    def sleep(self):
        delay = min(base_seconds * (2 ** attempts), max_seconds)
        time.sleep(delay)
        attempts += 1
    
    def reset(self):
        attempts = 0
```

Backoff resets when:
- A task is successfully claimed
- Shutdown is requested

### 6.8 Health Check

Expose a simple health endpoint (if serve mode is enabled):

```
GET /worker/health

Response:
{
  "status": "healthy",
  "worker_id": "worker_...",
  "state": "EXECUTING",
  "started_at": "2026-08-03T10:00:00Z",
  "uptime_seconds": 3600,
  "slots": {
    "total": 1,
    "active": 1,
    "available": 0
  },
  "tasks": {
    "completed": 42,
    "failed": 2,
    "active": 1
  }
}
```

## 7. Error Handling

### 7.1 Database Connection Loss

- Log: `worker.database_error`
- Wait 10 seconds
- Retry connection
- After 5 failures, exit with code 2

### 7.2 Task Handler Crash

- Catch all exceptions from handler
- Log full traceback
- Mark task as failed/terminal
- Continue with next task

### 7.3 Lease Renewal Failure

- Stop task execution immediately
- Do NOT mark task complete
- Log: `task.lease_lost`
- Another worker will retry

## 8. Observability

### 8.1 Structured Logs

```
worker.started: worker_id, slots, configuration
worker.shutdown_requested: active_tasks
worker.stopped: total_completed, total_failed, duration_seconds
task.claimed: task_id, worker_id, lease_token
task.started: task_id, kind
task.completed: task_id, duration_ms, result
task.failed: task_id, error_code, message
lease.renewed: task_id
lease.expired: task_id
```

### 8.2 Metrics

- `worker_uptime_seconds`: Worker running duration
- `worker_active_slots`: Currently executing tasks
- `worker_tasks_completed_total`: Total successful tasks
- `worker_tasks_failed_total`: Total failed tasks
- `worker_lease_renewals_total`: Total lease extensions
- `worker_task_duration_seconds`: Histogram of task execution time

## 9. Database Schema

### 9.1 No Schema Changes Required

The existing Task and TaskAttempt tables support resident workers without modification. The `lease_expires_at` and `fencing_token` columns already provide lease management.

### 9.2 Future Enhancement (Phase 5B)

Optional `WorkerRegistration` table for distributed coordination:

```sql
CREATE TABLE worker_registrations (
    worker_id TEXT PRIMARY KEY,
    hostname TEXT NOT NULL,
    pid INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    last_heartbeat_at TEXT NOT NULL,
    slots INTEGER NOT NULL,
    active_tasks INTEGER NOT NULL
);
```

## 10. CLI Interface

### 10.1 New Command

```powershell
pcbflow worker run [--slots N] [--json]
```

Replaces `worker --once` for production use.

### 10.2 Backward Compatibility

Keep `worker --once` for testing and development:

```powershell
pcbflow worker --once --json  # Execute one task and exit
```

## 11. Testing Strategy

### 11.1 Unit Tests

- `test_worker_lifecycle_transitions`
- `test_backoff_respects_limits`
- `test_shutdown_waits_for_tasks`
- `test_lease_renewal_extends_deadline`
- `test_concurrent_slot_management`

### 11.2 Integration Tests

- `test_resident_worker_processes_queue_until_empty`
- `test_worker_survives_database_connection_loss`
- `test_graceful_shutdown_completes_active_tasks`
- `test_expired_lease_stops_task_execution`
- `test_multiple_workers_process_distinct_tasks`

### 11.3 Contract Tests

- `test_worker_run_command_exits_on_empty_queue`
- `test_worker_run_respects_sigterm`
- `test_health_endpoint_returns_current_state`

## 12. Implementation Plan

See `docs/superpowers/plans/2026-08-03-resident-worker-implementation.md` for the task-by-task implementation plan.

## 13. Future Enhancements

### Phase 5B: Task Cancellation
- Cancel signal propagation
- Process tree termination
- Cleanup of partial work

### Phase 5C: Distributed Workers
- Worker registration table
- Heartbeat monitoring
- Dead worker cleanup
- Failover handling

### Phase 5D: Auto-scaling
- Load-based slot adjustment
- Worker pool management
- Container orchestration integration

### Phase 5E: Advanced Queueing
- Task priorities
- Queue partitioning by kind
- Rate limiting per kind
- Fair scheduling

## 14. References

- Phase 2A Task System: `src/pcbflow/tasks.py`
- Current Worker: `src/pcbflow/cli.py` `worker_once()`
- Lease Management: `src/pcbflow/repositories.py` `TaskStore`
- Configuration: `src/pcbflow/config.py`

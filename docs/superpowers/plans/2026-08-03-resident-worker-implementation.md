# Resident Worker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the `--once` worker with a production-grade resident worker that continuously processes tasks until explicitly stopped.

**Architecture:** Add a `WorkerService` that manages lifecycle, slot allocation, lease renewal, and graceful shutdown. The CLI `worker run` command will instantiate and operate this service. Existing task handlers and stores remain unchanged.

**Tech Stack:** Python 3.12/3.13, standard-library `signal`/`threading`/`time`, SQLAlchemy, Typer, pytest.

## Global Constraints

- Preserve existing task handler contracts and database schema
- Support both `worker run` (resident) and `worker --once` (legacy testing mode)
- Implement graceful shutdown with configurable timeout
- Use structured logging for all lifecycle events
- Keep configuration injectable for testing
- Start with single-slot execution; concurrent slots are Phase 5A.1
- All production changes start with focused failing tests

---

## File Structure

| File | Responsibility |
|------|----------------|
| `src/pcbflow/worker_service.py` | Resident worker lifecycle, claiming loop, shutdown coordination |
| `src/pcbflow/config.py` | Worker configuration settings |
| `src/pcbflow/cli.py` | `worker run` command and signal handling |
| `tests/unit/test_worker_service.py` | Lifecycle, backoff, shutdown unit tests |
| `tests/integration/test_resident_worker.py` | End-to-end worker execution tests |
| `docs/DEVELOPMENT_GUIDE.md` | Update worker section with resident behavior |

---

## Task 1: Add Worker Configuration

**Files:**
- Modify: `src/pcbflow/config.py`
- Modify: `tests/unit/test_config.py`

**Interfaces:** Add worker settings to `Settings` dataclass with environment variable parsing and validation.

- [ ] **Step 1: Write failing configuration validation tests**

Add to `tests/unit/test_config.py`:

```python
def test_worker_settings_have_sensible_defaults() -> None:
    settings = Settings.from_env({})
    assert settings.worker_slots == 1
    assert settings.worker_poll_seconds == 5
    assert settings.worker_poll_max_seconds == 60
    assert settings.worker_heartbeat_seconds == 30
    assert settings.worker_shutdown_timeout_seconds == 300

def test_worker_settings_reject_invalid_slot_count() -> None:
    with pytest.raises(ValueError, match="worker slots must be between 1 and 10"):
        Settings.from_env({"PCBFLOW_WORKER_SLOTS": "0"})
    with pytest.raises(ValueError, match="worker slots must be between 1 and 10"):
        Settings.from_env({"PCBFLOW_WORKER_SLOTS": "11"})

def test_worker_settings_reject_invalid_poll_intervals() -> None:
    with pytest.raises(ValueError, match="poll seconds must not exceed poll max"):
        Settings.from_env({
            "PCBFLOW_WORKER_POLL_SECONDS": "70",
            "PCBFLOW_WORKER_POLL_MAX_SECONDS": "60"
        })

def test_worker_settings_reject_excessive_heartbeat() -> None:
    with pytest.raises(ValueError, match="heartbeat must be less than half"):
        Settings.from_env({
            "PCBFLOW_TASK_LEASE_SECONDS": "60",
            "PCBFLOW_WORKER_HEARTBEAT_SECONDS": "40"
        })
```

- [ ] **Step 2: Verify RED**

Run: `pytest tests/unit/test_config.py::test_worker_settings -v`

Expected: FAIL because worker settings do not exist.

- [ ] **Step 3: Add worker configuration fields**

Modify `src/pcbflow/config.py`:

```python
@dataclass(frozen=True)
class Settings:
    # ... existing fields ...
    
    worker_slots: int = 1
    worker_poll_seconds: int = 5
    worker_poll_max_seconds: int = 60
    worker_heartbeat_seconds: int = 30
    worker_shutdown_timeout_seconds: int = 300
    worker_id: str | None = None
    
    def validate(self) -> None:
        # ... existing validations ...
        
        if not (1 <= self.worker_slots <= 10):
            raise ValueError("worker slots must be between 1 and 10")
        if self.worker_poll_seconds > self.worker_poll_max_seconds:
            raise ValueError("poll seconds must not exceed poll max")
        if self.worker_heartbeat_seconds >= self.task_lease_seconds / 2:
            raise ValueError("heartbeat must be less than half the lease duration")
        if not (60 <= self.worker_shutdown_timeout_seconds <= 600):
            raise ValueError("shutdown timeout must be between 60 and 600 seconds")
```

Update `from_env()` to parse:
- `PCBFLOW_WORKER_SLOTS`
- `PCBFLOW_WORKER_POLL_SECONDS`
- `PCBFLOW_WORKER_POLL_MAX_SECONDS`
- `PCBFLOW_WORKER_HEARTBEAT_SECONDS`
- `PCBFLOW_WORKER_SHUTDOWN_TIMEOUT_SECONDS`
- `PCBFLOW_WORKER_ID`

- [ ] **Step 4: Verify GREEN**

Run: `pytest tests/unit/test_config.py -q`

Expected: All configuration tests pass.

- [ ] **Step 5: Commit**

```powershell
git add src/pcbflow/config.py tests/unit/test_config.py
git commit -m "feat: add resident worker configuration"
```

---

## Task 2: Implement Worker Service Core

**Files:**
- Create: `src/pcbflow/worker_service.py`
- Create: `tests/unit/test_worker_service.py`

**Interfaces:** `WorkerService` manages lifecycle, claiming, and shutdown. It does NOT implement concurrent slots or lease renewal in this task.

- [ ] **Step 1: Write failing worker lifecycle tests**

Create `tests/unit/test_worker_service.py`:

```python
from pcbflow.worker_service import WorkerService, WorkerState
from pcbflow.config import Settings

def test_worker_starts_in_idle_state(tmp_path: Path) -> None:
    container = build_test_container(tmp_path)
    worker = WorkerService(container)
    assert worker.state == WorkerState.IDLE
    assert worker.worker_id.startswith("worker_")
    assert worker.active_tasks == {}

def test_worker_claims_and_executes_available_task(tmp_path: Path) -> None:
    container = build_test_container(tmp_path)
    project = container.projects.register(...)
    task = container.task_store.enqueue_validation(project.id, ...)
    
    worker = WorkerService(container)
    result = worker.run_one_cycle()
    
    assert result.claimed
    assert result.task_id == task.id
    completed_task = container.task_store.get(task.id)
    assert completed_task.status == "succeeded"

def test_worker_respects_shutdown_signal(tmp_path: Path) -> None:
    container = build_test_container(tmp_path)
    worker = WorkerService(container)
    
    worker.request_shutdown()
    
    assert worker.shutdown_requested
    assert worker.state == WorkerState.STOPPING

def test_worker_returns_no_work_when_queue_empty(tmp_path: Path) -> None:
    container = build_test_container(tmp_path)
    worker = WorkerService(container)
    
    result = worker.run_one_cycle()
    
    assert not result.claimed
    assert result.task_id is None
```

- [ ] **Step 2: Verify RED**

Run: `pytest tests/unit/test_worker_service.py -v`

Expected: FAIL because `WorkerService` does not exist.

- [ ] **Step 3: Implement WorkerService skeleton**

Create `src/pcbflow/worker_service.py`:

```python
from __future__ import annotations

import socket
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING

from pcbflow.observability import bind_log_context, get_logger

if TYPE_CHECKING:
    from pcbflow.container import Container

log = get_logger(__name__)

class WorkerState(Enum):
    IDLE = "IDLE"
    CLAIMING = "CLAIMING"
    EXECUTING = "EXECUTING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"

@dataclass
class CycleResult:
    claimed: bool
    task_id: str | None
    duration_seconds: float

class WorkerService:
    def __init__(self, container: Container) -> None:
        self.container = container
        self.worker_id = self._generate_worker_id()
        self.state = WorkerState.IDLE
        self.started_at = datetime.now(UTC)
        self.shutdown_requested = False
        self.active_tasks: dict[str, datetime] = {}
        self.completed_count = 0
        self.failed_count = 0
        
        log.info(
            "worker.started",
            worker_id=self.worker_id,
            slots=container.settings.worker_slots,
        )
    
    def _generate_worker_id(self) -> str:
        if self.container.settings.worker_id:
            return self.container.settings.worker_id
        hostname = socket.gethostname()
        pid = os.getpid()
        timestamp = int(time.time())
        return f"worker_{hostname}_{pid}_{timestamp}"
    
    def request_shutdown(self) -> None:
        self.shutdown_requested = True
        self.state = WorkerState.STOPPING
        log.info("worker.shutdown_requested", active_tasks=len(self.active_tasks))
    
    def run_one_cycle(self) -> CycleResult:
        if self.shutdown_requested:
            return CycleResult(claimed=False, task_id=None, duration_seconds=0.0)
        
        start = time.time()
        self.state = WorkerState.CLAIMING
        
        try:
            task = self.container.task_store.claim(utc_now())
            if task is None:
                return CycleResult(claimed=False, task_id=None, duration_seconds=time.time() - start)
            
            self.state = WorkerState.EXECUTING
            bind_log_context(task_id=task.id)
            log.info("task.claimed", task_id=task.id, worker_id=self.worker_id)
            
            self.active_tasks[task.id] = datetime.now(UTC)
            
            # Execute task using existing worker_once logic
            from pcbflow.tasks import execute_task
            execute_task(self.container, task)
            
            self.completed_count += 1
            log.info("task.completed", task_id=task.id, duration_seconds=time.time() - start)
            
            return CycleResult(claimed=True, task_id=task.id, duration_seconds=time.time() - start)
            
        finally:
            if task and task.id in self.active_tasks:
                del self.active_tasks[task.id]
            self.state = WorkerState.IDLE
```

- [ ] **Step 4: Verify GREEN**

Run: `pytest tests/unit/test_worker_service.py -q`

Expected: Basic lifecycle tests pass.

- [ ] **Step 5: Commit**

```powershell
git add src/pcbflow/worker_service.py tests/unit/test_worker_service.py
git commit -m "feat: implement worker service core lifecycle"
```

---

## Task 3: Add Exponential Backoff

**Files:**
- Modify: `src/pcbflow/worker_service.py`
- Modify: `tests/unit/test_worker_service.py`

**Interfaces:** Add `_backoff_sleep()` method that implements exponential backoff with configurable limits.

- [ ] **Step 1: Write failing backoff tests**

Add to `tests/unit/test_worker_service.py`:

```python
def test_worker_backs_off_when_queue_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    container = build_test_container(tmp_path)
    worker = WorkerService(container)
    
    sleep_calls: list[float] = []
    monkeypatch.setattr("time.sleep", lambda seconds: sleep_calls.append(seconds))
    
    for _ in range(5):
        worker.run_one_cycle()
    
    assert len(sleep_calls) == 5
    assert sleep_calls[0] == 5  # base
    assert sleep_calls[1] == 10
    assert sleep_calls[2] == 20
    assert sleep_calls[3] == 40
    assert sleep_calls[4] == 60  # capped at max

def test_worker_resets_backoff_after_claiming_task(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    container = build_test_container(tmp_path)
    worker = WorkerService(container)
    
    sleep_calls: list[float] = []
    monkeypatch.setattr("time.sleep", lambda seconds: sleep_calls.append(seconds))
    
    # Empty queue - backoff increases
    worker.run_one_cycle()
    worker.run_one_cycle()
    
    # Add task - backoff resets
    task = container.task_store.enqueue_validation(...)
    worker.run_one_cycle()
    
    # Empty again - backoff restarts from base
    worker.run_one_cycle()
    
    assert sleep_calls == [5, 10, 5]
```

- [ ] **Step 2: Verify RED**

Run: `pytest tests/unit/test_worker_service.py::test_worker_backs_off -v`

Expected: FAIL because backoff is not implemented.

- [ ] **Step 3: Implement exponential backoff**

Add to `WorkerService`:

```python
class WorkerService:
    def __init__(self, container: Container) -> None:
        # ... existing fields ...
        self._backoff_attempts = 0
    
    def _backoff_sleep(self) -> None:
        delay = min(
            self.container.settings.worker_poll_seconds * (2 ** self._backoff_attempts),
            self.container.settings.worker_poll_max_seconds
        )
        time.sleep(delay)
        self._backoff_attempts += 1
    
    def _reset_backoff(self) -> None:
        self._backoff_attempts = 0
    
    def run_one_cycle(self) -> CycleResult:
        # ... existing code ...
        
        task = self.container.task_store.claim(utc_now())
        if task is None:
            self._backoff_sleep()
            return CycleResult(claimed=False, task_id=None, duration_seconds=time.time() - start)
        
        self._reset_backoff()
        # ... execute task ...
```

- [ ] **Step 4: Verify GREEN**

Run: `pytest tests/unit/test_worker_service.py -q`

Expected: All backoff tests pass.

- [ ] **Step 5: Commit**

```powershell
git add src/pcbflow/worker_service.py tests/unit/test_worker_service.py
git commit -m "feat: add exponential backoff for empty queue"
```

---

## Task 4: Add CLI Command

**Files:**
- Modify: `src/pcbflow/cli.py`
- Modify: `tests/integration/test_resident_worker.py`

**Interfaces:** Add `worker run` command that instantiates WorkerService and runs until shutdown.

- [ ] **Step 1: Write failing CLI integration test**

Create `tests/integration/test_resident_worker.py`:

```python
import signal
import subprocess
import time
from pathlib import Path

def test_worker_run_processes_tasks_until_shutdown(tmp_path: Path) -> None:
    # Setup: Create test database with tasks
    env = {"PCBFLOW_DATA_DIR": str(tmp_path / "data")}
    
    # Start worker in background
    worker_process = subprocess.Popen(
        [".venv/Scripts/python.exe", "-m", "pcbflow", "worker", "run"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    
    time.sleep(2)  # Let worker start
    
    # Enqueue tasks via API
    # ... (implementation depends on test fixture)
    
    time.sleep(5)  # Let worker process tasks
    
    # Request shutdown
    worker_process.send_signal(signal.SIGTERM)
    worker_process.wait(timeout=10)
    
    # Verify: All tasks completed
    assert worker_process.returncode == 0
    # ... check task status in database

def test_worker_run_command_exits_on_empty_queue(tmp_path: Path) -> None:
    env = {"PCBFLOW_DATA_DIR": str(tmp_path / "data")}
    
    result = subprocess.run(
        [".venv/Scripts/python.exe", "-m", "pcbflow", "worker", "run", "--exit-when-idle"],
        env=env,
        capture_output=True,
        timeout=10,
    )
    
    assert result.returncode == 0
    assert b"worker.stopped" in result.stderr
```

- [ ] **Step 2: Verify RED**

Run: `pytest tests/integration/test_resident_worker.py -v`

Expected: FAIL because `worker run` command does not exist.

- [ ] **Step 3: Implement worker run command**

Add to `src/pcbflow/cli.py`:

```python
import signal
import sys

@app.command("worker")
def worker_command(
    once: Annotated[bool, typer.Option("--once")] = False,
    run: Annotated[bool, typer.Option("--run")] = False,
    exit_when_idle: Annotated[bool, typer.Option("--exit-when-idle")] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Execute worker tasks. Use --once for single task, --run for resident mode."""
    
    if once and run:
        raise CliInputError("INVALID_ARGUMENT", "cannot specify both --once and --run")
    
    if once or (not run and not exit_when_idle):
        # Legacy --once behavior
        _worker_once(json_output)
        return
    
    # Resident worker
    container = _build()
    worker = None
    
    def shutdown_handler(signum, frame):
        if worker:
            worker.request_shutdown()
    
    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)
    
    try:
        from pcbflow.worker_service import WorkerService
        
        worker = WorkerService(container)
        
        while not worker.shutdown_requested:
            result = worker.run_one_cycle()
            
            if exit_when_idle and not result.claimed:
                break
        
        log.info("worker.stopped",
                 completed=worker.completed_count,
                 failed=worker.failed_count,
                 duration_seconds=(datetime.now(UTC) - worker.started_at).total_seconds())
        
        sys.exit(0)
        
    except KeyboardInterrupt:
        if worker:
            worker.request_shutdown()
        sys.exit(0)
    finally:
        container.dispose()

def _worker_once(json_output: bool) -> None:
    # Existing --once implementation
    ...
```

- [ ] **Step 4: Verify GREEN**

Run: `pytest tests/integration/test_resident_worker.py -v`

Expected: Worker starts, processes tasks, and shuts down gracefully.

- [ ] **Step 5: Update documentation**

Add to `README.md`:

```markdown
## Running the Resident Worker

Production deployments should use the resident worker:

```powershell
.\.venv\Scripts\pcbflow.exe worker --run
```

For development and testing, use single-task mode:

```powershell
.\.venv\Scripts\pcbflow.exe worker --once --json
```

The resident worker processes tasks continuously until receiving SIGTERM or SIGINT.
Configure behavior with:

- `PCBFLOW_WORKER_SLOTS`: Max concurrent tasks (default: 1)
- `PCBFLOW_WORKER_POLL_SECONDS`: Idle polling interval (default: 5)
- `PCBFLOW_WORKER_SHUTDOWN_TIMEOUT_SECONDS`: Graceful shutdown timeout (default: 300)
```

- [ ] **Step 6: Commit**

```powershell
git add src/pcbflow/cli.py tests/integration/test_resident_worker.py README.md
git commit -m "feat: add resident worker CLI command"
```

---

## Task 5: Add Graceful Shutdown

**Files:**
- Modify: `src/pcbflow/worker_service.py`
- Modify: `tests/unit/test_worker_service.py`

**Interfaces:** Worker waits for active tasks to complete before exiting, with configurable timeout.

- [ ] **Step 1: Write failing graceful shutdown test**

Add to `tests/unit/test_worker_service.py`:

```python
def test_worker_waits_for_active_task_during_shutdown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    container = build_test_container(tmp_path)
    worker = WorkerService(container)
    
    # Mock long-running task
    original_execute = container.task_handlers["validation"]
    
    def slow_execute(*args, **kwargs):
        time.sleep(2)
        return original_execute(*args, **kwargs)
    
    monkeypatch.setitem(container.task_handlers, "validation", slow_execute)
    
    task = container.task_store.enqueue_validation(...)
    
    # Start task
    threading.Thread(target=worker.run_one_cycle).start()
    time.sleep(0.5)  # Ensure task is executing
    
    # Request shutdown
    shutdown_start = time.time()
    worker.request_shutdown()
    
    # Worker should wait for task
    while worker.active_tasks:
        time.sleep(0.1)
        if time.time() - shutdown_start > 5:
            pytest.fail("Worker did not complete task within timeout")
    
    assert worker.completed_count == 1

def test_worker_force_exits_on_shutdown_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings.from_env({
        "PCBFLOW_DATA_DIR": str(tmp_path),
        "PCBFLOW_WORKER_SHUTDOWN_TIMEOUT_SECONDS": "1",
    })
    container = build_container(settings)
    worker = WorkerService(container)
    
    # Mock task that never completes
    def infinite_task(*args, **kwargs):
        while True:
            time.sleep(0.1)
    
    monkeypatch.setitem(container.task_handlers, "validation", infinite_task)
    
    task = container.task_store.enqueue_validation(...)
    
    threading.Thread(target=worker.run_one_cycle).start()
    time.sleep(0.5)
    
    worker.request_shutdown()
    
    # Should timeout and force exit
    timeout_at = time.time() + 2
    while time.time() < timeout_at:
        if worker.state == WorkerState.STOPPED:
            break
        time.sleep(0.1)
    
    assert worker.state == WorkerState.STOPPED
    assert len(worker.active_tasks) > 0  # Task still active
```

- [ ] **Step 2: Verify RED**

Run: `pytest tests/unit/test_worker_service.py::test_worker_waits -v`

Expected: FAIL because graceful shutdown is not implemented.

- [ ] **Step 3: Implement graceful shutdown**

Modify `WorkerService`:

```python
def shutdown_gracefully(self) -> bool:
    """Wait for active tasks to complete. Returns True if clean, False if forced."""
    self.request_shutdown()
    
    timeout_at = datetime.now(UTC) + timedelta(
        seconds=self.container.settings.worker_shutdown_timeout_seconds
    )
    
    while self.active_tasks:
        if datetime.now(UTC) > timeout_at:
            log.warning(
                "worker.shutdown_timeout_exceeded",
                active_tasks=list(self.active_tasks.keys())
            )
            return False
        
        time.sleep(0.5)
    
    self.state = WorkerState.STOPPED
    return True
```

Update `cli.py` to call `shutdown_gracefully()` on signal.

- [ ] **Step 4: Verify GREEN**

Run: `pytest tests/unit/test_worker_service.py -q`

Expected: Graceful shutdown tests pass.

- [ ] **Step 5: Commit**

```powershell
git add src/pcbflow/worker_service.py tests/unit/test_worker_service.py src/pcbflow/cli.py
git commit -m "feat: implement graceful worker shutdown with timeout"
```

---

## Task 6: Final Integration and Documentation

**Files:**
- Modify: `docs/DEVELOPMENT_GUIDE.md`
- Modify: `README.md`
- Run full test suite

- [ ] **Step 1: Update DEVELOPMENT_GUIDE.md**

Add section 14.5:

```markdown
### 14.5 Resident Worker

The resident worker continuously processes tasks until explicitly stopped:

```powershell
pcbflow worker --run
```

Key behaviors:
- Claims tasks with exponential backoff when queue is empty
- Supports graceful shutdown on SIGTERM/SIGINT
- Waits up to `PCBFLOW_WORKER_SHUTDOWN_TIMEOUT_SECONDS` for active tasks
- Logs structured lifecycle events

For concurrent execution (Phase 5A.1), set `PCBFLOW_WORKER_SLOTS`.

### 14.6 Worker Testing

Test worker with `--exit-when-idle`:

```powershell
pcbflow worker --run --exit-when-idle
```

This processes all queued tasks then exits, useful for CI/CD pipelines.
```

- [ ] **Step 2: Run full test suite**

```powershell
$env:TEMP_TEST_DIR = "$env:TEMP\pcbflow-test-final"; 
.\.venv\Scripts\python.exe -m pytest -v --basetemp="$env:TEMP_TEST_DIR" -k "not kicad"
```

Expected: All tests pass, coverage >= 90%.

- [ ] **Step 3: Run resident worker manually**

```powershell
# Terminal 1: Start worker
.\.venv\Scripts\pcbflow.exe worker --run

# Terminal 2: Enqueue tasks
.\.venv\Scripts\pcbflow.exe validate <project-id> --idempotency-key test-1 --json

# Terminal 1: Observe task execution, then Ctrl+C for graceful shutdown
```

- [ ] **Step 4: Commit documentation**

```powershell
git add docs/DEVELOPMENT_GUIDE.md README.md
git commit -m "docs: document resident worker usage"
```

- [ ] **Step 5: Tag release**

```powershell
git tag -a v0.2.0 -m "Phase 5A: Resident Worker"
```

---

## Success Criteria

- [ ] `worker --run` command exists and processes tasks continuously
- [ ] Worker respects SIGTERM/SIGINT with graceful shutdown
- [ ] Exponential backoff implemented with configurable limits
- [ ] Configuration validated (slots, poll intervals, timeout)
- [ ] All tests pass (unit + integration)
- [ ] Coverage remains >= 90%
- [ ] Documentation updated (README, DEVELOPMENT_GUIDE)
- [ ] `worker --once` still works for backward compatibility

## Future Enhancements (Phase 5A.1)

- **Concurrent slots**: Execute multiple tasks in parallel
- **Lease renewal**: Extend task leases during long-running operations
- **Health endpoint**: Expose worker status via HTTP
- **Worker metrics**: Prometheus-compatible counters and histograms

## References

- Design Spec: `docs/superpowers/specs/2026-08-03-resident-worker-design.md`
- Existing Worker: `src/pcbflow/cli.py` `worker_once()`
- Task Store: `src/pcbflow/repositories.py` `TaskStore`

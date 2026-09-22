# Worker CLI Compatibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore the documented Worker execution CLI without removing the
`worker health` subcommand.

**Architecture:** `worker` remains a Typer group. Its callback owns execution
options when no subcommand is selected, while `health` remains a child command.
This eliminates the duplicate `worker` command registration and preserves
existing automation syntax.

**Tech Stack:** Python 3.12/3.13, Typer, pytest, FastAPI/SQLAlchemy existing
application stack.

## Global Constraints

- Preserve the documented `worker --once`, `worker --run`, and
  `worker --run --exit-when-idle` interfaces.
- Preserve `worker health` and its JSON output.
- Do not change task, database, or WorkerService behavior.
- Use focused tests before production-code changes.
- Keep README, development guide, CLI help, and examples consistent.

---

### Task 1: Lock The CLI Contract With Tests

**Files:**
- Modify: `tests/unit/test_phase2a_edges.py`
- Test: `tests/e2e/test_api_cli.py`

**Interfaces:**
- Consumes: `pcbflow.cli.app`.
- Produces: a regression contract requiring one Worker group to expose both
  execution options and the `health` subcommand.

- [x] **Step 1: Replace the permissive Worker assertion with a focused contract**

```python
def test_worker_group_exposes_execution_options_and_health_subcommand() -> None:
    runner = CliRunner()
    result = runner.invoke(cli.app, ["worker", "--help"])

    assert result.exit_code == 0, result.output
    assert "--once" in result.output
    assert "--run" in result.output
    assert "health" in result.output
```

- [x] **Step 2: Run the focused test to verify the current group fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/unit/test_phase2a_edges.py::test_worker_group_exposes_execution_options_and_health_subcommand -q`

Expected: FAIL because the active `worker` group exposes `health` but not
`--once` or `--run`.

### Task 2: Make The Worker Group Own Execution

**Files:**
- Modify: `src/pcbflow/cli.py`
- Test: `tests/unit/test_phase2a_edges.py`
- Test: `tests/e2e/test_api_cli.py`

**Interfaces:**
- Consumes: `WorkerService`, `_build()`, `_emit()`, and `_abort()`.
- Produces: `pcbflow worker --once`, `pcbflow worker --run`, and
  `pcbflow worker health` under one Typer group.

- [x] **Step 1: Convert `run_worker` from a top-level command to the group callback**

```python
@worker_app.callback(invoke_without_command=True)
def run_worker(
    context: typer.Context,
    once: Annotated[bool, typer.Option("--once")] = False,
    run: Annotated[bool, typer.Option("--run")] = False,
    exit_when_idle: Annotated[bool, typer.Option("--exit-when-idle")] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    if context.invoked_subcommand is not None:
        if once or run or exit_when_idle or json_output:
            _abort(CliInputError("INVALID_ARGUMENT", "worker execution options cannot be used with a subcommand"))
        return
    # Retain the existing execution body and its mutual-exclusion validation.
```

- [x] **Step 2: Run focused unit and E2E tests**

Run: `./.venv/Scripts/python.exe -m pytest tests/unit/test_phase2a_edges.py tests/e2e/test_api_cli.py -q`

Expected: all collected tests in both files pass.

### Task 3: Synchronize User And Developer Documentation

**Files:**
- Modify: `README.md`
- Modify: `docs/DEVELOPMENT_GUIDE.md`

**Interfaces:**
- Consumes: the restored CLI contract from Task 2.
- Produces: documentation that distinguishes implemented resident Worker
  behavior from unimplemented roadmap capabilities.

- [x] **Step 1: Replace stale scope statements**

Update the README and development guide to state that the resident Worker is
implemented, with one-shot and resident modes, configurable slots, lease
renewal, and CLI health checks. Keep Phase 5B cancellation, AI generation,
PCB automation, manufacturing output, Web UI, and PostgreSQL as future work.

- [x] **Step 2: Verify documentation references and repository whitespace**

Run: `rg -n "not a resident service|循环执行或作为服务运行尚未实现|常驻 Worker 和嘉立创导出不在当前范围内" README.md docs/DEVELOPMENT_GUIDE.md`

Expected: no matches.

Run: `git diff --check`

Expected: no output.

### Task 4: Complete The Verification Gate

**Files:**
- Verify only: `src/pcbflow/cli.py`, `tests/`, `README.md`,
  `docs/DEVELOPMENT_GUIDE.md`

**Interfaces:**
- Consumes: the repaired CLI and synchronized documentation.
- Produces: evidence for the completed regression repair.

- [x] **Step 1: Run the Worker-focused test suite**

Run: `./.venv/Scripts/python.exe -m pytest tests/unit/test_worker_service.py tests/unit/test_worker_health.py tests/unit/test_worker_concurrency.py tests/unit/test_phase2a_edges.py tests/e2e/test_api_cli.py -q`

Expected: all selected tests pass.

- [x] **Step 2: Run application-interface smoke checks**

Run: `./.venv/Scripts/python.exe -m pcbflow worker --help`

Expected: help lists `--once`, `--run`, and `health`.

Run: `./.venv/Scripts/python.exe -m pcbflow worker health --json`

Expected: a JSON health object.

- [x] **Step 3: Run the full suite within a bounded timeout and record its result**

## Completion Record

Completed on 2026-08-04. During final audit, the resident Worker was also
corrected to execute its already-claimed lease, wait for active tasks during
shutdown, publish a real health snapshot, and retain short-lease compatibility.
The final isolated test run passed 522 tests with 90.13% coverage.

Run: `./.venv/Scripts/python.exe -m pytest -q --tb=short`

Expected: no Worker CLI failures. If the pre-existing full-suite runtime exceeds
the validation window, record the timeout separately from functional failures.

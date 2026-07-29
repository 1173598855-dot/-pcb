# Phase 2A Controlled Design Change Kernel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the first safe write-capable PCBFlow slice: adopt a KiCad project into managed Git, freeze structured requirements at G1, execute deterministic schematic commands in isolation, produce semantic evidence, and accept or reject a candidate without mutating the import source.

**Architecture:** Keep SQLite authoritative for workflow state, managed Git authoritative for accepted design history, and the content-addressed store authoritative for immutable evidence. All writes flow through strict Pydantic command models, a fenced task handler, an isolated Git worktree, a loss-preserving KiCad S-expression CST, mandatory ERC, and digest-bound review decisions.

**Tech Stack:** Python 3.12/3.13, FastAPI, Pydantic v2 strict mode, SQLAlchemy 2, Alembic, Typer, PyYAML 6, Git CLI, KiCad 9 CLI, pytest, Hypothesis.

## Global Constraints

- Python requirement remains `>=3.12,<3.14`.
- Phase 2A supports SQLite only.
- Supported write contract is KiCad 9.x only; unsupported capability returns a stable error.
- Add `PyYAML>=6.0.2,<7`; do not add GitPython or a third-party KiCad object writer.
- Never use regex or unstructured string replacement to modify `.kicad_sch`.
- Never write to the registered external `source_path`.
- Database `Project.current_revision` is the workflow authority; Git refs are reconciled projections.
- Every create/submit/decision path is idempotent and compares the full canonical input.
- Every task transition that writes a result must require a current, unexpired fencing token.
- Mandatory proposal validations are Schema, preconditions, path limits, post-write parse, semantic Diff, and KiCad ERC.
- Pydantic request and command models use strict mode and `extra="forbid"`.
- Structured logs carry only approved IDs, revisions, digests, stable codes, and trace context; metrics use low-cardinality labels and never include paths, tokens, or project content.
- AI, Web UI, PCB editing, manufacturing output, supplier access, and resident Worker behavior remain out of scope.
- Full-suite coverage must remain at least 90% at Phase 2A completion.

## Planned File Map

Create:

```text
src/pcbflow/canonical.py
src/pcbflow/observability.py
src/pcbflow/design_tables.py
src/pcbflow/workspaces.py
src/pcbflow/revisions.py
src/pcbflow/revision_store.py
src/pcbflow/requirements.py
src/pcbflow/requirement_store.py
src/pcbflow/commands.py
src/pcbflow/approvals.py
src/pcbflow/proposals.py
src/pcbflow/proposal_store.py
src/pcbflow/schematic/__init__.py
src/pcbflow/schematic/cst.py
src/pcbflow/schematic/semantic.py
src/pcbflow/schematic/adapter.py
src/pcbflow/schematic/modules.py
src/pcbflow/schematic/diff.py
alembic/versions/0002_controlled_design_changes.py
tests/unit/test_canonical.py
tests/unit/test_observability.py
tests/unit/test_requirements.py
tests/unit/test_commands.py
tests/unit/test_revisions.py
tests/unit/test_schematic_cst.py
tests/unit/test_schematic_semantic.py
tests/unit/test_schematic_diff.py
tests/unit/test_schematic_modules.py
tests/unit/test_schematic_adapter.py
tests/unit/test_schematic_goldens.py
tests/integration/test_design_migration.py
tests/integration/test_managed_projects.py
tests/integration/test_requirement_workflow.py
tests/integration/test_command_batches.py
tests/integration/test_proposals.py
tests/integration/test_proposal_decisions.py
tests/integration/test_reconciliation.py
tests/e2e/test_controlled_design_change.py
tests/contract/test_kicad_schematic_write.py
tests/fixtures/requirements/reference-controller.yaml
tests/fixtures/kicad/controlled-design/board.kicad_sch
tests/fixtures/kicad/golden/blank/blank.kicad_sch
tests/fixtures/kicad/golden/simple/simple.kicad_sch
tests/fixtures/kicad/golden/hierarchical/root.kicad_sch
tests/fixtures/kicad/golden/hierarchical/child/child.kicad_sch
tests/fixtures/kicad/golden/unicode/unicode.kicad_sch
tests/fixtures/kicad/golden/multi-unit/multi-unit.kicad_sch
tests/fixtures/kicad/golden/custom-properties/custom-properties.kicad_sch
tests/fixtures/kicad/golden/erc-error/erc-error.kicad_sch
tests/fixtures/modules/status-led-v1/module.yaml
tests/fixtures/modules/status-led-v1/status-led.kicad_sch
```

Modify:

```text
pyproject.toml
alembic/env.py
src/pcbflow/config.py
src/pcbflow/process.py
src/pcbflow/domain.py
src/pcbflow/tables.py
src/pcbflow/repositories.py
src/pcbflow/tasks.py
src/pcbflow/artifacts.py
src/pcbflow/validation.py
src/pcbflow/kicad.py
src/pcbflow/container.py
src/pcbflow/api.py
src/pcbflow/cli.py
tests/conftest.py
tests/unit/test_config.py
tests/unit/test_process.py
tests/unit/test_artifacts.py
tests/integration/test_migrations.py
tests/integration/test_projects.py
tests/integration/test_tasks.py
tests/integration/test_validation.py
tests/e2e/test_api_cli.py
tests/contract/test_kicad_cli.py
README.md
docs/DEVELOPMENT_GUIDE.md
```

---

### Task 1: Canonical serialization, settings, and controlled process environment

**Files:**
- Create: `src/pcbflow/canonical.py`
- Create: `tests/unit/test_canonical.py`
- Modify: `pyproject.toml`
- Modify: `src/pcbflow/config.py:10-62`
- Modify: `src/pcbflow/process.py:28-112`
- Modify: `tests/unit/test_config.py`
- Modify: `tests/unit/test_process.py`

**Interfaces:**
- Produces: `canonical_json_bytes(value: object) -> bytes`
- Produces: `canonical_digest(value: object) -> str`
- Produces: `Settings.projects_dir: Path`
- Produces: `Settings.workspaces_dir: Path`
- Produces: `Settings.module_catalog_dir: Path | None`
- Produces: `ProcessPort.run(argv: Sequence[str], cwd: Path, timeout_seconds: float, *, env: Mapping[str, str] | None = None) -> ProcessResult`
- Extends: `ProcessResult.stdout_bytes: bytes` and `stderr_bytes: bytes` while retaining decoded `stdout` and `stderr`.

- [ ] **Step 1: Write failing canonicalization and settings tests**

```python
# tests/unit/test_canonical.py
from __future__ import annotations

import pytest

from pcbflow.canonical import canonical_digest, canonical_json_bytes


def test_canonical_json_is_utf8_sorted_compact_and_stable() -> None:
    left = {"中文": "值", "b": [2, 1], "a": {"z": True, "x": None}}
    right = {"a": {"x": None, "z": True}, "b": [2, 1], "中文": "值"}

    assert canonical_json_bytes(left) == canonical_json_bytes(right)
    assert canonical_json_bytes(left) == (
        b'{"a":{"x":null,"z":true},"b":[2,1],'
        b'"\xe4\xb8\xad\xe6\x96\x87":"\xe5\x80\xbc"}'
    )
    assert canonical_digest(left) == canonical_digest(right)
    assert canonical_digest(left).startswith("sha256:")


def test_canonical_json_rejects_non_finite_numbers() -> None:
    with pytest.raises(ValueError):
        canonical_json_bytes({"value": float("nan")})
```

Append to `tests/unit/test_config.py`:

```python
def test_settings_derive_managed_paths_and_optional_module_catalog(
    tmp_path: Path,
) -> None:
    settings = Settings.from_env(
        {
            "PCBFLOW_DATA_DIR": str(tmp_path / "data"),
            "PCBFLOW_MODULE_CATALOG_DIR": str(tmp_path / "modules"),
        }
    )

    assert settings.projects_dir == (tmp_path / "data" / "projects").resolve()
    assert settings.workspaces_dir == (tmp_path / "data" / "workspaces").resolve()
    assert settings.module_catalog_dir == (tmp_path / "modules").resolve()
```

Append to `tests/unit/test_process.py`:

```python
def test_runner_uses_controlled_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PCBFLOW_SECRET_SHOULD_NOT_LEAK", "secret")
    runner = ProcessRunner(10_000)
    result = runner.run(
        [
            sys.executable,
            "-c",
            (
                "import os;"
                "print(os.environ.get('PCBFLOW_VISIBLE'));"
                "print(os.environ.get('PCBFLOW_SECRET_SHOULD_NOT_LEAK'))"
            ),
        ],
        tmp_path,
        10,
        env={"PCBFLOW_VISIBLE": "yes"},
    )

    assert result.stdout.splitlines() == ["yes", "None"]
    expected_bytes = (
        b"yes\r\nNone\r\n" if os.name == "nt" else b"yes\nNone\n"
    )
    assert result.stdout_bytes == expected_bytes
```

Import `os` in `tests/unit/test_process.py` for the platform-specific newline
assertion.

- [ ] **Step 2: Run the focused tests and verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_canonical.py tests/unit/test_config.py tests/unit/test_process.py -q
```

Expected: FAIL because `pcbflow.canonical`, the new Settings fields, and `env` parameter do not exist.

- [ ] **Step 3: Implement canonicalization, settings, and environment filtering**

```python
# src/pcbflow/canonical.py
from __future__ import annotations

import hashlib
import json


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_digest(value: object) -> str:
    return f"sha256:{hashlib.sha256(canonical_json_bytes(value)).hexdigest()}"
```

Add to `Settings`:

```python
module_catalog_dir: Path | None = None

@property
def projects_dir(self) -> Path:
    return (self.data_dir / "projects").resolve()

@property
def workspaces_dir(self) -> Path:
    return (self.data_dir / "workspaces").resolve()
```

Populate it in `from_env`:

```python
configured_catalog = values.get("PCBFLOW_MODULE_CATALOG_DIR")
module_catalog_dir=(
    Path(configured_catalog).resolve() if configured_catalog else None
),
```

Extend `ensure_directories()`:

```python
self.projects_dir.mkdir(parents=True, exist_ok=True)
self.workspaces_dir.mkdir(parents=True, exist_ok=True)
```

Add `PyYAML>=6.0.2,<7` to `[project].dependencies`.

Change the process protocol and replace `ProcessRunner.run` plus
`_terminate_tree` with these complete implementations:

```python
from typing import Mapping


class ProcessPort(Protocol):
    def run(
        self,
        argv: Sequence[str],
        cwd: Path,
        timeout_seconds: float,
        *,
        env: Mapping[str, str] | None = None,
    ) -> ProcessResult: ...


def run(
    self,
    argv: Sequence[str],
    cwd: Path,
    timeout_seconds: float,
    *,
    env: Mapping[str, str] | None = None,
) -> ProcessResult:
    if not argv:
        raise ValueError("argv must not be empty")
    working_directory = cwd.resolve(strict=True)
    if not working_directory.is_dir():
        raise ValueError("cwd must be a directory")

    allowed_names = (
        "PATH",
        "PATHEXT",
        "SystemRoot",
        "WINDIR",
        "COMSPEC",
        "TEMP",
        "TMP",
    )
    process_env = {
        name: os.environ[name]
        for name in allowed_names
        if name in os.environ
    }
    if env is not None:
        process_env.update({str(key): str(value) for key, value in env.items()})

    options: dict[str, object] = {}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True

    process = subprocess.Popen(
        [str(value) for value in argv],
        cwd=working_directory,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        env=process_env,
        **options,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    stdout = _BoundedCapture(self._max_output_bytes)
    stderr = _BoundedCapture(self._max_output_bytes)
    stdout_thread = threading.Thread(target=stdout.drain, args=(process.stdout,))
    stderr_thread = threading.Thread(target=stderr.drain, args=(process.stderr,))
    stdout_thread.start()
    stderr_thread.start()

    try:
        returncode = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as error:
        self._terminate_tree(process, process_env)
        process.wait()
        stdout_thread.join()
        stderr_thread.join()
        raise ProcessTimeoutError(argv, timeout_seconds) from error

    stdout_thread.join()
    stderr_thread.join()
    return ProcessResult(
        argv=tuple(str(value) for value in argv),
        returncode=returncode,
        stdout=stdout.text(),
        stderr=stderr.text(),
        output_truncated=stdout.truncated or stderr.truncated,
        stdout_bytes=stdout.bytes(),
        stderr_bytes=stderr.bytes(),
    )


@staticmethod
def _terminate_tree(
    process: subprocess.Popen[bytes], process_env: Mapping[str, str]
) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                timeout=5,
                check=False,
                env=dict(process_env),
            )
        except (OSError, subprocess.SubprocessError):
            process.kill()
        else:
            if result.returncode != 0 and process.poll() is None:
                process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
```

Expose the bounded raw bytes without decoding loss:

```python
def bytes(self) -> bytes:
    return bytes(self._data)


def text(self) -> str:
    return self.bytes().decode("utf-8", errors="replace")


@dataclass(frozen=True, slots=True)
class ProcessResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    output_truncated: bool
    stdout_bytes: bytes = b""
    stderr_bytes: bytes = b""
```

The two methods above belong on `_BoundedCapture`. Existing fake
`ProcessResult` construction remains compatible because the byte fields have
defaults; Git plumbing must require non-truncated raw output before parsing it.
No process launched by `ProcessRunner`, including the Windows `taskkill`
fallback, may inherit the full parent environment.

- [ ] **Step 4: Run focused and regression tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_canonical.py tests/unit/test_config.py tests/unit/test_process.py tests/unit/test_kicad.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add pyproject.toml src/pcbflow/canonical.py src/pcbflow/config.py src/pcbflow/process.py tests/unit/test_canonical.py tests/unit/test_config.py tests/unit/test_process.py
git commit -m "feat: add deterministic serialization foundations"
```

---

### Task 2: Enforce lease expiry on every fenced task transition

**Files:**
- Modify: `src/pcbflow/repositories.py:299-411`
- Modify: `src/pcbflow/tasks.py:43-72`
- Modify: `tests/integration/test_tasks.py`

**Interfaces:**
- Changes: `TaskRepository.start(task_id: str, lease_token: str, now: datetime) -> None`
- Changes: `TaskRepository.complete(task_id: str, lease_token: str, result: dict[str, Any], now: datetime) -> None`
- Changes: `TaskRepository.fail(task_id: str, lease_token: str, error_code: str, retryable: bool, now: datetime) -> None`
- Produces: `TaskRepository.assert_active(task_id: str, lease_token: str, now: datetime) -> None`
- Guarantees: a lease with `lease_expires_at <= now` cannot start, complete, or fail.

- [ ] **Step 1: Write failing expired-transition tests**

Append to `tests/integration/test_tasks.py`:

```python
def test_expired_token_cannot_start_or_complete(
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
        repository.complete(
            task.id,
            lease.lease_token,
            {"ok": True},
            expired_at,
        )
    with pytest.raises(StaleLeaseError):
        repository.assert_active(task.id, lease.lease_token, expired_at)
```

Update Worker tests to use a deterministic mutable clock and assert the Worker passes its clock into start and finish.

- [ ] **Step 2: Run the test and verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration/test_tasks.py -q
```

Expected: FAIL because transition methods do not accept `now` and do not compare expiry.

- [ ] **Step 3: Implement expiry-aware transitions**

Replace the task transition methods with the shared strict active-lease
predicate and complete implementations below:

```python
def start(self, task_id: str, lease_token: str, now: datetime) -> None:
    self._transition_with_lease(
        task_id,
        lease_token,
        [TaskStatus.LEASED],
        now,
        status=TaskStatus.RUNNING.value,
        updated_at=now,
    )

def _transition_with_lease(
    self,
    task_id: str,
    lease_token: str,
    allowed: list[TaskStatus],
    now: datetime,
    **values: Any,
) -> None:
    with self._sessions.begin() as session:
        changed = session.execute(
            update(TaskRow)
            .where(
                TaskRow.id == task_id,
                TaskRow.lease_token == lease_token,
                TaskRow.status.in_([status.value for status in allowed]),
                TaskRow.lease_expires_at.is_not(None),
                TaskRow.lease_expires_at > now,
            )
            .values(**values, version=TaskRow.version + 1)
            .execution_options(synchronize_session=False)
        )
        if changed.rowcount != 1:
            raise StaleLeaseError(task_id)


def complete(
    self,
    task_id: str,
    lease_token: str,
    result: dict[str, Any],
    now: datetime,
) -> None:
    json.dumps(result, sort_keys=True, allow_nan=False)
    self._finish_attempt(
        task_id,
        lease_token,
        now,
        "succeeded",
        None,
        status=TaskStatus.SUCCEEDED.value,
        result_json=result,
        last_error_code=None,
    )


def fail(
    self,
    task_id: str,
    lease_token: str,
    error_code: str,
    retryable: bool,
    now: datetime,
) -> None:
    status = (
        TaskStatus.RETRY_WAIT.value
        if retryable
        else TaskStatus.FAILED_TERMINAL.value
    )
    self._finish_attempt(
        task_id,
        lease_token,
        now,
        "retryable_failure" if retryable else "terminal_failure",
        error_code,
        status=status,
        result_json=None,
        last_error_code=error_code,
    )


def _finish_attempt(
    self,
    task_id: str,
    lease_token: str,
    now: datetime,
    outcome: str,
    error_code: str | None,
    **values: Any,
) -> None:
    with self._sessions.begin() as session:
        changed = session.execute(
            update(TaskRow)
            .where(
                TaskRow.id == task_id,
                TaskRow.lease_token == lease_token,
                TaskRow.status.in_(
                    [TaskStatus.LEASED.value, TaskStatus.RUNNING.value]
                ),
                TaskRow.lease_expires_at.is_not(None),
                TaskRow.lease_expires_at > now,
            )
            .values(
                **values,
                lease_owner=None,
                lease_token=None,
                lease_expires_at=None,
                updated_at=now,
                version=TaskRow.version + 1,
            )
            .execution_options(synchronize_session=False)
        )
        if changed.rowcount != 1:
            raise StaleLeaseError(task_id)
        session.execute(
            update(TaskAttemptRow)
            .where(
                TaskAttemptRow.task_id == task_id,
                TaskAttemptRow.lease_token == lease_token,
                TaskAttemptRow.finished_at.is_(None),
            )
            .values(finished_at=now, outcome=outcome, error_code=error_code)
        )
```

Replace `Worker.run_once` so every transition receives the injected clock:

```python
def run_once(self) -> bool:
    lease = self._repository.claim_next(
        self._worker_id, self._clock(), self._lease_seconds
    )
    if lease is None:
        return False
    self._repository.start(lease.task_id, lease.lease_token, self._clock())
    handler = self._handlers.get(lease.kind)
    if handler is None:
        self._repository.fail(
            lease.task_id,
            lease.lease_token,
            "UNKNOWN_TASK_KIND",
            False,
            self._clock(),
        )
        return True
    try:
        result = handler(lease)
    except RetryableTaskError as error:
        self._repository.fail(
            lease.task_id,
            lease.lease_token,
            error.code,
            True,
            self._clock(),
        )
    except TerminalTaskError as error:
        self._repository.fail(
            lease.task_id,
            lease.lease_token,
            error.code,
            False,
            self._clock(),
        )
    except Exception:
        logger.exception("Unhandled task error", extra={"task_id": lease.task_id})
        self._repository.fail(
            lease.task_id,
            lease.lease_token,
            "UNHANDLED_TASK_ERROR",
            False,
            self._clock(),
        )
    else:
        self._repository.complete(
            lease.task_id,
            lease.lease_token,
            result,
            self._clock(),
        )
    return True
```

Implement the reusable read-only fence check with the identical strict-expiry
predicate; proposal stores use it before external Git writes:

```python
def assert_active(self, task_id: str, lease_token: str, now: datetime) -> None:
    with self._sessions() as session:
        active = session.scalar(
            select(TaskRow.id).where(
                TaskRow.id == task_id,
                TaskRow.lease_token == lease_token,
                TaskRow.status == TaskStatus.RUNNING.value,
                TaskRow.lease_expires_at.is_not(None),
                TaskRow.lease_expires_at > now,
            )
        )
    if active is None:
        raise StaleLeaseError(task_id)
```

- [ ] **Step 4: Run task and E2E regression tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration/test_tasks.py tests/e2e/test_api_cli.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/pcbflow/repositories.py src/pcbflow/tasks.py tests/integration/test_tasks.py
git commit -m "fix: enforce task lease expiry at commit"
```

---

### Task 3: Add the Phase 2A schema migration and ORM rows

**Files:**
- Create: `alembic/versions/0002_controlled_design_changes.py`
- Create: `src/pcbflow/design_tables.py`
- Create: `tests/integration/test_design_migration.py`
- Modify: `alembic/env.py:8-12`
- Modify: `src/pcbflow/tables.py:23-31`
- Modify: `src/pcbflow/domain.py:20-25`
- Modify: `tests/integration/test_migrations.py`
- Modify: `tests/integration/test_projects.py`

**Interfaces:**
- Produces tables: `project_revisions`, `requirement_sets`, `gate_decisions`, `design_command_batches`, `design_commands`, `change_proposals`, `outbox_events`
- Extends `projects`: `mode`, `managed_repo_key`, `current_revision`, `project_snapshot_digest`, `active_requirement_set_id`, `adoption_idempotency_key`, `adoption_input_digest`, `managed_at`, `version`
- Guarantees: the adoption key is globally unique, requirement import and submission keys are separately unique per project, and `canonical_artifact_digest` references `artifacts.digest`.
- Produces: `ProjectMode`
- Extends: `Project` with managed-state projections.

- [ ] **Step 1: Write a failing migration contract**

```python
# tests/integration/test_design_migration.py
from __future__ import annotations

from sqlalchemy import inspect


def test_phase_2a_migration_creates_design_schema(migrated_engine) -> None:
    inspector = inspect(migrated_engine)
    tables = set(inspector.get_table_names())
    assert {
        "project_revisions",
        "requirement_sets",
        "gate_decisions",
        "design_command_batches",
        "design_commands",
        "change_proposals",
        "outbox_events",
    } <= tables

    project_columns = {
        column["name"] for column in inspector.get_columns("projects")
    }
    assert {
        "mode",
        "managed_repo_key",
        "current_revision",
        "project_snapshot_digest",
        "active_requirement_set_id",
        "adoption_idempotency_key",
        "adoption_input_digest",
        "managed_at",
        "version",
    } <= project_columns
    project_uniques = {
        tuple(item["column_names"])
        for item in inspector.get_indexes("projects")
        if item.get("unique")
    }
    assert ("adoption_idempotency_key",) in project_uniques

    command_uniques = {
        tuple(item["column_names"])
        for item in inspector.get_unique_constraints("design_commands")
    }
    assert ("project_id", "idempotency_key") in command_uniques
    assert ("batch_id", "ordinal") in command_uniques

    requirement_uniques = {
        tuple(item["column_names"])
        for item in inspector.get_unique_constraints("requirement_sets")
    }
    assert ("project_id", "idempotency_key") in requirement_uniques
    assert ("project_id", "submission_idempotency_key") in requirement_uniques
    requirement_foreign_keys = {
        (
            tuple(item["constrained_columns"]),
            item["referred_table"],
            tuple(item["referred_columns"]),
        )
        for item in inspector.get_foreign_keys("requirement_sets")
    }
    assert (
        ("canonical_artifact_digest",),
        "artifacts",
        ("digest",),
    ) in requirement_foreign_keys
```

Append the registered-project projection regression to
`tests/integration/test_projects.py` and import `ProjectMode`:

```python
def test_new_project_exposes_registered_phase_2a_defaults(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    repository = ProjectRepository(session_factory)
    source = tmp_path / "registered"
    source.mkdir()

    project = repository.create("Registered", source, "registered-defaults")

    assert project.mode is ProjectMode.REGISTERED
    assert project.managed_repo_key is None
    assert project.current_revision is None
    assert project.project_snapshot_digest is None
    assert project.active_requirement_set_id is None
    assert project.adoption_idempotency_key is None
    assert project.adoption_input_digest is None
    assert project.managed_at is None
    assert project.version == 1
```

- [ ] **Step 2: Run migration tests and verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration/test_migrations.py tests/integration/test_design_migration.py -q
```

Expected: FAIL because revision `0002` and the new tables do not exist.

- [ ] **Step 3: Implement the migration**

Create `0002_controlled_design_changes.py` with:

```python
"""Add managed revisions and controlled design changes."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_controlled_design_changes"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("mode", sa.String(32), nullable=False, server_default="registered"),
    )
    op.add_column("projects", sa.Column("managed_repo_key", sa.Text(), nullable=True))
    op.add_column("projects", sa.Column("current_revision", sa.String(80), nullable=True))
    op.add_column(
        "projects",
        sa.Column("project_snapshot_digest", sa.String(71), nullable=True),
    )
    op.add_column(
        "projects",
        sa.Column("active_requirement_set_id", sa.String(64), nullable=True),
    )
    op.add_column(
        "projects",
        sa.Column("adoption_idempotency_key", sa.String(255), nullable=True),
    )
    op.add_column(
        "projects",
        sa.Column("adoption_input_digest", sa.String(71), nullable=True),
    )
    op.create_index(
        "uq_projects_adoption_key",
        "projects",
        ["adoption_idempotency_key"],
        unique=True,
    )
    op.add_column(
        "projects", sa.Column("managed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "projects",
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
    )

    op.create_table(
        "requirement_sets",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(64),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("submission_idempotency_key", sa.String(255), nullable=True),
        sa.Column("base_revision", sa.String(80), nullable=False),
        sa.Column("schema_version", sa.String(16), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("canonical_digest", sa.String(71), nullable=False),
        sa.Column(
            "canonical_artifact_digest",
            sa.String(71),
            sa.ForeignKey("artifacts.digest"),
            nullable=False,
        ),
        sa.Column("candidate_revision", sa.String(80), nullable=True),
        sa.Column("candidate_snapshot_digest", sa.String(71), nullable=True),
        sa.Column("frozen_revision", sa.String(80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("frozen_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "project_id", "idempotency_key", name="uq_requirement_set_key"
        ),
        sa.UniqueConstraint(
            "project_id",
            "submission_idempotency_key",
            name="uq_requirement_submission_key",
        ),
    )
    op.create_index(
        "ix_requirement_sets_project_status",
        "requirement_sets",
        ["project_id", "status"],
    )

    op.create_table(
        "project_revisions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(64),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("revision", sa.String(80), nullable=False),
        sa.Column("parent_revision", sa.String(80), nullable=True),
        sa.Column("snapshot_digest", sa.String(71), nullable=False),
        sa.Column(
            "requirement_set_id",
            sa.String(64),
            sa.ForeignKey("requirement_sets.id"),
            nullable=True,
        ),
        sa.Column("command_batch_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "project_id", "revision", name="uq_project_revision"
        ),
    )

    op.create_table(
        "gate_decisions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(64),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("gate", sa.String(32), nullable=False),
        sa.Column("subject_type", sa.String(32), nullable=False),
        sa.Column("subject_id", sa.String(64), nullable=False),
        sa.Column("subject_digest", sa.String(71), nullable=False),
        sa.Column("base_revision", sa.String(80), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("actor_type", sa.String(32), nullable=False),
        sa.Column("actor_id", sa.String(255), nullable=False),
        sa.Column("comment", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "project_id", "idempotency_key", name="uq_gate_decision_key"
        ),
    )

    op.create_table(
        "design_command_batches",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(64),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "requirement_set_id",
            sa.String(64),
            sa.ForeignKey("requirement_sets.id"),
            nullable=False,
        ),
        sa.Column("base_revision", sa.String(80), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("actor_json", sa.JSON(), nullable=False),
        sa.Column("intent", sa.Text(), nullable=False),
        sa.Column("risk", sa.String(16), nullable=False),
        sa.Column("commands_json", sa.JSON(), nullable=False),
        sa.Column("canonical_digest", sa.String(71), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "project_id", "idempotency_key", name="uq_command_batch_key"
        ),
    )

    op.create_table(
        "design_commands",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "batch_id",
            sa.String(64),
            sa.ForeignKey("design_command_batches.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            sa.String(64),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("operation_type", sa.String(128), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("canonical_digest", sa.String(71), nullable=False),
        sa.UniqueConstraint("batch_id", "ordinal", name="uq_command_ordinal"),
        sa.UniqueConstraint(
            "project_id", "idempotency_key", name="uq_design_command_key"
        ),
        sa.UniqueConstraint("batch_id", "id", name="uq_batch_command_id"),
    )

    op.create_table(
        "change_proposals",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(64),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "command_batch_id",
            sa.String(64),
            sa.ForeignKey("design_command_batches.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "task_id",
            sa.String(64),
            sa.ForeignKey("tasks.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("candidate_revision", sa.String(80), nullable=True),
        sa.Column("candidate_snapshot_digest", sa.String(71), nullable=True),
        sa.Column("review_digest", sa.String(71), nullable=True),
        sa.Column("semantic_diff_digest", sa.String(71), nullable=True),
        sa.Column("evidence_set_digest", sa.String(71), nullable=True),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column("last_error_code", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.create_index(
        "ix_change_proposals_project_status",
        "change_proposals",
        ["project_id", "status"],
    )

    op.create_table(
        "outbox_events",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("aggregate_type", sa.String(64), nullable=False),
        sa.Column("aggregate_id", sa.String(64), nullable=False),
        sa.Column("event_type", sa.String(128), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error_code", sa.String(128), nullable=True),
    )
    op.create_index(
        "ix_outbox_unprocessed",
        "outbox_events",
        ["processed_at", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_unprocessed", table_name="outbox_events")
    op.drop_table("outbox_events")
    op.drop_index(
        "ix_change_proposals_project_status", table_name="change_proposals"
    )
    op.drop_table("change_proposals")
    op.drop_table("design_commands")
    op.drop_table("design_command_batches")
    op.drop_table("gate_decisions")
    op.drop_table("project_revisions")
    op.drop_index(
        "ix_requirement_sets_project_status", table_name="requirement_sets"
    )
    op.drop_table("requirement_sets")
    op.drop_column("projects", "version")
    op.drop_column("projects", "managed_at")
    op.drop_index("uq_projects_adoption_key", table_name="projects")
    op.drop_column("projects", "adoption_input_digest")
    op.drop_column("projects", "adoption_idempotency_key")
    op.drop_column("projects", "active_requirement_set_id")
    op.drop_column("projects", "project_snapshot_digest")
    op.drop_column("projects", "current_revision")
    op.drop_column("projects", "managed_repo_key")
    op.drop_column("projects", "mode")
```

Create matching SQLAlchemy rows in `design_tables.py` using the shared `Base`,
including the requirement-set idempotency constraint and artifact foreign key.
Add the new project columns to `ProjectRow`, import `pcbflow.design_tables` in
`alembic/env.py`, and define:

```python
class ProjectMode(StrEnum):
    REGISTERED = "registered"
    MANAGED = "managed"
```

Add these exact `ProjectRow` mappings in `tables.py`:

```python
mode: Mapped[str] = mapped_column(
    String(32), nullable=False, default=ProjectMode.REGISTERED.value
)
managed_repo_key: Mapped[str | None] = mapped_column(Text, nullable=True)
current_revision: Mapped[str | None] = mapped_column(String(80), nullable=True)
project_snapshot_digest: Mapped[str | None] = mapped_column(
    String(71), nullable=True
)
active_requirement_set_id: Mapped[str | None] = mapped_column(
    String(64), nullable=True
)
adoption_idempotency_key: Mapped[str | None] = mapped_column(
    String(255), nullable=True, unique=True
)
adoption_input_digest: Mapped[str | None] = mapped_column(
    String(71), nullable=True
)
managed_at: Mapped[datetime | None] = mapped_column(
    DateTime(timezone=True), nullable=True
)
version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
```

Import `ProjectMode` into `tables.py`, and replace the Phase 0 `Project`
projection with the complete immutable model:

```python
@dataclass(frozen=True, slots=True)
class Project:
    id: str
    name: str
    source_path: Path
    created_at: datetime
    mode: ProjectMode
    managed_repo_key: str | None
    current_revision: str | None
    project_snapshot_digest: str | None
    active_requirement_set_id: str | None
    adoption_idempotency_key: str | None
    adoption_input_digest: str | None
    managed_at: datetime | None
    version: int
```

Update `ProjectRepository._project` in Task 4 to map every field. The migration
must drop `uq_projects_adoption_key` before either adoption column during
SQLite downgrade, as shown above.

- [ ] **Step 4: Run migration and project regression tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration/test_migrations.py tests/integration/test_design_migration.py tests/integration/test_projects.py -q
```

Expected: PASS, and existing/new projects deserialize with
`mode="registered"`, both adoption fields `None`, all other managed fields
`None`, and `version == 1`.

- [ ] **Step 5: Commit**

```powershell
git add alembic/env.py alembic/versions/0002_controlled_design_changes.py src/pcbflow/design_tables.py src/pcbflow/tables.py src/pcbflow/domain.py tests/integration/test_design_migration.py tests/integration/test_migrations.py tests/integration/test_projects.py
git commit -m "feat: add controlled design persistence schema"
```

---

### Task 4: Extend ProjectRepository for managed revisions

**Files:**
- Create: `tests/integration/test_managed_projects.py`
- Create: `src/pcbflow/revision_store.py`
- Modify: `src/pcbflow/domain.py`
- Modify: `src/pcbflow/repositories.py:54-157`

**Interfaces:**
- Produces: `ProjectRepository.mark_managed(project_id: str, *, repo_key: str, revision: str, snapshot_digest: str, adoption_idempotency_key: str, adoption_input_digest: str, source_head: str | None, expected_version: int) -> Project`
- Produces: `ProjectRepository.find_by_adoption_key(idempotency_key: str) -> Project | None`
- Produces: `ProjectRepository.compare_and_set_revision(project_id: str, *, expected_revision: str, new_revision: str, snapshot_digest: str, expected_version: int) -> Project`
- Produces: `ProjectRevisionStore.get(project_id: str, revision: str) -> ProjectRevision`
- Produces: `ProjectRevisionNotFoundError`

- [ ] **Step 1: Write failing managed-project repository tests**

```python
# tests/integration/test_managed_projects.py
from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from pcbflow.design_tables import OutboxEventRow
from pcbflow.domain import ProjectMode
from pcbflow.revision_store import ProjectRevisionStore
from pcbflow.repositories import (
    IdempotencyConflictError,
    ProjectRepository,
    RevisionConflictError,
)


def test_project_can_be_marked_managed_and_revision_compared(
    session_factory, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    projects = ProjectRepository(session_factory)
    revisions = ProjectRevisionStore(session_factory)
    project = projects.create("Controller", source, "project-create")

    managed = projects.mark_managed(
        project.id,
        repo_key=project.id,
        revision="git:1111111111111111111111111111111111111111",
        snapshot_digest="sha256:" + "a" * 64,
        adoption_idempotency_key="adopt-controller",
        adoption_input_digest="sha256:" + "c" * 64,
        source_head="git:" + "9" * 40,
        expected_version=project.version,
    )
    assert managed.mode is ProjectMode.MANAGED
    assert managed.version == 2
    assert managed.adoption_idempotency_key == "adopt-controller"
    assert managed.adoption_input_digest == "sha256:" + "c" * 64
    assert projects.find_by_adoption_key("adopt-controller") == managed

    replayed = projects.mark_managed(
        project.id,
        repo_key=project.id,
        revision="git:1111111111111111111111111111111111111111",
        snapshot_digest="sha256:" + "a" * 64,
        adoption_idempotency_key="adopt-controller",
        adoption_input_digest="sha256:" + "c" * 64,
        source_head="git:" + "9" * 40,
        expected_version=project.version,
    )
    assert replayed == managed
    assert revisions.get(project.id, managed.current_revision).revision == (
        managed.current_revision
    )

    with pytest.raises(IdempotencyConflictError):
        projects.mark_managed(
            project.id,
            repo_key=project.id,
            revision="git:1111111111111111111111111111111111111111",
            snapshot_digest="sha256:" + "a" * 64,
            adoption_idempotency_key="adopt-controller",
            adoption_input_digest="sha256:" + "d" * 64,
            source_head="git:" + "9" * 40,
            expected_version=managed.version,
        )

    rekeyed = projects.mark_managed(
        project.id,
        repo_key=project.id,
        revision="git:1111111111111111111111111111111111111111",
        snapshot_digest="sha256:" + "a" * 64,
        adoption_idempotency_key="a-second-adoption-key",
        adoption_input_digest="sha256:" + "c" * 64,
        source_head="git:" + "8" * 40,
        expected_version=managed.version,
    )
    assert rekeyed == managed

    with session_factory() as session:
        adopted_event = session.scalar(
            select(OutboxEventRow).where(
                OutboxEventRow.aggregate_id == project.id,
                OutboxEventRow.event_type == "project.adopted",
            )
        )
        assert adopted_event is not None
        assert adopted_event.payload_json["source_head"] == "git:" + "9" * 40

    with pytest.raises(RevisionConflictError):
        projects.compare_and_set_revision(
            project.id,
            expected_revision="git:" + "2" * 40,
            new_revision="git:" + "3" * 40,
            snapshot_digest="sha256:" + "b" * 64,
            expected_version=managed.version,
        )
```

- [ ] **Step 2: Run the test and verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration/test_managed_projects.py -q
```

Expected: FAIL because the repositories and error do not exist.

- [ ] **Step 3: Implement optimistic managed-project updates**

Add:

```python
class RevisionConflictError(RuntimeError):
    def __init__(self, expected: str | None, actual: str | None) -> None:
        super().__init__(f"expected {expected}, found {actual}")
        self.expected = expected
        self.actual = actual
```

Map all new Project fields in `_project`. Implement updates with:

```python
changed = session.execute(
    update(ProjectRow)
    .where(
        ProjectRow.id == project_id,
        ProjectRow.version == expected_version,
        ProjectRow.current_revision == expected_revision,
    )
    .values(
        current_revision=new_revision,
        project_snapshot_digest=snapshot_digest,
        version=ProjectRow.version + 1,
    )
    .execution_options(synchronize_session=False)
)
if changed.rowcount != 1:
    actual = session.get(ProjectRow, project_id)
    if actual is None:
        raise ProjectNotFoundError(project_id)
    raise RevisionConflictError(expected_revision, actual.current_revision)
```

`find_by_adoption_key` queries the globally unique nullable adoption key.
`mark_managed` must check that key before its optimistic version predicate:

1. If the key exists on another project, or its stored import-content digest differs,
   raise `IdempotencyConflictError`.
2. If the key exists on this project and repo key, revision, snapshot, and
   input digest all match, return the existing managed projection even when
   the caller's `expected_version` is stale.
3. If this project is already managed under a different key but its stored
   import-content digest matches, return the existing managed projection
   without replacing the original adoption key or provenance. If the digest
   differs, raise `IdempotencyConflictError`; changed source content requires a
   later explicit re-import workflow.
4. Otherwise compare-and-set the registered Project, persist both adoption
   fields, and insert its initial `ProjectRevisionRow` with `parent_revision`,
   `requirement_set_id`, and `command_batch_id` all `None` in the same SQLite
   transaction. Insert one `project.adopted` outbox row in that transaction;
   its payload records project ID, revision, snapshot digest, import-content
   digest, and nullable source HEAD provenance.
5. If the unique-key insert/update loses a race, re-read by adoption key and
   apply rules 1-2 instead of leaking `IntegrityError`.

The atomic initial revision insert is why the test no longer calls another
repository after `mark_managed`. G1 updates active set and revision in its
own requirement-store transaction rather than adding another broad method to
`ProjectRepository`.

Create immutable `ProjectRevision` in `domain.py` and the focused SQLAlchemy
`ProjectRevisionStore` in `revision_store.py`; do not add new design-table
repositories to the Phase 0 `repositories.py` module.

```python
# src/pcbflow/domain.py
@dataclass(frozen=True, slots=True)
class ProjectRevision:
    id: str
    project_id: str
    revision: str
    parent_revision: str | None
    snapshot_digest: str
    requirement_set_id: str | None
    command_batch_id: str | None
    created_at: datetime
```

```python
# src/pcbflow/revision_store.py
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from pcbflow.design_tables import ProjectRevisionRow
from pcbflow.domain import ProjectRevision


class ProjectRevisionNotFoundError(LookupError):
    pass


def _revision(row: ProjectRevisionRow) -> ProjectRevision:
    created_at = (
        row.created_at.replace(tzinfo=UTC)
        if row.created_at.tzinfo is None
        else row.created_at.astimezone(UTC)
    )
    return ProjectRevision(
        id=row.id,
        project_id=row.project_id,
        revision=row.revision,
        parent_revision=row.parent_revision,
        snapshot_digest=row.snapshot_digest,
        requirement_set_id=row.requirement_set_id,
        command_batch_id=row.command_batch_id,
        created_at=created_at,
    )


class ProjectRevisionStore:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def get(self, project_id: str, revision: str) -> ProjectRevision:
        with self._sessions() as session:
            row = session.scalar(
                select(ProjectRevisionRow).where(
                    ProjectRevisionRow.project_id == project_id,
                    ProjectRevisionRow.revision == revision,
                )
            )
            if row is None:
                raise ProjectRevisionNotFoundError(f"{project_id}:{revision}")
            return _revision(row)
```

- [ ] **Step 4: Run focused tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration/test_managed_projects.py tests/integration/test_projects.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/pcbflow/domain.py src/pcbflow/repositories.py src/pcbflow/revision_store.py tests/integration/test_managed_projects.py
git commit -m "feat: persist managed project revisions"
```

---

### Task 5: Add safe workspace copying and managed Git adoption

**Files:**
- Create: `src/pcbflow/workspaces.py`
- Create: `src/pcbflow/revisions.py`
- Create: `tests/unit/test_revisions.py`
- Modify: `tests/integration/test_managed_projects.py`
- Modify: `src/pcbflow/container.py:34-106`

**Interfaces:**
- Produces: `WorkspaceCopier.copy(source: Path, destination: Path, *, exclude_names: frozenset[str] = frozenset(), registered_excludes: frozenset[str] = frozenset()) -> None`
- Produces: `GitCli.init_bare(repo: Path) -> None`
- Produces: `GitCli.read_source_head(source: Path) -> str | None`
- Produces: `GitCli.commit_snapshot(repo: Path, tree: Path, *, parent: str | None, message: str, timestamp: datetime, registered_excludes: frozenset[str] = frozenset()) -> str`
- Produces: `GitCli.add_worktree(repo: Path, revision: str, destination: Path) -> ContextManager[Path]`
- Produces: `GitCli.update_ref(repo: Path, ref: str, new_revision: str, expected_revision: str | None) -> None`
- Produces: `GitCli.list_refs(repo: Path, prefix: str) -> dict[str, str]`
- Produces: `RevisionService.adopt(project_id: str, idempotency_key: str) -> Project`
- Produces: `RevisionService.materialize(project_id: str, revision: str, purpose: str) -> ContextManager[Path]`
- Produces: `RevisionService.snapshot_digest(root: Path, registered_excludes: frozenset[str] = frozenset()) -> str`
- Produces: `CandidateRevision(revision: str, snapshot_digest: str)`
- Produces: `RevisionService.commit_candidate(project: Project, workspace: Path, base_revision: str, ref: str, message: str, timestamp: datetime) -> CandidateRevision`
- Produces: `RevisionService.resolve_design_ref(project_id: str) -> str | None`
- Produces: `RevisionService.resolve_proposal_ref(project_id: str, proposal_id: str) -> str | None`
- Produces: `RevisionService.list_proposal_refs(project_id: str) -> dict[str, str]`
- Produces: `RevisionService.is_ancestor(project_id: str, ancestor: str, descendant: str) -> bool`
- Produces: `RevisionService.assert_clean(project_id: str, revision: str, workspace: Path) -> None` and `ProjectWorktreeDirtyError`.
- Adds: `Container.revision_store: ProjectRevisionStore`; the one
  `RevisionService` instance receives this store by constructor injection.

- [ ] **Step 1: Write failing Git and adoption tests**

```python
# tests/unit/test_revisions.py
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from pcbflow.process import ProcessRunner
from pcbflow.revisions import GitCli


def test_git_cli_creates_deterministic_commit_and_compare_updates_ref(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo.git"
    tree = tmp_path / "tree"
    tree.mkdir()
    original = b"(kicad_sch\r\n  (version 20250114))\r\n"
    (tree / "board.kicad_sch").write_bytes(original)
    (tree / "~board.kicad_sch.lck").write_text("volatile", encoding="utf-8")
    (tree / "build").mkdir()
    (tree / "build" / "cache.bin").write_bytes(b"cache")
    git = GitCli(ProcessRunner(100_000), timeout_seconds=20)
    created_at = datetime(2026, 7, 29, 12, tzinfo=UTC)

    git.init_bare(repo)
    first = git.commit_snapshot(
        repo,
        tree,
        parent=None,
        message="pcbflow: adopt project",
        timestamp=created_at,
        registered_excludes=frozenset({"build"}),
    )
    second = git.commit_snapshot(
        repo,
        tree,
        parent=None,
        message="pcbflow: adopt project",
        timestamp=created_at,
        registered_excludes=frozenset({"build"}),
    )
    assert first == second

    git.update_ref(repo, "refs/heads/design", first, expected_revision=None)
    assert git.resolve_ref(repo, "refs/heads/design") == first
    git.update_ref(repo, "refs/heads/design", first, expected_revision=None)
    assert git.resolve_ref(repo, "refs/heads/design") == first
    with git.add_worktree(repo, first, tmp_path / "checkout") as checkout:
        assert (checkout / "board.kicad_sch").read_bytes() == original
        assert not (checkout / "~board.kicad_sch.lck").exists()
        assert not (checkout / "build").exists()

    git.update_ref(repo, "refs/heads/master", first, expected_revision=None)
    linked_source = tmp_path / "linked-source"
    linked_source.mkdir()
    (linked_source / ".git").write_text(
        f"gitdir: {repo.as_posix()}\n", encoding="utf-8"
    )
    assert git.read_source_head(linked_source) == first
```

Append to `tests/integration/test_managed_projects.py`:

```python
def test_adopt_copies_snapshot_without_mutating_source_or_importing_git_metadata(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "board.kicad_sch").write_text("(kicad_sch)", encoding="utf-8")
    (source / "~board.kicad_sch.lck").write_text("volatile", encoding="utf-8")
    (source / ".git").mkdir()
    (source / ".git" / "config").write_text("untrusted", encoding="utf-8")
    before = {
        path.relative_to(source): path.read_bytes()
        for path in source.rglob("*")
        if path.is_file()
    }
    container = build_container(_settings(tmp_path))
    project = container.projects.create("Controller", source, "create-project")

    adopted = container.revisions.adopt(project.id, "adopt-project")

    assert adopted.mode is ProjectMode.MANAGED
    assert adopted.current_revision.startswith("git:")
    assert {
        path.relative_to(source): path.read_bytes()
        for path in source.rglob("*")
        if path.is_file()
    } == before
    with container.revisions.materialize(
        project.id, adopted.current_revision, "assert-adopt"
    ) as checkout:
        assert (checkout / "board.kicad_sch").is_file()
        assert (checkout / "pcbflow.yaml").is_file()
        assert not (checkout / "~board.kicad_sch.lck").exists()
        assert not (checkout / ".git" / "config").is_file()


def test_adopt_replay_and_rekey_return_original_for_unchanged_import(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "board.kicad_sch").write_text("(kicad_sch)", encoding="utf-8")
    container = build_container(_settings(tmp_path))
    project = container.projects.create("Controller", source, "create-replay")

    first = container.revisions.adopt(project.id, "adopt-replay")
    replay = container.revisions.adopt(project.id, "adopt-replay")
    rekeyed = container.revisions.adopt(project.id, "adopt-replay-new-key")

    assert replay == first
    assert rekeyed == first
    assert first.version == 2

    (source / "board.kicad_sch").write_text("(kicad_sch changed)", encoding="utf-8")
    with pytest.raises(IdempotencyConflictError):
        container.revisions.adopt(project.id, "adopt-replay")
    with pytest.raises(IdempotencyConflictError):
        container.revisions.adopt(project.id, "adopt-after-source-change")


def test_adopt_rejects_cross_project_key_reuse(
    tmp_path: Path,
) -> None:
    first_source = tmp_path / "first"
    second_source = tmp_path / "second"
    first_source.mkdir()
    second_source.mkdir()
    (first_source / "board.kicad_sch").write_text("(kicad_sch)", encoding="utf-8")
    (second_source / "board.kicad_sch").write_text("(kicad_sch)", encoding="utf-8")
    container = build_container(_settings(tmp_path))
    first = container.projects.create("First", first_source, "create-first")
    second = container.projects.create("Second", second_source, "create-second")

    container.revisions.adopt(first.id, "one-adoption-key")

    with pytest.raises(IdempotencyConflictError):
        container.revisions.adopt(second.id, "one-adoption-key")
```

Import `IdempotencyConflictError` from `pcbflow.repositories` in this test
module.

- [ ] **Step 2: Run focused tests and verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_revisions.py tests/integration/test_managed_projects.py -q
```

Expected: FAIL because GitCli, WorkspaceCopier, RevisionService, and Container wiring do not exist.

- [ ] **Step 3: Implement safe copying and Git plumbing**

```python
# src/pcbflow/workspaces.py
from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path, PurePosixPath


KICAD_LOCK_SUFFIXES = (
    ".kicad_sch.lck",
    ".kicad_pcb.lck",
    ".kicad_pro.lck",
)


def is_kicad_lock_name(name: str) -> bool:
    return name.startswith("~") and name.endswith(KICAD_LOCK_SUFFIXES)


def normalize_snapshot_excludes(values: frozenset[str]) -> frozenset[PurePosixPath]:
    normalized: set[PurePosixPath] = set()
    for value in values:
        path = PurePosixPath(value)
        if (
            not value
            or path.is_absolute()
            or path.as_posix() != value
            or "\\" in value
            or (path.parts and path.parts[0].endswith(":"))
            or any(part in {"", ".", "..", ".git", "pcbflow.yaml"} for part in path.parts)
            or any(part.startswith(".pcbflow-tmp-") for part in path.parts)
        ):
            raise ValueError(f"invalid snapshot exclusion: {value}")
        normalized.add(path)
    return frozenset(normalized)


def is_snapshot_excluded(
    relative: PurePosixPath,
    registered_excludes: frozenset[PurePosixPath],
) -> bool:
    if ".git" in relative.parts or is_kicad_lock_name(relative.name):
        return True
    return any(
        relative == excluded or excluded in relative.parents
        for excluded in registered_excludes
    )


class WorkspaceLimitError(ValueError):
    pass


class WorkspaceLinkError(ValueError):
    pass


class WorkspaceCopier:
    def __init__(self, max_files: int, max_bytes: int) -> None:
        self._max_files = max_files
        self._max_bytes = max_bytes

    def copy(
        self,
        source: Path,
        destination: Path,
        *,
        exclude_names: frozenset[str] = frozenset(),
        registered_excludes: frozenset[str] = frozenset(),
    ) -> None:
        source = source.resolve(strict=True)
        normalized_excludes = normalize_snapshot_excludes(registered_excludes)
        file_count = 0
        total_bytes = 0
        for root, directories, files in os.walk(source, followlinks=False):
            root_path = Path(root)
            root_relative = root_path.relative_to(source)
            directories[:] = [
                name
                for name in directories
                if name not in exclude_names
                and not is_snapshot_excluded(
                    PurePosixPath((root_relative / name).as_posix()),
                    normalized_excludes,
                )
            ]
            for name in [*directories, *files]:
                path = root_path / name
                metadata = path.lstat()
                attributes = getattr(metadata, "st_file_attributes", 0)
                reparse_flag = getattr(
                    stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0
                )
                if path.is_symlink() or attributes & reparse_flag:
                    raise WorkspaceLinkError(str(path))
            for name in files:
                path = root_path / name
                relative = PurePosixPath(path.relative_to(source).as_posix())
                if name in exclude_names or is_snapshot_excluded(
                    relative, normalized_excludes
                ):
                    continue
                file_count += 1
                total_bytes += path.stat().st_size
                if file_count > self._max_files:
                    raise WorkspaceLimitError("file_count")
                if total_bytes > self._max_bytes:
                    raise WorkspaceLimitError("total_bytes")
        def ignored(directory: str, names: list[str]) -> set[str]:
            root_relative = Path(directory).resolve().relative_to(source)
            return {
                name
                for name in names
                if name in exclude_names
                or is_snapshot_excluded(
                    PurePosixPath((root_relative / name).as_posix()),
                    normalized_excludes,
                )
            }

        shutil.copytree(source, destination, symlinks=False, ignore=ignored)
```

In `revisions.py`, define `GitCli` using only argument arrays. Import
`PurePosixPath`, `stat`, `WorkspaceLinkError`, `is_snapshot_excluded`, and
`normalize_snapshot_excludes`. Use a temporary index for `commit_snapshot`:

```python
env = {
    "GIT_INDEX_FILE": str(index_path),
    "GIT_AUTHOR_NAME": "PCBFlow",
    "GIT_AUTHOR_EMAIL": "pcbflow@local.invalid",
    "GIT_COMMITTER_NAME": "PCBFlow",
    "GIT_COMMITTER_EMAIL": "pcbflow@local.invalid",
    "GIT_AUTHOR_DATE": timestamp.isoformat(),
    "GIT_COMMITTER_DATE": timestamp.isoformat(),
}
normalized_registered_excludes = normalize_snapshot_excludes(
    registered_excludes
)
self._run(
    ["git", f"--git-dir={repo}", "read-tree", "--empty"],
    tree,
    env=env,
)
for path in sorted(
    (candidate for candidate in tree.rglob("*") if candidate.is_file()),
    key=lambda candidate: candidate.relative_to(tree).as_posix(),
):
    metadata = path.lstat()
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if path.is_symlink() or attributes & reparse_flag:
        raise WorkspaceLinkError(str(path))
    relative = path.relative_to(tree).as_posix()
    if is_snapshot_excluded(
        PurePosixPath(relative), normalized_registered_excludes
    ):
        continue
    blob = self._run(
        [
            "git",
            f"--git-dir={repo}",
            "hash-object",
            "-w",
            "--no-filters",
            "--",
            str(path),
        ],
        tree,
        env=env,
    ).stdout.strip()
    mode = "100755" if metadata.st_mode & stat.S_IXUSR else "100644"
    self._run(
        [
            "git",
            f"--git-dir={repo}",
            "update-index",
            "--add",
            "--cacheinfo",
            mode,
            blob,
            relative,
        ],
        tree,
        env=env,
    )
tree_id = self._run(
    ["git", f"--git-dir={repo}", "write-tree"], tree, env=env
).stdout.strip()
argv = ["git", f"--git-dir={repo}", "commit-tree", tree_id, "-m", message]
if parent is not None:
    argv.extend(["-p", parent.removeprefix("git:")])
commit = self._run(argv, tree, env=env).stdout.strip()
return f"git:{commit}"
```

`GitCli.read_source_head` runs `git -C <source> rev-parse --verify HEAD^{commit}`
with the same controlled environment, disabled hooks and system/global config,
bounded output, and no shell. Return `git:<object-id>` only for a valid 40-64
lowercase hex object; return `None` when the source is not a readable Git
worktree. The read-only probe may let Git parse its local worktree metadata, but
never copies or executes its config, hooks, refs, or objects.

After safely copying the source, before writing `pcbflow.yaml` or touching the
managed repo, compute the adoption operation's full canonical input:

```python
source_snapshot_digest = self.snapshot_digest(
    staging_workspace,
    registered_excludes=frozenset(),
)
input_digest = canonical_digest(
    {
        "schema_version": "1.0",
        "project_id": project_id,
        "source_snapshot_digest": source_snapshot_digest,
    }
)
```

`RevisionService.adopt` must:

1. Reject an empty idempotency key. Query
   `ProjectRepository.find_by_adoption_key`; if the key belongs to another
   project, raise `IdempotencyConflictError` immediately.
2. Load the requested Project, read nullable source HEAD provenance, and safely
   copy the source while excluding Git metadata and KiCad lock files. Compute
   `source_snapshot_digest` and `input_digest` from those copied bytes.
3. If the key already belongs to this project, return the original Project only
   when its stored input digest matches; otherwise raise
   `IdempotencyConflictError`.
4. If the Project is already managed under another key, return it when its
   stored input digest matches. If source content changed, raise
   `IdempotencyConflictError`; Phase 2A never silently overwrites the accepted
   import and the caller must use the later explicit re-import workflow.
5. Write a deterministic minimal `pcbflow.yaml` without `current_revision`.
   Include snapshot policy version `1`, an initially empty validated
   `snapshot_excludes` list, and the import source snapshot digest; source HEAD
   remains audit provenance rather than a self-changing manifest input.
6. Create a deterministic initial commit using `Project.created_at`.
7. Create `refs/heads/design`.
8. Materialize that exact object once and require its exclusion-policy-v1
   snapshot digest to equal the pre-commit staging digest. This detects any Git
   attribute/checkout transform before managed state is published.
9. Call `ProjectRepository.mark_managed` with the idempotency key,
   `input_digest`, and nullable source HEAD; it atomically persists the managed
   Project, initial ProjectRevision, and adoption provenance outbox event only
   after the proof passes. A same-content race returns the persisted object.
10. Remove the staging/materialization workspaces in `finally`; on a proof
   failure also remove the not-yet-published managed repo and raise
   `ProjectWorktreeDirtyError`. Never remove a repo that existed before this
   call or that a concurrent successful adoption has published.

The new-path persistence call is exact:

```python
managed = self._projects.mark_managed(
    project.id,
    repo_key=project.id,
    revision=revision,
    snapshot_digest=project_snapshot_digest,
    adoption_idempotency_key=idempotency_key,
    adoption_input_digest=input_digest,
    source_head=source_head,
    expected_version=project.version,
)
return managed
```

`materialize` must create a worktree from the explicit object ID and remove it in `finally`.

`snapshot_digest`, safe copy, and every snapshot commit/manifest use exclusion
policy version `1`. Exclude Git worktree metadata, KiCad lock files matching the
three exact suffixes above, and normalized cache/build paths explicitly listed
in the managed manifest's `snapshot_excludes` array. Initial adoption registers
an empty array. Reject leftover `.pcbflow-tmp-` files, links, reparse points,
sockets, devices, invalid registered exclusions, and paths escaping the root;
include every other regular project file, including `pcbflow.yaml`, in sorted
POSIX-relative path order with file mode, size, and raw SHA-256. The same
`is_snapshot_excluded` predicate must drive copy, digest, and Git indexing.

`commit_candidate` strictly loads `snapshot_excludes` from the managed
`pcbflow.yaml`, computes the snapshot digest, creates the deterministic
commit with `base_revision` as parent, and compare-and-swaps the caller-provided
candidate ref. `resolve_*` methods return revisions with the `git:` prefix.
`GitCli.update_ref` must first resolve the ref and return successfully when it
already equals `new_revision`; otherwise it performs the compare-and-swap
against `expected_revision`. This same-target replay rule is required after a
crash that created a requirement or proposal ref before SQLite was updated.
`list_refs` uses `git for-each-ref --format=%(refname)%00%(objectname)` and
returns only refs under the exact requested prefix, with object IDs normalized
to the `git:` form. `RevisionService.assert_clean` loads the exact
ProjectRevision, recomputes exclusion-policy-v1 `snapshot_digest` from the
materialized bytes and modes, and raises `ProjectWorktreeDirtyError` on any
mismatch before controlled edits begin. It does not trust Git status or a
moving ref as the integrity comparison.
`is_ancestor` calls `git merge-base --is-ancestor` with explicit object IDs and
maps exit codes 0/1 to true/false; any other exit is `GitOperationError`.

Wire one revision store and service in `build_container`:

```python
revision_store = ProjectRevisionStore(sessions)
git = GitCli(runner, timeout_seconds=settings.process_timeout_seconds)
workspace_copier = WorkspaceCopier(
    max_files=settings.max_project_files,
    max_bytes=settings.max_project_bytes,
)
revisions = RevisionService(
    projects=projects,
    revision_store=revision_store,
    git=git,
    copier=workspace_copier,
    projects_dir=settings.projects_dir,
    workspaces_dir=settings.workspaces_dir,
)
```

Store both `revision_store` and `revisions` on `Container`; later services
receive the same `revisions` instance.

Every Git invocation sets `GIT_CONFIG_NOSYSTEM=1`, points
`GIT_CONFIG_GLOBAL` and `core.attributesFile` at a platform null file, disables
`core.autocrlf`, and uses an empty platform-owned `core.hooksPath`. Never read
repository hooks or user/system Git configuration. Snapshot commits must hash
raw file bytes without clean/smudge filters; use `git hash-object -w
--no-filters` plus explicit index entries rather than `git add` for untrusted
trees.

- [ ] **Step 4: Run adoption and baseline tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_revisions.py tests/integration/test_managed_projects.py tests/integration/test_projects.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/pcbflow/workspaces.py src/pcbflow/revisions.py src/pcbflow/container.py tests/unit/test_revisions.py tests/integration/test_managed_projects.py
git commit -m "feat: adopt projects into managed git revisions"
```

---

### Task 6: Define strict requirement models and deterministic YAML views

**Files:**
- Create: `src/pcbflow/requirements.py`
- Create: `tests/unit/test_requirements.py`
- Create: `tests/fixtures/requirements/reference-controller.yaml`

**Interfaces:**
- Produces: `RequirementSetPayload`
- Produces: `RequirementSetStatus`
- Produces: `RequirementSet`
- Produces: `load_requirement_payload(data: bytes) -> RequirementSetPayload`
- Produces: `render_requirement_files(payload: RequirementSetPayload) -> dict[Path, bytes]`
- Produces: `load_rendered_requirement_files(files: Mapping[Path, bytes]) -> RequirementSetPayload`
- Produces: `requirement_digest(payload: RequirementSetPayload) -> str`
- Produces: `g1_subject_digest(project_id: str, requirement_set_id: str, requirements_digest: str, base_revision: str, candidate_revision: str, candidate_snapshot_digest: str) -> str`

- [ ] **Step 1: Write failing strict-schema and canonical-view tests**

```python
# tests/unit/test_requirements.py
from __future__ import annotations

import pytest
from pydantic import ValidationError

from pcbflow.canonical import canonical_json_bytes
from pcbflow.requirements import (
    RequirementSetPayload,
    g1_subject_digest,
    load_requirement_payload,
    load_rendered_requirement_files,
    render_requirement_files,
    requirement_digest,
)


VALID = b"""
schema_version: "1.0"
requirements:
  - id: REQ-FUNC-001
    kind: functional
    statement: The board shall expose one status LED.
    rationale: Local diagnostics.
    priority: must
    source: user
    verification_method: inspection
    acceptance_criteria: LED D1 is present in the schematic.
interfaces: []
power_rails: []
assumptions: []
verification_items:
  - id: VER-001
    requirement_ids: [REQ-FUNC-001]
    method: inspection
    acceptance_criteria: Semantic diff contains the verified LED module.
"""


def test_requirement_payload_is_strict_and_has_stable_digest() -> None:
    payload = load_requirement_payload(VALID)
    round_trip = RequirementSetPayload.model_validate_json(
        canonical_json_bytes(payload.model_dump(mode="json")), strict=True
    )
    assert requirement_digest(payload) == requirement_digest(round_trip)
    files = render_requirement_files(payload)
    assert set(files) == {
        Path("requirements/product.yaml"),
        Path("requirements/interfaces.yaml"),
        Path("requirements/power-tree.yaml"),
        Path("requirements/assumptions.yaml"),
        Path("requirements/verification.yaml"),
    }
    assert load_rendered_requirement_files(files) == payload


def test_requirement_payload_rejects_unknown_fields_and_duplicate_ids() -> None:
    with pytest.raises(ValidationError):
        load_requirement_payload(VALID.replace(b"source: user", b"source: user\n    x: 1"))

    duplicate = VALID.replace(
        b"interfaces: []",
        b"  - id: REQ-FUNC-001\n    kind: functional\n"
        b"    statement: Duplicate\n    rationale: Duplicate\n"
        b"    priority: must\n    source: user\n"
        b"    verification_method: inspection\n"
        b"    acceptance_criteria: duplicate\ninterfaces: []",
    )
    with pytest.raises(ValidationError):
        load_requirement_payload(duplicate)

    no_verification = VALID.split(b"verification_items:", 1)[0] + (
        b"verification_items: []\n"
    )
    with pytest.raises(ValidationError):
        load_requirement_payload(no_verification)


def test_g1_digest_changes_when_candidate_snapshot_changes() -> None:
    payload = load_requirement_payload(VALID)
    left = g1_subject_digest(
        "prj_1",
        "reqset_1",
        requirement_digest(payload),
        "git:" + "1" * 40,
        "git:" + "2" * 40,
        "sha256:" + "a" * 64,
    )
    right = g1_subject_digest(
        "prj_1",
        "reqset_1",
        requirement_digest(payload),
        "git:" + "1" * 40,
        "git:" + "2" * 40,
        "sha256:" + "b" * 64,
    )
    assert left != right
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_requirements.py -q
```

Expected: FAIL because requirement models do not exist.

- [ ] **Step 3: Implement strict models and renderers**

Use:

```python
class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class RequirementKind(StrEnum):
    FUNCTIONAL = "functional"
    INTERFACE = "interface"
    POWER = "power"
    ENVIRONMENT = "environment"
    MECHANICAL = "mechanical"
    MANUFACTURING = "manufacturing"
    COST = "cost"
    COMPLIANCE = "compliance"
    VERIFICATION = "verification"


class RequirementPriority(StrEnum):
    MUST = "must"
    SHOULD = "should"
    COULD = "could"


class RequirementSetStatus(StrEnum):
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    FROZEN = "frozen"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"
```

Define exact models:

```python
class Requirement(StrictModel):
    id: str = Field(pattern=r"^REQ-[A-Z0-9-]+$")
    kind: RequirementKind
    statement: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    priority: RequirementPriority
    source: str = Field(min_length=1)
    verification_method: str = Field(min_length=1)
    acceptance_criteria: str = Field(min_length=1)


class InterfaceDefinition(StrictModel):
    id: str = Field(pattern=r"^IF-[A-Z0-9-]+$")
    name: str = Field(min_length=1)
    direction: Literal["input", "output", "bidirectional", "passive"]
    nominal_voltage_v: float | None = Field(default=None, ge=0)
    absolute_max_voltage_v: float | None = Field(default=None, ge=0)


class PowerRail(StrictModel):
    id: str = Field(pattern=r"^PWR-[A-Z0-9-]+$")
    source: str = Field(min_length=1)
    minimum_current_a: float = Field(ge=0)
    typical_current_a: float = Field(ge=0)
    maximum_current_a: float = Field(ge=0)

    @model_validator(mode="after")
    def ordered_currents(self) -> PowerRail:
        if not (
            self.minimum_current_a
            <= self.typical_current_a
            <= self.maximum_current_a
        ):
            raise ValueError("power rail currents must be ordered")
        return self


class Assumption(StrictModel):
    id: str = Field(pattern=r"^ASM-[A-Z0-9-]+$")
    statement: str = Field(min_length=1)
    blocking: bool
    owner: str = Field(min_length=1)
    closure_condition: str = Field(min_length=1)


class VerificationItem(StrictModel):
    id: str = Field(pattern=r"^VER-[A-Z0-9-]+$")
    requirement_ids: tuple[str, ...] = Field(min_length=1)
    method: str = Field(min_length=1)
    acceptance_criteria: str = Field(min_length=1)


class RequirementSetPayload(StrictModel):
    schema_version: Literal["1.0"]
    requirements: tuple[Requirement, ...] = Field(min_length=1)
    interfaces: tuple[InterfaceDefinition, ...]
    power_rails: tuple[PowerRail, ...]
    assumptions: tuple[Assumption, ...]
    verification_items: tuple[VerificationItem, ...] = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class RequirementSet:
    id: str
    project_id: str
    base_revision: str
    schema_version: str
    status: RequirementSetStatus
    payload: RequirementSetPayload
    canonical_digest: str
    canonical_artifact_digest: str
    idempotency_key: str
    submission_idempotency_key: str | None
    candidate_revision: str | None
    candidate_snapshot_digest: str | None
    frozen_revision: str | None
    created_at: datetime
    submitted_at: datetime | None
    frozen_at: datetime | None

    def subject_digest(self) -> str:
        if self.candidate_revision is None or self.candidate_snapshot_digest is None:
            raise ValueError("requirement set has no G1 candidate")
        return g1_subject_digest(
            self.project_id,
            self.id,
            self.canonical_digest,
            self.base_revision,
            self.candidate_revision,
            self.candidate_snapshot_digest,
        )
```

Define the digest helper exactly:

```python
def g1_subject_digest(
    project_id: str,
    requirement_set_id: str,
    requirements_digest: str,
    base_revision: str,
    candidate_revision: str,
    candidate_snapshot_digest: str,
) -> str:
    return canonical_digest(
        {
            "schema_version": "1.0",
            "project_id": project_id,
            "requirement_set_id": requirement_set_id,
            "requirements_digest": requirements_digest,
            "base_revision": base_revision,
            "candidate_revision": candidate_revision,
            "candidate_snapshot_digest": candidate_snapshot_digest,
        }
    )
```

`RequirementSetPayload` must validate unique IDs, valid requirement references,
no blocking duplicate identifiers, and no non-finite values. Use
`yaml.safe_load`, then validate through
`RequirementSetPayload.model_validate_json(canonical_json_bytes(value),
strict=True)` so JSON arrays become immutable tuples without Python coercion.
Use `yaml.safe_dump(..., sort_keys=True, allow_unicode=True)` for views. Digest
only `payload.model_dump(mode="json")` through `canonical_digest`.
`load_rendered_requirement_files` requires exactly the five paths above,
safe-loads each mapping, rejects duplicate top-level sections, reconstructs
the six `RequirementSetPayload` fields, validates strictly, and is used to
prove every rendered candidate has the same `requirement_digest` before Git
commit. Import `Mapping` from `collections.abc` for its public signature.

- [ ] **Step 4: Run unit tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_requirements.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/pcbflow/requirements.py tests/unit/test_requirements.py tests/fixtures/requirements/reference-controller.yaml
git commit -m "feat: define strict requirement contracts"
```

---

### Task 7: Persist requirement sets and digest-bound gate decisions

**Files:**
- Create: `src/pcbflow/approvals.py`
- Create: `src/pcbflow/requirement_store.py`
- Create: `tests/integration/test_requirement_workflow.py`
- Modify: `tests/conftest.py`

**Interfaces:**
- Produces: `RequirementStore.create_draft(...) -> RequirementSet`
- Produces: `RequirementStore.get(requirement_set_id: str) -> RequirementSet`
- Produces: `RequirementStore.find_by_import_key(project_id: str, idempotency_key: str) -> RequirementSet | None`
- Produces: `RequirementStore.find_by_submission_key(project_id: str, idempotency_key: str) -> RequirementSet | None`
- Produces: `RequirementStore.mark_submitted(requirement_set_id: str, submission_idempotency_key: str, candidate_revision: str, candidate_snapshot_digest: str) -> RequirementSet`
- Produces: `RequirementStore.reject(...) -> RequirementSet`
- Produces: `RequirementStore.approve_and_freeze(...) -> RequirementSet`
- Produces: `GateDecisionStore.add(...) -> GateDecision`
- Produces: `GateDecision`
- Produces: `ApprovalDigestMismatchError`

- [ ] **Step 1: Write failing persistence tests**

```python
# tests/integration/test_requirement_workflow.py
from __future__ import annotations

import pytest

from pcbflow.approvals import ApprovalDigestMismatchError, GateDecisionStore
from pcbflow.requirement_store import RequirementStore
from pcbflow.requirements import RequirementSetStatus, load_requirement_payload


def test_requirement_draft_is_idempotent_and_submitted_content_is_immutable(
    session_factory, artifact_store, managed_project, requirement_yaml: bytes
) -> None:
    store = RequirementStore(session_factory, artifact_store)
    payload = load_requirement_payload(requirement_yaml)
    first = store.create_draft(
        managed_project.id,
        managed_project.current_revision,
        payload,
        "requirement-draft-1",
    )
    repeated = store.create_draft(
        managed_project.id,
        managed_project.current_revision,
        payload,
        "requirement-draft-1",
    )
    assert repeated.id == first.id

    submitted = store.mark_submitted(
        first.id,
        "requirement-submit-1",
        candidate_revision="git:" + "2" * 40,
        candidate_snapshot_digest="sha256:" + "b" * 64,
    )
    assert submitted.status is RequirementSetStatus.PENDING_APPROVAL

    with pytest.raises(ValueError, match="immutable"):
        store.replace_payload(submitted.id, payload)


def test_gate_decision_rejects_digest_reuse_with_different_subject(
    session_factory, managed_project
) -> None:
    decisions = GateDecisionStore(session_factory)
    decisions.add(
        project_id=managed_project.id,
        gate="G1",
        subject_type="requirement_set",
        subject_id="reqset_1",
        subject_digest="sha256:" + "a" * 64,
        base_revision=managed_project.current_revision,
        idempotency_key="approve-g1",
        decision="approve",
        actor_type="human",
        actor_id="local-user",
        comment="approved",
    )
    with pytest.raises(ApprovalDigestMismatchError):
        decisions.add(
            project_id=managed_project.id,
            gate="G1",
            subject_type="requirement_set",
            subject_id="reqset_1",
            subject_digest="sha256:" + "b" * 64,
            base_revision=managed_project.current_revision,
            idempotency_key="approve-g1",
            decision="approve",
            actor_type="human",
            actor_id="local-user",
            comment="approved",
        )
```

Append these reusable fixtures to `tests/conftest.py` and add the corresponding
imports for `Settings`, `build_container`, and `Project`:

```python
@pytest.fixture
def container(database_url: str, tmp_path: Path):
    settings = Settings.from_env(
        {
            "PCBFLOW_DATA_DIR": str(tmp_path / "service-data"),
            "PCBFLOW_DATABASE_URL": database_url,
        }
    )
    services = build_container(settings)
    try:
        yield services
    finally:
        services.dispose()


@pytest.fixture
def artifact_store(container):
    return container.artifacts


@pytest.fixture
def managed_project(container, tmp_path: Path) -> Project:
    source = tmp_path / "managed-source"
    source.mkdir()
    (source / "board.kicad_sch").write_bytes(b"(kicad_sch)\n")
    project = container.projects.create("Controller", source, "managed-project")
    return container.revisions.adopt(project.id, "adopt-managed-project")


@pytest.fixture
def requirement_yaml() -> bytes:
    fixture = (
        Path(__file__).parent
        / "fixtures"
        / "requirements"
        / "reference-controller.yaml"
    )
    return fixture.read_bytes()
```

- [ ] **Step 2: Run the test and verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration/test_requirement_workflow.py -q
```

Expected: FAIL because stores and decision models do not exist.

- [ ] **Step 3: Implement stores with full-input idempotency**

`RequirementStore.create_draft` must:

1. Compute canonical JSON bytes and digest without writing external state.
2. Read the existing `(project_id, idempotency_key)` row first. Return it when
   project, base revision, schema version, payload, and digest all match; raise
   `IdempotencyConflictError` before CAS writes when any canonical input differs.
3. Store canonical JSON bytes in Artifact Store only for a new key.
4. In one SQLite transaction, recheck the key for races, register the returned
   descriptor in `artifacts`, and insert `RequirementSetRow` with `draft`.
5. Return an immutable `RequirementSet`, or raise
   `IdempotencyConflictError` when the key is reused with different input.

`find_by_import_key` and `find_by_submission_key` return the current immutable
projection for their separate unique-key columns. They do not mutate state;
Task 8 uses them before current-revision validation so a completed operation can
be replayed after G1 advances the project.

Define:

```python
@dataclass(frozen=True, slots=True)
class GateDecision:
    id: str
    project_id: str
    gate: str
    subject_type: str
    subject_id: str
    subject_digest: str
    base_revision: str
    decision: str
    actor_type: str
    actor_id: str
    comment: str
    created_at: datetime
```

Use the `requirement_sets.idempotency_key` column and unique constraint created
in Task 3; do not add a second migration.

- [ ] **Step 4: Run persistence tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration/test_requirement_workflow.py tests/integration/test_design_migration.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/pcbflow/approvals.py src/pcbflow/requirement_store.py tests/conftest.py tests/integration/test_requirement_workflow.py
git commit -m "feat: persist requirements and gate decisions"
```

---

### Task 8: Submit and approve G1 requirement candidates

**Files:**
- Modify: `src/pcbflow/requirements.py`
- Modify: `src/pcbflow/requirement_store.py`
- Modify: `src/pcbflow/approvals.py`
- Modify: `src/pcbflow/revisions.py`
- Modify: `src/pcbflow/container.py`
- Modify: `tests/integration/test_requirement_workflow.py`
- Create: `tests/integration/test_reconciliation.py`

**Interfaces:**
- Produces: `RequirementService.import_draft(...) -> RequirementSet`
- Produces: `RequirementService.submit(requirement_set_id: str, idempotency_key: str) -> RequirementSet`
- Produces: `ApprovalService.decide_g1(...) -> RequirementSet`
- Produces: `RevisionReconciler.run_once() -> int`
- Produces: `RequirementsBlockedError(blocking_ids: tuple[str, ...])`.
- Guarantees: G1 approval advances Project revision and active requirement set atomically in SQLite.

- [ ] **Step 1: Write failing G1 workflow and recovery tests**

Append:

```python
from pathlib import Path


def _snapshot(root: Path) -> dict[Path, bytes]:
    return {
        path.relative_to(root): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_submit_and_approve_g1_advances_revision_without_touching_import_source(
    container, managed_project, requirement_yaml: bytes
) -> None:
    source_before = _snapshot(managed_project.source_path)
    draft = container.requirements.import_draft(
        managed_project.id,
        requirement_yaml,
        "requirements-import-1",
    )
    pending = container.requirements.submit(draft.id, "requirements-submit-1")
    assert pending.status is RequirementSetStatus.PENDING_APPROVAL
    assert pending.candidate_revision is not None

    subject_digest = pending.subject_digest()
    frozen = container.approvals.decide_g1(
        requirement_set_id=pending.id,
        subject_digest=subject_digest,
        decision="approve",
        actor_type="human",
        actor_id="local-user",
        comment="requirements accepted",
        idempotency_key="g1-approve-1",
    )
    repeated_submit = container.requirements.submit(
        draft.id, "requirements-submit-1"
    )
    repeated_import = container.requirements.import_draft(
        managed_project.id,
        requirement_yaml,
        "requirements-import-1",
    )
    project = container.projects.get(managed_project.id)
    assert frozen.status is RequirementSetStatus.FROZEN
    assert repeated_submit == frozen
    assert repeated_import == frozen
    assert frozen.frozen_revision == project.current_revision
    assert project.active_requirement_set_id == frozen.id
    assert _snapshot(managed_project.source_path) == source_before


def test_reject_g1_is_idempotent_and_does_not_advance_revision(
    container, managed_project, requirement_yaml: bytes
) -> None:
    source_before = _snapshot(managed_project.source_path)
    draft = container.requirements.import_draft(
        managed_project.id,
        requirement_yaml,
        "requirements-reject-import",
    )
    pending = container.requirements.submit(
        draft.id, "requirements-reject-submit"
    )
    rejected = container.approvals.decide_g1(
        requirement_set_id=pending.id,
        subject_digest=pending.subject_digest(),
        decision="reject",
        actor_type="human",
        actor_id="local-user",
        comment="requirements need revision",
        idempotency_key="g1-reject-1",
    )
    repeated = container.approvals.decide_g1(
        requirement_set_id=pending.id,
        subject_digest=pending.subject_digest(),
        decision="reject",
        actor_type="human",
        actor_id="local-user",
        comment="requirements need revision",
        idempotency_key="g1-reject-1",
    )

    project = container.projects.get(managed_project.id)
    assert rejected.status is RequirementSetStatus.REJECTED
    assert repeated.id == rejected.id
    assert project.current_revision == managed_project.current_revision
    assert project.active_requirement_set_id is None
    assert _snapshot(managed_project.source_path) == source_before


def test_submit_blocks_open_blocking_assumptions_before_creating_candidate(
    container, managed_project, requirement_yaml: bytes
) -> None:
    blocked_yaml = requirement_yaml.replace(
        b"assumptions: []",
        b"assumptions:\n"
        b"  - id: ASM-POWER-001\n"
        b"    statement: Input voltage is not confirmed.\n"
        b"    blocking: true\n"
        b"    owner: hardware-lead\n"
        b"    closure_condition: Confirm the input voltage range.\n",
    )
    draft = container.requirements.import_draft(
        managed_project.id, blocked_yaml, "requirements-blocked-import"
    )

    with pytest.raises(RequirementsBlockedError) as captured:
        container.requirements.submit(draft.id, "requirements-blocked-submit")

    assert captured.value.blocking_ids == ("ASM-POWER-001",)
    assert container.requirement_store.get(draft.id).status is RequirementSetStatus.DRAFT
    assert container.projects.get(managed_project.id).current_revision == (
        managed_project.current_revision
    )
```

Add `RequirementsBlockedError` to the imports from `pcbflow.requirements` in
this test file.

```python
# tests/integration/test_reconciliation.py
from __future__ import annotations

import pytest


@pytest.fixture
def approved_requirement_set(container, managed_project, requirement_yaml: bytes):
    draft = container.requirements.import_draft(
        managed_project.id,
        requirement_yaml,
        "reconciliation-requirements",
    )
    pending = container.requirements.submit(
        draft.id, "reconciliation-requirements-submit"
    )
    return container.approvals.decide_g1(
        requirement_set_id=pending.id,
        subject_digest=pending.subject_digest(),
        decision="approve",
        actor_type="human",
        actor_id="local-user",
        comment="approved for reconciliation",
        idempotency_key="reconciliation-g1",
    )


def test_reconciler_repairs_design_ref_from_database_revision(
    container, approved_requirement_set
) -> None:
    project = container.projects.get(approved_requirement_set.project_id)
    container.revisions.git.update_ref(
        container.revisions.repo_path(project.id),
        "refs/heads/design",
        approved_requirement_set.base_revision,
        expected_revision=project.current_revision,
    )

    assert container.reconciler.run_once() == 1
    assert container.revisions.resolve_design_ref(project.id) == (
        project.current_revision
    )
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration/test_requirement_workflow.py tests/integration/test_reconciliation.py -q
```

Expected: FAIL because services and reconciler do not exist.

- [ ] **Step 3: Implement G1 candidate creation and approval**

Define the stable blocking error in `requirements.py`:

```python
class RequirementsBlockedError(ValueError):
    def __init__(self, blocking_ids: tuple[str, ...]) -> None:
        super().__init__("blocking requirements assumptions remain open")
        self.blocking_ids = blocking_ids
```

`RequirementService.import_draft(project_id, data, idempotency_key)` parses the
strict payload first, then checks the import key before reading current project
state:

```python
payload = load_requirement_payload(data)
existing = self._store.find_by_import_key(project_id, idempotency_key)
if existing is not None:
    if existing.canonical_digest != requirement_digest(payload):
        raise IdempotencyConflictError(idempotency_key)
    return existing
project = self._projects.get(project_id)
if project.mode is not ProjectMode.MANAGED:
    raise ProjectNotManagedError(project.id)
return self._store.create_draft(
    project.id,
    project.current_revision,
    payload,
    idempotency_key,
)
```

`RequirementService.submit(requirement_set_id, idempotency_key)`:

```python
requirement_set = self._store.get(requirement_set_id)
existing = self._store.find_by_submission_key(
    requirement_set.project_id, idempotency_key
)
if existing is not None:
    if existing.id != requirement_set.id:
        raise IdempotencyConflictError(idempotency_key)
    return existing
if requirement_set.submission_idempotency_key is not None:
    raise IdempotencyConflictError(idempotency_key)
project = self._projects.get(requirement_set.project_id)
blocking_ids = tuple(
    sorted(item.id for item in requirement_set.payload.assumptions if item.blocking)
)
if blocking_ids:
    raise RequirementsBlockedError(blocking_ids)
if project.current_revision != requirement_set.base_revision:
    raise RevisionConflictError(
        requirement_set.base_revision, project.current_revision
    )
files = render_requirement_files(requirement_set.payload)
with self._revisions.materialize(
    project.id, project.current_revision, f"requirements-{requirement_set.id}"
) as workspace:
    for relative, data in files.items():
        target = workspace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    written = {
        relative: (workspace / relative).read_bytes()
        for relative in files
    }
    round_trip = load_rendered_requirement_files(written)
    if requirement_digest(round_trip) != requirement_set.canonical_digest:
        raise RuntimeError("rendered requirement digest mismatch")
    self._write_manifest_candidate(workspace, requirement_set)
    candidate = self._revisions.commit_candidate(
        project=project,
        workspace=workspace,
        base_revision=project.current_revision,
        ref=f"refs/pcbflow/requirements/{requirement_set.id}",
        message=f"pcbflow: submit requirements {requirement_set.id}",
        timestamp=requirement_set.created_at,
    )
return self._store.mark_submitted(
    requirement_set.id,
    idempotency_key,
    candidate.revision,
    candidate.snapshot_digest,
)
```

`_write_manifest_candidate` writes deterministic YAML containing only
`schema_version`, `project_id`, `mode`, and a `requirement_candidate` mapping
with requirement-set ID, canonical digest, and base revision. It must not write
`current_revision`, candidate revision, timestamps, absolute paths, or any
self-referential value into the commit being hashed.

`mark_submitted` sets `submission_idempotency_key` in the same transition.
When the set is already pending with the same key, candidate revision, and
snapshot digest, return it unchanged. Reuse of the same project submission key
for another set or different candidate raises `IdempotencyConflictError`.
`RequirementService` performs the early lookup above, so the same submission
key returns the set's current pending/frozen/rejected projection without
materializing a worktree or comparing a now-advanced project revision.

`ApprovalService.decide_g1` must recompute `subject_digest`, reject mismatches, and call one store transaction that:

1. Compares project version and base revision.
2. Supersedes the previous active set.
3. Freezes the new set.
4. Advances Project current revision and active set.
5. Inserts ProjectRevision and GateDecision.
6. Inserts outbox event `project.revision.accepted`.

For `decision="reject"`, use one transaction that appends the reject
GateDecision and audit outbox event, changes only the RequirementSet from
`pending_approval` to `rejected`, and leaves Project, ProjectRevision, active
requirement set, and design ref unchanged.

For G1 idempotency, compare only canonical request fields: requirement set,
subject digest, decision, actor, comment, base revision, and idempotency key.
Generate `created_at` and approval artifact bytes only after proving the key is
new. A replay with the same request returns the stored decision/frozen set and
must not compare a newly generated timestamp or create another artifact.

After commit, call the reconciler. A reconciler failure must not roll back the database decision.

- [ ] **Step 4: Run G1 and baseline tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration/test_requirement_workflow.py tests/integration/test_reconciliation.py tests/e2e/test_api_cli.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/pcbflow/requirements.py src/pcbflow/requirement_store.py src/pcbflow/approvals.py src/pcbflow/revisions.py src/pcbflow/container.py tests/integration/test_requirement_workflow.py tests/integration/test_reconciliation.py
git commit -m "feat: freeze requirements through g1 revisions"
```

---

### Task 9: Define the strict DesignCommand contract and preconditions

**Files:**
- Create: `src/pcbflow/commands.py`
- Create: `tests/unit/test_commands.py`

**Interfaces:**
- Produces: `Actor`, `RiskLevel`, `ValidationKind`, `SchematicObjectRef`
- Produces: `DesignCommand`, `CommandBatch`, and the four supported operation payloads.
- Produces: `load_command_batch(data: bytes) -> CommandBatch`
- Produces: `command_batch_digest(batch: CommandBatch) -> str`
- Produces: `evaluate_precondition(precondition: Precondition, context: PreconditionContext) -> PreconditionResult`
- Produces: `DesignCommandSchemaError`

- [ ] **Step 1: Write failing strict-schema, invariant, digest, and precondition tests**

```python
# tests/unit/test_commands.py
from __future__ import annotations

import json

import pytest

from pcbflow.commands import (
    DesignCommandSchemaError,
    PreconditionState,
    ProjectRevisionEquals,
    ToolCapabilityAvailable,
    command_batch_digest,
    evaluate_precondition,
    load_command_batch,
)


def _batch() -> dict[str, object]:
    actor = {"type": "human", "id": "local-user"}
    command = {
        "schema_version": "1.0",
        "command_id": "cmd_status_led",
        "batch_id": "bat_status_led",
        "project_id": "prj_controller",
        "base_revision": "git:" + "1" * 40,
        "idempotency_key": "controller:r1:status-led:1",
        "actor": actor,
        "intent": "Instantiate the verified status LED module",
        "risk": "medium",
        "preconditions": [
            {
                "type": "project.revision_equals",
                "revision": "git:" + "1" * 40,
            },
            {
                "type": "tool.capability_available",
                "capability": "kicad.cst.write.v1",
            },
        ],
        "operation": {
            "type": "schematic.instantiate_module",
            "payload": {
                "module_revision_id": "modrev_status_led_v1",
                "instance_name": "STATUS_LED",
                "target_sheet_ref": {
                    "kind": "sheet",
                    "sheet_uuid": "00000000-0000-0000-0000-000000000001",
                    "object_uuid": "00000000-0000-0000-0000-000000000001",
                    "pin_number": None,
                },
                "parameter_bindings": {"LED_VALUE": "GREEN"},
                "port_bindings": {},
                "placement_slot": "auto",
            },
        },
        "required_validations": ["semantic_diff", "kicad_erc"],
        "provenance": {
            "requirement_ids": ["REQ-FUNC-001"],
            "evidence_ids": [],
            "module_revision_ids": ["modrev_status_led_v1"],
        },
    }
    return {
        "schema_version": "1.0",
        "batch_id": "bat_status_led",
        "project_id": "prj_controller",
        "base_revision": "git:" + "1" * 40,
        "requirement_set_id": "reqset_controller_v1",
        "idempotency_key": "controller:r1:status-led",
        "actor": actor,
        "intent": "Add a verified status LED",
        "risk": "medium",
        "commands": [command],
    }


def _load(value: dict[str, object]):
    return load_command_batch(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    )


def test_batch_is_strict_and_rejects_unknown_fields() -> None:
    value = _batch()
    value["unknown"] = True
    with pytest.raises(DesignCommandSchemaError):
        _load(value)


def test_batch_rejects_inner_identity_or_actor_mismatch() -> None:
    value = _batch()
    value["commands"][0]["project_id"] = "prj_other"  # type: ignore[index]
    with pytest.raises(DesignCommandSchemaError, match="project_id"):
        _load(value)


def test_command_order_changes_the_batch_digest() -> None:
    left = _batch()
    right = _batch()
    second = dict(right["commands"][0])  # type: ignore[index]
    second["command_id"] = "cmd_status_led_property"
    second["idempotency_key"] = "controller:r1:status-led:2"
    second["operation"] = {
        "type": "schematic.set_property",
        "payload": {
            "subject_ref": {
                "kind": "symbol",
                "sheet_uuid": "00000000-0000-0000-0000-000000000001",
                "object_uuid": "00000000-0000-0000-0000-000000000002",
                "pin_number": None,
            },
            "property_name": "Value",
            "value": "GREEN",
            "expected_old_value": "LED",
        },
    }
    right["commands"] = [second, right["commands"][0]]  # type: ignore[index]
    left["commands"] = [left["commands"][0], second]  # type: ignore[index]
    assert command_batch_digest(_load(left)) != command_batch_digest(_load(right))


class _Context:
    current_revision = "git:" + "1" * 40
    requirements_digest = "sha256:" + "a" * 64

    def has_object(self, _subject_ref):
        return None

    def property_value(self, _subject_ref, _name):
        return None

    def has_module(self, _instance_name):
        return None

    def has_capability(self, capability: str):
        return capability == "kicad.cst.write.v1"


def test_preconditions_distinguish_true_false_and_unknown() -> None:
    context = _Context()
    revision = ProjectRevisionEquals(
        type="project.revision_equals", revision=context.current_revision
    )
    capability = ToolCapabilityAvailable(
        type="tool.capability_available",
        capability="kicad.cst.write.v1",
    )
    missing = ToolCapabilityAvailable(
        type="tool.capability_available", capability="kicad.ipc.write.v1"
    )
    assert evaluate_precondition(revision, context).state is PreconditionState.TRUE
    assert evaluate_precondition(capability, context).state is PreconditionState.TRUE
    assert evaluate_precondition(missing, context).state is PreconditionState.FALSE
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_commands.py -q
```

Expected: FAIL because `pcbflow.commands` does not exist.

- [ ] **Step 3: Implement the exact command models and evaluator**

Create `src/pcbflow/commands.py` with strict, frozen Pydantic models. Use these
public types and discriminators:

```python
from __future__ import annotations

import json
from enum import StrEnum
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from pcbflow.canonical import canonical_digest, canonical_json_bytes


class StrictCommandModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Actor(StrictCommandModel):
    type: Literal["human", "service"]
    id: str = Field(min_length=1, max_length=255)


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ValidationKind(StrEnum):
    SCHEMA = "schema"
    PRECONDITIONS = "preconditions"
    PATH_LIMITS = "path_limits"
    POST_WRITE_PARSE = "post_write_parse"
    SEMANTIC_DIFF = "semantic_diff"
    KICAD_ERC = "kicad_erc"


class SchematicObjectRef(StrictCommandModel):
    kind: Literal[
        "sheet",
        "symbol",
        "pin",
        "hierarchical_port",
        "label",
        "net",
        "wire_endpoint",
    ]
    sheet_uuid: str = Field(min_length=1)
    object_uuid: str = Field(min_length=1)
    pin_number: str | None


class ProjectRevisionEquals(StrictCommandModel):
    type: Literal["project.revision_equals"]
    revision: str = Field(pattern=r"^git:[0-9a-f]{40,64}$")


class RequirementsDigestEquals(StrictCommandModel):
    type: Literal["requirements.digest_equals"]
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class SchematicObjectExists(StrictCommandModel):
    type: Literal["schematic.object_exists"]
    subject_ref: SchematicObjectRef


class SchematicPropertyEquals(StrictCommandModel):
    type: Literal["schematic.property_equals"]
    subject_ref: SchematicObjectRef
    property_name: str = Field(min_length=1)
    value: str


class SchematicModuleAbsent(StrictCommandModel):
    type: Literal["schematic.module_absent"]
    instance_name: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,63}$")


class ToolCapabilityAvailable(StrictCommandModel):
    type: Literal["tool.capability_available"]
    capability: str = Field(min_length=1)


Precondition = Annotated[
    ProjectRevisionEquals
    | RequirementsDigestEquals
    | SchematicObjectExists
    | SchematicPropertyEquals
    | SchematicModuleAbsent
    | ToolCapabilityAvailable,
    Field(discriminator="type"),
]


class InstantiateModulePayload(StrictCommandModel):
    module_revision_id: str = Field(pattern=r"^modrev_[A-Za-z0-9_-]+$")
    instance_name: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,63}$")
    target_sheet_ref: SchematicObjectRef
    parameter_bindings: dict[str, str]
    port_bindings: dict[str, SchematicObjectRef]
    placement_slot: str = Field(min_length=1)


class SetPropertyPayload(StrictCommandModel):
    subject_ref: SchematicObjectRef
    property_name: str = Field(min_length=1)
    value: str
    expected_old_value: str | None


class AssignFootprintPayload(StrictCommandModel):
    subject_ref: SchematicObjectRef
    footprint_revision_id: str = Field(pattern=r"^fprev_[A-Za-z0-9_-]+$")


class AddLabelPayload(StrictCommandModel):
    target_ref: SchematicObjectRef
    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_./+-]{0,126}$")
    scope: Literal["local", "global", "hierarchical"]


class InstantiateModuleOperation(StrictCommandModel):
    type: Literal["schematic.instantiate_module"]
    payload: InstantiateModulePayload


class SetPropertyOperation(StrictCommandModel):
    type: Literal["schematic.set_property"]
    payload: SetPropertyPayload


class AssignFootprintOperation(StrictCommandModel):
    type: Literal["schematic.assign_footprint"]
    payload: AssignFootprintPayload


class AddLabelOperation(StrictCommandModel):
    type: Literal["schematic.add_label"]
    payload: AddLabelPayload


DesignOperation = Annotated[
    InstantiateModuleOperation
    | SetPropertyOperation
    | AssignFootprintOperation
    | AddLabelOperation,
    Field(discriminator="type"),
]


class Provenance(StrictCommandModel):
    requirement_ids: tuple[str, ...] = Field(min_length=1)
    evidence_ids: tuple[str, ...]
    module_revision_ids: tuple[str, ...]


class DesignCommand(StrictCommandModel):
    schema_version: Literal["1.0"]
    command_id: str = Field(pattern=r"^cmd_[A-Za-z0-9_-]+$")
    batch_id: str = Field(pattern=r"^bat_[A-Za-z0-9_-]+$")
    project_id: str = Field(pattern=r"^prj_[A-Za-z0-9_-]+$")
    base_revision: str = Field(pattern=r"^git:[0-9a-f]{40,64}$")
    idempotency_key: str = Field(min_length=1, max_length=255)
    actor: Actor
    intent: str = Field(min_length=1)
    risk: RiskLevel
    preconditions: tuple[Precondition, ...]
    operation: DesignOperation
    required_validations: tuple[ValidationKind, ...]
    provenance: Provenance

    @model_validator(mode="after")
    def unique_validations(self) -> DesignCommand:
        if len(set(self.required_validations)) != len(self.required_validations):
            raise ValueError("required_validations must be unique")
        return self


class CommandBatch(StrictCommandModel):
    schema_version: Literal["1.0"]
    batch_id: str = Field(pattern=r"^bat_[A-Za-z0-9_-]+$")
    project_id: str = Field(pattern=r"^prj_[A-Za-z0-9_-]+$")
    base_revision: str = Field(pattern=r"^git:[0-9a-f]{40,64}$")
    requirement_set_id: str = Field(pattern=r"^reqset_[A-Za-z0-9_-]+$")
    idempotency_key: str = Field(min_length=1, max_length=255)
    actor: Actor
    intent: str = Field(min_length=1)
    risk: RiskLevel
    commands: tuple[DesignCommand, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def command_envelope_matches(self) -> CommandBatch:
        for command in self.commands:
            for field in ("batch_id", "project_id", "base_revision", "actor"):
                if getattr(command, field) != getattr(self, field):
                    raise ValueError(f"command {field} does not match batch")
        command_ids = [command.command_id for command in self.commands]
        keys = [command.idempotency_key for command in self.commands]
        if len(set(command_ids)) != len(command_ids):
            raise ValueError("command_id must be unique inside a batch")
        if len(set(keys)) != len(keys):
            raise ValueError("command idempotency_key must be unique")
        return self


class DesignCommandSchemaError(ValueError):
    pass


def load_command_batch(data: bytes) -> CommandBatch:
    try:
        value = json.loads(
            data.decode("utf-8"),
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON number: {value}")
            ),
        )
        return CommandBatch.model_validate_json(
            canonical_json_bytes(value), strict=True
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValidationError,
        ValueError,
    ) as error:
        raise DesignCommandSchemaError(str(error)) from error


def command_batch_digest(batch: CommandBatch) -> str:
    return canonical_digest(batch.model_dump(mode="json"))
```

Add `PreconditionState`, `PreconditionResult`, `PreconditionContext`, and an
exhaustive `match`-based evaluator. `None` from a context lookup means
`unknown`; false and unknown are both blocking to the executor:

```python
class PreconditionState(StrEnum):
    TRUE = "true"
    FALSE = "false"
    UNKNOWN = "unknown"


class PreconditionResult(StrictCommandModel):
    type: str
    state: PreconditionState
    subject_ref: SchematicObjectRef | None
    actual: object | None


class PreconditionContext(Protocol):
    current_revision: str
    requirements_digest: str

    def has_object(self, subject_ref: SchematicObjectRef) -> bool | None: ...
    def property_value(
        self, subject_ref: SchematicObjectRef, name: str
    ) -> str | None: ...
    def has_module(self, instance_name: str) -> bool | None: ...
    def has_capability(self, capability: str) -> bool | None: ...


def _state(value: bool | None) -> PreconditionState:
    if value is None:
        return PreconditionState.UNKNOWN
    return PreconditionState.TRUE if value else PreconditionState.FALSE


def evaluate_precondition(
    precondition: Precondition, context: PreconditionContext
) -> PreconditionResult:
    subject_ref = getattr(precondition, "subject_ref", None)
    match precondition:
        case ProjectRevisionEquals(revision=revision):
            actual: object | None = context.current_revision
            state = _state(actual == revision)
        case RequirementsDigestEquals(digest=digest):
            actual = context.requirements_digest
            state = _state(actual == digest)
        case SchematicObjectExists(subject_ref=reference):
            actual = context.has_object(reference)
            state = _state(actual)
        case SchematicPropertyEquals(
            subject_ref=reference, property_name=name, value=expected
        ):
            actual = context.property_value(reference, name)
            state = (
                PreconditionState.UNKNOWN
                if actual is None
                else _state(actual == expected)
            )
        case SchematicModuleAbsent(instance_name=instance_name):
            actual = context.has_module(instance_name)
            state = (
                PreconditionState.UNKNOWN
                if actual is None
                else _state(not actual)
            )
        case ToolCapabilityAvailable(capability=capability):
            actual = context.has_capability(capability)
            state = _state(actual)
        case _:
            raise AssertionError("unreachable precondition variant")
    return PreconditionResult(
        type=precondition.type,
        state=state,
        subject_ref=subject_ref,
        actual=actual,
    )
```

- [ ] **Step 4: Run focused unit tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_commands.py tests/unit/test_canonical.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/pcbflow/commands.py tests/unit/test_commands.py
git commit -m "feat: define strict design command contracts"
```

---

### Task 10: Persist command batches and atomically queue one proposal

**Files:**
- Create: `src/pcbflow/proposals.py`
- Create: `src/pcbflow/proposal_store.py`
- Create: `tests/integration/test_command_batches.py`
- Modify: `src/pcbflow/container.py`
- Modify: `tests/conftest.py`

**Interfaces:**
- Produces: `ProposalStatus` and immutable `ChangeProposal`.
- Produces: `CommandBatchStore.get(batch_id: str) -> CommandBatch`
- Produces: `ProposalStore.get(proposal_id: str) -> ChangeProposal`
- Produces: `ProposalStore.find_existing(batch: CommandBatch) -> ChangeProposal | None`
- Produces: `ProposalStore.create_queued(batch: CommandBatch) -> ChangeProposal`
- Produces: `ProposalService.create(data: bytes, idempotency_key: str) -> ChangeProposal`
- Produces: `DESIGN_PROPOSAL_TASK_KIND = "design.execute_proposal"`
- Guarantees: batch rows, command rows, task, proposal, and outbox event are inserted in one SQLite transaction.

- [ ] **Step 1: Write failing atomic-persistence and idempotency tests**

```python
# tests/integration/test_command_batches.py
from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select

from pcbflow.design_tables import (
    ChangeProposalRow,
    DesignCommandBatchRow,
    DesignCommandRow,
    OutboxEventRow,
)
from pcbflow.domain import TaskStatus
from pcbflow.repositories import IdempotencyConflictError
from pcbflow.tables import TaskRow


def _batch(project, requirement_set) -> dict[str, object]:
    actor = {"type": "human", "id": "local-user"}
    return {
        "schema_version": "1.0",
        "batch_id": "bat_set_status_value",
        "project_id": project.id,
        "base_revision": project.current_revision,
        "requirement_set_id": requirement_set.id,
        "idempotency_key": "proposal:set-status-value",
        "actor": actor,
        "intent": "Set the status indicator value",
        "risk": "low",
        "commands": [
            {
                "schema_version": "1.0",
                "command_id": "cmd_set_status_value",
                "batch_id": "bat_set_status_value",
                "project_id": project.id,
                "base_revision": project.current_revision,
                "idempotency_key": "proposal:set-status-value:1",
                "actor": actor,
                "intent": "Set the status indicator value",
                "risk": "low",
                "preconditions": [
                    {
                        "type": "project.revision_equals",
                        "revision": project.current_revision,
                    }
                ],
                "operation": {
                    "type": "schematic.set_property",
                    "payload": {
                        "subject_ref": {
                            "kind": "symbol",
                            "sheet_uuid": "00000000-0000-0000-0000-000000000001",
                            "object_uuid": "00000000-0000-0000-0000-000000000002",
                            "pin_number": None,
                        },
                        "property_name": "Value",
                        "value": "GREEN",
                        "expected_old_value": "LED",
                    },
                },
                "required_validations": ["semantic_diff", "kicad_erc"],
                "provenance": {
                    "requirement_ids": ["REQ-FUNC-001"],
                    "evidence_ids": [],
                    "module_revision_ids": [],
                },
            }
        ],
    }


def test_create_persists_batch_commands_task_proposal_and_outbox_once(
    container, frozen_requirement_set
) -> None:
    project = container.projects.get(frozen_requirement_set.project_id)
    value = _batch(project, frozen_requirement_set)
    data = json.dumps(value, separators=(",", ":")).encode()

    first = container.proposals.create(data, value["idempotency_key"])
    repeated = container.proposals.create(data, value["idempotency_key"])

    assert repeated.id == first.id
    task = container.tasks.get(first.task_id)
    assert task.status is TaskStatus.QUEUED
    assert task.payload == {
        "proposal_id": first.id,
        "command_batch_id": first.command_batch_id,
        "project_id": first.project_id,
    }
    with container.sessions() as session:
        assert session.scalar(select(func.count()).select_from(DesignCommandBatchRow)) == 1
        assert session.scalar(select(func.count()).select_from(DesignCommandRow)) == 1
        assert session.scalar(select(func.count()).select_from(ChangeProposalRow)) == 1
        assert session.scalar(select(func.count()).select_from(TaskRow)) >= 1
        assert session.scalar(select(func.count()).select_from(OutboxEventRow)) >= 1


def test_create_rejects_same_key_with_different_canonical_input(
    container, frozen_requirement_set
) -> None:
    project = container.projects.get(frozen_requirement_set.project_id)
    value = _batch(project, frozen_requirement_set)
    key = str(value["idempotency_key"])
    container.proposals.create(json.dumps(value).encode(), key)
    value["intent"] = "A different intent"

    with pytest.raises(IdempotencyConflictError):
        container.proposals.create(json.dumps(value).encode(), key)
```

Append the shared frozen-set fixture to `tests/conftest.py` so Tasks 10, 16,
and later API tests use one exact setup:

```python
@pytest.fixture
def frozen_requirement_set(container, managed_project, requirement_yaml: bytes):
    draft = container.requirements.import_draft(
        managed_project.id, requirement_yaml, "command-test-requirements"
    )
    pending = container.requirements.submit(draft.id, "command-test-submit")
    return container.approvals.decide_g1(
        requirement_set_id=pending.id,
        subject_digest=pending.subject_digest(),
        decision="approve",
        actor_type="human",
        actor_id="local-user",
        comment="approved",
        idempotency_key="command-test-g1",
    )
```

- [ ] **Step 2: Run the integration test and verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration/test_command_batches.py -q
```

Expected: FAIL because proposal domain objects, stores, and container wiring do
not exist.

- [ ] **Step 3: Implement the proposal domain and one-transaction queue path**

Create these domain values in `src/pcbflow/proposals.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from pcbflow.commands import CommandBatch, load_command_batch
from pcbflow.domain import ProjectMode
from pcbflow.repositories import IdempotencyConflictError, ProjectRepository
from pcbflow.requirement_store import RequirementStore
from pcbflow.requirements import RequirementSetStatus

DESIGN_PROPOSAL_TASK_KIND = "design.execute_proposal"


class ProposalStatus(StrEnum):
    QUEUED = "queued"
    EXECUTING = "executing"
    VALIDATION_FAILED = "validation_failed"
    READY_FOR_REVIEW = "ready_for_review"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class ChangeProposal:
    id: str
    project_id: str
    command_batch_id: str
    task_id: str
    status: ProposalStatus
    candidate_revision: str | None
    candidate_snapshot_digest: str | None
    review_digest: str | None
    semantic_diff_digest: str | None
    evidence_set_digest: str | None
    result: dict[str, object] | None
    last_error_code: str | None
    created_at: datetime
    updated_at: datetime
    version: int


class ProjectNotManagedError(RuntimeError):
    pass


class ProposalService:
    def __init__(
        self,
        projects: ProjectRepository,
        requirements: RequirementStore,
        store: ProposalStore,
    ) -> None:
        self._projects = projects
        self._requirements = requirements
        self._store = store

    def create(self, data: bytes, idempotency_key: str) -> ChangeProposal:
        batch = load_command_batch(data)
        if batch.idempotency_key != idempotency_key:
            raise IdempotencyConflictError(idempotency_key)
        existing = self._store.find_existing(batch)
        if existing is not None:
            return existing
        project = self._projects.get(batch.project_id)
        if project.mode is not ProjectMode.MANAGED:
            raise ProjectNotManagedError(project.id)
        if project.current_revision != batch.base_revision:
            raise RevisionConflictError(batch.base_revision, project.current_revision)
        requirement_set = self._requirements.get(batch.requirement_set_id)
        if (
            requirement_set.project_id != project.id
            or requirement_set.status is not RequirementSetStatus.FROZEN
            or project.active_requirement_set_id != requirement_set.id
        ):
            raise ValueError("batch must use the active frozen requirement set")
        return self._store.create_queued(batch)
```

Import `ProposalStore` and `RevisionConflictError` without introducing a
runtime import cycle. Put all SQLAlchemy work in `proposal_store.py`, not in the
service. The transaction must use the input `batch_id` and `command_id` values
as row IDs and perform these exact writes:

```python
def find_existing(self, batch: CommandBatch) -> ChangeProposal | None:
    batch_json = batch.model_dump(mode="json")
    digest = command_batch_digest(batch)
    with self._sessions() as session:
        existing = session.scalar(
            select(DesignCommandBatchRow).where(
                DesignCommandBatchRow.project_id == batch.project_id,
                DesignCommandBatchRow.idempotency_key == batch.idempotency_key,
            )
        )
        if existing is None:
            return None
        if (
            existing.canonical_digest != digest
            or existing.commands_json != batch_json["commands"]
        ):
            raise IdempotencyConflictError(batch.idempotency_key)
        proposal = session.scalar(
            select(ChangeProposalRow).where(
                ChangeProposalRow.command_batch_id == existing.id
            )
        )
        if proposal is None:
            raise RuntimeError("command batch exists without proposal")
        return _proposal(proposal)


def create_queued(self, batch: CommandBatch) -> ChangeProposal:
    batch_json = batch.model_dump(mode="json")
    digest = command_batch_digest(batch)
    with self._sessions.begin() as session:
        existing = session.scalar(
            select(DesignCommandBatchRow).where(
                DesignCommandBatchRow.project_id == batch.project_id,
                DesignCommandBatchRow.idempotency_key == batch.idempotency_key,
            )
        )
        if existing is not None:
            if (
                existing.canonical_digest != digest
                or existing.commands_json != batch_json["commands"]
            ):
                raise IdempotencyConflictError(batch.idempotency_key)
            proposal = session.scalar(
                select(ChangeProposalRow).where(
                    ChangeProposalRow.command_batch_id == existing.id
                )
            )
            if proposal is None:
                raise RuntimeError("command batch exists without proposal")
            return _proposal(proposal)

        now = self._clock()
        batch_row = DesignCommandBatchRow(
            id=batch.batch_id,
            project_id=batch.project_id,
            requirement_set_id=batch.requirement_set_id,
            base_revision=batch.base_revision,
            idempotency_key=batch.idempotency_key,
            actor_json=batch.actor.model_dump(mode="json"),
            intent=batch.intent,
            risk=batch.risk.value,
            commands_json=batch_json["commands"],
            canonical_digest=digest,
            created_at=now,
        )
        session.add(batch_row)
        for ordinal, command in enumerate(batch.commands):
            command_json = command.model_dump(mode="json")
            session.add(
                DesignCommandRow(
                    id=command.command_id,
                    batch_id=batch.batch_id,
                    project_id=batch.project_id,
                    ordinal=ordinal,
                    idempotency_key=command.idempotency_key,
                    operation_type=command.operation.type,
                    payload_json=command_json,
                    canonical_digest=canonical_digest(command_json),
                )
            )

        task_id = new_id("tsk")
        proposal_id = new_id("prop")
        session.add(
            TaskRow(
                id=task_id,
                project_id=batch.project_id,
                kind=DESIGN_PROPOSAL_TASK_KIND,
                payload_json={
                    "proposal_id": proposal_id,
                    "command_batch_id": batch.batch_id,
                    "project_id": batch.project_id,
                },
                result_json=None,
                status=TaskStatus.QUEUED.value,
                idempotency_key=(
                    f"proposal:{batch.project_id}:{batch.idempotency_key}"
                ),
                lease_owner=None,
                lease_token=None,
                lease_expires_at=None,
                last_error_code=None,
                attempt_count=0,
                created_at=now,
                updated_at=now,
                version=1,
            )
        )
        proposal = ChangeProposalRow(
            id=proposal_id,
            project_id=batch.project_id,
            command_batch_id=batch.batch_id,
            task_id=task_id,
            status=ProposalStatus.QUEUED.value,
            candidate_revision=None,
            candidate_snapshot_digest=None,
            review_digest=None,
            semantic_diff_digest=None,
            evidence_set_digest=None,
            result_json=None,
            last_error_code=None,
            created_at=now,
            updated_at=now,
            version=1,
        )
        session.add(proposal)
        session.add(
            OutboxEventRow(
                id=new_id("evt"),
                aggregate_type="change_proposal",
                aggregate_id=proposal_id,
                event_type="proposal.queued",
                payload_json={
                    "project_id": batch.project_id,
                    "command_batch_id": batch.batch_id,
                    "task_id": task_id,
                },
                created_at=now,
                processed_at=None,
                attempt_count=0,
                last_error_code=None,
            )
        )
        session.flush()
        return _proposal(proposal)
```

`find_existing` performs the identical canonical digest and `commands_json`
comparison in a read-only transaction and returns the existing proposal or
`None`. It raises `IdempotencyConflictError` for the same project/key with any
different canonical batch field. `create_queued` repeats the check under its
write transaction for races, but calls `clock()` only after proving the key is
new. The service therefore returns an accepted/rejected/stale existing proposal
before comparing the project's now-advanced revision or active requirement
state.

Also implement `CommandBatchStore.get` by validating the immutable
`commands_json` back through `CommandBatch` and comparing the recomputed
digest. Implement `ProposalStore.get` with a stable `ProposalNotFoundError`.
Wire `command_batches`, `proposal_store`, and `proposals` into `Container`.

- [ ] **Step 4: Run command persistence and G1 regressions**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration/test_command_batches.py tests/integration/test_requirement_workflow.py tests/integration/test_tasks.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/pcbflow/proposals.py src/pcbflow/proposal_store.py src/pcbflow/container.py tests/conftest.py tests/integration/test_command_batches.py
git commit -m "feat: persist and queue design proposals"
```

---

### Task 11: Build a bounded, loss-preserving KiCad S-expression CST

**Files:**
- Create: `src/pcbflow/schematic/__init__.py`
- Create: `src/pcbflow/schematic/cst.py`
- Create: `tests/unit/test_schematic_cst.py`
- Create: `tests/fixtures/kicad/controlled-design/board.kicad_sch`

**Interfaces:**
- Produces: `CstLimits`, `CstDocument`, `CstList`, `CstAtom`, `CstEdit`.
- Produces: `parse_cst(data: bytes, limits: CstLimits = CstLimits()) -> CstDocument`
- Produces: `apply_edits(document: CstDocument, edits: tuple[CstEdit, ...]) -> bytes`
- Produces: `replace_node(target: CstNode, replacement: CstNode) -> CstEdit`
- Produces: `insert_before_close(parent: CstList, nodes: tuple[CstNode, ...], indent: int) -> CstEdit`
- Produces: `make_atom`, `make_string`, and `make_list` for controlled new subtrees.
- Guarantees: parsing and rendering with no edits returns the original bytes exactly.

- [ ] **Step 1: Write failing byte-roundtrip, edit-span, Unicode, and limit tests**

```python
# tests/unit/test_schematic_cst.py
from __future__ import annotations

import pytest
from hypothesis import given, strategies as st

from pcbflow.schematic.cst import (
    CstLimitError,
    CstLimits,
    apply_edits,
    make_string,
    parse_cst,
    replace_node,
)


SOURCE = (
    b'(kicad_sch\r\n'
    b'  (version 20250114)\r\n'
    b'  (generator "pcbflow-test")\r\n'
    b'  (uuid 00000000-0000-0000-0000-000000000001)\r\n'
    b'  (symbol (lib_id "Device:LED")\r\n'
    b'    (uuid 00000000-0000-0000-0000-000000000002)\r\n'
    b'    (property "Reference" "D1")\r\n'
    b'    (property "Value" "\xe7\x8a\xb6\xe6\x80\x81LED"))\r\n'
    b'  (unknown_future_node (nested "preserve me")))\r\n'
)


def test_no_change_roundtrip_is_byte_exact() -> None:
    document = parse_cst(SOURCE)
    assert apply_edits(document, ()) == SOURCE
    assert document.root.head == "kicad_sch"
    assert document.root.find_children("unknown_future_node")


def test_replacing_one_string_preserves_all_other_bytes() -> None:
    document = parse_cst(SOURCE)
    symbol = document.root.find_children("symbol")[0]
    value_property = [
        node
        for node in symbol.find_children("property")
        if node.atom_text(1) == "Value"
    ][0]
    target = value_property.items[2]
    changed = apply_edits(
        document,
        (replace_node(target, make_string("GREEN")),),
    )

    assert changed[: target.start] == SOURCE[: target.start]
    assert changed[target.start : target.start + len(b'"GREEN"')] == b'"GREEN"'
    assert changed[target.start + len(b'"GREEN"') :] == SOURCE[target.end :]
    assert apply_edits(parse_cst(changed), ()) == changed


@given(st.text(st.characters(blacklist_categories=("Cs",)), max_size=128))
def test_arbitrary_unicode_string_node_roundtrips(value: str) -> None:
    document = parse_cst(b'(root "old")')
    changed = apply_edits(
        document,
        (replace_node(document.root.items[1], make_string(value)),),
    )
    reparsed = parse_cst(changed)
    assert reparsed.root.atom_text(1) == value
    assert apply_edits(reparsed, ()) == changed


def test_parser_enforces_size_depth_node_and_string_limits() -> None:
    with pytest.raises(CstLimitError, match="file_size"):
        parse_cst(b"(x)", CstLimits(max_file_bytes=2))
    with pytest.raises(CstLimitError, match="depth"):
        parse_cst(b"(((x)))", CstLimits(max_depth=2))
    with pytest.raises(CstLimitError, match="nodes"):
        parse_cst(b"(x y z)", CstLimits(max_nodes=2))
    with pytest.raises(CstLimitError, match="string"):
        parse_cst(b'(x "long")', CstLimits(max_string_bytes=3))
```

The committed `board.kicad_sch` fixture must use KiCad 9 format and include a
stable root UUID, one LED symbol with pins, a custom property, CRLF whitespace,
and Unicode text. Unknown-node preservation stays in the inline `SOURCE`
fixture so the committed project remains acceptable to real KiCad.

- [ ] **Step 2: Run tests and verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_schematic_cst.py -q
```

Expected: FAIL because the CST module does not exist.

- [ ] **Step 3: Implement the tokenizer, parser, and byte-splice editor**

Use immutable nodes whose `start` and `end` offsets refer to the original byte
buffer. Trivia is tokenized and retained in `CstDocument.tokens`, while list
items contain only semantic atoms and child lists:

```python
# src/pcbflow/schematic/cst.py
from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum


class CstParseError(ValueError):
    pass


class CstLimitError(CstParseError):
    pass


@dataclass(frozen=True, slots=True)
class CstLimits:
    max_file_bytes: int = 20_000_000
    max_depth: int = 256
    max_nodes: int = 1_000_000
    max_string_bytes: int = 1_000_000


class TokenKind(StrEnum):
    LEFT = "left"
    RIGHT = "right"
    ATOM = "atom"
    STRING = "string"
    TRIVIA = "trivia"


@dataclass(frozen=True, slots=True)
class Token:
    kind: TokenKind
    start: int
    end: int
    raw: bytes


@dataclass(frozen=True, slots=True)
class CstAtom:
    value: str
    quoted: bool
    start: int = -1
    end: int = -1


@dataclass(frozen=True, slots=True)
class CstList:
    items: tuple[CstNode, ...]
    start: int = -1
    end: int = -1
    close_start: int = -1

    @property
    def head(self) -> str | None:
        if self.items and isinstance(self.items[0], CstAtom):
            return self.items[0].value
        return None

    def find_children(self, head: str) -> tuple[CstList, ...]:
        return tuple(
            item
            for item in self.items[1:]
            if isinstance(item, CstList) and item.head == head
        )

    def atom_text(self, index: int) -> str:
        item = self.items[index]
        if not isinstance(item, CstAtom):
            raise CstParseError(f"item {index} is not an atom")
        return item.value


CstNode = CstAtom | CstList


@dataclass(frozen=True, slots=True)
class CstDocument:
    source: bytes
    tokens: tuple[Token, ...]
    root: CstList


@dataclass(frozen=True, slots=True)
class CstEdit:
    start: int
    end: int
    replacement: bytes
```

Tokenizer rules are byte-based: accept ASCII parentheses, ASCII whitespace,
UTF-8 atoms, and JSON-compatible quoted-string escapes. Reject invalid UTF-8,
NUL, unterminated strings, trailing roots, unmatched parentheses, and any
configured limit breach. Decode a string token with
`json.loads(token.raw.decode("utf-8"))`; encode controlled strings with
`json.dumps(value, ensure_ascii=False, separators=(",", ":"))`.

Implement editing with non-overlapping byte splices:

```python
def make_atom(value: str) -> CstAtom:
    if not value or any(character.isspace() or character in '()"' for character in value):
        raise ValueError("bare atom contains reserved characters")
    return CstAtom(value=value, quoted=False)


def make_string(value: str) -> CstAtom:
    return CstAtom(value=value, quoted=True)


def make_list(*items: CstNode) -> CstList:
    return CstList(items=tuple(items))


def render_node(node: CstNode) -> bytes:
    if isinstance(node, CstAtom):
        if node.quoted:
            return json.dumps(node.value, ensure_ascii=False).encode("utf-8")
        return node.value.encode("utf-8")
    return b"(" + b" ".join(render_node(item) for item in node.items) + b")"


def replace_node(target: CstNode, replacement: CstNode) -> CstEdit:
    if target.start < 0 or target.end < target.start:
        raise ValueError("target is not attached to a parsed document")
    return CstEdit(target.start, target.end, render_node(replacement))


def insert_before_close(
    parent: CstList, nodes: tuple[CstNode, ...], indent: int
) -> CstEdit:
    if parent.close_start < 0:
        raise ValueError("parent is not attached to a parsed document")
    prefix = b"\n" + (b" " * indent)
    replacement = b"".join(prefix + render_node(node) for node in nodes)
    return CstEdit(parent.close_start, parent.close_start, replacement)


def apply_edits(document: CstDocument, edits: tuple[CstEdit, ...]) -> bytes:
    ordered = sorted(edits, key=lambda edit: (edit.start, edit.end))
    cursor = 0
    output = bytearray()
    for edit in ordered:
        if edit.start < cursor or edit.end < edit.start or edit.end > len(document.source):
            raise ValueError("CST edits overlap or escape the source")
        output.extend(document.source[cursor : edit.start])
        output.extend(edit.replacement)
        cursor = edit.end
    output.extend(document.source[cursor:])
    return bytes(output)
```

`parse_cst` must increment the node counter for every atom and list and must
return exactly one root `CstList`. Export the public names from
`schematic/__init__.py` without importing the adapter yet.

- [ ] **Step 4: Run CST and property tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_schematic_cst.py -q
```

Expected: PASS, including the Hypothesis examples.

- [ ] **Step 5: Commit**

```powershell
git add src/pcbflow/schematic/__init__.py src/pcbflow/schematic/cst.py tests/unit/test_schematic_cst.py tests/fixtures/kicad/controlled-design/board.kicad_sch
git commit -m "feat: add loss-preserving kicad cst"
```

---

### Task 12: Extract semantic schematic IR and stable attributed Diff

**Files:**
- Create: `src/pcbflow/schematic/semantic.py`
- Create: `src/pcbflow/schematic/diff.py`
- Create: `tests/unit/test_schematic_semantic.py`
- Create: `tests/unit/test_schematic_diff.py`

**Interfaces:**
- Produces: `SchematicDocument`, `Sheet`, `HierarchicalPort`, `Symbol`, `SymbolProperty`, `PinReference`, `Label`, `NetConnectivity`, `FootprintAssignment`.
- Produces: `ParsedSchematic` and `CstLocation` for controlled editing.
- Produces: `inspect_schematic(project: Path) -> SchematicDocument`
- Produces: `parse_schematic(project: Path) -> ParsedSchematic`
- Produces: `SemanticChange`, `SemanticDiff`, `ChangeSelector`, `CommandAttribution`.
- Produces: `build_semantic_diff(before, after, attributions) -> SemanticDiff`
- Produces: `semantic_diff_bytes(value: SemanticDiff) -> bytes`
- Guarantees: changes are stably ordered and every semantic change is attributed to exactly one command.

- [ ] **Step 1: Write failing semantic extraction and attributed-Diff tests**

```python
# tests/unit/test_schematic_semantic.py
from __future__ import annotations

from pathlib import Path

from pcbflow.schematic.semantic import inspect_schematic, parse_schematic


def test_inspect_uses_kicad_uuid_as_symbol_identity() -> None:
    fixture = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "kicad"
        / "controlled-design"
    )
    parsed = parse_schematic(fixture)
    document = parsed.document

    assert document.kicad_major == 9
    assert len(document.sheets) == 1
    assert len(document.symbols) == 1
    symbol = document.symbols[0]
    assert symbol.ref.object_uuid == "00000000-0000-0000-0000-000000000002"
    assert symbol.reference == "D1"
    assert symbol.value == "状态LED"
    assert parsed.location(symbol.ref).file_path.name == "board.kicad_sch"
    assert inspect_schematic(fixture) == document
```

```python
# tests/unit/test_schematic_diff.py
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from pcbflow.commands import RiskLevel
from pcbflow.schematic.diff import (
    ChangeKind,
    ChangeSelector,
    CommandAttribution,
    UnattributedSemanticChangeError,
    build_semantic_diff,
    semantic_diff_bytes,
)
from pcbflow.schematic.semantic import inspect_schematic


def _document():
    fixture = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "kicad"
        / "controlled-design"
    )
    return inspect_schematic(fixture)


def test_property_and_footprint_changes_are_stable_and_attributed() -> None:
    before = _document()
    symbol = before.symbols[0]
    changed_symbol = replace(
        symbol,
        value="GREEN",
        footprint="LED_SMD:LED_0603_1608Metric",
        properties=tuple(
            replace(item, value="GREEN") if item.name == "Value" else item
            for item in symbol.properties
        ),
    )
    after = replace(before, symbols=(changed_symbol,))
    attribution = CommandAttribution(
        command_id="cmd_update_led",
        requirement_ids=("REQ-FUNC-001",),
        risk=RiskLevel.LOW,
        selectors=(
            ChangeSelector(
                kind=ChangeKind.FOOTPRINT_CHANGED,
                subject_ref=symbol.ref,
                field="Footprint",
            ),
            ChangeSelector(
                kind=ChangeKind.SYMBOL_PROPERTY_CHANGED,
                subject_ref=symbol.ref,
                field="Value",
            ),
        ),
    )

    result = build_semantic_diff(before, after, (attribution,))

    assert [item.kind.value for item in result.changes] == [
        "footprint_changed",
        "symbol_property_changed",
    ]
    assert all(item.command_id == "cmd_update_led" for item in result.changes)
    assert all(item.requirement_ids == ("REQ-FUNC-001",) for item in result.changes)
    assert semantic_diff_bytes(result) == semantic_diff_bytes(result)


def test_unattributed_change_is_rejected() -> None:
    before = _document()
    after = replace(
        before,
        symbols=(replace(before.symbols[0], value="UNTRACKED"),),
    )
    with pytest.raises(UnattributedSemanticChangeError):
        build_semantic_diff(before, after, ())
```

- [ ] **Step 2: Run semantic tests and verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_schematic_semantic.py tests/unit/test_schematic_diff.py -q
```

Expected: FAIL because semantic and Diff modules do not exist.

- [ ] **Step 3: Implement immutable IR, CST locations, and deterministic Diff**

Define these immutable values in `semantic.py`; all tuple fields must be sorted
by the semantic reference key before constructing `SchematicDocument`:

```python
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pcbflow.commands import SchematicObjectRef
from pcbflow.schematic.cst import CstDocument, CstList, parse_cst


@dataclass(frozen=True, slots=True)
class Point:
    x: float
    y: float


@dataclass(frozen=True, slots=True)
class SymbolProperty:
    name: str
    value: str


@dataclass(frozen=True, slots=True)
class HierarchicalPort:
    ref: SchematicObjectRef
    name: str
    direction: str
    position: Point


@dataclass(frozen=True, slots=True)
class Sheet:
    ref: SchematicObjectRef
    name: str
    file_name: str
    parent_sheet_uuid: str | None
    ports: tuple[HierarchicalPort, ...]


@dataclass(frozen=True, slots=True)
class PinReference:
    ref: SchematicObjectRef
    symbol_ref: SchematicObjectRef
    number: str
    name: str
    position: Point


@dataclass(frozen=True, slots=True)
class FootprintAssignment:
    symbol_ref: SchematicObjectRef
    library_id: str


@dataclass(frozen=True, slots=True)
class Symbol:
    ref: SchematicObjectRef
    library_id: str
    reference: str
    value: str
    unit: int
    position: Point
    properties: tuple[SymbolProperty, ...]
    footprint: str | None
    pins: tuple[PinReference, ...]


@dataclass(frozen=True, slots=True)
class Label:
    ref: SchematicObjectRef
    name: str
    scope: str
    target_ref: SchematicObjectRef | None
    position: Point


@dataclass(frozen=True, slots=True)
class NetConnectivity:
    ref: SchematicObjectRef
    name: str | None
    members: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SchematicDocument:
    kicad_major: int
    root_file: str
    root_sheet_ref: SchematicObjectRef
    sheets: tuple[Sheet, ...]
    symbols: tuple[Symbol, ...]
    labels: tuple[Label, ...]
    nets: tuple[NetConnectivity, ...]
    footprints: tuple[FootprintAssignment, ...]


@dataclass(frozen=True, slots=True)
class CstLocation:
    file_path: Path
    document: CstDocument
    node: CstList


@dataclass(frozen=True, slots=True)
class ParsedSchematic:
    document: SchematicDocument
    locations: dict[str, CstLocation]

    def location(self, reference: SchematicObjectRef) -> CstLocation:
        try:
            return self.locations[object_ref_key(reference)]
        except KeyError as error:
            raise SemanticObjectNotFoundError(object_ref_key(reference)) from error


class SemanticObjectNotFoundError(LookupError):
    pass


def object_ref_key(reference: SchematicObjectRef) -> str:
    pin = "" if reference.pin_number is None else f":{reference.pin_number}"
    return (
        f"{reference.kind}:{reference.sheet_uuid}:"
        f"{reference.object_uuid}{pin}"
    )
```

`parse_schematic` must find all `*.kicad_sch` files, resolve child filenames
from hierarchical sheet properties, and require exactly one unreferenced root;
multiple unreferenced roots are ambiguous. Reject links and files outside
`project`, parse `(version ...)`, and require the explicitly supported KiCad 9
format version. Walk known nodes by head name while leaving unknown CST nodes
untouched. Extract UUIDs from `(uuid ...)`; never synthesize identity from a
reference designator. Record the enclosing CST node for sheets, symbols,
properties, pins, labels, and nets in `locations`. A missing UUID in an object
that commands can target is `KicadSemanticError`, not a fallback identity.

Implement Diff as strict Pydantic output so canonical JSON is direct:

```python
# src/pcbflow/schematic/diff.py
from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict

from pcbflow.canonical import canonical_json_bytes
from pcbflow.commands import RiskLevel, SchematicObjectRef
from pcbflow.schematic.semantic import SchematicDocument, object_ref_key


class ChangeKind(StrEnum):
    SHEET_ADDED = "sheet_added"
    SHEET_REMOVED = "sheet_removed"
    SYMBOL_ADDED = "symbol_added"
    SYMBOL_REMOVED = "symbol_removed"
    SYMBOL_PROPERTY_CHANGED = "symbol_property_changed"
    FOOTPRINT_CHANGED = "footprint_changed"
    LABEL_ADDED = "label_added"
    LABEL_REMOVED = "label_removed"
    NET_CONNECTIVITY_CHANGED = "net_connectivity_changed"


@dataclass(frozen=True, slots=True)
class ChangeSelector:
    kind: ChangeKind
    subject_ref: SchematicObjectRef
    field: str | None


@dataclass(frozen=True, slots=True)
class CommandAttribution:
    command_id: str
    requirement_ids: tuple[str, ...]
    risk: RiskLevel
    selectors: tuple[ChangeSelector, ...]


class SemanticChange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    kind: ChangeKind
    subject_ref: SchematicObjectRef
    before: object | None
    after: object | None
    field: str | None
    command_id: str
    requirement_ids: tuple[str, ...]
    risk: RiskLevel


class SemanticDiff(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal["1.0"] = "1.0"
    changes: tuple[SemanticChange, ...]


class UnattributedSemanticChangeError(RuntimeError):
    pass


def semantic_diff_bytes(value: SemanticDiff) -> bytes:
    return canonical_json_bytes(value.model_dump(mode="json"))
```

In `build_semantic_diff`, index every collection by `object_ref_key`, compare
all nine `ChangeKind` categories, and represent dataclasses with `asdict`.
Set `field` to the property name for property changes, `"Footprint"` for
footprint changes, and `None` for whole-object/connectivity changes. Resolve
attribution by exact `(kind, subject key, field)` selector. Zero matches and
multiple matches both raise `UnattributedSemanticChangeError`. Sort final changes by
`(kind.value, object_ref_key(subject_ref), field or "", command_id)`; do not filter UUID,
footprint, hierarchy, user-property, or connectivity changes.

- [ ] **Step 4: Run semantic, CST, and Diff tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_schematic_cst.py tests/unit/test_schematic_semantic.py tests/unit/test_schematic_diff.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/pcbflow/schematic/semantic.py src/pcbflow/schematic/diff.py tests/unit/test_schematic_semantic.py tests/unit/test_schematic_diff.py
git commit -m "feat: add schematic semantic ir and diff"
```

---

### Task 13: Load verified modules read-only and instantiate one hierarchically

**Files:**
- Create: `src/pcbflow/schematic/modules.py`
- Create: `src/pcbflow/schematic/adapter.py`
- Create: `tests/unit/test_schematic_modules.py`
- Create: `tests/fixtures/modules/status-led-v1/module.yaml`
- Create: `tests/fixtures/modules/status-led-v1/status-led.kicad_sch`
- Modify: `src/pcbflow/schematic/__init__.py`

**Interfaces:**
- Produces: `ModuleCatalogPort.get(module_revision_id: str) -> ModuleRevision`
- Produces: `FileModuleCatalog` with link, path, file-count, size, type, schema, and digest checks.
- Produces: `derive_module_uuid(project_id, batch_id, command_id, module_revision_digest, module_local_uuid) -> str`
- Produces: `SchematicAdapter.inspect(project: Path) -> SchematicDocument`
- Produces: `SchematicAdapter.apply(project: Path, commands: tuple[DesignCommand, ...]) -> ApplyResult`
- Produces: `CstSchematicAdapter(module_catalog: ModuleCatalogPort | None)`, `ApplyResult`, `CommandResult`, and `AdapterCapabilityReport`.
- Implements: `schematic.instantiate_module` only; the other operation variants return `DESIGN_COMMAND_UNSUPPORTED` until Task 14.

- [ ] **Step 1: Write failing module integrity and deterministic-instantiation tests**

```python
# tests/unit/test_schematic_modules.py
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from pcbflow.commands import load_command_batch
from pcbflow.schematic.adapter import CstSchematicAdapter
from pcbflow.schematic.modules import (
    FileModuleCatalog,
    ModuleIntegrityError,
    derive_module_uuid,
)


def _fixture_root() -> Path:
    return Path(__file__).resolve().parents[1] / "fixtures"


def _command(project_id: str, revision: str):
    actor = {"type": "human", "id": "local-user"}
    value = {
        "schema_version": "1.0",
        "batch_id": "bat_status_led",
        "project_id": project_id,
        "base_revision": revision,
        "requirement_set_id": "reqset_controller",
        "idempotency_key": "status-led",
        "actor": actor,
        "intent": "Add the verified status LED",
        "risk": "medium",
        "commands": [
            {
                "schema_version": "1.0",
                "command_id": "cmd_status_led",
                "batch_id": "bat_status_led",
                "project_id": project_id,
                "base_revision": revision,
                "idempotency_key": "status-led:1",
                "actor": actor,
                "intent": "Add the verified status LED",
                "risk": "medium",
                "preconditions": [],
                "operation": {
                    "type": "schematic.instantiate_module",
                    "payload": {
                        "module_revision_id": "modrev_status_led_v1",
                        "instance_name": "STATUS_LED",
                        "target_sheet_ref": {
                            "kind": "sheet",
                            "sheet_uuid": "00000000-0000-0000-0000-000000000001",
                            "object_uuid": "00000000-0000-0000-0000-000000000001",
                            "pin_number": None,
                        },
                        "parameter_bindings": {"LED_VALUE": "GREEN"},
                        "port_bindings": {},
                        "placement_slot": "auto",
                    },
                },
                "required_validations": ["semantic_diff", "kicad_erc"],
                "provenance": {
                    "requirement_ids": ["REQ-FUNC-001"],
                    "evidence_ids": [],
                    "module_revision_ids": ["modrev_status_led_v1"],
                },
            }
        ],
    }
    return load_command_batch(json.dumps(value).encode()).commands[0]


def test_module_catalog_rejects_template_digest_mismatch(tmp_path: Path) -> None:
    source = _fixture_root() / "modules" / "status-led-v1"
    catalog_root = tmp_path / "modules"
    shutil.copytree(source, catalog_root / "status-led-v1")
    template = catalog_root / "status-led-v1" / "status-led.kicad_sch"
    template.write_bytes(template.read_bytes() + b"\n")

    catalog = FileModuleCatalog(catalog_root, max_files=16, max_bytes=1_000_000)
    with pytest.raises(ModuleIntegrityError, match="template digest"):
        catalog.get("modrev_status_led_v1")


def test_uuid_derivation_is_stable_and_input_bound() -> None:
    first = derive_module_uuid(
        "prj_controller",
        "bat_status_led",
        "cmd_status_led",
        "sha256:" + "a" * 64,
        "led-symbol",
    )
    repeated = derive_module_uuid(
        "prj_controller",
        "bat_status_led",
        "cmd_status_led",
        "sha256:" + "a" * 64,
        "led-symbol",
    )
    changed = derive_module_uuid(
        "prj_controller",
        "bat_status_led",
        "cmd_status_led",
        "sha256:" + "b" * 64,
        "led-symbol",
    )
    assert first == repeated
    assert first != changed


def test_instantiate_module_is_deterministic_and_does_not_modify_catalog(
    tmp_path: Path,
) -> None:
    fixture = _fixture_root() / "kicad" / "controlled-design"
    left = tmp_path / "left"
    right = tmp_path / "right"
    shutil.copytree(fixture, left)
    shutil.copytree(fixture, right)
    catalog_root = _fixture_root() / "modules"
    catalog_before = {
        path.relative_to(catalog_root): path.read_bytes()
        for path in catalog_root.rglob("*")
        if path.is_file()
    }
    adapter = CstSchematicAdapter(
        FileModuleCatalog(catalog_root, max_files=16, max_bytes=1_000_000)
    )
    command = _command("prj_controller", "git:" + "1" * 40)

    left_result = adapter.apply(left, (command,))
    right_result = adapter.apply(right, (command,))

    assert left_result.modified_files == right_result.modified_files
    assert {
        path.relative_to(left): path.read_bytes()
        for path in left.rglob("*")
        if path.is_file()
    } == {
        path.relative_to(right): path.read_bytes()
        for path in right.rglob("*")
        if path.is_file()
    }
    assert any(item.name == "STATUS_LED" for item in left_result.after.sheets)
    assert catalog_before == {
        path.relative_to(catalog_root): path.read_bytes()
        for path in catalog_root.rglob("*")
        if path.is_file()
    }
```

The committed module fixture must be generated/saved by KiCad 9 and use LF
line endings. `module.yaml` has this exact shape; set `template.digest` to the
lowercase SHA-256 of the committed template bytes and make the test assert the
literal value:

```yaml
schema_version: "1.0"
module_revision_id: modrev_status_led_v1
status: verified
name: Status LED
kicad_major: 9
adapter_contract: pcbflow.schematic.cst.v1
template:
  path: status-led.kicad_sch
uuid_bindings:
  10000000-0000-0000-0000-000000000001: module-root
  10000000-0000-0000-0000-000000000002: led-symbol
parameters:
  LED_VALUE:
    property_name: Value
    required: true
ports: {}
footprints:
  fprev_led_0603_v1:
    library_id: LED_SMD:LED_0603_1608Metric
    digest: sha256:8b17248f9a5b3a14f469e60e61d22687891d16f04a2013a347e2c81dfe94141b
```

After the KiCad template bytes are final, compute the one content-derived
field:

```powershell
(Get-FileHash -Algorithm SHA256 -LiteralPath 'tests\fixtures\modules\status-led-v1\status-led.kicad_sch').Hash.ToLowerInvariant()
```

Apply a one-line patch that adds `template.digest: sha256:` followed by that
64-character output. Immediately run the catalog test; its first assertion
must compare the literal manifest value with the independently recomputed raw
template digest. No other fixture field is generated.

- [ ] **Step 2: Run module tests and verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_schematic_modules.py -q
```

Expected: FAIL because module catalog and adapter do not exist.

- [ ] **Step 3: Implement the read-only catalog and hierarchical operation**

Use strict manifest models and immutable return values:

```python
# src/pcbflow/schematic/modules.py
from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid5

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from pcbflow.canonical import canonical_digest

MODULE_UUID_NAMESPACE = UUID("9bcf613a-1ced-5b76-9a90-8fd90ac5f16d")


class ModuleIntegrityError(ValueError):
    pass


class ModuleRevisionNotFoundError(LookupError):
    pass


class _StrictManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class TemplateEntry(_StrictManifest):
    path: str = Field(pattern=r"^[A-Za-z0-9_.-]+\.kicad_sch$")
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class ParameterEntry(_StrictManifest):
    property_name: str = Field(min_length=1)
    required: bool


class FootprintEntry(_StrictManifest):
    library_id: str = Field(pattern=r"^[A-Za-z0-9_.+-]+:[A-Za-z0-9_.+-]+$")
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class ModuleManifest(_StrictManifest):
    schema_version: str = Field(pattern=r"^1\.0$")
    module_revision_id: str = Field(pattern=r"^modrev_[A-Za-z0-9_-]+$")
    status: str = Field(pattern=r"^verified$")
    name: str = Field(min_length=1)
    kicad_major: int
    adapter_contract: str = Field(pattern=r"^pcbflow\.schematic\.cst\.v1$")
    template: TemplateEntry
    uuid_bindings: dict[str, str]
    parameters: dict[str, ParameterEntry]
    ports: dict[str, str]
    footprints: dict[str, FootprintEntry]


@dataclass(frozen=True, slots=True)
class ModuleRevision:
    manifest: ModuleManifest
    manifest_digest: str
    template_path: Path
    template_bytes: bytes


def derive_module_uuid(
    project_id: str,
    batch_id: str,
    command_id: str,
    module_revision_digest: str,
    module_local_uuid: str,
) -> str:
    value = "\x1f".join(
        (
            project_id,
            batch_id,
            command_id,
            module_revision_digest,
            module_local_uuid,
        )
    )
    return str(uuid5(MODULE_UUID_NAMESPACE, value))
```

`FileModuleCatalog.get` must resolve only
`<catalog>/<module_revision_id without modrev_ prefix>/module.yaml` candidates
whose manifest ID matches the request. For the repository fixture, also permit
the directory name `status-led-v1` by scanning direct child manifests and
building an ID index once. Reject symlinks and Windows reparse points, absolute
manifest paths, `..`, extensions other than `.yaml` and `.kicad_sch`, more than
`max_files`, or more than `max_bytes`. Use `yaml.safe_load`, strict validation,
SHA-256 over raw template bytes, and
`canonical_digest(manifest.model_dump(mode="json"))`. Never open files for
write and never expose a mutable catalog path to callers.
`CstSchematicAdapter` accepts `None` when no catalog directory is configured;
inspection, property writes, and label writes remain available, while module
instantiation and footprint assignment raise the stable not-found error without
probing arbitrary filesystem locations.

Define the adapter contract in `adapter.py`:

```python
@dataclass(frozen=True, slots=True)
class AdapterCapabilityReport:
    adapter_contract: str
    kicad_major: int
    supported_operations: tuple[str, ...]
    module_digests: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CommandResult:
    command_id: str
    operation_type: str
    effects: tuple[ChangeSelector, ...]
    created_files: tuple[str, ...]
    provenance_digests: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ApplyResult:
    modified_files: tuple[str, ...]
    command_results: tuple[CommandResult, ...]
    after: SchematicDocument
    capability_report: AdapterCapabilityReport


class SchematicAdapter(Protocol):
    def inspect(self, project: Path) -> SchematicDocument: ...
    def apply(
        self, project: Path, commands: tuple[DesignCommand, ...]
    ) -> ApplyResult: ...
```

Import `ChangeSelector` and `ChangeKind` from `pcbflow.schematic.diff` in the
adapter. Every operation must return exact selectors, not only a broad object
list; this keeps attribution unambiguous when multiple commands touch different
fields of the same symbol.

For `schematic.instantiate_module`, perform these concrete operations without
regex or free-form string replacement:

1. Load and verify the requested `ModuleRevision`.
2. Require `kicad_major == 9`, adapter contract `v1`, exact parameter names,
   all required parameters, and exact port names.
3. Derive every template UUID through `derive_module_uuid`; find matching CST
   atoms and replace them with `CstEdit` objects.
4. Apply parameter values only to manifest-declared property CST nodes.
5. Write the child page to
   `generated/<lowercase-instance-name>-<command_id>.kicad_sch` using a sibling
   temporary file and `os.replace`.
6. Insert one KiCad hierarchical `(sheet ...)` subtree in the target sheet.
   Its sheet UUID and sheet-pin UUIDs are derived, its filename is relative,
   and `auto` placement selects the first free 25 mm grid slot in stable order.
7. Resolve port bindings by semantic reference; do not accept coordinates from
   the command. Insert only the sheet pins and labels required to bind those
   existing semantic targets.
8. Reparse both changed files, inspect the complete project, and return stable
   sorted file paths plus one exact `ChangeSelector` for every sheet, symbol,
   label, footprint, property, or connectivity change caused by the command.

If any step fails, remove newly created temporary files and leave the original
project bytes intact. Export the adapter types through `schematic/__init__.py`.

- [ ] **Step 4: Run module, semantic, and CST tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_schematic_modules.py tests/unit/test_schematic_semantic.py tests/unit/test_schematic_cst.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/pcbflow/schematic/modules.py src/pcbflow/schematic/adapter.py src/pcbflow/schematic/__init__.py tests/unit/test_schematic_modules.py tests/fixtures/modules/status-led-v1/module.yaml tests/fixtures/modules/status-led-v1/status-led.kicad_sch
git commit -m "feat: instantiate verified schematic modules"
```

---

### Task 14: Implement controlled property, footprint, and label operations

**Files:**
- Create: `tests/unit/test_schematic_adapter.py`
- Modify: `src/pcbflow/schematic/adapter.py`
- Modify: `src/pcbflow/schematic/modules.py`
- Modify: `tests/fixtures/modules/status-led-v1/module.yaml`

**Interfaces:**
- Implements: `schematic.set_property`.
- Implements: `schematic.assign_footprint` through `FileModuleCatalog.get_footprint`.
- Implements: `schematic.add_label` for pin, hierarchical-port, wire-endpoint, and known-net references.
- Produces: `UnsupportedDesignCommandError`, `PropertyWriteNotAllowedError`, `FootprintRevisionNotFoundError`, and `LabelTargetError`.
- Guarantees: no operation accepts a filesystem path or a naked coordinate.

- [ ] **Step 1: Write failing positive and boundary tests for all three operations**

```python
# tests/unit/test_schematic_adapter.py
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from pcbflow.commands import load_command_batch
from pcbflow.schematic.adapter import (
    CstSchematicAdapter,
    LabelTargetError,
    PropertyWriteNotAllowedError,
)
from pcbflow.schematic.modules import (
    FileModuleCatalog,
    FootprintRevisionNotFoundError,
)


def _fixtures() -> Path:
    return Path(__file__).resolve().parents[1] / "fixtures"


def _command(operation: dict[str, object], command_id: str):
    actor = {"type": "human", "id": "local-user"}
    value = {
        "schema_version": "1.0",
        "batch_id": "bat_adapter_ops",
        "project_id": "prj_controller",
        "base_revision": "git:" + "1" * 40,
        "requirement_set_id": "reqset_controller",
        "idempotency_key": "adapter-ops",
        "actor": actor,
        "intent": "Apply one controlled operation",
        "risk": "low",
        "commands": [
            {
                "schema_version": "1.0",
                "command_id": command_id,
                "batch_id": "bat_adapter_ops",
                "project_id": "prj_controller",
                "base_revision": "git:" + "1" * 40,
                "idempotency_key": f"adapter-ops:{command_id}",
                "actor": actor,
                "intent": "Apply one controlled operation",
                "risk": "low",
                "preconditions": [],
                "operation": operation,
                "required_validations": ["semantic_diff", "kicad_erc"],
                "provenance": {
                    "requirement_ids": ["REQ-FUNC-001"],
                    "evidence_ids": [],
                    "module_revision_ids": [],
                },
            }
        ],
    }
    return load_command_batch(json.dumps(value).encode()).commands[0]


def _symbol_ref() -> dict[str, object]:
    return {
        "kind": "symbol",
        "sheet_uuid": "00000000-0000-0000-0000-000000000001",
        "object_uuid": "00000000-0000-0000-0000-000000000002",
        "pin_number": None,
    }


def _adapter() -> CstSchematicAdapter:
    catalog = FileModuleCatalog(
        _fixtures() / "modules", max_files=32, max_bytes=2_000_000
    )
    return CstSchematicAdapter(catalog)


def test_property_footprint_and_label_operations_reparse_semantically(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    shutil.copytree(_fixtures() / "kicad" / "controlled-design", project)
    commands = (
        _command(
            {
                "type": "schematic.set_property",
                "payload": {
                    "subject_ref": _symbol_ref(),
                    "property_name": "Value",
                    "value": "GREEN",
                    "expected_old_value": "状态LED",
                },
            },
            "cmd_property",
        ),
        _command(
            {
                "type": "schematic.assign_footprint",
                "payload": {
                    "subject_ref": _symbol_ref(),
                    "footprint_revision_id": "fprev_led_0603_v1",
                },
            },
            "cmd_footprint",
        ),
        _command(
            {
                "type": "schematic.add_label",
                "payload": {
                    "target_ref": {
                        "kind": "pin",
                        "sheet_uuid": "00000000-0000-0000-0000-000000000001",
                        "object_uuid": "00000000-0000-0000-0000-000000000002",
                        "pin_number": "1",
                    },
                    "name": "STATUS_LED_K",
                    "scope": "local",
                },
            },
            "cmd_label",
        ),
    )

    result = _adapter().apply(project, commands)

    symbol = result.after.symbols[0]
    assert symbol.value == "GREEN"
    assert symbol.footprint == "LED_SMD:LED_0603_1608Metric"
    assert any(label.name == "STATUS_LED_K" for label in result.after.labels)
    assert [item.command_id for item in result.command_results] == [
        "cmd_property",
        "cmd_footprint",
        "cmd_label",
    ]


@pytest.mark.parametrize("name", ["uuid", "ki_locked", "Footprint"])
def test_set_property_rejects_internal_or_special_fields(
    tmp_path: Path, name: str
) -> None:
    project = tmp_path / "project"
    shutil.copytree(_fixtures() / "kicad" / "controlled-design", project)
    command = _command(
        {
            "type": "schematic.set_property",
            "payload": {
                "subject_ref": _symbol_ref(),
                "property_name": name,
                "value": "forbidden",
                "expected_old_value": None,
            },
        },
        "cmd_forbidden_property",
    )
    with pytest.raises(PropertyWriteNotAllowedError):
        _adapter().apply(project, (command,))


def test_assign_footprint_rejects_unknown_revision(tmp_path: Path) -> None:
    project = tmp_path / "project"
    shutil.copytree(_fixtures() / "kicad" / "controlled-design", project)
    command = _command(
        {
            "type": "schematic.assign_footprint",
            "payload": {
                "subject_ref": _symbol_ref(),
                "footprint_revision_id": "fprev_unknown",
            },
        },
        "cmd_unknown_footprint",
    )
    with pytest.raises(FootprintRevisionNotFoundError):
        _adapter().apply(project, (command,))


def test_add_label_rejects_symbol_or_coordinate_like_target(tmp_path: Path) -> None:
    project = tmp_path / "project"
    shutil.copytree(_fixtures() / "kicad" / "controlled-design", project)
    command = _command(
        {
            "type": "schematic.add_label",
            "payload": {
                "target_ref": _symbol_ref(),
                "name": "INVALID_TARGET",
                "scope": "local",
            },
        },
        "cmd_invalid_label",
    )
    with pytest.raises(LabelTargetError):
        _adapter().apply(project, (command,))
```

- [ ] **Step 2: Run adapter tests and verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_schematic_adapter.py -q
```

Expected: FAIL because only module instantiation is implemented.

- [ ] **Step 3: Implement exact allowlists and semantic targeting**

Add the footprint lookup value to `modules.py`:

```python
@dataclass(frozen=True, slots=True)
class FootprintRevision:
    revision_id: str
    library_id: str
    digest: str
    module_revision_id: str


class FootprintRevisionNotFoundError(LookupError):
    pass


def get_footprint(self, revision_id: str) -> FootprintRevision:
    matches = [
        FootprintRevision(
            revision_id=revision_id,
            library_id=entry.library_id,
            digest=entry.digest,
            module_revision_id=module.manifest.module_revision_id,
        )
        for module in self._verified_modules()
        if (entry := module.manifest.footprints.get(revision_id)) is not None
    ]
    if len(matches) != 1:
        raise FootprintRevisionNotFoundError(revision_id)
    return matches[0]
```

In `adapter.py`, dispatch exhaustively on the discriminated operation. Use
these exact write rules:

```python
PROPERTY_ALLOWLIST = frozenset({"Reference", "Value", "Description"})
USER_PROPERTY_PREFIX = "User."
LABEL_TARGET_KINDS = frozenset(
    {"pin", "hierarchical_port", "wire_endpoint", "net"}
)
```

- `set_property`: resolve the semantic symbol, reject `Footprint`, UUID and
  `ki_*` fields, permit only `PROPERTY_ALLOWLIST` or `User.*`, compare
  `expected_old_value` when supplied, and replace or insert exactly one
  `(property ...)` CST subtree. A reference change may not collide with any
  other reference in the document. Return one
  `(SYMBOL_PROPERTY_CHANGED, symbol_ref, property_name)` selector.
- `assign_footprint`: call `get_footprint`; write only its normalized
  `library_id` to the `Footprint` property; add its digest to
  `CommandResult.provenance_digests`. Never interpret the revision ID as a
  path. Return one `(FOOTPRINT_CHANGED, symbol_ref, "Footprint")` selector.
- `add_label`: require `LABEL_TARGET_KINDS`; resolve the target to an existing
  semantic position internally; reject duplicate name/scope bindings that
  point at another net; derive the label UUID from project, batch, command,
  target key, name, and scope; insert the appropriate KiCad `label`,
  `global_label`, or `hierarchical_label` subtree. The payload contains no
  coordinate field. Return one `(LABEL_ADDED, new_label_ref, None)` selector;
  any connectivity change caused by the binding receives its own exact
  selector.

Accumulate edits per file, reject overlapping edits, atomically replace each
file, then parse the complete project once after all commands. A command whose
expected object disappears during the batch fails the entire adapter call and
restores all original bytes from an in-memory rollback map.

- [ ] **Step 4: Run all adapter and semantic tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_schematic_adapter.py tests/unit/test_schematic_modules.py tests/unit/test_schematic_semantic.py tests/unit/test_schematic_diff.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/pcbflow/schematic/adapter.py src/pcbflow/schematic/modules.py tests/unit/test_schematic_adapter.py tests/fixtures/modules/status-led-v1/module.yaml
git commit -m "feat: add controlled schematic edit operations"
```

---

### Task 15: Execute proposals with fencing, mandatory ERC, and immutable evidence

**Files:**
- Create: `tests/integration/test_proposals.py`
- Modify: `src/pcbflow/proposals.py`
- Modify: `src/pcbflow/proposal_store.py`
- Modify: `src/pcbflow/artifacts.py`
- Modify: `src/pcbflow/revisions.py`
- Modify: `src/pcbflow/repositories.py`
- Modify: `src/pcbflow/validation.py`
- Modify: `src/pcbflow/kicad.py`
- Modify: `src/pcbflow/container.py`
- Modify: `tests/integration/test_tasks.py`
- Modify: `tests/integration/test_validation.py`
- Modify: `tests/unit/test_artifacts.py`

**Interfaces:**
- Produces: `ProposalExecutor.__call__(lease: TaskLease) -> dict[str, object]`
- Produces: `EvidenceSet`, `EvidenceItem`, `EvidenceRegistration`, and `proposal_review_digest(...) -> str`.
- Produces: `ProposalStore.begin_execution(proposal_id: str, task_id: str, lease_token: str, now: datetime) -> ChangeProposal`.
- Produces: `ProposalStore.mark_validation_failed(proposal_id: str, task_id: str, lease_token: str, now: datetime, error_code: str, semantic_diff_digest: str | None, evidence_set_digest: str, result: dict[str, object], evidence: tuple[EvidenceRegistration, ...]) -> ChangeProposal`.
- Produces: `ProposalStore.mark_ready(proposal_id: str, task_id: str, lease_token: str, now: datetime, candidate_revision: str, candidate_snapshot_digest: str, review_digest: str, semantic_diff_digest: str, evidence_set_digest: str, result: dict[str, object], evidence: tuple[EvidenceRegistration, ...]) -> ChangeProposal`.
- Guarantees: each proposal transition requires a current unexpired task lease.
- Extends: `GitCli.diff_worktree(...) -> bytes` and `RevisionService.commit_candidate(...) -> CandidateRevision`.
- Extends: `KicadPort` with `probe() -> KicadCapability`.
- Guarantees: mandatory validations cannot be removed by command input, and a stale lease cannot publish proposal database state.

- [ ] **Step 1: Write failing ready, validation-failed, no-effect, and stale-fence tests**

```python
# tests/integration/test_proposals.py
from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pcbflow.config import Settings
from pcbflow.container import build_container
from pcbflow.domain import TaskStatus
from pcbflow.kicad import KicadCapability, RawValidationReport
from pcbflow.proposals import ProposalStatus
from pcbflow.repositories import StaleLeaseError

NOW = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
PASSING_ERC = b'{"version":"1.0","source":"board.kicad_sch","violations":[]}'
FAILING_ERC = (
    b'{"version":"1.0","source":"board.kicad_sch","violations":['
    b'{"type":"pin_not_connected","severity":"error",'
    b'"description":"pin is not connected","items":[]}]}'
)


class FakeProposalKicad:
    def __init__(self, report: bytes = PASSING_ERC) -> None:
        self.report = report

    def probe(self) -> KicadCapability:
        return KicadCapability(
            available=True,
            path=Path("kicad-cli"),
            version="9.0.2",
            executable_digest="sha256:" + "9" * 64,
            reason=None,
        )

    def validate(
        self, project_dir: Path, output_dir: Path
    ) -> tuple[RawValidationReport, ...]:
        assert project_dir.is_dir()
        output_dir.mkdir(parents=True, exist_ok=True)
        return (
            RawValidationReport(
                kind="erc",
                data=self.report,
                argv=("kicad-cli", "sch", "erc"),
                returncode=0,
                tool_version="9.0.2",
            ),
        )


def _settings(tmp_path: Path, module_catalog: Path) -> Settings:
    return Settings.from_env(
        {
            "PCBFLOW_DATA_DIR": str(tmp_path / "data"),
            "PCBFLOW_MODULE_CATALOG_DIR": str(module_catalog),
        }
    )


def _snapshot(root: Path) -> dict[Path, bytes]:
    return {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def _prepare(container, tmp_path: Path):
    fixtures = Path(__file__).resolve().parents[1] / "fixtures"
    source = tmp_path / "import-source"
    shutil.copytree(fixtures / "kicad" / "controlled-design", source)
    project = container.projects.create("Controller", source, "proposal-project")
    managed = container.revisions.adopt(project.id, "proposal-adopt")
    requirements = (fixtures / "requirements" / "reference-controller.yaml").read_bytes()
    draft = container.requirements.import_draft(
        managed.id, requirements, "proposal-requirements"
    )
    pending = container.requirements.submit(draft.id, "proposal-requirements-submit")
    frozen = container.approvals.decide_g1(
        requirement_set_id=pending.id,
        subject_digest=pending.subject_digest(),
        decision="approve",
        actor_type="human",
        actor_id="local-user",
        comment="approved",
        idempotency_key="proposal-g1",
    )
    return source, container.projects.get(managed.id), frozen


def _instantiate_batch(project, requirement_set) -> bytes:
    actor = {"type": "human", "id": "local-user"}
    value = {
        "schema_version": "1.0",
        "batch_id": "bat_execute_status_led",
        "project_id": project.id,
        "base_revision": project.current_revision,
        "requirement_set_id": requirement_set.id,
        "idempotency_key": "execute-status-led",
        "actor": actor,
        "intent": "Instantiate the verified status LED",
        "risk": "medium",
        "commands": [
            {
                "schema_version": "1.0",
                "command_id": "cmd_execute_status_led",
                "batch_id": "bat_execute_status_led",
                "project_id": project.id,
                "base_revision": project.current_revision,
                "idempotency_key": "execute-status-led:1",
                "actor": actor,
                "intent": "Instantiate the verified status LED",
                "risk": "medium",
                "preconditions": [
                    {
                        "type": "project.revision_equals",
                        "revision": project.current_revision,
                    },
                    {
                        "type": "requirements.digest_equals",
                        "digest": requirement_set.canonical_digest,
                    },
                    {
                        "type": "schematic.module_absent",
                        "instance_name": "STATUS_LED",
                    },
                    {
                        "type": "tool.capability_available",
                        "capability": "kicad.cst.write.v1",
                    },
                ],
                "operation": {
                    "type": "schematic.instantiate_module",
                    "payload": {
                        "module_revision_id": "modrev_status_led_v1",
                        "instance_name": "STATUS_LED",
                        "target_sheet_ref": {
                            "kind": "sheet",
                            "sheet_uuid": "00000000-0000-0000-0000-000000000001",
                            "object_uuid": "00000000-0000-0000-0000-000000000001",
                            "pin_number": None,
                        },
                        "parameter_bindings": {"LED_VALUE": "GREEN"},
                        "port_bindings": {},
                        "placement_slot": "auto",
                    },
                },
                "required_validations": ["semantic_diff"],
                "provenance": {
                    "requirement_ids": ["REQ-FUNC-001"],
                    "evidence_ids": [],
                    "module_revision_ids": ["modrev_status_led_v1"],
                },
            }
        ],
    }
    return json.dumps(value, separators=(",", ":")).encode()


def test_worker_builds_one_reviewable_candidate_and_complete_evidence(
    tmp_path: Path,
) -> None:
    fixtures = Path(__file__).resolve().parents[1] / "fixtures"
    container = build_container(
        _settings(tmp_path, fixtures / "modules"),
        kicad_override=FakeProposalKicad(),
        clock=lambda: NOW,
    )
    try:
        source, project, requirement_set = _prepare(container, tmp_path)
        source_before = _snapshot(source)
        proposal = container.proposals.create(
            _instantiate_batch(project, requirement_set), "execute-status-led"
        )

        assert container.worker.run_once()

        ready = container.proposal_store.get(proposal.id)
        task = container.tasks.get(proposal.task_id)
        assert ready.status is ProposalStatus.READY_FOR_REVIEW
        assert ready.candidate_revision is not None
        assert ready.review_digest is not None
        assert task.status is TaskStatus.SUCCEEDED
        assert container.revisions.resolve_proposal_ref(project.id, proposal.id) == (
            ready.candidate_revision
        )
        evidence = container.evidence.list_for_project(project.id)
        assert {
            "design_command_batch",
            "project_snapshot_before",
            "project_snapshot_after",
            "git_text_diff",
            "schematic_semantic_diff",
            "kicad_erc",
            "command_execution_log",
            "adapter_capability_report",
            "proposal_evidence_set",
        } <= {item.kind for item in evidence}
        assert all(container.artifacts.verify(item.artifact_digest) for item in evidence)
        assert _snapshot(source) == source_before
    finally:
        container.dispose()


def test_erc_failure_never_becomes_reviewable_or_advances_revision(
    tmp_path: Path,
) -> None:
    fixtures = Path(__file__).resolve().parents[1] / "fixtures"
    container = build_container(
        _settings(tmp_path, fixtures / "modules"),
        kicad_override=FakeProposalKicad(FAILING_ERC),
        clock=lambda: NOW,
    )
    try:
        _source, project, requirement_set = _prepare(container, tmp_path)
        before_revision = project.current_revision
        proposal = container.proposals.create(
            _instantiate_batch(project, requirement_set), "execute-status-led"
        )
        assert container.worker.run_once()
        failed = container.proposal_store.get(proposal.id)
        assert failed.status is ProposalStatus.VALIDATION_FAILED
        assert failed.candidate_revision is None
        assert failed.evidence_set_digest is not None
        assert all(
            container.artifacts.verify(item.artifact_digest)
            for item in container.evidence.list_for_project(project.id)
            if item.task_id == proposal.task_id
        )
        assert container.projects.get(project.id).current_revision == before_revision
    finally:
        container.dispose()


def test_expired_lease_cannot_mark_proposal_executing(tmp_path: Path) -> None:
    fixtures = Path(__file__).resolve().parents[1] / "fixtures"
    clock_value = NOW
    container = build_container(
        _settings(tmp_path, fixtures / "modules"),
        kicad_override=FakeProposalKicad(),
        clock=lambda: clock_value,
    )
    try:
        _source, project, requirement_set = _prepare(container, tmp_path)
        proposal = container.proposals.create(
            _instantiate_batch(project, requirement_set), "execute-status-led"
        )
        lease = container.tasks.claim_next("worker-a", NOW, 1)
        assert lease is not None
        container.tasks.start(lease.task_id, lease.lease_token, NOW)
        with pytest.raises(StaleLeaseError):
            container.proposal_executor.begin(
                lease, now=NOW + timedelta(seconds=1)
            )
        assert container.proposal_store.get(proposal.id).status is ProposalStatus.QUEUED
    finally:
        container.dispose()
```

Add this no-effect test beside them:

```python
def _no_effect_batch(project, requirement_set) -> bytes:
    value = json.loads(_instantiate_batch(project, requirement_set))
    value["batch_id"] = "bat_no_effect"
    value["idempotency_key"] = "proposal-no-effect"
    value["intent"] = "Write the existing value"
    command = value["commands"][0]
    command["batch_id"] = "bat_no_effect"
    command["command_id"] = "cmd_no_effect"
    command["idempotency_key"] = "proposal-no-effect:1"
    command["intent"] = "Write the existing value"
    command["preconditions"] = []
    command["operation"] = {
        "type": "schematic.set_property",
        "payload": {
            "subject_ref": {
                "kind": "symbol",
                "sheet_uuid": "00000000-0000-0000-0000-000000000001",
                "object_uuid": "00000000-0000-0000-0000-000000000002",
                "pin_number": None,
            },
            "property_name": "Value",
            "value": "状态LED",
            "expected_old_value": "状态LED",
        },
    }
    command["provenance"]["module_revision_ids"] = []
    return json.dumps(value, separators=(",", ":")).encode()


def test_no_effect_batch_fails_without_candidate_or_revision_change(
    tmp_path: Path,
) -> None:
    fixtures = Path(__file__).resolve().parents[1] / "fixtures"
    container = build_container(
        _settings(tmp_path, fixtures / "modules"),
        kicad_override=FakeProposalKicad(),
        clock=lambda: NOW,
    )
    try:
        _source, project, requirement_set = _prepare(container, tmp_path)
        proposal = container.proposals.create(
            _no_effect_batch(project, requirement_set), "proposal-no-effect"
        )
        assert container.worker.run_once()
        failed = container.proposal_store.get(proposal.id)
        task = container.tasks.get(proposal.task_id)
        assert failed.status is ProposalStatus.VALIDATION_FAILED
        assert failed.candidate_revision is None
        assert failed.evidence_set_digest is not None
        assert task.last_error_code == "DESIGN_COMMAND_NO_EFFECT"
        assert container.revisions.resolve_proposal_ref(project.id, proposal.id) is None
        assert container.projects.get(project.id).current_revision == (
            project.current_revision
        )
    finally:
        container.dispose()
```

Append this corruption test to `tests/unit/test_artifacts.py`:

```python
from pcbflow.artifacts import ArtifactConflictError


def test_put_rejects_a_corrupted_existing_digest_object(tmp_path: Path) -> None:
    store = ContentAddressedStore(tmp_path / "artifacts")
    descriptor = store.put_bytes(b"trusted evidence", "application/octet-stream")
    descriptor.path.write_bytes(b"corrupted bytes")

    with pytest.raises(ArtifactConflictError, match=descriptor.digest):
        store.put_bytes(b"trusted evidence", "application/octet-stream")
```

- [ ] **Step 2: Run proposal execution tests and verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_artifacts.py tests/integration/test_proposals.py tests/integration/test_tasks.py -q
```

Expected: FAIL because executor, fenced proposal transitions, evidence set,
and candidate Git helpers do not exist.

- [ ] **Step 3: Implement the fenced execution pipeline**

Harden `ContentAddressedStore.put_bytes`: when the target digest path already
exists, stream-hash and size-check it before returning. Raise
`ArtifactConflictError(digest)` if its bytes do not match; never overwrite or
silently bless a corrupted CAS object. Keep the existing temporary-file plus
`os.replace` path for new objects. Proposal execution maps this integrity
failure to terminal `CANDIDATE_VALIDATION_FAILED` without exposing the storage
path.

Define canonical evidence and review contracts in `proposals.py`; import
`ArtifactDescriptor` from `pcbflow.artifacts` (the existing dataclass import is
reused) and `Literal` from `typing`:

```python
class EvidenceItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    kind: str
    artifact_digest: str
    media_type: str
    verdict: str


class EvidenceSet(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal["1.0"] = "1.0"
    project_id: str
    task_id: str
    proposal_id: str
    base_revision: str
    candidate_revision: str | None
    artifacts: tuple[EvidenceItem, ...]


@dataclass(frozen=True, slots=True)
class EvidenceRegistration:
    descriptor: ArtifactDescriptor
    item: EvidenceItem

    def __post_init__(self) -> None:
        if self.descriptor.digest != self.item.artifact_digest:
            raise ValueError("evidence digest does not match descriptor")
        if self.descriptor.media_type != self.item.media_type:
            raise ValueError("evidence media type does not match descriptor")


MANDATORY_VALIDATIONS = frozenset(
    {
        ValidationKind.SCHEMA,
        ValidationKind.PRECONDITIONS,
        ValidationKind.PATH_LIMITS,
        ValidationKind.POST_WRITE_PARSE,
        ValidationKind.SEMANTIC_DIFF,
        ValidationKind.KICAD_ERC,
    }
)


def proposal_review_digest(
    *,
    proposal_id: str,
    project_id: str,
    base_revision: str,
    candidate_revision: str,
    candidate_snapshot_digest: str,
    requirement_set_digest: str,
    semantic_diff_digest: str,
    evidence_set_digest: str,
    adapter_capability_digest: str,
) -> str:
    return canonical_digest(
        {
            "schema_version": "1.0",
            "proposal_id": proposal_id,
            "project_id": project_id,
            "base_revision": base_revision,
            "candidate_revision": candidate_revision,
            "candidate_snapshot_digest": candidate_snapshot_digest,
            "requirement_set_digest": requirement_set_digest,
            "semantic_diff_digest": semantic_diff_digest,
            "evidence_set_digest": evidence_set_digest,
            "adapter_capability_digest": adapter_capability_digest,
        }
    )
```

Every proposal state method must include the same active-fence predicate in its
transaction:

```python
def _assert_active_fence(
    session: Session,
    task_id: str,
    lease_token: str,
    now: datetime,
) -> None:
    active = session.scalar(
        select(TaskRow.id).where(
            TaskRow.id == task_id,
            TaskRow.lease_token == lease_token,
            TaskRow.status == TaskStatus.RUNNING.value,
            TaskRow.lease_expires_at.is_not(None),
            TaskRow.lease_expires_at > now,
        )
    )
    if active is None:
        raise StaleLeaseError(task_id)
```

`begin_execution` changes `queued` or a prior crash's `executing` row to
`executing`; `mark_validation_failed` stores the stable error code, available
semantic/evidence/result digests, and fenced evidence registrations but no
candidate revision, candidate snapshot, or review digest; `mark_ready` writes all candidate/digest/result fields and
`ready_for_review`. Each compares proposal `version`, task ID, and active
fence, increments version, and inserts an outbox event in the same transaction.

Extend `TaskRepository.start/complete/fail` exactly as Task 2 specifies and add
`assert_active(task_id, lease_token, now)`. Update every existing Worker call
and test to pass its injected clock. `ProposalExecutor.begin` simply calls
`TaskRepository.assert_active` and `ProposalStore.begin_execution`, which makes
the stale-fence test deterministic.

Implement `ProposalExecutor.__call__` in this exact order:

1. Load proposal, immutable batch record including database `created_at`,
   project, and frozen requirement set.
2. Call `TaskRepository.assert_active`. If an already-ready proposal has a
   valid stored ref, artifacts, digests, and stored result, return that result
   without changing proposal state or producing another commit. If a prior
   attempt already stored `validation_failed`, raise `TerminalTaskError` with
   the stored stable code so the new lease can finish the task row without
   re-executing. Otherwise call fenced `begin_execution`, which accepts only
   `queued` or crash-retry `executing` state.
3. Require managed mode, exact current/base revision, active frozen
   RequirementSet, and `RevisionService.is_ancestor(project.id,
   frozen_revision, base_revision)`.
4. Probe KiCad and require major version 9 plus adapter capability
   `kicad.cst.write.v1`.
5. Materialize the exact database base revision, call
   `RevisionService.assert_clean(project.id, batch.base_revision, workspace)`, capture a versioned
   snapshot manifest, and inspect the before IR.
6. Evaluate every precondition. Store all results in the execution log. Any
   false or unknown result raises terminal
   `DESIGN_COMMAND_PRECONDITION_FAILED` with the serialized result.
7. Apply commands in order; reparse the project; reject modified paths outside
   the worktree, links/reparse points, file-count overflow, or byte overflow.
8. Build `CommandAttribution(selectors=command_result.effects, ...)` from every
   `CommandResult` and command provenance, then build semantic Diff. An empty Diff raises
   `DESIGN_COMMAND_NO_EFFECT`.
9. Run KiCad validation outside the project directory. Require exactly one ERC
   report and retain its normalized findings.
10. Save canonical batch JSON, before/after snapshot manifests, Git text Diff,
    semantic Diff, raw ERC, execution log, and capability report in the CAS;
    do not register database Evidence rows yet. If validation failed, build an
    EvidenceSet with `candidate_revision=None`, mark failing items with verdict
    `fail`, and call one fenced `mark_validation_failed` transaction that
    registers every available Artifact/Evidence row and stores
    `CANDIDATE_VALIDATION_FAILED`. Then raise the matching
    `TerminalTaskError`; do not create a commit or ref.
11. Fence again. Create a deterministic candidate commit using batch
    `created_at`, controlled author data derived from immutable actor metadata,
    and message `pcbflow: proposal <proposal_id>`. Create
    `refs/pcbflow/proposals/<proposal_id>` with compare-and-swap.
12. Build and store the canonical `EvidenceSet` in the CAS.
13. Compute `review_digest`, fence again, and call one `mark_ready` transaction
    that inserts or verifies every Artifact row, inserts all Evidence rows using
    `subject="<proposal_id>@<candidate_revision>"` and verdict `pass`, writes
    candidate/digest/result fields, appends the audit outbox event, and changes
    the proposal to `ready_for_review`. The transaction's active-fence predicate
    prevents expired work from publishing partial candidate database facts.
14. Return proposal ID, candidate revision, review digest, semantic Diff
    digest, evidence set digest, and evidence IDs for Worker completion.

For any terminal validation error, call `mark_validation_failed` only while
the lease is active, then raise `TerminalTaskError` with the stable code. If
the lease expired, propagate `StaleLeaseError`; a later Worker lease retries
the deterministic operation. A crash may leave an unreachable commit or a
proposal ref, but never a ready database row written by an expired token.

Every terminal stage persists the evidence available at that point: batch,
before snapshot, capability report, and execution log are always present;
after snapshot, text/semantic Diff, and ERC are added only after their stages
complete. The failed EvidenceSet lists exactly those items and their verdicts.
`mark_validation_failed` takes the same
`tuple[EvidenceRegistration, ...]` form as `mark_ready` and registers it in the
same fenced transaction as the terminal proposal state.

`ProposalStore.mark_ready` takes
`evidence: tuple[EvidenceRegistration, ...]`. It requires exactly one
registration for every required evidence kind plus `proposal_evidence_set`,
requires every descriptor/item pair to agree, verifies the EvidenceSet binds
the same project/task/proposal/base/candidate values, and inserts Artifact,
Evidence, proposal, and outbox rows in its one fenced transaction.

Use the existing `CandidateRevision` value and this exact two-argument
`resolve_proposal_ref(project_id, proposal_id)` implementation:

```python
def resolve_proposal_ref(
    self, project_id: str, proposal_id: str
) -> str | None:
    return self.git.resolve_ref(
        self.repo_path(project_id),
        f"refs/pcbflow/proposals/{proposal_id}",
    )
```

`GitCli.diff_worktree` must use
argument arrays, `--no-ext-diff`, `--binary`, `--no-renames`, and controlled
environment. It returns `ProcessResult.stdout_bytes`, rejects truncated output,
and never re-encodes a Diff through text. Snapshot manifests contain sorted relative path, file type,
size, and SHA-256 plus exclusion-policy version `1`.

Extend `KicadPort` with `probe`; existing fake implementations in tests must
return a deterministic `KicadCapability`. Wire `ProposalExecutor` as the
handler for `DESIGN_PROPOSAL_TASK_KIND` while retaining the existing read-only
validation handler.

Use these exact production dependencies in `build_container` so tests and the
runtime expose the same objects:

```python
selected_kicad: KicadPort = (
    kicad_override if kicad_override is not None else kicad
)
module_catalog = (
    FileModuleCatalog(
        settings.module_catalog_dir,
        max_files=settings.max_project_files,
        max_bytes=settings.max_project_bytes,
    )
    if settings.module_catalog_dir is not None
    else None
)
adapter = CstSchematicAdapter(module_catalog)
proposal_executor = ProposalExecutor(
    proposal_store=proposal_store,
    command_batches=command_batches,
    projects=projects,
    requirements=requirement_store,
    tasks=tasks,
    revisions=revisions,
    adapter=adapter,
    kicad=selected_kicad,
    artifacts=artifacts,
    evidence=evidence,
    clock=clock,
)
handlers = {
    VALIDATION_TASK_KIND: validation_handler,
    DESIGN_PROPOSAL_TASK_KIND: proposal_executor,
}
```

Add `proposal_executor: ProposalExecutor` to `Container`; use
`selected_kicad` for both validation and proposal execution rather than storing
or invoking a different real CLI instance when a test override is supplied.

- [ ] **Step 4: Run proposal, task, validation, and adapter regressions**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_artifacts.py tests/integration/test_proposals.py tests/integration/test_tasks.py tests/integration/test_validation.py tests/unit/test_schematic_adapter.py tests/unit/test_schematic_modules.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/pcbflow/proposals.py src/pcbflow/proposal_store.py src/pcbflow/artifacts.py src/pcbflow/revisions.py src/pcbflow/repositories.py src/pcbflow/validation.py src/pcbflow/kicad.py src/pcbflow/container.py tests/unit/test_artifacts.py tests/integration/test_proposals.py tests/integration/test_tasks.py tests/integration/test_validation.py
git commit -m "feat: execute fenced design proposals"
```

---

### Task 16: Accept, reject, stale, and reconcile proposal decisions

**Files:**
- Create: `tests/integration/test_proposal_decisions.py`
- Modify: `src/pcbflow/proposals.py`
- Modify: `src/pcbflow/proposal_store.py`
- Modify: `src/pcbflow/approvals.py`
- Modify: `src/pcbflow/revisions.py`
- Modify: `src/pcbflow/container.py`
- Modify: `tests/integration/test_reconciliation.py`

**Interfaces:**
- Produces: `ProposalDecisionService.accept(...) -> ChangeProposal`
- Produces: `ProposalDecisionService.reject(...) -> ChangeProposal`
- Produces: `CandidateNotReviewableError` and `RevisionReconciliationRequiredError`.
- Extends: `RevisionReconciler.run_once() -> int` to cover requirement, proposal, design-ref, and object consistency.
- Guarantees: acceptance advances SQLite first, Git `design` ref second; rejection and stale never advance the project revision.

- [ ] **Step 1: Write failing accept, reject, stale, idempotency, and recovery tests**

```python
# tests/integration/test_proposal_decisions.py
from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from pcbflow.approvals import ApprovalDigestMismatchError
from pcbflow.canonical import canonical_json_bytes
from pcbflow.proposals import (
    EvidenceItem,
    EvidenceRegistration,
    EvidenceSet,
    ProposalDecisionService,
    ProposalStatus,
    proposal_review_digest,
)
from pcbflow.repositories import RevisionConflictError
from pcbflow.revisions import RevisionReconciler

NOW = datetime(2026, 7, 29, 13, 0, tzinfo=UTC)


class FakeDecisionRevisions:
    def __init__(self) -> None:
        self.design_revision: str | None = None
        self.proposal_revision: str | None = None
        self.fail_next_design_update = False

    def object_exists(self, _project_id: str, revision: str) -> bool:
        return revision.startswith("git:")

    def resolve_proposal_ref(self, _project_id: str, _proposal_id: str):
        return self.proposal_revision

    def resolve_design_ref(self, _project_id: str):
        return self.design_revision

    def promote_design_ref(
        self,
        _project_id: str,
        revision: str,
        _expected_revision: str,
    ) -> None:
        if self.fail_next_design_update:
            self.fail_next_design_update = False
            raise RuntimeError("injected design ref failure")
        self.design_revision = revision


def _decision_service(container, revisions) -> ProposalDecisionService:
    reconciler = _reconciler(container, revisions)
    return ProposalDecisionService(
        proposal_store=container.proposal_store,
        command_batches=container.command_batches,
        projects=container.projects,
        requirements=container.requirement_store,
        revisions=revisions,
        artifacts=container.artifacts,
        evidence=container.evidence,
        reconciler=reconciler,
        clock=lambda: NOW,
    )


def _reconciler(container, revisions) -> RevisionReconciler:
    return RevisionReconciler(
        projects=container.projects,
        requirements=container.requirement_store,
        proposals=container.proposal_store,
        revisions=revisions,
        sessions=container.sessions,
        clock=lambda: NOW,
    )


def _batch(project, requirement_set, suffix: str) -> bytes:
    actor = {"type": "human", "id": "local-user"}
    symbol_ref = {
        "kind": "symbol",
        "sheet_uuid": "00000000-0000-0000-0000-000000000001",
        "object_uuid": "00000000-0000-0000-0000-000000000002",
        "pin_number": None,
    }
    value = {
        "schema_version": "1.0",
        "batch_id": f"bat_decision_{suffix}",
        "project_id": project.id,
        "base_revision": project.current_revision,
        "requirement_set_id": requirement_set.id,
        "idempotency_key": f"decision-{suffix}",
        "actor": actor,
        "intent": f"Decision fixture {suffix}",
        "risk": "low",
        "commands": [
            {
                "schema_version": "1.0",
                "command_id": f"cmd_decision_{suffix}",
                "batch_id": f"bat_decision_{suffix}",
                "project_id": project.id,
                "base_revision": project.current_revision,
                "idempotency_key": f"decision-{suffix}:1",
                "actor": actor,
                "intent": f"Decision fixture {suffix}",
                "risk": "low",
                "preconditions": [],
                "operation": {
                    "type": "schematic.set_property",
                    "payload": {
                        "subject_ref": symbol_ref,
                        "property_name": "Value",
                        "value": suffix,
                        "expected_old_value": "状态LED",
                    },
                },
                "required_validations": ["semantic_diff", "kicad_erc"],
                "provenance": {
                    "requirement_ids": ["REQ-FUNC-001"],
                    "evidence_ids": [],
                    "module_revision_ids": [],
                },
            }
        ],
    }
    return json.dumps(value, separators=(",", ":")).encode()


def _ready(container, frozen_requirement_set, suffix: str):
    project = container.projects.get(frozen_requirement_set.project_id)
    batch_bytes = _batch(project, frozen_requirement_set, suffix)
    proposal = container.proposals.create(
        batch_bytes, f"decision-{suffix}"
    )
    lease = container.tasks.claim_next("decision-worker", NOW, 60)
    assert lease is not None and lease.task_id == proposal.task_id
    container.tasks.start(lease.task_id, lease.lease_token, NOW)
    executing = container.proposal_store.begin_execution(
        proposal_id=proposal.id,
        task_id=proposal.task_id,
        lease_token=lease.lease_token,
        now=NOW,
    )
    assert executing.status is ProposalStatus.EXECUTING
    candidate_revision = "git:" + ({"accept": "a", "reject": "b", "stale": "c"}[suffix] * 40)
    candidate_snapshot_digest = "sha256:" + "d" * 64

    evidence_inputs = {
        "design_command_batch": (
            batch_bytes,
            "application/vnd.pcbflow.design-command-batch+json",
        ),
        "project_snapshot_before": (
            canonical_json_bytes({"schema_version": "1.0", "files": []}),
            "application/vnd.pcbflow.project-snapshot+json",
        ),
        "project_snapshot_after": (
            canonical_json_bytes(
                {
                    "schema_version": "1.0",
                    "files": [],
                    "digest": candidate_snapshot_digest,
                }
            ),
            "application/vnd.pcbflow.project-snapshot+json",
        ),
        "git_text_diff": (
            b"diff --git a/board.kicad_sch b/board.kicad_sch\n",
            "text/x-diff",
        ),
        "schematic_semantic_diff": (
            canonical_json_bytes(
                {
                    "schema_version": "1.0",
                    "changes": [
                        {
                            "kind": "symbol_property_changed",
                            "subject_ref": {
                                "kind": "symbol",
                                "sheet_uuid": "00000000-0000-0000-0000-000000000001",
                                "object_uuid": "00000000-0000-0000-0000-000000000002",
                                "pin_number": None,
                            },
                            "before": {"Value": "状态LED"},
                            "after": {"Value": suffix},
                            "field": "Value",
                            "command_id": f"cmd_decision_{suffix}",
                            "requirement_ids": ["REQ-FUNC-001"],
                            "risk": "low",
                        }
                    ],
                }
            ),
            "application/vnd.pcbflow.semantic-diff+json",
        ),
        "kicad_erc": (
            b'{"version":"1.0","source":"board.kicad_sch","violations":[]}',
            "application/json",
        ),
        "command_execution_log": (
            canonical_json_bytes(
                {"schema_version": "1.0", "commands": [], "result": "pass"}
            ),
            "application/vnd.pcbflow.command-log+json",
        ),
        "adapter_capability_report": (
            canonical_json_bytes(
                {
                    "adapter_contract": "pcbflow.schematic.cst.v1",
                    "kicad_major": 9,
                    "supported_operations": ["schematic.set_property"],
                }
            ),
            "application/vnd.pcbflow.adapter-capability+json",
        ),
    }
    registrations: list[EvidenceRegistration] = []
    for kind, (data, media_type) in evidence_inputs.items():
        descriptor = container.artifacts.put_bytes(data, media_type)
        registrations.append(
            EvidenceRegistration(
                descriptor=descriptor,
                item=EvidenceItem(
                    kind=kind,
                    artifact_digest=descriptor.digest,
                    media_type=descriptor.media_type,
                    verdict="pass",
                ),
            )
        )
    registrations.sort(key=lambda value: value.item.kind)
    semantic = next(
        value.descriptor
        for value in registrations
        if value.item.kind == "schematic_semantic_diff"
    )
    capability = next(
        value.descriptor
        for value in registrations
        if value.item.kind == "adapter_capability_report"
    )
    evidence_value = EvidenceSet(
        project_id=project.id,
        task_id=proposal.task_id,
        proposal_id=proposal.id,
        base_revision=project.current_revision,
        candidate_revision=candidate_revision,
        artifacts=tuple(value.item for value in registrations),
    )
    evidence_set = container.artifacts.put_bytes(
        canonical_json_bytes(evidence_value.model_dump(mode="json")),
        "application/vnd.pcbflow.evidence-set+json",
    )
    registrations.append(
        EvidenceRegistration(
            descriptor=evidence_set,
            item=EvidenceItem(
                kind="proposal_evidence_set",
                artifact_digest=evidence_set.digest,
                media_type=evidence_set.media_type,
                verdict="pass",
            ),
        )
    )
    review_digest = proposal_review_digest(
        proposal_id=proposal.id,
        project_id=project.id,
        base_revision=project.current_revision,
        candidate_revision=candidate_revision,
        candidate_snapshot_digest=candidate_snapshot_digest,
        requirement_set_digest=frozen_requirement_set.canonical_digest,
        semantic_diff_digest=semantic.digest,
        evidence_set_digest=evidence_set.digest,
        adapter_capability_digest=capability.digest,
    )
    ready = container.proposal_store.mark_ready(
        proposal_id=proposal.id,
        task_id=proposal.task_id,
        lease_token=lease.lease_token,
        now=NOW,
        candidate_revision=candidate_revision,
        candidate_snapshot_digest=candidate_snapshot_digest,
        review_digest=review_digest,
        semantic_diff_digest=semantic.digest,
        evidence_set_digest=evidence_set.digest,
        result={
            "validations": {
                "schema": "pass",
                "preconditions": "pass",
                "path_limits": "pass",
                "post_write_parse": "pass",
                "semantic_diff": "pass",
                "kicad_erc": "pass",
            },
            "adapter_capability_digest": capability.digest,
        },
        evidence=tuple(registrations),
    )
    container.tasks.complete(
        lease.task_id,
        lease.lease_token,
        {"proposal_id": proposal.id},
        NOW,
    )
    return project, ready


def test_accept_is_idempotent_and_advances_database_revision(
    container, frozen_requirement_set
) -> None:
    revisions = FakeDecisionRevisions()
    project, proposal = _ready(container, frozen_requirement_set, "accept")
    revisions.proposal_revision = proposal.candidate_revision
    revisions.design_revision = project.current_revision
    service = _decision_service(container, revisions)

    accepted = service.accept(
        proposal_id=proposal.id,
        candidate_digest=proposal.review_digest,
        actor_type="human",
        actor_id="local-user",
        comment="accepted",
        idempotency_key="accept-proposal",
    )
    repeated = service.accept(
        proposal_id=proposal.id,
        candidate_digest=proposal.review_digest,
        actor_type="human",
        actor_id="local-user",
        comment="accepted",
        idempotency_key="accept-proposal",
    )
    replayed_create = container.proposals.create(
        _batch(project, frozen_requirement_set, "accept"),
        "decision-accept",
    )

    assert accepted.status is ProposalStatus.ACCEPTED
    assert repeated.id == accepted.id
    assert replayed_create == accepted
    assert container.projects.get(project.id).current_revision == (
        proposal.candidate_revision
    )
    assert revisions.design_revision == proposal.candidate_revision
    assert any(
        item.kind == "approval_signature"
        for item in container.evidence.list_for_project(project.id)
    )


def test_reject_records_decision_without_advancing_revision(
    container, frozen_requirement_set
) -> None:
    revisions = FakeDecisionRevisions()
    project, proposal = _ready(container, frozen_requirement_set, "reject")
    revisions.proposal_revision = proposal.candidate_revision
    revisions.design_revision = project.current_revision
    rejected = _decision_service(container, revisions).reject(
        proposal_id=proposal.id,
        reason="not needed",
        actor_type="human",
        actor_id="local-user",
        idempotency_key="reject-proposal",
    )
    assert rejected.status is ProposalStatus.REJECTED
    assert container.projects.get(project.id).current_revision == project.current_revision
    assert revisions.design_revision == project.current_revision


def test_accept_with_changed_base_marks_proposal_stale(
    container, frozen_requirement_set
) -> None:
    revisions = FakeDecisionRevisions()
    project, proposal = _ready(container, frozen_requirement_set, "stale")
    revisions.proposal_revision = proposal.candidate_revision
    revisions.design_revision = project.current_revision
    container.projects.compare_and_set_revision(
        project.id,
        expected_revision=project.current_revision,
        new_revision="git:" + "9" * 40,
        snapshot_digest="sha256:" + "9" * 64,
        expected_version=project.version,
    )

    with pytest.raises(RevisionConflictError):
        _decision_service(container, revisions).accept(
            proposal_id=proposal.id,
            candidate_digest=proposal.review_digest,
            actor_type="human",
            actor_id="local-user",
            comment="late approval",
            idempotency_key="accept-stale",
        )
    assert container.proposal_store.get(proposal.id).status is ProposalStatus.STALE


def test_digest_mismatch_cannot_reuse_acceptance_key(
    container, frozen_requirement_set
) -> None:
    revisions = FakeDecisionRevisions()
    _project, proposal = _ready(container, frozen_requirement_set, "accept")
    revisions.proposal_revision = proposal.candidate_revision
    with pytest.raises(ApprovalDigestMismatchError):
        _decision_service(container, revisions).accept(
            proposal_id=proposal.id,
            candidate_digest="sha256:" + "0" * 64,
            actor_type="human",
            actor_id="local-user",
            comment="wrong digest",
            idempotency_key="wrong-digest",
        )
```

Append this recovery case to `tests/integration/test_proposal_decisions.py`,
where `FakeDecisionRevisions`, `_ready`, and `NOW` are already defined:

```python
def test_reconciler_repairs_design_ref_after_committed_acceptance(
    container, frozen_requirement_set
) -> None:
    revisions = FakeDecisionRevisions()
    project, proposal = _ready(container, frozen_requirement_set, "accept")
    revisions.proposal_revision = proposal.candidate_revision
    revisions.design_revision = project.current_revision
    revisions.fail_next_design_update = True
    service = _decision_service(container, revisions)

    accepted = service.accept(
        proposal_id=proposal.id,
        candidate_digest=proposal.review_digest,
        actor_type="human",
        actor_id="local-user",
        comment="accepted before injected ref failure",
        idempotency_key="accept-before-ref-failure",
    )
    assert accepted.status is ProposalStatus.ACCEPTED
    assert revisions.design_revision == project.current_revision

    reconciler = _reconciler(container, revisions)
    assert reconciler.run_once() == 1
    assert revisions.design_revision == proposal.candidate_revision
```

- [ ] **Step 2: Run decision and reconciliation tests and verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration/test_proposal_decisions.py tests/integration/test_reconciliation.py -q
```

Expected: FAIL because proposal decision transactions and full reconciliation
do not exist.

- [ ] **Step 3: Implement digest-bound decisions and recovery**

`ProposalDecisionService.accept` must:

1. Load proposal, batch, project, and RequirementSet.
2. Require `ready_for_review`, exact `candidate_digest == review_digest`, all
   mandatory validations recorded as pass, active frozen RequirementSet, a
   readable candidate object, and proposal ref equal to candidate revision.
   Open and strictly validate the canonical EvidenceSet; require exact
   project/task/proposal/base/candidate bindings, all required evidence kinds,
   matching semantic/capability digests, and a successfully verified CAS object
   for every item. Recompute `review_digest` from the stored fields and reject
   any mismatch as `CandidateNotReviewableError`.
3. Create canonical approval bytes containing schema version, gate
   `DESIGN_CHANGE`, proposal/project IDs, base/candidate revisions, review
   digest, decision, actor, comment, and timestamp. Store them in CAS before
   the database transaction.
4. Call one `ProposalStore.accept` transaction that compares project version
   and current revision, inserts the artifact row and `approval_signature`
   Evidence row, advances `Project.current_revision`, inserts
   `ProjectRevision`, appends `GateDecision`, marks proposal accepted, and
   appends `project.revision.accepted` outbox event.
5. After commit, compare-and-swap `refs/heads/design`. Log and leave the outbox
   event pending if this projection fails; do not roll back SQLite.

The acceptance transaction uses the decision key
`(project_id, idempotency_key)`. Same key and the same complete canonical
request returns the accepted proposal; any request field change raises
`IdempotencyConflictError` or `ApprovalDigestMismatchError`.
The canonical idempotency input excludes server-generated `created_at` and
artifact storage metadata. Check an existing key before calling `clock()` or
writing approval bytes; only a new key receives a timestamp, artifact, evidence
row, GateDecision, ProjectRevision, and outbox event.

If `Project.current_revision != proposal.base_revision`, the same transaction
marks the proposal `stale`, inserts a stale outbox event, commits, then raises
`RevisionConflictError`. It must not insert ProjectRevision or alter the
project revision.

`reject` requires `ready_for_review`, stores canonical rejection bytes, inserts
a `DESIGN_CHANGE` reject GateDecision and `rejection_decision` Evidence row,
marks the proposal rejected, and leaves Project unchanged. Rejection is also
full-input idempotent and follows the same generated-timestamp/artifact replay
rule as acceptance. The stale transition likewise writes at most one event for
one idempotency key.

Extend `RevisionReconciler.run_once` to scan these exact cases:

- for each managed project, if the design ref differs from the single database
  `Project.current_revision`, verify that exact ProjectRevision and its G1 or
  proposal acceptance facts, then compare-and-swap the ref once and count one
  repair; never iterate historical accepted proposals as competing targets;
- ready proposal whose candidate object/ref is missing or digest does not
  match: raise `RevisionReconciliationRequiredError`;
- pending RequirementSet whose candidate object/ref is missing: raise the same
  terminal error;
- a requirements ref for a still-`draft` RequirementSet is recoverable crash
  residue from submit-before-database; retain it for deterministic submit
  replay, while a requirements ref with no RequirementSet row is deduplicated
  as an unknown-ref audit event;
- managed Project whose database current revision object is missing: raise the
  same terminal error;
- proposal refs unknown to SQLite: record an audit/outbox event and leave them
  untouched for retention cleanup; deduplicate this event by exact
  `(project_id, ref_name, revision)` so automatic startup reconciliation does
  not append the same warning on every process start;
- a proposal ref whose proposal row is still `queued` or `executing` is a
  recoverable crash residue, not an unknown ref: leave it reachable so the
  deterministic executor replay can verify/reuse or replace it;
- an initialized managed repo with no design ref: recreate it from the database
  current revision after proving the object exists.

All write services call `reconciler.assert_writable(project_id)` before
creating new requirement candidates, command batches, or proposal decisions.
Revision materialization continues to use the database object ID directly.
Each service performs its read-only full-input idempotency lookup first; a
matching completed replay remains readable even when reconciliation currently
blocks new writes. Only a genuinely new key reaches `assert_writable`.

Build the production graph in this order and with these exact arguments:

```python
reconciler = RevisionReconciler(
    projects=projects,
    requirements=requirement_store,
    proposals=proposal_store,
    revisions=revisions,
    sessions=sessions,
    clock=clock,
)
requirements = RequirementService(
    store=requirement_store,
    projects=projects,
    revisions=revisions,
    reconciler=reconciler,
)
approvals = ApprovalService(
    requirement_store=requirement_store,
    projects=projects,
    revisions=revisions,
    reconciler=reconciler,
    clock=clock,
)
proposals = ProposalService(
    projects=projects,
    requirements=requirement_store,
    store=proposal_store,
    reconciler=reconciler,
)
proposal_decisions = ProposalDecisionService(
    proposal_store=proposal_store,
    command_batches=command_batches,
    projects=projects,
    requirements=requirement_store,
    revisions=revisions,
    artifacts=artifacts,
    evidence=evidence,
    reconciler=reconciler,
    clock=clock,
)
```

Add `reconciler: RevisionReconciler` and
`proposal_decisions: ProposalDecisionService` to `Container`. Extend the three
earlier write-service constructors with the shown `reconciler` dependency; do
not create a second reconciler inside any service.

After constructing the complete `Container`, call `reconciler.run_once()`
before returning it from `build_container`. A safe projection repair therefore
happens automatically on every API, CLI, and Worker process start. If object or
digest consistency cannot be proven, dispose the just-created engine and
propagate `RevisionReconciliationRequiredError`; never return a write-capable
partially reconciled container.

- [ ] **Step 4: Run decision, execution, G1, and recovery regressions**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration/test_proposal_decisions.py tests/integration/test_reconciliation.py tests/integration/test_proposals.py tests/integration/test_requirement_workflow.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/pcbflow/proposals.py src/pcbflow/proposal_store.py src/pcbflow/approvals.py src/pcbflow/revisions.py src/pcbflow/container.py tests/integration/test_proposal_decisions.py tests/integration/test_reconciliation.py
git commit -m "feat: decide and reconcile design proposals"
```

---

### Task 17: Expose the controlled-change workflow through REST

**Files:**
- Create: `tests/e2e/test_controlled_design_change.py`
- Modify: `src/pcbflow/api.py`
- Modify: `src/pcbflow/requirements.py`
- Modify: `src/pcbflow/proposals.py`
- Modify: `tests/e2e/test_api_cli.py`

**Interfaces:**
- Adds all Phase 2A REST routes from the design spec.
- Requires `Idempotency-Key` on adopt, requirement import/submit, approvals, proposal create/accept/reject.
- Uses request bodies for requirements and commands; no Phase 2A route accepts a module, footprint, or command filesystem path.
- Maps domain failures to the existing correlation-ID error envelope and stable status/error codes.

- [ ] **Step 1: Write failing route, strict-body, idempotency-header, and OpenAPI tests**

```python
# tests/e2e/test_controlled_design_change.py
from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import httpx
import yaml

from pcbflow.api import create_app
from pcbflow.config import Settings
from pcbflow.container import build_container
from pcbflow.kicad import KicadCapability, RawValidationReport

PASSING_ERC = b'{"version":"1.0","source":"board.kicad_sch","violations":[]}'


class FakeKicad9:
    def probe(self) -> KicadCapability:
        return KicadCapability(
            True,
            Path("kicad-cli"),
            "9.0.2",
            "sha256:" + "9" * 64,
            None,
        )

    def validate(
        self, project_dir: Path, output_dir: Path
    ) -> tuple[RawValidationReport, ...]:
        output_dir.mkdir(parents=True, exist_ok=True)
        return (
            RawValidationReport(
                kind="erc",
                data=PASSING_ERC,
                argv=("kicad-cli", "sch", "erc"),
                returncode=0,
                tool_version="9.0.2",
            ),
        )


def _client(container) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(container)),
        base_url="http://testserver",
    )


def _command_batch(project: dict, requirement_set: dict) -> dict:
    actor = {"type": "human", "id": "local-user"}
    return {
        "schema_version": "1.0",
        "batch_id": "bat_api_status_led",
        "project_id": project["id"],
        "base_revision": project["current_revision"],
        "requirement_set_id": requirement_set["id"],
        "idempotency_key": "api-proposal",
        "actor": actor,
        "intent": "Add the verified status LED",
        "risk": "medium",
        "commands": [
            {
                "schema_version": "1.0",
                "command_id": "cmd_api_status_led",
                "batch_id": "bat_api_status_led",
                "project_id": project["id"],
                "base_revision": project["current_revision"],
                "idempotency_key": "api-proposal:1",
                "actor": actor,
                "intent": "Add the verified status LED",
                "risk": "medium",
                "preconditions": [],
                "operation": {
                    "type": "schematic.instantiate_module",
                    "payload": {
                        "module_revision_id": "modrev_status_led_v1",
                        "instance_name": "STATUS_LED",
                        "target_sheet_ref": {
                            "kind": "sheet",
                            "sheet_uuid": "00000000-0000-0000-0000-000000000001",
                            "object_uuid": "00000000-0000-0000-0000-000000000001",
                            "pin_number": None,
                        },
                        "parameter_bindings": {"LED_VALUE": "GREEN"},
                        "port_bindings": {},
                        "placement_slot": "auto",
                    },
                },
                "required_validations": ["semantic_diff", "kicad_erc"],
                "provenance": {
                    "requirement_ids": ["REQ-FUNC-001"],
                    "evidence_ids": [],
                    "module_revision_ids": ["modrev_status_led_v1"],
                },
            }
        ],
    }


def test_rest_contract_covers_adopt_requirements_g1_and_proposal_queue(
    tmp_path: Path,
) -> None:
    fixtures = Path(__file__).resolve().parents[1] / "fixtures"
    source = tmp_path / "source"
    shutil.copytree(fixtures / "kicad" / "controlled-design", source)
    settings = Settings.from_env(
        {
            "PCBFLOW_DATA_DIR": str(tmp_path / "data"),
            "PCBFLOW_MODULE_CATALOG_DIR": str(fixtures / "modules"),
        }
    )
    container = build_container(settings, kicad_override=FakeKicad9())

    async def exercise() -> None:
        async with _client(container) as client:
            created = await client.post(
                "/api/v1/projects",
                headers={"Idempotency-Key": "api-create-project"},
                json={"name": "Controller", "source_path": str(source)},
            )
            project_id = created.json()["id"]
            adopted = await client.post(
                f"/api/v1/projects/{project_id}:adopt",
                headers={"Idempotency-Key": "api-adopt-project"},
            )
            assert adopted.status_code == 200
            project = adopted.json()
            assert project["mode"] == "managed"

            requirement_payload = yaml.safe_load(
                (fixtures / "requirements" / "reference-controller.yaml").read_text(
                    encoding="utf-8"
                )
            )
            imported = await client.post(
                f"/api/v1/projects/{project_id}/requirement-sets",
                headers={"Idempotency-Key": "api-import-requirements"},
                json=requirement_payload,
            )
            assert imported.status_code == 201
            assert imported.json()["subject_digest"] is None
            submitted = await client.post(
                f"/api/v1/requirement-sets/{imported.json()['id']}:submit",
                headers={"Idempotency-Key": "api-submit-requirements"},
            )
            assert submitted.json()["subject_digest"].startswith("sha256:")
            approved = await client.post(
                "/api/v1/approvals",
                headers={"Idempotency-Key": "api-approve-g1"},
                json={
                    "subject_type": "requirement_set",
                    "subject_id": submitted.json()["id"],
                    "subject_digest": submitted.json()["subject_digest"],
                    "decision": "approve",
                    "actor": {"type": "human", "id": "local-user"},
                    "comment": "approved",
                },
            )
            assert approved.json()["status"] == "frozen"
            assert approved.json()["subject_digest"] == submitted.json()[
                "subject_digest"
            ]
            project = (await client.get("/api/v1/projects")).json()[0]

            batch = _command_batch(project, approved.json())
            queued = await client.post(
                f"/api/v1/projects/{project_id}/proposals",
                headers={"Idempotency-Key": "api-proposal"},
                json=batch,
            )
            assert queued.status_code == 202
            proposal_id = queued.json()["id"]
            assert (await client.get(f"/api/v1/proposals/{proposal_id}")).status_code == 200

            missing_header = await client.post(
                f"/api/v1/projects/{project_id}/proposals", json=batch
            )
            assert missing_header.status_code == 422
            invalid = dict(batch)
            invalid["extra"] = True
            invalid_response = await client.post(
                f"/api/v1/projects/{project_id}/proposals",
                headers={"Idempotency-Key": "api-invalid-proposal"},
                json=invalid,
            )
            assert invalid_response.status_code == 422
            assert invalid_response.json()["error"]["code"] == (
                "DESIGN_COMMAND_SCHEMA_INVALID"
            )

            schema = create_app(container).openapi()
            routes = {
                f"{method.upper()} {path}"
                for path, operations in schema["paths"].items()
                for method in operations
                if method.lower() in {"get", "post", "put", "patch", "delete"}
            }
            assert {
                "POST /api/v1/projects/{project_id}:adopt",
                "POST /api/v1/projects/{project_id}/requirement-sets",
                "GET /api/v1/requirement-sets/{requirement_set_id}",
                "POST /api/v1/requirement-sets/{requirement_set_id}:submit",
                "POST /api/v1/approvals",
                "POST /api/v1/projects/{project_id}/proposals",
                "GET /api/v1/proposals/{proposal_id}",
                "GET /api/v1/proposals/{proposal_id}/diff",
                "POST /api/v1/proposals/{proposal_id}:accept",
                "POST /api/v1/proposals/{proposal_id}:reject",
            } <= routes

    try:
        asyncio.run(exercise())
    finally:
        container.dispose()
```

- [ ] **Step 2: Run REST tests and verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/e2e/test_controlled_design_change.py tests/e2e/test_api_cli.py -q
```

Expected: FAIL because Phase 2A routes and error mappings do not exist.

- [ ] **Step 3: Implement strict requests, routes, and stable error mapping**

Add strict request models:

```python
class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ActorRequest(StrictRequest):
    type: Literal["human", "service"]
    id: str = Field(min_length=1)


class ApprovalRequest(StrictRequest):
    subject_type: Literal["requirement_set"]
    subject_id: str = Field(min_length=1)
    subject_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    decision: Literal["approve", "reject"]
    actor: ActorRequest
    comment: str = Field(min_length=1)


class AcceptProposalRequest(StrictRequest):
    candidate_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    actor: ActorRequest
    comment: str = Field(min_length=1)


class RejectProposalRequest(StrictRequest):
    actor: ActorRequest
    reason: str = Field(min_length=1)
```

Implement the routes exactly as listed in the spec. Requirements import accepts
a JSON object and validates it with `RequirementSetPayload`; proposal create
accepts a JSON object, serializes it with `canonical_json_bytes`, and delegates
to `ProposalService.create`. Assert path `project_id` equals the validated body
project ID. Return semantic Diff by opening `proposal.semantic_diff_digest` and
decoding canonical JSON from Artifact Store.

Serialize RequirementSets through this helper so pending G1 responses and CLI
output expose the digest the reviewer must sign without storing a duplicate
field:

```python
def _requirement_response(requirement_set: RequirementSet) -> dict[str, object]:
    value = jsonable_encoder(requirement_set)
    value["subject_digest"] = (
        requirement_set.subject_digest()
        if requirement_set.candidate_revision is not None
        else None
    )
    return value
```

Use `_requirement_response` for every REST route that returns a RequirementSet:
import, get, submit, and both G1 approval outcomes. Drafts expose
`subject_digest: null`; pending, frozen, and rejected candidates expose the
digest of the exact candidate that was decided.

Change `RequirementService.submit` to
`submit(requirement_set_id: str, idempotency_key: str)`. Persist/compare the
submission key and complete input so repeated submit returns the same candidate
and a reused key for another set raises `IDEMPOTENCY_CONFLICT`. The CLI and API
must both pass it.

Add explicit exception handlers with these mappings:

| Domain error | HTTP | Code |
| --- | ---: | --- |
| requirement Pydantic/YAML validation | 422 | `REQUIREMENTS_SCHEMA_INVALID` |
| blocking assumption or incomplete verification | 422 | `REQUIREMENTS_BLOCKED` |
| `ApprovalDigestMismatchError` | 409 | `APPROVAL_DIGEST_MISMATCH` |
| `ProjectNotManagedError` | 409 | `PROJECT_NOT_MANAGED` |
| `ProjectWorktreeDirtyError` | 409 | `PROJECT_WORKTREE_DIRTY` |
| `RevisionConflictError` | 409 | `PROJECT_REVISION_CONFLICT` |
| `DesignCommandSchemaError` | 422 | `DESIGN_COMMAND_SCHEMA_INVALID` |
| failed/unknown precondition | 422 | `DESIGN_COMMAND_PRECONDITION_FAILED` |
| unsupported operation/capability | 422 | `DESIGN_COMMAND_UNSUPPORTED` |
| module or footprint revision missing | 404 | `MODULE_REVISION_NOT_FOUND` |
| CST/semantic parse error | 422 | `KICAD_PARSE_FAILED` |
| roundtrip mismatch | 422 | `KICAD_ROUNDTRIP_FAILED` |
| no semantic change | 422 | `DESIGN_COMMAND_NO_EFFECT` |
| failed mandatory validation | 422 | `CANDIDATE_VALIDATION_FAILED` |
| proposal state invalid | 409 | `CANDIDATE_NOT_REVIEWABLE` |
| Git failure | 503 or 422 by retryability | `GIT_OPERATION_FAILED` |
| terminal reconciliation issue | 409 | `REVISION_RECONCILIATION_REQUIRED` |

Retain the existing 409 `IDEMPOTENCY_CONFLICT` handler, but make its public
message generic (the operation conflicts with an existing idempotent result)
rather than claiming the same key was always reused. Adoption also uses this
code when a managed project's observed import content changed under a new key.

All handlers use `_error_response`; include stable `details` and user action,
never exception repr, local absolute paths, subprocess output, or stack traces.
Keep every existing route and response test passing. Remote mode continues to
reject project source paths, while requirements and commands remain body-only.

- [ ] **Step 4: Run REST, baseline API, and OpenAPI tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/e2e/test_controlled_design_change.py tests/e2e/test_api_cli.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/pcbflow/api.py src/pcbflow/requirements.py src/pcbflow/proposals.py tests/e2e/test_controlled_design_change.py tests/e2e/test_api_cli.py
git commit -m "feat: expose controlled changes over rest"
```

---

### Task 18: Add the complete Typer CLI workflow

**Files:**
- Modify: `src/pcbflow/cli.py`
- Modify: `tests/e2e/test_api_cli.py`
- Modify: `README.md`

**Interfaces:**
- Adds `project adopt`, `requirements import/show/submit`, `approval decide`, and `proposal create/show/diff/accept/reject`.
- Preserves `project add/list`, `validate`, `worker --once`, `task show`, `findings`, `evidence`, `doctor`, and `serve`.
- Provides `--json` on every read or workflow command and `--idempotency-key` on every create, submit, or decision command.

- [ ] **Step 1: Write failing CLI help and persisted workflow tests**

Append to `tests/e2e/test_api_cli.py`:

```python
import yaml

from pcbflow.kicad import KicadCapability


class FakeCliKicad:
    def probe(self) -> KicadCapability:
        return KicadCapability(
            True,
            Path("kicad-cli"),
            "9.0.2",
            "sha256:" + "9" * 64,
            None,
        )

    def validate(self, project_dir: Path, output_dir: Path):
        output_dir.mkdir(parents=True, exist_ok=True)
        return (
            RawValidationReport(
                kind="erc",
                data=b'{"version":"1.0","source":"board.kicad_sch","violations":[]}',
                argv=("kicad-cli", "sch", "erc"),
                returncode=0,
                tool_version="9.0.2",
            ),
        )


def test_phase_2a_cli_help_lists_all_command_groups() -> None:
    runner = CliRunner()
    root = runner.invoke(app, ["--help"])
    assert root.exit_code == 0
    for name in ("project", "requirements", "approval", "proposal", "worker"):
        assert name in root.output

    assert "adopt" in runner.invoke(app, ["project", "--help"]).output
    assert "import" in runner.invoke(app, ["requirements", "--help"]).output
    assert "decide" in runner.invoke(app, ["approval", "--help"]).output
    proposal_help = runner.invoke(app, ["proposal", "--help"]).output
    for name in ("create", "show", "diff", "accept", "reject"):
        assert name in proposal_help


def test_cli_runs_the_controlled_change_workflow(
    tmp_path: Path, monkeypatch
) -> None:
    fixtures = Path(__file__).resolve().parents[1] / "fixtures"
    source = tmp_path / "cli controlled source"
    shutil.copytree(fixtures / "kicad" / "controlled-design", source)
    data_dir = tmp_path / "cli-controlled-data"
    env = {
        "PCBFLOW_DATA_DIR": str(data_dir),
        "PCBFLOW_MODULE_CATALOG_DIR": str(fixtures / "modules"),
    }

    def build_for_test():
        return build_container(
            Settings.from_env(env),
            kicad_override=FakeCliKicad(),
        )

    monkeypatch.setattr("pcbflow.cli._build", build_for_test)
    runner = CliRunner()
    created = runner.invoke(
        app,
        [
            "project", "add", str(source),
            "--name", "Controller",
            "--idempotency-key", "cli-controlled-project",
            "--json",
        ],
        env=env,
    )
    assert created.exit_code == 0, created.output
    project = json.loads(created.stdout)

    adopted = runner.invoke(
        app,
        [
            "project", "adopt", project["id"],
            "--idempotency-key", "cli-adopt",
            "--json",
        ],
        env=env,
    )
    assert adopted.exit_code == 0, adopted.output

    requirements = runner.invoke(
        app,
        [
            "requirements", "import", project["id"],
            "--file", str(fixtures / "requirements" / "reference-controller.yaml"),
            "--idempotency-key", "cli-requirements",
            "--json",
        ],
        env=env,
    )
    requirement_set = json.loads(requirements.stdout)
    assert requirement_set["subject_digest"] is None
    submitted = runner.invoke(
        app,
        [
            "requirements", "submit", requirement_set["id"],
            "--idempotency-key", "cli-submit-requirements",
            "--json",
        ],
        env=env,
    )
    pending = json.loads(submitted.stdout)
    assert pending["subject_digest"].startswith("sha256:")
    approved = runner.invoke(
        app,
        [
            "approval", "decide", pending["id"],
            "--subject-digest", pending["subject_digest"],
            "--approve",
            "--actor-id", "local-user",
            "--comment", "approved",
            "--idempotency-key", "cli-approve-g1",
            "--json",
        ],
        env=env,
    )
    frozen = json.loads(approved.stdout)
    assert frozen["subject_digest"] == pending["subject_digest"]

    managed = json.loads(
        runner.invoke(app, ["project", "list", "--json"], env=env).stdout
    )[0]
    batch = _command_batch(managed, frozen)
    batch_file = tmp_path / "commands.json"
    batch_file.write_text(json.dumps(batch), encoding="utf-8")
    queued = runner.invoke(
        app,
        [
            "proposal", "create", managed["id"],
            "--file", str(batch_file),
            "--idempotency-key", "api-proposal",
            "--json",
        ],
        env=env,
    )
    proposal = json.loads(queued.stdout)
    worked = runner.invoke(app, ["worker", "--once", "--json"], env=env)
    assert json.loads(worked.stdout) == {"handled": True}
    shown = json.loads(
        runner.invoke(
            app, ["proposal", "show", proposal["id"], "--json"], env=env
        ).stdout
    )
    assert shown["status"] == "ready_for_review"
    diff = runner.invoke(
        app, ["proposal", "diff", proposal["id"], "--json"], env=env
    )
    assert json.loads(diff.stdout)["changes"]
    accepted = runner.invoke(
        app,
        [
            "proposal", "accept", proposal["id"],
            "--candidate-digest", shown["review_digest"],
            "--actor-id", "local-user",
            "--comment", "accepted",
            "--idempotency-key", "cli-accept-proposal",
            "--json",
        ],
        env=env,
    )
    assert json.loads(accepted.stdout)["status"] == "accepted"
```

Keep this exact helper in the test file so it does not import another test module:

```python
def _command_batch(project: dict, requirement_set: dict) -> dict:
    actor = {"type": "human", "id": "local-user"}
    return {
        "schema_version": "1.0",
        "batch_id": "bat_api_status_led",
        "project_id": project["id"],
        "base_revision": project["current_revision"],
        "requirement_set_id": requirement_set["id"],
        "idempotency_key": "api-proposal",
        "actor": actor,
        "intent": "Add the verified status LED",
        "risk": "medium",
        "commands": [
            {
                "schema_version": "1.0",
                "command_id": "cmd_api_status_led",
                "batch_id": "bat_api_status_led",
                "project_id": project["id"],
                "base_revision": project["current_revision"],
                "idempotency_key": "api-proposal:1",
                "actor": actor,
                "intent": "Add the verified status LED",
                "risk": "medium",
                "preconditions": [],
                "operation": {
                    "type": "schematic.instantiate_module",
                    "payload": {
                        "module_revision_id": "modrev_status_led_v1",
                        "instance_name": "STATUS_LED",
                        "target_sheet_ref": {
                            "kind": "sheet",
                            "sheet_uuid": "00000000-0000-0000-0000-000000000001",
                            "object_uuid": "00000000-0000-0000-0000-000000000001",
                            "pin_number": None,
                        },
                        "parameter_bindings": {"LED_VALUE": "GREEN"},
                        "port_bindings": {},
                        "placement_slot": "auto",
                    },
                },
                "required_validations": ["semantic_diff", "kicad_erc"],
                "provenance": {
                    "requirement_ids": ["REQ-FUNC-001"],
                    "evidence_ids": [],
                    "module_revision_ids": ["modrev_status_led_v1"],
                },
            }
        ],
    }
```

- [ ] **Step 2: Run CLI tests and verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/e2e/test_api_cli.py -q
```

Expected: FAIL because the new command groups are missing.

- [ ] **Step 3: Implement command groups and deterministic output**

Register these Typer groups:

```python
requirements_app = typer.Typer(no_args_is_help=True)
approval_app = typer.Typer(no_args_is_help=True)
proposal_app = typer.Typer(no_args_is_help=True)
app.add_typer(requirements_app, name="requirements")
app.add_typer(approval_app, name="approval")
app.add_typer(proposal_app, name="proposal")
```

Implement exact service calls:

```text
project adopt       -> container.revisions.adopt
requirements import -> container.requirements.import_draft
requirements show   -> container.requirement_store.get
requirements submit -> container.requirements.submit
approval decide     -> container.approvals.decide_g1
proposal create     -> container.proposals.create
proposal show       -> container.proposal_store.get
proposal diff       -> open semantic_diff_digest from Artifact Store
proposal accept     -> container.proposal_decisions.accept
proposal reject     -> container.proposal_decisions.reject
```

For file inputs, require an existing regular file, reject links/reparse points,
read at most `settings.max_project_bytes`, and pass raw bytes to the service.
Assert the proposal file's validated `project_id` equals the positional project
ID. `approval decide` requires exactly one of `--approve` and `--reject`;
`proposal reject` requires `--reason`; decisions default actor type to `human`
and require `--actor-id`.

Use `_emit` for every result. For `--json`, serialize `jsonable_encoder(value)`
with `ensure_ascii=False`, sorted keys, compact separators, and a trailing
newline. Human output must contain only stable IDs/status/digests, never local
paths or internal exceptions. Add one shared domain-error mapper that prints
`<STABLE_CODE>: <message>` to stderr and exits code `2`; it uses the same code
mapping table as REST.

For requirement `show`, `import`, `submit`, and the result of `approval decide`,
pass this view to `_emit` so approval scripts receive the signed digest:

```python
def _requirement_view(value: RequirementSet) -> dict[str, object]:
    result = jsonable_encoder(value)
    result["subject_digest"] = (
        value.subject_digest() if value.candidate_revision is not None else None
    )
    return result
```

Update README's quick-start command list in this task so `--help` and the
document do not diverge while later documentation work remains pending.

- [ ] **Step 4: Run CLI, REST, and legacy command regressions**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/e2e/test_api_cli.py tests/e2e/test_controlled_design_change.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/pcbflow/cli.py tests/e2e/test_api_cli.py README.md
git commit -m "feat: add controlled change cli workflow"
```

---

### Task 19: Add audit-grade structured observability and metrics

**Files:**
- Create: `src/pcbflow/observability.py`
- Create: `tests/unit/test_observability.py`
- Modify: `src/pcbflow/tasks.py`
- Modify: `src/pcbflow/validation.py`
- Modify: `src/pcbflow/requirements.py`
- Modify: `src/pcbflow/approvals.py`
- Modify: `src/pcbflow/proposals.py`
- Modify: `src/pcbflow/proposal_store.py`
- Modify: `src/pcbflow/repositories.py`
- Modify: `src/pcbflow/revisions.py`
- Modify: `src/pcbflow/schematic/adapter.py`
- Modify: `src/pcbflow/api.py`
- Modify: `src/pcbflow/cli.py`
- Modify: `src/pcbflow/container.py`
- Modify: `tests/integration/test_command_batches.py`
- Modify: `tests/integration/test_managed_projects.py`
- Modify: `tests/integration/test_proposals.py`
- Modify: `tests/integration/test_proposal_decisions.py`
- Modify: `tests/integration/test_reconciliation.py`

**Interfaces:**
- Produces: `bind_log_context(...)`, `ensure_trace_id() -> str`, `log_event(...)`, and `audit_payload(...)`.
- Produces: `MetricName`, `MetricKind`, `MetricPoint`, and thread-safe `Metrics`.
- Changes: `build_container(..., metrics: Metrics | None = None, monotonic: Callable[[], float] = time.monotonic) -> Container`.
- Adds: `Container.metrics: Metrics`.
- Guarantees: every candidate log can carry the eight required context fields, every audit outbox payload contains actor/action/object/before/after/result/trace data, and metric labels contain no workflow IDs or local paths.

- [ ] **Step 1: Write failing logging, audit, and metric tests**

```python
# tests/unit/test_observability.py
from __future__ import annotations

import logging

import pytest

from pcbflow.observability import (
    MetricKind,
    MetricName,
    Metrics,
    audit_payload,
    bind_log_context,
    log_event,
)


def test_metrics_aggregate_counters_and_durations_with_sorted_labels() -> None:
    metrics = Metrics()
    metrics.increment(
        MetricName.PROPOSAL_VALIDATION_TOTAL,
        labels={"result": "failed", "code": "CANDIDATE_VALIDATION_FAILED"},
    )
    metrics.observe(
        MetricName.PROPOSAL_EXECUTION_SECONDS,
        1.25,
        labels={"result": "failed"},
    )

    points = metrics.snapshot()
    validation = next(
        point
        for point in points
        if point.name is MetricName.PROPOSAL_VALIDATION_TOTAL
    )
    duration = next(
        point
        for point in points
        if point.name is MetricName.PROPOSAL_EXECUTION_SECONDS
    )
    assert validation.kind is MetricKind.COUNTER
    assert validation.labels == (
        ("code", "CANDIDATE_VALIDATION_FAILED"),
        ("result", "failed"),
    )
    assert validation.count == 1
    assert validation.total == 1.0
    assert duration.kind is MetricKind.HISTOGRAM
    assert duration.count == 1
    assert duration.total == 1.25
    assert duration.maximum == 1.25
    with pytest.raises(ValueError, match="unsupported metric label"):
        metrics.increment(
            MetricName.PROJECT_REVISION_CONFLICT_TOTAL,
            labels={"project_id": "prj_controller"},
        )


def test_structured_log_context_is_allowlisted_and_trace_bound(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger("pcbflow.observability.test")
    with caplog.at_level(logging.INFO), bind_log_context(
        project_id="prj_controller",
        requirement_set_id="reqset_controller",
        command_batch_id="bat_status_led",
        proposal_id="prop_status_led",
        base_revision="git:" + "1" * 40,
        candidate_revision="git:" + "2" * 40,
        task_id="tsk_status_led",
        trace_id="trc_test",
    ):
        log_event(logger, logging.INFO, "proposal.ready", result="pass")

    record = caplog.records[-1]
    assert record.event == "proposal.ready"
    assert record.project_id == "prj_controller"
    assert record.trace_id == "trc_test"
    assert record.result == "pass"
    assert "C:\\Users" not in record.getMessage()

    with pytest.raises(ValueError, match="unsupported log field"):
        with bind_log_context(source_path="C:/secret/project"):
            pass


def test_audit_payload_contains_complete_reviewable_context() -> None:
    with bind_log_context(trace_id="trc_audit"):
        payload = audit_payload(
            actor_type="human",
            actor_id="local-user",
            action="proposal.accept",
            object_type="change_proposal",
            object_id="prop_1",
            before_digest="sha256:" + "a" * 64,
            after_digest="sha256:" + "b" * 64,
            result="accepted",
        )
    assert payload == {
        "schema_version": "1.0",
        "trace_id": "trc_audit",
        "actor": {"type": "human", "id": "local-user"},
        "action": "proposal.accept",
        "object": {"type": "change_proposal", "id": "prop_1"},
        "before_digest": "sha256:" + "a" * 64,
        "after_digest": "sha256:" + "b" * 64,
        "result": "accepted",
    }
```

Wrap proposal creation in
`tests/integration/test_command_batches.py` with a fixed trace and assert the
queued outbox event is a complete audit record:

```python
from pcbflow.observability import bind_log_context

with bind_log_context(trace_id="trc_command_batch"):
    first = container.proposals.create(data, value["idempotency_key"])
repeated = container.proposals.create(data, value["idempotency_key"])

with container.sessions() as session:
    queued_event = session.scalar(
        select(OutboxEventRow).where(
            OutboxEventRow.aggregate_id == first.id,
            OutboxEventRow.event_type == "proposal.queued",
        )
    )
    assert queued_event is not None
    assert queued_event.payload_json["trace_id"] == "trc_command_batch"
    assert queued_event.payload_json["actor"] == {
        "type": "human",
        "id": "local-user",
    }
    assert queued_event.payload_json["action"] == "proposal.create"
    assert queued_event.payload_json["object"] == {
        "type": "change_proposal",
        "id": first.id,
    }
    assert queued_event.payload_json["result"] == "queued"
```

Append these assertions to the existing successful proposal execution test:

```python
metric_names = {point.name for point in container.metrics.snapshot()}
assert {
    MetricName.PROPOSAL_EXECUTION_SECONDS,
    MetricName.SCHEMATIC_PARSE_SECONDS,
    MetricName.KICAD_ERC_SECONDS,
    MetricName.PROPOSAL_VALIDATION_TOTAL,
    MetricName.ADAPTER_EXECUTION_TOTAL,
} <= metric_names
```

Import `MetricName` in that test file. In proposal decision tests, pass
`metrics=container.metrics` to `_decision_service` and `_reconciler`, then
assert an acceptance records `PROPOSAL_REVIEW_WAIT_SECONDS`, a stale acceptance
records `PROJECT_REVISION_CONFLICT_TOTAL`, and a repaired design ref records
`GIT_REF_RECONCILIATION_RETRY_TOTAL`.

The exact constructor additions in those helpers are:

```python
return ProposalDecisionService(
    proposal_store=container.proposal_store,
    command_batches=container.command_batches,
    projects=container.projects,
    requirements=container.requirement_store,
    revisions=revisions,
    artifacts=container.artifacts,
    evidence=container.evidence,
    reconciler=reconciler,
    metrics=container.metrics,
    clock=lambda: NOW,
)

return RevisionReconciler(
    projects=container.projects,
    requirements=container.requirement_store,
    proposals=container.proposal_store,
    revisions=revisions,
    sessions=container.sessions,
    metrics=container.metrics,
    clock=lambda: NOW,
)
```

- [ ] **Step 2: Run focused observability tests and verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_observability.py tests/integration/test_command_batches.py tests/integration/test_proposals.py tests/integration/test_proposal_decisions.py tests/integration/test_reconciliation.py -q
```

Expected: FAIL because the observability module, audit envelopes, metrics,
trace propagation, and instrumentation do not exist.

- [ ] **Step 3: Implement bounded structured context, audit payloads, and instrumentation**

Create the complete standard-library implementation:

```python
# src/pcbflow/observability.py
from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from threading import Lock

from pcbflow.domain import new_id

_LOG_FIELDS = frozenset(
    {
        "project_id",
        "requirement_set_id",
        "command_batch_id",
        "proposal_id",
        "base_revision",
        "candidate_revision",
        "task_id",
        "trace_id",
        "error_code",
        "adapter_contract",
        "result",
    }
)
_METRIC_LABELS = frozenset({"result", "code", "contract"})
_context: ContextVar[dict[str, str]] = ContextVar(
    "pcbflow_log_context", default={}
)


def _fields(values: Mapping[str, object]) -> dict[str, str]:
    unknown = set(values) - _LOG_FIELDS
    if unknown:
        raise ValueError(f"unsupported log field: {sorted(unknown)[0]}")
    return {
        key: str(value)
        for key, value in values.items()
        if value is not None
    }


@contextmanager
def bind_log_context(**values: object) -> Iterator[None]:
    merged = {**_context.get(), **_fields(values)}
    token = _context.set(merged)
    try:
        yield
    finally:
        _context.reset(token)


def ensure_trace_id() -> str:
    current = _context.get()
    if trace_id := current.get("trace_id"):
        return trace_id
    trace_id = new_id("trc")
    _context.set({**current, "trace_id": trace_id})
    return trace_id


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    **values: object,
) -> None:
    context = {**_context.get(), **_fields(values)}
    context.setdefault("trace_id", ensure_trace_id())
    logger.log(level, event, extra={"event": event, **context})


def audit_payload(
    *,
    actor_type: str,
    actor_id: str,
    action: str,
    object_type: str,
    object_id: str,
    before_digest: str | None,
    after_digest: str | None,
    result: str,
) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "trace_id": ensure_trace_id(),
        "actor": {"type": actor_type, "id": actor_id},
        "action": action,
        "object": {"type": object_type, "id": object_id},
        "before_digest": before_digest,
        "after_digest": after_digest,
        "result": result,
    }


class MetricName(StrEnum):
    PROPOSAL_EXECUTION_SECONDS = "proposal_execution_duration_seconds"
    SCHEMATIC_PARSE_SECONDS = "schematic_parse_duration_seconds"
    KICAD_ERC_SECONDS = "kicad_erc_duration_seconds"
    PROPOSAL_VALIDATION_TOTAL = "proposal_validation_total"
    PROJECT_REVISION_CONFLICT_TOTAL = "project_revision_conflict_total"
    PROPOSAL_REVIEW_WAIT_SECONDS = "proposal_review_wait_seconds"
    GIT_REF_RECONCILIATION_RETRY_TOTAL = (
        "git_ref_reconciliation_retry_total"
    )
    ADAPTER_EXECUTION_TOTAL = "adapter_contract_execution_total"


class MetricKind(StrEnum):
    COUNTER = "counter"
    HISTOGRAM = "histogram"


@dataclass(frozen=True, slots=True)
class MetricPoint:
    name: MetricName
    kind: MetricKind
    labels: tuple[tuple[str, str], ...]
    count: int
    total: float
    maximum: float


@dataclass(slots=True)
class _Aggregate:
    count: int = 0
    total: float = 0.0
    maximum: float = 0.0


class Metrics:
    def __init__(self) -> None:
        self._lock = Lock()
        self._values: dict[
            tuple[MetricName, MetricKind, tuple[tuple[str, str], ...]],
            _Aggregate,
        ] = {}

    @staticmethod
    def _labels(
        labels: Mapping[str, str] | None,
    ) -> tuple[tuple[str, str], ...]:
        values = labels or {}
        unknown = set(values) - _METRIC_LABELS
        if unknown:
            raise ValueError(
                f"unsupported metric label: {sorted(unknown)[0]}"
            )
        return tuple(
            sorted((str(key), str(value)) for key, value in values.items())
        )

    def _record(
        self,
        name: MetricName,
        kind: MetricKind,
        value: float,
        labels: Mapping[str, str] | None,
    ) -> None:
        if value < 0:
            raise ValueError("metric value must be non-negative")
        label_key = self._labels(labels)
        key = (name, kind, label_key)
        with self._lock:
            aggregate = self._values.setdefault(key, _Aggregate())
            aggregate.count += 1
            aggregate.total += value
            aggregate.maximum = max(aggregate.maximum, value)

    def increment(
        self,
        name: MetricName,
        value: float = 1.0,
        *,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        self._record(name, MetricKind.COUNTER, value, labels)

    def observe(
        self,
        name: MetricName,
        value: float,
        *,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        self._record(name, MetricKind.HISTOGRAM, value, labels)

    def snapshot(self) -> tuple[MetricPoint, ...]:
        with self._lock:
            points = [
                MetricPoint(
                    name=name,
                    kind=kind,
                    labels=labels,
                    count=value.count,
                    total=value.total,
                    maximum=value.maximum,
                )
                for (name, kind, labels), value in self._values.items()
            ]
        return tuple(
            sorted(
                points,
                key=lambda point: (
                    point.name.value,
                    point.kind.value,
                    point.labels,
                ),
            )
        )
```

Apply this exact instrumentation matrix:

| Location | Context / metric behavior |
| --- | --- |
| API correlation middleware | Bind `trace_id=request.state.correlation_id` for the complete request. |
| CLI command entry | Call `ensure_trace_id()` before building services or invoking a workflow command. |
| `ValidationService.enqueue` and `ProposalStore.create_queued` | Store `trace_id` in task payloads. |
| `Worker.run_once` | Bind `task_id`, task `project_id`, and payload `trace_id`; replace `logger.exception` with `log_event(..., error_code="UNHANDLED_TASK_ERROR", result="failed")` and never log exception text or paths. |
| `ProposalExecutor` | Bind project/requirement/batch/proposal/base/task fields, time the entire call in `finally`, time ERC separately, increment validation `{result="pass"|"failed", code=<stable code>}`, and log candidate revision only after it exists. |
| `CstSchematicAdapter.inspect/apply` | Observe parse duration; increment adapter execution with labels `{contract="pcbflow.schematic.cst.v1", result="pass"|"failed"}`. |
| Requirement/proposal creation and decisions | Increment revision-conflict counter exactly where `RevisionConflictError` is raised. |
| Proposal accept/reject | Observe `max(0, (clock() - proposal.updated_at).total_seconds())` before the decision transaction. |
| Reconciler | Increment the Git-ref retry counter before each compare-and-swap repair attempt, including a failed attempt; log only IDs, revisions, stable result, and trace. |

Use `audit_payload` as the base payload for every new Phase 2A outbox event,
including the Task 4 `project.adopted` event. Update its repository test to
assert the required audit keys while retaining `source_head` provenance.
Add event-specific IDs and digests beside those required audit keys, but never
remove them. Actor mapping is exact: command batch actor for proposal creation,
decision actor for G1/accept/reject, and `{type: "service", id: "pcbflow"}` for
execution/reconciliation events. The `before_digest`/`after_digest` pairs are:

- proposal queued: `None` / command batch canonical digest;
- proposal ready or validation failed: command batch digest / review digest or evidence-set digest;
- G1 accepted/rejected: signed G1 subject digest / frozen revision snapshot digest or `None`;
- proposal accepted/rejected/stale: review digest / candidate snapshot digest or `None`;
- reconciliation repair: database revision / resulting Git ref revision.

Extend constructors only by keyword. Use one shared metrics instance:

```python
def build_container(
    settings: Settings,
    kicad_override: KicadPort | None = None,
    clock: Callable[[], datetime] = utc_now,
    *,
    metrics: Metrics | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> Container:
    metric_sink = metrics if metrics is not None else Metrics()
```

Pass `metric_sink` to the adapter, executor, decision services, validation
handler, and reconciler; pass `monotonic` only to code that records durations.
Store `metrics=metric_sink` in `Container`. Update the Task 16 test helpers to
pass `metrics=container.metrics`; no service creates its own metrics object.

- [ ] **Step 4: Run observability and workflow regressions**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_observability.py tests/integration/test_command_batches.py tests/integration/test_proposals.py tests/integration/test_proposal_decisions.py tests/integration/test_reconciliation.py tests/e2e/test_controlled_design_change.py -q
```

Expected: PASS. Audit payloads contain all required fields, structured logs
contain no absolute path field or arbitrary exception text, and all eight
metric families have at least one focused test.

- [ ] **Step 5: Commit**

```powershell
git add src/pcbflow/observability.py src/pcbflow/tasks.py src/pcbflow/validation.py src/pcbflow/requirements.py src/pcbflow/approvals.py src/pcbflow/proposals.py src/pcbflow/proposal_store.py src/pcbflow/repositories.py src/pcbflow/revisions.py src/pcbflow/schematic/adapter.py src/pcbflow/api.py src/pcbflow/cli.py src/pcbflow/container.py tests/unit/test_observability.py tests/integration/test_command_batches.py tests/integration/test_managed_projects.py tests/integration/test_proposals.py tests/integration/test_proposal_decisions.py tests/integration/test_reconciliation.py
git commit -m "feat: add controlled change observability"
```

---

### Task 20: Verify managed revisions end-to-end, inject failures, and finish docs

**Files:**
- Create: `tests/contract/test_kicad_schematic_write.py`
- Create: `tests/unit/test_schematic_goldens.py`
- Create: `tests/fixtures/kicad/golden/blank/blank.kicad_sch`
- Create: `tests/fixtures/kicad/golden/simple/simple.kicad_sch`
- Create: `tests/fixtures/kicad/golden/hierarchical/root.kicad_sch`
- Create: `tests/fixtures/kicad/golden/hierarchical/child/child.kicad_sch`
- Create: `tests/fixtures/kicad/golden/unicode/unicode.kicad_sch`
- Create: `tests/fixtures/kicad/golden/multi-unit/multi-unit.kicad_sch`
- Create: `tests/fixtures/kicad/golden/custom-properties/custom-properties.kicad_sch`
- Create: `tests/fixtures/kicad/golden/erc-error/erc-error.kicad_sch`
- Modify: `src/pcbflow/validation.py`
- Modify: `src/pcbflow/proposals.py`
- Modify: `src/pcbflow/approvals.py`
- Modify: `src/pcbflow/container.py`
- Modify: `tests/integration/test_validation.py`
- Modify: `tests/e2e/test_controlled_design_change.py`
- Modify: `tests/contract/test_kicad_cli.py`
- Modify: `README.md`
- Modify: `docs/DEVELOPMENT_GUIDE.md`

**Interfaces:**
- Changes: managed read-only validation materializes `Project.current_revision`; registered projects retain safe-copy validation.
- Produces: `FaultPoint`, `FaultInjector`, and `NoFaults` testable recovery hooks.
- Verifies: restart consistency, source immutability, golden CST/IR behavior, real KiCad 9 parse/ERC, legacy compatibility, and coverage >= 90%.

- [ ] **Step 1: Write failing managed-validation, restart, fault-injection, and real-KiCad tests**

Append to `tests/integration/test_validation.py`:

```python
def test_managed_validation_reads_database_revision_not_changed_import_source(
    tmp_path: Path,
) -> None:
    fixtures = Path(__file__).resolve().parents[1] / "fixtures"
    source = tmp_path / "source"
    shutil.copytree(fixtures / "kicad" / "controlled-design", source)
    expected_schematic = (source / "board.kicad_sch").read_bytes()
    settings = Settings.from_env({"PCBFLOW_DATA_DIR": str(tmp_path / "data")})

    class InspectingKicad(FakeKicad):
        def validate(self, project_dir: Path, output_dir: Path):
            assert (project_dir / "board.kicad_sch").read_bytes() == expected_schematic
            return super().validate(project_dir, output_dir)

    kicad = InspectingKicad(fixtures / "kicad", source)
    container = build_container(settings, kicad_override=kicad)
    try:
        project = container.projects.create("Controller", source, "managed-validation")
        managed = container.revisions.adopt(project.id, "managed-validation-adopt")
        (source / "board.kicad_sch").write_bytes(b"changed outside pcbflow")

        task = container.validation.enqueue(managed.id, "managed-validation-run")
        assert container.worker.run_once()
        assert container.tasks.get(task.id).status is TaskStatus.SUCCEEDED
    finally:
        container.dispose()
```

Append to `tests/e2e/test_controlled_design_change.py`:

```python
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi.encoders import jsonable_encoder
from sqlalchemy import func, select

from pcbflow.container import Container
from pcbflow.design_tables import GateDecisionRow, ProjectRevisionRow
from pcbflow.proposals import (
    ChangeProposal,
    FaultInjector,
    FaultPoint,
    NoFaults,
)

NOW = datetime(2026, 7, 29, 14, 0, tzinfo=UTC)


def _snapshot(root: Path) -> dict[Path, bytes]:
    return {
        path.relative_to(root): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class CrashOnce:
    def __init__(self, point: FaultPoint) -> None:
        self.point = point
        self.triggered = False

    def hit(self, point: FaultPoint) -> None:
        if point is self.point and not self.triggered:
            self.triggered = True
            raise RuntimeError(f"injected crash at {point.value}")


@dataclass(slots=True)
class MutableClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value


@dataclass(slots=True)
class ProposalScenario:
    container: Container
    settings: Settings
    clock: MutableClock
    source: Path
    source_before: dict[Path, bytes]
    project_id: str
    proposal: ChangeProposal
    batch_bytes: bytes


def _proposal_scenario(
    tmp_path: Path, faults: FaultInjector
) -> ProposalScenario:
    fixtures = Path(__file__).resolve().parents[1] / "fixtures"
    settings = Settings.from_env(
        {
            "PCBFLOW_DATA_DIR": str(tmp_path / "fault-data"),
            "PCBFLOW_MODULE_CATALOG_DIR": str(fixtures / "modules"),
        }
    )
    source = tmp_path / "fault-source"
    shutil.copytree(fixtures / "kicad" / "controlled-design", source)
    source_before = _snapshot(source)
    clock = MutableClock(NOW)
    container = build_container(
        settings,
        kicad_override=FakeKicad9(),
        clock=clock,
        faults=faults,
    )
    project = container.projects.create("Controller", source, "fault-project")
    managed = container.revisions.adopt(project.id, "fault-adopt")
    payload = (
        fixtures / "requirements" / "reference-controller.yaml"
    ).read_bytes()
    draft = container.requirements.import_draft(
        managed.id, payload, "fault-requirements"
    )
    pending = container.requirements.submit(draft.id, "fault-submit")
    frozen = container.approvals.decide_g1(
        requirement_set_id=pending.id,
        subject_digest=pending.subject_digest(),
        decision="approve",
        actor_type="human",
        actor_id="local-user",
        comment="approved",
        idempotency_key="fault-g1",
    )
    current = container.projects.get(managed.id)
    batch_bytes = json.dumps(
        _command_batch(jsonable_encoder(current), jsonable_encoder(frozen)),
        separators=(",", ":"),
    ).encode()
    proposal = container.proposals.create(batch_bytes, "api-proposal")
    return ProposalScenario(
        container=container,
        settings=settings,
        clock=clock,
        source=source,
        source_before=source_before,
        project_id=managed.id,
        proposal=proposal,
        batch_bytes=batch_bytes,
    )


def test_restart_after_accept_keeps_database_git_evidence_and_validation_consistent(
    tmp_path: Path,
) -> None:
    fixtures = Path(__file__).resolve().parents[1] / "fixtures"
    settings = Settings.from_env(
        {
            "PCBFLOW_DATA_DIR": str(tmp_path / "restart-data"),
            "PCBFLOW_MODULE_CATALOG_DIR": str(fixtures / "modules"),
        }
    )
    source = tmp_path / "restart-source"
    shutil.copytree(fixtures / "kicad" / "controlled-design", source)
    source_before = _snapshot(source)
    first = build_container(settings, kicad_override=FakeKicad9())
    try:
        project = first.projects.create("Controller", source, "restart-project")
        managed = first.revisions.adopt(project.id, "restart-adopt")
        payload = (
            fixtures / "requirements" / "reference-controller.yaml"
        ).read_bytes()
        draft = first.requirements.import_draft(
            managed.id, payload, "restart-requirements"
        )
        pending = first.requirements.submit(draft.id, "restart-submit")
        frozen = first.approvals.decide_g1(
            requirement_set_id=pending.id,
            subject_digest=pending.subject_digest(),
            decision="approve",
            actor_type="human",
            actor_id="local-user",
            comment="approved",
            idempotency_key="restart-g1",
        )
        current = first.projects.get(managed.id)
        batch = _command_batch(
            jsonable_encoder(current), jsonable_encoder(frozen)
        )
        proposal = first.proposals.create(
            json.dumps(batch).encode(), "api-proposal"
        )
        assert first.worker.run_once()
        ready = first.proposal_store.get(proposal.id)
        first.proposal_decisions.accept(
            proposal_id=ready.id,
            candidate_digest=ready.review_digest,
            actor_type="human",
            actor_id="local-user",
            comment="accepted",
            idempotency_key="restart-accept",
        )
        accepted_revision = first.projects.get(managed.id).current_revision
    finally:
        first.dispose()

    second = build_container(settings, kicad_override=FakeKicad9())
    try:
        assert second.reconciler.run_once() == 0
        project = second.projects.get(managed.id)
        assert project.current_revision == accepted_revision
        assert second.revisions.resolve_design_ref(project.id) == accepted_revision
        assert second.proposal_store.get(proposal.id).status.value == "accepted"
        assert all(
            second.artifacts.verify(item.artifact_digest)
            for item in second.evidence.list_for_project(project.id)
        )
        validation = second.validation.enqueue(project.id, "restart-read-only-validation")
        assert second.worker.run_once()
        assert second.tasks.get(validation.id).status is TaskStatus.SUCCEEDED
        assert _snapshot(source) == source_before
    finally:
        second.dispose()
```

Use this exact parameterized crash test for the four execution fault points:

```python
@pytest.mark.parametrize(
    "point",
    [
        FaultPoint.BEFORE_CANDIDATE_COMMIT,
        FaultPoint.AFTER_PROPOSAL_REF_BEFORE_DATABASE,
        FaultPoint.DURING_DIFF_ARTIFACT_SAVE,
        FaultPoint.AFTER_EXECUTION_BEFORE_FINAL_FENCE,
    ],
)
def test_crashed_proposal_execution_retries_without_duplicate_candidate(
    tmp_path: Path, point: FaultPoint
) -> None:
    scenario = _proposal_scenario(tmp_path, faults=CrashOnce(point))
    try:
        lease = scenario.container.tasks.claim_next("crashing-worker", NOW, 1)
        assert lease is not None
        scenario.container.tasks.start(lease.task_id, lease.lease_token, NOW)
        with pytest.raises(RuntimeError, match="injected crash"):
            scenario.container.proposal_executor(lease)

        after_expiry = lease.lease_expires_at + timedelta(seconds=1)
        scenario.clock.value = after_expiry
        assert scenario.container.worker.run_once()
        ready = scenario.container.proposal_store.get(scenario.proposal.id)
        assert ready.status.value == "ready_for_review"
        assert scenario.container.revisions.resolve_proposal_ref(
            ready.project_id, ready.id
        ) == ready.candidate_revision
        repeated = scenario.container.proposals.create(
            scenario.batch_bytes, "api-proposal"
        )
        assert repeated.id == ready.id
        assert _snapshot(scenario.source) == scenario.source_before
    finally:
        scenario.container.dispose()


def test_acceptance_crash_is_repaired_without_duplicate_database_rows(
    tmp_path: Path,
) -> None:
    scenario = _proposal_scenario(
        tmp_path,
        faults=CrashOnce(FaultPoint.AFTER_ACCEPT_DATABASE_BEFORE_DESIGN_REF),
    )
    first = scenario.container
    try:
        base_revision = first.projects.get(scenario.project_id).current_revision
        assert first.worker.run_once()
        ready = first.proposal_store.get(scenario.proposal.id)
        with pytest.raises(RuntimeError, match="injected crash"):
            first.proposal_decisions.accept(
                proposal_id=ready.id,
                candidate_digest=ready.review_digest,
                actor_type="human",
                actor_id="local-user",
                comment="accepted before crash",
                idempotency_key="fault-accept",
            )
        accepted_revision = first.projects.get(scenario.project_id).current_revision
        assert accepted_revision == ready.candidate_revision
        assert first.revisions.resolve_design_ref(scenario.project_id) == base_revision
        with first.sessions() as session:
            decision_count = session.scalar(
                select(func.count()).select_from(GateDecisionRow)
            )
            revision_count = session.scalar(
                select(func.count()).select_from(ProjectRevisionRow)
            )
    finally:
        first.dispose()

    second = build_container(
        scenario.settings,
        kicad_override=FakeKicad9(),
        clock=scenario.clock,
        faults=NoFaults(),
    )
    try:
        assert second.reconciler.run_once() == 0
        assert second.revisions.resolve_design_ref(scenario.project_id) == (
            accepted_revision
        )
        with second.sessions() as session:
            assert session.scalar(
                select(func.count()).select_from(GateDecisionRow)
            ) == decision_count
            assert session.scalar(
                select(func.count()).select_from(ProjectRevisionRow)
            ) == revision_count
        assert _snapshot(scenario.source) == scenario.source_before
    finally:
        second.dispose()
```

Replace the existing skip-producing test in `tests/contract/test_kicad_cli.py`
with this optional locator contract. Real installation/version/ERC coverage is
owned by the single test below, so an absent KiCad installation produces one
skip for the whole suite:

```python
def test_kicad_locator_is_optional_and_returns_a_file_when_present() -> None:
    executable = KicadCli.locate()
    if executable is not None:
        assert executable.is_file()
```

Create `tests/contract/test_kicad_schematic_write.py` with one real-KiCad test
that probes once, validates the controlled write, and loops over all goldens:

```python
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from pcbflow.commands import load_command_batch
from pcbflow.kicad import KicadCli, parse_kicad_report
from pcbflow.process import ProcessRunner
from pcbflow.schematic.adapter import CstSchematicAdapter
from pcbflow.schematic.modules import FileModuleCatalog


@pytest.mark.kicad
def test_real_kicad9_validates_controlled_write_and_all_goldens(
    tmp_path: Path,
) -> None:
    executable = KicadCli.locate()
    if executable is None:
        pytest.skip("kicad-cli is not installed")
    kicad = KicadCli(ProcessRunner(2_000_000), executable, 120)
    capability = kicad.probe()
    if (
        not capability.available
        or capability.version is None
        or not capability.version.startswith("9.")
    ):
        pytest.skip("supported KiCad 9 CLI is not available")

    fixtures = Path(__file__).resolve().parents[1] / "fixtures"
    project = tmp_path / "KiCad 9 controlled candidate"
    shutil.copytree(fixtures / "kicad" / "controlled-design", project)
    adapter = CstSchematicAdapter(
        FileModuleCatalog(fixtures / "modules", max_files=32, max_bytes=2_000_000)
    )
    command = load_command_batch(
        json.dumps(_real_kicad_batch()).encode()
    ).commands[0]
    adapter.apply(project, (command,))
    _assert_erc(kicad, project, tmp_path / "erc-controlled", False)

    cases = (
        ("blank", False),
        ("simple", False),
        ("hierarchical", False),
        ("unicode", False),
        ("multi-unit", False),
        ("custom-properties", False),
        ("erc-error", True),
    )
    golden_root = fixtures / "kicad" / "golden"
    for directory, expect_findings in cases:
        golden = tmp_path / f"golden {directory}"
        shutil.copytree(golden_root / directory, golden)
        _assert_erc(
            kicad,
            golden,
            tmp_path / f"erc-{directory}",
            expect_findings,
        )


def _assert_erc(
    kicad: KicadCli,
    project: Path,
    output: Path,
    expect_findings: bool,
) -> None:
    reports = kicad.validate(project, output)
    erc = [item for item in reports if item.kind == "erc"]
    assert len(erc) == 1
    findings = parse_kicad_report("erc", erc[0].data).findings
    assert bool(findings) is expect_findings
```

Keep this exact batch helper in the same file:

```python
def _real_kicad_batch() -> dict[str, object]:
    actor = {"type": "human", "id": "contract-test"}
    return {
        "schema_version": "1.0",
        "batch_id": "bat_real_kicad",
        "project_id": "prj_real_kicad",
        "base_revision": "git:" + "1" * 40,
        "requirement_set_id": "reqset_real_kicad",
        "idempotency_key": "real-kicad",
        "actor": actor,
        "intent": "Instantiate the verified module under real KiCad",
        "risk": "medium",
        "commands": [
            {
                "schema_version": "1.0",
                "command_id": "cmd_real_kicad",
                "batch_id": "bat_real_kicad",
                "project_id": "prj_real_kicad",
                "base_revision": "git:" + "1" * 40,
                "idempotency_key": "real-kicad:1",
                "actor": actor,
                "intent": "Instantiate the verified module under real KiCad",
                "risk": "medium",
                "preconditions": [],
                "operation": {
                    "type": "schematic.instantiate_module",
                    "payload": {
                        "module_revision_id": "modrev_status_led_v1",
                        "instance_name": "STATUS_LED",
                        "target_sheet_ref": {
                            "kind": "sheet",
                            "sheet_uuid": "00000000-0000-0000-0000-000000000001",
                            "object_uuid": "00000000-0000-0000-0000-000000000001",
                            "pin_number": None,
                        },
                        "parameter_bindings": {"LED_VALUE": "GREEN"},
                        "port_bindings": {},
                        "placement_slot": "auto",
                    },
                },
                "required_validations": ["semantic_diff", "kicad_erc"],
                "provenance": {
                    "requirement_ids": ["REQ-FUNC-001"],
                    "evidence_ids": [],
                    "module_revision_ids": ["modrev_status_led_v1"],
                },
            }
        ],
    }
```

This single contract test is the one expected skip when KiCad is absent; it
must run, not xfail, when KiCad 9 is installed.

Create `tests/unit/test_schematic_goldens.py` with the complete golden matrix:

```python
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from pcbflow.commands import RiskLevel, load_command_batch
from pcbflow.schematic.adapter import CstSchematicAdapter
from pcbflow.schematic.cst import apply_edits, parse_cst
from pcbflow.schematic.diff import CommandAttribution, build_semantic_diff
from pcbflow.schematic.modules import FileModuleCatalog

GOLDENS = (
    ("blank", "blank.kicad_sch"),
    ("simple", "simple.kicad_sch"),
    ("hierarchical", "root.kicad_sch"),
    ("unicode", "unicode.kicad_sch"),
    ("multi-unit", "multi-unit.kicad_sch"),
    ("custom-properties", "custom-properties.kicad_sch"),
    ("erc-error", "erc-error.kicad_sch"),
)


def _set_value_command(project_id: str, subject_ref, old: str):
    actor = {"type": "human", "id": "golden-test"}
    value = {
        "schema_version": "1.0",
        "batch_id": "bat_golden_edit",
        "project_id": project_id,
        "base_revision": "git:" + "1" * 40,
        "requirement_set_id": "reqset_golden",
        "idempotency_key": "golden-edit",
        "actor": actor,
        "intent": "Edit one golden symbol value",
        "risk": "low",
        "commands": [
            {
                "schema_version": "1.0",
                "command_id": "cmd_golden_edit",
                "batch_id": "bat_golden_edit",
                "project_id": project_id,
                "base_revision": "git:" + "1" * 40,
                "idempotency_key": "golden-edit:1",
                "actor": actor,
                "intent": "Edit one golden symbol value",
                "risk": "low",
                "preconditions": [],
                "operation": {
                    "type": "schematic.set_property",
                    "payload": {
                        "subject_ref": subject_ref.model_dump(mode="json"),
                        "property_name": "Value",
                        "value": old + "-EDITED",
                        "expected_old_value": old,
                    },
                },
                "required_validations": ["semantic_diff", "kicad_erc"],
                "provenance": {
                    "requirement_ids": ["REQ-FUNC-001"],
                    "evidence_ids": [],
                    "module_revision_ids": [],
                },
            }
        ],
    }
    return load_command_batch(json.dumps(value).encode()).commands[0]


@pytest.mark.parametrize(("directory", "root_name"), GOLDENS)
def test_golden_roundtrip_inspect_and_controlled_edit(
    tmp_path: Path, directory: str, root_name: str
) -> None:
    fixtures = Path(__file__).resolve().parents[1] / "fixtures"
    source = fixtures / "kicad" / "golden" / directory
    project = tmp_path / "Windows path with spaces" / directory
    shutil.copytree(source, project)

    for schematic in sorted(project.rglob("*.kicad_sch")):
        data = schematic.read_bytes()
        assert apply_edits(parse_cst(data), ()) == data

    adapter = CstSchematicAdapter(
        FileModuleCatalog(fixtures / "modules", max_files=32, max_bytes=2_000_000)
    )
    before = adapter.inspect(project)
    assert before.root_file == root_name
    keys = [symbol.ref.object_uuid for symbol in before.symbols]
    assert len(keys) == len(set(keys))
    if not before.symbols:
        assert directory == "blank"
        return

    command = _set_value_command(
        "prj_golden", before.symbols[0].ref, before.symbols[0].value
    )
    applied = adapter.apply(project, (command,))
    attribution = CommandAttribution(
        command_id=command.command_id,
        requirement_ids=command.provenance.requirement_ids,
        risk=RiskLevel.LOW,
        selectors=applied.command_results[0].effects,
    )
    diff = build_semantic_diff(before, applied.after, (attribution,))
    assert diff.changes
    assert {item.command_id for item in diff.changes} == {command.command_id}


def test_special_golden_semantics() -> None:
    root = Path(__file__).resolve().parents[1] / "fixtures" / "kicad" / "golden"
    adapter = CstSchematicAdapter(
        FileModuleCatalog(
            Path(__file__).resolve().parents[1] / "fixtures" / "modules",
            max_files=32,
            max_bytes=2_000_000,
        )
    )
    hierarchical = adapter.inspect(root / "hierarchical")
    unicode_doc = adapter.inspect(root / "unicode")
    multi = adapter.inspect(root / "multi-unit")
    custom = adapter.inspect(root / "custom-properties")
    assert len(hierarchical.sheets) >= 2
    assert any("状态" in symbol.value for symbol in unicode_doc.symbols)
    assert len({symbol.unit for symbol in multi.symbols}) >= 2
    assert any(
        item.name.startswith("User.")
        for symbol in custom.symbols
        for item in symbol.properties
    )
```

The real-KiCad loop is deliberately inside the single contract test above; do
not parameterize it or create another skip-producing KiCad test.

- [ ] **Step 2: Run new tests and verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_schematic_goldens.py tests/integration/test_validation.py tests/e2e/test_controlled_design_change.py tests/contract/test_kicad_cli.py tests/contract/test_kicad_schematic_write.py -q
```

Expected: FAIL because managed validation, fault hooks, restart recovery, and
the golden fixtures are incomplete. The contract test may SKIP only when a
supported KiCad 9 CLI is absent.

- [ ] **Step 3: Implement managed validation, fault hooks, goldens, and docs**

Refactor `ValidationTaskHandler` to receive `RevisionService` and
`WorkspaceCopier`. Select the input exactly once:

```python
if project.mode is ProjectMode.MANAGED:
    if project.current_revision is None:
        raise TerminalTaskError("REVISION_RECONCILIATION_REQUIRED", project.id)
    context = self._revisions.materialize(
        project.id, project.current_revision, f"validation-{lease.task_id}"
    )
else:
    context = self._registered_copy(project, lease.task_id)

with context as workspace:
    reports = self._kicad.validate(workspace, output)
```

Delete the duplicated copy walker from `validation.py`; registered copies use
the Task 5 `WorkspaceCopier` with the same link/file/byte policy. Never select
the managed design ref as validation input; use the database revision.

Add production no-op fault hooks, injected only by constructors:

```python
class FaultPoint(StrEnum):
    BEFORE_CANDIDATE_COMMIT = "before_candidate_commit"
    AFTER_PROPOSAL_REF_BEFORE_DATABASE = "after_proposal_ref_before_database"
    DURING_DIFF_ARTIFACT_SAVE = "during_diff_artifact_save"
    AFTER_EXECUTION_BEFORE_FINAL_FENCE = "after_execution_before_final_fence"
    AFTER_ACCEPT_DATABASE_BEFORE_DESIGN_REF = (
        "after_accept_database_before_design_ref"
    )


class FaultInjector(Protocol):
    def hit(self, point: FaultPoint) -> None: ...


class NoFaults:
    def hit(self, point: FaultPoint) -> None:
        return None
```

Call the first four points at the named stages in `ProposalExecutor`; call the
acceptance point after the SQLite transaction and before design-ref update.
Default every production container to `NoFaults`. The hooks may raise and must
not catch or reinterpret the injected exception inside the transaction.

Extend the Task 19 container signature without replacing its metrics or
monotonic parameters:

```python
def build_container(
    settings: Settings,
    kicad_override: KicadPort | None = None,
    clock: Callable[[], datetime] = utc_now,
    *,
    metrics: Metrics | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    faults: FaultInjector | None = None,
) -> Container:
    metric_sink = metrics if metrics is not None else Metrics()
    fault_injector = faults if faults is not None else NoFaults()
```

Pass the same `fault_injector` instance to `ProposalExecutor` and
`ProposalDecisionService`. Pass the same `metric_sink` and `monotonic` values
specified in Task 19; `NoFaults` must never be used as a default argument
instance in a function signature.

Both service constructors add the keyword-only parameter
`faults: FaultInjector | None = None` and store
`faults if faults is not None else NoFaults()`. This keeps focused unit and
integration construction backward-compatible, while `build_container` always
supplies the one shared `fault_injector` explicitly.

Generate every golden schematic by KiCad 9 and commit it unchanged. Add
parameterized tests that parse and byte-roundtrip all goldens, copy each into a
directory containing spaces, inspect stable UUID identity, and exercise any
operation supported by its semantic targets. The hierarchy fixture must load
`child/child.kicad_sch` through that exact relative sheet filename so only
`root.kicad_sch` is a top-level CLI root. Unicode must survive canonical evidence, multi-unit symbols must
retain unit identity, custom properties must remain present, and the ERC-error
fixture must yield at least one normalized finding. No golden contains an
unknown root node that real KiCad rejects; unknown-node preservation remains an
inline CST-only test.

Update `README.md` with installation, initialization, project adopt, requirement
G1, proposal create/worker/diff/accept/reject, read-only validation, data
directories, KiCad 9 requirement, and Phase 2A limitations.

Expand `docs/DEVELOPMENT_GUIDE.md` with these exact sections:

1. architecture and authority boundaries (SQLite, managed Git, Artifact Store);
2. Windows/Linux setup, virtual environment, and all environment variables;
3. migration workflow and schema ownership;
4. project adoption, revision reconciliation, and source immutability;
5. RequirementSet schema, canonicalization, G1, and digest signing;
6. DesignCommand schema, preconditions, idempotency, and examples for all four operations;
7. CST, semantic IR, Diff attribution, and prohibited regex edits;
8. verified module/footprint catalog authoring and digest verification;
9. Worker leases, fencing, deterministic commits, evidence sets, and recovery;
10. REST/CLI reference, status transitions, stable error codes, and examples;
11. unit/property/golden/integration/contract/E2E test commands and fixture rules;
12. structured log context, audit outbox fields, the eight metric families, low-cardinality label rules, and Phase 2A's process-local metric snapshot boundary;
13. fault-injection procedure, troubleshooting, security boundaries, and Phase 2B+ non-goals.

Every command and response example must match the implemented `--help` and
OpenAPI output. State clearly that AI generation, arbitrary component/wire
editing, PCB layout, manufacturing output, Web UI, and resident Workers are not
part of Phase 2A.

- [ ] **Step 4: Run the complete verification gate**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pytest --cov=pcbflow --cov-report=term-missing --cov-fail-under=90 -q
.\.venv\Scripts\python.exe -m pcbflow --help
.\.venv\Scripts\python.exe -m pcbflow project --help
.\.venv\Scripts\python.exe -m pcbflow requirements --help
.\.venv\Scripts\python.exe -m pcbflow approval --help
.\.venv\Scripts\python.exe -m pcbflow proposal --help
git diff --check
```

Expected: all non-KiCad tests PASS; only the explicitly marked real-KiCad
contract may SKIP when KiCad 9 is absent; coverage is at least 90%; every help
command exits 0; `git diff --check` prints nothing.

- [ ] **Step 5: Commit**

```powershell
git add src/pcbflow/validation.py src/pcbflow/proposals.py src/pcbflow/approvals.py src/pcbflow/container.py tests/unit/test_schematic_goldens.py tests/integration/test_validation.py tests/e2e/test_controlled_design_change.py tests/contract/test_kicad_cli.py tests/contract/test_kicad_schematic_write.py tests/fixtures/kicad/golden/blank/blank.kicad_sch tests/fixtures/kicad/golden/simple/simple.kicad_sch tests/fixtures/kicad/golden/hierarchical/root.kicad_sch tests/fixtures/kicad/golden/hierarchical/child/child.kicad_sch tests/fixtures/kicad/golden/unicode/unicode.kicad_sch tests/fixtures/kicad/golden/multi-unit/multi-unit.kicad_sch tests/fixtures/kicad/golden/custom-properties/custom-properties.kicad_sch tests/fixtures/kicad/golden/erc-error/erc-error.kicad_sch README.md docs/DEVELOPMENT_GUIDE.md
git commit -m "test: verify controlled design change recovery"
```

# Phase 0/1 Read-Only Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the first recoverable vertical slice that registers a KiCad project, probes `kicad-cli`, runs read-only ERC/DRC jobs in isolation, persists artifacts/findings, and exposes results through API and CLI.

**Architecture:** A Python package uses domain dataclasses and application services behind explicit ports. SQLAlchemy/Alembic persist projects and durable task state in SQLite, a content-addressed local store keeps raw tool evidence, and a KiCad adapter runs only through an argument-array process runner. FastAPI and Typer are thin adapters over the same services.

**Tech Stack:** Python `>=3.12,<3.14`, FastAPI, Pydantic v2, SQLAlchemy 2, Alembic, Typer, pytest, Hypothesis, SQLite WAL, `kicad-cli` 9.x when available.

## Global Constraints

- KiCad projects are read-only in this slice; every tool invocation runs against an isolated copy.
- AI, React, automatic edits, supplier networking, approvals, PostgreSQL, and manufacturing export are outside this plan.
- The local default database is SQLite with WAL, foreign keys, and a busy timeout.
- Every mutating application call accepts or creates an idempotency key.
- Process execution uses argument arrays with `shell=False`, a fixed working directory, timeout, bounded captured output, and full process-tree cancellation semantics.
- Missing `kicad-cli` is a valid doctor result, not an application crash; real KiCad contract tests skip with an explicit reason.
- Persisted times are timezone-aware UTC values; identifiers are opaque prefixed UUID values.
- Raw tool reports are immutable SHA-256-addressed artifacts; findings reference evidence rather than embedding raw logs.
- TDD is mandatory: observe the focused test fail before adding each behavior, then run the focused and broader suites.
- Do not add framework layers that are not exercised by this vertical slice.

---

## Planned File Map

```text
pyproject.toml                         Packaging, dependencies, pytest/coverage policy
.gitignore                             Local environments, caches, runtime data
README.md                              Install and vertical-slice usage
alembic.ini                            Migration CLI configuration
alembic/env.py                         SQLAlchemy metadata wiring
alembic/versions/0001_initial.py       Initial project/task/evidence schema
src/pcbflow/__init__.py                Package version
src/pcbflow/__main__.py                `python -m pcbflow` entry point
src/pcbflow/config.py                  Environment-derived immutable settings
src/pcbflow/domain.py                  Focused immutable domain records/enums/errors
src/pcbflow/db.py                      Engine/session creation and SQLite pragmas
src/pcbflow/tables.py                  SQLAlchemy persistence rows
src/pcbflow/repositories.py            Project, task, evidence and finding repositories
src/pcbflow/artifacts.py               Content-addressed artifact storage
src/pcbflow/process.py                 Safe subprocess port and implementation
src/pcbflow/kicad.py                   KiCad discovery, probe, ERC/DRC adapter and parser
src/pcbflow/tasks.py                   Lease-based worker and handler registry
src/pcbflow/validation.py              Read-only validation orchestration
src/pcbflow/container.py               Application composition root
src/pcbflow/api.py                     FastAPI routes and DTOs
src/pcbflow/cli.py                     Typer commands
tests/conftest.py                      Temporary settings/database fixtures
tests/unit/test_config.py              Settings tests
tests/unit/test_artifacts.py           Artifact store tests
tests/unit/test_process.py             Process runner tests
tests/unit/test_kicad.py               KiCad probe/parser/argv tests
tests/integration/test_migrations.py    Alembic schema test
tests/integration/test_projects.py      Project persistence tests
tests/integration/test_tasks.py         Lease/idempotency/fencing tests
tests/integration/test_validation.py    Orchestration and evidence tests
tests/e2e/test_api_cli.py               API/CLI vertical path
tests/contract/test_kicad_cli.py        Optional real KiCad contract
tests/fixtures/kicad/erc.json           Stable normalized-input fixture
tests/fixtures/kicad/drc.json           Stable normalized-input fixture
```

The initial slice deliberately keeps each cross-layer concept in one focused module. A module must be split when it exceeds one clear responsibility or becomes difficult to test without unrelated fixtures.

---

### Task 1: Package, Settings, and Test Harness

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `src/pcbflow/__init__.py`
- Create: `src/pcbflow/config.py`
- Create: `tests/unit/test_config.py`

**Interfaces:**
- Produces: `Settings.from_env(environ: Mapping[str, str] | None = None, cwd: Path | None = None) -> Settings`
- Produces: `Settings.ensure_directories() -> None`
- Produces: package version `pcbflow.__version__`

- [ ] **Step 1: Add packaging metadata and the failing settings test**

```toml
[build-system]
requires = ["setuptools>=75"]
build-backend = "setuptools.build_meta"

[project]
name = "pcbflow"
version = "0.1.0"
requires-python = ">=3.12,<3.14"
dependencies = [
  "alembic>=1.14,<2",
  "fastapi>=0.115,<1",
  "pydantic>=2.10,<3",
  "sqlalchemy>=2.0.36,<3",
  "typer>=0.15,<1",
  "uvicorn>=0.34,<1",
]

[project.optional-dependencies]
dev = [
  "httpx>=0.28,<1",
  "hypothesis>=6.120,<7",
  "pytest>=8.3,<9",
  "pytest-cov>=6,<7",
]

[project.scripts]
pcbflow = "pcbflow.cli:app"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-ra --strict-markers"
markers = ["kicad: requires a real supported kicad-cli"]
```

```python
# tests/unit/test_config.py
from pathlib import Path

from pcbflow.config import Settings


def test_settings_derive_local_paths(tmp_path: Path) -> None:
    settings = Settings.from_env({"PCBFLOW_DATA_DIR": str(tmp_path)})

    assert settings.data_dir == tmp_path.resolve()
    assert settings.database_url == f"sqlite+pysqlite:///{tmp_path.resolve().as_posix()}/pcbflow.db"
    assert settings.artifact_dir == tmp_path.resolve() / "artifacts"
    assert settings.kicad_cli is None


def test_settings_accept_explicit_kicad_cli(tmp_path: Path) -> None:
    executable = tmp_path / "kicad-cli.exe"
    settings = Settings.from_env(
        {
            "PCBFLOW_DATA_DIR": str(tmp_path / "data"),
            "PCBFLOW_KICAD_CLI": str(executable),
        }
    )

    assert settings.kicad_cli == executable.resolve()
```

- [ ] **Step 2: Create the virtual environment, install dependencies, and verify RED**

Run:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest tests/unit/test_config.py -v
```

Expected: collection fails with `ModuleNotFoundError: No module named 'pcbflow.config'`.

- [ ] **Step 3: Implement immutable settings and repository ignores**

```python
# src/pcbflow/config.py
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True, slots=True)
class Settings:
    data_dir: Path
    database_url: str
    artifact_dir: Path
    kicad_cli: Path | None
    task_lease_seconds: int = 60
    process_timeout_seconds: int = 120
    max_process_output_bytes: int = 2_000_000

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> "Settings":
        values = os.environ if environ is None else environ
        base = Path(values.get("PCBFLOW_DATA_DIR", str((cwd or Path.cwd()) / ".pcbflow-data"))).resolve()
        configured_cli = values.get("PCBFLOW_KICAD_CLI")
        return cls(
            data_dir=base,
            database_url=values.get(
                "PCBFLOW_DATABASE_URL",
                f"sqlite+pysqlite:///{base.as_posix()}/pcbflow.db",
            ),
            artifact_dir=Path(values.get("PCBFLOW_ARTIFACT_DIR", str(base / "artifacts"))).resolve(),
            kicad_cli=Path(configured_cli).resolve() if configured_cli else None,
            task_lease_seconds=int(values.get("PCBFLOW_TASK_LEASE_SECONDS", "60")),
            process_timeout_seconds=int(values.get("PCBFLOW_PROCESS_TIMEOUT_SECONDS", "120")),
            max_process_output_bytes=int(values.get("PCBFLOW_MAX_PROCESS_OUTPUT_BYTES", "2000000")),
        )

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
```

```python
# src/pcbflow/__init__.py
__version__ = "0.1.0"
```

```gitignore
.venv/
.pcbflow-data/
.pytest_cache/
.coverage
htmlcov/
__pycache__/
*.py[cod]
*.egg-info/
build/
dist/
node_modules/
.superpowers/
```

- [ ] **Step 4: Verify GREEN and the package metadata**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_config.py -v
.\.venv\Scripts\python.exe -c "import pcbflow; print(pcbflow.__version__)"
```

Expected: `2 passed`; version output is `0.1.0`.

- [ ] **Step 5: Commit the bootstrap**

```powershell
git add pyproject.toml .gitignore src/pcbflow/__init__.py src/pcbflow/config.py tests/unit/test_config.py
git commit -m "build: bootstrap pcbflow Python package"
```

---

### Task 2: Content-Addressed Artifact Store

**Files:**
- Create: `src/pcbflow/artifacts.py`
- Create: `tests/unit/test_artifacts.py`

**Interfaces:**
- Consumes: `Settings.artifact_dir`
- Produces: `ArtifactDescriptor(digest: str, size: int, media_type: str, path: Path)`
- Produces: `ContentAddressedStore.put_bytes(data: bytes, media_type: str) -> ArtifactDescriptor`
- Produces: `ContentAddressedStore.open(digest: str) -> BinaryIO`
- Produces: `ContentAddressedStore.verify(digest: str) -> bool`

- [ ] **Step 1: Write failing storage tests**

```python
# tests/unit/test_artifacts.py
import hashlib
from pathlib import Path

import pytest

from pcbflow.artifacts import ContentAddressedStore, InvalidDigestError


def test_put_bytes_is_content_addressed_and_idempotent(tmp_path: Path) -> None:
    store = ContentAddressedStore(tmp_path)
    first = store.put_bytes(b"erc report", "application/json")
    second = store.put_bytes(b"erc report", "application/json")
    expected = hashlib.sha256(b"erc report").hexdigest()

    assert first.digest == f"sha256:{expected}"
    assert first.path == tmp_path / "objects" / "sha256" / expected[:2] / expected[2:4] / expected
    assert first == second
    assert first.path.read_bytes() == b"erc report"
    assert store.verify(first.digest)


def test_open_rejects_malformed_digest(tmp_path: Path) -> None:
    store = ContentAddressedStore(tmp_path)

    with pytest.raises(InvalidDigestError):
        store.open("../../secret")
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_artifacts.py -v`

Expected: import fails because `pcbflow.artifacts` does not exist.

- [ ] **Step 3: Implement atomic storage and digest validation**

```python
# src/pcbflow/artifacts.py
from __future__ import annotations

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

_DIGEST = re.compile(r"^sha256:([0-9a-f]{64})$")


class InvalidDigestError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ArtifactDescriptor:
    digest: str
    size: int
    media_type: str
    path: Path


class ContentAddressedStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def _path(self, digest: str) -> Path:
        match = _DIGEST.fullmatch(digest)
        if match is None:
            raise InvalidDigestError(digest)
        value = match.group(1)
        return self.root / "objects" / "sha256" / value[:2] / value[2:4] / value

    def put_bytes(self, data: bytes, media_type: str) -> ArtifactDescriptor:
        value = hashlib.sha256(data).hexdigest()
        digest = f"sha256:{value}"
        target = self._path(digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            handle, temporary_name = tempfile.mkstemp(prefix="artifact-", dir=target.parent)
            try:
                with os.fdopen(handle, "wb") as temporary:
                    temporary.write(data)
                    temporary.flush()
                    os.fsync(temporary.fileno())
                os.replace(temporary_name, target)
            finally:
                if os.path.exists(temporary_name):
                    os.unlink(temporary_name)
        return ArtifactDescriptor(digest, len(data), media_type, target)

    def open(self, digest: str) -> BinaryIO:
        return self._path(digest).open("rb")

    def verify(self, digest: str) -> bool:
        with self.open(digest) as artifact:
            actual = hashlib.sha256(artifact.read()).hexdigest()
        return digest == f"sha256:{actual}"
```

- [ ] **Step 4: Verify focused and property tests**

Add this property test:

```python
from hypothesis import given, strategies as st


@given(st.binary(max_size=65_536))
def test_arbitrary_payload_round_trips(tmp_path: Path, payload: bytes) -> None:
    store = ContentAddressedStore(tmp_path)
    descriptor = store.put_bytes(payload, "application/octet-stream")

    with store.open(descriptor.digest) as artifact:
        assert artifact.read() == payload
    assert store.verify(descriptor.digest)
```

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_artifacts.py -v`

Expected: all artifact tests pass.

- [ ] **Step 5: Commit artifact storage**

```powershell
git add src/pcbflow/artifacts.py tests/unit/test_artifacts.py
git commit -m "feat: add content-addressed artifact store"
```

---

### Task 3: Initial Database Schema and Project Repository

**Files:**
- Create: `alembic.ini`
- Create: `alembic/env.py`
- Create: `alembic/versions/0001_initial.py`
- Create: `src/pcbflow/domain.py`
- Create: `src/pcbflow/db.py`
- Create: `src/pcbflow/tables.py`
- Create: `src/pcbflow/repositories.py`
- Create: `tests/conftest.py`
- Create: `tests/integration/test_migrations.py`
- Create: `tests/integration/test_projects.py`

**Interfaces:**
- Consumes: `Settings.database_url`
- Produces: `create_engine_and_session(database_url: str) -> tuple[Engine, sessionmaker[Session]]`
- Produces: `ProjectRepository.create(name: str, source_path: Path, idempotency_key: str) -> Project`
- Produces: `ProjectRepository.get(project_id: str) -> Project`
- Produces: `ProjectRepository.list() -> list[Project]`

- [ ] **Step 1: Write migration and repository tests**

```python
# tests/integration/test_projects.py
from pathlib import Path

from pcbflow.repositories import ProjectRepository


def test_create_project_is_idempotent(session_factory, tmp_path: Path) -> None:
    repository = ProjectRepository(session_factory)
    source = tmp_path / "board"
    source.mkdir()

    first = repository.create("Controller", source, "create-controller")
    second = repository.create("Controller", source, "create-controller")

    assert first == second
    assert repository.get(first.id) == first
    assert repository.list() == [first]
```

```python
# tests/integration/test_migrations.py
from sqlalchemy import inspect


def test_initial_migration_creates_vertical_slice_tables(migrated_engine) -> None:
    assert set(inspect(migrated_engine).get_table_names()) >= {
        "projects", "tasks", "task_attempts", "artifacts", "evidence", "findings"
    }
```

- [ ] **Step 2: Run tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/integration/test_migrations.py tests/integration/test_projects.py -v`

Expected: imports or fixtures fail because persistence modules do not exist.

- [ ] **Step 3: Implement domain records and SQLite engine behavior**

```python
# src/pcbflow/domain.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from uuid import uuid4


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def utc_now() -> datetime:
    return datetime.now(UTC)


class TaskStatus(StrEnum):
    QUEUED = "queued"
    LEASED = "leased"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    RETRY_WAIT = "retry_wait"
    FAILED_TERMINAL = "failed_terminal"


@dataclass(frozen=True, slots=True)
class Project:
    id: str
    name: str
    source_path: Path
    created_at: datetime
```

```python
# src/pcbflow/db.py
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker


def create_engine_and_session(database_url: str) -> tuple[Engine, sessionmaker[Session]]:
    engine = create_engine(database_url, future=True)
    if database_url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def configure_sqlite(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()
    return engine, sessionmaker(engine, expire_on_commit=False)
```

- [ ] **Step 4: Add SQLAlchemy rows and an explicit Alembic migration**

Define typed rows with these exact constraints:

```text
projects: id PK, name, source_path, idempotency_key UNIQUE, created_at
tasks: id PK, project_id FK nullable, kind, payload_json, result_json nullable,
       status, idempotency_key UNIQUE, lease_owner nullable, lease_token nullable,
       lease_expires_at nullable, last_error_code nullable, attempt_count,
       created_at, updated_at, version
task_attempts: id PK, task_id FK, attempt_number, lease_token, started_at,
               finished_at nullable, outcome nullable, error_code nullable
artifacts: digest PK, size, media_type, storage_path, created_at
evidence: id PK, project_id FK, task_id FK, kind, artifact_digest FK,
          subject, verdict, created_at
findings: id PK, project_id FK, task_id FK, evidence_id FK, rule_id,
          severity, subject, message, status, created_at
```

`alembic/env.py` imports `pcbflow.tables.Base.metadata`; `0001_initial.py` creates all six tables and their foreign keys/indexes explicitly, and `downgrade()` drops them in reverse dependency order.

- [ ] **Step 5: Implement project repository transactions**

```python
class ProjectNotFoundError(LookupError):
    pass


class ProjectRepository:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def create(self, name: str, source_path: Path, idempotency_key: str) -> Project:
        resolved = source_path.resolve(strict=True)
        if not resolved.is_dir():
            raise ValueError("project source must be a directory")
        with self._sessions.begin() as session:
            existing = session.scalar(
                select(ProjectRow).where(ProjectRow.idempotency_key == idempotency_key)
            )
            if existing is None:
                existing = ProjectRow(
                    id=new_id("prj"),
                    name=name,
                    source_path=str(resolved),
                    idempotency_key=idempotency_key,
                    created_at=utc_now(),
                )
                session.add(existing)
        return _project(existing)
```

Implement `get()` with `ProjectNotFoundError` and `list()` ordered by `created_at, id`.

- [ ] **Step 6: Run migrations and repository tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m pytest tests/integration/test_migrations.py tests/integration/test_projects.py -v
```

Expected: migration reaches `0001_initial`; all tests pass.

- [ ] **Step 7: Commit persistence foundation**

```powershell
git add alembic.ini alembic src/pcbflow/domain.py src/pcbflow/db.py src/pcbflow/tables.py src/pcbflow/repositories.py tests
git commit -m "feat: persist projects and validation records"
```

---

### Task 4: Safe Process Runner and KiCad Doctor

**Files:**
- Create: `src/pcbflow/process.py`
- Create: `src/pcbflow/kicad.py`
- Create: `tests/unit/test_process.py`
- Create: `tests/unit/test_kicad.py`

**Interfaces:**
- Produces: `ProcessRunner.run(argv: Sequence[str], cwd: Path, timeout_seconds: float) -> ProcessResult`
- Produces: `KicadCli.locate(configured: Path | None = None) -> Path | None`
- Produces: `KicadCli.probe() -> KicadCapability`
- `KicadCapability` fields: `available`, `path`, `version`, `executable_digest`, `reason`

- [ ] **Step 1: Write failing runner and probe tests**

```python
# tests/unit/test_process.py
import sys
from pathlib import Path

import pytest

from pcbflow.process import ProcessRunner, ProcessTimeoutError


def test_runner_uses_argument_array_and_captures_output(tmp_path: Path) -> None:
    result = ProcessRunner(max_output_bytes=1024).run(
        [sys.executable, "-c", "print('ready')"], tmp_path, 5
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "ready"
    assert result.stderr == ""


def test_runner_reports_timeout(tmp_path: Path) -> None:
    with pytest.raises(ProcessTimeoutError):
        ProcessRunner(max_output_bytes=1024).run(
            [sys.executable, "-c", "import time; time.sleep(5)"], tmp_path, 0.05
        )
```

```python
# tests/unit/test_kicad.py
from pathlib import Path

from pcbflow.kicad import KicadCli
from pcbflow.process import ProcessResult


class VersionRunner:
    def run(self, argv, cwd, timeout_seconds):
        return ProcessResult(tuple(argv), 0, "9.0.2\n", "", False)


def test_probe_returns_version_and_digest(tmp_path: Path) -> None:
    executable = tmp_path / "kicad-cli.exe"
    executable.write_bytes(b"fixture executable")

    report = KicadCli(VersionRunner(), executable, 5).probe()

    assert report.available
    assert report.version == "9.0.2"
    assert report.executable_digest.startswith("sha256:")
```

- [ ] **Step 2: Run tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_process.py tests/unit/test_kicad.py -v`

Expected: missing-module import failures.

- [ ] **Step 3: Implement bounded subprocess execution**

`ProcessRunner` must use `subprocess.Popen(..., shell=False, stdout=PIPE, stderr=PIPE, text=False)`. On Windows create a new process group and terminate the child tree on timeout; on other systems start a new session and terminate the process group. Decode with UTF-8 and `errors="replace"`; if either stream exceeds `max_output_bytes`, retain the prefix and set `output_truncated=True`.

```python
@dataclass(frozen=True, slots=True)
class ProcessResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    output_truncated: bool


class ProcessTimeoutError(TimeoutError):
    def __init__(self, argv: Sequence[str], timeout_seconds: float) -> None:
        super().__init__(f"process timed out after {timeout_seconds}s: {argv[0]}")
        self.argv = tuple(argv)
        self.timeout_seconds = timeout_seconds
```

- [ ] **Step 4: Implement KiCad location and capability probing**

Search order is explicit configured path, `shutil.which("kicad-cli")`, then version-sorted `C:/Program Files/KiCad/*/bin/kicad-cli.exe` on Windows. Reject non-files. `probe()` hashes the executable, invokes `--version`, and returns `available=False` with a stable reason for missing executable, timeout, nonzero exit, or unparseable version.

```python
@dataclass(frozen=True, slots=True)
class KicadCapability:
    available: bool
    path: Path | None
    version: str | None
    executable_digest: str | None
    reason: str | None
```

- [ ] **Step 5: Run focused tests and verify doctor behavior without KiCad**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_process.py tests/unit/test_kicad.py -v
.\.venv\Scripts\python.exe -c "from pcbflow.kicad import KicadCli; print(KicadCli.locate())"
```

Expected: tests pass; the second command prints a path or `None` without raising.

- [ ] **Step 6: Commit process and capability support**

```powershell
git add src/pcbflow/process.py src/pcbflow/kicad.py tests/unit/test_process.py tests/unit/test_kicad.py
git commit -m "feat: probe KiCad through safe process runner"
```

---

### Task 5: Durable Tasks, Leases, and Fencing

**Files:**
- Modify: `src/pcbflow/domain.py`
- Modify: `src/pcbflow/repositories.py`
- Create: `src/pcbflow/tasks.py`
- Create: `tests/integration/test_tasks.py`

**Interfaces:**
- Produces: `TaskRepository.enqueue(kind: str, payload: dict[str, object], idempotency_key: str, project_id: str | None) -> Task`
- Produces: `TaskRepository.claim_next(worker_id: str, now: datetime, lease_seconds: int) -> TaskLease | None`
- Produces: `TaskRepository.get(task_id: str) -> Task`
- Produces: `TaskRepository.start(task_id: str, lease_token: str) -> None`
- Produces: `TaskRepository.complete(task_id: str, lease_token: str, result: dict[str, object]) -> None`
- Produces: `TaskRepository.fail(task_id: str, lease_token: str, error_code: str, retryable: bool) -> None`
- Produces: `Worker.run_once() -> bool`

- [ ] **Step 1: Write lease, idempotency, and stale-token tests**

```python
def test_enqueue_is_idempotent(task_repository) -> None:
    first = task_repository.enqueue("validate", {"probe": True}, "validation:probe:r1", None)
    second = task_repository.enqueue("validate", {"probe": True}, "validation:probe:r1", None)
    assert first.id == second.id


def test_expired_lease_is_reclaimed_and_old_token_is_fenced(task_repository, utc_clock) -> None:
    task = task_repository.enqueue("validate", {}, "once", None)
    first = task_repository.claim_next("worker-a", utc_clock.now, 10)
    second = task_repository.claim_next("worker-b", utc_clock.now + timedelta(seconds=11), 10)

    assert first is not None and second is not None
    assert first.task_id == task.id == second.task_id
    assert first.lease_token != second.lease_token
    with pytest.raises(StaleLeaseError):
        task_repository.complete(task.id, first.lease_token, {})
```

- [ ] **Step 2: Run focused tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/integration/test_tasks.py -v`

Expected: task repository methods or domain types are missing.

- [ ] **Step 3: Add exact task records and errors**

```python
@dataclass(frozen=True, slots=True)
class Task:
    id: str
    project_id: str | None
    kind: str
    payload: dict[str, object]
    status: TaskStatus
    attempt_count: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class TaskLease:
    task_id: str
    kind: str
    payload: dict[str, object]
    lease_token: str
    lease_expires_at: datetime
    attempt_number: int


class StaleLeaseError(RuntimeError):
    pass


class RetryableTaskError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
```

- [ ] **Step 4: Implement compare-and-swap claim and fenced completion**

`claim_next()` selects the oldest `queued`/`retry_wait` task or an expired `leased`/`running` task. It issues an `UPDATE` constrained by the selected row ID and old `version`; the update assigns a new random lease token, owner, expiry, increments attempt/version, and inserts `task_attempts`. If affected-row count is zero, retry selection up to three times, then return `None`.

`start()`, `complete()`, and `fail()` update only where `id` and `lease_token` match and the lease has not been superseded. Zero affected rows raises `StaleLeaseError`. `complete()` stores canonical JSON result and clears lease fields. Retryable failure sets `retry_wait`; terminal failure sets `failed_terminal`.

- [ ] **Step 5: Implement a one-task worker**

```python
TaskHandler = Callable[[dict[str, object]], dict[str, object]]


class Worker:
    def __init__(self, repository, worker_id, handlers, clock, lease_seconds) -> None:
        self._repository = repository
        self._worker_id = worker_id
        self._handlers = handlers
        self._clock = clock
        self._lease_seconds = lease_seconds

    def run_once(self) -> bool:
        lease = self._repository.claim_next(
            self._worker_id, self._clock(), self._lease_seconds
        )
        if lease is None:
            return False
        self._repository.start(lease.task_id, lease.lease_token)
        handler = self._handlers.get(lease.kind)
        if handler is None:
            self._repository.fail(lease.task_id, lease.lease_token, "UNKNOWN_TASK_KIND", False)
            return True
        try:
            result = handler(lease.payload)
        except RetryableTaskError as error:
            self._repository.fail(lease.task_id, lease.lease_token, error.code, True)
        except Exception:
            self._repository.fail(lease.task_id, lease.lease_token, "UNHANDLED_TASK_ERROR", False)
        else:
            self._repository.complete(lease.task_id, lease.lease_token, result)
        return True
```

- [ ] **Step 6: Verify task behavior and full suite**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration/test_tasks.py -v
.\.venv\Scripts\python.exe -m pytest -q
```

Expected: task tests and all previous tests pass.

- [ ] **Step 7: Commit durable execution**

```powershell
git add src/pcbflow/domain.py src/pcbflow/repositories.py src/pcbflow/tasks.py tests/integration/test_tasks.py
git commit -m "feat: add durable fenced task execution"
```

---

### Task 6: KiCad ERC/DRC Reports and Read-Only Adapter

**Files:**
- Modify: `src/pcbflow/domain.py`
- Modify: `src/pcbflow/kicad.py`
- Create: `tests/fixtures/kicad/erc.json`
- Create: `tests/fixtures/kicad/drc.json`
- Modify: `tests/unit/test_kicad.py`
- Create: `tests/contract/test_kicad_cli.py`

**Interfaces:**
- Produces: `parse_kicad_report(kind: Literal["erc", "drc"], data: bytes) -> ValidationReport`
- Produces: `KicadCli.validate(project_dir: Path, output_dir: Path) -> tuple[RawValidationReport, ...]`
- Produces: `KicadPort` protocol with the same `validate()` signature
- `ValidationReport` fields: `kind`, `findings`; the raw report separately records `tool_version`
- `NormalizedFinding` fields: `rule_id`, `severity`, `subject`, `message`

- [ ] **Step 1: Add representative report fixtures and failing parser tests**

```json
{
  "version": "1.0",
  "source": "board.kicad_sch",
  "violations": [
    {
      "type": "pin_not_connected",
      "severity": "error",
      "description": "Input pin is not driven",
      "items": [{"description": "U1 pin 7", "uuid": "fixture-u1-pin7"}]
    }
  ]
}
```

```python
def test_parse_kicad_report_normalizes_findings(fixtures_dir: Path) -> None:
    report = parse_kicad_report("erc", (fixtures_dir / "kicad" / "erc.json").read_bytes())
    assert report.findings == (
        NormalizedFinding(
            rule_id="KICAD.ERC.PIN_NOT_CONNECTED",
            severity="error",
            subject="fixture-u1-pin7",
            message="Input pin is not driven: U1 pin 7",
        ),
    )
```

- [ ] **Step 2: Run the parser test and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_kicad.py -k parse -v`

Expected: parser/domain types are missing.

- [ ] **Step 3: Implement strict-but-diagnostic report parsing**

Parse UTF-8 JSON and require a top-level `violations` list. Normalize severity through `{error:error, warning:warning, exclusion:info, ignored:info}` and reject unknown severities with `KicadReportFormatError`. Compose stable rule IDs from validation kind and sanitized violation type. Use the first item UUID as subject when present, otherwise the report source. Preserve no unknown report fields in the domain object; raw bytes remain the evidence artifact.

Define the adapter boundary explicitly:

```python
@dataclass(frozen=True, slots=True)
class RawValidationReport:
    kind: Literal["erc", "drc"]
    data: bytes
    argv: tuple[str, ...]
    returncode: int
    tool_version: str


class KicadPort(Protocol):
    def validate(
        self, project_dir: Path, output_dir: Path
    ) -> tuple[RawValidationReport, ...]: ...
```

- [ ] **Step 4: Write failing command-construction tests**

Use a recording runner that recognizes `--output`, writes the appropriate fixture into that path, and returns zero. Assert exact invocations:

```python
(
    str(executable), "sch", "erc", "--format", "json",
    "--output", str(output_dir / "erc.json"), str(project_dir / "board.kicad_sch"),
)
(
    str(executable), "pcb", "drc", "--format", "json",
    "--output", str(output_dir / "drc.json"), str(project_dir / "board.kicad_pcb"),
)
```

Assert zero or one `.kicad_sch` and `.kicad_pcb` are accepted; multiple matching design files raise `AmbiguousKicadProjectError`. At least one design file is required.

- [ ] **Step 5: Implement read-only validation commands**

`validate()` resolves both directories, rejects output paths inside the source project, creates output directory, discovers exact design files, executes only supported checks, and returns raw report records containing `kind`, `data`, `argv`, `returncode`, and `tool_version`. A nonzero return with a valid report is findings-bearing success only when the probed CLI contract confirms violation exit-code behavior; any other nonzero result raises `KicadToolError` with stderr captured for evidence by the caller.

- [ ] **Step 6: Add an optional real-KiCad contract test**

```python
@pytest.mark.kicad
def test_installed_kicad_reports_supported_version(tmp_path: Path) -> None:
    executable = KicadCli.locate()
    if executable is None:
        pytest.skip("kicad-cli is not installed")
    capability = KicadCli(ProcessRunner(2_000_000), executable, 10).probe()
    assert capability.available
    assert capability.version is not None
    assert capability.version.startswith("9.")
```

- [ ] **Step 7: Verify parser, adapter, and optional contract**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_kicad.py -v
.\.venv\Scripts\python.exe -m pytest tests/contract/test_kicad_cli.py -v
```

Expected: unit tests pass; contract passes when KiCad 9 exists or reports one explicit skip.

- [ ] **Step 8: Commit KiCad validation adapter**

```powershell
git add src/pcbflow/domain.py src/pcbflow/kicad.py tests/unit/test_kicad.py tests/contract/test_kicad_cli.py tests/fixtures/kicad
git commit -m "feat: normalize KiCad ERC and DRC evidence"
```

---

### Task 7: Validation Orchestration and Evidence Persistence

**Files:**
- Modify: `src/pcbflow/repositories.py`
- Create: `src/pcbflow/validation.py`
- Create: `tests/integration/test_validation.py`

**Interfaces:**
- Consumes: `ProjectRepository`, `TaskRepository`, `ContentAddressedStore`, `KicadCli`
- Produces: `ValidationService.enqueue(project_id: str, idempotency_key: str) -> Task`
- Produces: `ValidationTaskHandler.__call__(payload: dict[str, object]) -> dict[str, object]`
- Produces: `EvidenceRepository.list_for_project(project_id: str) -> list[Evidence]`
- Produces: `FindingRepository.list_for_project(project_id: str) -> list[Finding]`

- [ ] **Step 1: Write failing end-to-end service test with a fake adapter**

```python
def test_validation_handler_persists_raw_evidence_and_findings(app_services, tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "board.kicad_sch").write_text("fixture", encoding="utf-8")
    (source / "board.kicad_pcb").write_text("fixture", encoding="utf-8")
    project = app_services.projects.create("Controller", source, "project-1")
    task = app_services.validation.enqueue(project.id, "validate-project-1-r1")

    assert app_services.worker.run_once()

    completed = app_services.tasks.get(task.id)
    evidence = app_services.evidence.list_for_project(project.id)
    findings = app_services.findings.list_for_project(project.id)
    assert completed.status is TaskStatus.SUCCEEDED
    assert [item.kind for item in evidence] == ["kicad_erc", "kicad_drc"]
    assert findings[0].rule_id == "KICAD.ERC.PIN_NOT_CONNECTED"
    assert all(item.evidence_id in {record.id for record in evidence} for item in findings)
```

The fake adapter asserts its project path is not the registered source path and writes only into its supplied output directory.

- [ ] **Step 2: Run test and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/integration/test_validation.py -v`

Expected: validation service, repositories, and composition fixture are missing.

- [ ] **Step 3: Implement evidence and finding repositories**

Add immutable domain records `Evidence` and `Finding`. `EvidenceRepository.add_report()` upserts artifact metadata by digest, inserts one evidence row per raw report, and returns the evidence ID. `FindingRepository.replace_for_evidence()` deletes no historical records; it inserts normalized findings for a new evidence ID and relies on evidence chronology for current projections. List methods order by `created_at, id`.

- [ ] **Step 4: Implement isolated validation handler**

```python
class ValidationTaskHandler:
    def __call__(self, payload: dict[str, object]) -> dict[str, object]:
        project_id = str(payload["project_id"])
        project = self._projects.get(project_id)
        with TemporaryDirectory(prefix="pcbflow-validation-") as temporary:
            workspace = Path(temporary) / "project"
            output = Path(temporary) / "output"
            shutil.copytree(project.source_path, workspace, symlinks=False)
            reports = self._kicad.validate(workspace, output)
            evidence_ids: list[str] = []
            finding_count = 0
            for raw in reports:
                descriptor = self._store.put_bytes(raw.data, "application/json")
                parsed = parse_kicad_report(raw.kind, raw.data)
                evidence = self._evidence.add_report(project_id, descriptor, raw.kind)
                self._findings.add_many(project_id, evidence.id, parsed.findings)
                evidence_ids.append(evidence.id)
                finding_count += len(parsed.findings)
        return {"evidence_ids": evidence_ids, "finding_count": finding_count}
```

Before `copytree`, reject source roots containing symlinks or Windows reparse points. Apply a configurable file-count and total-byte ceiling. Never follow links outside the source root.

- [ ] **Step 5: Implement enqueue semantics**

`ValidationService.enqueue()` verifies the project exists and delegates to `TaskRepository.enqueue(kind="kicad.read_only_validation", payload={"project_id": project_id}, ...)`. The handler registry maps only that exact kind.

- [ ] **Step 6: Verify isolation, evidence, and full regression suite**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration/test_validation.py -v
.\.venv\Scripts\python.exe -m pytest -q
```

Expected: service test and all previous tests pass; source fixture bytes remain unchanged.

- [ ] **Step 7: Commit orchestration**

```powershell
git add src/pcbflow/domain.py src/pcbflow/repositories.py src/pcbflow/validation.py tests/integration/test_validation.py tests/conftest.py
git commit -m "feat: persist isolated validation evidence"
```

---

### Task 8: Composition Root, REST API, and CLI

**Files:**
- Create: `src/pcbflow/container.py`
- Create: `src/pcbflow/api.py`
- Create: `src/pcbflow/cli.py`
- Create: `src/pcbflow/__main__.py`
- Create: `tests/e2e/test_api_cli.py`

**Interfaces:**
- Produces: `build_container(settings: Settings, kicad_override: KicadPort | None = None, clock: Callable[[], datetime] = utc_now) -> Container`
- Produces: `create_app(container: Container | None = None) -> FastAPI`
- Produces: Typer application `pcbflow.cli.app`

- [ ] **Step 1: Write failing API vertical-path test**

```python
def test_api_registers_project_runs_worker_and_returns_findings(client, tmp_path: Path) -> None:
    source = tmp_path / "project"
    source.mkdir()
    (source / "board.kicad_sch").write_text("fixture", encoding="utf-8")
    (source / "board.kicad_pcb").write_text("fixture", encoding="utf-8")

    created = client.post(
        "/api/v1/projects",
        headers={"Idempotency-Key": "create-api-project"},
        json={"name": "Controller", "source_path": str(source)},
    )
    assert created.status_code == 201
    project_id = created.json()["id"]

    queued = client.post(
        f"/api/v1/projects/{project_id}/validations",
        headers={"Idempotency-Key": "validate-api-project-r1"},
    )
    assert queued.status_code == 202
    task_id = queued.json()["id"]

    assert client.post("/api/v1/worker:run-once").json() == {"handled": True}
    assert client.get(f"/api/v1/tasks/{task_id}").json()["status"] == "succeeded"
    assert client.get(f"/api/v1/projects/{project_id}/findings").json()[0]["rule_id"].startswith("KICAD.")
```

- [ ] **Step 2: Write failing CLI smoke test**

```python
from typer.testing import CliRunner

from pcbflow.cli import app


def test_doctor_json_has_stable_shape() -> None:
    result = CliRunner().invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert set(payload["kicad_cli"]) == {
        "available", "path", "version", "executable_digest", "reason"
    }
```

- [ ] **Step 3: Run API/CLI tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/e2e/test_api_cli.py -v`

Expected: API, CLI, or composition modules are missing.

- [ ] **Step 4: Build one composition root**

`build_container()` ensures directories, creates the engine/session factory, runs migrations through Alembic, creates repositories/store/process runner/KiCad adapter/services, and returns an immutable `Container`. Tests pass a fake KiCad port; production uses detected CLI. Both API and CLI call this function and contain no duplicate repository wiring.

- [ ] **Step 5: Implement thin FastAPI routes**

Required routes and status codes:

```text
GET  /health                                      200
POST /api/v1/projects                             201 or idempotent 200
GET  /api/v1/projects                             200
POST /api/v1/projects/{id}/validations            202
GET  /api/v1/tasks/{id}                           200/404
POST /api/v1/worker:run-once                      200
GET  /api/v1/projects/{id}/evidence               200
GET  /api/v1/projects/{id}/findings               200
```

Pydantic request models reject unknown fields. A shared exception handler maps project/task not found to stable JSON `{error:{code,message,retryable,correlation_id,details,actions}}`. `source_path` remains local-only and is not accepted when the server is configured for remote mode.

- [ ] **Step 6: Implement CLI commands over the same services**

Required commands:

```text
pcbflow doctor [--json]
pcbflow project add PATH --name NAME --idempotency-key KEY [--json]
pcbflow project list [--json]
pcbflow validate PROJECT_ID --idempotency-key KEY [--json]
pcbflow worker --once [--json]
pcbflow task show TASK_ID [--json]
pcbflow findings PROJECT_ID [--json]
pcbflow serve --host 127.0.0.1 --port 8765
```

`__main__.py` contains only `from pcbflow.cli import app; app()`. JSON mode writes exactly one JSON document to stdout; diagnostics go to stderr.

- [ ] **Step 7: Verify API, CLI, OpenAPI, and full tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/e2e/test_api_cli.py -v
.\.venv\Scripts\pcbflow.exe doctor --json
.\.venv\Scripts\python.exe -c "from pcbflow.api import create_app; assert '/api/v1/projects' in create_app().openapi()['paths']"
.\.venv\Scripts\python.exe -m pytest -q
```

Expected: E2E and full suites pass; doctor returns valid JSON even when KiCad is absent.

- [ ] **Step 8: Commit API and CLI**

```powershell
git add src/pcbflow/container.py src/pcbflow/api.py src/pcbflow/cli.py src/pcbflow/__main__.py tests/e2e/test_api_cli.py
git commit -m "feat: expose validation workflow through API and CLI"
```

---

### Task 9: Restart Recovery, Documentation, and Final Verification

**Files:**
- Modify: `tests/integration/test_tasks.py`
- Modify: `tests/integration/test_validation.py`
- Create: `README.md`

**Interfaces:**
- Verifies all interfaces produced by Tasks 1-8.
- Produces documented local workflow and known KiCad prerequisite behavior.

- [ ] **Step 1: Add failing restart recovery test**

```python
def test_expired_task_is_completed_after_repository_restart(settings, migrated_database, fake_kicad) -> None:
    first = build_container(settings, kicad_override=fake_kicad)
    project = first.projects.create("Controller", fake_kicad.source, "restart-project")
    task = first.validation.enqueue(project.id, "restart-validation")
    lease = first.tasks.claim_next("crashed-worker", utc_now(), 1)
    assert lease is not None
    first.dispose()

    second = build_container(
        settings,
        kicad_override=fake_kicad,
        clock=lambda: lease.lease_expires_at + timedelta(seconds=1),
    )
    assert second.worker.run_once()
    assert second.tasks.get(task.id).status is TaskStatus.SUCCEEDED
    assert len(second.evidence.list_for_project(project.id)) == 2
```

- [ ] **Step 2: Run the recovery test and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/integration/test_tasks.py tests/integration/test_validation.py -k restart -v`

Expected: fails until the container exposes deterministic clock/disposal and expired running tasks are reclaimable.

- [ ] **Step 3: Implement only the missing recovery hooks**

Add `Container.dispose()` to call `engine.dispose()`. Inject `clock: Callable[[], datetime]` when constructing `Worker`; production defaults to `utc_now`. Ensure `claim_next()` treats expired `leased` and `running` rows as reclaimable and records a new attempt without deleting the abandoned attempt.

- [ ] **Step 4: Write exact local usage documentation**

README sections:

```text
Purpose and current read-only boundary
Prerequisites: Python 3.12/3.13; optional KiCad 9 CLI
Create venv and install editable dev dependencies
Run migrations/tests
Run `pcbflow doctor --json`
Register a local KiCad project
Queue validation and run one worker task
Inspect task, evidence, and findings
Start local API on 127.0.0.1:8765
Data directory and immutable artifact layout
Known limitation when kicad-cli is absent
Development commands and optional kicad marker
```

All examples use PowerShell and the actual commands from Task 8. State explicitly that no design file is modified and no manufacturing package is generated in this slice.

- [ ] **Step 5: Run full verification with fresh evidence**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pytest --cov=pcbflow --cov-report=term-missing --cov-fail-under=80
.\.venv\Scripts\python.exe -m pytest -m kicad -v
.\.venv\Scripts\pcbflow.exe doctor --json
git diff --check
```

Expected:

- Full suite: zero failures.
- Coverage: at least 80% overall.
- KiCad contract: passes on KiCad 9 or one explicit missing-tool skip.
- Doctor: valid JSON with `available`, `path`, `version`, `executable_digest`, `reason`.
- Git whitespace check: no output and exit code zero.

- [ ] **Step 6: Verify the original vertical-slice acceptance path**

Against a temporary data directory and test fixture, run project registration, validation enqueue, `worker --once`, task query, evidence query, then restart the command process and query the same task again. Record command output in the implementation handoff. Confirm the source directory hash is unchanged before and after.

- [ ] **Step 7: Commit recovery and documentation**

```powershell
git add README.md src/pcbflow tests/integration
git commit -m "docs: document recoverable validation slice"
```

---

## Plan Self-Review

### Spec Coverage

This plan implements only the deliberately selected Phase 0/1 vertical slice from design section 27:

| Design requirement | Plan task |
| --- | --- |
| Python package/configuration | Task 1 |
| Immutable artifact evidence | Task 2 |
| SQLite/Alembic persistence | Task 3 |
| KiCad capability detection | Task 4 |
| Durable queue, leases, retry boundary, restart | Tasks 5 and 9 |
| Read-only ERC/DRC and normalization | Task 6 |
| Isolation, evidence, findings | Task 7 |
| API/CLI query path | Task 8 |
| Tests, coverage, optional real tool contract | Tasks 1-9 |

Intentional later-plan items: design commands and semantic diffs, requirements compiler/G1, AI prompts/model gateway, component/module libraries, automated schematic/PCB edits, Web UI, approvals, JLCPCB/EasyEDA Pro adapters, release manifests, PostgreSQL, RBAC, and production feedback. None is required to validate the four highest-risk assumptions of the first slice.

### Consistency Checks

- The task kind is always `kicad.read_only_validation`.
- The normalized validation kinds are always `erc` and `drc`; evidence kinds are `kicad_erc` and `kicad_drc`.
- All revisions in later phases remain outside this read-only slice; idempotency keys prevent duplicate project/task creation here.
- `ContentAddressedStore` owns bytes; SQL rows own metadata and references.
- API and CLI both consume the same `Container` and application services.
- Missing KiCad is represented as a capability result and explicit contract-test skip, never as a fake successful validation.

import gc
import time
import weakref
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from pcbflow.config import Settings
from pcbflow.container import build_container
from pcbflow.db import create_engine_and_session
from pcbflow.domain import Project

# Engines created by the container and migrated-database fixtures register
# themselves here so the collector probe can drain them before forcing GC.
_TEST_ENGINES: weakref.WeakSet[Engine] = weakref.WeakSet()


def _drain_engine(engine: Engine, timeout_seconds: float = 5.0) -> None:
    """Wait for checked-out connections to return to ``engine``'s pool.

    Background worker threads (heartbeat renewal, concurrency fixtures) may
    still be finishing a database operation while a fixture tears down.
    Disposing the engine at that moment strands their connection in the
    abandoned pool object, which the garbage collector then reports as an
    unclosed database. Waiting for checked-out()==0 before dispose() closes
    that race window.
    """
    deadline = time.monotonic() + timeout_seconds
    while engine.pool.checkedout() and time.monotonic() < deadline:
        time.sleep(0.05)


@pytest.fixture(autouse=True)
def _collect_abandoned_connections() -> Iterator[None]:
    """Force collection after every test so leaked sqlite connections surface
    deterministically (ResourceWarning) instead of at a random later GC pass.
    In-flight connections are drained first so only genuinely abandoned ones
    are reported.
    """
    yield
    for engine in list(_TEST_ENGINES):
        _drain_engine(engine)
    gc.collect()


@pytest.fixture
def database_url(tmp_path: Path) -> str:
    return f"sqlite+pysqlite:///{(tmp_path / 'pcbflow.db').as_posix()}"


@pytest.fixture
def migrated_database(
    database_url: str,
) -> Iterator[tuple[Engine, sessionmaker[Session]]]:
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")
    engine, sessions = create_engine_and_session(database_url)
    _TEST_ENGINES.add(engine)
    try:
        yield engine, sessions
    finally:
        _drain_engine(engine)
        engine.dispose()


@pytest.fixture
def migrated_engine(
    migrated_database: tuple[Engine, sessionmaker[Session]],
) -> Engine:
    return migrated_database[0]


@pytest.fixture
def session_factory(
    migrated_database: tuple[Engine, sessionmaker[Session]],
) -> sessionmaker[Session]:
    return migrated_database[1]


@pytest.fixture
def container(database_url: str, tmp_path: Path):
    settings = Settings.from_env(
        {
            "PCBFLOW_DATA_DIR": str(tmp_path / "service-data"),
            "PCBFLOW_DATABASE_URL": database_url,
        }
    )
    services = build_container(settings)
    _TEST_ENGINES.add(services.engine)
    try:
        yield services
    finally:
        _drain_engine(services.engine)
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

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from pcbflow.db import create_engine_and_session
from pcbflow.config import Settings
from pcbflow.container import build_container
from pcbflow.domain import Project


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
    try:
        yield engine, sessions
    finally:
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

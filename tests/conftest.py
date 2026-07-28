from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from pcbflow.db import create_engine_and_session


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

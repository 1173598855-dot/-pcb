from sqlalchemy import Engine, inspect, text


def test_initial_migration_creates_vertical_slice_tables(
    migrated_engine: Engine,
) -> None:
    assert set(inspect(migrated_engine).get_table_names()) >= {
        "projects",
        "tasks",
        "task_attempts",
        "artifacts",
        "evidence",
        "findings",
        "component_revisions",
    }


def test_migrations_upgrade_to_component_catalog_head(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as connection:
        version = connection.scalar(text("SELECT version_num FROM alembic_version"))

    assert version == "0003_component_revision_catalog"


def test_component_revision_table_has_catalog_columns(migrated_engine: Engine) -> None:
    columns = {column["name"] for column in inspect(migrated_engine).get_columns("component_revisions")}
    assert {"id", "component_key", "revision", "manifest_artifact_digest"} <= columns


def test_sqlite_connections_enable_required_pragmas(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as connection:
        foreign_keys = connection.scalar(text("PRAGMA foreign_keys"))
        journal_mode = connection.scalar(text("PRAGMA journal_mode"))
        busy_timeout = connection.scalar(text("PRAGMA busy_timeout"))

    assert foreign_keys == 1
    assert str(journal_mode).lower() == "wal"
    assert busy_timeout == 5_000

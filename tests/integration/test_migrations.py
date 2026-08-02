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
        "component_module_bindings",
    }


def test_migrations_upgrade_to_component_module_bindings_head(
    migrated_engine: Engine,
) -> None:
    with migrated_engine.connect() as connection:
        version = connection.scalar(text("SELECT version_num FROM alembic_version"))

    assert version == "0005_component_module_bindings"


def test_tasks_table_has_retry_eligibility_column(migrated_engine: Engine) -> None:
    columns = {column["name"] for column in inspect(migrated_engine).get_columns("tasks")}

    assert "next_attempt_at" in columns


def test_component_revision_table_has_catalog_columns(migrated_engine: Engine) -> None:
    columns = {column["name"] for column in inspect(migrated_engine).get_columns("component_revisions")}
    assert {"id", "component_key", "revision", "manifest_artifact_digest"} <= columns


def test_component_module_bindings_table_has_constraints_and_list_index(
    migrated_engine: Engine,
) -> None:
    inspector = inspect(migrated_engine)
    columns = {
        column["name"]
        for column in inspector.get_columns("component_module_bindings")
    }
    constraints = {
        constraint["name"]: tuple(constraint["column_names"])
        for constraint in inspector.get_unique_constraints("component_module_bindings")
    }
    indexes = {
        index["name"]: tuple(index["column_names"])
        for index in inspector.get_indexes("component_module_bindings")
    }

    assert columns == {
        "id",
        "component_revision_id",
        "kicad_major",
        "module_revision_id",
        "module_manifest_digest",
        "idempotency_key",
        "created_at",
    }
    assert constraints["uq_component_module_binding_component_major"] == (
        "component_revision_id",
        "kicad_major",
    )
    assert constraints["uq_component_module_binding_idempotency"] == (
        "idempotency_key",
    )
    assert indexes["ix_component_module_bindings_component_created"] == (
        "component_revision_id",
        "created_at",
        "id",
    )


def test_sqlite_connections_enable_required_pragmas(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as connection:
        foreign_keys = connection.scalar(text("PRAGMA foreign_keys"))
        journal_mode = connection.scalar(text("PRAGMA journal_mode"))
        busy_timeout = connection.scalar(text("PRAGMA busy_timeout"))

    assert foreign_keys == 1
    assert str(journal_mode).lower() == "wal"
    assert busy_timeout == 5_000

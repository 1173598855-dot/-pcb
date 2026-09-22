from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from pcbflow.eda import registration_input_digest
from pcbflow.repositories import ProjectRepository


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
        "pcb_candidates",
    }


def test_migrations_upgrade_to_task_cancellation_head(
    migrated_engine: Engine,
) -> None:
    with migrated_engine.connect() as connection:
        version = connection.scalar(text("SELECT version_num FROM alembic_version"))

    assert version == "0009_pcb_candidates"


def test_pcb_candidate_table_has_frozen_inputs_and_state_columns(
    migrated_engine: Engine,
) -> None:
    columns = {
        column["name"] for column in inspect(migrated_engine).get_columns("pcb_candidates")
    }
    assert columns == {
        "id",
        "project_id",
        "task_id",
        "idempotency_key",
        "base_revision",
        "base_snapshot_digest",
        "board_snapshot_digest",
        "rulepack_digest",
        "capability_digest",
        "authority_digest",
        "operations_json",
        "operations_digest",
        "algorithm_evidence_json",
        "status",
        "result_json",
        "last_error_code",
        "accepted_revision",
        "created_at",
        "updated_at",
        "version",
    }


def test_tasks_table_has_retry_and_cancellation_columns(migrated_engine: Engine) -> None:
    columns = {column["name"] for column in inspect(migrated_engine).get_columns("tasks")}

    assert {"next_attempt_at", "cancelled_at", "cancellation_reason"} <= columns


def test_eda_authority_migration_upgrades_from_task_cancellation(
    database_url: str,
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)

    command.upgrade(config, "0006_task_cancellation")
    engine = create_engine(database_url)
    try:
        legacy_source = tmp_path / "legacy-project"
        legacy_source.mkdir()
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO projects "
                    "(id, name, source_path, idempotency_key, created_at) "
                    "VALUES (:id, :name, :source_path, :idempotency_key, :created_at)"
                ),
                {
                    "id": "prj_legacy",
                    "name": "Legacy",
                    "source_path": str(legacy_source.resolve()),
                    "idempotency_key": "legacy-project",
                    "created_at": "2026-08-04 00:00:00+00:00",
                },
            )

        command.upgrade(config, "0007_eda_authority")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO projects "
                    "(id, name, source_path, idempotency_key, created_at) "
                    "VALUES (:id, :name, :source_path, :idempotency_key, :created_at)"
                ),
                {
                    "id": "prj_ambiguous",
                    "name": "Ambiguous",
                    "source_path": str(legacy_source.resolve()),
                    "idempotency_key": "ambiguous-project",
                    "created_at": "2026-08-04 00:00:00+00:00",
                },
            )
            connection.execute(
                text(
                    "INSERT INTO project_eda_authorities "
                    "(project_id, eda_kind, eda_profile_id, board_profile_id, "
                    "rulepack_digest, idempotency_key, canonical_digest, created_at) "
                    "VALUES (:project_id, :eda_kind, :eda_profile_id, "
                    ":board_profile_id, :rulepack_digest, :idempotency_key, "
                    ":canonical_digest, :created_at)"
                ),
                {
                    "project_id": "prj_ambiguous",
                    "eda_kind": "lceda_pro",
                    "eda_profile_id": "lceda-pro-v1",
                    "board_profile_id": "legacy-board-v1",
                    "rulepack_digest": "sha256:" + "1" * 64,
                    "idempotency_key": "late-authority",
                    "canonical_digest": "sha256:" + "2" * 64,
                    "created_at": "2026-08-04 00:00:00+00:00",
                },
            )

        command.upgrade(config, "head")
        inspector = inspect(engine)
        columns = {
            column["name"]
            for column in inspector.get_columns("project_eda_authorities")
        }
        constraints = {
            constraint["name"]: tuple(constraint["column_names"])
            for constraint in inspector.get_unique_constraints(
                "project_eda_authorities"
            )
        }

        assert columns == {
            "project_id",
            "eda_kind",
            "eda_profile_id",
            "board_profile_id",
            "rulepack_digest",
            "idempotency_key",
            "canonical_digest",
            "created_at",
        }
        assert constraints["uq_project_eda_authority_replay"] == (
            "project_id",
            "idempotency_key",
        )
        with engine.connect() as connection:
            assert connection.scalar(
                text("SELECT name FROM projects WHERE id = 'prj_legacy'")
            ) == "Legacy"
            legacy_digest = connection.scalar(
                text(
                    "SELECT registration_input_digest FROM projects "
                    "WHERE id = 'prj_legacy'"
                )
            )
            assert legacy_digest is not None
            assert legacy_digest == registration_input_digest(
                "Legacy", str(legacy_source.resolve()), None
            )
            assert connection.scalar(
                text(
                    "SELECT registration_input_digest FROM projects "
                    "WHERE id = 'prj_ambiguous'"
                )
            ) is None
        assert "registration_input_digest" in {
            column["name"] for column in inspector.get_columns("projects")
        }
        replayed, created = ProjectRepository(sessionmaker(engine)).create_with_status(
            "Legacy", legacy_source, "legacy-project"
        )
        assert created is False
        assert replayed.id == "prj_legacy"
    finally:
        engine.dispose()


def test_registration_digest_migration_is_self_contained() -> None:
    migration = (
        Path(__file__).resolve().parents[2]
        / "alembic"
        / "versions"
        / "0008_project_registration_digest.py"
    )

    assert "pcbflow.eda" not in migration.read_text(encoding="utf-8")


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

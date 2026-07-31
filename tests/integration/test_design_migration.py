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

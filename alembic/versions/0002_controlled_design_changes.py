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
        sa.UniqueConstraint("project_id", "revision", name="uq_project_revision"),
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

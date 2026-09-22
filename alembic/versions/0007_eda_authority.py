"""Persist immutable per-project EDA authority."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_eda_authority"
down_revision: str | None = "0006_task_cancellation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "project_eda_authorities",
        sa.Column(
            "project_id",
            sa.String(64),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("eda_kind", sa.String(32), nullable=False),
        sa.Column("eda_profile_id", sa.String(128), nullable=False),
        sa.Column("board_profile_id", sa.String(128), nullable=False),
        sa.Column("rulepack_digest", sa.String(71), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("canonical_digest", sa.String(71), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "project_id",
            "idempotency_key",
            name="uq_project_eda_authority_replay",
        ),
    )


def downgrade() -> None:
    op.drop_table("project_eda_authorities")

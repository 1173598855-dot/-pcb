"""Schedule retryable tasks with bounded backoff."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_task_retry_schedule"
down_revision: str | None = "0003_component_revision_catalog"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE tasks SET next_attempt_at = updated_at "
            "WHERE status = 'retry_wait'"
        )
    )
    op.create_index(
        "ix_tasks_claim_ready",
        "tasks",
        ["status", "next_attempt_at", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_tasks_claim_ready", table_name="tasks")
    op.drop_column("tasks", "next_attempt_at")

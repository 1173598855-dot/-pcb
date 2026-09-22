"""Persist task cancellation state."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_task_cancellation"
down_revision: str | None = "0005_component_module_bindings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "tasks",
        sa.Column("cancellation_reason", sa.String(1000), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tasks", "cancellation_reason")
    op.drop_column("tasks", "cancelled_at")

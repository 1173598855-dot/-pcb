"""Persist immutable component module bindings."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_component_module_bindings"
down_revision: str | None = "0004_task_retry_schedule"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "component_module_bindings",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "component_revision_id",
            sa.String(64),
            sa.ForeignKey("component_revisions.id"),
            nullable=False,
        ),
        sa.Column("kicad_major", sa.Integer(), nullable=False),
        sa.Column("module_revision_id", sa.String(255), nullable=False),
        sa.Column("module_manifest_digest", sa.String(71), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "component_revision_id",
            "kicad_major",
            name="uq_component_module_binding_component_major",
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_component_module_binding_idempotency",
        ),
    )
    op.create_index(
        "ix_component_module_bindings_component_created",
        "component_module_bindings",
        ["component_revision_id", "created_at", "id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_component_module_bindings_component_created",
        table_name="component_module_bindings",
    )
    op.drop_table("component_module_bindings")

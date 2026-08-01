"""Create immutable component revision catalog."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_component_revision_catalog"
down_revision: str | None = "0002_controlled_design_changes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "component_revisions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("component_key", sa.String(255), nullable=False),
        sa.Column("manufacturer", sa.String(255), nullable=False),
        sa.Column("part_number", sa.String(255), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("revision", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("canonical_digest", sa.String(71), nullable=False),
        sa.Column("manifest_artifact_digest", sa.String(71), sa.ForeignKey("artifacts.digest"), nullable=False),
        sa.Column("datasheet_artifact_digest", sa.String(71), sa.ForeignKey("artifacts.digest"), nullable=False),
        sa.Column("pinout_artifact_digest", sa.String(71), sa.ForeignKey("artifacts.digest"), nullable=False),
        sa.Column("symbol_artifact_digest", sa.String(71), sa.ForeignKey("artifacts.digest"), nullable=False),
        sa.Column("footprint_artifact_digest", sa.String(71), sa.ForeignKey("artifacts.digest"), nullable=False),
        sa.Column("model_3d_artifact_digest", sa.String(71), sa.ForeignKey("artifacts.digest"), nullable=True),
        sa.Column("idempotency_key", sa.String(255), unique=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("component_key", "revision", name="uq_component_revision_identity"),
    )
    op.create_index(
        "ix_component_revisions_component_key",
        "component_revisions",
        ["component_key", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_component_revisions_component_key", table_name="component_revisions")
    op.drop_table("component_revisions")

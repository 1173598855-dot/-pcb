"""Persist PCB candidate lifecycle state."""
from collections.abc import Sequence
import sqlalchemy as sa
from alembic import op

revision: str = "0009_pcb_candidates"
down_revision: str | None = "0008_project_registration_digest"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def upgrade() -> None:
    op.create_table(
        "pcb_candidates",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("project_id", sa.String(64), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_id", sa.String(64), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("base_revision", sa.String(80), nullable=False),
        sa.Column("base_snapshot_digest", sa.String(71), nullable=True),
        sa.Column("board_snapshot_digest", sa.String(71), nullable=False),
        sa.Column("rulepack_digest", sa.String(71), nullable=False),
        sa.Column("capability_digest", sa.String(71), nullable=False),
        sa.Column("authority_digest", sa.String(71), nullable=False),
        sa.Column("operations_json", sa.JSON, nullable=False),
        sa.Column("operations_digest", sa.String(71), nullable=False),
        sa.Column("algorithm_evidence_json", sa.JSON, nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("result_json", sa.JSON, nullable=True),
        sa.Column("last_error_code", sa.String(128), nullable=True),
        sa.Column("accepted_revision", sa.String(80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.UniqueConstraint("project_id", "idempotency_key", name="uq_pcb_candidate_key"),
    )
    op.create_index("ix_pcb_candidates_project_status", "pcb_candidates", ["project_id", "status"])

def downgrade() -> None:
    op.drop_index("ix_pcb_candidates_project_status", table_name="pcb_candidates")
    op.drop_table("pcb_candidates")

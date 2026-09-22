"""Persist immutable project registration input digests."""

from collections.abc import Sequence
import hashlib
import json

import sqlalchemy as sa
from alembic import op

revision: str = "0008_project_registration_digest"
down_revision: str | None = "0007_eda_authority"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _authority_absent_registration_digest(name: str, source_path: str) -> str:
    payload = {
        "schema_version": "1.0",
        "name": name,
        "source_path": source_path,
        "authority_present": False,
        "authority": None,
    }
    canonical_json = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical_json).hexdigest()}"


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("registration_input_digest", sa.String(71), nullable=True),
    )
    connection = op.get_bind()
    projects_without_authority = connection.execute(
        sa.text(
            "SELECT p.id, p.name, p.source_path "
            "FROM projects AS p "
            "WHERE NOT EXISTS ("
            "SELECT 1 FROM project_eda_authorities AS a "
            "WHERE a.project_id = p.id)"
        )
    ).mappings()
    for project in projects_without_authority:
        connection.execute(
            sa.text(
                "UPDATE projects SET registration_input_digest = :digest "
                "WHERE id = :project_id"
            ),
            {
                "project_id": project["id"],
                "digest": _authority_absent_registration_digest(
                    project["name"], project["source_path"]
                ),
            },
        )


def downgrade() -> None:
    op.drop_column("projects", "registration_input_digest")

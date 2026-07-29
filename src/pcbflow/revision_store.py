from __future__ import annotations

from datetime import UTC

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from pcbflow.design_tables import ProjectRevisionRow
from pcbflow.domain import ProjectRevision


class ProjectRevisionNotFoundError(LookupError):
    pass


def _revision(row: ProjectRevisionRow) -> ProjectRevision:
    created_at = (
        row.created_at.replace(tzinfo=UTC)
        if row.created_at.tzinfo is None
        else row.created_at.astimezone(UTC)
    )
    return ProjectRevision(
        id=row.id,
        project_id=row.project_id,
        revision=row.revision,
        parent_revision=row.parent_revision,
        snapshot_digest=row.snapshot_digest,
        requirement_set_id=row.requirement_set_id,
        command_batch_id=row.command_batch_id,
        created_at=created_at,
    )


class ProjectRevisionStore:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def get(self, project_id: str, revision: str) -> ProjectRevision:
        with self._sessions() as session:
            row = session.scalar(
                select(ProjectRevisionRow).where(
                    ProjectRevisionRow.project_id == project_id,
                    ProjectRevisionRow.revision == revision,
                )
            )
            if row is None:
                raise ProjectRevisionNotFoundError(f"{project_id}:{revision}")
            return _revision(row)

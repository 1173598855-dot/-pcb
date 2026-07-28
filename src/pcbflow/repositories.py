from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from pcbflow.domain import Project, new_id, utc_now
from pcbflow.tables import ProjectRow


class ProjectNotFoundError(LookupError):
    pass


class IdempotencyConflictError(RuntimeError):
    pass


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _project(row: ProjectRow) -> Project:
    return Project(
        id=row.id,
        name=row.name,
        source_path=Path(row.source_path),
        created_at=_utc(row.created_at),
    )


class ProjectRepository:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def create(self, name: str, source_path: Path, idempotency_key: str) -> Project:
        resolved = source_path.resolve(strict=True)
        if not resolved.is_dir():
            raise ValueError("project source must be a directory")
        with self._sessions.begin() as session:
            row = session.scalar(
                select(ProjectRow).where(ProjectRow.idempotency_key == idempotency_key)
            )
            if row is not None:
                if row.name != name or Path(row.source_path) != resolved:
                    raise IdempotencyConflictError(idempotency_key)
                return _project(row)
            row = ProjectRow(
                id=new_id("prj"),
                name=name,
                source_path=str(resolved),
                idempotency_key=idempotency_key,
                created_at=utc_now(),
            )
            session.add(row)
        return _project(row)

    def get(self, project_id: str) -> Project:
        with self._sessions() as session:
            row = session.get(ProjectRow, project_id)
            if row is None:
                raise ProjectNotFoundError(project_id)
            return _project(row)

    def list(self) -> list[Project]:
        with self._sessions() as session:
            rows = session.scalars(
                select(ProjectRow).order_by(ProjectRow.created_at, ProjectRow.id)
            ).all()
            return [_project(row) for row in rows]

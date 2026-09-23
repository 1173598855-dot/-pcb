from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from pcbflow.design_tables import ProjectEdaAuthorityRow
from pcbflow.domain import EdaKind, utc_now
from pcbflow.eda import (
    EdaAuthorityConflictError,
    ProjectEdaAuthority,
    ProjectEdaAuthorityInput,
    authority_digest,
    validate_authority_input,
    validate_idempotency_key,
)
from pcbflow.repositories import ProjectNotFoundError
from pcbflow.tables import ProjectRow


def _utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=UTC)
        if value.tzinfo is None
        else value.astimezone(UTC)
    )


def _authority(row: ProjectEdaAuthorityRow) -> ProjectEdaAuthority:
    return ProjectEdaAuthority(
        project_id=row.project_id,
        eda_kind=EdaKind(row.eda_kind),
        eda_profile_id=row.eda_profile_id,
        board_profile_id=row.board_profile_id,
        rulepack_digest=row.rulepack_digest,
        canonical_digest=row.canonical_digest,
        created_at=_utc(row.created_at),
    )


def _matches(
    row: ProjectEdaAuthorityRow,
    authority: ProjectEdaAuthorityInput,
    digest: str,
) -> bool:
    return (
        row.eda_kind == authority.eda_kind.value
        and row.eda_profile_id == authority.eda_profile_id
        and row.board_profile_id == authority.board_profile_id
        and row.rulepack_digest == authority.rulepack_digest
        and row.canonical_digest == digest
    )


class ProjectEdaAuthorityStore:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def configure(
        self,
        project_id: str,
        authority: ProjectEdaAuthorityInput,
        idempotency_key: str,
    ) -> ProjectEdaAuthority:
        validate_idempotency_key(idempotency_key)
        validate_authority_input(authority)
        digest = authority_digest(project_id, authority)
        try:
            with self._sessions.begin() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                existing = session.get(ProjectEdaAuthorityRow, project_id)
                if existing is not None:
                    if _matches(existing, authority, digest):
                        return _authority(existing)
                    raise EdaAuthorityConflictError(
                        "project EDA authority is already immutable"
                    )
                if session.get(ProjectRow, project_id) is None:
                    raise ProjectNotFoundError(project_id)
                row = ProjectEdaAuthorityRow(
                    project_id=project_id,
                    eda_kind=authority.eda_kind.value,
                    eda_profile_id=authority.eda_profile_id,
                    board_profile_id=authority.board_profile_id,
                    rulepack_digest=authority.rulepack_digest,
                    idempotency_key=idempotency_key,
                    canonical_digest=digest,
                    created_at=utc_now(),
                )
                session.add(row)
                session.flush()
                return _authority(row)
        except IntegrityError:
            with self._sessions() as session:
                existing = session.get(ProjectEdaAuthorityRow, project_id)
                if existing is not None:
                    if _matches(existing, authority, digest):
                        return _authority(existing)
                    raise EdaAuthorityConflictError(
                        "project EDA authority is already immutable"
                    )
            raise

    def find_by_project_id(self, project_id: str) -> ProjectEdaAuthority | None:
        with self._sessions() as session:
            row = session.get(ProjectEdaAuthorityRow, project_id)
            return _authority(row) if row is not None else None

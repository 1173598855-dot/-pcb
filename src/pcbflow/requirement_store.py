from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from pcbflow.artifacts import ArtifactDescriptor, ContentAddressedStore
from pcbflow.canonical import canonical_json_bytes
from pcbflow.design_tables import RequirementSetRow
from pcbflow.domain import new_id, utc_now
from pcbflow.repositories import IdempotencyConflictError
from pcbflow.requirements import (
    RequirementSet,
    RequirementSetPayload,
    RequirementSetStatus,
    requirement_digest,
)
from pcbflow.tables import ArtifactRow


_REQUIREMENT_MEDIA_TYPE = "application/vnd.pcbflow.requirements+json"


class RequirementSetNotFoundError(LookupError):
    pass


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _requirement_set(row: RequirementSetRow) -> RequirementSet:
    payload = RequirementSetPayload.model_validate_json(
        canonical_json_bytes(row.payload_json), strict=True
    )
    return RequirementSet(
        id=row.id,
        project_id=row.project_id,
        base_revision=row.base_revision,
        schema_version=row.schema_version,
        status=RequirementSetStatus(row.status),
        payload=payload,
        canonical_digest=row.canonical_digest,
        canonical_artifact_digest=row.canonical_artifact_digest,
        idempotency_key=row.idempotency_key,
        submission_idempotency_key=row.submission_idempotency_key,
        candidate_revision=row.candidate_revision,
        candidate_snapshot_digest=row.candidate_snapshot_digest,
        frozen_revision=row.frozen_revision,
        created_at=_utc(row.created_at),
        submitted_at=_utc(row.submitted_at) if row.submitted_at is not None else None,
        frozen_at=_utc(row.frozen_at) if row.frozen_at is not None else None,
    )


class RequirementStore:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        artifacts: ContentAddressedStore,
    ) -> None:
        self._sessions = sessions
        self._artifacts = artifacts

    @staticmethod
    def _register_artifact(
        session: Session, descriptor: ArtifactDescriptor
    ) -> None:
        row = session.get(ArtifactRow, descriptor.digest)
        if row is None:
            session.add(
                ArtifactRow(
                    digest=descriptor.digest,
                    size=descriptor.size,
                    media_type=descriptor.media_type,
                    storage_path=str(descriptor.path),
                    created_at=utc_now(),
                )
            )
            session.flush()
            return
        if (
            row.size != descriptor.size
            or row.media_type != descriptor.media_type
            or Path(row.storage_path) != descriptor.path
        ):
            raise RuntimeError(f"artifact descriptor conflict: {descriptor.digest}")

    @staticmethod
    def _replay_draft(
        row: RequirementSetRow,
        *,
        project_id: str,
        base_revision: str,
        payload_json: dict[str, Any],
        digest: str,
        idempotency_key: str,
    ) -> RequirementSet:
        if (
            row.project_id != project_id
            or row.base_revision != base_revision
            or row.schema_version != payload_json.get("schema_version")
            or row.payload_json != payload_json
            or row.canonical_digest != digest
            or row.canonical_artifact_digest != digest
            or row.idempotency_key != idempotency_key
        ):
            raise IdempotencyConflictError(idempotency_key)
        return _requirement_set(row)

    @staticmethod
    def _replay_submission(
        row: RequirementSetRow,
        *,
        requirement_set_id: str,
        submission_idempotency_key: str,
        candidate_revision: str,
        candidate_snapshot_digest: str,
    ) -> RequirementSet:
        if (
            row.id != requirement_set_id
            or row.submission_idempotency_key != submission_idempotency_key
            or row.candidate_revision != candidate_revision
            or row.candidate_snapshot_digest != candidate_snapshot_digest
        ):
            raise IdempotencyConflictError(submission_idempotency_key)
        return _requirement_set(row)

    def get(self, requirement_set_id: str) -> RequirementSet:
        with self._sessions() as session:
            row = session.get(RequirementSetRow, requirement_set_id)
            if row is None:
                raise RequirementSetNotFoundError(requirement_set_id)
            return _requirement_set(row)

    def find_by_import_key(
        self, project_id: str, idempotency_key: str
    ) -> RequirementSet | None:
        with self._sessions() as session:
            row = session.scalar(
                select(RequirementSetRow).where(
                    RequirementSetRow.project_id == project_id,
                    RequirementSetRow.idempotency_key == idempotency_key,
                )
            )
            return _requirement_set(row) if row is not None else None

    def find_by_submission_key(
        self, project_id: str, idempotency_key: str
    ) -> RequirementSet | None:
        with self._sessions() as session:
            row = session.scalar(
                select(RequirementSetRow).where(
                    RequirementSetRow.project_id == project_id,
                    RequirementSetRow.submission_idempotency_key == idempotency_key,
                )
            )
            return _requirement_set(row) if row is not None else None

    def create_draft(
        self,
        project_id: str,
        base_revision: str,
        payload: RequirementSetPayload,
        idempotency_key: str,
    ) -> RequirementSet:
        if not idempotency_key:
            raise ValueError("idempotency key must not be empty")
        payload_json = payload.model_dump(mode="json")
        canonical = canonical_json_bytes(payload_json)
        digest = requirement_digest(payload)
        existing = self.find_by_import_key(project_id, idempotency_key)
        if existing is not None:
            with self._sessions() as session:
                row = session.get(RequirementSetRow, existing.id)
                assert row is not None
                return self._replay_draft(
                    row,
                    project_id=project_id,
                    base_revision=base_revision,
                    payload_json=payload_json,
                    digest=digest,
                    idempotency_key=idempotency_key,
                )

        descriptor = self._artifacts.put_bytes(canonical, _REQUIREMENT_MEDIA_TYPE)
        values = {
            "project_id": project_id,
            "base_revision": base_revision,
            "payload_json": payload_json,
            "digest": digest,
            "idempotency_key": idempotency_key,
        }
        try:
            with self._sessions.begin() as session:
                row = session.scalar(
                    select(RequirementSetRow).where(
                        RequirementSetRow.project_id == project_id,
                        RequirementSetRow.idempotency_key == idempotency_key,
                    )
                )
                if row is not None:
                    return self._replay_draft(row, **values)
                self._register_artifact(session, descriptor)
                now = utc_now()
                row = RequirementSetRow(
                    id=new_id("reqset"),
                    project_id=project_id,
                    idempotency_key=idempotency_key,
                    submission_idempotency_key=None,
                    base_revision=base_revision,
                    schema_version=payload.schema_version,
                    status=RequirementSetStatus.DRAFT.value,
                    payload_json=payload_json,
                    canonical_digest=digest,
                    canonical_artifact_digest=descriptor.digest,
                    candidate_revision=None,
                    candidate_snapshot_digest=None,
                    frozen_revision=None,
                    created_at=now,
                    submitted_at=None,
                    frozen_at=None,
                )
                session.add(row)
                session.flush()
                result = _requirement_set(row)
            return result
        except IntegrityError:
            with self._sessions() as session:
                row = session.scalar(
                    select(RequirementSetRow).where(
                        RequirementSetRow.project_id == project_id,
                        RequirementSetRow.idempotency_key == idempotency_key,
                    )
                )
                if row is None:
                    raise
                return self._replay_draft(row, **values)

    def replace_payload(
        self, requirement_set_id: str, payload: RequirementSetPayload
    ) -> RequirementSet:
        current = self.get(requirement_set_id)
        if current.status is not RequirementSetStatus.DRAFT:
            raise ValueError("submitted requirement content is immutable")
        payload_json = payload.model_dump(mode="json")
        canonical = canonical_json_bytes(payload_json)
        descriptor = self._artifacts.put_bytes(canonical, _REQUIREMENT_MEDIA_TYPE)
        with self._sessions.begin() as session:
            self._register_artifact(session, descriptor)
            changed = session.execute(
                update(RequirementSetRow)
                .where(
                    RequirementSetRow.id == requirement_set_id,
                    RequirementSetRow.status == RequirementSetStatus.DRAFT.value,
                )
                .values(
                    schema_version=payload.schema_version,
                    payload_json=payload_json,
                    canonical_digest=requirement_digest(payload),
                    canonical_artifact_digest=descriptor.digest,
                )
                .execution_options(synchronize_session=False)
            )
            if changed.rowcount != 1:
                raise ValueError("submitted requirement content is immutable")
            session.expire_all()
            row = session.get(RequirementSetRow, requirement_set_id)
            assert row is not None
            return _requirement_set(row)

    def mark_submitted(
        self,
        requirement_set_id: str,
        submission_idempotency_key: str,
        candidate_revision: str,
        candidate_snapshot_digest: str,
    ) -> RequirementSet:
        if not submission_idempotency_key:
            raise ValueError("idempotency key must not be empty")
        current = self.get(requirement_set_id)
        values = {
            "requirement_set_id": requirement_set_id,
            "submission_idempotency_key": submission_idempotency_key,
            "candidate_revision": candidate_revision,
            "candidate_snapshot_digest": candidate_snapshot_digest,
        }
        try:
            with self._sessions.begin() as session:
                keyed = session.scalar(
                    select(RequirementSetRow).where(
                        RequirementSetRow.project_id == current.project_id,
                        RequirementSetRow.submission_idempotency_key
                        == submission_idempotency_key,
                    )
                )
                if keyed is not None:
                    return self._replay_submission(keyed, **values)
                row = session.get(RequirementSetRow, requirement_set_id)
                if row is None:
                    raise RequirementSetNotFoundError(requirement_set_id)
                if row.submission_idempotency_key is not None:
                    return self._replay_submission(row, **values)
                if row.status != RequirementSetStatus.DRAFT.value:
                    raise ValueError("requirement set is immutable")
                row.submission_idempotency_key = submission_idempotency_key
                row.candidate_revision = candidate_revision
                row.candidate_snapshot_digest = candidate_snapshot_digest
                row.status = RequirementSetStatus.PENDING_APPROVAL.value
                row.submitted_at = utc_now()
                session.flush()
                result = _requirement_set(row)
            return result
        except IntegrityError:
            with self._sessions() as session:
                keyed = session.scalar(
                    select(RequirementSetRow).where(
                        RequirementSetRow.project_id == current.project_id,
                        RequirementSetRow.submission_idempotency_key
                        == submission_idempotency_key,
                    )
                )
                if keyed is None:
                    raise
                return self._replay_submission(keyed, **values)

    def reject(self, requirement_set_id: str) -> RequirementSet:
        with self._sessions.begin() as session:
            row = session.get(RequirementSetRow, requirement_set_id)
            if row is None:
                raise RequirementSetNotFoundError(requirement_set_id)
            if row.status == RequirementSetStatus.REJECTED.value:
                return _requirement_set(row)
            if row.status != RequirementSetStatus.PENDING_APPROVAL.value:
                raise ValueError("requirement set cannot be rejected")
            row.status = RequirementSetStatus.REJECTED.value
            session.flush()
            return _requirement_set(row)

    def approve_and_freeze(
        self, requirement_set_id: str, *, frozen_revision: str
    ) -> RequirementSet:
        with self._sessions.begin() as session:
            row = session.get(RequirementSetRow, requirement_set_id)
            if row is None:
                raise RequirementSetNotFoundError(requirement_set_id)
            if row.status == RequirementSetStatus.FROZEN.value:
                if row.frozen_revision != frozen_revision:
                    raise IdempotencyConflictError(requirement_set_id)
                return _requirement_set(row)
            if row.status != RequirementSetStatus.PENDING_APPROVAL.value:
                raise ValueError("requirement set cannot be frozen")
            if row.candidate_revision != frozen_revision:
                raise ValueError("frozen revision must match requirement candidate")
            row.status = RequirementSetStatus.FROZEN.value
            row.frozen_revision = frozen_revision
            row.frozen_at = utc_now()
            session.flush()
            return _requirement_set(row)

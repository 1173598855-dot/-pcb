from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from pcbflow.artifacts import ArtifactDescriptor
from pcbflow.components import (
    ComponentManifest,
    ComponentRevision,
    component_manifest_digest,
)
from pcbflow.domain import new_id, utc_now
from pcbflow.repositories import IdempotencyConflictError
from pcbflow.tables import ArtifactRow, ComponentRevisionRow


class ComponentRevisionNotFoundError(LookupError):
    pass


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _revision(row: ComponentRevisionRow) -> ComponentRevision:
    return ComponentRevision(
        id=row.id,
        component_key=row.component_key,
        manufacturer=row.manufacturer,
        part_number=row.part_number,
        name=row.name,
        revision=row.revision,
        status=row.status,
        canonical_digest=row.canonical_digest,
        manifest_artifact_digest=row.manifest_artifact_digest,
        datasheet_artifact_digest=row.datasheet_artifact_digest,
        pinout_artifact_digest=row.pinout_artifact_digest,
        symbol_artifact_digest=row.symbol_artifact_digest,
        footprint_artifact_digest=row.footprint_artifact_digest,
        model_3d_artifact_digest=row.model_3d_artifact_digest,
        idempotency_key=row.idempotency_key,
        created_at=_utc(row.created_at),
    )


class ComponentRevisionStore:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @staticmethod
    def _register_artifact(session: Session, descriptor: ArtifactDescriptor) -> None:
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
        if row.size != descriptor.size or row.media_type != descriptor.media_type or Path(row.storage_path) != descriptor.path:
            raise RuntimeError(f"artifact descriptor conflict: {descriptor.digest}")

    @staticmethod
    def _expected_digests(manifest: ComponentManifest, artifacts: tuple[ArtifactDescriptor, ...]) -> tuple[str, ...]:
        expected = [
            component_manifest_digest(manifest),
            manifest.datasheet.digest,
            manifest.pinout.digest,
            manifest.symbol.digest,
            manifest.footprint.digest,
        ]
        if manifest.model_3d is not None:
            expected.append(manifest.model_3d.digest)
        if len(artifacts) != len(expected) or tuple(item.digest for item in artifacts) != tuple(expected):
            raise ValueError("component artifact descriptors do not match manifest")
        return tuple(expected)

    @staticmethod
    def _descriptor_matches(
        session: Session, descriptor: ArtifactDescriptor
    ) -> bool:
        row = session.get(ArtifactRow, descriptor.digest)
        return row is not None and (
            row.size == descriptor.size
            and row.media_type == descriptor.media_type
            and Path(row.storage_path) == descriptor.path
        )

    @classmethod
    def _matches(
        cls,
        session: Session,
        row: ComponentRevisionRow,
        manifest: ComponentManifest,
        digest: str,
        artifacts: tuple[ArtifactDescriptor, ...],
    ) -> bool:
        expected = ComponentRevisionStore._expected_digests(manifest, artifacts)
        return (
            row.component_key == manifest.component_key
            and row.revision == manifest.revision
            and row.canonical_digest == digest
            and row.manifest_artifact_digest == expected[0]
            and row.datasheet_artifact_digest == expected[1]
            and row.pinout_artifact_digest == expected[2]
            and row.symbol_artifact_digest == expected[3]
            and row.footprint_artifact_digest == expected[4]
            and row.model_3d_artifact_digest == (expected[5] if len(expected) == 6 else None)
            and all(cls._descriptor_matches(session, descriptor) for descriptor in artifacts)
        )

    def create(self, *, manifest: ComponentManifest, canonical_digest: str, idempotency_key: str, artifacts: tuple[ArtifactDescriptor, ...]) -> ComponentRevision:
        if not idempotency_key:
            raise ValueError("idempotency key must not be empty")
        if canonical_digest != component_manifest_digest(manifest):
            raise IdempotencyConflictError(idempotency_key)
        self._expected_digests(manifest, artifacts)
        try:
            with self._sessions.begin() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                keyed = session.scalar(select(ComponentRevisionRow).where(ComponentRevisionRow.idempotency_key == idempotency_key))
                if keyed is not None:
                    if not self._matches(session, keyed, manifest, canonical_digest, artifacts):
                        raise IdempotencyConflictError(idempotency_key)
                    return _revision(keyed)
                identity = session.scalar(select(ComponentRevisionRow).where(ComponentRevisionRow.component_key == manifest.component_key, ComponentRevisionRow.revision == manifest.revision))
                if identity is not None:
                    if not self._matches(session, identity, manifest, canonical_digest, artifacts):
                        raise IdempotencyConflictError(idempotency_key)
                    return _revision(identity)
                for descriptor in artifacts:
                    try:
                        self._register_artifact(session, descriptor)
                    except RuntimeError as error:
                        raise IdempotencyConflictError(idempotency_key) from error
                row = ComponentRevisionRow(
                    id=new_id("comprev"), component_key=manifest.component_key,
                    manufacturer=manifest.manufacturer, part_number=manifest.part_number,
                    name=manifest.name, revision=manifest.revision, status=manifest.status,
                    canonical_digest=canonical_digest,
                    manifest_artifact_digest=artifacts[0].digest,
                    datasheet_artifact_digest=artifacts[1].digest,
                    pinout_artifact_digest=artifacts[2].digest,
                    symbol_artifact_digest=artifacts[3].digest,
                    footprint_artifact_digest=artifacts[4].digest,
                    model_3d_artifact_digest=artifacts[5].digest if len(artifacts) == 6 else None,
                    idempotency_key=idempotency_key, created_at=utc_now(),
                )
                session.add(row)
                session.flush()
                return _revision(row)
        except IntegrityError:
            with self._sessions() as session:
                row = session.scalar(select(ComponentRevisionRow).where(ComponentRevisionRow.idempotency_key == idempotency_key))
                if row is None:
                    row = session.scalar(select(ComponentRevisionRow).where(ComponentRevisionRow.component_key == manifest.component_key, ComponentRevisionRow.revision == manifest.revision))
                if row is None or not self._matches(session, row, manifest, canonical_digest, artifacts):
                    raise IdempotencyConflictError(idempotency_key)
                return _revision(row)

    def get(self, revision_id: str) -> ComponentRevision:
        with self._sessions() as session:
            row = session.get(ComponentRevisionRow, revision_id)
            if row is None:
                raise ComponentRevisionNotFoundError(revision_id)
            return _revision(row)

    def find_by_idempotency_key(self, idempotency_key: str) -> ComponentRevision | None:
        with self._sessions() as session:
            row = session.scalar(
                select(ComponentRevisionRow).where(
                    ComponentRevisionRow.idempotency_key == idempotency_key
                )
            )
            return _revision(row) if row is not None else None

    def find_by_identity(
        self, component_key: str, revision: str
    ) -> ComponentRevision | None:
        with self._sessions() as session:
            row = session.scalar(
                select(ComponentRevisionRow).where(
                    ComponentRevisionRow.component_key == component_key,
                    ComponentRevisionRow.revision == revision,
                )
            )
            return _revision(row) if row is not None else None

    def list_for_component(self, component_key: str) -> tuple[ComponentRevision, ...]:
        with self._sessions() as session:
            rows = session.scalars(select(ComponentRevisionRow).where(ComponentRevisionRow.component_key == component_key).order_by(ComponentRevisionRow.created_at, ComponentRevisionRow.id)).all()
            return tuple(_revision(row) for row in rows)

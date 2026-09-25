from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from pcbflow.component_bindings import (
    ComponentModuleBinding,
    ComponentModuleBindingNotFoundError,
)
from pcbflow.domain import new_id, utc_now
from pcbflow.repositories import IdempotencyConflictError
from pcbflow.tables import ComponentModuleBindingRow


class ComponentModuleBindingConflictError(RuntimeError):
    pass


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _binding(row: ComponentModuleBindingRow) -> ComponentModuleBinding:
    return ComponentModuleBinding(
        id=row.id,
        component_revision_id=row.component_revision_id,
        kicad_major=row.kicad_major,
        module_revision_id=row.module_revision_id,
        module_manifest_digest=row.module_manifest_digest,
        idempotency_key=row.idempotency_key,
        created_at=_utc(row.created_at),
    )


def _matches(
    row: ComponentModuleBindingRow,
    *,
    component_revision_id: str,
    kicad_major: int,
    module_revision_id: str,
    module_manifest_digest: str,
) -> bool:
    return (
        row.component_revision_id == component_revision_id
        and row.kicad_major == kicad_major
        and row.module_revision_id == module_revision_id
        and row.module_manifest_digest == module_manifest_digest
    )


class ComponentModuleBindingStore:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def create(
        self,
        *,
        component_revision_id: str,
        kicad_major: int,
        module_revision_id: str,
        module_manifest_digest: str,
        idempotency_key: str,
    ) -> ComponentModuleBinding:
        if not idempotency_key:
            raise ValueError("idempotency key must not be empty")
        if kicad_major <= 0:
            raise ValueError("KiCad major version must be positive")
        try:
            with self._sessions.begin() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                keyed = session.scalar(
                    select(ComponentModuleBindingRow).where(
                        ComponentModuleBindingRow.idempotency_key == idempotency_key
                    )
                )
                if keyed is not None:
                    if not _matches(
                        keyed,
                        component_revision_id=component_revision_id,
                        kicad_major=kicad_major,
                        module_revision_id=module_revision_id,
                        module_manifest_digest=module_manifest_digest,
                    ):
                        raise IdempotencyConflictError(idempotency_key)
                    return _binding(keyed)

                slot = session.scalar(
                    select(ComponentModuleBindingRow).where(
                        ComponentModuleBindingRow.component_revision_id
                        == component_revision_id,
                        ComponentModuleBindingRow.kicad_major == kicad_major,
                    )
                )
                if slot is not None:
                    if not _matches(
                        slot,
                        component_revision_id=component_revision_id,
                        kicad_major=kicad_major,
                        module_revision_id=module_revision_id,
                        module_manifest_digest=module_manifest_digest,
                    ):
                        raise ComponentModuleBindingConflictError(
                            component_revision_id, kicad_major
                        )
                    return _binding(slot)

                row = ComponentModuleBindingRow(
                    id=new_id("compmod"),
                    component_revision_id=component_revision_id,
                    kicad_major=kicad_major,
                    module_revision_id=module_revision_id,
                    module_manifest_digest=module_manifest_digest,
                    idempotency_key=idempotency_key,
                    created_at=utc_now(),
                )
                session.add(row)
                session.flush()
                return _binding(row)
        except IntegrityError as error:
            with self._sessions() as session:
                keyed = session.scalar(
                    select(ComponentModuleBindingRow).where(
                        ComponentModuleBindingRow.idempotency_key == idempotency_key
                    )
                )
                if keyed is not None:
                    if _matches(
                        keyed,
                        component_revision_id=component_revision_id,
                        kicad_major=kicad_major,
                        module_revision_id=module_revision_id,
                        module_manifest_digest=module_manifest_digest,
                    ):
                        return _binding(keyed)
                    raise IdempotencyConflictError(idempotency_key) from error
                slot = session.scalar(
                    select(ComponentModuleBindingRow).where(
                        ComponentModuleBindingRow.component_revision_id
                        == component_revision_id,
                        ComponentModuleBindingRow.kicad_major == kicad_major,
                    )
                )
                if slot is not None:
                    if _matches(
                        slot,
                        component_revision_id=component_revision_id,
                        kicad_major=kicad_major,
                        module_revision_id=module_revision_id,
                        module_manifest_digest=module_manifest_digest,
                    ):
                        return _binding(slot)
                    raise ComponentModuleBindingConflictError(
                        component_revision_id, kicad_major
                    ) from error
            raise

    def list_for_component_revision(
        self, component_revision_id: str
    ) -> tuple[ComponentModuleBinding, ...]:
        with self._sessions() as session:
            rows = session.scalars(
                select(ComponentModuleBindingRow)
                .where(
                    ComponentModuleBindingRow.component_revision_id
                    == component_revision_id
                )
                .order_by(
                    ComponentModuleBindingRow.created_at,
                    ComponentModuleBindingRow.id,
                )
            ).all()
            return tuple(_binding(row) for row in rows)

    def get(self, binding_id: str) -> ComponentModuleBinding:
        with self._sessions() as session:
            row = session.scalar(
                select(ComponentModuleBindingRow).where(
                    ComponentModuleBindingRow.id == binding_id
                )
            )
            if row is None:
                raise ComponentModuleBindingNotFoundError(binding_id)
            return _binding(row)

    def find_by_idempotency_key(
        self, idempotency_key: str
    ) -> ComponentModuleBinding | None:
        with self._sessions() as session:
            row = session.scalar(
                select(ComponentModuleBindingRow).where(
                    ComponentModuleBindingRow.idempotency_key == idempotency_key
                )
            )
            return _binding(row) if row is not None else None

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from pcbflow.component_binding_store import (
    ComponentModuleBindingConflictError,
    ComponentModuleBindingStore,
)
from pcbflow.repositories import IdempotencyConflictError
from tests.component_fixtures import build_component_directory


def _import_component(container):
    component_directory = build_component_directory(
        container.settings.data_dir / "binding-component", include_model=True
    )
    return container.components.import_revision(
        component_directory / "component.yaml", "component-binding-import"
    )


def test_store_creates_replays_and_lists_bindings(container) -> None:
    component = _import_component(container)
    store = ComponentModuleBindingStore(container.sessions)
    created = store.create(
        component_revision_id=component.id,
        kicad_major=10,
        module_revision_id="modrev_status_led_v1",
        module_manifest_digest="sha256:" + "a" * 64,
        idempotency_key="component-module-1",
    )
    replayed = store.create(
        component_revision_id=component.id,
        kicad_major=10,
        module_revision_id="modrev_status_led_v1",
        module_manifest_digest="sha256:" + "a" * 64,
        idempotency_key="component-module-1",
    )

    assert replayed == created
    assert store.list_for_component_revision(component.id) == (created,)


def test_store_rejects_conflicting_slot_and_idempotency_key(container) -> None:
    component = _import_component(container)
    store = ComponentModuleBindingStore(container.sessions)
    store.create(
        component_revision_id=component.id,
        kicad_major=9,
        module_revision_id="modrev_status_led_v1",
        module_manifest_digest="sha256:" + "a" * 64,
        idempotency_key="component-module-slot-1",
    )

    with pytest.raises(ComponentModuleBindingConflictError):
        store.create(
            component_revision_id=component.id,
            kicad_major=9,
            module_revision_id="modrev_other_v1",
            module_manifest_digest="sha256:" + "b" * 64,
            idempotency_key="component-module-slot-2",
        )
    with pytest.raises(IdempotencyConflictError):
        store.create(
            component_revision_id=component.id,
            kicad_major=10,
            module_revision_id="modrev_other_v1",
            module_manifest_digest="sha256:" + "b" * 64,
            idempotency_key="component-module-slot-1",
        )


def test_store_reserves_write_path_before_binding_reads(
    container, monkeypatch: pytest.MonkeyPatch
) -> None:
    component = _import_component(container)
    statements: list[str] = []
    execute = Session.execute
    scalar = Session.scalar

    def record_statement(statement) -> None:
        sql = getattr(statement, "text", None) or str(statement)
        if sql == "BEGIN IMMEDIATE" or sql.lstrip().upper().startswith("SELECT"):
            statements.append(sql)

    def record_execute(self, statement, *args, **kwargs):
        record_statement(statement)
        return execute(self, statement, *args, **kwargs)

    def record_scalar(self, statement, *args, **kwargs):
        record_statement(statement)
        return scalar(self, statement, *args, **kwargs)

    monkeypatch.setattr(Session, "execute", record_execute)
    monkeypatch.setattr(Session, "scalar", record_scalar)
    ComponentModuleBindingStore(container.sessions).create(
        component_revision_id=component.id,
        kicad_major=10,
        module_revision_id="modrev_status_led_v1",
        module_manifest_digest="sha256:" + "a" * 64,
        idempotency_key="component-module-write-reservation",
    )

    assert statements[0] == "BEGIN IMMEDIATE"
    binding_selects = [
        statement
        for statement in statements
        if "FROM component_module_bindings" in statement
    ]
    assert binding_selects
    assert statements.index("BEGIN IMMEDIATE") < statements.index(binding_selects[0])

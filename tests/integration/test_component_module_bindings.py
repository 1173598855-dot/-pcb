from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from tests.component_fixtures import build_component_directory

from pcbflow.component_binding_store import (
    ComponentModuleBindingConflictError,
    ComponentModuleBindingStore,
)
from pcbflow.component_bindings import (
    ComponentModuleBindingNotFoundError,
    ComponentModuleBindingService,
)
from pcbflow.config import Settings
from pcbflow.container import build_container
from pcbflow.repositories import IdempotencyConflictError
from pcbflow.schematic.modules import FileModuleCatalog


def _import_component(container):
    component_directory = build_component_directory(
        container.settings.data_dir / "binding-component", include_model=True
    )
    return container.components.import_revision(
        component_directory / "component.yaml", "component-binding-import"
    )


def _module_fixture_root() -> Path:
    return Path(__file__).parents[1] / "fixtures" / "modules"


def _catalog_container(tmp_path: Path):
    return build_container(
        Settings.from_env(
            {
                "PCBFLOW_DATA_DIR": str(tmp_path / "service-data"),
                "PCBFLOW_DATABASE_URL": (
                    f"sqlite+pysqlite:///{(tmp_path / 'pcbflow.db').as_posix()}"
                ),
                "PCBFLOW_MODULE_CATALOG_DIR": str(_module_fixture_root()),
            }
        )
    )


def _binding_inputs(component_revision_id: str, **overrides: object) -> dict[str, object]:
    inputs: dict[str, object] = {
        "component_revision_id": component_revision_id,
        "kicad_major": 10,
        "module_revision_id": "modrev_status_led_v1",
        "module_manifest_digest": "sha256:" + "a" * 64,
        "idempotency_key": "component-module-binding",
    }
    inputs.update(overrides)
    return inputs


def _force_integrity_error_after_hidden_binding_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scalar = Session.scalar
    flush = Session.flush
    hidden_reads = 0
    failed_flushes = 0

    def hide_initial_reads(self, statement, *args, **kwargs):
        nonlocal hidden_reads
        if hidden_reads < 2:
            hidden_reads += 1
            return None
        return scalar(self, statement, *args, **kwargs)

    def fail_flush(self, *args, **kwargs):
        nonlocal failed_flushes
        if failed_flushes == 0:
            failed_flushes += 1
            raise IntegrityError("INSERT", {}, RuntimeError("forced duplicate"))
        return flush(self, *args, **kwargs)

    monkeypatch.setattr(Session, "scalar", hide_initial_reads)
    monkeypatch.setattr(Session, "flush", fail_flush)


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


def test_store_gets_a_binding_or_raises_its_stable_not_found_error(container) -> None:
    component = _import_component(container)
    store = ComponentModuleBindingStore(container.sessions)
    created = store.create(
        component_revision_id=component.id,
        kicad_major=10,
        module_revision_id="modrev_status_led_v1",
        module_manifest_digest="sha256:" + "a" * 64,
        idempotency_key="component-module-get",
    )

    assert store.get(created.id) == created
    with pytest.raises(ComponentModuleBindingNotFoundError) as raised:
        store.get("compmod_missing")
    assert raised.value.code == "COMPONENT_MODULE_BINDING_NOT_FOUND"


@pytest.mark.parametrize(
    ("kicad_major", "idempotency_key", "message"),
    ((0, "component-module-invalid", "positive"), (10, "", "idempotency")),
)
def test_store_rejects_invalid_write_inputs(
    container, kicad_major: int, idempotency_key: str, message: str
) -> None:
    store = ComponentModuleBindingStore(container.sessions)

    with pytest.raises(ValueError, match=message):
        store.create(
            **_binding_inputs(
                "comprev_unused",
                kicad_major=kicad_major,
                idempotency_key=idempotency_key,
            )
        )


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


def test_store_recovers_identical_binding_after_integrity_error(
    container, monkeypatch: pytest.MonkeyPatch
) -> None:
    component = _import_component(container)
    store = ComponentModuleBindingStore(container.sessions)
    inputs = _binding_inputs(component.id, idempotency_key="component-module-race")
    winner = store.create(**inputs)

    _force_integrity_error_after_hidden_binding_reads(monkeypatch)

    assert store.create(**inputs) == winner


def test_store_recovers_idempotency_conflict_after_integrity_error(
    container, monkeypatch: pytest.MonkeyPatch
) -> None:
    component = _import_component(container)
    store = ComponentModuleBindingStore(container.sessions)
    store.create(
        **_binding_inputs(component.id, idempotency_key="component-module-race")
    )

    _force_integrity_error_after_hidden_binding_reads(monkeypatch)

    with pytest.raises(IdempotencyConflictError):
        store.create(
            **_binding_inputs(
                component.id,
                module_revision_id="modrev_other_v1",
                module_manifest_digest="sha256:" + "b" * 64,
                idempotency_key="component-module-race",
            )
        )


def test_store_recovers_slot_conflict_after_integrity_error(
    container, monkeypatch: pytest.MonkeyPatch
) -> None:
    component = _import_component(container)
    store = ComponentModuleBindingStore(container.sessions)
    store.create(
        **_binding_inputs(component.id, idempotency_key="component-module-winner")
    )

    _force_integrity_error_after_hidden_binding_reads(monkeypatch)

    with pytest.raises(ComponentModuleBindingConflictError):
        store.create(
            **_binding_inputs(
                component.id,
                module_revision_id="modrev_other_v1",
                module_manifest_digest="sha256:" + "b" * 64,
                idempotency_key="component-module-race",
            )
        )


def test_store_reraises_unrecovered_integrity_error(
    container, monkeypatch: pytest.MonkeyPatch
) -> None:
    component = _import_component(container)
    store = ComponentModuleBindingStore(container.sessions)

    _force_integrity_error_after_hidden_binding_reads(monkeypatch)

    with pytest.raises(IntegrityError, match="forced duplicate"):
        store.create(
            **_binding_inputs(component.id, idempotency_key="component-module-race")
        )


def test_configured_container_binds_catalog_modules_for_each_supported_major(
    tmp_path: Path,
) -> None:
    services = _catalog_container(tmp_path)
    try:
        component = _import_component(services)
        expected_digest = FileModuleCatalog(
            _module_fixture_root(), max_files=10_000, max_bytes=1_000_000_000
        ).get("modrev_status_led_v1").manifest_digest

        nine = services.component_module_bindings.bind(
            component.id, 9, "modrev_status_led_v1", "component-module-major-9"
        )
        ten = services.component_module_bindings.bind(
            component.id, 10, "modrev_status_led_v1", "component-module-major-10"
        )

        assert (nine.module_manifest_digest, ten.module_manifest_digest) == (
            expected_digest,
            expected_digest,
        )
        assert services.component_module_binding_store.list_for_component_revision(
            component.id
        ) == (nine, ten)
    finally:
        services.dispose()


def test_service_conflicts_when_catalog_digest_changes_for_a_bound_slot(
    container,
) -> None:
    component = _import_component(container)
    revisions = iter(
        (
            SimpleNamespace(
                manifest=SimpleNamespace(
                    status="verified",
                    kicad_majors=(10,),
                    module_revision_id="modrev_status_led_v1",
                ),
                manifest_digest="sha256:" + "a" * 64,
            ),
            SimpleNamespace(
                manifest=SimpleNamespace(
                    status="verified",
                    kicad_majors=(10,),
                    module_revision_id="modrev_status_led_v1",
                ),
                manifest_digest="sha256:" + "b" * 64,
            ),
        )
    )

    class SequencedCatalog:
        def get(self, module_revision_id: str) -> object:
            assert module_revision_id == "modrev_status_led_v1"
            return next(revisions)

    service = ComponentModuleBindingService(
        container.component_store,
        ComponentModuleBindingStore(container.sessions),
        SequencedCatalog(),
    )
    service.bind(
        component.id, 10, "modrev_status_led_v1", "component-module-first-digest"
    )

    with pytest.raises(ComponentModuleBindingConflictError):
        service.bind(
            component.id, 10, "modrev_status_led_v1", "component-module-second-digest"
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

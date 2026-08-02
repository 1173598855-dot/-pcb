from __future__ import annotations

from types import SimpleNamespace

import pytest

from pcbflow.component_bindings import (
    ComponentModuleBindingService,
    ModuleCatalogUnavailableError,
    ModuleKicadMajorUnsupportedError,
)
from pcbflow.schematic.modules import ModuleIntegrityError, ModuleRevisionNotFoundError


class FakeComponentStore:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def get(self, component_revision_id: str) -> object:
        self.calls.append(component_revision_id)
        return SimpleNamespace(id=component_revision_id, status="verified")


class RecordingBindingStore:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.list_calls: list[str] = []

    def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return SimpleNamespace(**kwargs)

    def list_for_component_revision(self, component_revision_id: str) -> tuple[object, ...]:
        self.list_calls.append(component_revision_id)
        return ()


class FakeCatalog:
    def __init__(
        self, *, majors: tuple[int, ...], digest: str, status: str = "verified"
    ) -> None:
        self._revision = SimpleNamespace(
            manifest=SimpleNamespace(
                status=status,
                kicad_majors=majors,
                module_revision_id="modrev_status_led_v1",
            ),
            manifest_digest=digest,
        )

    def get(self, module_revision_id: str) -> object:
        assert module_revision_id == "modrev_status_led_v1"
        return self._revision


class IntegrityFailingCatalog:
    def get(self, module_revision_id: str) -> object:
        raise ModuleIntegrityError(module_revision_id)


class MissingCatalogRevision:
    def get(self, module_revision_id: str) -> object:
        raise ModuleRevisionNotFoundError(module_revision_id)


class UnexpectedCatalog:
    def get(self, module_revision_id: str) -> object:
        raise AssertionError("catalog must not be read for an invalid KiCad major")


def test_bind_rejects_missing_catalog() -> None:
    service = ComponentModuleBindingService(
        FakeComponentStore(), RecordingBindingStore(), None
    )

    with pytest.raises(ModuleCatalogUnavailableError):
        service.bind("comprev_fixture", 10, "modrev_status_led_v1", "missing-catalog")


def test_bind_rejects_a_non_positive_major_before_reading_the_catalog() -> None:
    service = ComponentModuleBindingService(
        FakeComponentStore(), RecordingBindingStore(), UnexpectedCatalog()
    )

    with pytest.raises(ValueError, match="positive"):
        service.bind("comprev_fixture", 0, "modrev_status_led_v1", "invalid-major")


def test_bind_requires_the_module_to_support_the_requested_major() -> None:
    service = ComponentModuleBindingService(
        FakeComponentStore(),
        RecordingBindingStore(),
        FakeCatalog(majors=(9,), digest="sha256:" + "a" * 64),
    )

    with pytest.raises(ModuleKicadMajorUnsupportedError):
        service.bind("comprev_fixture", 10, "modrev_status_led_v1", "bad-major")


def test_bind_forwards_the_catalog_manifest_digest() -> None:
    bindings = RecordingBindingStore()
    digest = "sha256:" + "b" * 64
    service = ComponentModuleBindingService(
        FakeComponentStore(), bindings, FakeCatalog(majors=(9, 10), digest=digest)
    )

    service.bind("comprev_fixture", 10, "modrev_status_led_v1", "freeze-digest")

    assert bindings.calls == [
        {
            "component_revision_id": "comprev_fixture",
            "kicad_major": 10,
            "module_revision_id": "modrev_status_led_v1",
            "module_manifest_digest": digest,
            "idempotency_key": "freeze-digest",
        }
    ]


def test_bind_maps_catalog_integrity_failure_to_unavailable() -> None:
    service = ComponentModuleBindingService(
        FakeComponentStore(), RecordingBindingStore(), IntegrityFailingCatalog()
    )

    with pytest.raises(ModuleCatalogUnavailableError):
        service.bind("comprev_fixture", 10, "modrev_status_led_v1", "bad-catalog")


def test_bind_rejects_an_unverified_module_revision() -> None:
    service = ComponentModuleBindingService(
        FakeComponentStore(),
        RecordingBindingStore(),
        FakeCatalog(
            majors=(10,), digest="sha256:" + "a" * 64, status="unverified"
        ),
    )

    with pytest.raises(ModuleCatalogUnavailableError, match="not verified"):
        service.bind("comprev_fixture", 10, "modrev_status_led_v1", "unverified")


def test_bind_preserves_module_not_found_error() -> None:
    service = ComponentModuleBindingService(
        FakeComponentStore(), RecordingBindingStore(), MissingCatalogRevision()
    )

    with pytest.raises(ModuleRevisionNotFoundError):
        service.bind("comprev_fixture", 10, "modrev_status_led_v1", "missing-module")


def test_list_requires_an_existing_component_without_reading_catalog() -> None:
    components = FakeComponentStore()
    bindings = RecordingBindingStore()
    service = ComponentModuleBindingService(components, bindings, None)

    assert service.list_for_component_revision("comprev_fixture") == ()
    assert components.calls == ["comprev_fixture"]
    assert bindings.list_calls == ["comprev_fixture"]

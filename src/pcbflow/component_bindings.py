from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from pcbflow.schematic.modules import ModuleCatalogPort, ModuleIntegrityError

if TYPE_CHECKING:
    from pcbflow.component_binding_store import ComponentModuleBindingStore
    from pcbflow.component_store import ComponentRevisionStore


@dataclass(frozen=True, slots=True)
class ComponentModuleBinding:
    id: str
    component_revision_id: str
    kicad_major: int
    module_revision_id: str
    module_manifest_digest: str
    idempotency_key: str
    created_at: datetime


class ModuleCatalogUnavailableError(RuntimeError):
    pass


class ModuleKicadMajorUnsupportedError(ValueError):
    def __init__(self, module_revision_id: str, kicad_major: int) -> None:
        super().__init__(
            f"module revision {module_revision_id} does not support KiCad major "
            f"{kicad_major}"
        )


class ComponentModuleBindingService:
    def __init__(
        self,
        component_store: ComponentRevisionStore,
        binding_store: ComponentModuleBindingStore,
        module_catalog: ModuleCatalogPort | None,
    ) -> None:
        self._component_store = component_store
        self._binding_store = binding_store
        self._module_catalog = module_catalog

    def bind(
        self,
        component_revision_id: str,
        kicad_major: int,
        module_revision_id: str,
        idempotency_key: str,
    ) -> ComponentModuleBinding:
        self._component_store.get(component_revision_id)
        if kicad_major <= 0:
            raise ValueError("KiCad major must be positive")
        if self._module_catalog is None:
            raise ModuleCatalogUnavailableError("module catalog is not configured")
        try:
            module = self._module_catalog.get(module_revision_id)
        except ModuleIntegrityError as error:
            raise ModuleCatalogUnavailableError(
                "module catalog is unavailable"
            ) from error
        if module.manifest.status != "verified":
            raise ModuleCatalogUnavailableError("module revision is not verified")
        if kicad_major not in module.manifest.kicad_majors:
            raise ModuleKicadMajorUnsupportedError(module_revision_id, kicad_major)
        return self._binding_store.create(
            component_revision_id=component_revision_id,
            kicad_major=kicad_major,
            module_revision_id=module.manifest.module_revision_id,
            module_manifest_digest=module.manifest_digest,
            idempotency_key=idempotency_key,
        )

    def list_for_component_revision(
        self, component_revision_id: str
    ) -> tuple[ComponentModuleBinding, ...]:
        self._component_store.get(component_revision_id)
        return self._binding_store.list_for_component_revision(component_revision_id)

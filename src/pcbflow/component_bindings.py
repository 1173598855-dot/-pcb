from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Protocol

from pcbflow.schematic.modules import (
    ModuleCatalogPort,
    ModuleIntegrityError,
    ModuleRevision,
)

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


@dataclass(frozen=True, slots=True)
class BoundModuleResolution:
    binding: ComponentModuleBinding
    module: ModuleRevision


class BoundModuleResolverPort(Protocol):
    def resolve_for_instantiation(
        self, binding_id: str, kicad_major: int
    ) -> BoundModuleResolution: ...


class ComponentModuleBindingNotFoundError(LookupError):
    code = "COMPONENT_MODULE_BINDING_NOT_FOUND"

    def __init__(self, binding_id: str) -> None:
        self.binding_id = binding_id
        super().__init__(f"component module binding not found: {binding_id}")


class ComponentModuleBindingKicadMajorMismatchError(ValueError):
    code = "COMPONENT_MODULE_BINDING_KICAD_MAJOR_MISMATCH"

    def __init__(
        self, binding_id: str, binding_kicad_major: int, active_kicad_major: int
    ) -> None:
        self.binding_id = binding_id
        self.binding_kicad_major = binding_kicad_major
        self.active_kicad_major = active_kicad_major
        super().__init__(
            f"component module binding {binding_id} KiCad major "
            f"{binding_kicad_major} does not match active KiCad major "
            f"{active_kicad_major}"
        )


class ComponentModuleBindingDigestMismatchError(ValueError):
    code = "COMPONENT_MODULE_BINDING_DIGEST_MISMATCH"

    def __init__(
        self,
        binding_id: str,
        module_revision_id: str,
        frozen_manifest_digest: str,
        observed_manifest_digest: str,
    ) -> None:
        self.binding_id = binding_id
        self.module_revision_id = module_revision_id
        self.frozen_manifest_digest = frozen_manifest_digest
        self.observed_manifest_digest = observed_manifest_digest
        super().__init__(
            f"component module binding {binding_id} manifest digest does not "
            "match its frozen digest"
        )


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

    def resolve_for_instantiation(
        self, binding_id: str, kicad_major: int
    ) -> BoundModuleResolution:
        binding = self._binding_store.get(binding_id)
        if binding.kicad_major != kicad_major:
            raise ComponentModuleBindingKicadMajorMismatchError(
                binding.id, binding.kicad_major, kicad_major
            )
        if self._module_catalog is None:
            raise ModuleCatalogUnavailableError("module catalog is not configured")
        try:
            module = self._module_catalog.get(binding.module_revision_id)
        except ModuleIntegrityError as error:
            raise ModuleCatalogUnavailableError(
                "module catalog is unavailable"
            ) from error
        if module.manifest.status != "verified":
            raise ModuleCatalogUnavailableError("module revision is not verified")
        if module.manifest.module_revision_id != binding.module_revision_id:
            raise ComponentModuleBindingDigestMismatchError(
                binding.id,
                binding.module_revision_id,
                binding.module_manifest_digest,
                module.manifest_digest,
            )
        if kicad_major not in module.manifest.kicad_majors:
            raise ModuleKicadMajorUnsupportedError(binding.module_revision_id, kicad_major)
        if module.manifest_digest != binding.module_manifest_digest:
            raise ComponentModuleBindingDigestMismatchError(
                binding.id,
                binding.module_revision_id,
                binding.module_manifest_digest,
                module.manifest_digest,
            )
        return BoundModuleResolution(binding=binding, module=module)

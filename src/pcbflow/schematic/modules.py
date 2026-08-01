from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Protocol
from uuid import UUID, uuid5

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from pcbflow.canonical import canonical_digest
from pcbflow.schematic.cst import CstAtom, CstList, parse_cst


MODULE_UUID_NAMESPACE = UUID("9bcf613a-1ced-5b76-9a90-8fd90ac5f16d")
_ALLOWED_SUFFIXES = frozenset((".yaml", ".kicad_sch"))
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_PORT_DIRECTIONS = frozenset(("input", "output", "bidirectional", "tri_state", "passive"))


class ModuleIntegrityError(ValueError):
    pass


class ModuleRevisionNotFoundError(LookupError):
    pass


class FootprintRevisionNotFoundError(LookupError):
    pass


class _StrictManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class TemplateEntry(_StrictManifest):
    path: str = Field(pattern=r"^[A-Za-z0-9_.-]+\.kicad_sch$")
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class ParameterEntry(_StrictManifest):
    property_name: str = Field(min_length=1)
    required: bool


class FootprintEntry(_StrictManifest):
    library_id: str = Field(pattern=r"^[A-Za-z0-9_.+-]+:[A-Za-z0-9_.+-]+$")
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class _ManifestBase(_StrictManifest):
    module_revision_id: str = Field(pattern=r"^modrev_[A-Za-z0-9_-]+$")
    status: str = Field(pattern=r"^verified$")
    name: str = Field(min_length=1)
    adapter_contract: str = Field(pattern=r"^pcbflow\.schematic\.cst\.v1$")
    template: TemplateEntry
    uuid_bindings: dict[str, str]
    parameters: dict[str, ParameterEntry]
    ports: dict[str, str]
    footprints: dict[str, FootprintEntry]

    @field_validator("uuid_bindings")
    @classmethod
    def canonical_uuid_bindings(cls, values: dict[str, str]) -> dict[str, str]:
        for value, name in values.items():
            try:
                if str(UUID(value)) != value.lower():
                    raise ValueError
            except ValueError as error:
                raise ValueError("uuid_bindings keys must be canonical UUIDs") from error
            if not name:
                raise ValueError("uuid_bindings values must not be empty")
        return values

    @field_validator("ports")
    @classmethod
    def supported_port_directions(cls, values: dict[str, str]) -> dict[str, str]:
        if not all(name and direction in _PORT_DIRECTIONS for name, direction in values.items()):
            raise ValueError("ports contain an unsupported direction")
        return values


class _ManifestModelV1(_ManifestBase):
    schema_version: str = Field(pattern=r"^1\.0$")
    kicad_major: int = Field(ge=1)


class _ManifestModelV1_1(_ManifestBase):
    schema_version: str = Field(pattern=r"^1\.1$")
    kicad_majors: list[int] = Field(min_length=1)

    @field_validator("kicad_majors")
    @classmethod
    def canonical_kicad_majors(cls, values: list[int]) -> list[int]:
        if values != sorted(set(values)) or any(value < 1 for value in values):
            raise ValueError("kicad_majors must be sorted unique positive integers")
        return values


@dataclass(frozen=True, slots=True)
class ModuleManifest:
    schema_version: str
    module_revision_id: str
    status: str
    name: str
    kicad_majors: tuple[int, ...]
    adapter_contract: str
    template: TemplateEntry
    uuid_bindings: Mapping[str, str]
    parameters: Mapping[str, ParameterEntry]
    ports: Mapping[str, str]
    footprints: Mapping[str, FootprintEntry]

    @property
    def kicad_major(self) -> int:
        """Legacy accessor for callers that only support one major."""
        return self.kicad_majors[0]


@dataclass(frozen=True, slots=True)
class ModuleRevision:
    manifest: ModuleManifest
    manifest_digest: str
    template_id: str
    template_bytes: bytes


@dataclass(frozen=True, slots=True)
class FootprintRevision:
    revision_id: str
    library_id: str
    digest: str
    module_revision_id: str


class ModuleCatalogPort(Protocol):
    def get(self, module_revision_id: str) -> ModuleRevision: ...

    def get_footprint(self, revision_id: str) -> FootprintRevision: ...


def derive_module_uuid(
    project_id: str,
    batch_id: str,
    command_id: str,
    module_revision_digest: str,
    module_local_uuid: str,
) -> str:
    value = "\x1f".join(
        (project_id, batch_id, command_id, module_revision_digest, module_local_uuid)
    )
    return str(uuid5(MODULE_UUID_NAMESPACE, value))


class FileModuleCatalog:
    """Read verified modules from one security-checked catalog root."""

    def __init__(self, root: Path, *, max_files: int, max_bytes: int) -> None:
        if max_files < 1 or max_bytes < 1:
            raise ValueError("module catalog limits must be positive")
        self._root = Path(root)
        self._max_files = max_files
        self._max_bytes = max_bytes
        self._index: dict[str, Path] | None = None

    def get(self, module_revision_id: str) -> ModuleRevision:
        index = self._manifest_index()
        try:
            manifest_path = index[module_revision_id]
        except KeyError as error:
            raise ModuleRevisionNotFoundError(module_revision_id) from error
        return self._load_revision(manifest_path, module_revision_id)

    def get_footprint(self, revision_id: str) -> FootprintRevision:
        matches = [
            FootprintRevision(
                revision_id=revision_id,
                library_id=entry.library_id,
                digest=entry.digest,
                module_revision_id=module.manifest.module_revision_id,
            )
            for module in self._verified_modules()
            if (entry := module.manifest.footprints.get(revision_id)) is not None
        ]
        if len(matches) != 1:
            raise FootprintRevisionNotFoundError(revision_id)
        return matches[0]

    def _verified_modules(self) -> tuple[ModuleRevision, ...]:
        return tuple(
            self._load_revision(path, revision_id)
            for revision_id, path in sorted(self._manifest_index().items())
        )

    def _manifest_index(self) -> dict[str, Path]:
        if self._index is not None:
            return self._index
        root = self._checked_directory(self._root, "module catalog")
        files = self._checked_files(root)
        manifests = [path for path in files if path.name == "module.yaml"]
        index: dict[str, Path] = {}
        for path in manifests:
            if path.parent.parent != root:
                raise ModuleIntegrityError("module manifest must be one directory below catalog")
            manifest = self._read_manifest(path)
            if manifest.module_revision_id in index:
                raise ModuleIntegrityError("duplicate module revision id")
            index[manifest.module_revision_id] = path
        self._index = index
        return index

    def _load_revision(self, manifest_path: Path, requested_id: str) -> ModuleRevision:
        wire_manifest = self._read_manifest(manifest_path)
        if wire_manifest.module_revision_id != requested_id:
            raise ModuleIntegrityError("module revision id mismatch")
        module_dir = self._checked_directory(manifest_path.parent, "module directory")
        template_path = module_dir / wire_manifest.template.path
        if (
            template_path.parent != module_dir
            or template_path.suffix != ".kicad_sch"
            or not template_path.is_file()
        ):
            raise ModuleIntegrityError("invalid template path")
        self._checked_file(template_path)
        try:
            template_bytes = template_path.read_bytes()
        except OSError as error:
            raise ModuleIntegrityError("cannot read module template") from error
        digest = "sha256:" + hashlib.sha256(template_bytes).hexdigest()
        if digest != wire_manifest.template.digest:
            raise ModuleIntegrityError("template digest mismatch")
        template_uuids = _template_uuids(template_bytes)
        if set(wire_manifest.uuid_bindings) != template_uuids:
            raise ModuleIntegrityError("UUID bindings do not match template UUIDs")
        if len(set(wire_manifest.uuid_bindings.values())) != len(wire_manifest.uuid_bindings):
            raise ModuleIntegrityError("UUID binding roles must be unique")
        if _template_port_labels(template_bytes) != dict(wire_manifest.ports):
            raise ModuleIntegrityError("module port labels do not match manifest")
        manifest = ModuleManifest(
            schema_version=wire_manifest.schema_version,
            module_revision_id=wire_manifest.module_revision_id,
            status=wire_manifest.status,
            name=wire_manifest.name,
            kicad_majors=(
                (wire_manifest.kicad_major,)
                if isinstance(wire_manifest, _ManifestModelV1)
                else tuple(wire_manifest.kicad_majors)
            ),
            adapter_contract=wire_manifest.adapter_contract,
            template=wire_manifest.template,
            uuid_bindings=MappingProxyType(dict(wire_manifest.uuid_bindings)),
            parameters=MappingProxyType(dict(wire_manifest.parameters)),
            ports=MappingProxyType(dict(wire_manifest.ports)),
            footprints=MappingProxyType(dict(wire_manifest.footprints)),
        )
        return ModuleRevision(
            manifest=manifest,
            manifest_digest=canonical_digest(wire_manifest.model_dump(mode="json")),
            template_id=wire_manifest.template.path,
            template_bytes=template_bytes,
        )

    def _read_manifest(
        self, path: Path
    ) -> _ManifestModelV1 | _ManifestModelV1_1:
        self._checked_file(path)
        try:
            value = yaml.safe_load(path.read_bytes())
            if not isinstance(value, dict):
                raise ValueError("manifest must be a mapping")
            schema_version = value.get("schema_version")
            model = {
                "1.0": _ManifestModelV1,
                "1.1": _ManifestModelV1_1,
            }.get(schema_version)
            if model is None:
                raise ValueError("unsupported manifest schema version")
            return model.model_validate(value, strict=True)
        except (OSError, yaml.YAMLError, ValidationError, TypeError, ValueError) as error:
            raise ModuleIntegrityError("invalid module manifest") from error

    def _checked_files(self, root: Path) -> tuple[Path, ...]:
        files: list[Path] = []
        total_bytes = 0
        pending = [root]
        while pending:
            directory = pending.pop()
            try:
                entries = sorted(directory.iterdir(), key=lambda item: item.name)
            except OSError as error:
                raise ModuleIntegrityError("cannot enumerate module catalog") from error
            for entry in entries:
                if entry.is_symlink() or _is_reparse_point(entry):
                    raise ModuleIntegrityError("module catalog must not contain links")
                if entry.is_dir():
                    pending.append(entry)
                    continue
                self._checked_file(entry)
                if entry.suffix not in _ALLOWED_SUFFIXES:
                    raise ModuleIntegrityError("module catalog contains unsupported file type")
                files.append(entry)
                total_bytes += entry.stat().st_size
                if len(files) > self._max_files or total_bytes > self._max_bytes:
                    raise ModuleIntegrityError("module catalog exceeds configured limits")
        return tuple(files)

    @staticmethod
    def _checked_directory(path: Path, description: str) -> Path:
        if path.is_symlink() or _is_reparse_point(path) or not path.is_dir():
            raise ModuleIntegrityError(f"{description} must be a real directory")
        try:
            return path.resolve(strict=True)
        except OSError as error:
            raise ModuleIntegrityError(f"cannot resolve {description}") from error

    @staticmethod
    def _checked_file(path: Path) -> None:
        if path.is_symlink() or _is_reparse_point(path) or not path.is_file():
            raise ModuleIntegrityError("module catalog contains an unsafe file")


def _is_reparse_point(path: Path) -> bool:
    try:
        return bool(os.lstat(path).st_file_attributes & _REPARSE_POINT)
    except (AttributeError, OSError):
        return False


def _template_uuids(data: bytes) -> set[str]:
    try:
        document = parse_cst(data)
    except ValueError as error:
        raise ModuleIntegrityError("invalid module template") from error
    values: set[str] = set()
    for node in _lists(document.root):
        if node.head != "uuid" or len(node.items) != 2 or not isinstance(node.items[1], CstAtom):
            continue
        value = node.items[1].value
        try:
            if str(UUID(value)) != value.lower():
                raise ValueError
        except ValueError as error:
            raise ModuleIntegrityError("template UUID is not canonical") from error
        if value in values:
            raise ModuleIntegrityError("template contains duplicate UUID")
        values.add(value)
    if not values:
        raise ModuleIntegrityError("template contains no UUID bindings")
    return values


def _lists(node: CstList):
    yield node
    for item in node.items:
        if isinstance(item, CstList):
            yield from _lists(item)


def _template_port_labels(data: bytes) -> dict[str, str]:
    try:
        document = parse_cst(data)
        labels: dict[str, str] = {}
        for node in _lists(document.root):
            if node.head != "hierarchical_label":
                continue
            name = node.atom_text(1)
            shape = node.find_children("shape")
            if len(shape) != 1 or len(shape[0].items) != 2:
                raise ModuleIntegrityError("module port label shape is invalid")
            if name in labels:
                raise ModuleIntegrityError("module port labels contain duplicates")
            labels[name] = shape[0].atom_text(1)
        return labels
    except (ValueError, IndexError) as error:
        raise ModuleIntegrityError("module port labels are invalid") from error

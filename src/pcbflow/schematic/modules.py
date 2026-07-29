from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid5

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from pcbflow.canonical import canonical_digest


MODULE_UUID_NAMESPACE = UUID("9bcf613a-1ced-5b76-9a90-8fd90ac5f16d")
_ALLOWED_SUFFIXES = frozenset((".yaml", ".kicad_sch"))
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class ModuleIntegrityError(ValueError):
    pass


class ModuleRevisionNotFoundError(LookupError):
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


class ModuleManifest(_StrictManifest):
    schema_version: str = Field(pattern=r"^1\.0$")
    module_revision_id: str = Field(pattern=r"^modrev_[A-Za-z0-9_-]+$")
    status: str = Field(pattern=r"^verified$")
    name: str = Field(min_length=1)
    kicad_major: int
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


@dataclass(frozen=True, slots=True)
class ModuleRevision:
    manifest: ModuleManifest
    manifest_digest: str
    template_path: Path
    template_bytes: bytes


class ModuleCatalogPort(Protocol):
    def get(self, module_revision_id: str) -> ModuleRevision: ...


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
        manifest = self._read_manifest(manifest_path)
        if manifest.module_revision_id != requested_id:
            raise ModuleIntegrityError("module revision id mismatch")
        module_dir = self._checked_directory(manifest_path.parent, "module directory")
        template_path = module_dir / manifest.template.path
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
        if digest != manifest.template.digest:
            raise ModuleIntegrityError("template digest mismatch")
        return ModuleRevision(
            manifest=manifest,
            manifest_digest=canonical_digest(manifest.model_dump(mode="json")),
            template_path=template_path.resolve(strict=True),
            template_bytes=template_bytes,
        )

    def _read_manifest(self, path: Path) -> ModuleManifest:
        self._checked_file(path)
        try:
            value = yaml.safe_load(path.read_bytes())
            return ModuleManifest.model_validate(value, strict=True)
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

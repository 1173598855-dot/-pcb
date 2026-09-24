from __future__ import annotations

import io
import stat
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from pcbflow.artifacts import (
    ArtifactDigestMismatchError,
    ArtifactSizeLimitError,
    ContentAddressedStore,
)
from pcbflow.canonical import canonical_digest, canonical_json_bytes
from pcbflow.workspaces import (
    WorkspaceEntryError,
    WorkspaceLinkError,
    open_regular_file,
)

if TYPE_CHECKING:
    from pcbflow.component_store import ComponentRevisionStore


_MANIFEST_MEDIA_TYPE = "application/vnd.pcbflow.component-manifest+json"


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        allow_inf_nan=False,
    )


class ComponentAsset(StrictModel):
    path: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    media_type: str
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def reject_dot_paths(self) -> Self:
        if self.path in {".", ".."}:
            raise ValueError("component asset path cannot be dot")
        return self


class ComponentManifest(StrictModel):
    schema_version: Literal["1.0"]
    component_key: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    status: Literal["verified"]
    name: str = Field(min_length=1)
    manufacturer: str = Field(min_length=1)
    part_number: str = Field(min_length=1)
    datasheet: ComponentAsset
    pinout: ComponentAsset
    symbol: ComponentAsset
    footprint: ComponentAsset
    model_3d: ComponentAsset | None = None

    @model_validator(mode="after")
    def validate_identity_and_assets(self) -> Self:
        if self.component_key != f"{self.manufacturer}:{self.part_number}":
            raise ValueError("component_key must match manufacturer and part_number")

        assets = {
            "datasheet": self.datasheet,
            "pinout": self.pinout,
            "symbol": self.symbol,
            "footprint": self.footprint,
            "model_3d": self.model_3d,
        }
        expected_media_types = {
            "datasheet": "application/pdf",
            "pinout": "application/json",
            "symbol": "application/vnd.kicad.symbol",
            "footprint": "application/vnd.kicad.footprint",
            "model_3d": "model/step",
        }
        paths: list[str] = []
        for field_name, asset in assets.items():
            if asset is None:
                continue
            if asset.media_type != expected_media_types[field_name]:
                raise ValueError(f"invalid media type for {field_name}")
            paths.append(asset.path)
        if len(paths) != len(set(paths)):
            raise ValueError("component asset paths must be unique")
        return self


@dataclass(frozen=True, slots=True)
class ComponentRevision:
    id: str
    component_key: str
    manufacturer: str
    part_number: str
    name: str
    revision: str
    status: str
    canonical_digest: str
    manifest_artifact_digest: str
    datasheet_artifact_digest: str
    pinout_artifact_digest: str
    symbol_artifact_digest: str
    footprint_artifact_digest: str
    model_3d_artifact_digest: str | None
    idempotency_key: str
    created_at: datetime

    def __post_init__(self) -> None:
        if self.status != "verified":
            raise ValueError("component revision status must be verified")


class ComponentRevisionService:
    def __init__(
        self,
        store: ComponentRevisionStore,
        artifacts: ContentAddressedStore,
        *,
        max_bytes: int,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("component import byte limit must be positive")
        self._store = store
        self._artifacts = artifacts
        self._max_bytes = max_bytes

    def import_revision(
        self, manifest_path: Path, idempotency_key: str
    ) -> ComponentRevision:
        manifest_bytes = _read_regular_file(manifest_path, self._max_bytes)
        manifest = load_component_manifest(manifest_bytes)
        root = _checked_component_directory(manifest_path.parent)
        canonical_digest = component_manifest_digest(manifest)
        canonical_bytes = canonical_json_bytes(manifest.model_dump(mode="json"))
        remaining_bytes = self._max_bytes - len(manifest_bytes)

        with ExitStack() as cleanup:
            stages = [
                self._artifacts.stage_stream(
                    io.BytesIO(canonical_bytes),
                    _MANIFEST_MEDIA_TYPE,
                    expected_digest=canonical_digest,
                )
            ]
            cleanup.callback(stages[0].discard)
            for field_name in (
                "datasheet",
                "pinout",
                "symbol",
                "footprint",
                "model_3d",
            ):
                asset = getattr(manifest, field_name)
                if asset is None:
                    continue
                candidate = _declared_asset_path(root, asset.path)
                try:
                    with _open_regular_file(candidate) as stream:
                        stage = self._artifacts.stage_stream(
                            stream,
                            asset.media_type,
                            expected_digest=asset.digest,
                            max_bytes=remaining_bytes,
                        )
                except ArtifactDigestMismatchError as error:
                    raise ValueError(
                        f"component asset digest mismatch: {asset.path}"
                    ) from error
                except ArtifactSizeLimitError as error:
                    raise ValueError("component import exceeds size limit") from error
                remaining_bytes -= stage.size
                stages.append(stage)
                cleanup.callback(stage.discard)
            descriptors = tuple(stage.publish() for stage in stages)

        return self._store.create(
            manifest=manifest,
            canonical_digest=canonical_digest,
            idempotency_key=idempotency_key,
            artifacts=descriptors,
        )


def _checked_component_directory(path: Path) -> Path:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ValueError("component directory cannot be read") from error
    if _is_link_or_reparse_point(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("component directory must be a non-link directory")
    return path


def _declared_asset_path(root: Path, asset_path: str) -> Path:
    candidate = root / asset_path
    if candidate.parent != root:
        raise ValueError("component asset must be a direct sibling of the manifest")
    return candidate


@contextmanager
def _open_regular_file(path: Path) -> Iterator[BinaryIO]:
    with ExitStack() as stack:
        try:
            stream = stack.enter_context(open_regular_file(path))
        except WorkspaceLinkError as error:
            raise ValueError(
                "component evidence must be a regular non-link file"
            ) from error
        except WorkspaceEntryError as error:
            raise ValueError(
                "component evidence must be a regular non-link file"
            ) from error
        except OSError as error:
            raise ValueError("component evidence file cannot be read") from error
        yield stream


def _is_link_or_reparse_point(metadata: object) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(metadata, "st_file_attributes", 0)
    mode = getattr(metadata, "st_mode", 0)
    return stat.S_ISLNK(mode) or bool(attributes & reparse_flag)


def _read_regular_file(path: Path, max_bytes: int) -> bytes:
    with _open_regular_file(path) as stream:
        data = stream.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError("component import exceeds size limit")
    return data


class _UniqueKeySafeLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueKeySafeLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[object, object]:
    loader.flatten_mapping(node)
    value: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            if key in value:
                raise ValueError(f"duplicate YAML key: {key}")
            value[key] = loader.construct_object(value_node, deep=deep)
        except TypeError as error:
            raise ValueError("unhashable YAML key") from error
    return value


_UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def load_component_manifest(data: bytes) -> ComponentManifest:
    try:
        value = yaml.load(data, Loader=_UniqueKeySafeLoader)
        if not isinstance(value, dict):
            raise ValueError("component manifest must be a mapping")
        return ComponentManifest.model_validate(value, strict=True)
    except (yaml.YAMLError, TypeError, ValueError) as error:
        if isinstance(error, ValueError) and str(error) == "component manifest must be a mapping":
            raise
        raise ValueError("invalid component manifest") from error


def component_manifest_digest(manifest: ComponentManifest) -> str:
    return canonical_digest(manifest.model_dump(mode="json"))

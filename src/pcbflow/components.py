from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from pcbflow.canonical import canonical_digest


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

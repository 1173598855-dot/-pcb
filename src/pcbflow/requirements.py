from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from pcbflow.canonical import canonical_digest, canonical_json_bytes


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        allow_inf_nan=False,
    )


class RequirementKind(StrEnum):
    FUNCTIONAL = "functional"
    INTERFACE = "interface"
    POWER = "power"
    ENVIRONMENT = "environment"
    MECHANICAL = "mechanical"
    MANUFACTURING = "manufacturing"
    COST = "cost"
    COMPLIANCE = "compliance"
    VERIFICATION = "verification"


class RequirementPriority(StrEnum):
    MUST = "must"
    SHOULD = "should"
    COULD = "could"


class RequirementSetStatus(StrEnum):
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    FROZEN = "frozen"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class Requirement(StrictModel):
    id: str = Field(pattern=r"^REQ-[A-Z0-9-]+$")
    kind: RequirementKind
    statement: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    priority: RequirementPriority
    source: str = Field(min_length=1)
    verification_method: str = Field(min_length=1)
    acceptance_criteria: str = Field(min_length=1)


class InterfaceDefinition(StrictModel):
    id: str = Field(pattern=r"^IF-[A-Z0-9-]+$")
    name: str = Field(min_length=1)
    direction: Literal["input", "output", "bidirectional", "passive"]
    nominal_voltage_v: float | None = Field(default=None, ge=0)
    absolute_max_voltage_v: float | None = Field(default=None, ge=0)


class PowerRail(StrictModel):
    id: str = Field(pattern=r"^PWR-[A-Z0-9-]+$")
    source: str = Field(min_length=1)
    minimum_current_a: float = Field(ge=0)
    typical_current_a: float = Field(ge=0)
    maximum_current_a: float = Field(ge=0)

    @model_validator(mode="after")
    def ordered_currents(self) -> Self:
        if not (
            self.minimum_current_a
            <= self.typical_current_a
            <= self.maximum_current_a
        ):
            raise ValueError("power rail currents must be ordered")
        return self


class Assumption(StrictModel):
    id: str = Field(pattern=r"^ASM-[A-Z0-9-]+$")
    statement: str = Field(min_length=1)
    blocking: bool
    owner: str = Field(min_length=1)
    closure_condition: str = Field(min_length=1)


class VerificationItem(StrictModel):
    id: str = Field(pattern=r"^VER-[A-Z0-9-]+$")
    requirement_ids: tuple[str, ...] = Field(min_length=1)
    method: str = Field(min_length=1)
    acceptance_criteria: str = Field(min_length=1)


class RequirementSetPayload(StrictModel):
    schema_version: Literal["1.0"]
    requirements: tuple[Requirement, ...] = Field(min_length=1)
    interfaces: tuple[InterfaceDefinition, ...]
    power_rails: tuple[PowerRail, ...]
    assumptions: tuple[Assumption, ...]
    verification_items: tuple[VerificationItem, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def valid_identifiers_and_references(self) -> Self:
        identifiers = [item.id for item in self.requirements]
        identifiers.extend(item.id for item in self.interfaces)
        identifiers.extend(item.id for item in self.power_rails)
        identifiers.extend(item.id for item in self.assumptions)
        identifiers.extend(item.id for item in self.verification_items)
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("requirement payload identifiers must be unique")

        requirement_ids = {item.id for item in self.requirements}
        for item in self.verification_items:
            if len(item.requirement_ids) != len(set(item.requirement_ids)):
                raise ValueError("verification requirement references must be unique")
            missing = set(item.requirement_ids) - requirement_ids
            if missing:
                raise ValueError("verification item references unknown requirement")
        return self


@dataclass(frozen=True, slots=True)
class RequirementSet:
    id: str
    project_id: str
    base_revision: str
    schema_version: str
    status: RequirementSetStatus
    payload: RequirementSetPayload
    canonical_digest: str
    canonical_artifact_digest: str
    idempotency_key: str
    submission_idempotency_key: str | None
    candidate_revision: str | None
    candidate_snapshot_digest: str | None
    frozen_revision: str | None
    created_at: datetime
    submitted_at: datetime | None
    frozen_at: datetime | None

    def subject_digest(self) -> str:
        if self.candidate_revision is None or self.candidate_snapshot_digest is None:
            raise ValueError("requirement set has no G1 candidate")
        return g1_subject_digest(
            self.project_id,
            self.id,
            self.canonical_digest,
            self.base_revision,
            self.candidate_revision,
            self.candidate_snapshot_digest,
        )


_RENDERED_SECTIONS = {
    Path("requirements/product.yaml"): frozenset(
        {"schema_version", "requirements"}
    ),
    Path("requirements/interfaces.yaml"): frozenset({"interfaces"}),
    Path("requirements/power-tree.yaml"): frozenset({"power_rails"}),
    Path("requirements/assumptions.yaml"): frozenset({"assumptions"}),
    Path("requirements/verification.yaml"): frozenset({"verification_items"}),
}


def load_requirement_payload(data: bytes) -> RequirementSetPayload:
    value = yaml.safe_load(data)
    return RequirementSetPayload.model_validate_json(
        canonical_json_bytes(value), strict=True
    )


def _yaml_bytes(value: object) -> bytes:
    return yaml.safe_dump(
        value,
        sort_keys=True,
        allow_unicode=True,
        default_flow_style=False,
        line_break="\n",
    ).encode("utf-8")


def render_requirement_files(
    payload: RequirementSetPayload,
) -> dict[Path, bytes]:
    value = payload.model_dump(mode="json")
    return {
        path: _yaml_bytes({section: value[section] for section in sections})
        for path, sections in _RENDERED_SECTIONS.items()
    }


def load_rendered_requirement_files(
    files: Mapping[Path, bytes],
) -> RequirementSetPayload:
    if set(files) != set(_RENDERED_SECTIONS):
        raise ValueError("invalid requirement files")

    combined: dict[str, object] = {}
    for path, expected_sections in _RENDERED_SECTIONS.items():
        value = yaml.safe_load(files[path])
        if not isinstance(value, dict) or set(value) != set(expected_sections):
            raise ValueError(f"invalid requirement file sections: {path}")
        duplicate_sections = set(value) & set(combined)
        if duplicate_sections:
            raise ValueError("duplicate requirement file sections")
        combined.update(value)

    return RequirementSetPayload.model_validate_json(
        canonical_json_bytes(combined), strict=True
    )


def requirement_digest(payload: RequirementSetPayload) -> str:
    return canonical_digest(payload.model_dump(mode="json"))


def g1_subject_digest(
    project_id: str,
    requirement_set_id: str,
    requirements_digest: str,
    base_revision: str,
    candidate_revision: str,
    candidate_snapshot_digest: str,
) -> str:
    return canonical_digest(
        {
            "schema_version": "1.0",
            "project_id": project_id,
            "requirement_set_id": requirement_set_id,
            "requirements_digest": requirements_digest,
            "base_revision": base_revision,
            "candidate_revision": candidate_revision,
            "candidate_snapshot_digest": candidate_snapshot_digest,
        }
    )

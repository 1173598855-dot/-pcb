"""Request bodies accepted by the PCBFlow HTTP API.

These models are deliberately free of FastAPI routing concerns so that the
route layer in :mod:`pcbflow.api` can stay focused on orchestration. They
are re-exported from :mod:`pcbflow.api` for backwards compatibility.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pcbflow.eda import (
    ProjectEdaAuthorityInput,
    validate_authority_input,
)
from pcbflow.domain import EdaKind
from pcbflow.pcb_candidates import validate_candidate_public_inputs


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CreateProjectRequest(StrictRequest):
    name: str = Field(min_length=1)
    source_path: str = Field(min_length=1)
    eda_kind: Literal["kicad", "lceda_pro"] | None = None
    eda_profile_id: str | None = Field(default=None, min_length=1, max_length=128)
    board_profile_id: str | None = Field(
        default=None, min_length=1, max_length=128
    )
    rulepack_digest: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )

    @model_validator(mode="after")
    def require_complete_authority_tuple(self):
        values = (
            self.eda_kind,
            self.eda_profile_id,
            self.board_profile_id,
            self.rulepack_digest,
        )
        if any(value is not None for value in values) and any(
            value is None for value in values
        ):
            raise ValueError("EDA authority tuple must be complete")
        return self

    def authority_input(self) -> ProjectEdaAuthorityInput | None:
        if self.eda_kind is None:
            return None
        assert self.eda_profile_id is not None
        assert self.board_profile_id is not None
        assert self.rulepack_digest is not None
        return validate_authority_input(ProjectEdaAuthorityInput(
            eda_kind=EdaKind(self.eda_kind),
            eda_profile_id=self.eda_profile_id,
            board_profile_id=self.board_profile_id,
            rulepack_digest=self.rulepack_digest,
        ))


class ConfigureEdaAuthorityRequest(StrictRequest):
    eda_kind: Literal["kicad", "lceda_pro"]
    eda_profile_id: str = Field(min_length=1, max_length=128)
    board_profile_id: str = Field(min_length=1, max_length=128)
    rulepack_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    def authority_input(self) -> ProjectEdaAuthorityInput:
        return validate_authority_input(ProjectEdaAuthorityInput(
            eda_kind=EdaKind(self.eda_kind),
            eda_profile_id=self.eda_profile_id,
            board_profile_id=self.board_profile_id,
            rulepack_digest=self.rulepack_digest,
        ))


class CreateComponentRevisionRequest(StrictRequest):
    manifest_path: str = Field(min_length=1)


class CreateComponentModuleBindingRequest(StrictRequest):
    kicad_major: int = Field(ge=1)
    module_revision_id: str = Field(min_length=1)


class ActorRequest(StrictRequest):
    type: Literal["human", "service"]
    id: str = Field(min_length=1, max_length=255)


class ApprovalRequest(StrictRequest):
    subject_type: Literal["requirement_set"]
    subject_id: str = Field(min_length=1)
    subject_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    decision: Literal["approve", "reject"]
    actor: ActorRequest
    comment: str = Field(min_length=1)


class AcceptProposalRequest(StrictRequest):
    candidate_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    actor: ActorRequest
    comment: str = Field(min_length=1)


class RejectProposalRequest(StrictRequest):
    actor: ActorRequest
    reason: str = Field(min_length=1)


class CancelTaskRequest(StrictRequest):
    reason: str = Field(min_length=1, max_length=1000)

    @field_validator("reason")
    @classmethod
    def require_nonblank_reason(cls, reason: str) -> str:
        normalized = reason.strip()
        if not normalized:
            raise ValueError("reason must not be blank")
        return normalized


class CreatePcbCandidateRequest(StrictRequest):
    seed: int = Field(default=0, ge=0)
    net_ids: list[str] = Field(default_factory=list, max_length=256)
    board_snapshot_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    capability_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")

    @field_validator("net_ids")
    @classmethod
    def validate_net_ids(cls, values: list[str]) -> list[str]:
        if any(not value or value != value.strip() for value in values):
            raise ValueError("net_ids must contain nonblank trimmed strings")
        if len(values) != len(set(values)):
            raise ValueError("net_ids must be unique")
        return values

    @model_validator(mode="after")
    def validate_shared_candidate_inputs(self) -> "CreatePcbCandidateRequest":
        validate_candidate_public_inputs(
            seed=self.seed,
            net_ids=self.net_ids,
            board_snapshot_digest=self.board_snapshot_digest,
            capability_digest=self.capability_digest,
        )
        return self


class DecidePcbG3Request(StrictRequest):
    candidate_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    decision: Literal["approve", "reject"]
    actor: ActorRequest
    comment: str = Field(min_length=1)


class ExportPcbReleaseRequest(StrictRequest):
    pass


class DecidePcbG4Request(StrictRequest):
    manifest_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    decision: Literal["approve", "reject"]
    actor: ActorRequest
    comment: str = Field(min_length=1)


__all__ = [
    "AcceptProposalRequest",
    "ActorRequest",
    "ApprovalRequest",
    "CancelTaskRequest",
    "ConfigureEdaAuthorityRequest",
    "CreateComponentModuleBindingRequest",
    "CreateComponentRevisionRequest",
    "CreatePcbCandidateRequest",
    "CreateProjectRequest",
    "DecidePcbG3Request",
    "DecidePcbG4Request",
    "ExportPcbReleaseRequest",
    "RejectProposalRequest",
    "StrictRequest",
]

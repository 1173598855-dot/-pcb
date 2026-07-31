from __future__ import annotations

import json
from enum import StrEnum
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from pcbflow.canonical import canonical_digest, canonical_json_bytes


class StrictCommandModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Actor(StrictCommandModel):
    type: Literal["human", "service"]
    id: str = Field(min_length=1, max_length=255)


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ValidationKind(StrEnum):
    SCHEMA = "schema"
    PRECONDITIONS = "preconditions"
    PATH_LIMITS = "path_limits"
    POST_WRITE_PARSE = "post_write_parse"
    SEMANTIC_DIFF = "semantic_diff"
    KICAD_ERC = "kicad_erc"


class SchematicObjectRef(StrictCommandModel):
    kind: Literal[
        "sheet",
        "symbol",
        "pin",
        "hierarchical_port",
        "label",
        "net",
        "wire_endpoint",
    ]
    sheet_uuid: str = Field(min_length=1)
    object_uuid: str = Field(min_length=1)
    pin_number: str | None


class ProjectRevisionEquals(StrictCommandModel):
    type: Literal["project.revision_equals"]
    revision: str = Field(pattern=r"^git:[0-9a-f]{40,64}$")


class RequirementsDigestEquals(StrictCommandModel):
    type: Literal["requirements.digest_equals"]
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class SchematicObjectExists(StrictCommandModel):
    type: Literal["schematic.object_exists"]
    subject_ref: SchematicObjectRef


class SchematicPropertyEquals(StrictCommandModel):
    type: Literal["schematic.property_equals"]
    subject_ref: SchematicObjectRef
    property_name: str = Field(min_length=1)
    value: str


class SchematicModuleAbsent(StrictCommandModel):
    type: Literal["schematic.module_absent"]
    instance_name: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,63}$")


class ToolCapabilityAvailable(StrictCommandModel):
    type: Literal["tool.capability_available"]
    capability: str = Field(min_length=1)


Precondition = Annotated[
    ProjectRevisionEquals
    | RequirementsDigestEquals
    | SchematicObjectExists
    | SchematicPropertyEquals
    | SchematicModuleAbsent
    | ToolCapabilityAvailable,
    Field(discriminator="type"),
]


class InstantiateModulePayload(StrictCommandModel):
    module_revision_id: str = Field(pattern=r"^modrev_[A-Za-z0-9_-]+$")
    instance_name: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,63}$")
    target_sheet_ref: SchematicObjectRef
    parameter_bindings: dict[str, str]
    port_bindings: dict[str, SchematicObjectRef]
    placement_slot: str = Field(min_length=1)


class SetPropertyPayload(StrictCommandModel):
    subject_ref: SchematicObjectRef
    property_name: str = Field(min_length=1)
    value: str
    expected_old_value: str | None


class AssignFootprintPayload(StrictCommandModel):
    subject_ref: SchematicObjectRef
    footprint_revision_id: str = Field(pattern=r"^fprev_[A-Za-z0-9_-]+$")


class AddLabelPayload(StrictCommandModel):
    target_ref: SchematicObjectRef
    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_./+-]{0,126}$")
    scope: Literal["local", "global", "hierarchical"]


class InstantiateModuleOperation(StrictCommandModel):
    type: Literal["schematic.instantiate_module"]
    payload: InstantiateModulePayload


class SetPropertyOperation(StrictCommandModel):
    type: Literal["schematic.set_property"]
    payload: SetPropertyPayload


class AssignFootprintOperation(StrictCommandModel):
    type: Literal["schematic.assign_footprint"]
    payload: AssignFootprintPayload


class AddLabelOperation(StrictCommandModel):
    type: Literal["schematic.add_label"]
    payload: AddLabelPayload


DesignOperation = Annotated[
    InstantiateModuleOperation
    | SetPropertyOperation
    | AssignFootprintOperation
    | AddLabelOperation,
    Field(discriminator="type"),
]


class Provenance(StrictCommandModel):
    requirement_ids: tuple[str, ...] = Field(min_length=1)
    evidence_ids: tuple[str, ...]
    module_revision_ids: tuple[str, ...]


class DesignCommand(StrictCommandModel):
    schema_version: Literal["1.0"]
    command_id: str = Field(pattern=r"^cmd_[A-Za-z0-9_-]+$")
    batch_id: str = Field(pattern=r"^bat_[A-Za-z0-9_-]+$")
    project_id: str = Field(pattern=r"^prj_[A-Za-z0-9_-]+$")
    base_revision: str = Field(pattern=r"^git:[0-9a-f]{40,64}$")
    idempotency_key: str = Field(min_length=1, max_length=255)
    actor: Actor
    intent: str = Field(min_length=1)
    risk: RiskLevel
    preconditions: tuple[Precondition, ...]
    operation: DesignOperation
    required_validations: tuple[ValidationKind, ...]
    provenance: Provenance

    @model_validator(mode="after")
    def unique_validations(self) -> DesignCommand:
        if len(set(self.required_validations)) != len(self.required_validations):
            raise ValueError("required_validations must be unique")
        return self


class CommandBatch(StrictCommandModel):
    schema_version: Literal["1.0"]
    batch_id: str = Field(pattern=r"^bat_[A-Za-z0-9_-]+$")
    project_id: str = Field(pattern=r"^prj_[A-Za-z0-9_-]+$")
    base_revision: str = Field(pattern=r"^git:[0-9a-f]{40,64}$")
    requirement_set_id: str = Field(pattern=r"^reqset_[A-Za-z0-9_-]+$")
    idempotency_key: str = Field(min_length=1, max_length=255)
    actor: Actor
    intent: str = Field(min_length=1)
    risk: RiskLevel
    commands: tuple[DesignCommand, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def command_envelope_matches(self) -> CommandBatch:
        for command in self.commands:
            for field in ("batch_id", "project_id", "base_revision", "actor"):
                if getattr(command, field) != getattr(self, field):
                    raise ValueError(f"command {field} does not match batch")
        command_ids = [command.command_id for command in self.commands]
        keys = [command.idempotency_key for command in self.commands]
        if len(set(command_ids)) != len(command_ids):
            raise ValueError("command_id must be unique inside a batch")
        if len(set(keys)) != len(keys):
            raise ValueError("command idempotency_key must be unique")
        return self


class DesignCommandSchemaError(ValueError):
    pass


def load_command_batch(data: bytes) -> CommandBatch:
    try:
        value = json.loads(
            data.decode("utf-8"),
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON number: {value}")
            ),
        )
        return CommandBatch.model_validate_json(
            canonical_json_bytes(value), strict=True
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValidationError,
        ValueError,
    ) as error:
        raise DesignCommandSchemaError(str(error)) from error


def command_batch_digest(batch: CommandBatch) -> str:
    return canonical_digest(batch.model_dump(mode="json"))


class PreconditionState(StrEnum):
    TRUE = "true"
    FALSE = "false"
    UNKNOWN = "unknown"


class PreconditionResult(StrictCommandModel):
    type: str
    state: PreconditionState
    subject_ref: SchematicObjectRef | None
    actual: object | None


class PreconditionContext(Protocol):
    current_revision: str
    requirements_digest: str

    def has_object(self, subject_ref: SchematicObjectRef) -> bool | None: ...

    def property_value(
        self, subject_ref: SchematicObjectRef, name: str
    ) -> str | None: ...

    def has_module(self, instance_name: str) -> bool | None: ...

    def has_capability(self, capability: str) -> bool | None: ...


def _state(value: bool | None) -> PreconditionState:
    if value is None:
        return PreconditionState.UNKNOWN
    return PreconditionState.TRUE if value else PreconditionState.FALSE


def evaluate_precondition(
    precondition: Precondition, context: PreconditionContext
) -> PreconditionResult:
    subject_ref = getattr(precondition, "subject_ref", None)
    match precondition:
        case ProjectRevisionEquals(revision=revision):
            actual: object | None = context.current_revision
            state = _state(actual == revision)
        case RequirementsDigestEquals(digest=digest):
            actual = context.requirements_digest
            state = _state(actual == digest)
        case SchematicObjectExists(subject_ref=reference):
            actual = context.has_object(reference)
            state = _state(actual)
        case SchematicPropertyEquals(
            subject_ref=reference, property_name=name, value=expected
        ):
            actual = context.property_value(reference, name)
            state = (
                PreconditionState.UNKNOWN
                if actual is None
                else _state(actual == expected)
            )
        case SchematicModuleAbsent(instance_name=instance_name):
            actual = context.has_module(instance_name)
            state = (
                PreconditionState.UNKNOWN
                if actual is None
                else _state(not actual)
            )
        case ToolCapabilityAvailable(capability=capability):
            actual = context.has_capability(capability)
            state = _state(actual)
        case _:
            raise AssertionError("unreachable precondition variant")
    return PreconditionResult(
        type=precondition.type,
        state=state,
        subject_ref=subject_ref,
        actual=actual,
    )

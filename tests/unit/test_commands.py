from __future__ import annotations

import json

import pytest

from pcbflow.commands import (
    DesignCommandSchemaError,
    PreconditionState,
    ProjectRevisionEquals,
    RequirementsDigestEquals,
    SchematicModuleAbsent,
    SchematicObjectExists,
    SchematicObjectRef,
    SchematicPropertyEquals,
    ToolCapabilityAvailable,
    command_batch_digest,
    evaluate_precondition,
    load_command_batch,
)


def _batch() -> dict[str, object]:
    actor = {"type": "human", "id": "local-user"}
    command = {
        "schema_version": "1.0",
        "command_id": "cmd_status_led",
        "batch_id": "bat_status_led",
        "project_id": "prj_controller",
        "base_revision": "git:" + "1" * 40,
        "idempotency_key": "controller:r1:status-led:1",
        "actor": actor,
        "intent": "Instantiate the verified status LED module",
        "risk": "medium",
        "preconditions": [
            {
                "type": "project.revision_equals",
                "revision": "git:" + "1" * 40,
            },
            {
                "type": "tool.capability_available",
                "capability": "kicad.cst.write.v1",
            },
        ],
        "operation": {
            "type": "schematic.instantiate_module",
            "payload": {
                "module_revision_id": "modrev_status_led_v1",
                "instance_name": "STATUS_LED",
                "target_sheet_ref": {
                    "kind": "sheet",
                    "sheet_uuid": "00000000-0000-0000-0000-000000000001",
                    "object_uuid": "00000000-0000-0000-0000-000000000001",
                    "pin_number": None,
                },
                "parameter_bindings": {"LED_VALUE": "GREEN"},
                "port_bindings": {},
                "placement_slot": "auto",
            },
        },
        "required_validations": ["semantic_diff", "kicad_erc"],
        "provenance": {
            "requirement_ids": ["REQ-FUNC-001"],
            "evidence_ids": [],
            "module_revision_ids": ["modrev_status_led_v1"],
        },
    }
    return {
        "schema_version": "1.0",
        "batch_id": "bat_status_led",
        "project_id": "prj_controller",
        "base_revision": "git:" + "1" * 40,
        "requirement_set_id": "reqset_controller_v1",
        "idempotency_key": "controller:r1:status-led",
        "actor": actor,
        "intent": "Add a verified status LED",
        "risk": "medium",
        "commands": [command],
    }


def _load(value: dict[str, object]):
    return load_command_batch(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    )


def test_batch_is_strict_and_rejects_unknown_fields() -> None:
    value = _batch()
    value["unknown"] = True
    with pytest.raises(DesignCommandSchemaError):
        _load(value)


@pytest.mark.parametrize(
    ("field", "mismatched_value"),
    [
        ("batch_id", "bat_other"),
        ("project_id", "prj_other"),
        ("base_revision", "git:" + "2" * 40),
        ("actor", {"type": "human", "id": "other-user"}),
    ],
)
def test_batch_rejects_each_inner_envelope_mismatch(
    field: str, mismatched_value: object
) -> None:
    value = _batch()
    value["commands"][0][field] = mismatched_value  # type: ignore[index]
    with pytest.raises(DesignCommandSchemaError, match=field):
        _load(value)


def test_batch_rejects_duplicate_command_id() -> None:
    value = _batch()
    duplicate = json.loads(json.dumps(value["commands"][0]))  # type: ignore[index]
    duplicate["idempotency_key"] = "controller:r1:status-led:2"
    value["commands"].append(duplicate)  # type: ignore[index]
    with pytest.raises(DesignCommandSchemaError, match="command_id"):
        _load(value)


def test_batch_rejects_duplicate_command_idempotency_key() -> None:
    value = _batch()
    duplicate = json.loads(json.dumps(value["commands"][0]))  # type: ignore[index]
    duplicate["command_id"] = "cmd_status_led_second"
    value["commands"].append(duplicate)  # type: ignore[index]
    with pytest.raises(DesignCommandSchemaError, match="idempotency_key"):
        _load(value)


def test_command_rejects_duplicate_required_validations() -> None:
    value = _batch()
    value["commands"][0]["required_validations"] = [  # type: ignore[index]
        "semantic_diff",
        "semantic_diff",
    ]
    with pytest.raises(DesignCommandSchemaError, match="required_validations"):
        _load(value)


@pytest.mark.parametrize("non_finite", [b"NaN", b"1e999"])
def test_batch_rejects_non_finite_json(non_finite: bytes) -> None:
    with pytest.raises(DesignCommandSchemaError):
        load_command_batch(non_finite)


def test_digest_is_stable_when_json_object_key_order_changes() -> None:
    value = _batch()
    reordered = json.loads(json.dumps(value, sort_keys=True))
    assert command_batch_digest(_load(value)) == command_batch_digest(_load(reordered))


def test_command_order_changes_the_batch_digest() -> None:
    left = _batch()
    right = _batch()
    second = dict(right["commands"][0])  # type: ignore[index]
    second["command_id"] = "cmd_status_led_property"
    second["idempotency_key"] = "controller:r1:status-led:2"
    second["operation"] = {
        "type": "schematic.set_property",
        "payload": {
            "subject_ref": {
                "kind": "symbol",
                "sheet_uuid": "00000000-0000-0000-0000-000000000001",
                "object_uuid": "00000000-0000-0000-0000-000000000002",
                "pin_number": None,
            },
            "property_name": "Value",
            "value": "GREEN",
            "expected_old_value": "LED",
        },
    }
    right["commands"] = [second, right["commands"][0]]  # type: ignore[index]
    left["commands"] = [left["commands"][0], second]  # type: ignore[index]
    assert command_batch_digest(_load(left)) != command_batch_digest(_load(right))


class _Context:
    def __init__(
        self,
        *,
        current_revision: str = "git:" + "1" * 40,
        requirements_digest: str = "sha256:" + "a" * 64,
        object_exists: bool | None = None,
        property_value: str | None = None,
        module_exists: bool | None = None,
        capability_available: bool | None = None,
    ) -> None:
        self.current_revision = current_revision
        self.requirements_digest = requirements_digest
        self.object_exists = object_exists
        self._property_value = property_value
        self.module_exists = module_exists
        self.capability_available = capability_available

    def has_object(self, _subject_ref):
        return self.object_exists

    def property_value(self, _subject_ref, _name):
        return self._property_value

    def has_module(self, _instance_name):
        return self.module_exists

    def has_capability(self, _capability: str):
        return self.capability_available


def _subject_ref() -> SchematicObjectRef:
    return SchematicObjectRef(
        kind="symbol",
        sheet_uuid="00000000-0000-0000-0000-000000000001",
        object_uuid="00000000-0000-0000-0000-000000000002",
        pin_number=None,
    )


def _preconditions():
    reference = _subject_ref()
    return (
        ProjectRevisionEquals(
            type="project.revision_equals", revision="git:" + "1" * 40
        ),
        RequirementsDigestEquals(
            type="requirements.digest_equals", digest="sha256:" + "a" * 64
        ),
        SchematicObjectExists(
            type="schematic.object_exists", subject_ref=reference
        ),
        SchematicPropertyEquals(
            type="schematic.property_equals",
            subject_ref=reference,
            property_name="Value",
            value="GREEN",
        ),
        SchematicModuleAbsent(
            type="schematic.module_absent", instance_name="STATUS_LED"
        ),
        ToolCapabilityAvailable(
            type="tool.capability_available", capability="kicad.cst.write.v1"
        ),
    )


def test_all_preconditions_report_true_and_false() -> None:
    preconditions = _preconditions()
    true_context = _Context(
        object_exists=True,
        property_value="GREEN",
        module_exists=False,
        capability_available=True,
    )
    false_context = _Context(
        current_revision="git:" + "2" * 40,
        requirements_digest="sha256:" + "b" * 64,
        object_exists=False,
        property_value="RED",
        module_exists=True,
        capability_available=False,
    )
    for precondition in preconditions:
        assert (
            evaluate_precondition(precondition, true_context).state
            is PreconditionState.TRUE
        )
        assert (
            evaluate_precondition(precondition, false_context).state
            is PreconditionState.FALSE
        )


def test_lookup_preconditions_report_unknown_for_none() -> None:
    _, _, object_exists, property_equals, module_absent, capability_available = (
        _preconditions()
    )
    context = _Context()
    for precondition in (
        object_exists,
        property_equals,
        module_absent,
        capability_available,
    ):
        assert (
            evaluate_precondition(precondition, context).state
            is PreconditionState.UNKNOWN
        )

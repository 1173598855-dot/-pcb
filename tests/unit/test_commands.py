from __future__ import annotations

import json

import pytest

from pcbflow.commands import (
    DesignCommandSchemaError,
    PreconditionState,
    ProjectRevisionEquals,
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


def test_batch_rejects_inner_identity_or_actor_mismatch() -> None:
    value = _batch()
    value["commands"][0]["project_id"] = "prj_other"  # type: ignore[index]
    with pytest.raises(DesignCommandSchemaError, match="project_id"):
        _load(value)


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
    current_revision = "git:" + "1" * 40
    requirements_digest = "sha256:" + "a" * 64

    def has_object(self, _subject_ref):
        return None

    def property_value(self, _subject_ref, _name):
        return None

    def has_module(self, _instance_name):
        return None

    def has_capability(self, capability: str):
        return capability == "kicad.cst.write.v1"


def test_preconditions_distinguish_true_false_and_unknown() -> None:
    context = _Context()
    revision = ProjectRevisionEquals(
        type="project.revision_equals", revision=context.current_revision
    )
    capability = ToolCapabilityAvailable(
        type="tool.capability_available",
        capability="kicad.cst.write.v1",
    )
    missing = ToolCapabilityAvailable(
        type="tool.capability_available", capability="kicad.ipc.write.v1"
    )
    assert evaluate_precondition(revision, context).state is PreconditionState.TRUE
    assert evaluate_precondition(capability, context).state is PreconditionState.TRUE
    assert evaluate_precondition(missing, context).state is PreconditionState.FALSE

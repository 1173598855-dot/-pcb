from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from pcbflow.commands import load_command_batch
from pcbflow.schematic.adapter import (
    CstSchematicAdapter,
    LabelTargetError,
    PropertyWriteNotAllowedError,
)
from pcbflow.schematic.modules import (
    FileModuleCatalog,
    FootprintRevisionNotFoundError,
)


def _fixtures() -> Path:
    return Path(__file__).resolve().parents[1] / "fixtures"


def _command(operation: dict[str, object], command_id: str):
    actor = {"type": "human", "id": "local-user"}
    value = {
        "schema_version": "1.0",
        "batch_id": "bat_adapter_ops",
        "project_id": "prj_controller",
        "base_revision": "git:" + "1" * 40,
        "requirement_set_id": "reqset_controller",
        "idempotency_key": "adapter-ops",
        "actor": actor,
        "intent": "Apply one controlled operation",
        "risk": "low",
        "commands": [
            {
                "schema_version": "1.0",
                "command_id": command_id,
                "batch_id": "bat_adapter_ops",
                "project_id": "prj_controller",
                "base_revision": "git:" + "1" * 40,
                "idempotency_key": f"adapter-ops:{command_id}",
                "actor": actor,
                "intent": "Apply one controlled operation",
                "risk": "low",
                "preconditions": [],
                "operation": operation,
                "required_validations": ["semantic_diff", "kicad_erc"],
                "provenance": {
                    "requirement_ids": ["REQ-FUNC-001"],
                    "evidence_ids": [],
                    "module_revision_ids": [],
                },
            }
        ],
    }
    return load_command_batch(json.dumps(value).encode()).commands[0]


def _symbol_ref() -> dict[str, object]:
    return {
        "kind": "symbol",
        "sheet_uuid": "00000000-0000-0000-0000-000000000001",
        "object_uuid": "00000000-0000-0000-0000-000000000002",
        "pin_number": None,
    }


def _adapter() -> CstSchematicAdapter:
    catalog = FileModuleCatalog(
        _fixtures() / "modules", max_files=32, max_bytes=2_000_000
    )
    return CstSchematicAdapter(catalog)


def test_property_footprint_and_label_operations_reparse_semantically(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    shutil.copytree(_fixtures() / "kicad" / "controlled-design", project)
    commands = (
        _command(
            {
                "type": "schematic.set_property",
                "payload": {
                    "subject_ref": _symbol_ref(),
                    "property_name": "Value",
                    "value": "GREEN",
                    "expected_old_value": "\u72b6\u6001LED",
                },
            },
            "cmd_property",
        ),
        _command(
            {
                "type": "schematic.assign_footprint",
                "payload": {
                    "subject_ref": _symbol_ref(),
                    "footprint_revision_id": "fprev_led_0603_v1",
                },
            },
            "cmd_footprint",
        ),
        _command(
            {
                "type": "schematic.add_label",
                "payload": {
                    "target_ref": {
                        "kind": "pin",
                        "sheet_uuid": "00000000-0000-0000-0000-000000000001",
                        "object_uuid": "00000000-0000-0000-0000-000000000002",
                        "pin_number": "1",
                    },
                    "name": "STATUS_LED_K",
                    "scope": "local",
                },
            },
            "cmd_label",
        ),
    )

    result = _adapter().apply(project, commands)

    symbol = result.after.symbols[0]
    assert symbol.value == "GREEN"
    assert symbol.footprint == "LED_SMD:LED_0603_1608Metric"
    assert any(label.name == "STATUS_LED_K" for label in result.after.labels)
    assert [item.command_id for item in result.command_results] == [
        "cmd_property",
        "cmd_footprint",
        "cmd_label",
    ]


@pytest.mark.parametrize("name", ["uuid", "ki_locked", "Footprint"])
def test_set_property_rejects_internal_or_special_fields(
    tmp_path: Path, name: str
) -> None:
    project = tmp_path / "project"
    shutil.copytree(_fixtures() / "kicad" / "controlled-design", project)
    command = _command(
        {
            "type": "schematic.set_property",
            "payload": {
                "subject_ref": _symbol_ref(),
                "property_name": name,
                "value": "forbidden",
                "expected_old_value": None,
            },
        },
        "cmd_forbidden_property",
    )
    with pytest.raises(PropertyWriteNotAllowedError):
        _adapter().apply(project, (command,))


def test_assign_footprint_rejects_unknown_revision(tmp_path: Path) -> None:
    project = tmp_path / "project"
    shutil.copytree(_fixtures() / "kicad" / "controlled-design", project)
    command = _command(
        {
            "type": "schematic.assign_footprint",
            "payload": {
                "subject_ref": _symbol_ref(),
                "footprint_revision_id": "fprev_unknown",
            },
        },
        "cmd_unknown_footprint",
    )
    with pytest.raises(FootprintRevisionNotFoundError):
        _adapter().apply(project, (command,))


def test_add_label_rejects_symbol_or_coordinate_like_target(tmp_path: Path) -> None:
    project = tmp_path / "project"
    shutil.copytree(_fixtures() / "kicad" / "controlled-design", project)
    command = _command(
        {
            "type": "schematic.add_label",
            "payload": {
                "target_ref": _symbol_ref(),
                "name": "INVALID_TARGET",
                "scope": "local",
            },
        },
        "cmd_invalid_label",
    )
    with pytest.raises(LabelTargetError):
        _adapter().apply(project, (command,))

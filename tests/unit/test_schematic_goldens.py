from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from pcbflow.commands import RiskLevel, load_command_batch
from pcbflow.schematic.adapter import CstSchematicAdapter
from pcbflow.schematic.cst import apply_edits, parse_cst
from pcbflow.schematic.diff import CommandAttribution, build_semantic_diff
from pcbflow.schematic.modules import FileModuleCatalog

GOLDENS = (
    ("blank", "blank.kicad_sch"),
    ("simple", "simple.kicad_sch"),
    ("hierarchical", "root.kicad_sch"),
    ("unicode", "unicode.kicad_sch"),
    ("multi-unit", "multi-unit.kicad_sch"),
    ("custom-properties", "custom-properties.kicad_sch"),
    ("erc-error", "erc-error.kicad_sch"),
)


def _set_value_command(project_id: str, subject_ref, old: str):
    actor = {"type": "human", "id": "golden-test"}
    value = {
        "schema_version": "1.0",
        "batch_id": "bat_golden_edit",
        "project_id": project_id,
        "base_revision": "git:" + "1" * 40,
        "requirement_set_id": "reqset_golden",
        "idempotency_key": "golden-edit",
        "actor": actor,
        "intent": "Edit one golden symbol value",
        "risk": "low",
        "commands": [
            {
                "schema_version": "1.0",
                "command_id": "cmd_golden_edit",
                "batch_id": "bat_golden_edit",
                "project_id": project_id,
                "base_revision": "git:" + "1" * 40,
                "idempotency_key": "golden-edit:1",
                "actor": actor,
                "intent": "Edit one golden symbol value",
                "risk": "low",
                "preconditions": [],
                "operation": {
                    "type": "schematic.set_property",
                    "payload": {
                        "subject_ref": subject_ref.model_dump(mode="json"),
                        "property_name": "Value",
                        "value": old + "-EDITED",
                        "expected_old_value": old,
                    },
                },
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


@pytest.mark.parametrize(("directory", "root_name"), GOLDENS)
def test_golden_roundtrip_inspect_and_controlled_edit(
    tmp_path: Path, directory: str, root_name: str
) -> None:
    fixtures = Path(__file__).resolve().parents[1] / "fixtures"
    source = fixtures / "kicad" / "golden" / directory
    project = tmp_path / "Windows path with spaces" / directory
    shutil.copytree(source, project)

    for schematic in sorted(project.rglob("*.kicad_sch")):
        data = schematic.read_bytes()
        assert apply_edits(parse_cst(data), ()) == data

    adapter = CstSchematicAdapter(
        FileModuleCatalog(fixtures / "modules", max_files=32, max_bytes=2_000_000)
    )
    before = adapter.inspect(project)
    assert before.root_file == root_name
    keys = [symbol.ref.object_uuid for symbol in before.symbols]
    assert len(keys) == len(set(keys))
    if not before.symbols:
        assert directory == "blank"
        return

    command = _set_value_command(
        "prj_golden", before.symbols[0].ref, before.symbols[0].value
    )
    applied = adapter.apply(project, (command,))
    attribution = CommandAttribution(
        command_id=command.command_id,
        requirement_ids=command.provenance.requirement_ids,
        risk=RiskLevel.LOW,
        selectors=applied.command_results[0].effects,
    )
    diff = build_semantic_diff(before, applied.after, (attribution,))
    assert diff.changes
    assert {item.command_id for item in diff.changes} == {command.command_id}


def test_special_golden_semantics() -> None:
    root = Path(__file__).resolve().parents[1] / "fixtures" / "kicad" / "golden"
    adapter = CstSchematicAdapter(
        FileModuleCatalog(
            Path(__file__).resolve().parents[1] / "fixtures" / "modules",
            max_files=32,
            max_bytes=2_000_000,
        )
    )
    hierarchical = adapter.inspect(root / "hierarchical")
    unicode_doc = adapter.inspect(root / "unicode")
    multi = adapter.inspect(root / "multi-unit")
    custom = adapter.inspect(root / "custom-properties")
    assert len(hierarchical.sheets) >= 2
    assert any("状态" in symbol.value for symbol in unicode_doc.symbols)
    assert len({symbol.unit for symbol in multi.symbols}) >= 2
    assert any(
        item.name.startswith("User.")
        for symbol in custom.symbols
        for item in symbol.properties
    )

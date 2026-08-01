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
from pcbflow.schematic.cst import (
    apply_edits,
    insert_before_close,
    make_atom,
    make_list,
    make_string,
    parse_cst,
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


def _wire(uuid: str, start: tuple[str, str], end: tuple[str, str]):
    return make_list(
        make_atom("wire"),
        make_list(
            make_atom("pts"),
            make_list(make_atom("xy"), make_atom(start[0]), make_atom(start[1])),
            make_list(make_atom("xy"), make_atom(end[0]), make_atom(end[1])),
        ),
        make_list(make_atom("uuid"), make_atom(uuid)),
    )


def test_add_label_rejects_same_batch_binding_to_different_nets(tmp_path: Path) -> None:
    project = tmp_path / "project"
    shutil.copytree(_fixtures() / "kicad" / "controlled-design", project)
    board = project / "board.kicad_sch"
    document = parse_cst(board.read_bytes())
    board.write_bytes(
        apply_edits(
            document,
            (
                insert_before_close(
                    document.root,
                    (
                        _wire("00000000-0000-0000-0000-000000000100", ("123.19", "88.9"), ("120", "88.9")),
                        _wire("00000000-0000-0000-0000-000000000101", ("130.81", "88.9"), ("140", "88.9")),
                    ),
                    indent=2,
                ),
            ),
        )
    )
    first = _command(
        {
            "type": "schematic.add_label",
            "payload": {
                "target_ref": {
                    "kind": "wire_endpoint",
                    "sheet_uuid": "00000000-0000-0000-0000-000000000001",
                    "object_uuid": "00000000-0000-0000-0000-000000000100",
                    "pin_number": "start",
                },
                "name": "DUPLICATE_NET",
                "scope": "local",
            },
        },
        "cmd_label_one",
    )
    second = _command(
        {
            "type": "schematic.add_label",
            "payload": {
                "target_ref": {
                    "kind": "wire_endpoint",
                    "sheet_uuid": "00000000-0000-0000-0000-000000000001",
                    "object_uuid": "00000000-0000-0000-0000-000000000101",
                    "pin_number": "start",
                },
                "name": "DUPLICATE_NET",
                "scope": "local",
            },
        },
        "cmd_label_two",
    )

    with pytest.raises(LabelTargetError, match="another net"):
        _adapter().apply(project, (first, second))


def test_add_label_resolves_explicit_net_from_existing_member_endpoint(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    shutil.copytree(_fixtures() / "kicad" / "controlled-design", project)
    board = project / "board.kicad_sch"
    document = parse_cst(board.read_bytes())
    explicit_net = make_list(
        make_atom("net"),
        make_string("KNOWN_NET"),
        make_list(
            make_atom("members"),
            make_atom("pin:00000000-0000-0000-0000-000000000001:00000000-0000-0000-0000-000000000003:1"),
        ),
        make_list(make_atom("uuid"), make_atom("00000000-0000-0000-0000-000000000102")),
    )
    board.write_bytes(apply_edits(document, (insert_before_close(document.root, (explicit_net,), indent=2),)))
    command = _command(
        {
            "type": "schematic.add_label",
            "payload": {
                "target_ref": {
                    "kind": "net",
                    "sheet_uuid": "00000000-0000-0000-0000-000000000001",
                    "object_uuid": "00000000-0000-0000-0000-000000000102",
                    "pin_number": None,
                },
                "name": "KNOWN_NET_LABEL",
                "scope": "local",
            },
        },
        "cmd_known_net_label",
    )

    result = _adapter().apply(project, (command,))

    assert any(label.name == "KNOWN_NET_LABEL" for label in result.after.labels)


def test_add_label_resolves_child_explicit_net_wire_endpoint(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "sheets").mkdir()
    (project / "root.kicad_sch").write_text(
        """(kicad_sch
  (version 20250114)
  (uuid 00000000-0000-0000-0000-000000000010)
  (sheet
    (at 25 25)
    (size 50 25)
    (uuid 00000000-0000-0000-0000-000000000011)
    (property "Sheetname" "Child")
    (property "Sheetfile" "sheets/child.kicad_sch")))
""",
        encoding="utf-8",
    )
    child = project / "sheets" / "child.kicad_sch"
    shutil.copyfile(_fixtures() / "kicad" / "controlled-design" / "board.kicad_sch", child)
    document = parse_cst(child.read_bytes())
    wire_uuid = "00000000-0000-0000-0000-000000000103"
    net_uuid = "00000000-0000-0000-0000-000000000104"
    wire = _wire(wire_uuid, ("123.19", "88.9"), ("120", "88.9"))
    explicit_net = make_list(
        make_atom("net"),
        make_string("CHILD_KNOWN_NET"),
        make_list(make_atom("members"), make_atom(f"wire:00000000-0000-0000-0000-000000000011:{wire_uuid}")),
        make_list(make_atom("uuid"), make_atom(net_uuid)),
    )
    child.write_bytes(apply_edits(document, (insert_before_close(document.root, (wire, explicit_net), indent=2),)))
    command = _command(
        {
            "type": "schematic.add_label",
            "payload": {
                "target_ref": {
                    "kind": "net",
                    "sheet_uuid": "00000000-0000-0000-0000-000000000011",
                    "object_uuid": net_uuid,
                    "pin_number": None,
                },
                "name": "CHILD_NET_LABEL",
                "scope": "local",
            },
        },
        "cmd_child_net_label",
    )

    result = _adapter().apply(project, (command,))

    assert any(label.name == "CHILD_NET_LABEL" for label in result.after.labels)


def test_add_label_resolves_a_second_level_sheet_file_from_project_root(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "sheets" / "deep").mkdir(parents=True)
    (project / "root.kicad_sch").write_text(
        """(kicad_sch
  (version 20250114)
  (uuid 00000000-0000-0000-0000-000000000010)
  (sheet
    (at 25 25)
    (size 50 25)
    (uuid 00000000-0000-0000-0000-000000000011)
    (property "Sheetname" "Child")
    (property "Sheetfile" "sheets/child.kicad_sch")))
""",
        encoding="utf-8",
    )
    fixtures = _fixtures() / "kicad" / "controlled-design" / "board.kicad_sch"
    child = project / "sheets" / "child.kicad_sch"
    grandchild = project / "sheets" / "deep" / "grand.kicad_sch"
    shutil.copyfile(fixtures, child)
    shutil.copyfile(fixtures, grandchild)

    child_document = parse_cst(child.read_bytes())
    grand_sheet_uuid = "00000000-0000-0000-0000-000000000012"
    child_sheet = make_list(
        make_atom("sheet"),
        make_list(make_atom("at"), make_atom("25"), make_atom("25")),
        make_list(make_atom("size"), make_atom("50"), make_atom("25")),
        make_list(make_atom("uuid"), make_atom(grand_sheet_uuid)),
        make_list(make_atom("property"), make_string("Sheetname"), make_string("Grand")),
        make_list(
            make_atom("property"),
            make_string("Sheetfile"),
            make_string("deep/grand.kicad_sch"),
        ),
    )
    child.write_bytes(
        apply_edits(
            child_document,
            (insert_before_close(child_document.root, (child_sheet,), indent=2),),
        )
    )

    grand_document = parse_cst(grandchild.read_bytes())
    wire_uuid = "00000000-0000-0000-0000-000000000113"
    net_uuid = "00000000-0000-0000-0000-000000000114"
    grand_wire = _wire(wire_uuid, ("123.19", "88.9"), ("120", "88.9"))
    grand_net = make_list(
        make_atom("net"),
        make_string("GRANDCHILD_NET"),
        make_list(
            make_atom("members"),
            make_atom(f"wire:{grand_sheet_uuid}:{wire_uuid}"),
        ),
        make_list(make_atom("uuid"), make_atom(net_uuid)),
    )
    grandchild.write_bytes(
        apply_edits(
            grand_document,
            (insert_before_close(grand_document.root, (grand_wire, grand_net), indent=2),),
        )
    )

    command = _command(
        {
            "type": "schematic.add_label",
            "payload": {
                "target_ref": {
                    "kind": "net",
                    "sheet_uuid": grand_sheet_uuid,
                    "object_uuid": net_uuid,
                    "pin_number": None,
                },
                "name": "GRANDCHILD_NET_LABEL",
                "scope": "local",
            },
        },
        "cmd_grandchild_net_label",
    )

    result = _adapter().apply(project, (command,))

    assert any(label.name == "GRANDCHILD_NET_LABEL" for label in result.after.labels)

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from pcbflow.commands import load_command_batch
from pcbflow.schematic.adapter import (
    CstSchematicAdapter,
    DesignCommandUnsupportedError,
)
from pcbflow.schematic.modules import (
    FileModuleCatalog,
    ModuleIntegrityError,
    ModuleRevisionNotFoundError,
    derive_module_uuid,
)


TEMPLATE_DIGEST = "sha256:af9e40b8da5a63158a95cff1d222eb8c7ce57e05e88759df50dacfc775ccfe4e"


def _fixture_root() -> Path:
    return Path(__file__).resolve().parents[1] / "fixtures"


def _command(
    project_id: str,
    revision: str,
    operation_type: str = "schematic.instantiate_module",
    port_bindings: dict[str, dict[str, str | None]] | None = None,
):
    actor = {"type": "human", "id": "local-user"}
    operation: dict[str, object] = {
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
            "port_bindings": port_bindings or {},
            "placement_slot": "auto",
        },
    }
    if operation_type == "schematic.set_property":
        operation = {
            "type": operation_type,
            "payload": {
                "subject_ref": operation["payload"]["target_sheet_ref"],
                "property_name": "Value",
                "value": "GREEN",
                "expected_old_value": None,
            },
        }
    if operation_type == "schematic.assign_footprint":
        operation = {
            "type": operation_type,
            "payload": {
                "subject_ref": {
                    "kind": "symbol",
                    "sheet_uuid": "00000000-0000-0000-0000-000000000001",
                    "object_uuid": "00000000-0000-0000-0000-000000000002",
                    "pin_number": None,
                },
                "footprint_revision_id": "fprev_led_0603_v1",
            },
        }
    value = {
        "schema_version": "1.0",
        "batch_id": "bat_status_led",
        "project_id": project_id,
        "base_revision": revision,
        "requirement_set_id": "reqset_controller",
        "idempotency_key": "status-led",
        "actor": actor,
        "intent": "Add the verified status LED",
        "risk": "medium",
        "commands": [
            {
                "schema_version": "1.0",
                "command_id": "cmd_status_led",
                "batch_id": "bat_status_led",
                "project_id": project_id,
                "base_revision": revision,
                "idempotency_key": "status-led:1",
                "actor": actor,
                "intent": "Add the verified status LED",
                "risk": "medium",
                "preconditions": [],
                "operation": operation,
                "required_validations": ["semantic_diff", "kicad_erc"],
                "provenance": {
                    "requirement_ids": ["REQ-FUNC-001"],
                    "evidence_ids": [],
                    "module_revision_ids": ["modrev_status_led_v1"],
                },
            }
        ],
    }
    return load_command_batch(json.dumps(value).encode()).commands[0]


def test_module_catalog_uses_literal_raw_template_digest() -> None:
    module_dir = _fixture_root() / "modules" / "status-led-v1"
    manifest = (module_dir / "module.yaml").read_text(encoding="utf-8")
    template_digest = "sha256:" + hashlib.sha256(
        (module_dir / "status-led.kicad_sch").read_bytes()
    ).hexdigest()

    assert TEMPLATE_DIGEST == template_digest
    assert f"digest: {TEMPLATE_DIGEST}" in manifest


def test_module_catalog_rejects_template_digest_mismatch(tmp_path: Path) -> None:
    source = _fixture_root() / "modules" / "status-led-v1"
    catalog_root = tmp_path / "modules"
    shutil.copytree(source, catalog_root / "status-led-v1")
    template = catalog_root / "status-led-v1" / "status-led.kicad_sch"
    template.write_bytes(template.read_bytes() + b"\n")

    catalog = FileModuleCatalog(catalog_root, max_files=16, max_bytes=1_000_000)
    with pytest.raises(ModuleIntegrityError, match="template digest"):
        catalog.get("modrev_status_led_v1")


def test_uuid_derivation_is_stable_and_input_bound() -> None:
    first = derive_module_uuid(
        "prj_controller", "bat_status_led", "cmd_status_led", "sha256:" + "a" * 64, "led-symbol"
    )
    repeated = derive_module_uuid(
        "prj_controller", "bat_status_led", "cmd_status_led", "sha256:" + "a" * 64, "led-symbol"
    )
    changed = derive_module_uuid(
        "prj_controller", "bat_status_led", "cmd_status_led", "sha256:" + "b" * 64, "led-symbol"
    )

    assert first == repeated
    assert first != changed


def test_instantiate_module_is_deterministic_and_does_not_modify_catalog(tmp_path: Path) -> None:
    fixture = _fixture_root() / "kicad" / "controlled-design"
    left = tmp_path / "left"
    right = tmp_path / "right"
    shutil.copytree(fixture, left)
    shutil.copytree(fixture, right)
    catalog_root = _fixture_root() / "modules"
    catalog_before = {
        path.relative_to(catalog_root): path.read_bytes()
        for path in catalog_root.rglob("*")
        if path.is_file()
    }
    adapter = CstSchematicAdapter(FileModuleCatalog(catalog_root, max_files=16, max_bytes=1_000_000))
    command = _command("prj_controller", "git:" + "1" * 40)

    left_result = adapter.apply(left, (command,))
    right_result = adapter.apply(right, (command,))

    assert left_result.modified_files == right_result.modified_files
    assert {
        path.relative_to(left): path.read_bytes() for path in left.rglob("*") if path.is_file()
    } == {
        path.relative_to(right): path.read_bytes() for path in right.rglob("*") if path.is_file()
    }
    assert any(item.name == "STATUS_LED" for item in left_result.after.sheets)
    assert catalog_before == {
        path.relative_to(catalog_root): path.read_bytes()
        for path in catalog_root.rglob("*")
        if path.is_file()
    }


def test_future_operation_returns_stable_unsupported_code(tmp_path: Path) -> None:
    project = tmp_path / "project"
    shutil.copytree(_fixture_root() / "kicad" / "controlled-design", project)
    adapter = CstSchematicAdapter(None)

    with pytest.raises(DesignCommandUnsupportedError, match="DESIGN_COMMAND_UNSUPPORTED"):
        adapter.apply(project, (_command("prj_controller", "git:" + "1" * 40, "schematic.set_property"),))


def test_footprint_operation_without_catalog_returns_stable_not_found(tmp_path: Path) -> None:
    project = tmp_path / "project"
    shutil.copytree(_fixture_root() / "kicad" / "controlled-design", project)
    adapter = CstSchematicAdapter(None)

    with pytest.raises(ModuleRevisionNotFoundError, match="MODULE_CATALOG_NOT_CONFIGURED"):
        adapter.apply(
            project,
            (_command("prj_controller", "git:" + "1" * 40, "schematic.assign_footprint"),),
        )


def test_port_binding_adds_parent_label_as_a_root_sibling(tmp_path: Path) -> None:
    project = tmp_path / "project"
    shutil.copytree(_fixture_root() / "kicad" / "controlled-design", project)
    catalog_root = tmp_path / "modules"
    shutil.copytree(_fixture_root() / "modules", catalog_root)
    manifest = catalog_root / "status-led-v1" / "module.yaml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace("ports: {}", "ports:\n  OUT: output"),
        encoding="utf-8",
    )
    binding = {
        "OUT": {
            "kind": "symbol",
            "sheet_uuid": "00000000-0000-0000-0000-000000000001",
            "object_uuid": "00000000-0000-0000-0000-000000000002",
            "pin_number": None,
        }
    }
    adapter = CstSchematicAdapter(FileModuleCatalog(catalog_root, max_files=16, max_bytes=1_000_000))

    result = adapter.apply(project, (_command("prj_controller", "git:" + "1" * 40, port_bindings=binding),))

    assert any(label.name == "OUT" for label in result.after.labels)


def test_late_semantic_failure_restores_original_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    shutil.copytree(_fixture_root() / "kicad" / "controlled-design", project)
    before = {
        path.relative_to(project): path.read_bytes()
        for path in project.rglob("*")
        if path.is_file()
    }
    adapter = CstSchematicAdapter(
        FileModuleCatalog(_fixture_root() / "modules", max_files=16, max_bytes=1_000_000)
    )

    def fail_diff(*_args, **_kwargs):
        raise RuntimeError("semantic validation failed")

    monkeypatch.setattr("pcbflow.schematic.adapter.build_semantic_diff", fail_diff)
    with pytest.raises(RuntimeError, match="semantic validation failed"):
        adapter.apply(project, (_command("prj_controller", "git:" + "1" * 40),))

    assert before == {
        path.relative_to(project): path.read_bytes()
        for path in project.rglob("*")
        if path.is_file()
    }

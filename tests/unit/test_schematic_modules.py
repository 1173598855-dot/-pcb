from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pcbflow.commands import load_command_batch
from pcbflow.component_bindings import BoundModuleResolution, ComponentModuleBinding
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
from pcbflow.schematic.cst import (
    apply_edits,
    insert_before_close,
    make_atom,
    make_list,
    make_string,
    parse_cst,
)
from pcbflow.schematic.diff import ChangeKind
from pcbflow.schematic.semantic import KicadSemanticError, object_ref_key


TEMPLATE_DIGEST = "sha256:af9e40b8da5a63158a95cff1d222eb8c7ce57e05e88759df50dacfc775ccfe4e"


def _fixture_root() -> Path:
    return Path(__file__).resolve().parents[1] / "fixtures"


class RecordingBoundModuleResolver:
    def __init__(self, resolution: BoundModuleResolution) -> None:
        self._resolution = resolution
        self.calls: list[tuple[str, int]] = []

    def resolve_for_instantiation(
        self, binding_id: str, kicad_major: int
    ) -> BoundModuleResolution:
        self.calls.append((binding_id, kicad_major))
        return self._resolution


def _bound_resolution() -> BoundModuleResolution:
    module = FileModuleCatalog(
        _fixture_root() / "modules", max_files=16, max_bytes=1_000_000
    ).get("modrev_status_led_v1")
    return BoundModuleResolution(
        binding=ComponentModuleBinding(
            id="compmod_status_led_v1",
            component_revision_id="comprev_status_led_v1",
            kicad_major=9,
            module_revision_id="modrev_status_led_v1",
            module_manifest_digest=module.manifest_digest,
            idempotency_key="bound-module-fixture",
            created_at=datetime(2026, 8, 2, tzinfo=UTC),
        ),
        module=module,
    )


def _command(
    project_id: str,
    revision: str,
    operation_type: str = "schematic.instantiate_module",
    port_bindings: dict[str, dict[str, str | None]] | None = None,
    target_sheet_ref: dict[str, str | None] | None = None,
    placement_slot: str = "auto",
):
    actor = {"type": "human", "id": "local-user"}
    operation: dict[str, object] = {
        "type": "schematic.instantiate_module",
        "payload": {
            "module_revision_id": "modrev_status_led_v1",
            "instance_name": "STATUS_LED",
            "target_sheet_ref": target_sheet_ref or {
                "kind": "sheet",
                "sheet_uuid": "00000000-0000-0000-0000-000000000001",
                "object_uuid": "00000000-0000-0000-0000-000000000001",
                "pin_number": None,
            },
            "parameter_bindings": {"LED_VALUE": "GREEN"},
            "port_bindings": port_bindings or {},
            "placement_slot": placement_slot,
        },
    }
    if operation_type == "schematic.instantiate_bound_module":
        operation = {
            "type": operation_type,
            "payload": {
                "component_module_binding_id": "compmod_status_led_v1",
                "instance_name": "STATUS_LED",
                "target_sheet_ref": operation["payload"]["target_sheet_ref"],
                "parameter_bindings": {"LED_VALUE": "GREEN"},
                "port_bindings": port_bindings or {},
                "placement_slot": placement_slot,
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


def test_module_manifest_declares_verified_kicad_majors() -> None:
    revision = FileModuleCatalog(
        _fixture_root() / "modules", max_files=32, max_bytes=2_000_000
    ).get("modrev_status_led_v1")

    assert revision.manifest.kicad_majors == (9, 10)


def test_legacy_v1_manifest_normalizes_to_one_major(tmp_path: Path) -> None:
    source = _fixture_root() / "modules" / "status-led-v1"
    destination = tmp_path / "modules" / "status-led-v1"
    shutil.copytree(source, destination)
    manifest = destination / "module.yaml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8")
        .replace('schema_version: "1.1"', 'schema_version: "1.0"')
        .replace("kicad_majors: [9, 10]", "kicad_major: 9"),
        encoding="utf-8",
    )

    revision = FileModuleCatalog(
        tmp_path / "modules", max_files=32, max_bytes=2_000_000
    ).get("modrev_status_led_v1")

    assert revision.manifest.kicad_majors == (9,)


def test_v1_1_manifest_rejects_unsorted_duplicate_majors(tmp_path: Path) -> None:
    source = _fixture_root() / "modules" / "status-led-v1"
    destination = tmp_path / "modules" / "status-led-v1"
    shutil.copytree(source, destination)
    manifest = destination / "module.yaml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "kicad_majors: [9, 10]", "kicad_majors: [10, 9, 10]"
        ),
        encoding="utf-8",
    )

    with pytest.raises(ModuleIntegrityError, match="invalid module manifest"):
        FileModuleCatalog(
            tmp_path / "modules", max_files=32, max_bytes=2_000_000
        ).get("modrev_status_led_v1")


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
    assert left_result.capability_report.bound_module_resolutions == ()


def test_adapter_instantiates_a_bound_module_from_the_resolver(tmp_path: Path) -> None:
    project = tmp_path / "project"
    shutil.copytree(_fixture_root() / "kicad" / "controlled-design", project)
    resolution = _bound_resolution()
    resolver = RecordingBoundModuleResolver(resolution)
    adapter = CstSchematicAdapter(None, bound_module_resolver=resolver)
    command = _command(
        "prj_controller",
        "git:" + "1" * 40,
        operation_type="schematic.instantiate_bound_module",
    )

    result = adapter.apply(project, (command,), kicad_major=9)

    assert resolver.calls == [("compmod_status_led_v1", 9)]
    assert result.command_results[0].operation_type == "schematic.instantiate_bound_module"
    assert result.capability_report.module_digests == (
        resolution.module.manifest_digest,
    )
    assert result.capability_report.bound_module_resolutions[0].binding_id == (
        "compmod_status_led_v1"
    )


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


def test_symbol_port_binding_is_rejected(tmp_path: Path) -> None:
    project = tmp_path / "project"
    shutil.copytree(_fixture_root() / "kicad" / "controlled-design", project)
    catalog_root = tmp_path / "modules"
    shutil.copytree(_fixture_root() / "modules", catalog_root)
    _add_out_port(catalog_root / "status-led-v1")
    binding = {
        "OUT": {
            "kind": "symbol",
            "sheet_uuid": "00000000-0000-0000-0000-000000000001",
            "object_uuid": "00000000-0000-0000-0000-000000000002",
            "pin_number": None,
        }
    }
    adapter = CstSchematicAdapter(FileModuleCatalog(catalog_root, max_files=16, max_bytes=1_000_000))

    with pytest.raises(KicadSemanticError, match="port binding target"):
        adapter.apply(project, (_command("prj_controller", "git:" + "1" * 40, port_bindings=binding),))


def test_late_semantic_failure_restores_original_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    shutil.copytree(_fixture_root() / "kicad" / "controlled-design", project)
    before = _project_tree(project)
    adapter = CstSchematicAdapter(
        FileModuleCatalog(_fixture_root() / "modules", max_files=16, max_bytes=1_000_000)
    )

    def fail_diff(*_args, **_kwargs):
        raise RuntimeError("semantic validation failed")

    monkeypatch.setattr("pcbflow.schematic.adapter.build_semantic_diff", fail_diff)
    with pytest.raises(RuntimeError, match="semantic validation failed"):
        adapter.apply(project, (_command("prj_controller", "git:" + "1" * 40),))

    assert before == _project_tree(project)


def test_nested_target_writes_child_relative_to_parent_schematic(tmp_path: Path) -> None:
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
    shutil.copyfile(
        _fixture_root() / "kicad" / "controlled-design" / "board.kicad_sch",
        project / "sheets" / "child.kicad_sch",
    )
    target = {
        "kind": "sheet",
        "sheet_uuid": "00000000-0000-0000-0000-000000000010",
        "object_uuid": "00000000-0000-0000-0000-000000000011",
        "pin_number": None,
    }
    adapter = CstSchematicAdapter(
        FileModuleCatalog(_fixture_root() / "modules", max_files=16, max_bytes=1_000_000)
    )

    result = adapter.apply(
        project,
        (_command("prj_controller", "git:" + "1" * 40, target_sheet_ref=target),),
    )

    assert result.after.root_file == "root.kicad_sch"
    assert (project / "sheets" / "generated" / "status_led-cmd_status_led.kicad_sch").is_file()
    assert not (project / "generated").exists()


def test_catalog_revision_is_deeply_immutable_and_hides_catalog_path() -> None:
    revision = FileModuleCatalog(
        _fixture_root() / "modules", max_files=16, max_bytes=1_000_000
    ).get("modrev_status_led_v1")

    assert not hasattr(revision, "template_path")
    with pytest.raises(TypeError):
        revision.manifest.uuid_bindings["new"] = "bad"  # type: ignore[index]
    with pytest.raises(TypeError):
        revision.manifest.parameters["LED_VALUE"] = object()  # type: ignore[index]


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        ("  10000000-0000-0000-0000-000000000004: led-pin-2\n", "UUID bindings"),
        ("  10000000-0000-0000-0000-000000000099: extra\n", "UUID bindings"),
    ],
)
def test_catalog_rejects_uuid_binding_set_mismatch(
    tmp_path: Path, replacement: str, message: str
) -> None:
    catalog_root = tmp_path / "modules"
    shutil.copytree(_fixture_root() / "modules", catalog_root)
    manifest = catalog_root / "status-led-v1" / "module.yaml"
    original = manifest.read_text(encoding="utf-8")
    if "extra" in replacement:
        manifest.write_text(original.replace("parameters:", replacement + "parameters:"), encoding="utf-8")
    else:
        manifest.write_text(original.replace(replacement, ""), encoding="utf-8")

    with pytest.raises(ModuleIntegrityError, match=message):
        FileModuleCatalog(catalog_root, max_files=16, max_bytes=1_000_000).get("modrev_status_led_v1")


def test_catalog_rejects_invalid_port_direction(tmp_path: Path) -> None:
    catalog_root = tmp_path / "modules"
    shutil.copytree(_fixture_root() / "modules", catalog_root)
    manifest = catalog_root / "status-led-v1" / "module.yaml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace("ports: {}", "ports:\n  OUT: invalid"),
        encoding="utf-8",
    )

    with pytest.raises(ModuleIntegrityError, match="invalid module manifest"):
        FileModuleCatalog(catalog_root, max_files=16, max_bytes=1_000_000).get("modrev_status_led_v1")


def test_non_auto_placement_slot_is_stably_unsupported(tmp_path: Path) -> None:
    project = tmp_path / "project"
    shutil.copytree(_fixture_root() / "kicad" / "controlled-design", project)
    adapter = CstSchematicAdapter(
        FileModuleCatalog(_fixture_root() / "modules", max_files=16, max_bytes=1_000_000)
    )

    with pytest.raises(DesignCommandUnsupportedError, match="DESIGN_COMMAND_UNSUPPORTED"):
        adapter.apply(
            project,
            (_command("prj_controller", "git:" + "1" * 40, placement_slot="slot_a"),),
        )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda root: (root / "status-led-v1" / "unexpected.txt").write_text("x", encoding="utf-8"),
        lambda root: (root / "status-led-v1" / "module.yaml").write_text(
            (root / "status-led-v1" / "module.yaml").read_text(encoding="utf-8").replace(
                "path: status-led.kicad_sch", "path: ../escape.kicad_sch"
            ),
            encoding="utf-8",
        ),
    ],
)
def test_catalog_rejects_unsupported_files_and_template_escape(tmp_path: Path, mutate) -> None:
    catalog_root = tmp_path / "modules"
    shutil.copytree(_fixture_root() / "modules", catalog_root)
    mutate(catalog_root)

    with pytest.raises(ModuleIntegrityError):
        FileModuleCatalog(catalog_root, max_files=16, max_bytes=1_000_000).get("modrev_status_led_v1")


@pytest.mark.parametrize(("max_files", "max_bytes"), [(1, 1_000_000), (16, 1)])
def test_catalog_enforces_file_and_byte_limits(tmp_path: Path, max_files: int, max_bytes: int) -> None:
    catalog_root = tmp_path / "modules"
    shutil.copytree(_fixture_root() / "modules", catalog_root)

    with pytest.raises(ModuleIntegrityError, match="exceeds configured limits"):
        FileModuleCatalog(catalog_root, max_files=max_files, max_bytes=max_bytes).get("modrev_status_led_v1")


def test_catalog_rejects_reparse_point_signal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    catalog_root = tmp_path / "modules"
    shutil.copytree(_fixture_root() / "modules", catalog_root)
    monkeypatch.setattr(
        "pcbflow.schematic.modules._is_reparse_point", lambda path: path.name == "module.yaml"
    )

    with pytest.raises(ModuleIntegrityError, match="links"):
        FileModuleCatalog(catalog_root, max_files=16, max_bytes=1_000_000).get("modrev_status_led_v1")


def test_catalog_rejects_symlink(tmp_path: Path) -> None:
    catalog_root = tmp_path / "modules"
    shutil.copytree(_fixture_root() / "modules", catalog_root)
    link = catalog_root / "status-led-v1" / "linked.kicad_sch"
    try:
        os.symlink(catalog_root / "status-led-v1" / "status-led.kicad_sch", link)
    except OSError as error:
        pytest.skip(f"symlink creation unavailable: {error}")

    with pytest.raises(ModuleIntegrityError, match="links"):
        FileModuleCatalog(catalog_root, max_files=16, max_bytes=1_000_000).get("modrev_status_led_v1")


def test_uuid_derivation_is_sensitive_to_every_input() -> None:
    baseline = ("prj_controller", "bat_status_led", "cmd_status_led", "sha256:" + "a" * 64, "led-symbol")
    expected = derive_module_uuid(*baseline)
    variants = (
        ("prj_other", *baseline[1:]),
        (baseline[0], "bat_other", *baseline[2:]),
        (*baseline[:2], "cmd_other", *baseline[3:]),
        (*baseline[:3], "sha256:" + "b" * 64, baseline[4]),
        (*baseline[:4], "other-symbol"),
    )

    assert all(derive_module_uuid(*variant) != expected for variant in variants)


def test_empty_port_module_returns_only_exact_sheet_and_symbol_selectors(tmp_path: Path) -> None:
    project = tmp_path / "project"
    shutil.copytree(_fixture_root() / "kicad" / "controlled-design", project)
    command = _command("prj_controller", "git:" + "1" * 40)
    adapter = CstSchematicAdapter(
        FileModuleCatalog(_fixture_root() / "modules", max_files=16, max_bytes=1_000_000)
    )

    result = adapter.apply(project, (command,))

    manifest_digest = result.capability_report.module_digests[0]
    sheet_uuid = derive_module_uuid(
        command.project_id, command.batch_id, command.command_id, manifest_digest, "sheet:STATUS_LED"
    )
    assert [item.kind for item in result.command_results[0].effects] == [
        ChangeKind.SHEET_ADDED,
        ChangeKind.SYMBOL_ADDED,
    ]
    assert result.command_results[0].effects[0].subject_ref.object_uuid == sheet_uuid
    assert result.command_results[0].effects[1].subject_ref.sheet_uuid == sheet_uuid


def test_catalog_rejects_port_without_child_hierarchical_label(tmp_path: Path) -> None:
    catalog_root = tmp_path / "modules"
    shutil.copytree(_fixture_root() / "modules", catalog_root)
    manifest = catalog_root / "status-led-v1" / "module.yaml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace("ports: {}", "ports:\n  OUT: output"),
        encoding="utf-8",
    )

    with pytest.raises(ModuleIntegrityError, match="module port labels"):
        FileModuleCatalog(catalog_root, max_files=16, max_bytes=1_000_000).get("modrev_status_led_v1")


def test_port_binding_uses_pin_position_and_exact_label_selectors(tmp_path: Path) -> None:
    catalog_root = tmp_path / "modules"
    shutil.copytree(_fixture_root() / "modules", catalog_root)
    _add_out_port(catalog_root / "status-led-v1")
    project = tmp_path / "project"
    shutil.copytree(_fixture_root() / "kicad" / "controlled-design", project)
    pin_binding = {
        "OUT": {
            "kind": "pin",
            "sheet_uuid": "00000000-0000-0000-0000-000000000001",
            "object_uuid": "00000000-0000-0000-0000-000000000003",
            "pin_number": "1",
        }
    }
    adapter = CstSchematicAdapter(FileModuleCatalog(catalog_root, max_files=16, max_bytes=1_000_000))

    result = adapter.apply(
        project,
        (_command("prj_controller", "git:" + "1" * 40, port_bindings=pin_binding),),
    )

    labels = [label for label in result.after.labels if label.name == "OUT"]
    assert any(label.scope == "local" and label.position.x == 123.19 for label in labels)
    assert any(label.scope == "hierarchical" for label in labels)
    child_sheet = next(sheet for sheet in result.after.sheets if sheet.name == "STATUS_LED")
    child_pin = next(
        pin
        for symbol in result.after.symbols
        if symbol.ref.sheet_uuid == child_sheet.ref.object_uuid
        for pin in symbol.pins
        if pin.number == "1"
    )
    child_label = next(
        label
        for label in labels
        if label.scope == "hierarchical" and label.ref.sheet_uuid == child_sheet.ref.object_uuid
    )
    parent_label = next(label for label in labels if label.scope == "local")
    parent_port = child_sheet.ports[0]
    parent_pin = next(
        pin
        for symbol in result.after.symbols
        for pin in symbol.pins
        if pin.ref.object_uuid == "00000000-0000-0000-0000-000000000003"
    )
    assert any(
        {object_ref_key(child_pin.ref), object_ref_key(child_label.ref)} <= set(net.members)
        for net in result.after.nets
    )
    assert any(
        {
            object_ref_key(parent_pin.ref),
            object_ref_key(parent_label.ref),
            object_ref_key(parent_port.ref),
        } <= set(net.members)
        for net in result.after.nets
    )
    effects = result.command_results[0].effects
    assert sum(item.kind is ChangeKind.LABEL_ADDED for item in effects) == 2
    assert all(item.field is None for item in effects if item.kind is ChangeKind.LABEL_ADDED)


def test_net_port_binding_is_rejected(tmp_path: Path) -> None:
    catalog_root = tmp_path / "modules"
    shutil.copytree(_fixture_root() / "modules", catalog_root)
    _add_out_port(catalog_root / "status-led-v1")
    project = tmp_path / "project"
    shutil.copytree(_fixture_root() / "kicad" / "controlled-design", project)
    board = project / "board.kicad_sch"
    document = parse_cst(board.read_bytes())
    wire = make_list(
        make_atom("wire"),
        make_list(
            make_atom("pts"),
            make_list(make_atom("xy"), make_atom("123.19"), make_atom("88.9")),
            make_list(make_atom("xy"), make_atom("130"), make_atom("88.9")),
        ),
        make_list(make_atom("uuid"), make_atom("00000000-0000-0000-0000-000000000100")),
    )
    board.write_bytes(apply_edits(document, (insert_before_close(document.root, (wire,), indent=2),)))
    net_binding = {
        "OUT": {
            "kind": "net",
            "sheet_uuid": "00000000-0000-0000-0000-000000000001",
            "object_uuid": "00000000-0000-0000-0000-000000000100",
            "pin_number": None,
        }
    }
    adapter = CstSchematicAdapter(FileModuleCatalog(catalog_root, max_files=16, max_bytes=1_000_000))

    with pytest.raises(KicadSemanticError, match="port binding target"):
        adapter.apply(
            project,
            (_command("prj_controller", "git:" + "1" * 40, port_bindings=net_binding),),
        )


def test_uuid_shaped_property_and_label_values_are_not_uuid_bindings(tmp_path: Path) -> None:
    catalog_root = tmp_path / "modules"
    shutil.copytree(_fixture_root() / "modules", catalog_root)
    _add_uuid_shaped_values(catalog_root / "status-led-v1")
    revision = FileModuleCatalog(catalog_root, max_files=16, max_bytes=1_000_000).get(
        "modrev_status_led_v1"
    )
    project = tmp_path / "project"
    shutil.copytree(_fixture_root() / "kicad" / "controlled-design", project)
    adapter = CstSchematicAdapter(FileModuleCatalog(catalog_root, max_files=16, max_bytes=1_000_000))

    adapter.apply(project, (_command("prj_controller", "git:" + "1" * 40),))

    child = project / "generated" / "status_led-cmd_status_led.kicad_sch"
    rendered = child.read_text(encoding="utf-8")
    assert "10000000-0000-0000-0000-000000000099" in rendered
    assert "10000000-0000-0000-0000-000000000098" in rendered
    assert "10000000-0000-0000-0000-000000000001" not in rendered
    assert revision.manifest.uuid_bindings.get("10000000-0000-0000-0000-000000000099") is None


def _add_out_port(module_dir: Path) -> None:
    template = module_dir / "status-led.kicad_sch"
    document = parse_cst(template.read_bytes())
    port = make_list(
        make_atom("hierarchical_label"),
        make_string("OUT"),
        make_list(make_atom("shape"), make_atom("output")),
        make_list(make_atom("at"), make_atom("100"), make_atom("100"), make_atom("0")),
        make_list(make_atom("uuid"), make_atom("10000000-0000-0000-0000-000000000005")),
    )
    wire = make_list(
        make_atom("wire"),
        make_list(
            make_atom("pts"),
            make_list(make_atom("xy"), make_atom("96.19"), make_atom("100")),
            make_list(make_atom("xy"), make_atom("100"), make_atom("100")),
        ),
        make_list(make_atom("uuid"), make_atom("10000000-0000-0000-0000-000000000006")),
    )
    template.write_bytes(
        apply_edits(document, (insert_before_close(document.root, (port, wire), indent=2),))
    )
    digest = "sha256:" + hashlib.sha256(template.read_bytes()).hexdigest()
    manifest = module_dir / "module.yaml"
    text = manifest.read_text(encoding="utf-8")
    text = text.replace("ports: {}", "ports:\n  OUT: output")
    text = text.replace(
        "parameters:",
        "  10000000-0000-0000-0000-000000000005: out-port\n"
        "  10000000-0000-0000-0000-000000000006: out-wire\nparameters:",
    )
    manifest.write_text(text.replace(TEMPLATE_DIGEST, digest), encoding="utf-8")


def _add_uuid_shaped_values(module_dir: Path) -> None:
    template = module_dir / "status-led.kicad_sch"
    document = parse_cst(template.read_bytes())
    symbol = document.root.find_children("symbol")[0]
    property_node = make_list(
        make_atom("property"),
        make_string("UUID_NOTE"),
        make_string("10000000-0000-0000-0000-000000000099"),
    )
    label = make_list(
        make_atom("label"),
        make_string("10000000-0000-0000-0000-000000000098"),
        make_list(make_atom("at"), make_atom("110"), make_atom("100"), make_atom("0")),
        make_list(make_atom("uuid"), make_atom("10000000-0000-0000-0000-000000000007")),
    )
    template.write_bytes(
        apply_edits(
            document,
            (
                insert_before_close(symbol, (property_node,), indent=4),
                insert_before_close(document.root, (label,), indent=2),
            ),
        )
    )
    digest = "sha256:" + hashlib.sha256(template.read_bytes()).hexdigest()
    manifest = module_dir / "module.yaml"
    text = manifest.read_text(encoding="utf-8")
    text = text.replace(
        "parameters:",
        "  10000000-0000-0000-0000-000000000007: uuid-label\nparameters:",
    )
    manifest.write_text(text.replace(TEMPLATE_DIGEST, digest), encoding="utf-8")


def _project_tree(project: Path) -> dict[str, bytes | None]:
    return {
        path.relative_to(project).as_posix(): None if path.is_dir() else path.read_bytes()
        for path in sorted(project.rglob("*"), key=lambda item: item.as_posix())
    }

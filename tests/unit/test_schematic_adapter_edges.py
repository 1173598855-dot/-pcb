"""Edge-path tests for the CST schematic adapter.

The happy paths live in test_schematic_adapter.py, test_schematic_modules.py,
and test_schematic_goldens.py. This file pins the adapter's defensive
branches: apply-level guards, property-write validation, label target
resolution failures, rollback of controlled operations, and module template
render guards.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest
from tests.unit.test_schematic_adapter import _adapter, _command, _fixtures, _wire

from pcbflow.schematic.adapter import (
    CstSchematicAdapter,
    LabelTargetError,
    PropertyWriteNotAllowedError,
)
from pcbflow.schematic.cst import (
    apply_edits,
    insert_before_close,
    parse_cst,
)
from pcbflow.schematic.modules import FileModuleCatalog
from pcbflow.schematic.semantic import KicadSemanticError


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    shutil.copytree(_fixtures() / "kicad" / "controlled-design", project)
    return project


def _symbol_ref(object_uuid: str) -> dict[str, object]:
    return {
        "kind": "symbol",
        "sheet_uuid": "00000000-0000-0000-0000-000000000001",
        "object_uuid": object_uuid,
        "pin_number": None,
    }


def _set_property(
    command_id: str,
    *,
    object_uuid: str = "00000000-0000-0000-0000-000000000002",
    name: str = "Value",
    value: str = "GREEN",
    expected_old_value: str | None = None,
    kind: str = "symbol",
):
    return _command(
        {
            "type": "schematic.set_property",
            "payload": {
                "subject_ref": _symbol_ref(object_uuid) if kind == "symbol" else {
                    "kind": kind,
                    "sheet_uuid": "00000000-0000-0000-0000-000000000001",
                    "object_uuid": object_uuid,
                    "pin_number": None,
                },
                "property_name": name,
                "value": value,
                "expected_old_value": expected_old_value,
            },
        },
        command_id,
    )


def _label(command_id: str, target_ref: dict[str, object], *, name: str, scope: str = "local"):
    return _command(
        {
            "type": "schematic.add_label",
            "payload": {"target_ref": target_ref, "name": name, "scope": scope},
        },
        command_id,
    )


def _wire_endpoint(uuid: str, pin_number: str | None) -> dict[str, object]:
    return {
        "kind": "wire_endpoint",
        "sheet_uuid": "00000000-0000-0000-0000-000000000001",
        "object_uuid": uuid,
        "pin_number": pin_number,
    }


def _inject_wire(project: Path, uuid: str) -> None:
    board = project / "board.kicad_sch"
    document = parse_cst(board.read_bytes())
    board.write_bytes(
        apply_edits(
            document,
            (
                insert_before_close(
                    document.root,
                    (_wire(uuid, ("123.19", "88.9"), ("120", "88.9")),),
                    indent=2,
                ),
            ),
        )
    )


def _instantiate(module_revision_id: str, *, parameter_bindings: dict[str, str] | None = None):
    return _command(
        {
            "type": "schematic.instantiate_module",
            "payload": {
                "module_revision_id": module_revision_id,
                "instance_name": "STATUS_LED",
                "target_sheet_ref": {
                    "kind": "sheet",
                    "sheet_uuid": "00000000-0000-0000-0000-000000000001",
                    "object_uuid": "00000000-0000-0000-0000-000000000001",
                    "pin_number": None,
                },
                "parameter_bindings": parameter_bindings or {},
                "port_bindings": {},
                "placement_slot": "auto",
            },
        },
        "cmd_instantiate",
    )


def _write_module(
    root: Path,
    module_revision_id: str,
    template: bytes,
    *,
    uuid_bindings: dict[str, str] | None = None,
    parameters: dict[str, dict[str, object]] | None = None,
) -> Path:
    module_dir = root / module_revision_id
    module_dir.mkdir(parents=True)
    template_name = f"{module_revision_id}.kicad_sch"
    (module_dir / template_name).write_bytes(template)
    digest = hashlib.sha256(template).hexdigest()
    bindings = uuid_bindings or {}
    declared_parameters = parameters or {}

    def _mapping(name: str, values: dict[str, object], render) -> str:
        if not values:
            return f"{name}: {{}}\n"
        return name + ":\n" + "".join(render(key, value) for key, value in values.items())

    manifest = (
        'schema_version: "1.1"\n'
        f"module_revision_id: {module_revision_id}\n"
        "status: verified\n"
        "name: Edge module\n"
        "kicad_majors: [9]\n"
        "adapter_contract: pcbflow.schematic.cst.v1\n"
        "template:\n"
        f"  path: {template_name}\n"
        f"  digest: sha256:{digest}\n"
        + _mapping("uuid_bindings", bindings, lambda k, v: f"  {k}: {v}\n")
        + _mapping(
            "parameters",
            declared_parameters,
            lambda k, v: (
                f"  {k}:\n"
                f"    property_name: {v['property_name']}\n"
                f"    required: {str(v.get('required', False)).lower()}\n"
            ),
        )
        + "ports: {}\n"
        "footprints: {}\n"
    )
    (module_dir / "module.yaml").write_text(manifest, encoding="utf-8")
    return module_dir.parent


def test_inspect_rejects_an_unsupported_kicad_major(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsupported KiCad major: 8"):
        CstSchematicAdapter(None).inspect(_project(tmp_path), kicad_major=8)


def test_apply_rejects_an_unsupported_kicad_major(tmp_path: Path) -> None:
    command = _set_property("cmd_major")
    with pytest.raises(ValueError, match="unsupported KiCad major: 8"):
        _adapter().apply(_project(tmp_path), (command,), kicad_major=8)


def test_apply_rejects_an_empty_command_batch(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="at least one design command is required"):
        _adapter().apply(_project(tmp_path), ())


def test_apply_rejects_multiple_instantiate_commands(tmp_path: Path) -> None:
    project = _project(tmp_path)
    commands = (
        _instantiate("modrev_status_led_v1"),
        _instantiate("modrev_status_led_v1"),
    )

    with pytest.raises(ValueError, match="exactly one instantiate command per apply"):
        _adapter().apply(project, commands)


def test_apply_rejects_a_bound_module_without_a_resolver(tmp_path: Path) -> None:
    project = _project(tmp_path)
    command = _command(
        {
            "type": "schematic.instantiate_bound_module",
            "payload": {
                "component_module_binding_id": "compmod_status_led_v1",
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
        "cmd_bound",
    )

    with pytest.raises(ValueError, match="schematic.instantiate_bound_module"):
        CstSchematicAdapter(None).apply(project, (command,))


def test_set_property_rejects_names_outside_the_allowlist(tmp_path: Path) -> None:
    command = _set_property("cmd_tolerance", name="Tolerance", value="5%")

    with pytest.raises(PropertyWriteNotAllowedError, match="Tolerance"):
        _adapter().apply(_project(tmp_path), (command,))


def test_set_property_rejects_a_stale_expected_value(tmp_path: Path) -> None:
    command = _set_property(
        "cmd_stale", expected_old_value="WRONG-VALUE"
    )

    with pytest.raises(ValueError, match="property value mismatch: Value"):
        _adapter().apply(_project(tmp_path), (command,))


def test_set_property_rejects_a_duplicate_reference(tmp_path: Path) -> None:
    project = tmp_path / "multi-unit"
    shutil.copytree(_fixtures() / "kicad" / "golden" / "multi-unit", project)
    command = _set_property(
        "cmd_duplicate_ref",
        object_uuid="00000000-0000-0000-0000-000000000002",
        name="Reference",
        value="D2",
        expected_old_value="D1",
    )

    with pytest.raises(ValueError, match="reference already exists: D2"):
        _adapter().apply(project, (command,))


def test_set_property_renames_a_reference_to_a_unique_value(tmp_path: Path) -> None:
    project = tmp_path / "multi-unit"
    shutil.copytree(_fixtures() / "kicad" / "golden" / "multi-unit", project)
    command = _set_property(
        "cmd_rename_ref",
        object_uuid="00000000-0000-0000-0000-000000000002",
        name="Reference",
        value="D9",
        expected_old_value="D1",
    )

    result = _adapter().apply(project, (command,))

    assert result.command_results[0].effects
    assert any("multi-unit.kicad_sch" in path for path in result.modified_files)


def test_set_property_inserts_a_missing_user_property(tmp_path: Path) -> None:
    project = _project(tmp_path)
    command = _set_property("cmd_user_property", name="User.Tolerance", value="5%")

    result = _adapter().apply(project, (command,))

    assert result.command_results[0].effects
    content = (project / "board.kicad_sch").read_text(encoding="utf-8")
    assert 'property "User.Tolerance" "5%"' in content


def test_set_property_rejects_overlapping_edits_in_one_batch(tmp_path: Path) -> None:
    project = _project(tmp_path)
    commands = (
        _set_property("cmd_first", expected_old_value="状态LED"),
        _set_property("cmd_second", value="BLUE", expected_old_value="状态LED"),
    )

    with pytest.raises(ValueError, match="CST edits overlap"):
        _adapter().apply(project, commands)


def test_set_property_rejects_a_non_symbol_subject(tmp_path: Path) -> None:
    command = _set_property(
        "cmd_not_symbol",
        object_uuid="00000000-0000-0000-0000-000000000100",
        kind="net",
    )

    with pytest.raises(KicadSemanticError, match="must be a symbol"):
        _adapter().apply(_project(tmp_path), (command,))


def test_set_property_rejects_a_missing_symbol(tmp_path: Path) -> None:
    command = _set_property(
        "cmd_missing_symbol", object_uuid="00000000-0000-0000-0000-000000000999"
    )

    with pytest.raises(KicadSemanticError, match="symbol target was not found"):
        _adapter().apply(_project(tmp_path), (command,))


def test_add_label_resolves_the_end_of_a_wire(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _inject_wire(project, "00000000-0000-0000-0000-000000000100")
    command = _label(
        "cmd_wire_end", _wire_endpoint("00000000-0000-0000-0000-000000000100", "2"),
        name="END_NET",
    )

    result = _adapter().apply(project, (command,))

    assert result.command_results[0].effects
    added = next(label for label in result.after.labels if label.name == "END_NET")
    assert (added.position.x, added.position.y) == (120.0, 88.9)
    content = (project / "board.kicad_sch").read_text(encoding="utf-8")
    assert "(at 120 88.9" in content


def test_add_label_requires_a_wire_endpoint_selector(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _inject_wire(project, "00000000-0000-0000-0000-000000000100")
    command = _label(
        "cmd_no_endpoint", _wire_endpoint("00000000-0000-0000-0000-000000000100", None),
        name="BAD_NET",
    )

    with pytest.raises(LabelTargetError, match="must identify start/end or 1/2"):
        _adapter().apply(project, (command,))


def test_add_label_rejects_an_unknown_wire_endpoint_selector(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _inject_wire(project, "00000000-0000-0000-0000-000000000100")
    command = _label(
        "cmd_bad_endpoint", _wire_endpoint("00000000-0000-0000-0000-000000000100", "3"),
        name="BAD_NET",
    )

    with pytest.raises(LabelTargetError, match="must be start/end or 1/2"):
        _adapter().apply(project, (command,))


def test_add_label_rejects_an_unknown_wire_uuid(tmp_path: Path) -> None:
    project = _project(tmp_path)
    command = _label(
        "cmd_unknown_wire", _wire_endpoint("00000000-0000-0000-0000-000000000999", "start"),
        name="LOST_NET",
    )

    with pytest.raises(LabelTargetError, match="does not resolve"):
        _adapter().apply(project, (command,))


def test_add_label_adds_a_shape_node_for_global_labels(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _inject_wire(project, "00000000-0000-0000-0000-000000000100")
    command = _label(
        "cmd_global", _wire_endpoint("00000000-0000-0000-0000-000000000100", "start"),
        name="GLOBAL_NET",
        scope="global",
    )

    result = _adapter().apply(project, (command,))

    assert result.command_results[0].effects
    content = (project / "board.kicad_sch").read_text(encoding="utf-8")
    assert "global_label" in content
    assert "(shape input)" in content


def test_late_semantic_failure_restores_controlled_operation_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    board = project / "board.kicad_sch"
    before = board.read_bytes()

    def fail_diff(*_args, **_kwargs):
        raise RuntimeError("semantic validation failed")

    monkeypatch.setattr("pcbflow.schematic.adapter.build_semantic_diff", fail_diff)
    with pytest.raises(RuntimeError, match="semantic validation failed"):
        _adapter().apply(project, (_set_property("cmd_rollback"),))

    assert board.read_bytes() == before


def test_instantiate_rejects_a_template_that_is_not_a_schematic(
    tmp_path: Path,
) -> None:
    catalog_root = _write_module(
        tmp_path / "modules",
        "modrev_edge_not_schematic",
        b"(kicad_pcb (uuid 10000000-0000-0000-0000-000000000009))\n",
        uuid_bindings={"10000000-0000-0000-0000-000000000009": "module-root"},
    )
    project = _project(tmp_path)

    with pytest.raises(ValueError, match="not a KiCad schematic"):
        CstSchematicAdapter(
            FileModuleCatalog(catalog_root, max_files=16, max_bytes=1_000_000)
        ).apply(project, (_instantiate("modrev_edge_not_schematic"),))


def test_instantiate_rejects_an_ambiguous_parameter_property(tmp_path: Path) -> None:
    template = (
        b"(kicad_sch (version 20250114) (uuid 10000000-0000-0000-0000-000000000009)\n"
        b'  (symbol (lib_id "Device:LED") (at 0 0 0) (unit 1)\n'
        b"    (uuid 10000000-0000-0000-0000-000000000020)\n"
        b'    (property "Value" "A") (property "Value" "B")))\n'
    )
    catalog_root = _write_module(
        tmp_path / "modules",
        "modrev_edge_ambiguous",
        template,
        uuid_bindings={
            "10000000-0000-0000-0000-000000000009": "module-root",
            "10000000-0000-0000-0000-000000000020": "edge-symbol",
        },
        parameters={"LED_VALUE": {"property_name": "Value", "required": True}},
    )
    project = _project(tmp_path)

    with pytest.raises(ValueError, match="parameter property is ambiguous"):
        CstSchematicAdapter(
            FileModuleCatalog(catalog_root, max_files=16, max_bytes=1_000_000)
        ).apply(
            project,
            (
                _instantiate(
                    "modrev_edge_ambiguous", parameter_bindings={"LED_VALUE": "X"}
                ),
            ),
        )


def _renderable_module(tmp_path: Path, module_revision_id: str) -> Path:
    template = (
        b"(kicad_sch (version 20250114) (uuid 10000000-0000-0000-0000-000000000009)\n"
        b'  (property "Value" "A"))\n'
    )
    return _write_module(
        tmp_path / "modules",
        module_revision_id,
        template,
        uuid_bindings={"10000000-0000-0000-0000-000000000009": "module-root"},
        parameters={"LED_VALUE": {"property_name": "Value", "required": True}},
    )


def test_apply_rejects_a_mixed_unsupported_operation(tmp_path: Path) -> None:
    project = _project(tmp_path)
    commands = (
        _set_property("cmd_valid"),
        _instantiate("modrev_status_led_v1"),
    )

    with pytest.raises(ValueError, match="schematic.instantiate_module"):
        _adapter().apply(project, commands)


def test_apply_rejects_a_project_that_is_not_a_real_directory(
    tmp_path: Path,
) -> None:
    with pytest.raises(KicadSemanticError, match="must be a real directory"):
        _adapter().apply(
            tmp_path / "missing-project", (_set_property("cmd_ghost"),)
        )


def test_instantiate_rejects_an_unknown_target_sheet(tmp_path: Path) -> None:
    catalog_root = _renderable_module(tmp_path, "modrev_edge_renderable")
    project = _project(tmp_path)
    command = _command(
        {
            "type": "schematic.instantiate_module",
            "payload": {
                "module_revision_id": "modrev_edge_renderable",
                "instance_name": "STATUS_LED",
                "target_sheet_ref": {
                    "kind": "sheet",
                    "sheet_uuid": "00000000-0000-0000-0000-000000000001",
                    "object_uuid": "00000000-0000-0000-0000-000000000999",
                    "pin_number": None,
                },
                "parameter_bindings": {"LED_VALUE": "GREEN"},
                "port_bindings": {},
                "placement_slot": "auto",
            },
        },
        "cmd_bad_sheet",
    )

    with pytest.raises(KicadSemanticError, match="module target sheet was not found"):
        CstSchematicAdapter(
            FileModuleCatalog(catalog_root, max_files=16, max_bytes=1_000_000)
        ).apply(project, (command,))


def test_instantiate_rejects_unknown_parameter_bindings(tmp_path: Path) -> None:
    catalog_root = _renderable_module(tmp_path, "modrev_edge_params")
    project = _project(tmp_path)

    with pytest.raises(ValueError, match="parameter bindings do not match manifest"):
        CstSchematicAdapter(
            FileModuleCatalog(catalog_root, max_files=16, max_bytes=1_000_000)
        ).apply(
            project,
            (_instantiate("modrev_edge_params", parameter_bindings={"BOGUS": "x"}),),
        )


def test_instantiate_rejects_port_bindings_on_a_portless_module(tmp_path: Path) -> None:
    catalog_root = _renderable_module(tmp_path, "modrev_edge_ports")
    project = _project(tmp_path)
    command = _command(
        {
            "type": "schematic.instantiate_module",
            "payload": {
                "module_revision_id": "modrev_edge_ports",
                "instance_name": "STATUS_LED",
                "target_sheet_ref": {
                    "kind": "sheet",
                    "sheet_uuid": "00000000-0000-0000-0000-000000000001",
                    "object_uuid": "00000000-0000-0000-0000-000000000001",
                    "pin_number": None,
                },
                "parameter_bindings": {"LED_VALUE": "GREEN"},
                "port_bindings": {
                    "OUT": {
                        "kind": "net",
                        "sheet_uuid": "00000000-0000-0000-0000-000000000001",
                        "object_uuid": "net-gnd",
                        "pin_number": None,
                    }
                },
                "placement_slot": "auto",
            },
        },
        "cmd_ports",
    )

    with pytest.raises(ValueError, match="port bindings do not match manifest"):
        CstSchematicAdapter(
            FileModuleCatalog(catalog_root, max_files=16, max_bytes=1_000_000)
        ).apply(project, (command,))

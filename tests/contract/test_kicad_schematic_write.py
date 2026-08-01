from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from pcbflow.commands import load_command_batch
from pcbflow.kicad import KicadCli, parse_kicad_report
from pcbflow.process import ProcessRunner
from pcbflow.schematic.adapter import CstSchematicAdapter
from pcbflow.schematic.modules import FileModuleCatalog


@pytest.mark.kicad
def test_real_kicad_validates_versioned_fixture(
    tmp_path: Path,
) -> None:
    executable = KicadCli.locate()
    if executable is None:
        pytest.skip("kicad-cli is not installed")
    kicad = KicadCli(ProcessRunner(2_000_000), executable, 120)
    capability = kicad.probe()
    if not capability.available or capability.major not in (9, 10):
        pytest.skip("a verified KiCad 9 or 10 CLI is not available")

    fixtures = Path(__file__).resolve().parents[1] / "fixtures"
    project = tmp_path / f"KiCad {capability.major} validation"
    shutil.copytree(
        fixtures / "kicad" / "real" / str(capability.major) / "validation",
        project,
    )
    reports = kicad.validate(project, tmp_path / "validation-output")
    assert {report.kind for report in reports} == {"erc", "drc"}
    assert all(report.profile_id == capability.profile_id for report in reports)
    assert all(report.profile_revision == capability.profile_revision for report in reports)


@pytest.mark.kicad
def test_real_kicad10_controlled_write_uses_profile(tmp_path: Path) -> None:
    executable = KicadCli.locate()
    if executable is None:
        pytest.skip("kicad-cli is not installed")
    kicad = KicadCli(ProcessRunner(2_000_000), executable, 120)
    capability = kicad.probe()
    if not capability.available or capability.major != 10:
        pytest.skip("verified KiCad 10 CLI is not available")

    fixtures = Path(__file__).resolve().parents[1] / "fixtures"
    project = tmp_path / "KiCad 10 controlled candidate"
    shutil.copytree(
        fixtures / "kicad" / "real" / "10" / "controlled-write",
        project,
    )
    adapter = CstSchematicAdapter(
        FileModuleCatalog(
            fixtures / "modules",
            max_files=32,
            max_bytes=2_000_000,
        )
    )
    before = adapter.inspect(project, kicad_major=10)
    symbol = next(item for item in before.symbols if item.reference == "U1")
    command = load_command_batch(
        json.dumps(_real_set_property_batch(symbol.ref)).encode()
    ).commands[0]
    result = adapter.apply(project, (command,), kicad_major=10)

    assert result.after.kicad_major == 10
    assert next(item for item in result.after.symbols if item.ref == symbol.ref).value == "KICAD10"
    reports = kicad.validate(project, tmp_path / "erc-controlled")
    erc = next(report for report in reports if report.kind == "erc")
    assert parse_kicad_report("erc", erc.data)
    assert erc.profile_id == "kicad-10-v1"


def _assert_erc(
    kicad: KicadCli,
    project: Path,
    output: Path,
    expect_findings: bool,
) -> None:
    reports = kicad.validate(project, output)
    erc = [item for item in reports if item.kind == "erc"]
    assert len(erc) == 1
    findings = parse_kicad_report("erc", erc[0].data).findings
    assert bool(findings) is expect_findings


def _real_set_property_batch(symbol_ref) -> dict[str, object]:
    actor = {"type": "human", "id": "contract-test"}
    command = {
        "schema_version": "1.0",
        "command_id": "cmd_real_kicad_set_property",
        "batch_id": "bat_real_kicad_set_property",
        "project_id": "prj_real_kicad",
        "base_revision": "git:" + "1" * 40,
        "idempotency_key": "real-kicad-set-property:1",
        "actor": actor,
        "intent": "Set a verified schematic property",
        "risk": "low",
        "preconditions": [],
        "operation": {
            "type": "schematic.set_property",
            "payload": {
                "subject_ref": symbol_ref.model_dump(mode="json"),
                "property_name": "Value",
                "value": "KICAD10",
                "expected_old_value": None,
            },
        },
        "required_validations": ["semantic_diff", "kicad_erc"],
        "provenance": {
            "requirement_ids": ["REQ-FUNC-001"],
            "evidence_ids": [],
            "module_revision_ids": [],
        },
    }
    return {
        "schema_version": "1.0",
        "batch_id": "bat_real_kicad_set_property",
        "project_id": "prj_real_kicad",
        "base_revision": "git:" + "1" * 40,
        "requirement_set_id": "reqset_real_kicad",
        "idempotency_key": "real-kicad-set-property",
        "actor": actor,
        "intent": "Set a verified schematic property",
        "risk": "low",
        "commands": [command],
    }


def _real_kicad_batch() -> dict[str, object]:
    actor = {"type": "human", "id": "contract-test"}
    return {
        "schema_version": "1.0",
        "batch_id": "bat_real_kicad",
        "project_id": "prj_real_kicad",
        "base_revision": "git:" + "1" * 40,
        "requirement_set_id": "reqset_real_kicad",
        "idempotency_key": "real-kicad",
        "actor": actor,
        "intent": "Instantiate the verified module under real KiCad",
        "risk": "medium",
        "commands": [
            {
                "schema_version": "1.0",
                "command_id": "cmd_real_kicad",
                "batch_id": "bat_real_kicad",
                "project_id": "prj_real_kicad",
                "base_revision": "git:" + "1" * 40,
                "idempotency_key": "real-kicad:1",
                "actor": actor,
                "intent": "Instantiate the verified module under real KiCad",
                "risk": "medium",
                "preconditions": [],
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
        ],
    }

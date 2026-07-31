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
def test_real_kicad9_validates_controlled_write_and_all_goldens(
    tmp_path: Path,
) -> None:
    executable = KicadCli.locate()
    if executable is None:
        pytest.skip("kicad-cli is not installed")
    kicad = KicadCli(ProcessRunner(2_000_000), executable, 120)
    capability = kicad.probe()
    if (
        not capability.available
        or capability.version is None
        or not capability.version.startswith("9.")
    ):
        pytest.skip("supported KiCad 9 CLI is not available")

    fixtures = Path(__file__).resolve().parents[1] / "fixtures"
    project = tmp_path / "KiCad 9 controlled candidate"
    shutil.copytree(fixtures / "kicad" / "controlled-design", project)
    adapter = CstSchematicAdapter(
        FileModuleCatalog(fixtures / "modules", max_files=32, max_bytes=2_000_000)
    )
    command = load_command_batch(json.dumps(_real_kicad_batch()).encode()).commands[0]
    adapter.apply(project, (command,))
    _assert_erc(kicad, project, tmp_path / "erc-controlled", False)

    cases = (
        ("blank", False),
        ("simple", False),
        ("hierarchical", False),
        ("unicode", False),
        ("multi-unit", False),
        ("custom-properties", False),
        ("erc-error", True),
    )
    golden_root = fixtures / "kicad" / "golden"
    for directory, expect_findings in cases:
        golden = tmp_path / f"golden {directory}"
        shutil.copytree(golden_root / directory, golden)
        _assert_erc(
            kicad,
            golden,
            tmp_path / f"erc-{directory}",
            expect_findings,
        )


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

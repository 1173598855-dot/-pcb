"""Contract tests for the real KiCad manufacturing exporter.

These tests drive a genuine ``kicad-cli`` and are therefore marked ``kicad``.
They skip cleanly when no supported executable is installed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pcbflow.kicad import KicadCli
from pcbflow.kicad_export import (
    KicadExportRequest,
    KicadExportUnavailableError,
    KicadManufacturingExporter,
)
from pcbflow.process import ProcessRunner


FIXTURE = Path(__file__).parents[1] / "fixtures" / "kicad" / "real" / "10" / "validation"


class _NeverRuns:
    def run(self, argv, cwd, timeout_seconds, *, env=None):  # pragma: no cover
        raise AssertionError("a missing executable must not be invoked")


def test_exporter_requires_an_executable() -> None:
    exporter = KicadManufacturingExporter(_NeverRuns(), executable=None)

    with pytest.raises(KicadExportUnavailableError):
        exporter.export(
            KicadExportRequest(
                board_file=FIXTURE / "EuroCard160mmX100mm.kicad_pcb",
                schematic_file=None,
            ),
            FIXTURE,
        )


def test_export_request_rejects_empty_layers() -> None:
    with pytest.raises(ValueError, match="copper_layers"):
        KicadExportRequest(
            board_file=FIXTURE / "EuroCard160mmX100mm.kicad_pcb",
            schematic_file=None,
            copper_layers=(),
        )


@pytest.mark.kicad
def test_real_kicad_exports_gerber_drill_position_bom_and_drc(tmp_path: Path) -> None:
    executable = KicadCli.locate()
    if executable is None:
        pytest.skip("kicad-cli is not installed")

    exporter = KicadManufacturingExporter(
        ProcessRunner(2_000_000), executable, timeout_seconds=120
    )
    result = exporter.export(
        KicadExportRequest(
            board_file=FIXTURE / "EuroCard160mmX100mm.kicad_pcb",
            schematic_file=FIXTURE / "EuroCard160mmX100mm.kicad_sch",
            copper_layers=("F.Cu", "B.Cu"),
        ),
        tmp_path / "release",
    )

    assert result.gerber_files, "at least one Gerber must be produced"
    assert any(path.suffix == ".gtl" for path in result.gerber_files)
    assert any(path.suffix == ".gbl" for path in result.gerber_files)
    assert result.drill_files and result.drill_files[0].suffix == ".drl"
    assert result.drc_report.is_file()
    for path in result.all_files():
        assert path.is_file() and path.stat().st_size > 0


@pytest.mark.kicad
def test_real_kicad_drc_report_is_machine_readable(tmp_path: Path) -> None:
    executable = KicadCli.locate()
    if executable is None:
        pytest.skip("kicad-cli is not installed")

    exporter = KicadManufacturingExporter(
        ProcessRunner(2_000_000), executable, timeout_seconds=120
    )
    result = exporter.export(
        KicadExportRequest(
            board_file=FIXTURE / "EuroCard160mmX100mm.kicad_pcb",
            schematic_file=None,
        ),
        tmp_path / "release",
    )

    payload = json.loads(result.drc_report.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    assert "violations" in payload or "sheets" in payload

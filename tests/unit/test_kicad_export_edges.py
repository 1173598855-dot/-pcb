"""Failure-contract tests for the real KiCad manufacturing exporter.

The happy path is exercised by tests/contract/test_kicad_export_contract.py
against a genuine ``kicad-cli``. This file pins the exporter's fail-closed
behavior with a scripted runner: command timeouts, nonzero exits, missing
inputs, degraded optional artifacts, and empty output directories. The
orchestration test also freezes the exact ``kicad-cli`` argv contract.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from pcbflow.kicad_export import (
    KicadExportError,
    KicadExportRequest,
    KicadExportUnavailableError,
    KicadManufacturingExporter,
)
from pcbflow.process import ProcessResult, ProcessTimeoutError

Handler = Callable[[list[str], Path], ProcessResult]


class ScriptedRunner:
    """A ProcessPort stub whose responses come from a per-command handler."""

    def __init__(self, handler: Handler) -> None:
        self._handler = handler
        self.calls: list[tuple[list[str], Path]] = []

    def run(
        self, argv: list[str], cwd: Path, timeout_seconds: float, *, env=None
    ) -> ProcessResult:
        self.calls.append((list(argv), cwd))
        return self._handler(argv, cwd)


def _ok() -> ProcessResult:
    return ProcessResult((), 0, "", "", False)


def _failing(returncode: int, stderr: str) -> ProcessResult:
    return ProcessResult((), returncode, "", stderr, False)


def _exporter(
    tmp_path: Path, handler: Handler, *, schematic: bool = True
) -> tuple[KicadManufacturingExporter, ScriptedRunner, Path, Path]:
    executable = tmp_path / "kicad-cli.exe"
    executable.write_bytes(b"stub")
    board = tmp_path / "board.kicad_pcb"
    board.write_text("(kicad_pcb)", encoding="utf-8")
    schematic_file = None
    if schematic:
        schematic_file = tmp_path / "board.kicad_sch"
        schematic_file.write_text("(kicad_sch)", encoding="utf-8")
    runner = ScriptedRunner(handler)
    exporter = KicadManufacturingExporter(runner, executable)
    request = KicadExportRequest(
        board_file=board, schematic_file=schematic_file
    )
    return exporter, runner, request, tmp_path


def _success_handler(argv: list[str], cwd: Path) -> ProcessResult:
    """Simulate a kicad-cli that writes its artifacts per export command."""
    if cwd.name == "gerbers":
        (cwd / "board-F_Cu.gtl").write_bytes(b"G04 front copper*\nM02*\n")
        (cwd / "board-B_Cu.gbl").write_bytes(b"G04 back copper*\nM02*\n")
        (cwd / "board-job.gbrjob").write_text("{}", encoding="utf-8")
    elif cwd.name == "drill":
        (cwd / "board-PTH.drl").write_bytes(b"M48\nM30\n")
        (cwd / "board-NPTH.drl").write_bytes(b"M48\nM30\n")
    elif argv[1:3] == ["pcb", "export"]:
        (cwd / "positions.csv").write_text("Ref,X,Y\nR1,1,2\n", encoding="utf-8")
    elif argv[1:3] == ["sch", "export"]:
        (cwd / "bom.csv").write_text("Ref,Value\nR1,10k\n", encoding="utf-8")
    elif argv[1:3] == ["pcb", "drc"]:
        (cwd / "drc.json").write_text("{}", encoding="utf-8")
    return _ok()


def test_successful_export_freezes_the_kicad_cli_argv_contract(
    tmp_path: Path,
) -> None:
    exporter, runner, request, output = _exporter(
        tmp_path, _success_handler
    )
    resolved_output = (output / "export").resolve()

    result = exporter.export(request, output / "export")

    commands = [argv[1:3] for argv, _cwd in runner.calls]
    assert commands == [
        ["pcb", "export"],
        ["pcb", "export"],
        ["pcb", "export"],
        ["sch", "export"],
        ["pcb", "drc"],
    ]
    subcommands = [argv[3] if argv[2] == "export" else argv[2] for argv, _cwd in runner.calls]
    assert subcommands == ["gerbers", "drill", "pos", "bom", "drc"]
    gerbers_argv, gerbers_cwd = runner.calls[0]
    assert gerbers_argv[4:8] == ["--output", str(result.gerber_dir), "--layers", "F.Cu,B.Cu"]
    assert gerbers_cwd == result.gerber_dir
    drill_argv, drill_cwd = runner.calls[1]
    assert drill_argv[4:8] == [
        "--output",
        str(resolved_output / "drill"),
        "--format",
        "excellon",
    ]
    assert drill_cwd == resolved_output / "drill"
    assert runner.calls[4][0][3:7] == ["--format", "json", "--output", str(result.drc_report)]
    assert result.position_file == resolved_output / "positions.csv"
    assert result.bom_file == resolved_output / "bom.csv"
    assert result.drc_report == resolved_output / "drc.json"
    assert result.gerber_files == (
        resolved_output / "gerbers" / "board-B_Cu.gbl",
        resolved_output / "gerbers" / "board-F_Cu.gtl",
        resolved_output / "gerbers" / "board-job.gbrjob",
    )
    assert result.drill_files == (
        resolved_output / "drill" / "board-NPTH.drl",
        resolved_output / "drill" / "board-PTH.drl",
    )
    assert result.all_files() == (
        *result.gerber_files,
        *result.drill_files,
        result.drc_report,
        result.position_file,
        result.bom_file,
    )


def test_successful_export_without_schematic_skips_the_bom_command(
    tmp_path: Path,
) -> None:
    exporter, runner, request, output = _exporter(
        tmp_path, _success_handler, schematic=False
    )

    result = exporter.export(request, output / "export")

    assert result.bom_file is None
    assert [argv[1:3] for argv, _cwd in runner.calls] == [
        ["pcb", "export"],
        ["pcb", "export"],
        ["pcb", "export"],
        ["pcb", "drc"],
    ]


def test_missing_executable_is_reported_without_running_anything(
    tmp_path: Path,
) -> None:
    runner = ScriptedRunner(_success_handler)
    exporter = KicadManufacturingExporter(runner, tmp_path / "missing-cli.exe")

    with pytest.raises(KicadExportUnavailableError):
        exporter.export(
            KicadExportRequest(
                board_file=tmp_path / "board.kicad_pcb", schematic_file=None
            ),
            tmp_path / "export",
        )
    assert runner.calls == []


def test_missing_board_file_fails_before_creating_output(tmp_path: Path) -> None:
    def handler(argv: list[str], cwd: Path) -> ProcessResult:
        raise AssertionError("no command may run for a missing board")

    exporter, runner, _request, output = _exporter(tmp_path, handler)
    request = KicadExportRequest(
        board_file=tmp_path / "missing.kicad_pcb", schematic_file=None
    )

    with pytest.raises(KicadExportError) as raised:
        exporter.export(request, output / "export")

    assert "board not found" in raised.value.stderr
    assert raised.value.returncode == -1
    assert runner.calls == []
    assert not (output / "export").exists()


def test_command_timeout_maps_to_a_timeout_export_error(tmp_path: Path) -> None:
    def handler(argv: list[str], cwd: Path) -> ProcessResult:
        raise ProcessTimeoutError(tuple(argv), 120.0)

    exporter, _runner, request, output = _exporter(tmp_path, handler)

    with pytest.raises(KicadExportError) as raised:
        exporter.export(request, output / "export")

    assert raised.value.command == "pcb export"
    assert raised.value.returncode == -1
    assert raised.value.stderr == "export timed out"


def test_failing_command_maps_its_exit_code_and_stderr(tmp_path: Path) -> None:
    exporter, _runner, request, output = _exporter(
        tmp_path, lambda _argv, _cwd: _failing(3, "gerber backend crashed")
    )

    with pytest.raises(KicadExportError) as raised:
        exporter.export(request, output / "export")

    assert raised.value.command == "pcb export"
    assert raised.value.returncode == 3
    assert raised.value.stderr == "gerber backend crashed"


def test_failed_position_export_degrades_to_none(tmp_path: Path) -> None:
    def handler(argv: list[str], cwd: Path) -> ProcessResult:
        if argv[3] == "pos":
            return _failing(1, "no footprint positions")
        return _success_handler(argv, cwd)

    exporter, _runner, request, output = _exporter(tmp_path, handler)

    result = exporter.export(request, output / "export")

    assert result.position_file is None
    assert result.bom_file is not None


def test_failed_bom_export_degrades_to_none(tmp_path: Path) -> None:
    def handler(argv: list[str], cwd: Path) -> ProcessResult:
        if argv[1:3] == ["sch", "export"]:
            return _failing(1, "bom plugin missing")
        return _success_handler(argv, cwd)

    exporter, _runner, request, output = _exporter(tmp_path, handler)

    result = exporter.export(request, output / "export")

    assert result.bom_file is None
    assert result.position_file is not None


def test_export_succeeds_when_optional_files_are_not_written(tmp_path: Path) -> None:
    def handler(argv: list[str], cwd: Path) -> ProcessResult:
        result = _success_handler(argv, cwd)
        # The commands claim success but write nothing for the optional steps.
        if argv[3] in {"pos", "bom"}:
            (cwd / "positions.csv").unlink(missing_ok=True)
            (cwd / "bom.csv").unlink(missing_ok=True)
        return result

    exporter, _runner, request, output = _exporter(tmp_path, handler)

    result = exporter.export(request, output / "export")

    assert result.position_file is None
    assert result.bom_file is None


def test_missing_drc_report_fails_closed(tmp_path: Path) -> None:
    def handler(argv: list[str], cwd: Path) -> ProcessResult:
        if argv[1:3] == ["pcb", "drc"]:
            return _ok()
        return _success_handler(argv, cwd)

    exporter, _runner, request, output = _exporter(tmp_path, handler)

    with pytest.raises(KicadExportError) as raised:
        exporter.export(request, output / "export")

    assert raised.value.stderr == "DRC report was not created"
    assert raised.value.returncode == 0


def test_empty_gerber_output_fails_closed(tmp_path: Path) -> None:
    def handler(argv: list[str], cwd: Path) -> ProcessResult:
        if argv[1:3] == ["pcb", "drc"]:
            (cwd / "drc.json").write_text("{}", encoding="utf-8")
        return _ok()

    exporter, _runner, request, output = _exporter(tmp_path, handler)

    with pytest.raises(KicadExportError) as raised:
        exporter.export(request, output / "export")

    assert raised.value.stderr == "no Gerber files were produced"


def test_empty_drill_output_fails_closed(tmp_path: Path) -> None:
    def handler(argv: list[str], cwd: Path) -> ProcessResult:
        if cwd.name == "gerbers":
            (cwd / "board-F_Cu.gtl").write_bytes(b"G04*\nM02*\n")
        elif argv[1:3] == ["pcb", "drc"]:
            (cwd / "drc.json").write_text("{}", encoding="utf-8")
        return _ok()

    exporter, _runner, request, output = _exporter(tmp_path, handler)

    with pytest.raises(KicadExportError) as raised:
        exporter.export(request, output / "export")

    assert raised.value.stderr == "no drill files were produced"


def test_export_request_rejects_blank_layer_names() -> None:
    with pytest.raises(ValueError, match="nonblank"):
        KicadExportRequest(
            board_file=Path("board.kicad_pcb"),
            schematic_file=None,
            copper_layers=("F.Cu", "   "),
        )

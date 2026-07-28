import hashlib
import json
from pathlib import Path

import pytest

from pcbflow.domain import NormalizedFinding
from pcbflow.kicad import (
    AmbiguousKicadProjectError,
    KicadCli,
    KicadReportFormatError,
    parse_kicad_report,
)
from pcbflow.process import ProcessResult


class VersionRunner:
    def __init__(self, result: ProcessResult | None = None) -> None:
        self.result = result or ProcessResult(
            ("kicad-cli", "--version"), 0, "9.0.2\n", "", False
        )
        self.calls: list[tuple[tuple[str, ...], Path, float]] = []

    def run(self, argv, cwd, timeout_seconds):
        self.calls.append((tuple(argv), cwd, timeout_seconds))
        return self.result


def test_probe_returns_version_and_digest(tmp_path: Path) -> None:
    executable = tmp_path / "kicad-cli.exe"
    executable.write_bytes(b"fixture executable")
    runner = VersionRunner()

    report = KicadCli(runner, executable, 5).probe()

    expected = hashlib.sha256(b"fixture executable").hexdigest()
    assert report.available
    assert report.path == executable.resolve()
    assert report.version == "9.0.2"
    assert report.executable_digest == f"sha256:{expected}"
    assert report.reason is None
    assert runner.calls == [((str(executable.resolve()), "--version"), tmp_path, 5)]


def test_probe_reports_missing_executable() -> None:
    report = KicadCli(VersionRunner(), None, 5).probe()

    assert not report.available
    assert report.reason == "kicad_cli_not_found"
    assert report.path is None


def test_probe_reports_nonzero_version_command(tmp_path: Path) -> None:
    executable = tmp_path / "kicad-cli.exe"
    executable.write_bytes(b"fixture executable")
    runner = VersionRunner(
        ProcessResult((str(executable), "--version"), 2, "", "broken", False)
    )

    report = KicadCli(runner, executable, 5).probe()

    assert not report.available
    assert report.reason == "version_command_failed"


def test_probe_rejects_unsupported_major_version(tmp_path: Path) -> None:
    executable = tmp_path / "kicad-cli.exe"
    executable.write_bytes(b"fixture executable")
    runner = VersionRunner(
        ProcessResult((str(executable), "--version"), 0, "8.0.7\n", "", False)
    )

    report = KicadCli(runner, executable, 5).probe()

    assert not report.available
    assert report.version == "8.0.7"
    assert report.reason == "unsupported_version"


def test_locate_prefers_explicit_existing_path(tmp_path: Path) -> None:
    executable = tmp_path / "custom-kicad-cli.exe"
    executable.write_bytes(b"fixture")

    assert KicadCli.locate(executable) == executable.resolve()


def test_parse_kicad_report_normalizes_findings() -> None:
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "kicad" / "erc.json"

    report = parse_kicad_report("erc", fixture.read_bytes())

    assert report.kind == "erc"
    assert report.findings == (
        NormalizedFinding(
            rule_id="KICAD.ERC.PIN_NOT_CONNECTED",
            severity="error",
            subject="fixture-u1-pin7",
            message="Input pin is not driven: U1 pin 7",
        ),
    )


def test_parse_kicad_report_rejects_unknown_severity() -> None:
    payload = json.dumps(
        {
            "source": "board.kicad_pcb",
            "violations": [
                {
                    "type": "clearance",
                    "severity": "catastrophic",
                    "description": "bad",
                    "items": [],
                }
            ],
        }
    ).encode()

    with pytest.raises(KicadReportFormatError, match="severity"):
        parse_kicad_report("drc", payload)


def test_parse_kicad_report_rejects_invalid_json() -> None:
    with pytest.raises(KicadReportFormatError, match="JSON"):
        parse_kicad_report("erc", b"not-json")


class ReportRunner:
    def __init__(self, reports: dict[str, bytes]) -> None:
        self.reports = reports
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv, cwd, timeout_seconds):
        call = tuple(str(value) for value in argv)
        self.calls.append(call)
        if call[-1] == "--version":
            return ProcessResult(call, 0, "9.0.2\n", "", False)
        kind = call[1]
        output = Path(call[call.index("--output") + 1])
        output.write_bytes(self.reports["erc" if kind == "sch" else "drc"])
        return ProcessResult(call, 0, "", "", False)


def test_validate_builds_exact_read_only_commands(tmp_path: Path) -> None:
    fixture_dir = Path(__file__).resolve().parents[1] / "fixtures" / "kicad"
    runner = ReportRunner(
        {
            "erc": (fixture_dir / "erc.json").read_bytes(),
            "drc": (fixture_dir / "drc.json").read_bytes(),
        }
    )
    executable = tmp_path / "kicad-cli.exe"
    executable.write_bytes(b"fixture executable")
    project = tmp_path / "project"
    output = tmp_path / "output"
    project.mkdir()
    schematic = project / "board.kicad_sch"
    board = project / "board.kicad_pcb"
    schematic.write_text("fixture", encoding="utf-8")
    board.write_text("fixture", encoding="utf-8")

    reports = KicadCli(runner, executable, 5).validate(project, output)

    assert [report.kind for report in reports] == ["erc", "drc"]
    assert runner.calls[1:] == [
        (
            str(executable.resolve()),
            "sch",
            "erc",
            "--format",
            "json",
            "--output",
            str(output.resolve() / "erc.json"),
            str(schematic.resolve()),
        ),
        (
            str(executable.resolve()),
            "pcb",
            "drc",
            "--format",
            "json",
            "--output",
            str(output.resolve() / "drc.json"),
            str(board.resolve()),
        ),
    ]
    assert reports[0].tool_version == "9.0.2"


def test_validate_rejects_multiple_root_schematics(tmp_path: Path) -> None:
    executable = tmp_path / "kicad-cli.exe"
    executable.write_bytes(b"fixture")
    project = tmp_path / "project"
    project.mkdir()
    (project / "one.kicad_sch").write_text("fixture", encoding="utf-8")
    (project / "two.kicad_sch").write_text("fixture", encoding="utf-8")

    with pytest.raises(AmbiguousKicadProjectError):
        KicadCli(VersionRunner(), executable, 5).validate(
            project, tmp_path / "output"
        )


def test_validate_rejects_output_inside_source(tmp_path: Path) -> None:
    executable = tmp_path / "kicad-cli.exe"
    executable.write_bytes(b"fixture")
    project = tmp_path / "project"
    project.mkdir()
    (project / "board.kicad_sch").write_text("fixture", encoding="utf-8")

    with pytest.raises(ValueError, match="outside"):
        KicadCli(VersionRunner(), executable, 5).validate(
            project, project / "evidence"
        )

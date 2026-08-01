import hashlib
import json
from pathlib import Path

import pytest

from pcbflow.domain import NormalizedFinding
from pcbflow.kicad import (
    AmbiguousKicadProjectError,
    KicadCli,
    KicadDesignFormatError,
    KicadProjectNotFoundError,
    KicadReportFormatError,
    KicadToolError,
    KicadUnavailableError,
    parse_kicad_report,
)
from pcbflow.process import ProcessResult
from pcbflow.process import ProcessTimeoutError


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


def test_probe_populates_kicad_10_compatibility_profile(tmp_path: Path) -> None:
    executable = tmp_path / "kicad-cli.exe"
    executable.write_bytes(b"fixture executable")
    runner = VersionRunner(
        ProcessResult((str(executable), "--version"), 0, "10.0.4\n", "", False)
    )

    report = KicadCli(runner, executable, 5).probe()

    assert report.available
    assert report.version == "10.0.4"
    assert report.major == 10
    assert report.profile_id == "kicad-10-v1"
    assert report.profile_revision == 1


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


def test_probe_reports_timeout_and_runner_oserror(tmp_path: Path) -> None:
    executable = tmp_path / "kicad-cli.exe"
    executable.write_bytes(b"fixture executable")

    class TimeoutRunner:
        def run(self, argv, cwd, timeout_seconds):
            raise ProcessTimeoutError(argv, timeout_seconds)

    timeout = KicadCli(TimeoutRunner(), executable, 5).probe()
    assert timeout.reason == "version_command_timeout"

    class OSErrorRunner:
        def run(self, argv, cwd, timeout_seconds):
            raise OSError("cannot execute")

    failed = KicadCli(OSErrorRunner(), executable, 5).probe()
    assert failed.reason == "executable_read_failed"


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


def test_locate_selects_highest_registered_numeric_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    program_files = tmp_path / "Program Files"
    local_app_data = tmp_path / "Local"
    for root, versions in ((program_files, ("9.0", "11.0")), (local_app_data / "Programs", ("10.0",))):
        for version in versions:
            executable = root / "KiCad" / version / "bin" / "kicad-cli.exe"
            executable.parent.mkdir(parents=True, exist_ok=True)
            executable.write_bytes(version.encode())
    monkeypatch.setattr("pcbflow.kicad.os.name", "nt")
    monkeypatch.setenv("ProgramFiles", str(program_files))
    monkeypatch.setenv("LocalAppData", str(local_app_data))
    monkeypatch.setattr("pcbflow.kicad.shutil.which", lambda _: None)

    selected = KicadCli.locate()

    assert selected == (local_app_data / "Programs" / "KiCad" / "10.0" / "bin" / "kicad-cli.exe").resolve()


def test_locate_skips_unknown_numeric_path_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unknown = tmp_path / "path" / "KiCad" / "11.0" / "bin" / "kicad-cli.exe"
    unknown.parent.mkdir(parents=True)
    unknown.write_bytes(b"unknown")
    known_root = tmp_path / "Program Files"
    known = known_root / "KiCad" / "10.0" / "bin" / "kicad-cli.exe"
    known.parent.mkdir(parents=True)
    known.write_bytes(b"known")
    monkeypatch.setattr("pcbflow.kicad.os.name", "nt")
    monkeypatch.setenv("ProgramFiles", str(known_root))
    monkeypatch.setenv("LocalAppData", str(tmp_path / "Local"))
    monkeypatch.setattr("pcbflow.kicad.shutil.which", lambda _: str(unknown))

    assert KicadCli.locate() == known.resolve()


def test_install_version_parser_handles_numeric_and_invalid_paths(tmp_path: Path) -> None:
    assert KicadCli._install_version(
        tmp_path / "KiCad" / "10" / "bin" / "kicad-cli.exe"
    ) == (10, 0)
    assert KicadCli._install_version(
        tmp_path / "KiCad" / "bad" / "bin" / "kicad-cli.exe"
    ) is None


@pytest.mark.parametrize(
    "payload",
    [
        b"not-cst",
        b"(other (version 20250114))",
        b"(kicad_sch (version))",
        b"(kicad_sch (version bad))",
        b"(kicad_sch (version 20250114) (version 20250114))",
    ],
)
def test_validate_rejects_malformed_design_format(tmp_path: Path, payload: bytes) -> None:
    path = tmp_path / "board.kicad_sch"
    path.write_bytes(payload)

    with pytest.raises(Exception) as caught:
        KicadCli._validate_design_format(
            path, frozenset((20250114,)), "kicad_sch", "kicad-9-v1"
        )

    assert getattr(caught.value, "code", None) == "KICAD_FILE_FORMAT_UNSUPPORTED"


def test_validate_rejects_file_as_project(tmp_path: Path) -> None:
    executable = tmp_path / "kicad-cli.exe"
    executable.write_bytes(b"fixture")
    project = tmp_path / "not-a-directory"
    project.write_text("file", encoding="utf-8")

    with pytest.raises(KicadProjectNotFoundError, match="directory"):
        KicadCli(VersionRunner(), executable, 5).validate(
            project, tmp_path / "output"
        )


def test_validate_rejects_multiple_root_boards(tmp_path: Path) -> None:
    executable = tmp_path / "kicad-cli.exe"
    executable.write_bytes(b"fixture")
    project = tmp_path / "project"
    project.mkdir()
    (project / "one.kicad_pcb").write_text(
        "(kicad_pcb (version 20241229))", encoding="utf-8"
    )
    (project / "two.kicad_pcb").write_text(
        "(kicad_pcb (version 20241229))", encoding="utf-8"
    )

    with pytest.raises(AmbiguousKicadProjectError):
        KicadCli(VersionRunner(), executable, 5).validate(
            project, tmp_path / "output"
        )


def test_validate_reports_tool_failure_and_missing_report(tmp_path: Path) -> None:
    executable = tmp_path / "kicad-cli.exe"
    executable.write_bytes(b"fixture")
    project = tmp_path / "project"
    project.mkdir()
    (project / "board.kicad_pcb").write_text(
        "(kicad_pcb (version 20241229))", encoding="utf-8"
    )

    class FailingRunner(ReportRunner):
        def run(self, argv, cwd, timeout_seconds):
            call = tuple(str(value) for value in argv)
            self.calls.append(call)
            if call[-1] == "--version":
                return ProcessResult(call, 0, "10.0.4\n", "", False)
            return ProcessResult(call, 2, "", "failed", False)

    with pytest.raises(KicadToolError) as failed:
        KicadCli(FailingRunner({}), executable, 5).validate(
            project, tmp_path / "output"
        )
    assert failed.value.returncode == 2
    assert failed.value.stderr == "failed"

    class MissingReportRunner(ReportRunner):
        def run(self, argv, cwd, timeout_seconds):
            call = tuple(str(value) for value in argv)
            self.calls.append(call)
            if call[-1] == "--version":
                return ProcessResult(call, 0, "10.0.4\n", "", False)
            return ProcessResult(call, 0, "", "", False)

    missing_output = tmp_path / "output-missing"
    missing_output.mkdir()
    stale_report = missing_output / "drc.json"
    stale_report.write_text("stale report", encoding="utf-8")
    with pytest.raises(KicadToolError) as caught:
        KicadCli(MissingReportRunner({}), executable, 5).validate(
            project, missing_output
        )
    assert caught.value.stderr == "report file was not created"
    assert not stale_report.exists()


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


def test_parse_kicad_report_normalizes_sheet_scoped_erc_findings() -> None:
    payload = json.dumps(
        {
            "source": "board.kicad_sch",
            "sheets": [
                {
                    "path": "/",
                    "uuid_path": "/fixture-sheet",
                    "violations": [
                        {
                            "type": "pin not connected",
                            "severity": "warning",
                            "description": "Pin is not connected",
                            "items": [
                                {
                                    "uuid": "fixture-pin",
                                    "description": "U1 pin 1",
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    ).encode()

    report = parse_kicad_report("erc", payload)

    assert report.findings == (
        NormalizedFinding(
            rule_id="KICAD.ERC.PIN_NOT_CONNECTED",
            severity="warning",
            subject="fixture-pin",
            message="Pin is not connected: U1 pin 1",
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
    schematic.write_text(
        '(kicad_sch (version 20250114) (uuid "00000000-0000-0000-0000-000000000001"))',
        encoding="utf-8",
    )
    board.write_text('(kicad_pcb (version 20241229))', encoding="utf-8")

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
    assert reports[0].profile_id == "kicad-9-v1"


def test_validate_rejects_an_executable_replaced_during_version_probe(
    tmp_path: Path,
) -> None:
    fixture_dir = Path(__file__).resolve().parents[1] / "fixtures" / "kicad"
    executable = tmp_path / "kicad-cli.exe"
    executable.write_bytes(b"original executable")
    project = tmp_path / "project"
    project.mkdir()
    (project / "board.kicad_pcb").write_text(
        "(kicad_pcb (version 20241229))", encoding="utf-8"
    )

    class ReplacingVersionRunner(ReportRunner):
        def run(self, argv, cwd, timeout_seconds):
            call = tuple(str(value) for value in argv)
            if call[-1] == "--version":
                self.calls.append(call)
                executable.write_bytes(b"replacement executable")
                return ProcessResult(call, 0, "9.0.2\n", "", False)
            return super().run(argv, cwd, timeout_seconds)

    runner = ReplacingVersionRunner(
        {"erc": (fixture_dir / "erc.json").read_bytes(), "drc": (fixture_dir / "drc.json").read_bytes()}
    )
    with pytest.raises(KicadUnavailableError, match="executable_changed"):
        KicadCli(runner, executable, 5).validate(project, tmp_path / "output")

    assert runner.calls == [(str(executable.resolve()), "--version")]


def test_validate_rejects_an_executable_replaced_during_validation(
    tmp_path: Path,
) -> None:
    fixture_dir = Path(__file__).resolve().parents[1] / "fixtures" / "kicad"
    executable = tmp_path / "kicad-cli.exe"
    executable.write_bytes(b"original executable")
    project = tmp_path / "project"
    project.mkdir()
    (project / "board.kicad_pcb").write_text(
        "(kicad_pcb (version 20241229))", encoding="utf-8"
    )

    class ReplacingValidationRunner(ReportRunner):
        def run(self, argv, cwd, timeout_seconds):
            result = super().run(argv, cwd, timeout_seconds)
            if tuple(str(value) for value in argv)[-1] != "--version":
                executable.write_bytes(b"replacement executable")
            return result

    runner = ReplacingValidationRunner(
        {"erc": (fixture_dir / "erc.json").read_bytes(), "drc": (fixture_dir / "drc.json").read_bytes()}
    )
    with pytest.raises(KicadUnavailableError, match="executable_changed"):
        KicadCli(runner, executable, 5).validate(project, tmp_path / "output")

    assert len(runner.calls) == 2


def test_validate_rejects_a_format_outside_the_selected_profile(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "kicad-cli.exe"
    executable.write_bytes(b"fixture executable")
    project = tmp_path / "project"
    project.mkdir()
    schematic = project / "board.kicad_sch"
    schematic.write_text(
        '(kicad_sch (version 20990101) (uuid "00000000-0000-0000-0000-000000000001"))',
        encoding="utf-8",
    )

    with pytest.raises(KicadDesignFormatError) as caught:
        KicadCli(
            VersionRunner(
                ProcessResult((str(executable), "--version"), 0, "10.0.4\n", "", False)
            ),
            executable,
            5,
        ).validate(project, tmp_path / "output")

    assert caught.value.code == "KICAD_FILE_FORMAT_UNSUPPORTED"


def test_kicad_10_profile_builds_the_verified_erc_command(tmp_path: Path) -> None:
    from pcbflow.kicad_compatibility import select_kicad_profile

    profile = select_kicad_profile("10.0.4")
    assert profile is not None
    argv = profile.validation_argv(
        executable=tmp_path / "kicad-cli.exe",
        kind="erc",
        design_file=tmp_path / "board.kicad_sch",
        report_file=tmp_path / "erc.json",
    )

    assert argv[1:] == (
        "sch",
        "erc",
        "--format",
        "json",
        "--output",
        str(tmp_path / "erc.json"),
        str(tmp_path / "board.kicad_sch"),
    )


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

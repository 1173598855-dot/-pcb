import hashlib
from pathlib import Path

from pcbflow.kicad import KicadCli
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


def test_locate_prefers_explicit_existing_path(tmp_path: Path) -> None:
    executable = tmp_path / "custom-kicad-cli.exe"
    executable.write_bytes(b"fixture")

    assert KicadCli.locate(executable) == executable.resolve()

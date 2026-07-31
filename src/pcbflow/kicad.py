from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from pcbflow.domain import NormalizedFinding, ValidationReport
from pcbflow.process import ProcessPort, ProcessTimeoutError

_VERSION = re.compile(r"(?<!\d)(\d+\.\d+(?:\.\d+)?)(?!\d)")


@dataclass(frozen=True, slots=True)
class KicadCapability:
    available: bool
    path: Path | None
    version: str | None
    executable_digest: str | None
    reason: str | None


@dataclass(frozen=True, slots=True)
class RawValidationReport:
    kind: Literal["erc", "drc"]
    data: bytes
    argv: tuple[str, ...]
    returncode: int
    tool_version: str


class KicadPort(Protocol):
    def probe(self) -> KicadCapability: ...

    def validate(
        self, project_dir: Path, output_dir: Path
    ) -> tuple[RawValidationReport, ...]: ...


class KicadReportFormatError(ValueError):
    pass


class AmbiguousKicadProjectError(ValueError):
    pass


class KicadProjectNotFoundError(ValueError):
    pass


class KicadUnavailableError(RuntimeError):
    pass


class KicadToolError(RuntimeError):
    def __init__(self, kind: str, returncode: int, stderr: str) -> None:
        super().__init__(f"kicad {kind} failed with exit code {returncode}")
        self.kind = kind
        self.returncode = returncode
        self.stderr = stderr


def parse_kicad_report(
    kind: Literal["erc", "drc"], data: bytes
) -> ValidationReport:
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise KicadReportFormatError("report is not valid UTF-8 JSON") from error
    if not isinstance(document, dict):
        raise KicadReportFormatError("report JSON must be an object")
    violations = document.get("violations")
    if not isinstance(violations, list):
        raise KicadReportFormatError("report violations must be an array")

    source = document.get("source", "unknown")
    if not isinstance(source, str):
        raise KicadReportFormatError("report source must be a string")
    severity_map = {
        "error": "error",
        "warning": "warning",
        "exclusion": "info",
        "ignored": "info",
        "info": "info",
    }
    findings: list[NormalizedFinding] = []
    for violation in violations:
        if not isinstance(violation, dict):
            raise KicadReportFormatError("violation must be an object")
        violation_type = violation.get("type")
        severity = violation.get("severity")
        description = violation.get("description")
        items = violation.get("items", [])
        if not isinstance(violation_type, str) or not violation_type:
            raise KicadReportFormatError("violation type must be a string")
        if not isinstance(severity, str) or severity.lower() not in severity_map:
            raise KicadReportFormatError("unknown violation severity")
        if not isinstance(description, str) or not description:
            raise KicadReportFormatError("violation description must be a string")
        if not isinstance(items, list):
            raise KicadReportFormatError("violation items must be an array")

        subject = source
        item_descriptions: list[str] = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise KicadReportFormatError("violation item must be an object")
            item_description = item.get("description")
            if isinstance(item_description, str) and item_description:
                item_descriptions.append(item_description)
            item_uuid = item.get("uuid")
            if index == 0 and isinstance(item_uuid, str) and item_uuid:
                subject = item_uuid

        normalized_type = re.sub(r"[^A-Za-z0-9]+", "_", violation_type)
        normalized_type = normalized_type.strip("_").upper()
        if not normalized_type:
            raise KicadReportFormatError("violation type has no identifier")
        message = description
        if item_descriptions:
            message = f"{description}: {'; '.join(item_descriptions)}"
        findings.append(
            NormalizedFinding(
                rule_id=f"KICAD.{kind.upper()}.{normalized_type}",
                severity=severity_map[severity.lower()],
                subject=subject,
                message=message,
            )
        )
    return ValidationReport(kind=kind, findings=tuple(findings))


class KicadCli:
    def __init__(
        self,
        runner: ProcessPort,
        executable: Path | None,
        timeout_seconds: float,
    ) -> None:
        self._runner = runner
        self._executable = executable.resolve() if executable is not None else None
        self._timeout_seconds = timeout_seconds

    @staticmethod
    def locate(configured: Path | None = None) -> Path | None:
        if configured is not None:
            candidate = configured.resolve()
            return candidate if candidate.is_file() else None

        from_path = shutil.which("kicad-cli")
        if from_path:
            candidate = Path(from_path).resolve()
            if candidate.is_file():
                return candidate

        if os.name == "nt":
            program_files = Path(os.environ.get("ProgramFiles", "C:/Program Files"))
            candidates = sorted(
                program_files.glob("KiCad/*/bin/kicad-cli.exe"),
                key=lambda path: path.parts[-3],
                reverse=True,
            )
            for candidate in candidates:
                if candidate.is_file():
                    return candidate.resolve()
        return None

    def probe(self) -> KicadCapability:
        if self._executable is None or not self._executable.is_file():
            return KicadCapability(False, None, None, None, "kicad_cli_not_found")

        try:
            digest = self._hash_executable(self._executable)
            result = self._runner.run(
                [str(self._executable), "--version"],
                self._executable.parent,
                self._timeout_seconds,
            )
        except ProcessTimeoutError:
            return KicadCapability(
                False, self._executable, None, None, "version_command_timeout"
            )
        except OSError:
            return KicadCapability(
                False, self._executable, None, None, "executable_read_failed"
            )

        if result.returncode != 0:
            return KicadCapability(
                False, self._executable, None, digest, "version_command_failed"
            )
        match = _VERSION.search(result.stdout)
        if match is None:
            return KicadCapability(
                False, self._executable, None, digest, "version_unparseable"
            )
        version = match.group(1)
        if version.split(".", 1)[0] != "9":
            return KicadCapability(
                False, self._executable, version, digest, "unsupported_version"
            )
        return KicadCapability(
            True, self._executable, version, digest, None
        )

    def validate(
        self, project_dir: Path, output_dir: Path
    ) -> tuple[RawValidationReport, ...]:
        try:
            project = project_dir.resolve(strict=True)
        except FileNotFoundError as error:
            raise KicadProjectNotFoundError(
                "KiCad project path does not exist"
            ) from error
        if not project.is_dir():
            raise KicadProjectNotFoundError("KiCad project path must be a directory")
        output = output_dir.resolve()
        if output.is_relative_to(project):
            raise ValueError("validation output must be outside the source project")

        schematics = sorted(project.glob("*.kicad_sch"))
        boards = sorted(project.glob("*.kicad_pcb"))
        if len(schematics) > 1:
            raise AmbiguousKicadProjectError("multiple root schematics found")
        if len(boards) > 1:
            raise AmbiguousKicadProjectError("multiple root boards found")
        if not schematics and not boards:
            raise KicadProjectNotFoundError("no root KiCad design files found")

        capability = self.probe()
        if not capability.available or capability.path is None or capability.version is None:
            raise KicadUnavailableError(capability.reason or "kicad_cli_unavailable")
        output.mkdir(parents=True, exist_ok=True)

        reports: list[RawValidationReport] = []
        if schematics:
            reports.append(
                self._run_validation(
                    "erc", "sch", schematics[0], output / "erc.json", capability
                )
            )
        if boards:
            reports.append(
                self._run_validation(
                    "drc", "pcb", boards[0], output / "drc.json", capability
                )
            )
        return tuple(reports)

    def _run_validation(
        self,
        kind: Literal["erc", "drc"],
        command_group: Literal["sch", "pcb"],
        design_file: Path,
        report_file: Path,
        capability: KicadCapability,
    ) -> RawValidationReport:
        assert capability.path is not None
        assert capability.version is not None
        if report_file.exists():
            report_file.unlink()
        argv = (
            str(capability.path),
            command_group,
            kind,
            "--format",
            "json",
            "--output",
            str(report_file),
            str(design_file.resolve()),
        )
        result = self._runner.run(argv, report_file.parent, self._timeout_seconds)
        if result.returncode != 0:
            raise KicadToolError(kind, result.returncode, result.stderr)
        if not report_file.is_file():
            raise KicadToolError(kind, result.returncode, "report file was not created")
        data = report_file.read_bytes()
        parse_kicad_report(kind, data)
        return RawValidationReport(
            kind=kind,
            data=data,
            argv=tuple(result.argv),
            returncode=result.returncode,
            tool_version=capability.version,
        )

    @staticmethod
    def _hash_executable(executable: Path) -> str:
        digest = hashlib.sha256()
        with executable.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return f"sha256:{digest.hexdigest()}"

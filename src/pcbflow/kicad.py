from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from pcbflow.canonical import hash_file
from pcbflow.domain import NormalizedFinding, ValidationReport
from pcbflow.kicad_compatibility import (
    KicadCompatibilityProfile,
    KicadOperationUnsupportedError,  # noqa: F401  re-exported for proposals
    select_kicad_profile,
)
from pcbflow.process import ProcessPort, ProcessTimeoutError
from pcbflow.schematic.cst import CstParseError, parse_cst

_VERSION = re.compile(r"(?<!\d)(\d+\.\d+(?:\.\d+)?(?:-[0-9A-Za-z.-]+)?)(?!\d)")


@dataclass(frozen=True, slots=True)
class KicadCapability:
    available: bool
    path: Path | None
    version: str | None
    executable_digest: str | None
    reason: str | None
    major: int | None = None
    profile_id: str | None = None
    profile_revision: int | None = None


@dataclass(frozen=True, slots=True)
class RawValidationReport:
    kind: Literal["erc", "drc"]
    data: bytes
    argv: tuple[str, ...]
    returncode: int
    tool_version: str
    executable_digest: str
    profile_id: str
    profile_revision: int


class KicadPort(Protocol):
    def probe(self) -> KicadCapability: ...

    def validate(
        self, project_dir: Path, output_dir: Path
    ) -> tuple[RawValidationReport, ...]: ...


@runtime_checkable
class KicadCapabilityBoundPort(Protocol):
    def validate_with_capability(
        self,
        project_dir: Path,
        output_dir: Path,
        capability: KicadCapability,
    ) -> tuple[RawValidationReport, ...]: ...


class KicadReportFormatError(ValueError):
    pass


class KicadInputLimitError(ValueError):
    def __init__(self, code: str, path: Path, limit: int) -> None:
        super().__init__(f"KiCad input exceeds {limit} bytes: {path.name}")
        self.code = code
        self.path = path
        self.limit = limit


class KicadDesignFormatError(ValueError):
    code = "KICAD_FILE_FORMAT_UNSUPPORTED"

    def __init__(self, path: Path, version: str | None, profile_id: str) -> None:
        detail = version if version is not None else "unknown"
        super().__init__(
            f"KiCad design format {detail} in {path.name} is not supported by {profile_id}"
        )
        self.path = path
        self.version = version
        self.profile_id = profile_id


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
    if "violations" in document:
        violations = document["violations"]
        if not isinstance(violations, list):
            raise KicadReportFormatError("report violations must be an array")
    else:
        sheets = document.get("sheets")
        if not isinstance(sheets, list):
            raise KicadReportFormatError("report violations must be an array")
        violations = []
        for sheet in sheets:
            if not isinstance(sheet, dict):
                raise KicadReportFormatError("report sheet must be an object")
            sheet_violations = sheet.get("violations")
            if not isinstance(sheet_violations, list):
                raise KicadReportFormatError(
                    "report sheet violations must be an array"
                )
            violations.extend(sheet_violations)

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
        *,
        max_design_file_bytes: int = 50_000_000,
        max_report_bytes: int = 10_000_000,
    ) -> None:
        self._runner = runner
        self._executable = executable.resolve() if executable is not None else None
        self._timeout_seconds = timeout_seconds
        if max_design_file_bytes <= 0:
            raise ValueError("max_design_file_bytes must be positive")
        if max_report_bytes <= 0:
            raise ValueError("max_report_bytes must be positive")
        self._max_design_file_bytes = max_design_file_bytes
        self._max_report_bytes = max_report_bytes

    @staticmethod
    def locate(configured: Path | None = None) -> Path | None:
        if configured is not None:
            try:
                candidate = configured.resolve()
                return candidate if candidate.is_file() else None
            except OSError:
                return None

        from_path = shutil.which("kicad-cli")
        if from_path:
            candidate = Path(from_path).resolve()
            install_version = KicadCli._install_version(candidate)
            path_profile_known = (
                install_version is None
                or select_kicad_profile(
                    f"{install_version[0]}.{install_version[1]}"
                )
                is not None
            )
            if candidate.is_file() and path_profile_known:
                return candidate

        if os.name == "nt":
            roots = (
                Path(os.environ.get("PROGRAMFILES", "C:/Program Files")),
                Path(os.environ.get("LOCALAPPDATA", "C:/Users/Default/AppData/Local"))
                / "Programs",
            )
            candidates: list[tuple[tuple[int, int], Path]] = []
            for root in roots:
                for candidate in root.glob("KiCad/*/bin/kicad-cli.exe"):
                    if not candidate.is_file():
                        continue
                    version_parts = KicadCli._install_version(candidate)
                    if version_parts is None:
                        continue
                    if select_kicad_profile(f"{version_parts[0]}.{version_parts[1]}") is None:
                        continue
                    candidates.append((version_parts, candidate))
            for _, candidate in sorted(candidates, key=lambda item: item[0], reverse=True):
                return candidate.resolve()
        return None

    @staticmethod
    def _install_version(path: Path) -> tuple[int, int] | None:
        name = path.parent.parent.name
        parts = name.split(".")
        if not parts or any(not part.isdecimal() for part in parts):
            return None
        if len(parts) == 1:
            return int(parts[0]), 0
        return int(parts[0]), int(parts[1])

    def probe(self) -> KicadCapability:
        if self._executable is None or not self._executable.is_file():
            return KicadCapability(False, None, None, None, "kicad_cli_not_found")

        try:
            digest = self._hash_executable(self._executable)
        except OSError:
            return KicadCapability(
                False, self._executable, None, None, "executable_read_failed"
            )
        try:
            result = self._runner.run(
                [str(self._executable), "--version"],
                self._executable.parent,
                self._timeout_seconds,
            )
        except ProcessTimeoutError:
            if not self._executable_matches(self._executable, digest):
                return KicadCapability(
                    False, self._executable, None, None, "executable_changed"
                )
            return KicadCapability(
                False, self._executable, None, None, "version_command_timeout"
            )
        except OSError:
            if not self._executable_matches(self._executable, digest):
                return KicadCapability(
                    False, self._executable, None, None, "executable_changed"
                )
            return KicadCapability(
                False, self._executable, None, None, "executable_read_failed"
            )

        if not self._executable_matches(self._executable, digest):
            return KicadCapability(
                False, self._executable, None, None, "executable_changed"
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
        profile = select_kicad_profile(version)
        if profile is None:
            return KicadCapability(
                False, self._executable, version, digest, "unsupported_version"
            )
        return KicadCapability(
            True,
            self._executable,
            version,
            digest,
            None,
            profile.major,
            profile.profile_id,
            profile.revision,
        )

    def validate(
        self,
        project_dir: Path,
        output_dir: Path,
    ) -> tuple[RawValidationReport, ...]:
        return self._validate(project_dir, output_dir)

    def validate_with_capability(
        self,
        project_dir: Path,
        output_dir: Path,
        capability: KicadCapability,
    ) -> tuple[RawValidationReport, ...]:
        return self._validate(
            project_dir,
            output_dir,
            expected_capability=capability,
        )

    def _validate(
        self,
        project_dir: Path,
        output_dir: Path,
        *,
        expected_capability: KicadCapability | None = None,
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

        if expected_capability is not None:
            if expected_capability.path != self._executable:
                raise KicadUnavailableError("expected_capability_path_mismatch")
            capability = expected_capability
        else:
            capability = self.probe()
        if (
            not capability.available
            or capability.path is None
            or capability.version is None
            or capability.executable_digest is None
            or capability.profile_id is None
            or capability.profile_revision is None
        ):
            raise KicadUnavailableError(capability.reason or "kicad_cli_unavailable")
        profile = select_kicad_profile(capability.version)
        if profile is None:
            raise KicadUnavailableError("unsupported_version")
        if expected_capability is not None and (
            capability.major != profile.major
            or capability.profile_id != profile.profile_id
            or capability.profile_revision != profile.revision
        ):
            raise KicadUnavailableError("expected_capability_profile_mismatch")
        self._validate_design_formats(schematics, boards, profile)
        output.mkdir(parents=True, exist_ok=True)

        reports: list[RawValidationReport] = []
        if schematics:
            reports.append(
                self._run_validation(
                    "erc", schematics[0], output / "erc.json", capability
                )
            )
        if boards:
            reports.append(
                self._run_validation(
                    "drc", boards[0], output / "drc.json", capability
                )
            )
        return tuple(reports)

    def _run_validation(
        self,
        kind: Literal["erc", "drc"],
        design_file: Path,
        report_file: Path,
        capability: KicadCapability,
    ) -> RawValidationReport:
        assert capability.path is not None
        assert capability.version is not None
        assert capability.executable_digest is not None
        assert capability.profile_id is not None
        assert capability.profile_revision is not None
        if report_file.exists():
            report_file.unlink()
        self._assert_executable_digest(capability.path, capability.executable_digest)
        profile = select_kicad_profile(capability.version)
        if profile is None:
            raise KicadUnavailableError("unsupported_version")
        argv = profile.validation_argv(
            executable=capability.path,
            kind=kind,
            design_file=design_file.resolve(),
            report_file=report_file,
        )
        try:
            result = self._runner.run(argv, report_file.parent, self._timeout_seconds)
        finally:
            self._assert_executable_digest(
                capability.path, capability.executable_digest
            )
        if result.returncode != 0:
            raise KicadToolError(kind, result.returncode, result.stderr)
        if not report_file.is_file():
            raise KicadToolError(kind, result.returncode, "report file was not created")
        data = self._read_bounded(
            report_file,
            self._max_report_bytes,
            "KICAD_REPORT_LIMIT_EXCEEDED",
        )
        parse_kicad_report(kind, data)
        return RawValidationReport(
            kind=kind,
            data=data,
            argv=tuple(result.argv),
            returncode=result.returncode,
            tool_version=capability.version,
            executable_digest=capability.executable_digest,
            profile_id=capability.profile_id,
            profile_revision=capability.profile_revision,
        )

    def _validate_design_formats(
        self,
        schematics: list[Path],
        boards: list[Path],
        profile: KicadCompatibilityProfile,
    ) -> None:
        for path in schematics:
            KicadCli._validate_design_format(
                path,
                profile.schematic_format_versions,
                "kicad_sch",
                profile.profile_id,
                max_design_file_bytes=self._max_design_file_bytes,
            )
        for path in boards:
            KicadCli._validate_design_format(
                path,
                profile.pcb_format_versions,
                "kicad_pcb",
                profile.profile_id,
                max_design_file_bytes=self._max_design_file_bytes,
            )

    @staticmethod
    def _validate_design_format(
        path: Path,
        accepted: frozenset[int],
        expected_head: str,
        profile_id: str,
        *,
        max_design_file_bytes: int = 50_000_000,
    ) -> None:
        try:
            document = parse_cst(
                KicadCli._read_bounded(
                    path,
                    max_design_file_bytes,
                    "KICAD_DESIGN_FILE_LIMIT_EXCEEDED",
                )
            )
        except KicadInputLimitError:
            raise
        except (OSError, CstParseError) as error:
            raise KicadDesignFormatError(path, None, profile_id) from error
        if document.root.head != expected_head:
            raise KicadDesignFormatError(path, None, profile_id)
        versions = document.root.find_children("version")
        if len(versions) != 1:
            raise KicadDesignFormatError(path, None, profile_id)
        try:
            version = int(versions[0].atom_text(1))
        except (IndexError, ValueError, CstParseError) as error:
            raise KicadDesignFormatError(path, None, profile_id) from error
        if version not in accepted:
            raise KicadDesignFormatError(path, str(version), profile_id)

    @staticmethod
    def _read_bounded(path: Path, limit: int, code: str) -> bytes:
        if path.stat().st_size > limit:
            raise KicadInputLimitError(code, path, limit)
        with path.open("rb") as stream:
            chunks: list[bytes] = []
            remaining = limit + 1
            while remaining:
                chunk = stream.read(remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > limit:
            raise KicadInputLimitError(code, path, limit)
        return data

    @staticmethod
    def _hash_executable(executable: Path) -> str:
        return hash_file(executable)

    @staticmethod
    def _executable_matches(executable: Path, expected_digest: str) -> bool:
        try:
            return KicadCli._hash_executable(executable) == expected_digest
        except OSError:
            return False

    @staticmethod
    def _assert_executable_digest(executable: Path, expected_digest: str) -> None:
        if not KicadCli._executable_matches(executable, expected_digest):
            raise KicadUnavailableError("executable_changed")

"""Package a real KiCad export into the frozen release artifact set.

``PcbReleaseTaskHandler`` expects ``ReleaseArtifacts.files`` to contain exactly
the five native kinds ``gerber``, ``drill``, ``bom``, ``cpl`` and ``assembly``,
each pointing at a single file. A real KiCad export instead produces a directory
of Gerbers, a directory of drill files, a position CSV, a BOM CSV and a DRC
report. This module bridges that gap without touching the design:

- ``gerber``    -> a ZIP of every Gerber and the job file
- ``drill``     -> a ZIP of every Excellon drill file
- ``bom``       -> the schematic BOM CSV
- ``cpl``       -> the position/CPL CSV
- ``assembly``  -> a ZIP of the manufacturing evidence (DRC report + provenance)

Everything is written into the caller's workspace, and the DRC report is parsed
with the same normalizer used for validation so a blocking finding fails the
package before it can be published.
"""

from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass
from pathlib import Path

from pcbflow.board.adapter import ReleaseArtifacts
from pcbflow.domain import ValidationReport
from pcbflow.kicad import parse_kicad_report
from pcbflow.kicad_export import KicadExportResult


class ReleasePackageError(RuntimeError):
    """The real export could not be shaped into a release artifact set."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


_BLOCKING_SEVERITIES = frozenset({"error", "critical", "fatal", "blocker"})
_NATIVE_KINDS = ("gerber", "drill", "bom", "cpl", "assembly")


@dataclass(frozen=True, slots=True)
class PackagedRelease:
    """The five native release files plus the normalized DRC report."""

    artifacts: ReleaseArtifacts
    drc_report: ValidationReport


def _zip_bytes(entries: tuple[tuple[str, bytes], ...]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries:
            info = zipfile.ZipInfo(name)
            info.date_time = (1980, 1, 1, 0, 0, 0)
            archive.writestr(info, payload)
    return buffer.getvalue()


def package_kicad_release(
    export: KicadExportResult,
    output_dir: Path,
    *,
    candidate_digest: str,
) -> PackagedRelease:
    """Shape a :class:`KicadExportResult` into the frozen release artifact set."""
    output = output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)

    drc_data = export.drc_report.read_bytes()
    drc_report = parse_kicad_report("drc", drc_data)
    blocking = tuple(
        finding
        for finding in drc_report.findings
        if finding.severity.casefold() in _BLOCKING_SEVERITIES
    )
    if blocking:
        raise ReleasePackageError(
            "PCB_NATIVE_DRC_BLOCKED",
            f"native DRC reported {len(blocking)} blocking finding(s)",
        )

    gerber_entries = tuple(
        (path.name, path.read_bytes()) for path in export.gerber_files
    )
    drill_entries = tuple(
        (path.name, path.read_bytes()) for path in export.drill_files
    )
    if not gerber_entries:
        raise ReleasePackageError("PCB_RELEASE_EXPORT_INVALID", "no Gerber output")
    if not drill_entries:
        raise ReleasePackageError("PCB_RELEASE_EXPORT_INVALID", "no drill output")

    if export.bom_file is None or not export.bom_file.is_file():
        raise ReleasePackageError("MANUFACTURING_ARTIFACT_MISSING", "BOM is missing")
    if export.position_file is None or not export.position_file.is_file():
        raise ReleasePackageError("MANUFACTURING_ARTIFACT_MISSING", "CPL is missing")

    provenance = json.dumps(
        {
            "schema_version": "1.0",
            "candidate_digest": candidate_digest,
            "gerber_files": [name for name, _ in gerber_entries],
            "drill_files": [name for name, _ in drill_entries],
            "drc_finding_count": len(drc_report.findings),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    assembly_entries = (
        ("drc.json", drc_data),
        ("provenance.json", provenance),
    )

    files: dict[str, Path] = {}
    targets = {
        "gerber": ("gerbers.zip", _zip_bytes(gerber_entries)),
        "drill": ("drill.zip", _zip_bytes(drill_entries)),
        "bom": ("bom.csv", export.bom_file.read_bytes()),
        "cpl": ("cpl.csv", export.position_file.read_bytes()),
        "assembly": ("assembly.zip", _zip_bytes(assembly_entries)),
    }
    for kind, (name, payload) in targets.items():
        target = output / name
        target.write_bytes(payload)
        files[kind] = target

    ordered = tuple((kind, files[kind]) for kind in _NATIVE_KINDS)
    return PackagedRelease(
        artifacts=ReleaseArtifacts(files=ordered),
        drc_report=drc_report,
    )


__all__ = [
    "PackagedRelease",
    "ReleasePackageError",
    "package_kicad_release",
]

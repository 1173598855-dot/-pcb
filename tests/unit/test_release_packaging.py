"""Tests for packaging a real KiCad export into the frozen release set."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from pcbflow.artifacts import ContentAddressedStore
from pcbflow.kicad_export import KicadExportResult
from pcbflow.release_packaging import (
    ReleasePackageError,
    package_kicad_release,
)

_EMPTY_DRC = json.dumps(
    {"violations": [], "unconnected_items": [], "schematic_parity": []}
).encode("utf-8")


def _clean_drc(severity: str = "warning") -> bytes:
    return json.dumps(
        {
            "violations": [
                {
                    "type": "silk_overlap",
                    "severity": severity,
                    "description": "Silkscreen overlap",
                    "items": [],
                }
            ]
        }
    ).encode("utf-8")


def _fake_export(root: Path, drc: bytes = _EMPTY_DRC, *, with_bom: bool = True, with_pos: bool = True) -> KicadExportResult:
    gerbers = root / "gerbers"
    drill = root / "drill"
    gerbers.mkdir(parents=True, exist_ok=True)
    drill.mkdir(parents=True, exist_ok=True)
    (gerbers / "board-F_Cu.gtl").write_bytes(b"G04 front copper*\nM02*\n")
    (gerbers / "board-B_Cu.gbl").write_bytes(b"G04 back copper*\nM02*\n")
    (gerbers / "board-job.gbrjob").write_text("{}", encoding="utf-8")
    (drill / "board.drl").write_bytes(b"M48\nM30\n")
    drc_path = root / "drc.json"
    drc_path.write_bytes(drc)
    bom = root / "bom.csv"
    bom.write_text("Designator,Value\nR1,10k\n", encoding="utf-8")
    pos = root / "positions.csv"
    pos.write_text("Ref,X,Y\nR1,1,2\n", encoding="utf-8")
    return KicadExportResult(
        gerber_dir=gerbers,
        gerber_files=tuple(sorted(gerbers.iterdir())),
        drill_files=tuple(sorted(drill.iterdir())),
        position_file=pos if with_pos else None,
        bom_file=bom if with_bom else None,
        drc_report=drc_path,
    )


def test_packages_the_five_native_kinds_and_keeps_drc_readable(tmp_path: Path) -> None:
    export = _fake_export(tmp_path / "export", drc=_clean_drc())

    package = package_kicad_release(
        export, tmp_path / "release", candidate_digest="sha256:" + "a" * 64
    )

    kinds = tuple(kind for kind, _ in package.artifacts.files)
    assert kinds == ("gerber", "drill", "bom", "cpl", "assembly")
    assert package.drc_report.findings[0].severity == "warning"
    for kind, path in package.artifacts.files:
        assert path.is_file() and path.stat().st_size > 0
        if kind in {"gerber", "drill", "assembly"}:
            with zipfile.ZipFile(path) as archive:
                assert archive.namelist()


def test_gerber_archive_contains_every_gerber_file(tmp_path: Path) -> None:
    export = _fake_export(tmp_path / "export")

    package = package_kicad_release(
        export, tmp_path / "release", candidate_digest="sha256:" + "b" * 64
    )

    gerber_zip = dict(package.artifacts.files)["gerber"]
    with zipfile.ZipFile(gerber_zip) as archive:
        names = set(archive.namelist())
    assert {"board-F_Cu.gtl", "board-B_Cu.gbl", "board-job.gbrjob"} <= names


def test_blocking_drc_fails_the_package(tmp_path: Path) -> None:
    export = _fake_export(tmp_path / "export", drc=_clean_drc("error"))

    with pytest.raises(ReleasePackageError) as raised:
        package_kicad_release(
            export, tmp_path / "release", candidate_digest="sha256:" + "c" * 64
        )

    assert raised.value.code == "PCB_NATIVE_DRC_BLOCKED"
    assert not (tmp_path / "release" / "gerbers.zip").exists()


def test_missing_bom_and_cpl_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(ReleasePackageError, match="BOM"):
        package_kicad_release(
            _fake_export(tmp_path / "a", with_bom=False),
            tmp_path / "ra",
            candidate_digest="sha256:" + "d" * 64,
        )
    with pytest.raises(ReleasePackageError, match="CPL"):
        package_kicad_release(
            _fake_export(tmp_path / "b", with_pos=False),
            tmp_path / "rb",
            candidate_digest="sha256:" + "e" * 64,
        )


def test_packaged_files_publish_into_content_addressed_store(tmp_path: Path) -> None:
    export = _fake_export(tmp_path / "export")
    package = package_kicad_release(
        export, tmp_path / "release", candidate_digest="sha256:" + "f" * 64
    )
    store = ContentAddressedStore(tmp_path / "store")

    for _kind, path in package.artifacts.files:
        descriptor = store.put_bytes(path.read_bytes(), "application/octet-stream")
        assert store.verify(descriptor.digest)
        assert descriptor.size == path.stat().st_size


def test_assembly_archive_carries_drc_and_provenance(tmp_path: Path) -> None:
    export = _fake_export(tmp_path / "export")
    package = package_kicad_release(
        export, tmp_path / "release", candidate_digest="sha256:" + "1" * 64
    )
    assembly = dict(package.artifacts.files)["assembly"]
    with zipfile.ZipFile(assembly) as archive:
        names = set(archive.namelist())
        provenance = json.loads(archive.read("provenance.json"))
    assert names == {"drc.json", "provenance.json"}
    assert provenance["candidate_digest"] == "sha256:" + "1" * 64
    assert "board-F_Cu.gtl" in provenance["gerber_files"]

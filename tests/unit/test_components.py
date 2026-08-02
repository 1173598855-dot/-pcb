from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from pcbflow.artifacts import ContentAddressedStore
from pcbflow.components import (
    ComponentRevisionService,
    ComponentRevision,
    component_manifest_digest,
    load_component_manifest,
)
from tests.component_fixtures import build_component_directory, component_yaml


class _NeverCalledStore:
    def create(self, **_kwargs):
        pytest.fail("component store must not be called after import failure")


def _component_service(
    tmp_path: Path,
) -> tuple[ComponentRevisionService, ContentAddressedStore]:
    artifacts = ContentAddressedStore(tmp_path / "artifacts")
    return (
        ComponentRevisionService(
            _NeverCalledStore(), artifacts, max_bytes=1_000_000
        ),
        artifacts,
    )


def test_load_component_manifest_accepts_verified_canonical_input() -> None:
    manifest = load_component_manifest(component_yaml())
    assert manifest.component_key == "Acme:LED-0603-RED"
    assert manifest.status == "verified"
    assert component_manifest_digest(manifest).startswith("sha256:")


@pytest.mark.parametrize(
    "replacement",
    [b"status: draft", b"path: ../datasheet.pdf", b"media_type: text/plain"],
)
def test_load_component_manifest_rejects_unverified_or_unsafe_assets(
    replacement: bytes,
) -> None:
    with pytest.raises(ValueError):
        if replacement == b"status: draft":
            data = component_yaml().replace(b"status: verified", replacement)
        elif replacement == b"path: ../datasheet.pdf":
            data = component_yaml().replace(b"path: datasheet.pdf", replacement)
        else:
            data = component_yaml().replace(
                b"media_type: application/pdf", replacement
            )
        load_component_manifest(data)


def test_declared_asset_digest_changes_canonical_manifest_digest() -> None:
    original = load_component_manifest(component_yaml())
    changed = load_component_manifest(
        component_yaml().replace(b"sha256:aaaaaaaa", b"sha256:ffffffff")
    )
    assert component_manifest_digest(original) != component_manifest_digest(changed)


def test_manifest_rejects_duplicate_asset_paths_and_key_mismatch() -> None:
    duplicate = component_yaml().replace(b"path: pinout.json", b"path: datasheet.pdf")
    with pytest.raises(ValueError):
        load_component_manifest(duplicate)
    mismatch = component_yaml().replace(
        b"component_key: Acme:LED-0603-RED", b"component_key: Other:LED-0603-RED"
    )
    with pytest.raises(ValueError):
        load_component_manifest(mismatch)


def test_manifest_rejects_duplicate_yaml_keys_and_parser_errors() -> None:
    duplicate = component_yaml().replace(
        b"schema_version: '1.0'\n", b"schema_version: '1.0'\nschema_version: '1.0'\n"
    )
    with pytest.raises(ValueError):
        load_component_manifest(duplicate)
    for malformed in (b"[", b"- one\n- two\n", b"schema_version: 1.0\n"):
        with pytest.raises(ValueError):
            load_component_manifest(malformed)


@pytest.mark.parametrize("path", [b".", b".."])
def test_manifest_rejects_dot_asset_paths(path: bytes) -> None:
    data = component_yaml().replace(b"path: datasheet.pdf", b"path: " + path)
    with pytest.raises(ValueError):
        load_component_manifest(data)


def test_component_revision_rejects_non_verified_status() -> None:
    with pytest.raises(ValueError):
        ComponentRevision(
            id="comprev_test",
            component_key="Acme:LED-0603-RED",
            manufacturer="Acme",
            part_number="LED-0603-RED",
            name="LED",
            revision="Rev-A",
            status="draft",
            canonical_digest="sha256:" + "a" * 64,
            manifest_artifact_digest="sha256:" + "b" * 64,
            datasheet_artifact_digest="sha256:" + "c" * 64,
            pinout_artifact_digest="sha256:" + "d" * 64,
            symbol_artifact_digest="sha256:" + "e" * 64,
            footprint_artifact_digest="sha256:" + "f" * 64,
            model_3d_artifact_digest=None,
            idempotency_key="key",
            created_at=datetime.now(),
        )


def test_component_import_rejects_asset_replaced_after_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pcbflow import workspaces

    component_directory = build_component_directory(tmp_path / "component")
    asset = component_directory / "symbol.kicad_sym"
    outside = tmp_path / "outside.kicad_sym"
    outside.write_bytes(asset.read_bytes())
    service, artifacts = _component_service(tmp_path)
    real_open = workspaces.os.open

    def replace_after_validation(path, flags, *args, **kwargs):
        if Path(path) == asset:
            asset.unlink()
            try:
                asset.symlink_to(outside)
            except OSError:
                pytest.skip("symlink creation is unavailable on this platform")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(workspaces.os, "open", replace_after_validation)
    with pytest.raises(ValueError, match="regular non-link file"):
        service.import_revision(component_directory / "component.yaml", "asset-race")

    staging = artifacts.root / ".staging"
    assert not staging.exists() or not any(staging.iterdir())


def test_component_import_propagates_storage_error_from_asset_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    component_directory = build_component_directory(tmp_path / "component")
    service, artifacts = _component_service(tmp_path)
    real_stage_stream = artifacts.stage_stream
    calls = 0

    def fail_on_second_stage(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("disk full")
        return real_stage_stream(*args, **kwargs)

    monkeypatch.setattr(artifacts, "stage_stream", fail_on_second_stage)
    with pytest.raises(OSError, match="disk full"):
        service.import_revision(component_directory / "component.yaml", "storage-race")

    staging = artifacts.root / ".staging"
    assert not staging.exists() or not any(staging.iterdir())

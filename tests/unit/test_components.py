from __future__ import annotations

import pytest

from pcbflow.components import component_manifest_digest, load_component_manifest
from tests.component_fixtures import component_yaml


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

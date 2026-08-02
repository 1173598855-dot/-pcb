from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path

import pytest

from pcbflow.artifacts import ArtifactDescriptor, _STREAM_CHUNK_BYTES
from pcbflow.canonical import canonical_json_bytes
from pcbflow.components import (
    ComponentRevision,
    component_manifest_digest,
    load_component_manifest,
)
from pcbflow.repositories import IdempotencyConflictError
from tests.component_fixtures import build_component_directory, component_yaml


@pytest.fixture
def component_directory(tmp_path: Path) -> Path:
    return build_component_directory(tmp_path / "component", include_model=True)


def component_artifact_digests(revision: ComponentRevision) -> tuple[str, ...]:
    return tuple(
        digest
        for digest in (
            revision.manifest_artifact_digest,
            revision.datasheet_artifact_digest,
            revision.pinout_artifact_digest,
            revision.symbol_artifact_digest,
            revision.footprint_artifact_digest,
            revision.model_3d_artifact_digest,
        )
        if digest is not None
    )


def test_component_import_stores_canonical_manifest_and_all_asset_evidence(
    container, component_directory: Path
) -> None:
    created = container.components.import_revision(
        component_directory / "component.yaml", "component-import-1"
    )
    assert created.model_3d_artifact_digest is not None
    assert all(
        container.artifacts.verify(digest)
        for digest in component_artifact_digests(created)
    )


def test_component_import_rejects_digest_mismatched_asset_without_revision(
    container, component_directory: Path
) -> None:
    (component_directory / "symbol.kicad_sym").write_bytes(b"tampered")

    with pytest.raises(ValueError, match="digest mismatch"):
        container.components.import_revision(
            component_directory / "component.yaml", "component-import-2"
        )

    assert container.component_store.list_for_component("Acme:LED-0603-RED") == ()


def test_component_import_streams_large_assets_without_put_bytes(
    container, component_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    large_datasheet = b"%PDF-1.4\n" + b"x" * (_STREAM_CHUNK_BYTES * 2)
    datasheet = component_directory / "datasheet.pdf"
    datasheet.write_bytes(large_datasheet)
    manifest_path = component_directory / "component.yaml"
    manifest = load_component_manifest(manifest_path.read_bytes())
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8").replace(
            manifest.datasheet.digest,
            "sha256:" + hashlib.sha256(large_datasheet).hexdigest(),
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        container.artifacts,
        "put_bytes",
        lambda *_args, **_kwargs: pytest.fail("component import must stage artifacts"),
    )

    revision = container.components.import_revision(
        manifest_path, "component-stream-large"
    )

    assert container.artifacts.verify(revision.datasheet_artifact_digest)


def test_component_import_discards_all_stages_after_later_asset_failure(
    container, component_directory: Path
) -> None:
    manifest_path = component_directory / "component.yaml"
    manifest = load_component_manifest(manifest_path.read_bytes())
    (component_directory / "footprint.kicad_mod").write_bytes(b"tampered")

    with pytest.raises(ValueError, match="digest mismatch"):
        container.components.import_revision(manifest_path, "component-stream-failure")

    assert container.component_store.list_for_component(manifest.component_key) == ()
    staging = container.artifacts.root / ".staging"
    assert not staging.exists() or not any(staging.iterdir())
    assert not container.artifacts._path(manifest.datasheet.digest).exists()


def test_component_import_rejects_parent_asset_path_without_revision(
    container, component_directory: Path
) -> None:
    manifest_path = component_directory / "component.yaml"
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8").replace(
            "path: datasheet.pdf", "path: ../datasheet.pdf"
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        container.components.import_revision(manifest_path, "component-import-parent")

    assert container.component_store.list_for_component("Acme:LED-0603-RED") == ()


def test_component_import_rejects_linked_asset_without_revision(
    container, component_directory: Path, tmp_path: Path
) -> None:
    linked_asset = component_directory / "symbol.kicad_sym"
    target = tmp_path / "outside.kicad_sym"
    target.write_bytes(linked_asset.read_bytes())
    linked_asset.unlink()
    try:
        linked_asset.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable on this platform")

    with pytest.raises(ValueError, match="regular non-link file"):
        container.components.import_revision(
            component_directory / "component.yaml", "component-import-link"
        )

    assert container.component_store.list_for_component("Acme:LED-0603-RED") == ()


def test_component_import_rejects_missing_asset_without_revision(
    container, component_directory: Path
) -> None:
    (component_directory / "footprint.kicad_mod").unlink()

    with pytest.raises(ValueError, match="cannot be read"):
        container.components.import_revision(
            component_directory / "component.yaml", "component-import-missing"
        )

    assert container.component_store.list_for_component("Acme:LED-0603-RED") == ()


def test_component_import_enforces_cumulative_byte_limit(
    container, component_directory: Path
) -> None:
    declared_files = (
        "datasheet.pdf",
        "pinout.json",
        "symbol.kicad_sym",
        "footprint.kicad_mod",
        "model.step",
    )
    byte_limit = (component_directory / "component.yaml").stat().st_size
    byte_limit += sum(
        (component_directory / filename).stat().st_size
        for filename in declared_files
    ) - 1
    limited_service = type(container.components)(
        container.component_store, container.artifacts, max_bytes=byte_limit
    )

    with pytest.raises(ValueError, match="size limit"):
        limited_service.import_revision(
            component_directory / "component.yaml", "component-import-too-large"
        )

    assert container.component_store.list_for_component("Acme:LED-0603-RED") == ()


def test_component_import_rejects_nonpositive_byte_limit(container) -> None:
    with pytest.raises(ValueError, match="byte limit must be positive"):
        type(container.components)(
            container.component_store,
            container.artifacts,
            max_bytes=0,
        )


def test_component_import_replays_identical_import(
    container, component_directory: Path
) -> None:
    first = container.components.import_revision(
        component_directory / "component.yaml", "component-import-replay"
    )
    second = container.components.import_revision(
        component_directory / "component.yaml", "component-import-replay"
    )

    assert second == first
    assert container.component_store.list_for_component("Acme:LED-0603-RED") == (
        first,
    )


def _component_manifest(artifact_store):
    manifest = load_component_manifest(component_yaml())
    updates = {}
    for field_name in ("datasheet", "pinout", "symbol", "footprint", "model_3d"):
        asset = getattr(manifest, field_name)
        if asset is None:
            continue
        descriptor = artifact_store.put_bytes(
            field_name.encode("ascii"), asset.media_type
        )
        updates[field_name] = asset.model_copy(update={"digest": descriptor.digest})
    return manifest.model_copy(update=updates)


def _component_artifacts(artifact_store, manifest):
    canonical = artifact_store.put_bytes(
        canonical_json_bytes(manifest.model_dump(mode="json")),
        "application/vnd.pcbflow.component-manifest+json",
    )
    descriptors = [canonical]
    for field_name in ("datasheet", "pinout", "symbol", "footprint", "model_3d"):
        asset = getattr(manifest, field_name)
        if asset is None:
            continue
        descriptors.append(
            ArtifactDescriptor(
                asset.digest,
                len(field_name),
                asset.media_type,
                artifact_store._path(asset.digest),
            )
        )
    return tuple(descriptors)


def test_component_revision_store_replays_identical_import(
    session_factory, artifact_store
) -> None:
    from pcbflow.component_store import ComponentRevisionStore

    manifest = _component_manifest(artifact_store)
    store = ComponentRevisionStore(session_factory)
    first = store.create(
        manifest=manifest,
        canonical_digest=component_manifest_digest(manifest),
        idempotency_key="component-led-1",
        artifacts=_component_artifacts(artifact_store, manifest),
    )
    second = store.create(
        manifest=manifest,
        canonical_digest=component_manifest_digest(manifest),
        idempotency_key="component-led-1",
        artifacts=_component_artifacts(artifact_store, manifest),
    )
    assert second == first


def test_component_revision_store_rejects_key_or_identity_conflicts(
    session_factory, artifact_store
) -> None:
    from pcbflow.component_store import ComponentRevisionStore

    manifest = _component_manifest(artifact_store)
    store = ComponentRevisionStore(session_factory)
    artifacts = _component_artifacts(artifact_store, manifest)
    store.create(
        manifest=manifest,
        canonical_digest=component_manifest_digest(manifest),
        idempotency_key="component-led-1",
        artifacts=artifacts,
    )
    changed = manifest.model_copy(update={"name": "Different LED"})
    with __import__("pytest").raises(IdempotencyConflictError):
        store.create(
            manifest=changed,
            canonical_digest=component_manifest_digest(changed),
            idempotency_key="component-led-1",
            artifacts=_component_artifacts(artifact_store, changed),
        )
    with __import__("pytest").raises(IdempotencyConflictError):
        store.create(
            manifest=changed,
            canonical_digest=component_manifest_digest(changed),
            idempotency_key="component-led-2",
            artifacts=_component_artifacts(artifact_store, changed),
        )


def test_component_revision_store_replays_same_identity_with_new_key(
    session_factory, artifact_store
) -> None:
    from pcbflow.component_store import ComponentRevisionStore

    manifest = _component_manifest(artifact_store)
    store = ComponentRevisionStore(session_factory)
    artifacts = _component_artifacts(artifact_store, manifest)
    first = store.create(
        manifest=manifest,
        canonical_digest=component_manifest_digest(manifest),
        idempotency_key="component-led-identity-1",
        artifacts=artifacts,
    )
    second = store.create(
        manifest=manifest,
        canonical_digest=component_manifest_digest(manifest),
        idempotency_key="component-led-identity-2",
        artifacts=artifacts,
    )
    assert second == first
    assert store.find_by_idempotency_key("component-led-identity-2") is None
    assert store.find_by_identity(manifest.component_key, manifest.revision) == first


def test_component_revision_store_rejects_replay_with_conflicting_artifact_metadata(
    session_factory, artifact_store
) -> None:
    from pcbflow.component_store import ComponentRevisionStore

    manifest = _component_manifest(artifact_store)
    store = ComponentRevisionStore(session_factory)
    artifacts = _component_artifacts(artifact_store, manifest)
    store.create(
        manifest=manifest,
        canonical_digest=component_manifest_digest(manifest),
        idempotency_key="component-led-metadata-1",
        artifacts=artifacts,
    )

    conflicting = (
        artifacts[0],
        replace(artifacts[1], size=artifacts[1].size + 1),
        *artifacts[2:],
    )
    with pytest.raises(IdempotencyConflictError):
        store.create(
            manifest=manifest,
            canonical_digest=component_manifest_digest(manifest),
            idempotency_key="component-led-metadata-1",
            artifacts=conflicting,
        )


def test_component_revision_store_rejects_empty_idempotency_key(
    session_factory, artifact_store
) -> None:
    from pcbflow.component_store import ComponentRevisionStore

    manifest = _component_manifest(artifact_store)
    store = ComponentRevisionStore(session_factory)
    with pytest.raises(ValueError, match="idempotency key must not be empty"):
        store.create(
            manifest=manifest,
            canonical_digest=component_manifest_digest(manifest),
            idempotency_key="",
            artifacts=_component_artifacts(artifact_store, manifest),
        )


def test_component_revision_store_rejects_wrong_canonical_digest(
    session_factory, artifact_store
) -> None:
    from pcbflow.component_store import ComponentRevisionStore

    manifest = _component_manifest(artifact_store)
    store = ComponentRevisionStore(session_factory)
    with pytest.raises(IdempotencyConflictError):
        store.create(
            manifest=manifest,
            canonical_digest="sha256:" + "0" * 64,
            idempotency_key="component-led-wrong-digest",
            artifacts=_component_artifacts(artifact_store, manifest),
        )

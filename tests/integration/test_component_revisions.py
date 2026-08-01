from __future__ import annotations

from dataclasses import replace

import pytest

from pcbflow.artifacts import ArtifactDescriptor
from pcbflow.canonical import canonical_json_bytes
from pcbflow.components import component_manifest_digest, load_component_manifest
from pcbflow.repositories import IdempotencyConflictError
from tests.component_fixtures import component_yaml


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

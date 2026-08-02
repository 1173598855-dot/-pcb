# Streaming Component Import Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Stream verified component assets into the content-addressed store with bounded memory while preserving existing component import contracts.

**Architecture:** Add a store-owned staging primitive that hashes a binary stream into a temporary file, validates its size and expected digest, and atomically publishes it only on request. ComponentRevisionService stages the canonical manifest and every declared asset, publishes them after validation succeeds, then calls the unchanged component-revision store with descriptors in the existing order.

**Tech Stack:** Python 3.12/3.13, standard-library hashlib/tempfile/os, SQLAlchemy, Pydantic, PyYAML, pytest, pytest-cov.

## Global Constraints

- Keep the component manifest, REST API, CLI JSON, SQLite schema, and ComponentRevisionStore.create() signature unchanged.
- Use a fixed 1 MiB stream chunk and enforce the existing cumulative max_bytes limit exactly.
- Preserve declared SHA-256 validation and existing component input error messages.
- Create temporary files only below the artifact-store root; publish with os.replace() on the same filesystem.
- Do not overwrite a verified existing artifact object or create a component revision for an unpublished artifact.
- Preserve descriptor order: canonical manifest, datasheet, pinout, symbol, footprint, optional 3D model.
- Use focused tests before implementation, then the complete test suite and 90 percent coverage gate.

---

## File Structure

| File | Responsibility |
| --- | --- |
| src/pcbflow/artifacts.py | Stage bounded binary streams, validate digests, publish or discard temporary content-addressed objects. |
| src/pcbflow/components.py | Open validated component assets once and stage them without loading all asset bytes into memory. |
| tests/unit/test_artifacts.py | Prove bounded stream reads, staged publication, size rejection, digest rejection, and cleanup. |
| tests/integration/test_component_revisions.py | Prove large component assets use the streaming path and later failures leave no revision or staging residue. |
| docs/OPTIMIZATION_GUIDE.md | Record the completed component-import optimization and its validation evidence. |

### Task 1: Add Bounded Artifact Staging

**Files:**
- Modify: src/pcbflow/artifacts.py
- Modify: tests/unit/test_artifacts.py

**Interfaces:**
- Consumes: BinaryIO, an optional declared digest, an optional byte limit, and a media type.
- Produces: ContentAddressedStore.stage_stream(stream, media_type, *, expected_digest=None, max_bytes=None) -> StagedArtifact.
- Produces: StagedArtifact.publish() -> ArtifactDescriptor and StagedArtifact.discard() -> None.
- Produces: ArtifactDigestMismatchError and ArtifactSizeLimitError for callers that need stable domain-level error translation.

- [ ] **Step 1: Write the failing artifact staging tests**

Add this support reader and tests to tests/unit/test_artifacts.py:

~~~python
import io

from pcbflow.artifacts import (
    ArtifactDigestMismatchError,
    ArtifactSizeLimitError,
    _STREAM_CHUNK_BYTES,
)


class RecordingReader(io.BytesIO):
    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.request_sizes: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.request_sizes.append(size)
        return super().read(size)


def test_stage_stream_reads_bounded_chunks_and_publishes_content(tmp_path: Path) -> None:
    store = ContentAddressedStore(tmp_path / "artifacts")
    payload = b"x" * (_STREAM_CHUNK_BYTES * 2 + 17)
    expected = f"sha256:{hashlib.sha256(payload).hexdigest()}"
    reader = RecordingReader(payload)

    staged = store.stage_stream(
        reader,
        "application/octet-stream",
        expected_digest=expected,
        max_bytes=len(payload),
    )

    assert all(0 < size <= _STREAM_CHUNK_BYTES for size in reader.request_sizes)
    assert not store._path(expected).exists()
    descriptor = staged.publish()
    assert descriptor.digest == expected
    assert descriptor.size == len(payload)
    assert descriptor.path.read_bytes() == payload


def test_stage_stream_rejects_bad_digest_or_limit_without_publishing(tmp_path: Path) -> None:
    store = ContentAddressedStore(tmp_path / "artifacts")
    payload = b"trusted bytes"
    expected = f"sha256:{hashlib.sha256(payload).hexdigest()}"

    with pytest.raises(ArtifactDigestMismatchError):
        store.stage_stream(
            io.BytesIO(payload),
            "application/octet-stream",
            expected_digest="sha256:" + "0" * 64,
        )
    with pytest.raises(ArtifactSizeLimitError):
        store.stage_stream(
            io.BytesIO(payload),
            "application/octet-stream",
            expected_digest=expected,
            max_bytes=len(payload) - 1,
        )

    staging = store.root / ".staging"
    assert not store._path(expected).exists()
    assert not staging.exists() or not any(staging.iterdir())
~~~

- [ ] **Step 2: Run test to verify it fails**

Run:

~~~powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\test_artifacts.py -q
~~~

Expected: FAIL because stage_stream, its errors, and _STREAM_CHUNK_BYTES do not exist.

- [ ] **Step 3: Write the minimal staging implementation**

In src/pcbflow/artifacts.py, add a 1 MiB stream chunk constant, these errors, and a mutable staged value:

~~~python
class ArtifactDigestMismatchError(ValueError):
    def __init__(self, expected_digest: str, actual_digest: str) -> None:
        super().__init__(
            f"artifact digest mismatch: expected {expected_digest}, got {actual_digest}"
        )
        self.expected_digest = expected_digest
        self.actual_digest = actual_digest


class ArtifactSizeLimitError(ValueError):
    pass


@dataclass(slots=True)
class StagedArtifact:
    _store: ContentAddressedStore
    _temporary_path: Path | None
    digest: str
    size: int
    media_type: str

    def publish(self) -> ArtifactDescriptor:
        return self._store._publish_staged(self)

    def discard(self) -> None:
        if self._temporary_path is not None:
            self._temporary_path.unlink(missing_ok=True)
            self._temporary_path = None
~~~

Implement stage_stream so it creates root/.staging, reads with stream.read(min(_STREAM_CHUNK_BYTES, remaining + 1)) when max_bytes is set, hashes and writes each chunk to a mkstemp file, flushes and fsyncs it, validates the optional digest, and unlinks the temporary file on every exception. Call self._path() to validate an expected digest before reading.

Implement _publish_staged so it creates the digest-derived target directory, rehashes an existing target and compares both digest and size, otherwise uses os.replace(staged._temporary_path, target). Clear the temporary path and return ArtifactDescriptor(staged.digest, staged.size, staged.media_type, target). Extract the existing-target verification into a private helper shared by publication paths.

Replace put_bytes with:

~~~python
def put_bytes(self, data: bytes, media_type: str) -> ArtifactDescriptor:
    with io.BytesIO(data) as stream:
        return self.stage_stream(stream, media_type).publish()
~~~

Add import io and retain ArtifactConflictError behavior for corrupted existing objects.

- [ ] **Step 4: Run focused artifact tests to verify they pass**

Run:

~~~powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\test_artifacts.py -q
~~~

Expected: PASS, including existing content-addressed and corruption tests.

- [ ] **Step 5: Commit the isolated artifact-store change**

~~~powershell
git add src/pcbflow/artifacts.py tests/unit/test_artifacts.py
git commit -m "feat: stage streamed artifacts"
~~~

### Task 2: Stream Declared Component Assets

**Files:**
- Modify: src/pcbflow/components.py
- Modify: tests/integration/test_component_revisions.py

**Interfaces:**
- Consumes: ContentAddressedStore.stage_stream and its staging errors from Task 1.
- Produces: unchanged ComponentRevisionService.import_revision(manifest_path, idempotency_key) -> ComponentRevision behavior with bounded asset memory.
- Produces: descriptor tuples in the unchanged ComponentRevisionStore.create order.

- [ ] **Step 1: Write failing integration tests for streamed imports and cleanup**

Append these tests to tests/integration/test_component_revisions.py:

~~~python
from pcbflow.artifacts import _STREAM_CHUNK_BYTES
from pcbflow.components import load_component_manifest


def test_component_import_streams_large_assets_without_put_bytes(
    container, component_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    large_datasheet = b"%PDF-1.4\n" + b"x" * (_STREAM_CHUNK_BYTES * 2)
    datasheet = component_directory / "datasheet.pdf"
    datasheet.write_bytes(large_datasheet)
    manifest_path = component_directory / "component.yaml"
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8").replace(
            "sha256:" + "a" * 64,
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
~~~

- [ ] **Step 2: Run test to verify it fails**

Run:

~~~powershell
.\.venv\Scripts\python.exe -m pytest tests\integration\test_component_revisions.py -q
~~~

Expected: FAIL at the large-asset test because component imports still call
put_bytes with complete asset payloads. The later-failure assertion may already
pass before the change because the current implementation reads and validates
all assets before it publishes them; it protects that existing no-residue
contract while the streaming path is introduced.

- [ ] **Step 3: Write the minimal component streaming implementation**

Remove _DeclaredAssetBytes and _load_declared_assets. Keep _read_regular_file for YAML, factor its file-kind check into _validate_regular_file(path), and add:

~~~python
@contextmanager
def _open_regular_file(path: Path) -> Iterator[BinaryIO]:
    _validate_regular_file(path)
    try:
        with path.open("rb") as stream:
            yield stream
    except OSError as error:
        raise ValueError("component evidence file cannot be read") from error
~~~

Use io.BytesIO, ExitStack, and Task 1 types in ComponentRevisionService.import_revision:

~~~python
manifest_bytes = _read_regular_file(manifest_path, self._max_bytes)
manifest = load_component_manifest(manifest_bytes)
canonical_digest = component_manifest_digest(manifest)
canonical_bytes = canonical_json_bytes(manifest.model_dump(mode="json"))
remaining_bytes = self._max_bytes - len(manifest_bytes)

with ExitStack() as cleanup:
    stages = [
        self._artifacts.stage_stream(
            io.BytesIO(canonical_bytes),
            _MANIFEST_MEDIA_TYPE,
            expected_digest=canonical_digest,
        )
    ]
    for stage in stages:
        cleanup.callback(stage.discard)
    for field_name in ("datasheet", "pinout", "symbol", "footprint", "model_3d"):
        asset = getattr(manifest, field_name)
        if asset is None:
            continue
        candidate = _declared_asset_path(manifest_path.parent, asset.path)
        try:
            with _open_regular_file(candidate) as stream:
                stage = self._artifacts.stage_stream(
                    stream,
                    asset.media_type,
                    expected_digest=asset.digest,
                    max_bytes=remaining_bytes,
                )
        except ArtifactDigestMismatchError as error:
            raise ValueError(f"component asset digest mismatch: {asset.path}") from error
        except ArtifactSizeLimitError as error:
            raise ValueError("component import exceeds size limit") from error
        remaining_bytes -= stage.size
        stages.append(stage)
        cleanup.callback(stage.discard)
    descriptors = tuple(stage.publish() for stage in stages)

return self._store.create(
    manifest=manifest,
    canonical_digest=canonical_digest,
    idempotency_key=idempotency_key,
    artifacts=descriptors,
)
~~~

Implement _declared_asset_path(root, asset_path) with the current direct-sibling check (candidate.parent == root) so traversal rejection is unchanged. Add contextlib and binary-stream typing imports. Discard callbacks become no-ops after successful publication.

- [ ] **Step 4: Run component regression tests to verify they pass**

Run:

~~~powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\test_components.py tests\integration\test_component_revisions.py tests\e2e\test_component_api_cli.py -q
~~~

Expected: PASS with no REST or CLI response-shape change and no component revision after the late digest mismatch.

- [ ] **Step 5: Commit the component-import change**

~~~powershell
git add src/pcbflow/components.py tests/integration/test_component_revisions.py
git commit -m "feat: stream component import assets"
~~~

### Task 3: Record the Optimization and Run the Full Gate

**Files:**
- Modify: docs/OPTIMIZATION_GUIDE.md

**Interfaces:**
- Consumes: passing focused tests from Tasks 1 and 2.
- Produces: an accurate optimization status and reproducible verification record.

- [ ] **Step 1: Update the optimization guide after focused tests pass**

In the Execution Status table, add a completed P1 entry for content-addressed component imports. State that assets stream through a store-owned staging area, each asset is SHA-256 checked before publication, and the cumulative import limit remains enforced. Add the focused artifact/component command to the verification matrix and remove the matching item from Deferred Optimizations.

- [ ] **Step 2: Run the complete quality gate**

Run:

~~~powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pytest --cov=pcbflow --cov-report=term-missing --cov-fail-under=90
.\.venv\Scripts\python.exe -m compileall -q src
git diff --check
~~~

Expected: all available tests pass, the two real-KiCad tests may skip only when kicad-cli is absent, coverage is at least 90 percent, and compilation and patch checks exit zero.

- [ ] **Step 3: Record the fresh verification result**

Replace Latest Verification Run counts and coverage in docs/OPTIMIZATION_GUIDE.md with the exact output from Step 2. Keep the local kicad-cli prerequisite note only if those tests were skipped.

- [ ] **Step 4: Commit the documentation and verification record**

~~~powershell
git add docs/OPTIMIZATION_GUIDE.md
git commit -m "docs: record streamed component import optimization"
~~~

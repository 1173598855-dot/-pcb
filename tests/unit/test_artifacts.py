import io
import hashlib
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from pcbflow.artifacts import (
    ArtifactConflictError,
    ArtifactDigestMismatchError,
    ArtifactSizeLimitError,
    ContentAddressedStore,
    InvalidDigestError,
    _STREAM_CHUNK_BYTES,
)


class RecordingReader(io.BytesIO):
    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.request_sizes: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.request_sizes.append(size)
        return super().read(size)


def test_put_rejects_a_corrupted_existing_digest_object(tmp_path: Path) -> None:
    store = ContentAddressedStore(tmp_path / "artifacts")
    descriptor = store.put_bytes(b"trusted evidence", "application/octet-stream")
    descriptor.path.write_bytes(b"corrupted bytes")
    with pytest.raises(ArtifactConflictError, match=descriptor.digest):
        store.put_bytes(b"trusted evidence", "application/octet-stream")


def test_put_bytes_is_content_addressed_and_idempotent(tmp_path: Path) -> None:
    store = ContentAddressedStore(tmp_path)
    first = store.put_bytes(b"erc report", "application/json")
    second = store.put_bytes(b"erc report", "application/json")
    expected = hashlib.sha256(b"erc report").hexdigest()

    assert first.digest == f"sha256:{expected}"
    assert (
        first.path
        == tmp_path.resolve()
        / "objects"
        / "sha256"
        / expected[:2]
        / expected[2:4]
        / expected
    )
    assert first == second
    assert first.path.read_bytes() == b"erc report"
    assert store.verify(first.digest)


@given(st.binary(max_size=65_536))
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_arbitrary_payload_round_trips(tmp_path: Path, payload: bytes) -> None:
    store = ContentAddressedStore(tmp_path)
    descriptor = store.put_bytes(payload, "application/octet-stream")

    with store.open(descriptor.digest) as artifact:
        assert artifact.read() == payload
    assert store.verify(descriptor.digest)


def test_open_rejects_malformed_digest(tmp_path: Path) -> None:
    store = ContentAddressedStore(tmp_path)

    with pytest.raises(InvalidDigestError):
        store.open("../../secret")


def test_verify_detects_corrupted_artifact(tmp_path: Path) -> None:
    store = ContentAddressedStore(tmp_path)
    descriptor = store.put_bytes(b"original", "application/octet-stream")
    descriptor.path.write_bytes(b"corrupted")

    assert not store.verify(descriptor.digest)


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


def test_stage_stream_rejects_bad_digest_or_limit_without_publishing(
    tmp_path: Path,
) -> None:
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


def test_stage_stream_discards_duplicate_staging_file_after_publish(tmp_path: Path) -> None:
    store = ContentAddressedStore(tmp_path / "artifacts")
    payload = b"duplicate content"

    store.stage_stream(io.BytesIO(payload), "application/octet-stream").publish()
    store.stage_stream(io.BytesIO(payload), "application/octet-stream").publish()

    assert not any((store.root / ".staging").iterdir())

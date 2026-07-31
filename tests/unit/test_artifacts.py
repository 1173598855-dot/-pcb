import hashlib
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from pcbflow.artifacts import ArtifactConflictError, ContentAddressedStore, InvalidDigestError


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

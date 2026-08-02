from __future__ import annotations

import hashlib
import io
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

_DIGEST = re.compile(r"^sha256:([0-9a-f]{64})$")
_STREAM_CHUNK_BYTES = 1024 * 1024


class InvalidDigestError(ValueError):
    pass


class ArtifactConflictError(RuntimeError):
    def __init__(self, digest: str) -> None:
        super().__init__(f"artifact integrity conflict: {digest}")
        self.digest = digest


class ArtifactDigestMismatchError(ValueError):
    def __init__(self, expected_digest: str, actual_digest: str) -> None:
        super().__init__(
            f"artifact digest mismatch: expected {expected_digest}, got {actual_digest}"
        )
        self.expected_digest = expected_digest
        self.actual_digest = actual_digest


class ArtifactSizeLimitError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ArtifactDescriptor:
    digest: str
    size: int
    media_type: str
    path: Path


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


class ContentAddressedStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def _path(self, digest: str) -> Path:
        match = _DIGEST.fullmatch(digest)
        if match is None:
            raise InvalidDigestError(digest)
        value = match.group(1)
        return self.root / "objects" / "sha256" / value[:2] / value[2:4] / value

    def put_bytes(self, data: bytes, media_type: str) -> ArtifactDescriptor:
        with io.BytesIO(data) as stream:
            staged = self.stage_stream(stream, media_type)
        try:
            return staged.publish()
        finally:
            staged.discard()

    def stage_stream(
        self,
        stream: BinaryIO,
        media_type: str,
        *,
        expected_digest: str | None = None,
        max_bytes: int | None = None,
    ) -> StagedArtifact:
        if expected_digest is not None:
            self._path(expected_digest)
        if max_bytes is not None and max_bytes < 0:
            raise ArtifactSizeLimitError(f"artifact exceeds byte limit: {max_bytes}")

        staging = self.root / ".staging"
        staging.mkdir(parents=True, exist_ok=True)
        handle, temporary_name = tempfile.mkstemp(prefix="artifact-", dir=staging)
        temporary_path = Path(temporary_name)
        digest_hash = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(handle, "wb") as temporary:
                while True:
                    read_size = _STREAM_CHUNK_BYTES
                    if max_bytes is not None:
                        read_size = min(read_size, max_bytes - size + 1)
                    chunk = stream.read(read_size)
                    if not chunk:
                        break
                    size += len(chunk)
                    if max_bytes is not None and size > max_bytes:
                        raise ArtifactSizeLimitError(
                            f"artifact exceeds byte limit: {max_bytes}"
                        )
                    digest_hash.update(chunk)
                    temporary.write(chunk)
                temporary.flush()
                os.fsync(temporary.fileno())

            digest = f"sha256:{digest_hash.hexdigest()}"
            if expected_digest is not None and digest != expected_digest:
                raise ArtifactDigestMismatchError(expected_digest, digest)
            return StagedArtifact(self, temporary_path, digest, size, media_type)
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise

    def _publish_staged(self, staged: StagedArtifact) -> ArtifactDescriptor:
        if staged._temporary_path is None:
            raise RuntimeError("staged artifact has already been published or discarded")
        target = self._path(staged.digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            self._verify_existing(target, staged.digest, staged.size)
            staged._temporary_path.unlink(missing_ok=True)
        else:
            os.replace(staged._temporary_path, target)
        staged._temporary_path = None
        return ArtifactDescriptor(staged.digest, staged.size, staged.media_type, target)

    def _verify_existing(self, target: Path, digest: str, expected_size: int) -> None:
        digest_hash = hashlib.sha256()
        size = 0
        with target.open("rb") as existing:
            while chunk := existing.read(_STREAM_CHUNK_BYTES):
                digest_hash.update(chunk)
                size += len(chunk)
        if size != expected_size or digest != f"sha256:{digest_hash.hexdigest()}":
            raise ArtifactConflictError(digest)

    def open(self, digest: str) -> BinaryIO:
        return self._path(digest).open("rb")

    def verify(self, digest: str) -> bool:
        with self.open(digest) as artifact:
            actual = hashlib.sha256(artifact.read()).hexdigest()
        return digest == f"sha256:{actual}"

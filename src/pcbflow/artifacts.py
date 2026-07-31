from __future__ import annotations

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

_DIGEST = re.compile(r"^sha256:([0-9a-f]{64})$")


class InvalidDigestError(ValueError):
    pass


class ArtifactConflictError(RuntimeError):
    def __init__(self, digest: str) -> None:
        super().__init__(f"artifact integrity conflict: {digest}")
        self.digest = digest


@dataclass(frozen=True, slots=True)
class ArtifactDescriptor:
    digest: str
    size: int
    media_type: str
    path: Path


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
        value = hashlib.sha256(data).hexdigest()
        digest = f"sha256:{value}"
        target = self._path(digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            digest_hash = hashlib.sha256()
            size = 0
            with target.open("rb") as existing:
                while chunk := existing.read(1024 * 1024):
                    digest_hash.update(chunk)
                    size += len(chunk)
            if size != len(data) or digest_hash.hexdigest() != value:
                raise ArtifactConflictError(digest)
        else:
            handle, temporary_name = tempfile.mkstemp(
                prefix="artifact-", dir=target.parent
            )
            try:
                with os.fdopen(handle, "wb") as temporary:
                    temporary.write(data)
                    temporary.flush()
                    os.fsync(temporary.fileno())
                os.replace(temporary_name, target)
            finally:
                if os.path.exists(temporary_name):
                    os.unlink(temporary_name)
        return ArtifactDescriptor(digest, len(data), media_type, target)

    def open(self, digest: str) -> BinaryIO:
        return self._path(digest).open("rb")

    def verify(self, digest: str) -> bool:
        with self.open(digest) as artifact:
            actual = hashlib.sha256(artifact.read()).hexdigest()
        return digest == f"sha256:{actual}"

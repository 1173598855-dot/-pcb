from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re

from pcbflow.canonical import canonical_digest
from pcbflow.domain import EdaKind, EdaOperation, RequestInvalidError

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_MAX_IDENTIFIER_LENGTH = 128
_MAX_IDEMPOTENCY_KEY_LENGTH = 255


class EdaAuthorityConflictError(RequestInvalidError):
    pass


@dataclass(frozen=True, slots=True)
class ProjectEdaAuthorityInput:
    eda_kind: EdaKind
    eda_profile_id: str
    board_profile_id: str
    rulepack_digest: str


@dataclass(frozen=True, slots=True)
class ProjectEdaAuthority:
    project_id: str
    eda_kind: EdaKind
    eda_profile_id: str
    board_profile_id: str
    rulepack_digest: str
    canonical_digest: str
    created_at: datetime


def validate_idempotency_key(idempotency_key: str) -> str:
    if (
        not isinstance(idempotency_key, str)
        or not idempotency_key.strip()
        or idempotency_key != idempotency_key.strip()
        or len(idempotency_key) > _MAX_IDEMPOTENCY_KEY_LENGTH
    ):
        raise RequestInvalidError(
            "idempotency key must be nonblank, trimmed, and at most 255 characters"
        )
    return idempotency_key


def validate_authority_input(
    authority: ProjectEdaAuthorityInput,
) -> ProjectEdaAuthorityInput:
    if not isinstance(authority.eda_kind, EdaKind):
        raise RequestInvalidError("unsupported EDA kind")
    for label, value in (
        ("EDA profile id", authority.eda_profile_id),
        ("board profile id", authority.board_profile_id),
    ):
        if (
            not isinstance(value, str)
            or not value.strip()
            or value != value.strip()
            or len(value) > _MAX_IDENTIFIER_LENGTH
        ):
            raise RequestInvalidError(
                f"{label} must be nonblank, trimmed, and at most 128 characters"
            )
    if not isinstance(authority.rulepack_digest, str) or not _DIGEST.fullmatch(
        authority.rulepack_digest
    ):
        raise RequestInvalidError("rulepack digest must be a sha256 digest")
    return authority


def authority_digest(project_id: str, authority: ProjectEdaAuthorityInput) -> str:
    return canonical_digest(
        {
            "schema_version": "1.0",
            "project_id": project_id,
            "eda_kind": authority.eda_kind.value,
            "eda_profile_id": authority.eda_profile_id,
            "board_profile_id": authority.board_profile_id,
            "rulepack_digest": authority.rulepack_digest,
        }
    )


def registration_input_digest(
    name: str,
    source_path: Path | str,
    authority: ProjectEdaAuthorityInput | None,
) -> str:
    return canonical_digest(registration_input_payload(name, source_path, authority))


def registration_input_payload(
    name: str,
    source_path: Path | str,
    authority: ProjectEdaAuthorityInput | None,
) -> dict[str, object]:
    authority_payload: dict[str, str] | None = None
    if authority is not None:
        authority_payload = {
            "eda_kind": authority.eda_kind.value,
            "eda_profile_id": authority.eda_profile_id,
            "board_profile_id": authority.board_profile_id,
            "rulepack_digest": authority.rulepack_digest,
        }
    return {
        "schema_version": "1.0",
        "name": name,
        "source_path": str(source_path),
        "authority_present": authority is not None,
        "authority": authority_payload,
    }


@dataclass(frozen=True, slots=True)
class EdaCapability:
    available: bool
    executable: Path | None
    version: str | None
    executable_digest: str | None
    profile_id: str | None
    profile_revision: int | None
    operations: frozenset[EdaOperation]
    write_verified: bool
    reason: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.operations, frozenset):
            raise TypeError("operations must be a frozenset")

    def is_usable(self) -> bool:
        return self.available and self.write_verified

    def get_supported_operations(self) -> frozenset[EdaOperation]:
        if self.write_verified:
            return self.operations
        return frozenset()

    def matches_profile(self, expected_profile: str) -> bool:
        return self.profile_id == expected_profile

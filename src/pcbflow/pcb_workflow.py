from __future__ import annotations

import hashlib
import io
import json
import re
from collections.abc import Sequence
from dataclasses import replace
from typing import Protocol

from pcbflow.artifacts import ContentAddressedStore
from pcbflow.canonical import canonical_json_bytes
from pcbflow.domain import (
    EdaKind,
    EdaOperation,
    RequestInvalidError,
    Task,
    TaskLease,
    TaskStatus,
    utc_now,
)
from pcbflow.eda import EdaCapability, validate_idempotency_key
from pcbflow.eda_authority_store import ProjectEdaAuthorityStore
from pcbflow.lceda_pro import LcedaProCapabilityError
from pcbflow.repositories import EvidenceRepository, ProjectRepository, TaskRepository

LCEDA_CAPABILITY_TASK_KIND = "pcb.lceda_pro_capability_probe"
CAPABILITY_MEDIA_TYPE = "application/vnd.pcbflow.eda-capability+json"
CAPABILITY_EVIDENCE_KIND = "lceda_pro_capability"
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


class CapabilityProbePort(Protocol):
    def probe(self) -> EdaCapability: ...


def _blocked() -> LcedaProCapabilityError:
    return LcedaProCapabilityError("LCEDA_PRO_WRITE_CAPABILITY_UNVERIFIED")


def _validated_capability_payload(raw: bytes) -> dict[str, object]:
    """Decode the exact canonical LCEDA capability representation or fail closed."""
    try:
        payload = json.loads(raw.decode("utf-8"))
        expected = {
            "available", "executable", "version", "executable_digest", "profile_id",
            "profile_revision", "operations", "write_verified", "reason",
        }
        if not isinstance(payload, dict) or set(payload) != expected:
            raise ValueError("invalid capability fields")
        if type(payload["available"]) is not bool or type(payload["write_verified"]) is not bool:
            raise ValueError("invalid capability booleans")
        for key in ("executable", "version", "executable_digest", "profile_id", "reason"):
            if payload[key] is not None and not isinstance(payload[key], str):
                raise ValueError("invalid capability scalar")
        revision = payload["profile_revision"]
        if revision is not None and (type(revision) is not int or revision < 1):
            raise ValueError("invalid profile revision")
        operations = payload["operations"]
        if not isinstance(operations, list) or any(not isinstance(item, str) for item in operations):
            raise ValueError("invalid operations")
        values = [EdaOperation(item).value for item in operations]
        if values != sorted(values) or len(values) != len(set(values)):
            raise ValueError("operations must be ordered and unique")
        if raw != canonical_json_bytes(payload):
            raise ValueError("capability JSON is not canonical")
        available = payload["available"]
        verified = payload["write_verified"]
        if not available and (
            payload["profile_id"] is not None
            or payload["profile_revision"] is not None
            or values
            or verified
            or not payload["reason"]
        ):
            raise ValueError("invalid unavailable capability")
        if verified and (
            not isinstance(payload["executable"], str)
            or not payload["executable"]
            or not isinstance(payload["version"], str)
            or not payload["version"]
            or not isinstance(payload["executable_digest"], str)
            or _DIGEST.fullmatch(payload["executable_digest"]) is None
            or not isinstance(payload["profile_id"], str)
            or not payload["profile_id"]
            or payload["profile_revision"] is None
            or payload["reason"] is not None
            or EdaOperation.APPLY_OPERATIONS.value not in values
        ):
            raise ValueError("invalid verified capability")
        return payload
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        raise _blocked() from error


def _read_capability(
    artifacts: ContentAddressedStore, evidence: EvidenceRepository, digest: str
) -> dict[str, object]:
    try:
        if evidence.artifact_media_type(digest) != CAPABILITY_MEDIA_TYPE:
            raise ValueError("unexpected media type")
        with artifacts.open(digest) as stream:
            raw = stream.read()
        actual_digest = f"sha256:{hashlib.sha256(raw).hexdigest()}"
        if actual_digest != digest:
            raise ValueError("artifact digest verification failed")
        return _validated_capability_payload(raw)
    except LcedaProCapabilityError:
        raise
    except Exception as error:
        raise _blocked() from error


def capability_artifact_bytes(capability: EdaCapability) -> bytes:
    """Serialize capabilities without relying on dataclass or set JSON coercion."""
    return canonical_json_bytes(
        {
            "available": capability.available,
            "executable": (
                str(capability.executable) if capability.executable is not None else None
            ),
            "version": capability.version,
            "executable_digest": capability.executable_digest,
            "profile_id": capability.profile_id,
            "profile_revision": capability.profile_revision,
            "operations": sorted(operation.value for operation in capability.operations),
            "write_verified": capability.write_verified,
            "reason": capability.reason,
        }
    )


class CapabilityProbeTaskHandler:
    def __init__(
        self,
        authorities: ProjectEdaAuthorityStore,
        tasks: TaskRepository,
        evidence: EvidenceRepository,
        artifacts: ContentAddressedStore,
        adapter: CapabilityProbePort,
    ) -> None:
        self._authorities = authorities
        self._tasks = tasks
        self._evidence = evidence
        self._artifacts = artifacts
        self._adapter = adapter

    def __call__(self, lease: TaskLease) -> dict[str, object]:
        project_id = str(lease.payload["project_id"])
        authority = self._authorities.find_by_project_id(project_id)
        if authority is None or authority.eda_kind is not EdaKind.LCEDA_PRO:
            raise RequestInvalidError("capability probes require lceda_pro authority")
        self._assert_active(lease)
        existing = next(
            (
                item for item in self._evidence.list_for_project(project_id)
                if item.task_id == lease.task_id and item.kind == CAPABILITY_EVIDENCE_KIND
            ),
            None,
        )
        if existing is not None:
            payload = _read_capability(self._artifacts, self._evidence, existing.artifact_digest)
            expected_verdict = "pass" if payload["write_verified"] else "blocked"
            if (
                existing.subject != authority.eda_profile_id
                or existing.verdict != expected_verdict
                or (
                    payload["write_verified"]
                    and payload["profile_id"] != authority.eda_profile_id
                )
            ):
                raise _blocked()
            return {
                "capability_digest": existing.artifact_digest,
                "write_verified": payload["write_verified"],
            }
        capability = self._adapter.probe()
        if capability.profile_id != authority.eda_profile_id and capability.write_verified:
            capability = replace(
                capability,
                write_verified=False,
                reason="authority_profile_mismatch",
            )
        self._assert_active(lease)
        staged = self._artifacts.stage_stream(
            io.BytesIO(capability_artifact_bytes(capability)), CAPABILITY_MEDIA_TYPE
        )
        descriptor = None
        try:
            self._assert_active(lease)
            descriptor = staged.publish()
            self._evidence.add_report(
                project_id=project_id,
                task_id=lease.task_id,
                descriptor=descriptor,
                kind=CAPABILITY_EVIDENCE_KIND,
                subject=authority.eda_profile_id,
                verdict="pass" if capability.write_verified else "blocked",
                lease_token=lease.lease_token,
                now=utc_now(),
            )
        except BaseException:
            if descriptor is None:
                staged.discard()
            else:
                self._evidence.settle_publication((descriptor,), (staged,))
            raise
        else:
            staged.discard()
        return {
            "capability_digest": descriptor.digest,
            "write_verified": capability.write_verified,
        }

    def _assert_active(self, lease: TaskLease) -> None:
        self._tasks.assert_active(lease.task_id, lease.lease_token, utc_now())


class CapabilityGateService:
    def __init__(
        self,
        projects: ProjectRepository,
        authorities: ProjectEdaAuthorityStore,
        tasks: TaskRepository,
        evidence: EvidenceRepository,
        artifacts: ContentAddressedStore,
    ) -> None:
        self._projects = projects
        self._authorities = authorities
        self._tasks = tasks
        self._evidence = evidence
        self._artifacts = artifacts

    def enqueue(self, project_id: str, idempotency_key: str) -> Task:
        validate_idempotency_key(idempotency_key)
        self._require_lceda_authority(project_id)
        return self._tasks.enqueue(
            LCEDA_CAPABILITY_TASK_KIND,
            {"project_id": project_id},
            idempotency_key,
            project_id,
        )

    def require_operation(
        self, project_id: str, eda_kind: EdaKind, operation: EdaOperation
    ) -> str:
        return self.require_operations(project_id, eda_kind, (operation,))

    def require_operations(
        self,
        project_id: str,
        eda_kind: EdaKind,
        operations: Sequence[EdaOperation],
    ) -> str:
        if (
            isinstance(operations, (str, bytes))
            or not isinstance(operations, Sequence)
            or not operations
            or any(not isinstance(operation, EdaOperation) for operation in operations)
            or len(set(operations)) != len(operations)
        ):
            raise _blocked()
        authority = self._require_lceda_authority(project_id)
        if eda_kind is not EdaKind.LCEDA_PRO or authority.eda_kind is not eda_kind:
            raise LcedaProCapabilityError("LCEDA_PRO_WRITE_CAPABILITY_UNVERIFIED")
        records = self._evidence.list_for_project(project_id)
        evidence = next(
            (item for item in reversed(records) if item.kind == CAPABILITY_EVIDENCE_KIND),
            None,
        )
        if evidence is None or evidence.subject != authority.eda_profile_id:
            raise _blocked()
        try:
            task = self._tasks.get(evidence.task_id)
            if (
                task.kind != LCEDA_CAPABILITY_TASK_KIND
                or task.project_id != project_id
                or task.status is not TaskStatus.SUCCEEDED
                or task.result is None
                or task.result.get("capability_digest") != evidence.artifact_digest
            ):
                raise ValueError("unbound capability evidence")
            payload = _read_capability(self._artifacts, self._evidence, evidence.artifact_digest)
            if (
                payload["profile_id"] != authority.eda_profile_id
                or evidence.verdict != "pass"
                or task.result.get("write_verified") is not payload["write_verified"]
                or payload["write_verified"] is not True
                or any(
                    operation.value not in payload["operations"]
                    for operation in operations
                )
            ):
                raise ValueError("unverified operation")
            return evidence.artifact_digest
        except Exception as error:
            if isinstance(error, LcedaProCapabilityError):
                raise
            raise _blocked() from error

    def _require_lceda_authority(self, project_id: str):
        self._projects.get(project_id)
        authority = self._authorities.find_by_project_id(project_id)
        if authority is None or authority.eda_kind is not EdaKind.LCEDA_PRO:
            raise RequestInvalidError("capability probes require lceda_pro authority")
        return authority

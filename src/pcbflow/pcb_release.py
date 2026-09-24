from __future__ import annotations

import csv
import hashlib
import io
import json
from collections.abc import Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from pcbflow.approvals import ApprovalDigestMismatchError, GateDecisionStore
from pcbflow.artifacts import ArtifactDescriptor, ContentAddressedStore, StagedArtifact
from pcbflow.board.adapter import CandidateWorkspace, PcbEdaAdapter, ReleaseArtifacts
from pcbflow.cancellation import TaskCancelledError
from pcbflow.canonical import canonical_json_bytes
from pcbflow.design_tables import GateDecisionRow, PcbCandidateRow
from pcbflow.domain import (
    EdaKind,
    EdaOperation,
    NormalizedFinding,
    ProjectMode,
    RequestInvalidError,
    Task,
    TaskLease,
    ValidationReport,
    new_id,
    utc_now,
)
from pcbflow.eda import validate_idempotency_key
from pcbflow.lceda_pro import LcedaProCapabilityError
from pcbflow.manufacturing import (
    REQUIRED_RELEASE_ARTIFACTS,
    ManufacturingValidator,
    ReleaseArtifact,
    ReleaseManifest,
)
from pcbflow.pcb_candidates import (
    G3_REQUIRED_EVIDENCE_MEDIA_TYPES,
    PCB_EXPORT_RELEASE_TASK_KIND,
    PcbCandidate,
    PcbCandidateNotFoundError,
    PcbCandidateNotReviewableError,
    PcbCandidateStatus,
    PcbCandidateStore,
    _load_candidate_rulepack,
    _validate_candidate_operations,
    deserialize_board_operations,
    validate_candidate_digest,
)
from pcbflow.repositories import EvidenceRepository, StaleLeaseError, TaskRepository
from pcbflow.tasks import TerminalTaskError

_G4_APPROVAL_MEDIA_TYPE = "application/vnd.pcbflow.g4-approval+json"
_NATIVE_RELEASE_KINDS = frozenset({"gerber", "drill", "bom", "cpl", "assembly"})
_FROZEN_RELEASE_KINDS = frozenset({"native_drc", "rulepack", "candidate_summary"})
_RELEASE_MEDIA_TYPES = {
    "gerber": "application/vnd.gerber",
    "drill": "application/vnd.excellon",
    "bom": "text/csv",
    "cpl": "text/csv",
    "assembly": "application/pdf",
}
_TRUE = frozenset({"1", "true", "yes", "y", "dnp", "hand_solder"})


class PcbReleaseCapabilityError(LcedaProCapabilityError):
    def __init__(self) -> None:
        super().__init__("PCB_RELEASE_CAPABILITY_BLOCKED")


@dataclass(frozen=True, slots=True)
class _ReleaseError(Exception):
    code: str
    message: str


@dataclass(slots=True)
class _ReleasePublication:
    descriptors: tuple[ArtifactDescriptor, ...]
    staged: tuple[StagedArtifact, ...]

    def discard(self) -> None:
        for artifact in self.staged:
            artifact.discard()

    def rollback(self) -> None:
        for artifact in self.staged:
            artifact.rollback()


class PcbReleaseService:
    """Reserve a reviewed candidate for native manufacturing export."""

    def __init__(
        self,
        candidates: PcbCandidateStore,
        capability_gate: Any,
    ) -> None:
        self._candidates = candidates
        self._capability_gate = capability_gate

    def enqueue_export(self, candidate_id: str, idempotency_key: str) -> Task:
        candidate = self._candidates.get(candidate_id)
        if candidate.status is not PcbCandidateStatus.G3_APPROVED:
            raise PcbCandidateNotReviewableError()
        self.require_export_capability(candidate)
        return self._candidates.enqueue_release_export(candidate_id, idempotency_key)

    def require_export_capability(self, candidate: PcbCandidate) -> None:
        try:
            digest = self._capability_gate.require_operation(
                candidate.project_id, EdaKind.LCEDA_PRO, EdaOperation.EXPORT_RELEASE
            )
        except LcedaProCapabilityError as error:
            raise PcbReleaseCapabilityError() from error
        if digest != candidate.capability_digest:
            raise PcbReleaseCapabilityError()


class PcbReleaseTaskHandler:
    def __init__(
        self,
        candidates: PcbCandidateStore,
        projects: Any,
        tasks: TaskRepository,
        evidence: EvidenceRepository,
        artifacts: ContentAddressedStore,
        capability_gate: Any,
        adapter: PcbEdaAdapter,
        workspaces_dir: Path,
        revisions: Any,
        clock=utc_now,
    ) -> None:
        self._candidates = candidates
        self._projects = projects
        self._tasks = tasks
        self._evidence = evidence
        self._artifacts = artifacts
        self._capability_gate = capability_gate
        self._adapter = adapter
        self._workspaces_dir = workspaces_dir
        self._revisions = revisions
        self._clock = clock

    def __call__(self, lease: TaskLease) -> dict[str, Any]:
        try:
            candidate = self._candidate_for_lease(lease)
        except _ReleaseError as error:
            raise TerminalTaskError(error.code, error.message) from error
        publication: _ReleasePublication | None = None
        try:
            self._require_capabilities(candidate)
            release_result, publication = self._export(candidate, lease)
            ready = self._candidates.mark_ready_for_g4(
                candidate.id,
                lease.task_id,
                lease.lease_token,
                self._clock(),
                release_result=release_result,
                descriptors=publication.descriptors,
            )
        except (TaskCancelledError, StaleLeaseError):
            if publication is not None:
                self._discard_or_rollback(publication)
            raise
        except PcbReleaseCapabilityError as error:
            if publication is not None:
                self._discard_or_rollback(publication)
            self._restore(candidate, lease, error.code)
            raise TerminalTaskError(error.code, str(error)) from error
        except _ReleaseError as error:
            if publication is not None:
                self._discard_or_rollback(publication)
            self._restore(candidate, lease, error.code)
            raise TerminalTaskError(error.code, error.message) from error
        except (FileNotFoundError, OSError, ValueError, TypeError) as error:
            if publication is not None:
                self._discard_or_rollback(publication)
            self._restore(candidate, lease, "PCB_RELEASE_EXPORT_FAILED")
            raise TerminalTaskError("PCB_RELEASE_EXPORT_FAILED", str(error)) from error
        if publication is not None:
            self._discard_or_rollback(publication)
        # mark_ready_for_g4 persisted release_result above, so result is set here.
        ready_result = cast("dict[str, Any]", ready.result)
        return {
            "candidate_id": candidate.id,
            "manifest_digest": ready_result["release"]["manifest_digest"],
            "status": PcbCandidateStatus.READY_FOR_G4.value,
        }

    def _discard_or_rollback(self, publication: _ReleasePublication) -> None:
        self._candidates.settle_release_publication(
            publication.descriptors,
            publication.staged,
        )

    def _restore(self, candidate: PcbCandidate, lease: TaskLease, code: str) -> None:
        try:
            self._candidates.restore_g3_after_release_failure(
                candidate.id,
                lease.task_id,
                lease.lease_token,
                self._clock(),
                error_code=code,
            )
        except (StaleLeaseError, PcbCandidateNotReviewableError):
            # A cancellation or lease takeover owns the durable outcome.
            pass

    def _candidate_for_lease(self, lease: TaskLease) -> PcbCandidate:
        candidate_id = lease.payload.get("candidate_id")
        project_id = lease.payload.get("project_id")
        if not isinstance(candidate_id, str) or not isinstance(project_id, str):
            raise _ReleaseError("PCB_RELEASE_NOT_FOUND", "release task has no candidate reference")
        try:
            candidate = self._candidates.get(candidate_id)
        except PcbCandidateNotFoundError as error:
            raise _ReleaseError("PCB_RELEASE_NOT_FOUND", "candidate was not found") from error
        release = candidate.result.get("release") if isinstance(candidate.result, dict) else None
        if (
            candidate.project_id != project_id
            or candidate.status is not PcbCandidateStatus.RELEASE_PENDING
            or not isinstance(release, dict)
            or release.get("task_id") != lease.task_id
        ):
            raise _ReleaseError("PCB_RELEASE_NOT_REVIEWABLE", "candidate is not reserved for this release task")
        return candidate

    def _require_capabilities(self, candidate: PcbCandidate) -> None:
        operations = (
            EdaOperation.SNAPSHOT,
            EdaOperation.CREATE_CANDIDATE,
            EdaOperation.APPLY_OPERATIONS,
            EdaOperation.RUN_DRC,
            EdaOperation.EXPORT_RELEASE,
        )
        try:
            require_many = getattr(self._capability_gate, "require_operations", None)
            if callable(require_many):
                digest = require_many(candidate.project_id, EdaKind.LCEDA_PRO, operations)
            else:
                values = {
                    self._capability_gate.require_operation(
                        candidate.project_id, EdaKind.LCEDA_PRO, operation
                    )
                    for operation in operations
                }
                if len(values) != 1:
                    raise LcedaProCapabilityError("LCEDA_PRO_WRITE_CAPABILITY_UNVERIFIED")
                digest = values.pop()
        except LcedaProCapabilityError as error:
            raise PcbReleaseCapabilityError() from error
        if digest != candidate.capability_digest:
            raise PcbReleaseCapabilityError()

    def _export(
        self, candidate: PcbCandidate, lease: TaskLease
    ) -> tuple[dict[str, Any], _ReleasePublication]:
        result = _frozen_result(candidate)
        frozen = {
            kind: _existing_descriptor(
                self._artifacts,
                self._evidence,
                result["evidence_artifacts"][kind],
                G3_REQUIRED_EVIDENCE_MEDIA_TYPES[kind],
            )
            for kind in _FROZEN_RELEASE_KINDS
        }
        _validate_frozen_native_drc(self._artifacts, frozen["native_drc"])
        rulepack, _algorithm_evidence, _rulepack_bytes = _load_candidate_rulepack(
            candidate
        )
        _validate_rulepack_descriptor(frozen["rulepack"], candidate)
        _validate_candidate_summary(self._artifacts, frozen["candidate_summary"], candidate)

        project = self._projects.get(candidate.project_id)
        managed_revision = project.mode is ProjectMode.MANAGED
        source_context = (
            self._revisions.materialize(
                project.id, candidate.base_revision, "pcb-release"
            )
            if managed_revision
            else nullcontext(project.source_path)
        )
        publication: _ReleasePublication | None = None
        with source_context as frozen_source:
            source_before = self._revisions.snapshot_digest(frozen_source)
            if (
                not managed_revision
                and candidate.base_snapshot_digest is not None
                and source_before != candidate.base_snapshot_digest
            ):
                raise _ReleaseError("PCB_CANDIDATE_SOURCE_CHANGED", "registered source tree changed after G3")
            self._tasks.assert_active(lease.task_id, lease.lease_token, self._clock())
            before = self._adapter.load_snapshot(frozen_source)
            before_digest = before.canonical_digest()
            if before_digest != candidate.board_snapshot_digest:
                raise _ReleaseError("PCB_CANDIDATE_INPUT_DIGEST_MISMATCH", "source BoardIR no longer matches the frozen candidate")
            operations = deserialize_board_operations(candidate.operations)
            _validate_candidate_operations(
                candidate,
                before,
                operations,
                expected_snapshot_digest=before_digest,
            )

            self._workspaces_dir.mkdir(parents=True, exist_ok=True)
            with TemporaryDirectory(prefix=f"pcb-release-{candidate.id}-", dir=self._workspaces_dir) as temporary:
                workspace = self._adapter.create_candidate(
                    frozen_source, Path(temporary) / "native-candidate"
                )
                self._tasks.assert_active(lease.task_id, lease.lease_token, self._clock())
                applied = self._adapter.apply_operations(workspace, operations, before)
                reread = self._adapter.load_snapshot(workspace.path)
                if reread != applied or reread.canonical_digest() != result["candidate_board_snapshot_digest"]:
                    raise _ReleaseError("PCB_RELEASE_CANDIDATE_MISMATCH", "recreated native candidate differs from G3 evidence")
                reports = self._adapter.run_drc(workspace)
                _validate_native_reports(reports)
                release = self._adapter.export_release(workspace, rulepack)
                exported = _read_release_files(release, workspace)

                validation = ManufacturingValidator().validate(
                    artifacts=exported,
                    required_artifacts=_NATIVE_RELEASE_KINDS,
                    dnp_designators=_flagged_designators(exported["bom"], "dnp"),
                    hand_solder_designators=_flagged_designators(exported["bom"], "handsolder"),
                )
                if not validation.ok:
                    raise _manufacturing_error(validation.findings)
                release_result, publication = self._stage_manifest(
                    candidate, exported, frozen
                )

            try:
                self._tasks.assert_active(lease.task_id, lease.lease_token, self._clock())
                source_after = self._revisions.snapshot_digest(frozen_source)
                if source_after != source_before:
                    raise _ReleaseError("PCB_CANDIDATE_SOURCE_CHANGED", "release export modified the frozen source tree")
            except BaseException:
                if publication is not None:
                    self._discard_or_rollback(publication)
                raise
        return release_result, publication

    def _stage_manifest(
        self,
        candidate: PcbCandidate,
        exported: Mapping[str, bytes],
        frozen: Mapping[str, ArtifactDescriptor],
    ) -> tuple[dict[str, Any], _ReleasePublication]:
        staged: dict[str, StagedArtifact] = {}
        published: list[tuple[ArtifactDescriptor, StagedArtifact]] = []
        # _export validated candidate.result via _frozen_result before staging.
        candidate_result = cast("dict[str, Any]", candidate.result)
        try:
            for kind in sorted(_NATIVE_RELEASE_KINDS):
                staged[kind] = self._artifacts.stage_stream(
                    io.BytesIO(exported[kind]), _RELEASE_MEDIA_TYPES[kind]
                )
            descriptors: dict[str, ArtifactDescriptor] = {}
            for kind in sorted(staged):
                descriptor = staged[kind].publish()
                descriptors[kind] = descriptor
                published.append((descriptor, staged[kind]))
            manifest = ReleaseManifest(
                schema_version="1.0",
                candidate_id=candidate.id,
                candidate_digest=candidate_result["candidate_digest"],
                authority_digest=candidate.authority_digest,
                capability_digest=candidate.capability_digest,
                rulepack_digest=candidate.rulepack_digest,
                artifacts=tuple(
                    ReleaseArtifact(
                        kind,
                        descriptor.digest,
                        descriptor.media_type,
                        descriptor.size,
                    )
                    for kind, descriptor in {**descriptors, **frozen}.items()
                ),
            )
            manifest_stage = self._artifacts.stage_stream(
                io.BytesIO(canonical_json_bytes(manifest.to_canonical_dict())),
                "application/vnd.pcbflow.release-manifest+json",
            )
            staged["manifest"] = manifest_stage
            manifest_descriptor = manifest_stage.publish()
            published.append((manifest_descriptor, manifest_stage))
            if manifest_descriptor.digest != manifest.canonical_digest():
                raise _ReleaseError("PCB_RELEASE_MANIFEST_INVALID", "manifest digest is not canonical")
            release_artifacts = {kind: descriptor.digest for kind, descriptor in {**descriptors, **frozen}.items()}
            return (
                {
                    "manifest_digest": manifest_descriptor.digest,
                    "artifacts": release_artifacts,
                    "candidate_digest": candidate_result["candidate_digest"],
                },
                _ReleasePublication(
                    descriptors=(*descriptors.values(), manifest_descriptor),
                    staged=tuple(staged.values()),
                ),
            )
        except BaseException:
            if published:
                self._candidates.settle_release_publication(
                    tuple(descriptor for descriptor, _artifact in published),
                    tuple(artifact for _descriptor, artifact in published),
                )
            for artifact in staged.values():
                artifact.discard()
            raise


class PcbReleaseApprovalService:
    """Record G4 decisions against the immutable release manifest."""

    def __init__(
        self,
        candidates: PcbCandidateStore,
        decisions: GateDecisionStore,
        artifacts: ContentAddressedStore,
        sessions: sessionmaker[Session],
    ) -> None:
        self._candidates = candidates
        self._decisions = decisions
        self._artifacts = artifacts
        self._sessions = sessions

    def decide_g4(
        self,
        *,
        candidate_id: str,
        manifest_digest: str,
        idempotency_key: str,
        actor_id: str,
        decision: str,
        comment: str,
    ) -> PcbCandidate:
        validate_idempotency_key(idempotency_key)
        validate_candidate_digest(manifest_digest, field="manifest_digest")
        if decision not in {"approve", "reject"}:
            raise RequestInvalidError("unsupported G4 decision")
        if not isinstance(actor_id, str) or not actor_id.strip():
            raise RequestInvalidError("actor_id must not be blank")
        if not isinstance(comment, str):
            raise RequestInvalidError("comment must be a string")
        candidate = self._candidates.get(candidate_id)
        normalized_actor = actor_id.strip()
        release = _release_payload(candidate)
        expected = release["manifest_digest"]
        if manifest_digest != expected:
            raise ApprovalDigestMismatchError(expected, manifest_digest)
        if not _manifest_matches_candidate(self._artifacts, candidate, manifest_digest):
            raise PcbCandidateNotReviewableError()
        values = {
            "gate": "G4_RELEASE",
            "subject_type": "pcb_candidate",
            "subject_id": candidate.id,
            "subject_digest": manifest_digest,
            "base_revision": candidate.base_revision,
            "idempotency_key": idempotency_key,
            "decision": decision,
            "actor_type": "human",
            "actor_id": normalized_actor,
            "comment": comment,
        }
        with self._sessions.begin() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row = session.get(PcbCandidateRow, candidate.id)
            if row is None:
                raise PcbCandidateNotReviewableError()
            gate_row = session.scalar(
                select(GateDecisionRow).where(
                    GateDecisionRow.project_id == candidate.project_id,
                    GateDecisionRow.idempotency_key == idempotency_key,
                )
            )
            if gate_row is not None:
                gate = GateDecisionStore._replay(gate_row, **values)
                persisted = row.result_json
                persisted_decision = (
                    persisted.get("g4_decision")
                    if isinstance(persisted, dict)
                    else None
                )
                if not isinstance(persisted_decision, dict):
                    raise PcbCandidateNotReviewableError()
                expected_payload = _g4_payload(
                    gate.id,
                    idempotency_key,
                    manifest_digest,
                    decision,
                    normalized_actor,
                    comment,
                    # The stored payload was produced by _g4_payload with a str
                    # digest and is compared for equality below.
                    cast(str, persisted_decision.get("approval_artifact_digest")),
                )
                expected_status = (
                    PcbCandidateStatus.RELEASED.value
                    if decision == "approve"
                    else PcbCandidateStatus.READY_FOR_G4.value
                )
                if (
                    row.status != expected_status
                    or persisted_decision != expected_payload
                ):
                    raise PcbCandidateNotReviewableError()
            else:
                if row.status != PcbCandidateStatus.READY_FOR_G4.value:
                    raise PcbCandidateNotReviewableError()
                now = utc_now()
                gate_id = new_id("gdec")
                approval = self._approval_artifact(
                    candidate,
                    gate_id,
                    now,
                    decision,
                    normalized_actor,
                    comment,
                    manifest_digest,
                )
                GateDecisionStore._register_artifact(session, approval, now)
                session.add(
                    GateDecisionRow(
                        id=gate_id,
                        project_id=candidate.project_id,
                        created_at=now,
                        **values,
                    )
                )
                result = dict(row.result_json or {})
                release_result = dict(result.get("release") or {})
                result["release"] = {
                    **release_result,
                    "status": "released" if decision == "approve" else "ready_for_g4",
                }
                result["g4_decision"] = _g4_payload(
                    gate_id, idempotency_key, manifest_digest, decision, normalized_actor, comment, approval.digest
                )
                row.status = (
                    PcbCandidateStatus.RELEASED.value
                    if decision == "approve"
                    else PcbCandidateStatus.READY_FOR_G4.value
                )
                row.result_json = result
                row.last_error_code = None if decision == "approve" else "G4_REJECTED"
                row.updated_at = now
                row.version += 1
        return self._candidates.get(candidate.id)

    def _approval_artifact(
        self,
        candidate: PcbCandidate,
        gate_id: str,
        created_at: datetime,
        decision: str,
        actor_id: str,
        comment: str,
        manifest_digest: str,
    ) -> ArtifactDescriptor:
        return self._artifacts.put_bytes(
            canonical_json_bytes(
                {
                    "schema_version": "1.0",
                    "gate": "G4_RELEASE",
                    "gate_decision_id": gate_id,
                    "project_id": candidate.project_id,
                    "candidate_id": candidate.id,
                    "manifest_digest": manifest_digest,
                    "base_revision": candidate.base_revision,
                    "decision": decision,
                    "actor": {"type": "human", "id": actor_id},
                    "comment": comment,
                    "created_at": created_at.isoformat().replace("+00:00", "Z"),
                }
            ),
            _G4_APPROVAL_MEDIA_TYPE,
        )


def _frozen_result(candidate: PcbCandidate) -> dict[str, Any]:
    if not isinstance(candidate.result, dict):
        raise _ReleaseError("PCB_RELEASE_NOT_REVIEWABLE", "candidate result is missing")
    result = candidate.result
    g3 = result.get("g3_decision")
    artifacts = result.get("evidence_artifacts")
    if (
        not isinstance(g3, dict)
        or g3.get("decision") != "approve"
        or not isinstance(artifacts, dict)
        or any(not isinstance(artifacts.get(kind), str) for kind in _FROZEN_RELEASE_KINDS)
        or not isinstance(result.get("candidate_digest"), str)
        or not isinstance(result.get("candidate_board_snapshot_digest"), str)
    ):
        raise _ReleaseError("PCB_RELEASE_NOT_REVIEWABLE", "G3 evidence is incomplete")
    return result


def _existing_descriptor(
    artifacts: ContentAddressedStore,
    evidence: EvidenceRepository,
    digest: str,
    expected_media_type: str,
) -> ArtifactDescriptor:
    if evidence.artifact_media_type(digest) != expected_media_type or not artifacts.verify(digest):
        raise _ReleaseError("PCB_RELEASE_EVIDENCE_TAMPERED", "frozen G3 evidence failed integrity verification")
    path = artifacts._path(digest)
    return ArtifactDescriptor(digest, path.stat().st_size, expected_media_type, path)


def _validate_frozen_native_drc(
    artifacts: ContentAddressedStore, descriptor: ArtifactDescriptor
) -> None:
    payload = _canonical_json(artifacts, descriptor.digest)
    if type(payload) is not dict or payload.get("schema_version") != "1.0":
        raise _ReleaseError("PCB_RELEASE_EVIDENCE_TAMPERED", "native DRC evidence is invalid")
    reports = payload.get("reports")
    if type(reports) is not list or not reports:
        raise _ReleaseError("PCB_RELEASE_EVIDENCE_TAMPERED", "native DRC evidence is missing")
    for report in reports:
        if type(report) is not dict or type(report.get("findings")) is not list:
            raise _ReleaseError("PCB_RELEASE_EVIDENCE_TAMPERED", "native DRC report is invalid")
        if any(
            type(finding) is not dict
            or str(finding.get("severity", "")).casefold()
            in {"error", "critical", "fatal", "blocker"}
            for finding in report["findings"]
        ):
            raise _ReleaseError("PCB_NATIVE_DRC_BLOCKED", "native DRC has blocking findings")


def _validate_rulepack_descriptor(
    descriptor: ArtifactDescriptor,
    candidate: PcbCandidate,
) -> None:
    if descriptor.digest != candidate.rulepack_digest:
        raise _ReleaseError("PCB_RELEASE_EVIDENCE_TAMPERED", "rulepack does not match candidate")


def _validate_candidate_summary(
    artifacts: ContentAddressedStore,
    descriptor: ArtifactDescriptor,
    candidate: PcbCandidate,
) -> None:
    # Callers validated candidate.result via _frozen_result before this check.
    candidate_result = cast("dict[str, Any]", candidate.result)
    payload = _canonical_json(artifacts, descriptor.digest)
    if type(payload) is not dict or any(
        payload.get(key) != value
        for key, value in {
            "candidate_id": candidate.id,
            "project_id": candidate.project_id,
            "base_revision": candidate.base_revision,
            "board_snapshot_digest": candidate.board_snapshot_digest,
            "candidate_board_snapshot_digest": candidate_result["candidate_board_snapshot_digest"],
            "rulepack_digest": candidate.rulepack_digest,
            "capability_digest": candidate.capability_digest,
            "authority_digest": candidate.authority_digest,
            "operations_digest": candidate.operations_digest,
        }.items()
    ):
        raise _ReleaseError("PCB_RELEASE_EVIDENCE_TAMPERED", "candidate summary does not match frozen candidate")


def _validate_native_reports(reports: object) -> None:
    if type(reports) is not tuple or not reports or any(
        not isinstance(report, ValidationReport)
        or type(report.kind) is not str
        or not report.kind.strip()
        or type(report.findings) is not tuple
        or any(not isinstance(finding, NormalizedFinding) for finding in report.findings)
        for report in reports
    ):
        raise _ReleaseError("PCB_NATIVE_DRC_REPORT_INVALID", "native DRC returned a malformed report")
    if any(
        finding.severity.casefold() in {"error", "critical", "fatal", "blocker"}
        for report in reports
        for finding in report.findings
    ):
        raise _ReleaseError("PCB_NATIVE_DRC_BLOCKED", "native DRC reported blocking findings")


def _read_release_files(
    release: object, workspace: CandidateWorkspace
) -> dict[str, bytes]:
    if not isinstance(release, ReleaseArtifacts) or type(release.files) is not tuple:
        raise _ReleaseError("PCB_RELEASE_EXPORT_INVALID", "adapter returned invalid release artifacts")
    files: dict[str, bytes] = {}
    output_root = workspace.output_dir.resolve()
    for item in release.files:
        if type(item) is not tuple or len(item) != 2:
            raise _ReleaseError("PCB_RELEASE_EXPORT_INVALID", "release file entry is invalid")
        kind, path = item
        if (
            not isinstance(kind, str)
            or kind not in _NATIVE_RELEASE_KINDS
            or not isinstance(path, Path)
        ):
            raise _ReleaseError("PCB_RELEASE_EXPORT_INVALID", "release file kind is unsupported")
        resolved = path.resolve()
        if not resolved.is_relative_to(output_root) or not resolved.is_file() or kind in files:
            raise _ReleaseError("PCB_RELEASE_EXPORT_INVALID", "release file is outside the candidate output")
        files[kind] = resolved.read_bytes()
    missing = _NATIVE_RELEASE_KINDS - set(files)
    if missing:
        raise _ReleaseError(
            "MANUFACTURING_ARTIFACT_MISSING",
            "release export is missing: " + ", ".join(sorted(missing)),
        )
    return files


def _flagged_designators(data: bytes, field: str) -> frozenset[str]:
    try:
        reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig")))
        if reader.fieldnames is None:
            return frozenset()
        columns = {name.strip().casefold(): name for name in reader.fieldnames if name}
        designator = columns.get("designator")
        target = columns.get(field.casefold())
        if designator is None or target is None:
            return frozenset()
        return frozenset(
            str(row.get(designator) or "").strip()
            for row in reader
            if str(row.get(target) or "").strip().casefold() in _TRUE
            and str(row.get(designator) or "").strip()
        )
    except (UnicodeDecodeError, csv.Error):
        return frozenset()


def _manufacturing_error(findings: Sequence[NormalizedFinding]) -> _ReleaseError:
    rule_ids = {finding.rule_id for finding in findings}
    if "PCB.MFG.ARTIFACT_MISSING" in rule_ids:
        code = "MANUFACTURING_ARTIFACT_MISSING"
    elif "PCB.MFG.REFERENCE_SET_MISMATCH" in rule_ids:
        code = "MANUFACTURING_REFERENCE_SET_MISMATCH"
    else:
        code = "MANUFACTURING_VALIDATION_FAILED"
    return _ReleaseError(code, "; ".join(finding.message for finding in findings))


def _canonical_json(artifacts: ContentAddressedStore, digest: str) -> Any:
    try:
        with artifacts.open(digest) as stream:
            raw = stream.read()
        if f"sha256:{hashlib.sha256(raw).hexdigest()}" != digest:
            raise ValueError("digest mismatch")
        payload = json.loads(raw.decode("utf-8"))
        if raw != canonical_json_bytes(payload):
            raise ValueError("noncanonical JSON")
        return payload
    except (OSError, UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise _ReleaseError("PCB_RELEASE_EVIDENCE_TAMPERED", "release artifact is unreadable") from error


def _release_payload(candidate: PcbCandidate) -> dict[str, Any]:
    if not isinstance(candidate.result, dict) or not isinstance(candidate.result.get("release"), dict):
        raise PcbCandidateNotReviewableError()
    release = candidate.result["release"]
    if not isinstance(release.get("manifest_digest"), str):
        raise PcbCandidateNotReviewableError()
    return release


def _manifest_matches_candidate(
    artifacts: ContentAddressedStore, candidate: PcbCandidate, manifest_digest: str
) -> bool:
    try:
        if candidate.status not in {PcbCandidateStatus.READY_FOR_G4, PcbCandidateStatus.RELEASED}:
            return False
        release = _release_payload(candidate)
        if release["manifest_digest"] != manifest_digest or not artifacts.verify(manifest_digest):
            return False
        payload = _canonical_json(artifacts, manifest_digest)
        if type(payload) is not dict or set(payload) != {
            "schema_version", "candidate_id", "candidate_digest", "authority_digest",
            "capability_digest", "rulepack_digest", "artifacts",
        }:
            return False
        parsed = ReleaseManifest(
            schema_version=payload["schema_version"],
            candidate_id=payload["candidate_id"],
            candidate_digest=payload["candidate_digest"],
            authority_digest=payload["authority_digest"],
            capability_digest=payload["capability_digest"],
            rulepack_digest=payload["rulepack_digest"],
            artifacts=tuple(
                ReleaseArtifact(
                    kind=item["kind"],
                    digest=item["digest"],
                    media_type=item["media_type"],
                    size=item["size"],
                )
                for item in payload["artifacts"]
            ),
        )
        if parsed.canonical_digest() != manifest_digest:
            return False
        if (
            parsed.candidate_id != candidate.id
            or not isinstance(candidate.result, dict)
            or parsed.candidate_digest != candidate.result.get("candidate_digest")
            or parsed.authority_digest != candidate.authority_digest
            or parsed.capability_digest != candidate.capability_digest
            or parsed.rulepack_digest != candidate.rulepack_digest
            or {item.kind for item in parsed.artifacts} != REQUIRED_RELEASE_ARTIFACTS
        ):
            return False
        expected_digests = release.get("artifacts")
        if not isinstance(expected_digests, dict):
            return False
        for item in parsed.artifacts:
            if expected_digests.get(item.kind) != item.digest or not artifacts.verify(item.digest):
                return False
            path = artifacts._path(item.digest)
            if path.stat().st_size != item.size:
                return False
        return True
    except (OSError, TypeError, ValueError, _ReleaseError):
        return False


def _g4_payload(
    gate_id: str,
    idempotency_key: str,
    manifest_digest: str,
    decision: str,
    actor_id: str,
    comment: str,
    approval_digest: str,
) -> dict[str, str]:
    return {
        "gate_decision_id": gate_id,
        "idempotency_key": idempotency_key,
        "subject_digest": manifest_digest,
        "decision": decision,
        "actor_id": actor_id,
        "comment": comment,
        "approval_artifact_digest": approval_digest,
    }


__all__ = [
    "PCB_EXPORT_RELEASE_TASK_KIND",
    "PcbReleaseApprovalService",
    "PcbReleaseCapabilityError",
    "PcbReleaseService",
    "PcbReleaseTaskHandler",
]

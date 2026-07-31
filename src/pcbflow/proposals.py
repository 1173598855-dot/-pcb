from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import os
import stat
import tempfile
import hashlib
from pathlib import Path
from enum import StrEnum
from typing import TYPE_CHECKING, Literal
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from pcbflow.artifacts import ArtifactDescriptor
from pcbflow.canonical import canonical_digest, canonical_json_bytes
from pcbflow.commands import ValidationKind, evaluate_precondition, PreconditionContext
from pcbflow.commands import load_command_batch
from pcbflow.domain import ProjectMode
from pcbflow.repositories import (
    IdempotencyConflictError,
    ProjectRepository,
    RevisionConflictError,
)
from pcbflow.requirement_store import RequirementStore
from pcbflow.requirements import RequirementSetStatus
from pcbflow.tasks import TerminalTaskError
from pcbflow.repositories import StaleLeaseError, TaskRepository
from pcbflow.domain import TaskLease
from pcbflow.revisions import RevisionService
from pcbflow.schematic.adapter import CstSchematicAdapter, CommandResult
from pcbflow.schematic.diff import CommandAttribution, build_semantic_diff, semantic_diff_bytes
from pcbflow.schematic.semantic import SchematicDocument, object_ref_key
from pcbflow.kicad import KicadPort, KicadCapability, parse_kicad_report, KicadUnavailableError, KicadProjectNotFoundError, KicadToolError
from pcbflow.repositories import ProjectRepository
from pcbflow.validation import assert_project_tree_safe

if TYPE_CHECKING:
    from pcbflow.proposal_store import ProposalStore


DESIGN_PROPOSAL_TASK_KIND = "design.execute_proposal"


class ProposalStatus(StrEnum):
    QUEUED = "queued"
    EXECUTING = "executing"
    VALIDATION_FAILED = "validation_failed"
    READY_FOR_REVIEW = "ready_for_review"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class ChangeProposal:
    id: str
    project_id: str
    command_batch_id: str
    task_id: str
    status: ProposalStatus
    candidate_revision: str | None
    candidate_snapshot_digest: str | None
    review_digest: str | None
    semantic_diff_digest: str | None
    evidence_set_digest: str | None
    result: dict[str, object] | None
    last_error_code: str | None
    created_at: datetime
    updated_at: datetime
    version: int


class EvidenceItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    kind: str
    artifact_digest: str
    media_type: str
    verdict: str


class EvidenceSet(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal["1.0"] = "1.0"
    project_id: str
    task_id: str
    proposal_id: str
    base_revision: str
    candidate_revision: str | None
    artifacts: tuple[EvidenceItem, ...]


READY_EVIDENCE_MEDIA_TYPES = {
    "design_command_batch": "application/json",
    "project_snapshot_before": "application/json",
    "project_snapshot_after": "application/json",
    "git_text_diff": "application/octet-stream",
    "schematic_semantic_diff": "application/json",
    "kicad_erc": "application/json",
    "command_execution_log": "application/json",
    "adapter_capability_report": "application/json",
}
READY_EVIDENCE_KINDS = frozenset(
    (*READY_EVIDENCE_MEDIA_TYPES, "proposal_evidence_set")
)


@dataclass(frozen=True, slots=True)
class EvidenceRegistration:
    descriptor: ArtifactDescriptor
    item: EvidenceItem

    def __post_init__(self) -> None:
        if self.descriptor.digest != self.item.artifact_digest:
            raise ValueError("evidence digest does not match descriptor")
        if self.descriptor.media_type != self.item.media_type:
            raise ValueError("evidence media type does not match descriptor")


MANDATORY_VALIDATIONS = frozenset(ValidationKind)


def proposal_review_digest(*, proposal_id: str, project_id: str, base_revision: str,
                           candidate_revision: str, candidate_snapshot_digest: str,
                           requirement_set_digest: str, semantic_diff_digest: str,
                           evidence_set_digest: str, adapter_capability_digest: str) -> str:
    return canonical_digest({"schema_version": "1.0", "proposal_id": proposal_id,
        "project_id": project_id, "base_revision": base_revision,
        "candidate_revision": candidate_revision,
        "candidate_snapshot_digest": candidate_snapshot_digest,
        "requirement_set_digest": requirement_set_digest,
        "semantic_diff_digest": semantic_diff_digest,
        "evidence_set_digest": evidence_set_digest,
        "adapter_capability_digest": adapter_capability_digest})


class ProjectNotManagedError(RuntimeError):
    pass


class ProposalService:
    def __init__(
        self,
        projects: ProjectRepository,
        requirements: RequirementStore,
        store: ProposalStore,
    ) -> None:
        self._projects = projects
        self._requirements = requirements
        self._store = store

    def create(self, data: bytes, idempotency_key: str) -> ChangeProposal:
        batch = load_command_batch(data)
        if batch.idempotency_key != idempotency_key:
            raise IdempotencyConflictError(idempotency_key)
        existing = self._store.find_existing(batch)
        if existing is not None:
            return existing
        project = self._projects.get(batch.project_id)
        if project.mode is not ProjectMode.MANAGED:
            raise ProjectNotManagedError(project.id)
        if project.current_revision != batch.base_revision:
            raise RevisionConflictError(batch.base_revision, project.current_revision)
        requirement_set = self._requirements.get(batch.requirement_set_id)
        if (
            requirement_set.project_id != project.id
            or requirement_set.status is not RequirementSetStatus.FROZEN
            or project.active_requirement_set_id != requirement_set.id
        ):
            raise ValueError("batch must use the active frozen requirement set")
        return self._store.create_queued(batch)


class _PreconditionContext:
    def __init__(self, document: SchematicDocument, revision: str, requirements_digest: str, capability: KicadCapability) -> None:
        self.current_revision = revision
        self.requirements_digest = requirements_digest
        self._document = document
        self._capability = capability

    def has_object(self, reference):
        key = object_ref_key(reference)
        return any(object_ref_key(item.ref) == key for collection in (self._document.sheets, self._document.symbols, self._document.labels, self._document.nets) for item in collection)

    def property_value(self, reference, name):
        for symbol in self._document.symbols:
            if object_ref_key(symbol.ref) == object_ref_key(reference):
                return {item.name: item.value for item in symbol.properties}.get(name)
        return None

    def has_module(self, instance_name):
        return any(sheet.name == instance_name for sheet in self._document.sheets)

    def has_capability(self, capability):
        return capability == "kicad.cst.write.v1" and self._capability.available


class ProposalExecutor:
    def __init__(self, *, proposal_store, command_batches, projects, requirements, tasks: TaskRepository,
                 revisions: RevisionService, adapter: CstSchematicAdapter, kicad: KicadPort,
                 artifacts, evidence, clock, max_files: int = 10000,
                 max_bytes: int = 512 * 1024 * 1024) -> None:
        self._proposal_store = proposal_store; self._command_batches = command_batches; self._projects = projects
        self._requirements = requirements; self._tasks = tasks; self._revisions = revisions; self._adapter = adapter
        self._kicad = kicad; self._artifacts = artifacts; self._evidence = evidence; self._clock = clock
        self._max_files = max_files; self._max_bytes = max_bytes

    def begin(self, lease: TaskLease, *, now: datetime | None = None) -> ChangeProposal:
        now = self._clock() if now is None else now
        self._tasks.assert_active(lease.task_id, lease.lease_token, now)
        return self._proposal_store.begin_execution(str(lease.payload["proposal_id"]), lease.task_id, lease.lease_token, now)

    @staticmethod
    def _manifest(root: Path, revisions: RevisionService) -> bytes:
        files = []
        for path, relative, metadata in revisions_module_snapshot_files(root):
            files.append({"path": relative, "type": "file", "size": metadata.st_size, "digest": f"sha256:{__import__('hashlib').sha256(path.read_bytes()).hexdigest()}"})
        return canonical_json_bytes({"schema_version": "1.0", "snapshot_policy_version": 1, "files": files})

    def _put(self, data: bytes, media_type: str):
        return self._artifacts.put_bytes(data, media_type)

    def __call__(self, lease: TaskLease) -> dict[str, object]:
        now = self._clock(); proposal_id = str(lease.payload["proposal_id"])
        proposal = self._proposal_store.get(proposal_id)
        batch = self._command_batches.get(proposal.command_batch_id)
        project = self._projects.get(batch.project_id)
        requirements = self._requirements.get(batch.requirement_set_id)
        self._tasks.assert_active(lease.task_id, lease.lease_token, now)
        if proposal.status is ProposalStatus.READY_FOR_REVIEW and proposal.result is not None and self._revisions.resolve_proposal_ref(project.id, proposal.id) == proposal.candidate_revision:
            records = [item for item in self._evidence.list_for_project(project.id) if item.task_id == lease.task_id]
            by_kind = {item.kind: item for item in records}
            valid_contract = (
                proposal.candidate_revision is not None
                and proposal.candidate_snapshot_digest is not None
                and proposal.review_digest is not None
                and proposal.semantic_diff_digest is not None
                and proposal.evidence_set_digest is not None
                and len(records) == len(READY_EVIDENCE_KINDS)
                and set(by_kind) == READY_EVIDENCE_KINDS
                and all(
                    item.verdict == "pass"
                    and item.subject == f"{proposal.id}@{proposal.candidate_revision}"
                    and self._artifacts.verify(item.artifact_digest)
                    for item in records
                )
            )
            evidence_row = by_kind.get("proposal_evidence_set")
            if evidence_row is None or evidence_row.artifact_digest != proposal.evidence_set_digest:
                valid_contract = False
            try:
                evidence_set = EvidenceSet.model_validate_json(
                    self._artifacts.open(proposal.evidence_set_digest or "").read(), strict=True
                )
                evidence_items = {item.kind: item for item in evidence_set.artifacts}
                valid_contract = valid_contract and all((
                    evidence_set.project_id == project.id,
                    evidence_set.task_id == lease.task_id,
                    evidence_set.proposal_id == proposal.id,
                    evidence_set.base_revision == batch.base_revision,
                    evidence_set.candidate_revision == proposal.candidate_revision,
                    len(evidence_set.artifacts) == len(READY_EVIDENCE_MEDIA_TYPES),
                    len(evidence_items) == len(READY_EVIDENCE_MEDIA_TYPES),
                    set(evidence_items) == set(READY_EVIDENCE_MEDIA_TYPES),
                ))
                valid_contract = valid_contract and all(
                    by_kind[kind].artifact_digest == item.artifact_digest
                    and by_kind[kind].kind == item.kind
                    and by_kind[kind].verdict == item.verdict == "pass"
                    and item.media_type == READY_EVIDENCE_MEDIA_TYPES[kind]
                    for kind, item in evidence_items.items()
                )
                expected_review = proposal_review_digest(
                    proposal_id=proposal.id,
                    project_id=project.id,
                    base_revision=batch.base_revision,
                    candidate_revision=proposal.candidate_revision or "",
                    candidate_snapshot_digest=proposal.candidate_snapshot_digest or "",
                    requirement_set_digest=requirements.canonical_digest,
                    semantic_diff_digest=proposal.semantic_diff_digest or "",
                    evidence_set_digest=proposal.evidence_set_digest or "",
                    adapter_capability_digest=evidence_items["adapter_capability_report"].artifact_digest,
                )
                valid_contract = valid_contract and all((
                    evidence_items["schematic_semantic_diff"].artifact_digest == proposal.semantic_diff_digest,
                    proposal.review_digest == expected_review,
                    proposal.result.get("proposal_id") == proposal.id,
                    proposal.result.get("candidate_revision") == proposal.candidate_revision,
                    proposal.result.get("review_digest") == proposal.review_digest,
                    proposal.result.get("evidence_set_digest") == proposal.evidence_set_digest,
                    proposal.result.get("semantic_diff_digest") == proposal.semantic_diff_digest,
                ))
            except Exception:
                valid_contract = False
            if not valid_contract:
                raise TerminalTaskError("CANDIDATE_VALIDATION_FAILED", "stored proposal evidence integrity check failed")
            return proposal.result
        if proposal.status is ProposalStatus.VALIDATION_FAILED:
            raise TerminalTaskError(proposal.last_error_code or "CANDIDATE_VALIDATION_FAILED", "proposal validation already failed")
        self.begin(lease, now=now)
        evidence: list[EvidenceRegistration] = []
        capability: KicadCapability | None = None
        candidate_revision: str | None = None
        def add(kind, data, media="application/octet-stream", verdict="pass"):
            descriptor = self._put(data, media); evidence.append(EvidenceRegistration(descriptor, EvidenceItem(kind=kind, artifact_digest=descriptor.digest, media_type=media, verdict=verdict))); return descriptor
        def replace(kind, descriptor, verdict="pass"):
            registration = EvidenceRegistration(descriptor, EvidenceItem(kind=kind, artifact_digest=descriptor.digest, media_type=descriptor.media_type, verdict=verdict))
            for index, item in enumerate(evidence):
                if item.item.kind == kind:
                    evidence[index] = registration
                    return descriptor
            evidence.append(registration)
            return descriptor
        def add_failed_evidence_set():
            evidence[:] = [
                item for item in evidence
                if item.item.kind != "proposal_evidence_set"
            ]
            failed_set = EvidenceSet(
                project_id=project.id,
                task_id=lease.task_id,
                proposal_id=proposal_id,
                base_revision=batch.base_revision,
                candidate_revision=None,
                artifacts=tuple(item.item for item in evidence),
            )
            return add(
                "proposal_evidence_set",
                canonical_json_bytes(failed_set.model_dump(mode="json")),
                "application/json",
                "fail",
            )
        try:
            add("design_command_batch", canonical_json_bytes(batch.model_dump(mode="json")), "application/json")
            capability = self._kicad.probe()
            capability_descriptor = add("adapter_capability_report", canonical_json_bytes({
                "adapter_contract": "pcbflow.schematic.cst.v1", "kicad_major": 9,
                "kicad": {"available": capability.available, "version": capability.version,
                          "executable_digest": capability.executable_digest, "reason": capability.reason},
            }), "application/json")
            add("command_execution_log", canonical_json_bytes({"stage": "preflight", "preconditions": []}), "application/json")
            with self._revisions.materialize(project.id, batch.base_revision, "proposal") as workspace:
                self._revisions.assert_clean(project.id, batch.base_revision, workspace)
                before_manifest = add("project_snapshot_before", self._manifest(workspace, self._revisions), "application/json")
                before = self._adapter.inspect(workspace)
                assert capability is not None
                context = _PreconditionContext(before, batch.base_revision, requirements.canonical_digest, capability)
                results = [evaluate_precondition(precondition, context) for command in batch.commands for precondition in command.preconditions]
                execution_descriptor = self._put(canonical_json_bytes({"preconditions": [result.model_dump(mode="json") for result in results]}), "application/json")
                replace("command_execution_log", execution_descriptor)
                if not capability.available or not capability.version or int(capability.version.split(".", 1)[0]) != 9:
                    raise TerminalTaskError("KICAD_CLI_UNAVAILABLE", capability.reason or "KiCad 9 is required")
                if project.mode is not ProjectMode.MANAGED or project.current_revision != batch.base_revision or requirements.status is not RequirementSetStatus.FROZEN or project.active_requirement_set_id != requirements.id:
                    raise TerminalTaskError("DESIGN_COMMAND_PRECONDITION_FAILED", "proposal base is no longer current")
                if requirements.frozen_revision is None or not self._revisions.is_ancestor(project.id, requirements.frozen_revision, batch.base_revision):
                    raise TerminalTaskError("DESIGN_COMMAND_PRECONDITION_FAILED", "requirement revision is not an ancestor")
                if any(result.state.value != "true" for result in results):
                    raise TerminalTaskError("DESIGN_COMMAND_PRECONDITION_FAILED", "a design command precondition failed")
                applied = self._adapter.apply(workspace, batch.commands)
                after = applied.after
                applied_capability_descriptor = self._put(canonical_json_bytes({"adapter_contract": applied.capability_report.adapter_contract, "kicad_major": applied.capability_report.kicad_major, "supported_operations": applied.capability_report.supported_operations, "module_digests": applied.capability_report.module_digests}), "application/json")
                replace("adapter_capability_report", applied_capability_descriptor)
                capability_descriptor = applied_capability_descriptor
                for modified_path in applied.modified_files:
                    candidate_path = workspace / modified_path
                    try:
                        candidate_path.resolve(strict=False).relative_to(workspace.resolve())
                    except ValueError as error:
                        raise TerminalTaskError("PROJECT_PATH_OUTSIDE_WORKTREE", "adapter modified a path outside the worktree") from error
                assert_project_tree_safe(workspace, max_files=self._max_files, max_bytes=self._max_bytes)
                attributions = tuple(CommandAttribution(command_id=result.command_id, requirement_ids=next(c.provenance.requirement_ids for c in batch.commands if c.command_id == result.command_id), risk=next(c.risk for c in batch.commands if c.command_id == result.command_id), selectors=result.effects) for result in applied.command_results)
                semantic = build_semantic_diff(before, after, attributions)
                if not semantic.changes:
                    raise TerminalTaskError("DESIGN_COMMAND_NO_EFFECT", "design commands produced no semantic change")
                semantic_descriptor = add("schematic_semantic_diff", semantic_diff_bytes(semantic), "application/json")
                reports = self._kicad.validate(workspace, workspace.parent / "validation-output")
                ercs = [report for report in reports if report.kind == "erc"]
                if len(ercs) != 1: raise TerminalTaskError("CANDIDATE_VALIDATION_FAILED", "exactly one ERC report is required")
                erc_descriptor = add("kicad_erc", ercs[0].data, "application/json", "pass")
                parsed = parse_kicad_report("erc", ercs[0].data)
                if parsed.findings:
                    evidence[-1] = EvidenceRegistration(erc_descriptor, EvidenceItem(kind="kicad_erc", artifact_digest=erc_descriptor.digest, media_type="application/json", verdict="fail"))
                after_descriptor = add("project_snapshot_after", self._manifest(workspace, self._revisions), "application/json")
                diff_bytes = self._revisions.git.diff_worktree(workspace)
                diff_descriptor = add("git_text_diff", diff_bytes, "application/octet-stream")
                if parsed.findings:
                    raise TerminalTaskError("CANDIDATE_VALIDATION_FAILED", "KiCad ERC reported findings")
                self._tasks.assert_active(lease.task_id, lease.lease_token, self._clock())
                actor_name = f"PCBFlow {batch.actor.type}:{batch.actor.id}".replace("\r", "_").replace("\n", "_")
                actor_email = f"pcbflow+{hashlib.sha256(f'{batch.actor.type}:{batch.actor.id}'.encode()).hexdigest()[:24]}@local.invalid"
                candidate = self._revisions.commit_candidate(project, workspace, batch.base_revision, f"refs/pcbflow/proposals/{proposal_id}", f"pcbflow: proposal {proposal_id}", self._command_batches.created_at(batch.batch_id), publish_ref=False, author_name=actor_name, author_email=actor_email)
                candidate_revision = candidate.revision
                artifacts = tuple(item.item for item in evidence)
                evidence_set = EvidenceSet(project_id=project.id, task_id=lease.task_id, proposal_id=proposal_id, base_revision=batch.base_revision, candidate_revision=candidate.revision, artifacts=artifacts)
                evidence_set_descriptor = add("proposal_evidence_set", canonical_json_bytes(evidence_set.model_dump(mode="json")), "application/json")
                evidence_set_digest = evidence_set_descriptor.digest
                if not all(self._artifacts.verify(item.item.artifact_digest) for item in evidence):
                    raise TerminalTaskError("CANDIDATE_VALIDATION_FAILED", "candidate evidence integrity check failed")
                review = proposal_review_digest(proposal_id=proposal_id, project_id=project.id, base_revision=batch.base_revision, candidate_revision=candidate.revision, candidate_snapshot_digest=candidate.snapshot_digest, requirement_set_digest=requirements.canonical_digest, semantic_diff_digest=semantic_descriptor.digest, evidence_set_digest=evidence_set_digest, adapter_capability_digest=capability_descriptor.digest)
                result = {"proposal_id": proposal_id, "candidate_revision": candidate.revision, "review_digest": review, "semantic_diff_digest": semantic_descriptor.digest, "evidence_set_digest": evidence_set_digest, "evidence_ids": []}
                self._tasks.assert_active(lease.task_id, lease.lease_token, self._clock())
                self._revisions.publish_candidate_ref(project.id, f"refs/pcbflow/proposals/{proposal_id}", candidate.revision)
                self._tasks.assert_active(lease.task_id, lease.lease_token, self._clock())
                self._proposal_store.mark_ready(proposal_id, lease.task_id, lease.lease_token, self._clock(), candidate.revision, candidate.snapshot_digest, review, semantic_descriptor.digest, evidence_set_digest, result, tuple(evidence))
                return result
        except TerminalTaskError as error:
            evidence_set_digest = add_failed_evidence_set().digest
            digest_map = {item.item.kind: item.item.artifact_digest for item in evidence}
            semantic_digest = digest_map.get("schematic_semantic_diff")
            self._proposal_store.mark_validation_failed(proposal_id, lease.task_id, lease.lease_token, self._clock(), error.code, semantic_digest, evidence_set_digest, {"error_code": error.code, "artifact_digests": digest_map}, tuple(evidence))
            raise
        except StaleLeaseError:
            raise
        except Exception as error:
            failed = TerminalTaskError("CANDIDATE_VALIDATION_FAILED", str(error))
            evidence_set_digest = add_failed_evidence_set().digest
            digest_map = {item.item.kind: item.item.artifact_digest for item in evidence}
            self._proposal_store.mark_validation_failed(proposal_id, lease.task_id, lease.lease_token, self._clock(), failed.code, digest_map.get("schematic_semantic_diff"), evidence_set_digest, {"error_code": failed.code, "artifact_digests": digest_map}, tuple(evidence))
            raise failed from error


def revisions_module_snapshot_files(root: Path):
    for directory, directories, files in os.walk(root, followlinks=False):
        directories.sort(); files.sort()
        for name in files:
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            yield path, relative, path.stat()

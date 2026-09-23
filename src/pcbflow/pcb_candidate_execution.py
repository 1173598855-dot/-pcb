"""PcbCandidateExecutionTaskHandler and execution helpers.

Owns the runtime-only concerns: workspace setup, adapter execution,
DRC validation, evidence publication, and finalization into READY_FOR_G3
or terminal failure.
"""

from __future__ import annotations

import io
import json
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from pcbflow.board.adapter import BoardSemanticMismatchError, semantic_diff
from pcbflow.board.validation import BoardRuleChecker
from pcbflow.canonical import canonical_json_bytes, sha256_digest
from pcbflow.cancellation import TaskCancelledError
from pcbflow.domain import (
    EdaKind,
    EdaOperation,
    NormalizedFinding,
    ProjectMode,
    TaskLease,
    ValidationReport,
    utc_now,
)
from pcbflow.lceda_pro import LcedaProCapabilityError
from pcbflow.pcb_candidate_store import (
    PcbCandidateStatus,
    PcbCandidateStore,
    pcb_candidate_review_digest,
)
from pcbflow.repositories import (
    EvidenceRepository,
    FindingRepository,
    ProjectRepository,
    TaskRepository,
)
from pcbflow.repository_errors import StaleLeaseError
from pcbflow.tasks import TerminalTaskError

G3_REQUIRED_EVIDENCE = frozenset(
    {
        "pcb_input_snapshot",
        "eda_capability",
        "rulepack",
        "placement_evidence",
        "routing_evidence",
        "copper_evidence",
        "boardir_validation",
        "native_drc",
        "board_semantic_diff",
        "candidate_summary",
    }
)
G3_REQUIRED_EVIDENCE_MEDIA_TYPES = {
    "pcb_input_snapshot": "application/vnd.pcbflow.boardir+json",
    "eda_capability": "application/vnd.pcbflow.eda-capability+json",
    "rulepack": "application/vnd.pcbflow.rulepack+json",
    "placement_evidence": "application/vnd.pcbflow.pcb-placement-evidence+json",
    "routing_evidence": "application/vnd.pcbflow.pcb-routing-evidence+json",
    "copper_evidence": "application/vnd.pcbflow.pcb-copper-evidence+json",
    "boardir_validation": "application/vnd.pcbflow.pcb-boardir-validation+json",
    "native_drc": "application/vnd.pcbflow.pcb-native-drc+json",
    "board_semantic_diff": "application/vnd.pcbflow.pcb-semantic-diff+json",
    "candidate_summary": "application/vnd.pcbflow.pcb-candidate-summary+json",
}
G3_EVIDENCE_SET_KIND = "pcb_candidate_evidence_set"
G3_EVIDENCE_SET_MEDIA_TYPE = (
    "application/vnd.pcbflow.pcb-candidate-evidence-set+json"
)


@dataclass(frozen=True, slots=True)
class _CandidateExecutionError(Exception):
    code: str
    message: str


def _load_candidate_rulepack(candidate: Any) -> tuple[Any, dict[str, Any], bytes]:
    from pcbflow.board.rulepack import ManufacturingRulePack

    evidence = candidate.algorithm_evidence
    if not isinstance(evidence, dict):
        raise _CandidateExecutionError(
            "PCB_CANDIDATE_EVIDENCE_INCOMPLETE", "candidate algorithm evidence is missing"
        )
    for kind in ("rulepack", "placement", "routing", "copper"):
        if type(evidence.get(kind)) is not dict:
            raise _CandidateExecutionError(
                "PCB_CANDIDATE_EVIDENCE_INCOMPLETE",
                f"candidate is missing {kind} evidence",
            )
    try:
        rulepack = ManufacturingRulePack.load_json(
            canonical_json_bytes(evidence["rulepack"])
        )
    except (TypeError, ValueError) as error:
        raise _CandidateExecutionError(
            "PCB_CANDIDATE_RULEPACK_INVALID", "candidate rulepack evidence is invalid"
        ) from error
    rulepack_bytes = rulepack.canonical_bytes()
    if sha256_digest(rulepack_bytes) != candidate.rulepack_digest:
        raise _CandidateExecutionError(
            "PCB_CANDIDATE_RULEPACK_MISMATCH",
            "candidate rulepack does not match its frozen digest",
        )
    return rulepack, evidence, rulepack_bytes


def _validate_candidate_operations(
    candidate: Any,
    before: Any,
    operations: tuple[Any, ...],
    *,
    expected_snapshot_digest: str | None = None,
) -> None:
    from pcbflow.board.operations import BoardOperation

    if not operations:
        return
    if expected_snapshot_digest is None:
        expected_snapshot_digest = before.canonical_digest()
    for operation in operations:
        if not isinstance(operation, BoardOperation):
            continue
        if (
            operation.project_id != candidate.project_id
            or operation.baseline_revision != candidate.base_revision
            or operation.rulepack_digest != candidate.rulepack_digest
            or operation.expected_snapshot_digest != expected_snapshot_digest
        ):
            raise _CandidateExecutionError(
                "PCB_CANDIDATE_OPERATION_MISMATCH",
                "typed operation does not match the frozen candidate inputs",
            )


def _findings_payload(
    findings: Any,
) -> list[dict[str, str]]:
    return [
        {
            "rule_id": finding.rule_id,
            "severity": finding.severity,
            "subject": finding.subject,
            "message": finding.message,
        }
        for finding in findings
    ]


def _is_blocking_finding(finding: Any) -> bool:
    return finding.severity.casefold() in {"error", "critical", "fatal", "blocker"}


def _unconnected_net_ids(
    algorithm_evidence: dict[str, Any],
    findings: Any,
) -> tuple[str, ...]:
    routing = algorithm_evidence["routing"]
    value = routing.get("final_unconnected_nets")
    if type(value) is not list or any(type(item) is not str for item in value):
        raise _CandidateExecutionError(
            "PCB_CANDIDATE_EVIDENCE_INCOMPLETE",
            "routing evidence must include final_unconnected_nets",
        )
    disconnected = {
        finding.subject
        for finding in findings
        if finding.rule_id == "PCB_ROUTE_DISCONNECTED"
    }
    return tuple(sorted(set(value) | disconnected))


def _existing_artifact_descriptor(
    artifacts: Any,
    digest: str,
    media_type: str,
) -> Any:
    from pcbflow.artifacts import ArtifactDescriptor

    with artifacts.open(digest) as stream:
        raw = stream.read()
    actual_digest = sha256_digest(raw)
    if actual_digest != digest:
        raise _CandidateExecutionError(
            "PCB_CANDIDATE_ARTIFACT_MISMATCH",
            "frozen evidence artifact digest does not match its content",
        )
    return ArtifactDescriptor(
        digest=digest,
        size=len(raw),
        media_type=media_type,
        path=artifacts._path(digest),
    )


class PcbCandidateExecutionTaskHandler:
    def __init__(
        self,
        candidates: PcbCandidateStore,
        projects: ProjectRepository,
        tasks: TaskRepository,
        evidence: EvidenceRepository,
        findings: FindingRepository,
        artifacts: Any,
        capability_gate: Any,
        adapter: Any,
        workspaces_dir: Path,
        revisions: Any,
        clock=utc_now,
    ) -> None:
        self._candidates = candidates
        self._projects = projects
        self._tasks = tasks
        self._evidence = evidence
        self._findings = findings
        self._artifacts = artifacts
        self._capability_gate = capability_gate
        self._adapter = adapter
        self._workspaces_dir = workspaces_dir
        self._revisions = revisions
        self._clock = clock

    def __call__(self, lease: TaskLease) -> dict[str, Any]:
        candidate = self._candidate_for_lease(lease)
        try:
            self._candidates.mark_executing(
                candidate.id, lease.task_id, lease.lease_token, self._clock()
            )
            self._require_capabilities(candidate)
            result, board_findings, native_findings = self._execute(candidate, lease)
        except TaskCancelledError:
            raise
        except LcedaProCapabilityError:
            self._candidates.mark_blocked(
                candidate.id,
                lease.task_id,
                lease.lease_token,
                self._clock(),
                result={"code": "PCB_CAPABILITY_GATE_BLOCKED"},
            )
            return {
                "candidate_id": candidate.id,
                "status": PcbCandidateStatus.BLOCKED.value,
            }
        except _CandidateExecutionError as error:
            self._mark_failed(candidate, lease, error.code, error.message)
            raise TerminalTaskError(error.code, error.message) from error
        except (BoardSemanticMismatchError, FileNotFoundError, OSError, ValueError) as error:
            self._mark_failed(
                candidate,
                lease,
                "PCB_CANDIDATE_EXECUTION_FAILED",
                str(error),
            )
            raise TerminalTaskError("PCB_CANDIDATE_EXECUTION_FAILED", str(error)) from error
        except StaleLeaseError:
            raise
        except Exception as error:
            self._mark_failed(
                candidate,
                lease,
                "PCB_CANDIDATE_EXECUTION_FAILED",
                str(error),
            )
            raise TerminalTaskError("PCB_CANDIDATE_EXECUTION_FAILED", str(error)) from error

        blocking = tuple(
            finding
            for finding in board_findings + native_findings
            if _is_blocking_finding(finding)
        )
        if blocking or result["unconnected_net_count"] != 0:
            code = "PCB_NATIVE_DRC_BLOCKED" if any(
                finding in native_findings for finding in blocking
            ) else "PCB_BOARDIR_VALIDATION_FAILED"
            if result["unconnected_net_count"] != 0:
                code = "PCB_UNCONNECTED_NETS"
            failed = {**result, "validation_error_code": code}
            self._candidates.mark_validation_failed(
                candidate.id,
                lease.task_id,
                lease.lease_token,
                self._clock(),
                code,
                result=failed,
            )
            raise TerminalTaskError(code, "candidate validation reported blocking findings")

        try:
            self._candidates.mark_ready_for_g3(
                candidate.id,
                lease.task_id,
                lease.lease_token,
                self._clock(),
                result=result,
            )
        except (TaskCancelledError, StaleLeaseError):
            raise
        except Exception as error:
            self._mark_failed(
                candidate,
                lease,
                "PCB_CANDIDATE_FINALIZATION_FAILED",
                str(error),
            )
            raise TerminalTaskError(
                "PCB_CANDIDATE_FINALIZATION_FAILED",
                str(error),
            ) from error
        return {
            "candidate_id": candidate.id,
            "candidate_digest": result["candidate_digest"],
            "status": PcbCandidateStatus.READY_FOR_G3.value,
        }

    def _candidate_for_lease(self, lease: TaskLease) -> Any:
        project_id = lease.payload.get("project_id")
        candidate_key = lease.payload.get("candidate_key")
        if not isinstance(project_id, str) or not isinstance(candidate_key, str):
            raise TerminalTaskError(
                "PCB_CANDIDATE_NOT_FOUND",
                "PCB candidate task has no durable candidate reference",
            )
        candidate = self._candidates.find_by_idempotency_key(project_id, candidate_key)
        if candidate is None or candidate.task_id != lease.task_id:
            raise TerminalTaskError("PCB_CANDIDATE_NOT_FOUND", "PCB candidate not found")
        return candidate

    def _require_capabilities(self, candidate: Any) -> None:
        operations = (
            EdaOperation.SNAPSHOT,
            EdaOperation.CREATE_CANDIDATE,
            EdaOperation.APPLY_OPERATIONS,
            EdaOperation.RUN_DRC,
        )
        require_operations = getattr(self._capability_gate, "require_operations", None)
        if callable(require_operations):
            digest = require_operations(
                candidate.project_id, EdaKind.LCEDA_PRO, operations
            )
            if digest != candidate.capability_digest:
                raise LcedaProCapabilityError("LCEDA_PRO_WRITE_CAPABILITY_UNVERIFIED")
            return
        for operation in operations:
            digest = self._capability_gate.require_operation(
                candidate.project_id, EdaKind.LCEDA_PRO, operation
            )
            if digest != candidate.capability_digest:
                raise LcedaProCapabilityError("LCEDA_PRO_WRITE_CAPABILITY_UNVERIFIED")

    def _execute(
        self, candidate: Any, lease: TaskLease
    ) -> tuple[dict[str, Any], tuple[Any, ...], tuple[Any, ...]]:
        from pcbflow.board.operations import deserialize_board_operations

        if sha256_digest(canonical_json_bytes(candidate.operations)) != candidate.operations_digest:
            raise _CandidateExecutionError(
                "PCB_CANDIDATE_OPERATION_DIGEST_MISMATCH",
                "persisted candidate operations do not match the frozen digest",
            )
        project = self._projects.get(candidate.project_id)
        managed_revision = project.mode is ProjectMode.MANAGED
        source_context = (
            self._revisions.materialize(
                project.id, candidate.base_revision, "pcb-candidate"
            )
            if managed_revision
            else nullcontext(project.source_path)
        )
        with source_context as frozen_source:
            source_before = self._revisions.snapshot_digest(frozen_source)
            if (
                not managed_revision
                and candidate.base_snapshot_digest is not None
                and source_before != candidate.base_snapshot_digest
            ):
                raise _CandidateExecutionError(
                    "PCB_CANDIDATE_SOURCE_CHANGED",
                    "registered source tree no longer matches the frozen candidate base",
                )
            self._tasks.assert_active(lease.task_id, lease.lease_token, self._clock())
            before = self._adapter.load_snapshot(frozen_source)
            before_bytes = before.canonical_bytes()
            before_digest = sha256_digest(before_bytes)
            if before_digest != candidate.board_snapshot_digest:
                raise _CandidateExecutionError(
                    "PCB_CANDIDATE_INPUT_DIGEST_MISMATCH",
                    "source BoardIR digest does not match the frozen candidate input",
                )
            rulepack, algorithm_evidence, rulepack_bytes = _load_candidate_rulepack(candidate)
            operations = deserialize_board_operations(candidate.operations)
            _validate_candidate_operations(
                candidate,
                before,
                operations,
                expected_snapshot_digest=before_digest,
            )

            self._workspaces_dir.mkdir(parents=True, exist_ok=True)
            with TemporaryDirectory(
                prefix=f"pcb-candidate-{candidate.id}-", dir=self._workspaces_dir
            ) as temporary:
                workspace = self._adapter.create_candidate(
                    frozen_source, Path(temporary) / "native-candidate"
                )
                self._tasks.assert_active(lease.task_id, lease.lease_token, self._clock())
                applied = self._adapter.apply_operations(workspace, operations, before)
                reread = self._adapter.load_snapshot(workspace.path)
                if reread != applied:
                    raise _CandidateExecutionError(
                        "PCB_CANDIDATE_REOPEN_MISMATCH",
                        "candidate BoardIR changed after adapter apply/reopen",
                    )
                before.validate_proposed(reread)
                board_findings = BoardRuleChecker().check(reread, rulepack)
                self._tasks.assert_active(lease.task_id, lease.lease_token, self._clock())
                reports = self._adapter.run_drc(workspace)
                if type(reports) is not tuple or not reports:
                    raise _CandidateExecutionError(
                        "PCB_NATIVE_DRC_REPORT_MISSING",
                        "native DRC did not return a report",
                    )
                if any(
                    not isinstance(report, ValidationReport)
                    or type(report.kind) is not str
                    or not report.kind.strip()
                    or type(report.findings) is not tuple
                    or any(
                        not isinstance(finding, NormalizedFinding)
                        for finding in report.findings
                    )
                    for report in reports
                ):
                    raise _CandidateExecutionError(
                        "PCB_NATIVE_DRC_REPORT_INVALID",
                        "native DRC returned a malformed report",
                    )

            self._tasks.assert_active(lease.task_id, lease.lease_token, self._clock())
            source_after = self._revisions.snapshot_digest(frozen_source)
            if source_after != source_before:
                raise _CandidateExecutionError(
                    "PCB_CANDIDATE_SOURCE_CHANGED",
                    "candidate execution modified the registered source tree",
                )
            native_findings = tuple(
                finding for report in reports for finding in report.findings
            )
            result = self._publish_evidence(
                candidate,
                lease,
                before_bytes,
                reread,
                rulepack_bytes,
                algorithm_evidence,
                source_before,
                source_after,
                semantic_diff(before, reread),
                board_findings,
                reports,
            )
            return result, board_findings, native_findings

    def _publish_evidence(
        self,
        candidate: Any,
        lease: TaskLease,
        before_bytes: bytes,
        after: Any,
        rulepack_bytes: bytes,
        algorithm_evidence: dict[str, Any],
        source_before: str,
        source_after: str,
        diff: Any,
        board_findings: tuple[Any, ...],
        reports: tuple[Any, ...],
    ) -> dict[str, Any]:
        native_findings = tuple(
            finding for report in reports for finding in report.findings
        )
        unconnected = _unconnected_net_ids(algorithm_evidence, board_findings)
        blocking_count = sum(
            _is_blocking_finding(finding)
            for finding in board_findings + native_findings
        )
        staged: dict[str, Any] = {}
        published: list[tuple[Any, Any]] = []
        evidence_committed = False

        def stage(kind: str, data: bytes, media_type: str) -> Any:
            self._tasks.assert_active(lease.task_id, lease.lease_token, self._clock())
            artifact = self._artifacts.stage_stream(io.BytesIO(data), media_type)
            staged[kind] = artifact
            self._tasks.assert_active(lease.task_id, lease.lease_token, self._clock())
            return artifact

        try:
            input_staged = stage(
                "pcb_input_snapshot",
                before_bytes,
                "application/vnd.pcbflow.boardir+json",
            )
            if input_staged.digest != candidate.board_snapshot_digest:
                raise _CandidateExecutionError(
                    "PCB_CANDIDATE_INPUT_DIGEST_MISMATCH",
                    "canonical BoardIR artifact does not match the frozen input digest",
                )
            rulepack_staged = stage(
                "rulepack",
                rulepack_bytes,
                "application/vnd.pcbflow.rulepack+json",
            )
            if rulepack_staged.digest != candidate.rulepack_digest:
                raise _CandidateExecutionError(
                    "PCB_CANDIDATE_RULEPACK_MISMATCH",
                    "canonical rulepack artifact does not match the frozen digest",
                )
            capability_descriptor = _existing_artifact_descriptor(
                self._artifacts,
                candidate.capability_digest,
                "application/vnd.pcbflow.eda-capability+json",
            )
            post_snapshot = after.canonical_bytes()
            post_snapshot_digest = sha256_digest(post_snapshot)
            validation_payload = {
                "schema_version": "1.0",
                "snapshot_digest": post_snapshot_digest,
                "snapshot": json.loads(post_snapshot),
                "findings": _findings_payload(board_findings),
            }
            native_payload = {
                "schema_version": "1.0",
                "reports": [
                    {"kind": report.kind, "findings": _findings_payload(report.findings)}
                    for report in reports
                ],
            }
            semantic_payload = {
                "schema_version": "1.0",
                "changed_object_ids": list(diff.changed_object_ids),
                "unexpected_object_ids": list(diff.unexpected_object_ids),
            }
            summary = {
                "schema_version": "1.0",
                "candidate_id": candidate.id,
                "project_id": candidate.project_id,
                "base_revision": candidate.base_revision,
                "source_snapshot_digest_before": source_before,
                "source_snapshot_digest_after": source_after,
                "board_snapshot_digest": candidate.board_snapshot_digest,
                "candidate_board_snapshot_digest": post_snapshot_digest,
                "rulepack_digest": candidate.rulepack_digest,
                "capability_digest": candidate.capability_digest,
                "authority_digest": candidate.authority_digest,
                "operations_digest": candidate.operations_digest,
                "blocking_finding_count": blocking_count,
                "unconnected_net_ids": list(unconnected),
            }
            stage(
                "placement_evidence",
                canonical_json_bytes(algorithm_evidence["placement"]),
                "application/vnd.pcbflow.pcb-placement-evidence+json",
            )
            stage(
                "routing_evidence",
                canonical_json_bytes(algorithm_evidence["routing"]),
                "application/vnd.pcbflow.pcb-routing-evidence+json",
            )
            stage(
                "copper_evidence",
                canonical_json_bytes(algorithm_evidence["copper"]),
                "application/vnd.pcbflow.pcb-copper-evidence+json",
            )
            stage(
                "boardir_validation",
                canonical_json_bytes(validation_payload),
                "application/vnd.pcbflow.pcb-boardir-validation+json",
            )
            stage(
                "native_drc",
                canonical_json_bytes(native_payload),
                "application/vnd.pcbflow.pcb-native-drc+json",
            )
            stage(
                "board_semantic_diff",
                canonical_json_bytes(semantic_payload),
                "application/vnd.pcbflow.pcb-semantic-diff+json",
            )
            stage(
                "candidate_summary",
                canonical_json_bytes(summary),
                "application/vnd.pcbflow.pcb-candidate-summary+json",
            )
            artifact_refs: dict[str, Any] = {
                **staged,
                "eda_capability": capability_descriptor,
            }
            evidence_kinds = tuple(sorted(artifact_refs))
            evidence_set = {
                "schema_version": "1.0",
                "candidate_id": candidate.id,
                "project_id": candidate.project_id,
                "task_id": candidate.task_id,
                "base_revision": candidate.base_revision,
                "items": [
                    {
                        "kind": kind,
                        "artifact_digest": artifact_refs[kind].digest,
                        "media_type": artifact_refs[kind].media_type,
                        "verdict": "pass"
                        if kind not in {"boardir_validation", "native_drc"}
                        or blocking_count == 0
                        else "fail",
                    }
                    for kind in sorted(artifact_refs)
                ],
            }
            stage(
                "evidence_set",
                canonical_json_bytes(evidence_set),
                "application/vnd.pcbflow.pcb-candidate-evidence-set+json",
            )
            descriptors: dict[str, Any] = {}
            for kind in sorted(staged):
                self._tasks.assert_active(lease.task_id, lease.lease_token, self._clock())
                if kind == "evidence_set":
                    continue
                descriptors[kind] = staged[kind].publish()
                published.append((descriptors[kind], staged[kind]))
            self._tasks.assert_active(lease.task_id, lease.lease_token, self._clock())
            evidence_set_descriptor = staged["evidence_set"].publish()
            published.append((evidence_set_descriptor, staged["evidence_set"]))
            self._tasks.assert_active(lease.task_id, lease.lease_token, self._clock())
            report_inputs = {
                G3_EVIDENCE_SET_KIND: (
                    evidence_set_descriptor,
                    candidate.id,
                    "pass",
                ),
                **{
                    item["kind"]: (
                        descriptors.get(item["kind"], capability_descriptor),
                        candidate.id,
                        item["verdict"],
                    )
                    for item in evidence_set["items"]
                },
            }
            self._evidence.add_reports_and_findings(
                project_id=candidate.project_id,
                task_id=lease.task_id,
                reports=report_inputs,
                findings={
                    "boardir_validation": board_findings,
                    "native_drc": native_findings,
                },
                lease_token=lease.lease_token,
                now=self._clock(),
            )
            evidence_committed = True
            candidate_digest = pcb_candidate_review_digest(
                candidate_id=candidate.id,
                project_id=candidate.project_id,
                base_revision=candidate.base_revision,
                base_snapshot_digest=candidate.base_snapshot_digest,
                board_snapshot_digest=candidate.board_snapshot_digest,
                candidate_board_snapshot_digest=post_snapshot_digest,
                rulepack_digest=candidate.rulepack_digest,
                capability_digest=candidate.capability_digest,
                authority_digest=candidate.authority_digest,
                operations_digest=candidate.operations_digest,
                evidence_set_digest=evidence_set_descriptor.digest,
            )
            return {
                **summary,
                "candidate_digest": candidate_digest,
                "evidence_set_digest": evidence_set_descriptor.digest,
                "evidence_kinds": list(evidence_kinds),
                "evidence_artifacts": {
                    kind: artifact_refs[kind].digest for kind in sorted(artifact_refs)
                },
                "unconnected_net_count": len(unconnected),
                "native_candidate_verified": True,
            }
        finally:
            for artifact in staged.values():
                if evidence_committed:
                    artifact.discard()
            if not evidence_committed:
                self._candidates.settle_publication(
                    tuple(descriptor for descriptor, _artifact in published),
                    tuple(artifact for _descriptor, artifact in published),
                )
                published_artifacts = {id(artifact) for _descriptor, artifact in published}
                for artifact in staged.values():
                    if id(artifact) not in published_artifacts:
                        artifact.discard()

    def _mark_failed(
        self, candidate: Any, lease: TaskLease, code: str, message: str
    ) -> None:
        self._candidates.mark_validation_failed(
            candidate.id,
            lease.task_id,
            lease.lease_token,
            self._clock(),
            code,
            result={
                "candidate_id": candidate.id,
                "operations_digest": candidate.operations_digest,
                "board_snapshot_digest": candidate.board_snapshot_digest,
                "rulepack_digest": candidate.rulepack_digest,
                "capability_digest": candidate.capability_digest,
                "authority_digest": candidate.authority_digest,
                "error_code": code,
                "error_message": message,
            },
        )

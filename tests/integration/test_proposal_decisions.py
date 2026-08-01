from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier

import pytest
from sqlalchemy import select, update

from pcbflow.approvals import ApprovalDigestMismatchError
from pcbflow.canonical import canonical_json_bytes
from pcbflow.domain import RequestInvalidError
from pcbflow.proposals import (
    EvidenceItem,
    EvidenceRegistration,
    EvidenceSet,
    ProposalDecisionService,
    CandidateNotReviewableError,
    ProposalStatus,
    READY_EVIDENCE_MEDIA_TYPES,
    proposal_review_digest,
)
from pcbflow.repositories import RevisionConflictError
from pcbflow.design_tables import ChangeProposalRow, OutboxEventRow
from pcbflow.tables import ArtifactRow, EvidenceRow
from pcbflow.revisions import RevisionReconciler
from pcbflow.observability import MetricName


NOW = datetime(2026, 7, 29, 13, 0, tzinfo=UTC)


class FakeDecisionRevisions:
    def __init__(self) -> None:
        self.design_revision: str | None = None
        self.proposal_revision: str | None = None
        self.fail_next_design_update = False

    def object_exists(self, _project_id: str, revision: str) -> bool:
        return revision.startswith("git:")

    def resolve_proposal_ref(self, _project_id: str, _proposal_id: str):
        return self.proposal_revision

    def resolve_design_ref(self, _project_id: str):
        return self.design_revision

    def promote_design_ref(
        self,
        _project_id: str,
        revision: str,
        _expected_revision: str | None,
    ) -> None:
        if self.fail_next_design_update:
            self.fail_next_design_update = False
            raise RuntimeError("injected design ref failure")
        self.design_revision = revision


def _decision_service(container, revisions) -> ProposalDecisionService:
    reconciler = _reconciler(container, revisions)
    return ProposalDecisionService(
        proposal_store=container.proposal_store,
        command_batches=container.command_batches,
        projects=container.projects,
        requirements=container.requirement_store,
        revisions=revisions,
        artifacts=container.artifacts,
        evidence=container.evidence,
        reconciler=reconciler,
        metrics=container.metrics,
        clock=lambda: NOW,
    )


def _reconciler(container, revisions) -> RevisionReconciler:
    return RevisionReconciler(
        projects=container.projects,
        requirements=container.requirement_store,
        proposals=container.proposal_store,
        revisions=revisions,
        sessions=container.sessions,
        metrics=container.metrics,
        clock=lambda: NOW,
    )


def _batch(project, requirement_set, suffix: str) -> bytes:
    actor = {"type": "human", "id": "local-user"}
    symbol_ref = {
        "kind": "symbol",
        "sheet_uuid": "00000000-0000-0000-0000-000000000001",
        "object_uuid": "00000000-0000-0000-0000-000000000002",
        "pin_number": None,
    }
    value = {
        "schema_version": "1.0",
        "batch_id": f"bat_decision_{suffix}",
        "project_id": project.id,
        "base_revision": project.current_revision,
        "requirement_set_id": requirement_set.id,
        "idempotency_key": f"decision-{suffix}",
        "actor": actor,
        "intent": f"Decision fixture {suffix}",
        "risk": "low",
        "commands": [
            {
                "schema_version": "1.0",
                "command_id": f"cmd_decision_{suffix}",
                "batch_id": f"bat_decision_{suffix}",
                "project_id": project.id,
                "base_revision": project.current_revision,
                "idempotency_key": f"decision-{suffix}:1",
                "actor": actor,
                "intent": f"Decision fixture {suffix}",
                "risk": "low",
                "preconditions": [],
                "operation": {
                    "type": "schematic.set_property",
                    "payload": {
                        "subject_ref": symbol_ref,
                        "property_name": "Value",
                        "value": suffix,
                        "expected_old_value": "LED",
                    },
                },
                "required_validations": ["semantic_diff", "kicad_erc"],
                "provenance": {
                    "requirement_ids": ["REQ-FUNC-001"],
                    "evidence_ids": [],
                    "module_revision_ids": [],
                },
            }
        ],
    }
    return json.dumps(value, separators=(",", ":")).encode()


def _ready(container, frozen_requirement_set, suffix: str):
    project = container.projects.get(frozen_requirement_set.project_id)
    batch_bytes = _batch(project, frozen_requirement_set, suffix)
    proposal = container.proposals.create(batch_bytes, f"decision-{suffix}")
    lease = container.tasks.claim_next("decision-worker", NOW, 60)
    assert lease is not None and lease.task_id == proposal.task_id
    container.tasks.start(lease.task_id, lease.lease_token, NOW)
    executing = container.proposal_store.begin_execution(
        proposal_id=proposal.id,
        task_id=proposal.task_id,
        lease_token=lease.lease_token,
        now=NOW,
    )
    assert executing.status is ProposalStatus.EXECUTING
    candidate_revision = "git:" + ({"accept": "a", "reject": "b", "stale": "c"}[suffix] * 40)
    candidate_snapshot_digest = "sha256:" + "d" * 64

    evidence_inputs = {
        "design_command_batch": (
            batch_bytes,
            READY_EVIDENCE_MEDIA_TYPES["design_command_batch"],
        ),
        "project_snapshot_before": (
            canonical_json_bytes({"schema_version": "1.0", "files": []}),
            READY_EVIDENCE_MEDIA_TYPES["project_snapshot_before"],
        ),
        "project_snapshot_after": (
            canonical_json_bytes(
                {
                    "schema_version": "1.0",
                    "files": [],
                    "digest": candidate_snapshot_digest,
                }
            ),
            READY_EVIDENCE_MEDIA_TYPES["project_snapshot_after"],
        ),
        "git_text_diff": (
            b"diff --git a/board.kicad_sch b/board.kicad_sch\n",
            READY_EVIDENCE_MEDIA_TYPES["git_text_diff"],
        ),
        "schematic_semantic_diff": (
            canonical_json_bytes(
                {
                    "schema_version": "1.0",
                    "changes": [
                        {
                            "kind": "symbol_property_changed",
                            "subject_ref": {
                                "kind": "symbol",
                                "sheet_uuid": "00000000-0000-0000-0000-000000000001",
                                "object_uuid": "00000000-0000-0000-0000-000000000002",
                                "pin_number": None,
                            },
                            "before": {"Value": "LED"},
                            "after": {"Value": suffix},
                            "field": "Value",
                            "command_id": f"cmd_decision_{suffix}",
                            "requirement_ids": ["REQ-FUNC-001"],
                            "risk": "low",
                        }
                    ],
                }
            ),
            READY_EVIDENCE_MEDIA_TYPES["schematic_semantic_diff"],
        ),
        "kicad_erc": (
            b'{"version":"1.0","source":"board.kicad_sch","violations":[]}',
            "application/json",
        ),
        "command_execution_log": (
            canonical_json_bytes(
                {"schema_version": "1.0", "commands": [], "result": "pass"}
            ),
            READY_EVIDENCE_MEDIA_TYPES["command_execution_log"],
        ),
        "adapter_capability_report": (
            canonical_json_bytes(
                {
                    "adapter_contract": "pcbflow.schematic.cst.v1",
                    "kicad_major": 9,
                    "supported_operations": ["schematic.set_property"],
                }
            ),
            READY_EVIDENCE_MEDIA_TYPES["adapter_capability_report"],
        ),
    }
    registrations: list[EvidenceRegistration] = []
    for kind, (data, media_type) in evidence_inputs.items():
        descriptor = container.artifacts.put_bytes(data, media_type)
        registrations.append(
            EvidenceRegistration(
                descriptor=descriptor,
                item=EvidenceItem(
                    kind=kind,
                    artifact_digest=descriptor.digest,
                    media_type=descriptor.media_type,
                    verdict="pass",
                ),
            )
        )
    registrations.sort(key=lambda value: value.item.kind)
    semantic = next(
        value.descriptor
        for value in registrations
        if value.item.kind == "schematic_semantic_diff"
    )
    capability = next(
        value.descriptor
        for value in registrations
        if value.item.kind == "adapter_capability_report"
    )
    evidence_value = EvidenceSet(
        project_id=project.id,
        task_id=proposal.task_id,
        proposal_id=proposal.id,
        base_revision=project.current_revision,
        candidate_revision=candidate_revision,
        artifacts=tuple(value.item for value in registrations),
    )
    evidence_set = container.artifacts.put_bytes(
        canonical_json_bytes(evidence_value.model_dump(mode="json")),
        "application/vnd.pcbflow.evidence-set+json",
    )
    registrations.append(
        EvidenceRegistration(
            descriptor=evidence_set,
            item=EvidenceItem(
                kind="proposal_evidence_set",
                artifact_digest=evidence_set.digest,
                media_type=evidence_set.media_type,
                verdict="pass",
            ),
        )
    )
    review_digest = proposal_review_digest(
        proposal_id=proposal.id,
        project_id=project.id,
        base_revision=project.current_revision,
        candidate_revision=candidate_revision,
        candidate_snapshot_digest=candidate_snapshot_digest,
        requirement_set_digest=frozen_requirement_set.canonical_digest,
        semantic_diff_digest=semantic.digest,
        evidence_set_digest=evidence_set.digest,
        adapter_capability_digest=capability.digest,
    )
    ready = container.proposal_store.mark_ready(
        proposal_id=proposal.id,
        task_id=proposal.task_id,
        lease_token=lease.lease_token,
        now=NOW,
        candidate_revision=candidate_revision,
        candidate_snapshot_digest=candidate_snapshot_digest,
        review_digest=review_digest,
        semantic_diff_digest=semantic.digest,
        evidence_set_digest=evidence_set.digest,
        result={
            "validations": {
                "schema": "pass",
                "preconditions": "pass",
                "path_limits": "pass",
                "post_write_parse": "pass",
                "semantic_diff": "pass",
                "kicad_erc": "pass",
            },
            "adapter_capability_digest": capability.digest,
        },
        evidence=tuple(registrations),
    )
    container.tasks.complete(
        lease.task_id,
        lease.lease_token,
        {"proposal_id": proposal.id},
        NOW,
    )
    return project, ready


def test_accept_is_idempotent_and_advances_database_revision(
    container, frozen_requirement_set
) -> None:
    revisions = FakeDecisionRevisions()
    project, proposal = _ready(container, frozen_requirement_set, "accept")
    revisions.proposal_revision = proposal.candidate_revision
    revisions.design_revision = project.current_revision
    service = _decision_service(container, revisions)

    accepted = service.accept(
        proposal_id=proposal.id,
        candidate_digest=proposal.review_digest,
        actor_type="human",
        actor_id="local-user",
        comment="accepted",
        idempotency_key="accept-proposal",
    )
    repeated = service.accept(
        proposal_id=proposal.id,
        candidate_digest=proposal.review_digest,
        actor_type="human",
        actor_id="local-user",
        comment="accepted",
        idempotency_key="accept-proposal",
    )
    replayed_create = container.proposals.create(
        _batch(project, frozen_requirement_set, "accept"),
        "decision-accept",
    )

    assert accepted.status is ProposalStatus.ACCEPTED
    assert repeated.id == accepted.id
    assert replayed_create == accepted
    assert container.projects.get(project.id).current_revision == (
        proposal.candidate_revision
    )
    assert revisions.design_revision == proposal.candidate_revision
    assert any(
        item.kind == "approval_signature"
        for item in container.evidence.list_for_project(project.id)
    )
    assert MetricName.PROPOSAL_REVIEW_WAIT_SECONDS in {
        point.name for point in container.metrics.snapshot()
    }


def test_reject_records_decision_without_advancing_revision(
    container, frozen_requirement_set
) -> None:
    revisions = FakeDecisionRevisions()
    project, proposal = _ready(container, frozen_requirement_set, "reject")
    revisions.proposal_revision = proposal.candidate_revision
    revisions.design_revision = project.current_revision
    rejected = _decision_service(container, revisions).reject(
        proposal_id=proposal.id,
        reason="not needed",
        actor_type="human",
        actor_id="local-user",
        idempotency_key="reject-proposal",
    )
    assert rejected.status is ProposalStatus.REJECTED
    assert container.projects.get(project.id).current_revision == project.current_revision
    assert revisions.design_revision == project.current_revision


def test_decision_store_maps_late_accept_and_reject_to_request_invalid(
    container, frozen_requirement_set
) -> None:
    reject_revisions = FakeDecisionRevisions()
    reject_project, reject_proposal = _ready(
        container, frozen_requirement_set, "reject"
    )
    reject_revisions.proposal_revision = reject_proposal.candidate_revision
    reject_revisions.design_revision = reject_project.current_revision
    _decision_service(container, reject_revisions).reject(
        proposal_id=reject_proposal.id,
        reason="rejected",
        actor_type="human",
        actor_id="local-user",
        idempotency_key="late-reject-first",
    )
    rejection_artifact = container.artifacts.put_bytes(
        b"late rejection", "application/json"
    )
    with pytest.raises(RequestInvalidError, match="candidate is not reviewable"):
        container.proposal_store.decide_reject(
            proposal_id=reject_proposal.id,
            base_revision=reject_project.current_revision,
            subject_digest=reject_proposal.review_digest or "",
            idempotency_key="late-reject-second",
            actor_type="human",
            actor_id="local-user",
            comment="late rejection",
            rejection_artifact=rejection_artifact,
            now=NOW,
        )
    accept_revisions = FakeDecisionRevisions()
    accept_project, accept_proposal = _ready(
        container, frozen_requirement_set, "accept"
    )
    accept_revisions.proposal_revision = accept_proposal.candidate_revision
    accept_revisions.design_revision = accept_project.current_revision
    _decision_service(container, accept_revisions).accept(
        proposal_id=accept_proposal.id,
        candidate_digest=accept_proposal.review_digest,
        actor_type="human",
        actor_id="local-user",
        comment="accepted",
        idempotency_key="late-accept-first",
    )
    approval_artifact = container.artifacts.put_bytes(
        b"late acceptance", "application/json"
    )
    with pytest.raises(RequestInvalidError, match="candidate is not reviewable"):
        container.proposal_store.decide_accept(
            proposal_id=accept_proposal.id,
            candidate_revision=accept_proposal.candidate_revision or "",
            base_revision=accept_project.current_revision,
            expected_project_version=accept_project.version,
            candidate_snapshot_digest=accept_proposal.candidate_snapshot_digest or "",
            subject_digest=accept_proposal.review_digest or "",
            idempotency_key="late-accept-second",
            actor_type="human",
            actor_id="local-user",
            comment="late acceptance",
            approval_artifact=approval_artifact,
            now=NOW,
        )


def test_concurrent_reject_maps_late_transaction_state_to_request_invalid(
    container, frozen_requirement_set, monkeypatch: pytest.MonkeyPatch
) -> None:
    revisions = FakeDecisionRevisions()
    project, proposal = _ready(container, frozen_requirement_set, "reject")
    revisions.proposal_revision = proposal.candidate_revision
    revisions.design_revision = project.current_revision
    service = _decision_service(container, revisions)
    barrier = Barrier(2)
    original_validate = service._validate_candidate

    def synchronize_validation(proposal_value, candidate_digest):
        result = original_validate(proposal_value, candidate_digest)
        barrier.wait(timeout=10)
        return result

    monkeypatch.setattr(service, "_validate_candidate", synchronize_validation)
    requests = (
        {"reason": "first rejection", "idempotency_key": "concurrent-reject-1"},
        {"reason": "second rejection", "idempotency_key": "concurrent-reject-2"},
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                service.reject,
                proposal_id=proposal.id,
                actor_type="human",
                actor_id="local-user",
                **request,
            )
            for request in requests
        ]
        outcomes: list[object] = []
        errors: list[BaseException] = []
        for future in futures:
            try:
                outcomes.append(future.result(timeout=15))
            except BaseException as error:
                errors.append(error)

    assert len(outcomes) == 1
    assert outcomes[0].status is ProposalStatus.REJECTED
    assert len(errors) == 1
    assert isinstance(errors[0], RequestInvalidError)


def test_reject_does_not_reconcile_or_mutate_design_ref(
    container, frozen_requirement_set
) -> None:
    revisions = FakeDecisionRevisions()
    project, proposal = _ready(container, frozen_requirement_set, "reject")
    revisions.proposal_revision = proposal.candidate_revision
    externally_advanced_ref = "git:" + "e" * 40
    revisions.design_revision = externally_advanced_ref

    rejected = _decision_service(container, revisions).reject(
        proposal_id=proposal.id,
        reason="withdraw without reconciling design state",
        actor_type="human",
        actor_id="local-user",
        idempotency_key="reject-with-drifted-design-ref",
    )

    assert rejected.status is ProposalStatus.REJECTED
    assert revisions.design_revision == externally_advanced_ref
    assert container.projects.get(project.id).current_revision == project.current_revision


def test_accept_with_changed_base_marks_proposal_stale(
    container, frozen_requirement_set
) -> None:
    revisions = FakeDecisionRevisions()
    project, proposal = _ready(container, frozen_requirement_set, "stale")
    revisions.proposal_revision = proposal.candidate_revision
    revisions.design_revision = project.current_revision
    container.projects.compare_and_set_revision(
        project.id,
        expected_revision=project.current_revision,
        new_revision="git:" + "9" * 40,
        snapshot_digest="sha256:" + "9" * 64,
        expected_version=project.version,
    )

    with pytest.raises(RevisionConflictError):
        _decision_service(container, revisions).accept(
            proposal_id=proposal.id,
            candidate_digest=proposal.review_digest,
            actor_type="human",
            actor_id="local-user",
            comment="late approval",
            idempotency_key="accept-stale",
        )
    assert container.proposal_store.get(proposal.id).status is ProposalStatus.STALE
    assert MetricName.PROJECT_REVISION_CONFLICT_TOTAL in {
        point.name for point in container.metrics.snapshot()
    }


def _supersede_requirement_set(container, project, requirement_yaml: bytes):
    requirements_reconciler = container.requirements._reconciler
    approvals_reconciler = container.approvals._reconciler
    container.requirements._reconciler = None
    container.approvals._reconciler = None
    try:
        draft = container.requirements.import_draft(
            project.id,
            requirement_yaml,
            "decision-superseding-requirements",
        )
        pending = container.requirements.submit(
            draft.id,
            "decision-superseding-requirements-submit",
        )
        return container.approvals.decide_g1(
            requirement_set_id=pending.id,
            subject_digest=pending.subject_digest(),
            decision="approve",
            actor_type="human",
            actor_id="local-user",
            comment="updated requirements approved",
            idempotency_key="decision-superseding-g1",
        )
    finally:
        container.requirements._reconciler = requirements_reconciler
        container.approvals._reconciler = approvals_reconciler


def test_accept_after_a_new_g1_transition_marks_old_ready_proposal_stale(
    container, frozen_requirement_set, requirement_yaml: bytes
) -> None:
    revisions = FakeDecisionRevisions()
    project, proposal = _ready(container, frozen_requirement_set, "stale")
    revisions.proposal_revision = proposal.candidate_revision
    revisions.design_revision = project.current_revision

    _supersede_requirement_set(container, project, requirement_yaml)
    current = container.projects.get(project.id)
    revisions.design_revision = current.current_revision

    with pytest.raises(RevisionConflictError):
        _decision_service(container, revisions).accept(
            proposal_id=proposal.id,
            candidate_digest=proposal.review_digest,
            actor_type="human",
            actor_id="local-user",
            comment="approval after updated G1",
            idempotency_key="accept-after-superseding-g1",
        )

    assert container.requirement_store.get(frozen_requirement_set.id).status.value == "superseded"
    assert container.proposal_store.get(proposal.id).status is ProposalStatus.STALE


def test_reject_after_a_new_g1_transition_records_the_old_decision(
    container, frozen_requirement_set, requirement_yaml: bytes
) -> None:
    revisions = FakeDecisionRevisions()
    project, proposal = _ready(container, frozen_requirement_set, "reject")
    revisions.proposal_revision = proposal.candidate_revision
    revisions.design_revision = project.current_revision

    _supersede_requirement_set(container, project, requirement_yaml)
    current = container.projects.get(project.id)
    revisions.design_revision = current.current_revision

    rejected = _decision_service(container, revisions).reject(
        proposal_id=proposal.id,
        reason="withdraw after updated G1",
        actor_type="human",
        actor_id="local-user",
        idempotency_key="reject-after-superseding-g1",
    )

    assert rejected.status is ProposalStatus.REJECTED
    assert container.requirement_store.get(frozen_requirement_set.id).status.value == "superseded"


def test_digest_mismatch_cannot_reuse_acceptance_key(
    container, frozen_requirement_set
) -> None:
    revisions = FakeDecisionRevisions()
    _project, proposal = _ready(container, frozen_requirement_set, "accept")
    revisions.proposal_revision = proposal.candidate_revision
    with pytest.raises(ApprovalDigestMismatchError):
        _decision_service(container, revisions).accept(
            proposal_id=proposal.id,
            candidate_digest="sha256:" + "0" * 64,
            actor_type="human",
            actor_id="local-user",
            comment="wrong digest",
            idempotency_key="wrong-digest",
        )


def test_reconciler_repairs_design_ref_after_committed_acceptance(
    container, frozen_requirement_set
) -> None:
    revisions = FakeDecisionRevisions()
    project, proposal = _ready(container, frozen_requirement_set, "accept")
    revisions.proposal_revision = proposal.candidate_revision
    revisions.design_revision = project.current_revision
    revisions.fail_next_design_update = True
    service = _decision_service(container, revisions)

    accepted = service.accept(
        proposal_id=proposal.id,
        candidate_digest=proposal.review_digest,
        actor_type="human",
        actor_id="local-user",
        comment="accepted before injected ref failure",
        idempotency_key="accept-before-ref-failure",
    )
    assert accepted.status is ProposalStatus.ACCEPTED
    assert revisions.design_revision == project.current_revision

    reconciler = _reconciler(container, revisions)
    assert reconciler.run_once() == 1
    assert revisions.design_revision == proposal.candidate_revision
    assert MetricName.GIT_REF_RECONCILIATION_RETRY_TOTAL in {
        point.name for point in container.metrics.snapshot()
    }


def test_stale_accept_replays_without_appending_a_second_stale_event(
    container, frozen_requirement_set
) -> None:
    revisions = FakeDecisionRevisions()
    project, proposal = _ready(container, frozen_requirement_set, "stale")
    revisions.proposal_revision = proposal.candidate_revision
    revisions.design_revision = project.current_revision
    container.projects.compare_and_set_revision(
        project.id,
        expected_revision=project.current_revision,
        new_revision="git:" + "8" * 40,
        snapshot_digest="sha256:" + "8" * 64,
        expected_version=project.version,
    )
    service = _decision_service(container, revisions)

    for _ in range(2):
        with pytest.raises(RevisionConflictError):
            service.accept(
                proposal_id=proposal.id,
                candidate_digest=proposal.review_digest,
                actor_type="human",
                actor_id="local-user",
                comment="late approval",
                idempotency_key="accept-stale-replay",
            )

    with container.sessions() as session:
        stale_events = session.scalars(
            select(OutboxEventRow).where(
                OutboxEventRow.aggregate_id == proposal.id,
                OutboxEventRow.event_type == "proposal.stale",
            )
        ).all()
    assert len(stale_events) == 1


def test_accept_rejects_an_evidence_set_row_that_is_not_bound_to_its_digest(
    container, frozen_requirement_set
) -> None:
    revisions = FakeDecisionRevisions()
    _project, proposal = _ready(container, frozen_requirement_set, "accept")
    revisions.proposal_revision = proposal.candidate_revision
    with container.sessions.begin() as session:
        replacement_digest = session.scalar(
            select(EvidenceRow.artifact_digest).where(
                EvidenceRow.task_id == proposal.task_id,
                EvidenceRow.kind == "schematic_semantic_diff",
            )
        )
        assert replacement_digest is not None
        session.execute(
            update(EvidenceRow)
            .where(
                EvidenceRow.task_id == proposal.task_id,
                EvidenceRow.kind == "proposal_evidence_set",
            )
            .values(artifact_digest=replacement_digest)
        )

    with pytest.raises(CandidateNotReviewableError):
        _decision_service(container, revisions).accept(
            proposal_id=proposal.id,
            candidate_digest=proposal.review_digest,
            actor_type="human",
            actor_id="local-user",
            comment="accept tampered evidence",
            idempotency_key="tampered-evidence",
        )


def test_accept_rejects_evidence_with_mismatched_registered_media_type(
    container, frozen_requirement_set
) -> None:
    revisions = FakeDecisionRevisions()
    _project, proposal = _ready(container, frozen_requirement_set, "accept")
    revisions.proposal_revision = proposal.candidate_revision
    with container.sessions.begin() as session:
        session.execute(
            update(ArtifactRow)
            .where(ArtifactRow.digest == proposal.semantic_diff_digest)
            .values(media_type="application/x-tampered")
        )

    with pytest.raises(CandidateNotReviewableError):
        _decision_service(container, revisions).accept(
            proposal_id=proposal.id,
            candidate_digest=proposal.review_digest,
            actor_type="human",
            actor_id="local-user",
            comment="accept tampered media",
            idempotency_key="tampered-media",
        )


def test_accept_rejects_evidence_with_noncanonical_media_type(
    container, frozen_requirement_set
) -> None:
    revisions = FakeDecisionRevisions()
    project, proposal = _ready(container, frozen_requirement_set, "accept")
    revisions.proposal_revision = proposal.candidate_revision
    revisions.design_revision = project.current_revision

    with container.artifacts.open(proposal.evidence_set_digest or "") as artifact:
        raw_evidence_set = artifact.read()
    evidence_set = EvidenceSet.model_validate_json(raw_evidence_set, strict=True)
    value = evidence_set.model_dump(mode="json")
    tampered_kind = "design_command_batch"
    tampered_item = next(
        item for item in value["artifacts"] if item["kind"] == tampered_kind
    )
    original_digest = tampered_item["artifact_digest"]
    tampered_item["media_type"] = "text/plain"
    replacement = container.artifacts.put_bytes(
        canonical_json_bytes(value), "application/vnd.pcbflow.evidence-set+json"
    )
    review_digest = proposal_review_digest(
        proposal_id=proposal.id,
        project_id=project.id,
        base_revision=project.current_revision,
        candidate_revision=proposal.candidate_revision or "",
        candidate_snapshot_digest=proposal.candidate_snapshot_digest or "",
        requirement_set_digest=frozen_requirement_set.canonical_digest,
        semantic_diff_digest=proposal.semantic_diff_digest or "",
        evidence_set_digest=replacement.digest,
        adapter_capability_digest=(proposal.result or {})[
            "adapter_capability_digest"
        ],
    )
    result = dict(proposal.result or {})
    result["evidence_set_digest"] = replacement.digest
    result["review_digest"] = review_digest
    with container.sessions.begin() as session:
        session.add(
            ArtifactRow(
                digest=replacement.digest,
                size=replacement.size,
                media_type=replacement.media_type,
                storage_path=str(replacement.path),
                created_at=NOW,
            )
        )
        session.execute(
            update(ArtifactRow)
            .where(ArtifactRow.digest == original_digest)
            .values(media_type="text/plain")
        )
        session.execute(
            update(EvidenceRow)
            .where(
                EvidenceRow.task_id == proposal.task_id,
                EvidenceRow.kind == "proposal_evidence_set",
            )
            .values(artifact_digest=replacement.digest)
        )
        session.execute(
            update(ChangeProposalRow)
            .where(ChangeProposalRow.id == proposal.id)
            .values(
                evidence_set_digest=replacement.digest,
                review_digest=review_digest,
                result_json=result,
            )
        )

    with pytest.raises(CandidateNotReviewableError):
        _decision_service(container, revisions).accept(
            proposal_id=proposal.id,
            candidate_digest=review_digest,
            actor_type="human",
            actor_id="local-user",
            comment="accept noncanonical media",
            idempotency_key="noncanonical-media",
        )

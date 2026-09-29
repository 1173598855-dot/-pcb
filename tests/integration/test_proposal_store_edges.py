"""Edge-path tests for ProposalStore: not-found errors, replay decisions,
stale-fence guards, evidence conflicts, and IntegrityError recovery.

The happy paths live in test_proposals.py and test_proposal_decisions.py;
this file exercises the store's defensive branches directly so the idempotency
and fencing contracts hold even when callers misbehave.
"""

from __future__ import annotations

import pytest
from sqlalchemy import delete, update
from tests.integration.test_proposal_decisions import (
    NOW,
    _batch,
    _ready,
)

from pcbflow.canonical import canonical_json_bytes
from pcbflow.commands import CommandBatch
from pcbflow.design_tables import (
    ChangeProposalRow,
)
from pcbflow.domain import RequestInvalidError
from pcbflow.proposal_store import (
    CommandBatchNotFoundError,
    ProposalNotFoundError,
)
from pcbflow.proposals import (
    READY_EVIDENCE_MEDIA_TYPES,
    EvidenceItem,
    EvidenceRegistration,
    EvidenceSet,
    ProposalStatus,
    proposal_review_digest,
)
from pcbflow.repositories import (
    EvidenceConflictError,
    IdempotencyConflictError,
    RevisionConflictError,
    StaleLeaseError,
)
from pcbflow.tables import ArtifactRow, EvidenceRow, ProjectRow


def _parse_batch(batch_bytes: bytes) -> CommandBatch:
    return CommandBatch.model_validate_json(batch_bytes, strict=True)


def _approve_kwargs(project, proposal, *, key: str, **overrides: object) -> dict:
    kwargs: dict = {
        "proposal_id": proposal.id,
        "candidate_revision": "git:" + "a" * 40,
        "base_revision": project.current_revision,
        "expected_project_version": project.version,
        "candidate_snapshot_digest": "sha256:" + "d" * 64,
        "subject_digest": proposal.review_digest,
        "idempotency_key": key,
        "actor_type": "human",
        "actor_id": "local-user",
        "comment": "ok",
        "approval_artifact": None,
        "now": NOW,
    }
    kwargs.update(overrides)
    return kwargs


def _reject_kwargs(project, proposal, *, key: str, **overrides: object) -> dict:
    kwargs: dict = {
        "proposal_id": proposal.id,
        "base_revision": project.current_revision,
        "subject_digest": proposal.review_digest,
        "idempotency_key": key,
        "actor_type": "human",
        "actor_id": "local-user",
        "comment": "no",
        "rejection_artifact": None,
        "now": NOW,
    }
    kwargs.update(overrides)
    return kwargs


def test_batch_and_proposal_lookups_report_missing_ids(container) -> None:
    with pytest.raises(CommandBatchNotFoundError):
        container.command_batches.get("bat_missing")
    with pytest.raises(CommandBatchNotFoundError):
        container.command_batches.created_at("bat_missing")
    with pytest.raises(ProposalNotFoundError):
        container.proposal_store.get("prop_missing")


def test_list_for_project_returns_created_proposals(
    container, frozen_requirement_set
) -> None:
    project, proposal = _ready(container, frozen_requirement_set, "accept")

    listed = container.proposal_store.list_for_project(project.id)

    assert [item.id for item in listed] == [proposal.id]


def test_find_existing_reports_batch_row_without_proposal(
    container, frozen_requirement_set
) -> None:
    project = container.projects.get(frozen_requirement_set.project_id)
    batch_bytes = _batch(project, frozen_requirement_set, "accept")
    proposal = container.proposals.create(batch_bytes, "decision-accept")
    with container.sessions.begin() as session:
        session.execute(
            delete(ChangeProposalRow).where(ChangeProposalRow.id == proposal.id)
        )

    with pytest.raises(RuntimeError, match="exists without proposal"):
        container.proposal_store.find_existing(_parse_batch(batch_bytes))


def test_create_queued_replays_existing_proposal_after_integrity_error(
    container, frozen_requirement_set, monkeypatch
) -> None:
    from sqlalchemy.exc import IntegrityError

    project = container.projects.get(frozen_requirement_set.project_id)
    batch_bytes = _batch(project, frozen_requirement_set, "accept")
    batch = _parse_batch(batch_bytes)
    store = container.proposal_store

    first = store.create_queued(batch)

    def raise_integrity(self, _batch):
        raise IntegrityError("INSERT failed", {}, Exception("unique key"))

    monkeypatch.setattr(type(store), "_create_queued", raise_integrity)

    replayed = store.create_queued(batch)

    assert replayed.id == first.id


def test_create_queued_maps_integrity_error_to_command_key_conflict(
    container, frozen_requirement_set, monkeypatch
) -> None:
    from sqlalchemy.exc import IntegrityError

    project = container.projects.get(frozen_requirement_set.project_id)
    batch = _parse_batch(_batch(project, frozen_requirement_set, "accept"))
    store = container.proposal_store

    def no_existing(self, session, batch, batch_json, digest):
        return None

    def conflict_key(self, session, batch):
        return batch.commands[0].idempotency_key

    monkeypatch.setattr(type(store), "_existing", no_existing)
    monkeypatch.setattr(type(store), "_conflicting_command_key", conflict_key)
    monkeypatch.setattr(
        type(store),
        "_create_queued",
        lambda self, _batch: (_ for _ in ()).throw(
            IntegrityError("INSERT failed", {}, Exception("unique key"))
        ),
    )

    with pytest.raises(IdempotencyConflictError, match="decision-accept:1"):
        store.create_queued(batch)


def test_create_queued_reraises_unmapped_integrity_error(
    container, frozen_requirement_set, monkeypatch
) -> None:
    from sqlalchemy.exc import IntegrityError

    project = container.projects.get(frozen_requirement_set.project_id)
    batch = _parse_batch(_batch(project, frozen_requirement_set, "accept"))
    store = container.proposal_store
    error = IntegrityError("INSERT failed", {}, Exception("unique key"))

    monkeypatch.setattr(
        type(store),
        "_create_queued",
        lambda self, _batch: (_ for _ in ()).throw(error),
    )
    monkeypatch.setattr(
        type(store), "_conflicting_command_key", lambda self, session, batch: None
    )

    with pytest.raises(IntegrityError):
        store.create_queued(batch)


def _two_leased_proposals(container, frozen_requirement_set):
    """Create two proposals and lease+start the second task; used to reach the
    proposal/task binding check with an actively valid lease token."""
    project = container.projects.get(frozen_requirement_set.project_id)
    first = container.proposals.create(
        _batch(project, frozen_requirement_set, "accept"), "decision-accept"
    )
    second = container.proposals.create(
        _batch(project, frozen_requirement_set, "reject"), "decision-reject"
    )
    container.tasks.claim_next("edge-worker", NOW, 60)
    lease = container.tasks.claim_next("edge-worker", NOW, 60)
    assert lease is not None and lease.task_id == second.task_id
    container.tasks.start(lease.task_id, lease.lease_token, NOW)
    return first, lease


def test_begin_execution_rejects_wrong_task_and_stale_token(
    container, frozen_requirement_set
) -> None:
    proposal, lease = _two_leased_proposals(container, frozen_requirement_set)

    with pytest.raises(ProposalNotFoundError):
        container.proposal_store.begin_execution(
            proposal_id=proposal.id,
            task_id=lease.task_id,
            lease_token=lease.lease_token,
            now=NOW,
        )
    with pytest.raises(StaleLeaseError):
        container.proposal_store.begin_execution(
            proposal_id=proposal.id,
            task_id=proposal.task_id,
            lease_token="lease-bogus",
            now=NOW,
        )


def test_begin_execution_returns_terminal_status_unchanged(
    container, frozen_requirement_set
) -> None:
    project = container.projects.get(frozen_requirement_set.project_id)
    proposal = container.proposals.create(
        _batch(project, frozen_requirement_set, "accept"), "decision-accept"
    )
    lease = container.tasks.claim_next("edge-worker", NOW, 60)
    assert lease is not None
    container.tasks.start(lease.task_id, lease.lease_token, NOW)
    store = container.proposal_store
    store.begin_execution(
        proposal_id=proposal.id,
        task_id=proposal.task_id,
        lease_token=lease.lease_token,
        now=NOW,
    )
    failed = store.mark_validation_failed(
        proposal_id=proposal.id,
        task_id=proposal.task_id,
        lease_token=lease.lease_token,
        now=NOW,
        error_code="KICAD_ERC_FAILED",
        semantic_diff_digest=None,
        evidence_set_digest="sha256:" + "1" * 64,
        result={"validations": {"kicad_erc": "fail"}},
        evidence=(),
    )
    assert failed.status is ProposalStatus.VALIDATION_FAILED

    again = store.begin_execution(
        proposal_id=proposal.id,
        task_id=proposal.task_id,
        lease_token=lease.lease_token,
        now=NOW,
    )

    assert again.status is ProposalStatus.VALIDATION_FAILED


def test_mark_validation_failed_rejects_wrong_task(
    container, frozen_requirement_set
) -> None:
    proposal, lease = _two_leased_proposals(container, frozen_requirement_set)

    with pytest.raises(ProposalNotFoundError):
        container.proposal_store.mark_validation_failed(
            proposal_id=proposal.id,
            task_id=lease.task_id,
            lease_token=lease.lease_token,
            now=NOW,
            error_code="KICAD_ERC_FAILED",
            semantic_diff_digest=None,
            evidence_set_digest="sha256:" + "1" * 64,
            result={},
            evidence=(),
        )


def test_mark_validation_failed_rejects_ready_status(
    container, frozen_requirement_set
) -> None:
    project, proposal, lease, batch_bytes = _begin(
        container, frozen_requirement_set, "accept"
    )
    candidate_revision = "git:" + "a" * 40
    registrations, semantic_digest, capability_digest = _evidence_bundle(
        container, project, proposal, batch_bytes, frozen_requirement_set,
        candidate_revision,
    )
    review_digest = proposal_review_digest(
        proposal_id=proposal.id,
        project_id=project.id,
        base_revision=project.current_revision,
        candidate_revision=candidate_revision,
        candidate_snapshot_digest="sha256:" + "d" * 64,
        requirement_set_digest=frozen_requirement_set.canonical_digest,
        semantic_diff_digest=semantic_digest,
        evidence_set_digest=registrations[-1].descriptor.digest,
        adapter_capability_digest=capability_digest,
    )
    container.proposal_store.mark_ready(
        proposal_id=proposal.id,
        task_id=proposal.task_id,
        lease_token=lease.lease_token,
        now=NOW,
        candidate_revision=candidate_revision,
        candidate_snapshot_digest="sha256:" + "d" * 64,
        review_digest=review_digest,
        semantic_diff_digest=semantic_digest,
        evidence_set_digest=registrations[-1].descriptor.digest,
        result={"validations": {}},
        evidence=tuple(registrations),
    )

    with pytest.raises(StaleLeaseError):
        container.proposal_store.mark_validation_failed(
            proposal_id=proposal.id,
            task_id=proposal.task_id,
            lease_token=lease.lease_token,
            now=NOW,
            error_code="KICAD_ERC_FAILED",
            semantic_diff_digest=None,
            evidence_set_digest="sha256:" + "1" * 64,
            result={},
            evidence=(),
        )


def test_mark_validation_failed_rejects_conflicting_artifact(
    container, frozen_requirement_set
) -> None:
    project = container.projects.get(frozen_requirement_set.project_id)
    proposal = container.proposals.create(
        _batch(project, frozen_requirement_set, "accept"), "decision-accept"
    )
    lease = container.tasks.claim_next("edge-worker", NOW, 60)
    assert lease is not None
    container.tasks.start(lease.task_id, lease.lease_token, NOW)
    store = container.proposal_store
    store.begin_execution(
        proposal_id=proposal.id,
        task_id=proposal.task_id,
        lease_token=lease.lease_token,
        now=NOW,
    )
    registered = container.artifacts.put_bytes(b"legit bytes", "application/json")
    from pcbflow.artifacts import ArtifactDescriptor

    with container.sessions.begin() as session:
        session.add(
            ArtifactRow(
                digest=registered.digest,
                size=registered.size + 5,
                media_type=registered.media_type,
                storage_path=str(registered.path),
                created_at=NOW,
            )
        )
    conflicting = EvidenceRegistration(
        descriptor=ArtifactDescriptor(
            digest=registered.digest,
            size=registered.size + 1,
            media_type=registered.media_type,
            path=registered.path,
        ),
        item=EvidenceItem(
            kind="kicad_erc",
            artifact_digest=registered.digest,
            media_type=registered.media_type,
            verdict="pass",
        ),
    )

    with pytest.raises(EvidenceConflictError):
        store.mark_validation_failed(
            proposal_id=proposal.id,
            task_id=proposal.task_id,
            lease_token=lease.lease_token,
            now=NOW,
            error_code="KICAD_ERC_FAILED",
            semantic_diff_digest=None,
            evidence_set_digest="sha256:" + "1" * 64,
            result={},
            evidence=(conflicting,),
        )


def test_mark_validation_failed_rejects_conflicting_evidence_row(
    container, frozen_requirement_set
) -> None:
    project = container.projects.get(frozen_requirement_set.project_id)
    proposal = container.proposals.create(
        _batch(project, frozen_requirement_set, "accept"), "decision-accept"
    )
    lease = container.tasks.claim_next("edge-worker", NOW, 60)
    assert lease is not None
    container.tasks.start(lease.task_id, lease.lease_token, NOW)
    store = container.proposal_store
    store.begin_execution(
        proposal_id=proposal.id,
        task_id=proposal.task_id,
        lease_token=lease.lease_token,
        now=NOW,
    )
    descriptor = container.artifacts.put_bytes(
        b'{"violations": []}', "application/json"
    )
    with container.sessions.begin() as session:
        session.add(
            ArtifactRow(
                digest=descriptor.digest,
                size=descriptor.size,
                media_type=descriptor.media_type,
                storage_path=str(descriptor.path),
                created_at=NOW,
            )
        )
        session.add(
            EvidenceRow(
                id="evd_preexisting",
                project_id=project.id,
                task_id=proposal.task_id,
                kind="kicad_erc",
                artifact_digest=descriptor.digest,
                subject="other-subject",
                verdict="fail",
                created_at=NOW,
            )
        )
    registration = EvidenceRegistration(
        descriptor=descriptor,
        item=EvidenceItem(
            kind="kicad_erc",
            artifact_digest=descriptor.digest,
            media_type=descriptor.media_type,
            verdict="pass",
        ),
    )

    with pytest.raises(EvidenceConflictError):
        store.mark_validation_failed(
            proposal_id=proposal.id,
            task_id=proposal.task_id,
            lease_token=lease.lease_token,
            now=NOW,
            error_code="KICAD_ERC_FAILED",
            semantic_diff_digest=None,
            evidence_set_digest="sha256:" + "1" * 64,
            result={},
            evidence=(registration,),
        )


def _begin(container, frozen_requirement_set, suffix: str):
    """Create, claim, start, and move a proposal into EXECUTING."""
    project = container.projects.get(frozen_requirement_set.project_id)
    batch_bytes = _batch(project, frozen_requirement_set, suffix)
    proposal = container.proposals.create(batch_bytes, f"decision-{suffix}")
    lease = container.tasks.claim_next("edge-worker", NOW, 60)
    assert lease is not None and lease.task_id == proposal.task_id
    container.tasks.start(lease.task_id, lease.lease_token, NOW)
    container.proposal_store.begin_execution(
        proposal_id=proposal.id,
        task_id=proposal.task_id,
        lease_token=lease.lease_token,
        now=NOW,
    )
    return project, proposal, lease, batch_bytes


def _evidence_bundle(
    container,
    project,
    proposal,
    batch_bytes,
    frozen_requirement_set,
    candidate_revision: str,
    snapshot_digest: str = "sha256:" + "d" * 64,
) -> tuple[list, str, str]:
    """Build the full ready evidence registrations for a proposal."""
    registrations: list[EvidenceRegistration] = []
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
                    "digest": snapshot_digest,
                }
            ),
            READY_EVIDENCE_MEDIA_TYPES["project_snapshot_after"],
        ),
        "git_text_diff": (
            b"diff --git a/board.kicad_sch b/board.kicad_sch\n",
            READY_EVIDENCE_MEDIA_TYPES["git_text_diff"],
        ),
        "schematic_semantic_diff": (
            canonical_json_bytes({"schema_version": "1.0", "changes": []}),
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
    return registrations, semantic.digest, capability.digest


def _mark_ready_kwargs(
    proposal,
    lease,
    candidate_revision: str,
    evidence_set_digest: str,
    semantic_digest: str,
    capability_digest: str,
) -> dict:
    return {
        "proposal_id": proposal.id,
        "task_id": proposal.task_id,
        "lease_token": lease.lease_token,
        "now": NOW,
        "candidate_revision": candidate_revision,
        "candidate_snapshot_digest": "sha256:" + "d" * 64,
        "review_digest": "sha256:" + "e" * 64,
        "semantic_diff_digest": semantic_digest,
        "evidence_set_digest": evidence_set_digest,
        "result": {"validations": {}},
        "evidence": None,  # filled by the caller
    }


def test_mark_ready_rejects_wrong_task(container, frozen_requirement_set) -> None:
    proposal, lease = _two_leased_proposals(container, frozen_requirement_set)

    with pytest.raises(ProposalNotFoundError):
        container.proposal_store.mark_ready(
            proposal_id=proposal.id,
            task_id=lease.task_id,
            lease_token=lease.lease_token,
            now=NOW,
            candidate_revision="git:" + "a" * 40,
            candidate_snapshot_digest="sha256:" + "d" * 64,
            review_digest="sha256:" + "e" * 64,
            semantic_diff_digest="sha256:" + "f" * 64,
            evidence_set_digest="sha256:" + "0" * 64,
            result={},
            evidence=(),
        )


def test_mark_ready_rejects_queued_status(container, frozen_requirement_set) -> None:
    project = container.projects.get(frozen_requirement_set.project_id)
    proposal = container.proposals.create(
        _batch(project, frozen_requirement_set, "accept"), "decision-accept"
    )
    lease = container.tasks.claim_next("edge-worker", NOW, 60)
    assert lease is not None
    container.tasks.start(lease.task_id, lease.lease_token, NOW)

    with pytest.raises(StaleLeaseError):
        container.proposal_store.mark_ready(
            proposal_id=proposal.id,
            task_id=proposal.task_id,
            lease_token=lease.lease_token,
            now=NOW,
            candidate_revision="git:" + "a" * 40,
            candidate_snapshot_digest="sha256:" + "d" * 64,
            review_digest="sha256:" + "e" * 64,
            semantic_diff_digest="sha256:" + "f" * 64,
            evidence_set_digest="sha256:" + "0" * 64,
            result={},
            evidence=(),
        )


def test_mark_ready_rejects_incomplete_evidence(
    container, frozen_requirement_set
) -> None:
    _project, proposal, lease, _batch_bytes = _begin(
        container, frozen_requirement_set, "accept"
    )

    with pytest.raises(ValueError, match="incomplete proposal evidence"):
        container.proposal_store.mark_ready(
            proposal_id=proposal.id,
            task_id=proposal.task_id,
            lease_token=lease.lease_token,
            now=NOW,
            candidate_revision="git:" + "a" * 40,
            candidate_snapshot_digest="sha256:" + "d" * 64,
            review_digest="sha256:" + "e" * 64,
            semantic_diff_digest="sha256:" + "f" * 64,
            evidence_set_digest="sha256:" + "0" * 64,
            result={},
            evidence=(),
        )


def test_mark_ready_rejects_evidence_set_digest_mismatch(
    container, frozen_requirement_set
) -> None:
    project, proposal, lease, batch_bytes = _begin(
        container, frozen_requirement_set, "accept"
    )
    registrations, semantic_digest, capability_digest = _evidence_bundle(
        container, project, proposal, batch_bytes, frozen_requirement_set,
        "git:" + "a" * 40,
    )
    kwargs = _mark_ready_kwargs(
        proposal, lease, "git:" + "a" * 40, "sha256:" + "0" * 64,
        semantic_digest, capability_digest,
    )
    kwargs["evidence"] = tuple(registrations)

    with pytest.raises(ValueError, match="evidence set digest mismatch"):
        container.proposal_store.mark_ready(**kwargs)


def test_mark_ready_rejects_unparseable_evidence_set(
    container, frozen_requirement_set
) -> None:
    project, proposal, lease, batch_bytes = _begin(
        container, frozen_requirement_set, "accept"
    )
    registrations, semantic_digest, capability_digest = _evidence_bundle(
        container, project, proposal, batch_bytes, frozen_requirement_set,
        "git:" + "a" * 40,
    )
    garbage = container.artifacts.put_bytes(
        b"definitely not json",
        "application/vnd.pcbflow.evidence-set+json",
    )
    registrations = [
        item
        for item in registrations
        if item.item.kind != "proposal_evidence_set"
    ]
    registrations.append(
        EvidenceRegistration(
            descriptor=garbage,
            item=EvidenceItem(
                kind="proposal_evidence_set",
                artifact_digest=garbage.digest,
                media_type=garbage.media_type,
                verdict="pass",
            ),
        )
    )
    kwargs = _mark_ready_kwargs(
        proposal, lease, "git:" + "a" * 40, garbage.digest,
        semantic_digest, capability_digest,
    )
    kwargs["evidence"] = tuple(registrations)

    with pytest.raises(ValueError, match="invalid proposal evidence set"):
        container.proposal_store.mark_ready(**kwargs)


def test_mark_ready_rejects_evidence_set_binding_mismatch(
    container, frozen_requirement_set
) -> None:
    project, proposal, lease, batch_bytes = _begin(
        container, frozen_requirement_set, "accept"
    )
    other_revision = "git:" + "f" * 40
    registrations, semantic_digest, capability_digest = _evidence_bundle(
        container, project, proposal, batch_bytes, frozen_requirement_set,
        other_revision,
    )
    kwargs = _mark_ready_kwargs(
        proposal, lease, "git:" + "a" * 40, registrations[-1].descriptor.digest,
        semantic_digest, capability_digest,
    )
    kwargs["evidence"] = tuple(registrations)

    with pytest.raises(ValueError, match="binding mismatch"):
        container.proposal_store.mark_ready(**kwargs)


def test_mark_ready_rejects_evidence_set_with_wrong_contents(
    container, frozen_requirement_set
) -> None:
    project, proposal, lease, batch_bytes = _begin(
        container, frozen_requirement_set, "accept"
    )
    candidate_revision = "git:" + "a" * 40
    registrations, semantic_digest, capability_digest = _evidence_bundle(
        container, project, proposal, batch_bytes, frozen_requirement_set,
        candidate_revision,
    )
    trimmed = EvidenceSet(
        project_id=project.id,
        task_id=proposal.task_id,
        proposal_id=proposal.id,
        base_revision=project.current_revision,
        candidate_revision=candidate_revision,
        artifacts=(registrations[0].item,),
    )
    bad_set = container.artifacts.put_bytes(
        canonical_json_bytes(trimmed.model_dump(mode="json")),
        "application/vnd.pcbflow.evidence-set+json",
    )
    registrations = [
        item
        for item in registrations
        if item.item.kind != "proposal_evidence_set"
    ]
    registrations.append(
        EvidenceRegistration(
            descriptor=bad_set,
            item=EvidenceItem(
                kind="proposal_evidence_set",
                artifact_digest=bad_set.digest,
                media_type=bad_set.media_type,
                verdict="pass",
            ),
        )
    )
    kwargs = _mark_ready_kwargs(
        proposal, lease, candidate_revision, bad_set.digest,
        semantic_digest, capability_digest,
    )
    kwargs["evidence"] = tuple(registrations)

    with pytest.raises(ValueError, match="invalid proposal evidence set contents"):
        container.proposal_store.mark_ready(**kwargs)


def test_mark_ready_replays_ready_proposal_unchanged(
    container, frozen_requirement_set
) -> None:
    project, proposal, lease, batch_bytes = _begin(
        container, frozen_requirement_set, "accept"
    )
    candidate_revision = "git:" + "a" * 40
    registrations, semantic_digest, capability_digest = _evidence_bundle(
        container, project, proposal, batch_bytes, frozen_requirement_set,
        candidate_revision,
    )
    review_digest = proposal_review_digest(
        proposal_id=proposal.id,
        project_id=project.id,
        base_revision=project.current_revision,
        candidate_revision=candidate_revision,
        candidate_snapshot_digest="sha256:" + "d" * 64,
        requirement_set_digest=frozen_requirement_set.canonical_digest,
        semantic_diff_digest=semantic_digest,
        evidence_set_digest=registrations[-1].descriptor.digest,
        adapter_capability_digest=capability_digest,
    )
    kwargs = _mark_ready_kwargs(
        proposal, lease, candidate_revision, registrations[-1].descriptor.digest,
        semantic_digest, capability_digest,
    )
    kwargs["evidence"] = tuple(registrations)
    kwargs["review_digest"] = review_digest
    store = container.proposal_store

    ready = store.mark_ready(**kwargs)
    assert ready.status is ProposalStatus.READY_FOR_REVIEW

    replayed = store.mark_ready(**kwargs)

    assert replayed.status is ProposalStatus.READY_FOR_REVIEW
    assert replayed.version == ready.version


def test_decide_accept_unknown_proposal(container, frozen_requirement_set) -> None:
    descriptor = container.artifacts.put_bytes(b"approval", "text/plain")
    kwargs = _approve_kwargs(
        type("P", (), {"current_revision": "git:" + "a" * 40, "version": 1})(),
        type("P2", (), {"id": "prop_missing", "review_digest": "sha256:" + "b" * 64})(),
        key="edge-missing",
    )
    kwargs["approval_artifact"] = descriptor

    with pytest.raises(ProposalNotFoundError):
        container.proposal_store.decide_accept(**kwargs)


def test_decide_accept_replays_and_conflicts_on_changed_inputs(
    container, frozen_requirement_set
) -> None:
    project, proposal = _ready(container, frozen_requirement_set, "accept")
    descriptor = container.artifacts.put_bytes(b"approval", "text/plain")
    store = container.proposal_store
    kwargs = _approve_kwargs(
        project, proposal, key="edge-accept-replay",
        approval_artifact=descriptor,
    )

    accepted = store.decide_accept(**kwargs)
    assert accepted.status is ProposalStatus.ACCEPTED

    replayed = store.accept(**kwargs)

    assert replayed.id == accepted.id
    assert replayed.status is ProposalStatus.ACCEPTED

    with pytest.raises(IdempotencyConflictError):
        store.decide_accept(**dict(kwargs, comment="changed"))


def test_decide_accept_replays_stale_decision_as_revision_conflict(
    container, frozen_requirement_set
) -> None:
    project, proposal = _ready(container, frozen_requirement_set, "stale")
    descriptor = container.artifacts.put_bytes(b"approval", "text/plain")
    base_revision = project.current_revision
    with container.sessions.begin() as session:
        session.execute(
            update(ProjectRow)
            .where(ProjectRow.id == project.id)
            .values(current_revision="git:" + "e" * 40, version=ProjectRow.version + 1)
        )
    kwargs = _approve_kwargs(
        project, proposal, key="edge-stale-replay",
        approval_artifact=descriptor, base_revision=base_revision,
        candidate_revision="git:" + "c" * 40,
    )
    store = container.proposal_store

    with pytest.raises(RevisionConflictError):
        store.decide_accept(**kwargs)

    with pytest.raises(RevisionConflictError):
        store.decide_accept(**kwargs)

    with pytest.raises(IdempotencyConflictError):
        store.decide_accept(**dict(kwargs, comment="different"))


def test_decide_accept_detects_project_version_conflict(
    container, frozen_requirement_set
) -> None:
    project, proposal = _ready(container, frozen_requirement_set, "accept")
    descriptor = container.artifacts.put_bytes(b"approval", "text/plain")
    kwargs = _approve_kwargs(
        project, proposal, key="edge-version-conflict",
        approval_artifact=descriptor,
        expected_project_version=project.version + 5,
    )

    with pytest.raises(RevisionConflictError):
        container.proposal_store.decide_accept(**kwargs)


def test_decide_accept_rejects_conflicting_approval_artifact(
    container, frozen_requirement_set
) -> None:
    project, proposal = _ready(container, frozen_requirement_set, "accept")
    registered = container.artifacts.put_bytes(b"approval", "text/plain")
    with container.sessions.begin() as session:
        session.add(
            ArtifactRow(
                digest=registered.digest,
                size=registered.size + 7,
                media_type=registered.media_type,
                storage_path=str(registered.path),
                created_at=NOW,
            )
        )
    kwargs = _approve_kwargs(
        project, proposal, key="edge-artifact-conflict",
        approval_artifact=registered,
    )

    with pytest.raises(EvidenceConflictError):
        container.proposal_store.decide_accept(**kwargs)


def test_decide_reject_unknown_proposal_replays_and_conflicts(
    container, frozen_requirement_set
) -> None:
    descriptor = container.artifacts.put_bytes(b"rejection", "text/plain")
    with pytest.raises(ProposalNotFoundError):
        container.proposal_store.decide_reject(
            proposal_id="prop_missing",
            base_revision="git:" + "a" * 40,
            subject_digest="sha256:" + "b" * 64,
            idempotency_key="edge-reject-missing",
            actor_type="human",
            actor_id="local-user",
            comment="no",
            rejection_artifact=descriptor,
            now=NOW,
        )

    project, proposal = _ready(container, frozen_requirement_set, "reject")
    store = container.proposal_store
    kwargs = _reject_kwargs(
        project, proposal, key="edge-reject-replay",
        rejection_artifact=descriptor,
    )

    rejected = store.decide_reject(**kwargs)
    assert rejected.status is ProposalStatus.REJECTED

    replayed = store.reject(**kwargs)

    assert replayed.id == rejected.id
    assert replayed.status is ProposalStatus.REJECTED

    with pytest.raises(IdempotencyConflictError):
        store.decide_reject(**dict(kwargs, comment="changed"))


def test_decide_reject_requires_reviewable_status(
    container, frozen_requirement_set
) -> None:
    project = container.projects.get(frozen_requirement_set.project_id)
    proposal = container.proposals.create(
        _batch(project, frozen_requirement_set, "reject"), "decision-reject"
    )
    descriptor = container.artifacts.put_bytes(b"rejection", "text/plain")

    with pytest.raises(RequestInvalidError, match="not reviewable"):
        container.proposal_store.decide_reject(
            proposal_id=proposal.id,
            base_revision=project.current_revision,
            subject_digest="sha256:" + "b" * 64,
            idempotency_key="decision-reject",
            actor_type="human",
            actor_id="local-user",
            comment="no",
            rejection_artifact=descriptor,
            now=NOW,
        )

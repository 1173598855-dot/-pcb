from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from pcbflow.approvals import ApprovalDigestMismatchError, GateDecisionStore
from pcbflow.canonical import canonical_json_bytes
from pcbflow.design_tables import RequirementSetRow
from pcbflow.repositories import IdempotencyConflictError
from pcbflow.requirement_store import RequirementStore
from pcbflow.requirements import (
    RequirementSetStatus,
    RequirementsBlockedError,
    load_requirement_payload,
)
from pcbflow.tables import ArtifactRow


def test_requirement_draft_is_idempotent_and_submitted_content_is_immutable(
    session_factory, artifact_store, managed_project, requirement_yaml: bytes
) -> None:
    store = RequirementStore(session_factory, artifact_store)
    payload = load_requirement_payload(requirement_yaml)
    first = store.create_draft(
        managed_project.id,
        managed_project.current_revision,
        payload,
        "requirement-draft-1",
    )
    repeated = store.create_draft(
        managed_project.id,
        managed_project.current_revision,
        payload,
        "requirement-draft-1",
    )
    assert repeated.id == first.id

    with artifact_store.open(first.canonical_artifact_digest) as artifact:
        assert artifact.read() == canonical_json_bytes(payload.model_dump(mode="json"))
    with session_factory() as session:
        row = session.get(ArtifactRow, first.canonical_artifact_digest)
        assert row is not None
        assert row.media_type == "application/vnd.pcbflow.requirements+json"

    submitted = store.mark_submitted(
        first.id,
        "requirement-submit-1",
        candidate_revision="git:" + "2" * 40,
        candidate_snapshot_digest="sha256:" + "b" * 64,
    )
    assert submitted.status is RequirementSetStatus.PENDING_APPROVAL

    with pytest.raises(ValueError, match="immutable"):
        store.replace_payload(submitted.id, payload)


def test_requirement_draft_key_compares_the_full_canonical_input(
    session_factory, artifact_store, managed_project, requirement_yaml: bytes
) -> None:
    store = RequirementStore(session_factory, artifact_store)
    payload = load_requirement_payload(requirement_yaml)
    store.create_draft(
        managed_project.id,
        managed_project.current_revision,
        payload,
        "requirement-draft-conflict",
    )
    changed = load_requirement_payload(
        requirement_yaml.replace(b"Local diagnostics.", b"Visible diagnostics.")
    )

    with pytest.raises(IdempotencyConflictError):
        store.create_draft(
            managed_project.id,
            managed_project.current_revision,
            changed,
            "requirement-draft-conflict",
        )
    with pytest.raises(IdempotencyConflictError):
        store.create_draft(
            managed_project.id,
            "git:" + "f" * 40,
            payload,
            "requirement-draft-conflict",
        )


def test_submission_key_is_idempotent_and_unique_per_project(
    session_factory, artifact_store, managed_project, requirement_yaml: bytes
) -> None:
    store = RequirementStore(session_factory, artifact_store)
    payload = load_requirement_payload(requirement_yaml)
    first = store.create_draft(
        managed_project.id,
        managed_project.current_revision,
        payload,
        "draft-for-submit-1",
    )
    second = store.create_draft(
        managed_project.id,
        managed_project.current_revision,
        payload,
        "draft-for-submit-2",
    )
    submitted = store.mark_submitted(
        first.id,
        "one-submission-key",
        candidate_revision="git:" + "2" * 40,
        candidate_snapshot_digest="sha256:" + "b" * 64,
    )
    replay = store.mark_submitted(
        first.id,
        "one-submission-key",
        candidate_revision="git:" + "2" * 40,
        candidate_snapshot_digest="sha256:" + "b" * 64,
    )
    assert replay == submitted
    assert store.find_by_submission_key(
        managed_project.id, "one-submission-key"
    ) == submitted

    with pytest.raises(IdempotencyConflictError):
        store.mark_submitted(
            second.id,
            "one-submission-key",
            candidate_revision="git:" + "3" * 40,
            candidate_snapshot_digest="sha256:" + "c" * 64,
        )


def test_requirement_store_persists_basic_reject_and_freeze_transitions(
    session_factory, artifact_store, managed_project, requirement_yaml: bytes
) -> None:
    store = RequirementStore(session_factory, artifact_store)
    payload = load_requirement_payload(requirement_yaml)
    rejected_draft = store.create_draft(
        managed_project.id,
        managed_project.current_revision,
        payload,
        "draft-to-reject",
    )
    rejected_pending = store.mark_submitted(
        rejected_draft.id,
        "submit-to-reject",
        candidate_revision="git:" + "2" * 40,
        candidate_snapshot_digest="sha256:" + "b" * 64,
    )
    assert store.reject(rejected_pending.id).status is RequirementSetStatus.REJECTED

    frozen_draft = store.create_draft(
        managed_project.id,
        managed_project.current_revision,
        payload,
        "draft-to-freeze",
    )
    frozen_pending = store.mark_submitted(
        frozen_draft.id,
        "submit-to-freeze",
        candidate_revision="git:" + "3" * 40,
        candidate_snapshot_digest="sha256:" + "c" * 64,
    )
    frozen = store.approve_and_freeze(
        frozen_pending.id, frozen_revision="git:" + "3" * 40
    )
    assert frozen.status is RequirementSetStatus.FROZEN
    assert frozen.frozen_revision == "git:" + "3" * 40

    with session_factory() as session:
        statuses = dict(
            session.execute(
                select(RequirementSetRow.id, RequirementSetRow.status).where(
                    RequirementSetRow.id.in_([rejected_pending.id, frozen_pending.id])
                )
            ).all()
        )
    assert statuses == {
        rejected_pending.id: RequirementSetStatus.REJECTED.value,
        frozen_pending.id: RequirementSetStatus.FROZEN.value,
    }


def test_gate_decision_rejects_digest_reuse_with_different_subject(
    session_factory, managed_project
) -> None:
    decisions = GateDecisionStore(session_factory)
    first = decisions.add(
        project_id=managed_project.id,
        gate="G1",
        subject_type="requirement_set",
        subject_id="reqset_1",
        subject_digest="sha256:" + "a" * 64,
        base_revision=managed_project.current_revision,
        idempotency_key="approve-g1",
        decision="approve",
        actor_type="human",
        actor_id="local-user",
        comment="approved",
    )
    repeated = decisions.add(
        project_id=managed_project.id,
        gate="G1",
        subject_type="requirement_set",
        subject_id="reqset_1",
        subject_digest="sha256:" + "a" * 64,
        base_revision=managed_project.current_revision,
        idempotency_key="approve-g1",
        decision="approve",
        actor_type="human",
        actor_id="local-user",
        comment="approved",
    )
    assert repeated == first

    with pytest.raises(ApprovalDigestMismatchError):
        decisions.add(
            project_id=managed_project.id,
            gate="G1",
            subject_type="requirement_set",
            subject_id="reqset_1",
            subject_digest="sha256:" + "b" * 64,
            base_revision=managed_project.current_revision,
            idempotency_key="approve-g1",
            decision="approve",
            actor_type="human",
            actor_id="local-user",
            comment="approved",
        )

    with pytest.raises(IdempotencyConflictError):
        decisions.add(
            project_id=managed_project.id,
            gate="G1",
            subject_type="requirement_set",
            subject_id="reqset_1",
            subject_digest="sha256:" + "a" * 64,
            base_revision=managed_project.current_revision,
            idempotency_key="approve-g1",
            decision="approve",
            actor_type="human",
            actor_id="different-actor",
            comment="approved",
        )


def _snapshot(root: Path) -> dict[Path, bytes]:
    return {
        path.relative_to(root): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_submit_and_approve_g1_advances_revision_without_touching_import_source(
    container, managed_project, requirement_yaml: bytes
) -> None:
    source_before = _snapshot(managed_project.source_path)
    draft = container.requirements.import_draft(
        managed_project.id,
        requirement_yaml,
        "requirements-import-1",
    )
    pending = container.requirements.submit(draft.id, "requirements-submit-1")
    assert pending.status is RequirementSetStatus.PENDING_APPROVAL
    assert pending.candidate_revision is not None

    subject_digest = pending.subject_digest()
    frozen = container.approvals.decide_g1(
        requirement_set_id=pending.id,
        subject_digest=subject_digest,
        decision="approve",
        actor_type="human",
        actor_id="local-user",
        comment="requirements accepted",
        idempotency_key="g1-approve-1",
    )
    repeated_submit = container.requirements.submit(
        draft.id, "requirements-submit-1"
    )
    repeated_import = container.requirements.import_draft(
        managed_project.id,
        requirement_yaml,
        "requirements-import-1",
    )
    project = container.projects.get(managed_project.id)
    assert frozen.status is RequirementSetStatus.FROZEN
    assert repeated_submit == frozen
    assert repeated_import == frozen
    assert frozen.frozen_revision == project.current_revision
    assert project.active_requirement_set_id == frozen.id
    assert _snapshot(managed_project.source_path) == source_before


def test_reject_g1_is_idempotent_and_does_not_advance_revision(
    container, managed_project, requirement_yaml: bytes
) -> None:
    source_before = _snapshot(managed_project.source_path)
    draft = container.requirements.import_draft(
        managed_project.id,
        requirement_yaml,
        "requirements-reject-import",
    )
    pending = container.requirements.submit(
        draft.id, "requirements-reject-submit"
    )
    rejected = container.approvals.decide_g1(
        requirement_set_id=pending.id,
        subject_digest=pending.subject_digest(),
        decision="reject",
        actor_type="human",
        actor_id="local-user",
        comment="requirements need revision",
        idempotency_key="g1-reject-1",
    )
    repeated = container.approvals.decide_g1(
        requirement_set_id=pending.id,
        subject_digest=pending.subject_digest(),
        decision="reject",
        actor_type="human",
        actor_id="local-user",
        comment="requirements need revision",
        idempotency_key="g1-reject-1",
    )

    project = container.projects.get(managed_project.id)
    assert rejected.status is RequirementSetStatus.REJECTED
    assert repeated.id == rejected.id
    assert project.current_revision == managed_project.current_revision
    assert project.active_requirement_set_id is None
    assert _snapshot(managed_project.source_path) == source_before


def test_submit_blocks_open_blocking_assumptions_before_creating_candidate(
    container, managed_project, requirement_yaml: bytes
) -> None:
    blocked_yaml = requirement_yaml.replace(
        b"assumptions: []",
        b"assumptions:\n"
        b"  - id: ASM-POWER-001\n"
        b"    statement: Input voltage is not confirmed.\n"
        b"    blocking: true\n"
        b"    owner: hardware-lead\n"
        b"    closure_condition: Confirm the input voltage range.\n",
    )
    draft = container.requirements.import_draft(
        managed_project.id, blocked_yaml, "requirements-blocked-import"
    )

    with pytest.raises(RequirementsBlockedError) as captured:
        container.requirements.submit(draft.id, "requirements-blocked-submit")

    assert captured.value.blocking_ids == ("ASM-POWER-001",)
    assert container.requirement_store.get(draft.id).status is RequirementSetStatus.DRAFT
    assert container.projects.get(managed_project.id).current_revision == (
        managed_project.current_revision
    )

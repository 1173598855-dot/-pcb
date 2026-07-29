from __future__ import annotations

import pytest
from sqlalchemy import select

from pcbflow.approvals import ApprovalDigestMismatchError, GateDecisionStore
from pcbflow.canonical import canonical_json_bytes
from pcbflow.design_tables import RequirementSetRow
from pcbflow.repositories import IdempotencyConflictError
from pcbflow.requirement_store import RequirementStore
from pcbflow.requirements import RequirementSetStatus, load_requirement_payload
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

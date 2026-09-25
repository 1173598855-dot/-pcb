from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC
from pathlib import Path
from threading import Event, Lock

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from pcbflow.approvals import ApprovalDigestMismatchError, GateDecisionStore
from pcbflow.artifacts import ArtifactDescriptor
from pcbflow.canonical import canonical_json_bytes
from pcbflow.container import build_container
from pcbflow.design_tables import GateDecisionRow, OutboxEventRow, RequirementSetRow
from pcbflow.repositories import IdempotencyConflictError, RevisionConflictError
from pcbflow.requirement_store import RequirementSetNotFoundError, RequirementStore
from pcbflow.requirements import (
    RequirementsBlockedError,
    RequirementSetStatus,
    load_requirement_payload,
)
from pcbflow.revisions import ProjectNotManagedError
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


def test_requirement_store_validates_keys_and_missing_ids(
    session_factory, artifact_store, managed_project, requirement_yaml: bytes
) -> None:
    store = RequirementStore(session_factory, artifact_store)
    payload = load_requirement_payload(requirement_yaml)

    assert store.list_for_project("missing-project") == ()
    assert store.find_by_import_key(managed_project.id, "missing-import") is None
    assert store.find_by_submission_key(managed_project.id, "missing-submit") is None
    with pytest.raises(RequirementSetNotFoundError):
        store.get("missing-requirement-set")
    with pytest.raises(ValueError, match="idempotency key"):
        store.create_draft(
            managed_project.id,
            managed_project.current_revision,
            payload,
            "",
        )
    with pytest.raises(ValueError, match="idempotency key"):
        store.mark_submitted(
            "missing-requirement-set",
            "",
            candidate_revision="git:" + "1" * 40,
            candidate_snapshot_digest="sha256:" + "a" * 64,
        )
    with pytest.raises(RequirementSetNotFoundError):
        store.reject("missing-requirement-set")
    with pytest.raises(RequirementSetNotFoundError):
        store.approve_and_freeze(
            "missing-requirement-set", frozen_revision="git:" + "1" * 40
        )


def test_requirement_store_replaces_and_lists_mutable_draft(
    session_factory, artifact_store, managed_project, requirement_yaml: bytes
) -> None:
    store = RequirementStore(session_factory, artifact_store)
    original = load_requirement_payload(requirement_yaml)
    changed = load_requirement_payload(
        requirement_yaml.replace(b"Local diagnostics.", b"Visible diagnostics.")
    )
    draft = store.create_draft(
        managed_project.id,
        managed_project.current_revision,
        original,
        "replace-draft",
    )

    replaced = store.replace_payload(draft.id, changed)

    assert replaced.payload == changed
    assert replaced.canonical_digest != draft.canonical_digest
    assert store.list_for_project(managed_project.id) == (replaced,)


def test_requirement_store_enforces_reject_and_freeze_state_guards(
    session_factory, artifact_store, managed_project, requirement_yaml: bytes
) -> None:
    store = RequirementStore(session_factory, artifact_store)
    payload = load_requirement_payload(requirement_yaml)
    draft = store.create_draft(
        managed_project.id,
        managed_project.current_revision,
        payload,
        "guarded-draft",
    )

    with pytest.raises(ValueError, match="cannot be rejected"):
        store.reject(draft.id)
    with pytest.raises(ValueError, match="cannot be frozen"):
        store.approve_and_freeze(
            draft.id, frozen_revision="git:" + "1" * 40
        )

    pending = store.mark_submitted(
        draft.id,
        "guarded-submit",
        candidate_revision="git:" + "2" * 40,
        candidate_snapshot_digest="sha256:" + "b" * 64,
    )
    with pytest.raises(ValueError, match="must match"):
        store.approve_and_freeze(
            pending.id, frozen_revision="git:" + "3" * 40
        )

    frozen = store.approve_and_freeze(
        pending.id, frozen_revision="git:" + "2" * 40
    )
    assert store.approve_and_freeze(
        frozen.id, frozen_revision="git:" + "2" * 40
    ) == frozen
    with pytest.raises(IdempotencyConflictError):
        store.approve_and_freeze(
            frozen.id, frozen_revision="git:" + "3" * 40
        )
    with pytest.raises(ValueError, match="cannot be rejected"):
        store.reject(frozen.id)

    rejected_draft = store.create_draft(
        managed_project.id,
        managed_project.current_revision,
        payload,
        "guarded-reject-draft",
    )
    rejected_pending = store.mark_submitted(
        rejected_draft.id,
        "guarded-reject-submit",
        candidate_revision="git:" + "4" * 40,
        candidate_snapshot_digest="sha256:" + "d" * 64,
    )
    rejected = store.reject(rejected_pending.id)
    assert store.reject(rejected.id) == rejected


def test_requirement_service_rejects_unmanaged_and_conflicting_state(
    container, managed_project, requirement_yaml: bytes, tmp_path: Path
) -> None:
    payload = load_requirement_payload(requirement_yaml)
    unmanaged_source = tmp_path / "registered-source"
    unmanaged_source.mkdir()
    (unmanaged_source / "board.kicad_sch").write_bytes(b"(kicad_sch)\n")
    unmanaged = container.projects.create(
        "Registered", unmanaged_source, "registered-project"
    )

    with pytest.raises(ProjectNotManagedError):
        container.requirements.import_draft(
            unmanaged.id, requirement_yaml, "unmanaged-import"
        )
    unmanaged_draft = container.requirement_store.create_draft(
        unmanaged.id,
        "git:" + "0" * 40,
        payload,
        "unmanaged-draft",
    )
    with pytest.raises(ProjectNotManagedError):
        container.requirements.submit(unmanaged_draft.id, "unmanaged-submit")

    stale = container.requirement_store.create_draft(
        managed_project.id,
        "git:" + "f" * 40,
        payload,
        "stale-base-draft",
    )
    with pytest.raises(RevisionConflictError):
        container.requirements.submit(stale.id, "stale-base-submit")

    first = container.requirement_store.create_draft(
        managed_project.id,
        managed_project.current_revision,
        payload,
        "service-conflict-first",
    )
    second = container.requirement_store.create_draft(
        managed_project.id,
        managed_project.current_revision,
        payload,
        "service-conflict-second",
    )
    container.requirement_store.mark_submitted(
        first.id,
        "shared-submission",
        candidate_revision="git:" + "5" * 40,
        candidate_snapshot_digest="sha256:" + "e" * 64,
    )
    with pytest.raises(IdempotencyConflictError):
        container.requirements.submit(second.id, "shared-submission")
    with pytest.raises(IdempotencyConflictError):
        container.requirements.submit(first.id, "different-submission")


def test_gate_decision_validates_requests_and_requires_artifact_evidence(
    session_factory, artifact_store, managed_project, requirement_yaml: bytes
) -> None:
    store = GateDecisionStore(session_factory)
    add_request = {
        "project_id": managed_project.id,
        "gate": "G1",
        "subject_type": "requirement_set",
        "subject_id": "reqset-validation",
        "subject_digest": "sha256:" + "a" * 64,
        "base_revision": managed_project.current_revision,
        "idempotency_key": "validation-key",
        "decision": "approve",
        "actor_type": "human",
        "actor_id": "local-user",
        "comment": "validated",
    }
    with pytest.raises(ValueError, match="idempotency key"):
        store.add(**(add_request | {"idempotency_key": ""}))
    with pytest.raises(ValueError, match="unsupported"):
        store.add(**(add_request | {"decision": "abstain"}))

    payload = load_requirement_payload(requirement_yaml)
    requirements = RequirementStore(session_factory, artifact_store)
    draft = requirements.create_draft(
        managed_project.id,
        managed_project.current_revision,
        payload,
        "artifact-required-draft",
    )
    pending = requirements.mark_submitted(
        draft.id,
        "artifact-required-submit",
        candidate_revision="git:" + "6" * 40,
        candidate_snapshot_digest="sha256:" + "f" * 64,
    )
    decision_request = {
        "project_id": managed_project.id,
        "requirement_set_id": pending.id,
        "subject_digest": pending.subject_digest(),
        "base_revision": pending.base_revision,
        "candidate_revision": pending.candidate_revision,
        "candidate_snapshot_digest": pending.candidate_snapshot_digest,
        "expected_project_version": managed_project.version,
        "idempotency_key": "artifact-required-decision",
        "decision": "approve",
        "actor_type": "human",
        "actor_id": "local-user",
        "comment": "requires evidence",
    }
    with pytest.raises(ValueError, match="idempotency key"):
        store.decide_g1(**(decision_request | {"idempotency_key": ""}))
    with pytest.raises(ValueError, match="unsupported"):
        store.decide_g1(**(decision_request | {"decision": "abstain"}))
    with pytest.raises(RuntimeError, match="content-addressed store"):
        store.decide_g1(**decision_request)


def test_gate_decision_store_rejects_conflicting_artifact_metadata(
    session_factory, artifact_store, managed_project
) -> None:
    descriptor = artifact_store.put_bytes(
        b"approval evidence", "application/vnd.pcbflow.g1-approval+json"
    )
    with session_factory.begin() as session:
        GateDecisionStore._register_artifact(
            session, descriptor, managed_project.created_at
        )

    conflicting = ArtifactDescriptor(
        digest=descriptor.digest,
        size=descriptor.size + 1,
        media_type=descriptor.media_type,
        path=descriptor.path,
    )
    with session_factory.begin() as session, pytest.raises(RuntimeError, match="artifact descriptor conflict"):
        GateDecisionStore._register_artifact(
            session, conflicting, managed_project.created_at
        )


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


def test_reject_g1_does_not_reconcile_a_drifted_design_ref(
    container, managed_project, requirement_yaml: bytes
) -> None:
    draft = container.requirements.import_draft(
        managed_project.id,
        requirement_yaml,
        "requirements-reject-drift-import",
    )
    pending = container.requirements.submit(
        draft.id, "requirements-reject-drift-submit"
    )
    assert pending.candidate_revision is not None
    container.revisions.git.update_ref(
        container.revisions.repo_path(managed_project.id),
        "refs/heads/design",
        pending.candidate_revision,
        expected_revision=managed_project.current_revision,
    )

    container.approvals.decide_g1(
        requirement_set_id=pending.id,
        subject_digest=pending.subject_digest(),
        decision="reject",
        actor_type="human",
        actor_id="local-user",
        comment="do not reconcile rejection",
        idempotency_key="g1-reject-drift",
    )

    assert container.revisions.resolve_design_ref(managed_project.id) == (
        pending.candidate_revision
    )


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_g1_decision_persists_immutable_approval_artifact_and_replays_it(
    container, managed_project, requirement_yaml: bytes, decision: str
) -> None:
    draft = container.requirements.import_draft(
        managed_project.id,
        requirement_yaml,
        f"requirements-artifact-{decision}-import",
    )
    pending = container.requirements.submit(
        draft.id, f"requirements-artifact-{decision}-submit"
    )
    with container.sessions() as session:
        artifact_digests_before = set(session.scalars(select(ArtifactRow.digest)))

    request = {
        "requirement_set_id": pending.id,
        "subject_digest": pending.subject_digest(),
        "decision": decision,
        "actor_type": "human",
        "actor_id": "local-user",
        "comment": f"artifact evidence for {decision}",
        "idempotency_key": f"g1-artifact-{decision}",
    }
    recorded = container.approvals.decide_g1(**request)
    replayed = container.approvals.decide_g1(**request)

    with container.sessions() as session:
        decision_row = session.scalar(
            select(GateDecisionRow).where(
                GateDecisionRow.project_id == managed_project.id,
                GateDecisionRow.idempotency_key == request["idempotency_key"],
            )
        )
        events = [
            row
            for row in session.scalars(select(OutboxEventRow))
            if row.payload_json.get("requirement_set_id") == pending.id
        ]
        artifact_digests_after = set(session.scalars(select(ArtifactRow.digest)))
        assert decision_row is not None
        assert len(events) == 1
        event = events[0]
        artifact_digest = event.payload_json["approval_artifact_digest"]
        assert event.payload_json["gate_decision_id"] == decision_row.id
        artifact = session.get(ArtifactRow, artifact_digest)
        assert artifact is not None
        assert artifact.media_type == "application/vnd.pcbflow.g1-approval+json"

    with container.artifacts.open(artifact_digest) as stored:
        raw = stored.read()
    approval = json.loads(raw)
    assert raw == canonical_json_bytes(approval)
    assert approval["schema_version"] == "1.0"
    assert approval["gate"] == "G1"
    assert approval["project_id"] == managed_project.id
    assert approval["requirement_set_id"] == pending.id
    assert approval["base_revision"] == pending.base_revision
    assert approval["base_snapshot_digest"] == managed_project.project_snapshot_digest
    assert approval["candidate_revision"] == pending.candidate_revision
    assert approval["candidate_snapshot_digest"] == pending.candidate_snapshot_digest
    assert approval["subject_digest"] == pending.subject_digest()
    assert approval["decision"] == decision
    assert approval["actor"] == {"type": "human", "id": "local-user"}
    assert approval["comment"] == request["comment"]
    assert approval["created_at"] == decision_row.created_at.replace(
        tzinfo=UTC
    ).isoformat().replace("+00:00", "Z")
    assert recorded.id == replayed.id
    assert artifact_digests_after == artifact_digests_before | {artifact_digest}


def test_concurrent_g1_replay_serializes_before_creating_approval_artifact(
    container, managed_project, requirement_yaml: bytes, monkeypatch
) -> None:
    draft = container.requirements.import_draft(
        managed_project.id,
        requirement_yaml,
        "requirements-concurrent-g1-import",
    )
    pending = container.requirements.submit(
        draft.id, "requirements-concurrent-g1-submit"
    )
    request = {
        "requirement_set_id": pending.id,
        "subject_digest": pending.subject_digest(),
        "decision": "reject",
        "actor_type": "human",
        "actor_id": "local-user",
        "comment": "concurrent idempotency replay",
        "idempotency_key": "g1-concurrent-replay",
    }
    first_artifact_started = Event()
    second_artifact_started = Event()
    second_transaction_attempted = Event()
    release_first_artifact = Event()
    calls_lock = Lock()
    artifact_calls = 0
    begin_calls = 0
    original_create = GateDecisionStore._create_g1_approval_artifact
    original_execute = Session.execute

    def pause_first_artifact(store, **kwargs):
        nonlocal artifact_calls
        with calls_lock:
            artifact_calls += 1
            call_number = artifact_calls
        if call_number == 1:
            first_artifact_started.set()
            assert release_first_artifact.wait(timeout=5)
        else:
            second_artifact_started.set()
        return original_create(store, **kwargs)

    monkeypatch.setattr(
        GateDecisionStore, "_create_g1_approval_artifact", pause_first_artifact
    )

    def observe_begin_immediate(session, statement, *args, **kwargs):
        nonlocal begin_calls
        if getattr(statement, "text", None) == "BEGIN IMMEDIATE":
            with calls_lock:
                begin_calls += 1
                begin_number = begin_calls
            if begin_number == 2:
                second_transaction_attempted.set()
        return original_execute(session, statement, *args, **kwargs)

    monkeypatch.setattr(Session, "execute", observe_begin_immediate)
    second_container = build_container(container.settings)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(container.approvals.decide_g1, **request)
            assert first_artifact_started.wait(timeout=5)
            second = executor.submit(second_container.approvals.decide_g1, **request)

            assert second_transaction_attempted.wait(timeout=5)
            assert not second_artifact_started.wait(timeout=0.1)
            release_first_artifact.set()
            first_result = first.result(timeout=10)
            second_result = second.result(timeout=10)

        assert not second_artifact_started.is_set()
        assert first_result == second_result
        with container.sessions() as session:
            decisions = session.scalars(
                select(GateDecisionRow).where(
                    GateDecisionRow.project_id == managed_project.id,
                    GateDecisionRow.idempotency_key == request["idempotency_key"],
                )
            ).all()
            assert len(decisions) == 1
            events = [
                row
                for row in session.scalars(select(OutboxEventRow))
                if row.payload_json.get("gate_decision_id") == decisions[0].id
            ]
            approval_rows = [
                row
                for row in session.scalars(select(ArtifactRow))
                if row.media_type == "application/vnd.pcbflow.g1-approval+json"
            ]

        assert len(events) == 1
        assert len(approval_rows) == 1
        approval_digest = events[0].payload_json["approval_artifact_digest"]
        assert approval_rows[0].digest == approval_digest
        assert container.artifacts._path(approval_digest).exists()
        approval_objects = []
        for artifact_path in container.artifacts.root.glob("objects/sha256/*/*/*"):
            try:
                artifact = json.loads(artifact_path.read_bytes())
            except json.JSONDecodeError:
                continue
            if artifact.get("gate") == "G1":
                approval_objects.append(artifact_path)
        assert approval_objects == [container.artifacts._path(approval_digest)]
    finally:
        release_first_artifact.set()
        second_container.dispose()


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

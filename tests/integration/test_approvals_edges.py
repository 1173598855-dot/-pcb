"""Edge-path tests for approvals: G1 decision branches and the G3 evidence
payload validators.

The G1 happy path is covered by the frozen_requirement_set fixture and the
requirement workflow tests; here the store's replay, mismatch, conflict, and
superseding branches are exercised directly. The G3 payload validators are
pure functions and are tested with crafted payloads.
"""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import update

from pcbflow.approvals import ApprovalDigestMismatchError, PcbApprovalService
from pcbflow.canonical import canonical_json_bytes
from pcbflow.pcb_candidates import (
    G3_EVIDENCE_SET_MEDIA_TYPE,
    G3_REQUIRED_EVIDENCE,
    G3_REQUIRED_EVIDENCE_MEDIA_TYPES,
    validate_candidate_digest,
)
from pcbflow.repositories import IdempotencyConflictError, RevisionConflictError
from pcbflow.requirement_store import RequirementSetNotFoundError
from pcbflow.tables import ProjectRow


def _pending(container, managed_project, requirement_yaml, key: str):
    draft = container.requirements.import_draft(managed_project.id, requirement_yaml, key)
    return container.requirements.submit(draft.id, f"{key}-submit")


def _store_kwargs(pending, project, key: str, **overrides: object) -> dict:
    kwargs: dict = {
        "project_id": project.id,
        "requirement_set_id": pending.id,
        "subject_digest": pending.subject_digest(),
        "base_revision": pending.base_revision,
        "candidate_revision": pending.candidate_revision,
        "candidate_snapshot_digest": pending.candidate_snapshot_digest,
        "expected_project_version": project.version,
        "idempotency_key": key,
        "decision": "approve",
        "actor_type": "human",
        "actor_id": "local-user",
        "comment": "g1 edge",
    }
    kwargs.update(overrides)
    return kwargs


def test_decide_g1_replays_existing_decision(
    container, managed_project, requirement_yaml
) -> None:
    pending = _pending(container, managed_project, requirement_yaml, "g1-edge-replay")
    kwargs = dict(
        requirement_set_id=pending.id,
        subject_digest=pending.subject_digest(),
        decision="approve",
        actor_type="human",
        actor_id="local-user",
        comment="g1 edge",
        idempotency_key="g1-edge-replay",
    )

    first = container.approvals.decide_g1(**kwargs)
    replayed = container.approvals.decide_g1(**kwargs)

    assert replayed.id == first.id
    assert replayed.status is first.status


def test_decide_g1_rejects_missing_requirement_set(
    container, managed_project, requirement_yaml
) -> None:
    with pytest.raises(RequirementSetNotFoundError):
        container.approvals.decide_g1(
            requirement_set_id="reqset_missing",
            subject_digest="sha256:" + "a" * 64,
            decision="approve",
            actor_type="human",
            actor_id="local-user",
            comment="g1 edge",
            idempotency_key="g1-edge-missing",
        )


def test_decide_g1_rejects_project_mismatch(
    container, managed_project, requirement_yaml
) -> None:
    pending = _pending(container, managed_project, requirement_yaml, "g1-edge-pm")
    project = container.projects.get(managed_project.id)
    kwargs = _store_kwargs(pending, project, "g1-edge-pm", project_id="prj_other")

    with pytest.raises(IdempotencyConflictError):
        container.gate_decisions.decide_g1(**kwargs)


def test_decide_g1_rejects_digest_mismatch(
    container, managed_project, requirement_yaml
) -> None:
    pending = _pending(container, managed_project, requirement_yaml, "g1-edge-digest")

    with pytest.raises(ApprovalDigestMismatchError):
        container.approvals.decide_g1(
            requirement_set_id=pending.id,
            subject_digest="sha256:" + "b" * 64,
            decision="approve",
            actor_type="human",
            actor_id="local-user",
            comment="g1 edge",
            idempotency_key="g1-edge-digest",
        )


def test_decide_g1_rejects_revision_mismatch(
    container, managed_project, requirement_yaml
) -> None:
    pending = _pending(container, managed_project, requirement_yaml, "g1-edge-rev")
    project = container.projects.get(managed_project.id)
    kwargs = _store_kwargs(
        pending, project, "g1-edge-rev",
        candidate_revision="git:" + "f" * 40,
    )

    with pytest.raises(IdempotencyConflictError):
        container.gate_decisions.decide_g1(**kwargs)


def test_decide_g1_conflicts_on_stale_project_version(
    container, managed_project, requirement_yaml
) -> None:
    pending = _pending(container, managed_project, requirement_yaml, "g1-edge-stale")
    project = container.projects.get(managed_project.id)
    kwargs = _store_kwargs(
        pending, project, "g1-edge-stale",
        expected_project_version=project.version - 1,
    )

    with pytest.raises(RevisionConflictError):
        container.gate_decisions.decide_g1(**kwargs)


def test_decide_g1_conflicts_on_project_version_race(
    container, managed_project, requirement_yaml
) -> None:
    pending = _pending(container, managed_project, requirement_yaml, "g1-edge-race")
    project = container.projects.get(managed_project.id)
    kwargs = _store_kwargs(
        pending, project, "g1-edge-race",
        expected_project_version=project.version + 3,
    )

    with pytest.raises(RevisionConflictError):
        container.gate_decisions.decide_g1(**kwargs)


def test_decide_g1_reports_missing_active_requirement_set(
    container, managed_project, requirement_yaml
) -> None:
    pending = _pending(container, managed_project, requirement_yaml, "g1-edge-ghost")
    project = container.projects.get(managed_project.id)
    with container.sessions.begin() as session:
        session.execute(
            update(ProjectRow)
            .where(ProjectRow.id == project.id)
            .values(active_requirement_set_id="reqset_ghost")
        )
    kwargs = _store_kwargs(pending, project, "g1-edge-ghost")

    with pytest.raises(RuntimeError, match="active requirement set is missing"):
        container.gate_decisions.decide_g1(**kwargs)


def test_decide_g1_reports_unfrozen_active_requirement_set(
    container, managed_project, requirement_yaml
) -> None:
    stale_active = _pending(
        container, managed_project, requirement_yaml, "g1-edge-active"
    )
    pending = _pending(container, managed_project, requirement_yaml, "g1-edge-unfrozen")
    project = container.projects.get(managed_project.id)
    with container.sessions.begin() as session:
        session.execute(
            update(ProjectRow)
            .where(ProjectRow.id == project.id)
            .values(active_requirement_set_id=stale_active.id)
        )
    kwargs = _store_kwargs(pending, project, "g1-edge-unfrozen")

    with pytest.raises(RuntimeError, match="active requirement set is not frozen"):
        container.gate_decisions.decide_g1(**kwargs)


def _candidate(**overrides: object) -> SimpleNamespace:
    fields: dict = {
        "id": "cand_edge",
        "project_id": "prj_edge",
        "task_id": "tsk_edge",
        "base_revision": "git:" + "1" * 40,
        "base_snapshot_digest": "sha256:" + "2" * 64,
        "board_snapshot_digest": "sha256:" + "3" * 64,
        "rulepack_digest": "sha256:" + "4" * 64,
        "capability_digest": "sha256:" + "5" * 64,
        "authority_digest": "sha256:" + "6" * 64,
        "operations_digest": "sha256:" + "7" * 64,
        "result": {
            "candidate_board_snapshot_digest": "sha256:" + "8" * 64,
            "evidence_set_digest": "sha256:" + "9" * 64,
            "evidence_artifacts": {},
        },
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _drc_digest(snapshot: dict) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(snapshot)).hexdigest()


def test_expected_candidate_digest_rejects_malformed_inputs() -> None:
    assert PcbApprovalService._expected_candidate_digest(_candidate(result=None)) is None
    assert (
        PcbApprovalService._expected_candidate_digest(
            _candidate(
                result={
                    "candidate_board_snapshot_digest": "not-a-digest",
                    "evidence_set_digest": "sha256:" + "9" * 64,
                }
            )
        )
        is None
    )
    assert (
        PcbApprovalService._expected_candidate_digest(
            _candidate(
                result={
                    "candidate_board_snapshot_digest": "sha256:" + "8" * 64,
                    "evidence_set_digest": None,
                }
            )
        )
        is None
    )


def test_expected_candidate_digest_matches_review_digest() -> None:
    from pcbflow.pcb_candidates import pcb_candidate_review_digest

    candidate = _candidate()
    expected = PcbApprovalService._expected_candidate_digest(candidate)
    assert expected == pcb_candidate_review_digest(
        candidate_id=candidate.id,
        project_id=candidate.project_id,
        base_revision=candidate.base_revision,
        base_snapshot_digest=candidate.base_snapshot_digest,
        board_snapshot_digest=candidate.board_snapshot_digest,
        candidate_board_snapshot_digest=candidate.result["candidate_board_snapshot_digest"],
        rulepack_digest=candidate.rulepack_digest,
        capability_digest=candidate.capability_digest,
        authority_digest=candidate.authority_digest,
        operations_digest=candidate.operations_digest,
        evidence_set_digest=candidate.result["evidence_set_digest"],
    )


def test_verify_evidence_set_rejects_malformed_shapes(container) -> None:
    service = container.pcb_approvals

    bad_artifacts = _candidate(
        result={
            "candidate_board_snapshot_digest": "sha256:" + "8" * 64,
            "evidence_set_digest": "sha256:" + "9" * 64,
            "evidence_artifacts": "not-a-dict",
        }
    )
    assert service._verify_evidence_set(bad_artifacts) is False

    missing_artifact = _candidate(result={"evidence_set_digest": "sha256:" + "9" * 64})
    assert service._verify_evidence_set(missing_artifact) is False

    unverified = _candidate()
    assert service._verify_evidence_set(unverified) is False


def test_verify_evidence_set_rejects_non_canonical_or_mismatched_sets(
    container,
) -> None:
    service = container.pcb_approvals
    candidate = _candidate()
    snapshot_digest = candidate.result["candidate_board_snapshot_digest"]

    non_canonical = container.artifacts.put_bytes(
        b'{"b": 1, "a": 2}', G3_EVIDENCE_SET_MEDIA_TYPE
    )
    candidate.result["evidence_set_digest"] = non_canonical.digest
    assert service._verify_evidence_set(candidate) is False

    wrong_keys = {"schema_version": "1.0", "candidate_id": candidate.id}
    wrong_keys_artifact = container.artifacts.put_bytes(
        canonical_json_bytes(wrong_keys), G3_EVIDENCE_SET_MEDIA_TYPE
    )
    candidate.result["evidence_set_digest"] = wrong_keys_artifact.digest
    assert service._verify_evidence_set(candidate) is False

    unwritable_bytes = b"\xff\xfe-not-utf8"
    binary_artifact = container.artifacts.put_bytes(
        unwritable_bytes, G3_EVIDENCE_SET_MEDIA_TYPE
    )
    candidate.result["evidence_set_digest"] = binary_artifact.digest
    assert service._verify_evidence_set(candidate) is False
    assert snapshot_digest


def _evidence_set_bytes(candidate, items) -> bytes:
    return canonical_json_bytes(
        {
            "schema_version": "1.0",
            "candidate_id": candidate.id,
            "project_id": candidate.project_id,
            "task_id": candidate.task_id,
            "base_revision": candidate.base_revision,
            "items": items,
        }
    )


def _full_items() -> list[dict]:
    return [
        {
            "kind": kind,
            "artifact_digest": "sha256:" + "c" * 64,
            "media_type": G3_REQUIRED_EVIDENCE_MEDIA_TYPES[kind],
            "verdict": "pass",
        }
        for kind in sorted(G3_REQUIRED_EVIDENCE)
    ]


def test_verify_evidence_set_rejects_bad_items(container) -> None:
    service = container.pcb_approvals
    candidate = _candidate()
    items = _full_items()

    wrong_binding = _evidence_set_bytes(candidate, items)
    wrong_binding = wrong_binding.replace(
        candidate.id.encode(), b"cand_other", 1
    )
    assert json.loads(wrong_binding)["candidate_id"] == "cand_other"
    artifact = container.artifacts.put_bytes(
        wrong_binding, G3_EVIDENCE_SET_MEDIA_TYPE
    )
    candidate.result["evidence_set_digest"] = artifact.digest
    assert service._verify_evidence_set(candidate) is False

    candidate = _candidate()
    duplicate = _full_items()
    duplicate.append(dict(duplicate[0]))
    artifact = container.artifacts.put_bytes(
        _evidence_set_bytes(candidate, duplicate), G3_EVIDENCE_SET_MEDIA_TYPE
    )
    candidate.result["evidence_set_digest"] = artifact.digest
    assert service._verify_evidence_set(candidate) is False

    candidate = _candidate()
    failing = _full_items()
    failing[0]["verdict"] = "fail"
    artifact = container.artifacts.put_bytes(
        _evidence_set_bytes(candidate, failing), G3_EVIDENCE_SET_MEDIA_TYPE
    )
    candidate.result["evidence_set_digest"] = artifact.digest
    assert service._verify_evidence_set(candidate) is False

    candidate = _candidate()
    missing_kind = _full_items()[:-1]
    artifact = container.artifacts.put_bytes(
        _evidence_set_bytes(candidate, missing_kind), G3_EVIDENCE_SET_MEDIA_TYPE
    )
    candidate.result["evidence_set_digest"] = artifact.digest
    assert service._verify_evidence_set(candidate) is False

    candidate = _candidate()
    wrong_media = _full_items()
    wrong_media[0]["media_type"] = "application/json"
    artifact = container.artifacts.put_bytes(
        _evidence_set_bytes(candidate, wrong_media), G3_EVIDENCE_SET_MEDIA_TYPE
    )
    candidate.result["evidence_set_digest"] = artifact.digest
    assert service._verify_evidence_set(candidate) is False


def test_native_drc_payload_validator() -> None:
    valid = {
        "schema_version": "1.0",
        "reports": [{"kind": "drc", "findings": [
            {"rule_id": "R1", "severity": "warning", "subject": "x", "message": "m"}
        ]}],
    }
    assert PcbApprovalService._valid_native_drc_payload(valid) is True

    for payload in (
        None,
        [],
        {},
        {"schema_version": "2.0", "reports": [{"kind": "drc", "findings": []}]},
        {"schema_version": "1.0", "reports": []},
        {"schema_version": "1.0"},
        {"schema_version": "1.0", "reports": {}},
        {"schema_version": "1.0", "reports": [{"kind": "  ", "findings": []}]},
        {"schema_version": "1.0", "reports": [{"kind": "drc", "findings": {}}]},
        {
            "schema_version": "1.0",
            "reports": [
                {"kind": "drc", "findings": [{"rule_id": "R1", "severity": "error",
                                              "subject": "x", "message": "m"}]}
            ],
        },
        {
            "schema_version": "1.0",
            "reports": [
                {"kind": "drc", "findings": [{"rule_id": "R1", "severity": "warning"}]}
            ],
        },
    ):
        assert PcbApprovalService._valid_native_drc_payload(payload) is False


def test_boardir_validation_payload_validator() -> None:
    snapshot = {"board": {"layers": 2}}
    digest = _drc_digest(snapshot)
    candidate = _candidate(
        result={"candidate_board_snapshot_digest": digest, "evidence_artifacts": {}}
    )
    valid = {
        "schema_version": "1.0",
        "snapshot_digest": digest,
        "snapshot": snapshot,
        "findings": [],
    }
    assert PcbApprovalService._valid_boardir_validation_payload(valid, candidate) is True

    other_snapshot = {"board": {"layers": 4}}
    wrong_content = dict(valid, snapshot=other_snapshot)
    assert (
        PcbApprovalService._valid_boardir_validation_payload(wrong_content, candidate)
        is False
    )

    with_error = dict(
        valid,
        findings=[
            {"rule_id": "R1", "severity": "critical", "subject": "x", "message": "m"}
        ],
    )
    assert (
        PcbApprovalService._valid_boardir_validation_payload(with_error, candidate)
        is False
    )

    other_candidate = _candidate(
        result={"candidate_board_snapshot_digest": "sha256:" + "e" * 64}
    )
    assert (
        PcbApprovalService._valid_boardir_validation_payload(valid, other_candidate)
        is False
    )
    assert PcbApprovalService._valid_boardir_validation_payload(None, candidate) is False


def test_candidate_summary_payload_validator() -> None:
    candidate = _candidate()
    base_payload = {
        "schema_version": "1.0",
        "candidate_id": candidate.id,
        "project_id": candidate.project_id,
        "base_revision": candidate.base_revision,
        "source_snapshot_digest_before": candidate.base_snapshot_digest,
        "source_snapshot_digest_after": candidate.base_snapshot_digest,
        "board_snapshot_digest": candidate.board_snapshot_digest,
        "candidate_board_snapshot_digest": candidate.result[
            "candidate_board_snapshot_digest"
        ],
        "rulepack_digest": candidate.rulepack_digest,
        "capability_digest": candidate.capability_digest,
        "authority_digest": candidate.authority_digest,
        "operations_digest": candidate.operations_digest,
        "blocking_finding_count": 0,
        "unconnected_net_ids": [],
    }
    assert PcbApprovalService._valid_candidate_summary_payload(base_payload, candidate) is True

    wrong_id = dict(base_payload, candidate_id="cand_other")
    assert PcbApprovalService._valid_candidate_summary_payload(wrong_id, candidate) is False

    no_base = _candidate(base_snapshot_digest=None)
    without_base = {
        key: value
        for key, value in base_payload.items()
        if key not in {"source_snapshot_digest_before", "source_snapshot_digest_after"}
    }
    without_base["source_snapshot_digest_before"] = None
    without_base["source_snapshot_digest_after"] = None
    assert (
        PcbApprovalService._valid_candidate_summary_payload(without_base, no_base)
        is False
    )


def test_read_canonical_json_handles_missing_and_non_canonical(container) -> None:
    service = container.pcb_approvals

    assert service._read_canonical_json("sha256:" + "0" * 64) is None

    non_canonical = container.artifacts.put_bytes(
        b'{"b": 1, "a": 2}', "application/json"
    )
    assert service._read_canonical_json(non_canonical.digest) is None

    canonical = container.artifacts.put_bytes(
        canonical_json_bytes({"a": 2, "b": 1}), "application/json"
    )
    assert service._read_canonical_json(canonical.digest) == {"a": 2, "b": 1}


def test_validate_candidate_digest_rejects_non_digest() -> None:
    with pytest.raises(ValueError):
        validate_candidate_digest("nope", field="evidence_set_digest")
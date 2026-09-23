from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, text

from pcbflow.approvals import ApprovalDigestMismatchError
from pcbflow.board import (
    BoardObjectId,
    BoardSnapshot,
    FixtureBoardAdapter,
    PlaceFootprints,
    PointUm,
)
from pcbflow.board.operations import FootprintPlacement
from pcbflow.board.rulepack import ManufacturingRulePack
from pcbflow.canonical import canonical_json_bytes
from pcbflow.config import Settings
from pcbflow.container import build_container
from pcbflow.design_tables import PcbCandidateRow
from pcbflow.domain import (
    EdaKind,
    EdaOperation,
    NormalizedFinding,
    RequestInvalidError,
    TaskStatus,
    utc_now,
)
from pcbflow.eda import EdaCapability, ProjectEdaAuthorityInput
from pcbflow.pcb_candidates import (
    PcbCandidateNotReviewableError,
    PcbCandidateStatus,
    pcb_candidate_review_digest,
)
from pcbflow.pcb_workflow import (
    CAPABILITY_EVIDENCE_KIND,
    CAPABILITY_MEDIA_TYPE,
    LCEDA_CAPABILITY_TASK_KIND,
    capability_artifact_bytes,
)
from pcbflow.repositories import EvidenceConflictError, StaleLeaseError
from pcbflow.tables import ArtifactRow, EvidenceRow, TaskRow

_BASE_REVISION = "git:" + "b" * 40


class CountingFixtureAdapter(FixtureBoardAdapter):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.apply_calls = 0
        self.load_calls = 0

    def load_snapshot(self, project_dir):
        self.load_calls += 1
        return super().load_snapshot(project_dir)

    def apply_operations(self, candidate, operations, expected_snapshot):
        self.apply_calls += 1
        return super().apply_operations(candidate, operations, expected_snapshot)


class CancelAfterDrcFixtureAdapter(FixtureBoardAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.drc_calls = 0
        self._on_drc = None

    def cancel_after_drc(self, callback) -> None:
        self._on_drc = callback

    def run_drc(self, candidate):
        reports = super().run_drc(candidate)
        self.drc_calls += 1
        assert self._on_drc is not None
        self._on_drc()
        return reports


class EmptyNativeDrcFixtureAdapter(FixtureBoardAdapter):
    def run_drc(self, candidate):
        return ()


class MalformedNativeDrcFixtureAdapter(FixtureBoardAdapter):
    def run_drc(self, candidate):
        return (None,)


@pytest.fixture
def g3_container(database_url: str, tmp_path: Path):
    settings = Settings.from_env(
        {
            "PCBFLOW_DATA_DIR": str(tmp_path / "service-data"),
            "PCBFLOW_DATABASE_URL": database_url,
        }
    )
    services = build_container(
        settings,
        pcb_adapter_override=FixtureBoardAdapter(),
    )
    try:
        yield services
    finally:
        services.dispose()


@pytest.fixture
def g3_drc_blocked_container(database_url: str, tmp_path: Path):
    settings = Settings.from_env(
        {
            "PCBFLOW_DATA_DIR": str(tmp_path / "service-data"),
            "PCBFLOW_DATABASE_URL": database_url,
        }
    )
    adapter = FixtureBoardAdapter(
        drc_findings=(
            NormalizedFinding("LCEDA.DRC.CLEARANCE", "error", "seg_1", "clearance"),
        )
    )
    services = build_container(settings, pcb_adapter_override=adapter)
    try:
        yield services
    finally:
        services.dispose()


@pytest.fixture
def g3_unverified_container(database_url: str, tmp_path: Path):
    settings = Settings.from_env(
        {
            "PCBFLOW_DATA_DIR": str(tmp_path / "service-data"),
            "PCBFLOW_DATABASE_URL": database_url,
        }
    )
    adapter = CountingFixtureAdapter()
    services = build_container(settings, pcb_adapter_override=adapter)
    try:
        yield services, adapter
    finally:
        services.dispose()


@pytest.fixture
def g3_cancellation_container(database_url: str, tmp_path: Path):
    settings = Settings.from_env(
        {
            "PCBFLOW_DATA_DIR": str(tmp_path / "service-data"),
            "PCBFLOW_DATABASE_URL": database_url,
        }
    )
    adapter = CancelAfterDrcFixtureAdapter()
    services = build_container(settings, pcb_adapter_override=adapter)
    try:
        yield services, adapter
    finally:
        services.dispose()


@pytest.fixture
def g3_empty_native_drc_container(database_url: str, tmp_path: Path):
    settings = Settings.from_env(
        {
            "PCBFLOW_DATA_DIR": str(tmp_path / "service-data"),
            "PCBFLOW_DATABASE_URL": database_url,
        }
    )
    services = build_container(
        settings,
        pcb_adapter_override=EmptyNativeDrcFixtureAdapter(),
    )
    try:
        yield services
    finally:
        services.dispose()


@pytest.fixture
def g3_malformed_native_drc_container(database_url: str, tmp_path: Path):
    settings = Settings.from_env(
        {
            "PCBFLOW_DATA_DIR": str(tmp_path / "service-data"),
            "PCBFLOW_DATABASE_URL": database_url,
        }
    )
    services = build_container(
        settings,
        pcb_adapter_override=MalformedNativeDrcFixtureAdapter(),
    )
    try:
        yield services
    finally:
        services.dispose()


def _verified_capability(
    services, project_id: str, *, operations: frozenset[EdaOperation] | None = None
) -> str:
    capability = EdaCapability(
        available=True,
        executable=Path("fixture-lceda.exe"),
        version="fixture",
        executable_digest="sha256:" + "1" * 64,
        profile_id="lceda-pro-v1",
        profile_revision=1,
        operations=frozenset(EdaOperation) if operations is None else operations,
        write_verified=True,
        reason=None,
    )
    descriptor = services.artifacts.put_bytes(
        capability_artifact_bytes(capability), CAPABILITY_MEDIA_TYPE
    )
    task = services.tasks.enqueue(
        LCEDA_CAPABILITY_TASK_KIND,
        {"project_id": project_id},
        "fixture-capability",
        project_id,
    )
    lease = services.tasks.claim_next("fixture-capability-worker", utc_now(), 30)
    assert lease is not None and lease.task_id == task.id
    services.tasks.start(lease.task_id, lease.lease_token, utc_now())
    services.evidence.add_report(
        project_id,
        task.id,
        descriptor,
        CAPABILITY_EVIDENCE_KIND,
        "lceda-pro-v1",
        "pass",
        lease_token=lease.lease_token,
        now=utc_now(),
    )
    services.tasks.complete(
        lease.task_id,
        lease.lease_token,
        {"capability_digest": descriptor.digest, "write_verified": True},
        utc_now(),
    )
    return descriptor.digest


def _create_candidate(
    services,
    tmp_path: Path,
    *,
    algorithm_evidence=None,
    verify_capability: bool = True,
    capability_operations: frozenset[EdaOperation] | None = None,
    managed: bool = False,
):
    fixture_root = Path(__file__).parents[1] / "fixtures" / "lceda-pro" / "minimal"
    source = tmp_path / "fixture-source"
    source.mkdir()
    (source / "expected-boardir.json").write_bytes(
        (fixture_root / "expected-boardir.json").read_bytes()
    )
    snapshot = BoardSnapshot.load_json((source / "expected-boardir.json").read_bytes())
    rulepack = ManufacturingRulePack.load_json(
        (Path(__file__).parents[1] / "fixtures" / "boardir" / "stm32-environment-controller-2l-rulepack.json").read_bytes()
    )
    project = services.projects.create("Fixture LCEDA board", source, "g3-project")
    if managed:
        project = services.revisions.adopt(project.id, "g3-managed-adopt")
    services.eda_authorities.configure(
        project.id,
        ProjectEdaAuthorityInput(
            eda_kind=EdaKind.LCEDA_PRO,
            eda_profile_id="lceda-pro-v1",
            board_profile_id=rulepack.profile_id,
            rulepack_digest=rulepack.canonical_digest(),
        ),
        "g3-authority",
    )
    capability_digest = (
        _verified_capability(
            services, project.id, operations=capability_operations
        )
        if verify_capability
        else "sha256:" + "e" * 64
    )
    operation = PlaceFootprints(
        project_id=project.id,
        baseline_revision=project.current_revision or _BASE_REVISION,
        risk="low",
        rulepack_digest=rulepack.canonical_digest(),
        target_object_ids=(BoardObjectId("U_MCU"),),
        idempotency_key="g3-place-mcu",
        expected_snapshot_digest=snapshot.canonical_digest(),
        placements=(
            FootprintPlacement(BoardObjectId("U_MCU"), PointUm(53_000, 40_000), "F.Cu"),
        ),
    )
    candidate = services.pcb_candidates.create(
        project_id=project.id,
        base_revision=project.current_revision or _BASE_REVISION,
        base_snapshot_digest=(
            project.project_snapshot_digest
            if managed
            else services.revisions.snapshot_digest(source)
        ),
        board_snapshot_digest=snapshot.canonical_digest(),
        rulepack_digest=rulepack.canonical_digest(),
        capability_digest=capability_digest,
        operations=(operation,),
        algorithm_evidence=(
            algorithm_evidence
            if algorithm_evidence is not None
            else {
                "rulepack": rulepack.to_canonical_dict(),
                "placement": {"objective_version": "fixture-placement-v1"},
                "routing": {"final_unconnected_nets": []},
                "copper": {"objective_version": "fixture-copper-v1"},
            }
        ),
        idempotency_key="g3-candidate",
        require_capability=verify_capability,
    )
    return project, candidate, source, fixture_root


def test_managed_candidate_executes_from_frozen_revision_after_source_drift(
    g3_container, tmp_path: Path
) -> None:
    _project, candidate, source, _fixture_root = _create_candidate(
        g3_container, tmp_path, managed=True
    )
    (source / "expected-boardir.json").write_text("external drift", encoding="utf-8")

    assert g3_container.worker.run_once() is True

    ready = g3_container.pcb_candidates.get(candidate.id)
    assert ready.status is PcbCandidateStatus.READY_FOR_G3


def _rebind_published_evidence(
    services,
    candidate_id: str,
    *,
    kind: str,
    artifact_bytes: bytes,
    media_type: str,
):
    candidate = services.pcb_candidates.get(candidate_id)
    assert candidate.result is not None
    with services.artifacts.open(candidate.result["evidence_set_digest"]) as stream:
        evidence_set = json.loads(stream.read().decode("utf-8"))
    replacement = services.artifacts.put_bytes(artifact_bytes, media_type)
    for item in evidence_set["items"]:
        if item["kind"] == kind:
            assert item["media_type"] == replacement.media_type
            item["artifact_digest"] = replacement.digest
            break
    else:
        raise AssertionError(f"{kind} evidence is required")
    replacement_evidence_set = services.artifacts.put_bytes(
        canonical_json_bytes(evidence_set),
        "application/vnd.pcbflow.pcb-candidate-evidence-set+json",
    )

    with services.sessions.begin() as session:
        for descriptor in (replacement, replacement_evidence_set):
            session.add(
                ArtifactRow(
                    digest=descriptor.digest,
                    size=descriptor.size,
                    media_type=descriptor.media_type,
                    storage_path=str(descriptor.path),
                    created_at=utc_now(),
                )
            )
        evidence_row = session.scalar(
            select(EvidenceRow).where(
                EvidenceRow.task_id == candidate.task_id,
                EvidenceRow.kind == kind,
            )
        )
        evidence_set_row = session.scalar(
            select(EvidenceRow).where(
                EvidenceRow.task_id == candidate.task_id,
                EvidenceRow.kind == "pcb_candidate_evidence_set",
            )
        )
        candidate_row = session.get(PcbCandidateRow, candidate.id)
        assert evidence_row is not None
        assert evidence_set_row is not None
        assert candidate_row is not None and isinstance(candidate_row.result_json, dict)
        evidence_row.artifact_digest = replacement.digest
        evidence_set_row.artifact_digest = replacement_evidence_set.digest
        result = dict(candidate_row.result_json)
        artifacts = dict(result["evidence_artifacts"])
        artifacts[kind] = replacement.digest
        result["evidence_artifacts"] = artifacts
        result["evidence_set_digest"] = replacement_evidence_set.digest
        result["candidate_digest"] = pcb_candidate_review_digest(
            candidate_id=candidate.id,
            project_id=candidate.project_id,
            base_revision=candidate.base_revision,
            base_snapshot_digest=candidate.base_snapshot_digest,
            board_snapshot_digest=candidate.board_snapshot_digest,
            candidate_board_snapshot_digest=result["candidate_board_snapshot_digest"],
            rulepack_digest=candidate.rulepack_digest,
            capability_digest=candidate.capability_digest,
            authority_digest=candidate.authority_digest,
            operations_digest=candidate.operations_digest,
            evidence_set_digest=replacement_evidence_set.digest,
        )
        candidate_row.result_json = result
    rebound = services.pcb_candidates.get(candidate.id)
    assert rebound.result is not None
    return rebound


def test_batch_evidence_registration_rolls_back_reports_and_findings_together(
    g3_container, tmp_path: Path
) -> None:
    project, candidate, _source, _fixture_root = _create_candidate(g3_container, tmp_path)
    lease = g3_container.tasks.claim_next("batch-evidence-worker", utc_now(), 30)
    assert lease is not None and lease.task_id == candidate.task_id
    g3_container.tasks.start(lease.task_id, lease.lease_token, utc_now())
    first = g3_container.artifacts.put_bytes(b"batch-first", "application/octet-stream")
    second = g3_container.artifacts.put_bytes(b"batch-second", "application/octet-stream")

    with pytest.raises(EvidenceConflictError):
        g3_container.evidence.add_reports_and_findings(
            project_id=project.id,
            task_id=candidate.task_id,
            reports={
                "batch-first": (first, candidate.id, "pass"),
                "batch-second": (second, candidate.id, "pass"),
            },
            findings={
                "batch-first": (
                    NormalizedFinding("PCB.TEST", "error", "U1", "same finding"),
                    NormalizedFinding("PCB.TEST", "warning", "U1", "same finding"),
                )
            },
            lease_token=lease.lease_token,
            now=utc_now(),
        )

    assert all(
        item.task_id != candidate.task_id
        for item in g3_container.evidence.list_for_project(project.id)
    )
    with g3_container.sessions() as session:
        assert session.get(ArtifactRow, first.digest) is None
        assert session.get(ArtifactRow, second.digest) is None


def test_batch_evidence_registration_fences_with_time_after_lock(
    g3_container, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, candidate, _source, _fixture_root = _create_candidate(g3_container, tmp_path)
    lease = g3_container.tasks.claim_next("batch-evidence-time-worker", utc_now(), 30)
    assert lease is not None and lease.task_id == candidate.task_id
    started_at = utc_now()
    g3_container.tasks.start(lease.task_id, lease.lease_token, started_at)
    lease_expiry = started_at + timedelta(seconds=1)
    with g3_container.sessions.begin() as session:
        row = session.get(TaskRow, lease.task_id)
        assert row is not None
        row.lease_expires_at = lease_expiry
    later = lease_expiry + timedelta(seconds=1)
    monkeypatch.setattr("pcbflow.repositories.utc_now", lambda: later)
    first = g3_container.artifacts.put_bytes(b"expired-first", "application/octet-stream")

    with pytest.raises(StaleLeaseError):
        g3_container.evidence.add_reports_and_findings(
            project_id=project.id,
            task_id=candidate.task_id,
            reports={"expired": (first, candidate.id, "pass")},
            findings={},
            lease_token=lease.lease_token,
            now=started_at,
        )

    assert all(
        item.task_id != candidate.task_id
        for item in g3_container.evidence.list_for_project(project.id)
    )


def test_candidate_execution_publishes_complete_evidence_then_accepts_idempotent_g3(
    g3_container, tmp_path: Path
) -> None:
    project, candidate, source, fixture_root = _create_candidate(
        g3_container, tmp_path
    )

    assert g3_container.worker.run_once() is True

    ready = g3_container.pcb_candidates.get(candidate.id)
    assert ready.status is PcbCandidateStatus.READY_FOR_G3
    assert ready.output_kind == "native_candidate"
    assert ready.result is not None
    assert ready.result["blocking_finding_count"] == 0
    assert ready.result["unconnected_net_count"] == 0
    assert set(ready.result["evidence_kinds"]) == {
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
    assert (source / "expected-boardir.json").read_bytes() == (
        fixture_root / "expected-boardir.json"
    ).read_bytes()
    input_snapshot = BoardSnapshot.load_json(
        (source / "expected-boardir.json").read_bytes()
    )
    rulepack = ManufacturingRulePack.load_json(
        (
            Path(__file__).parents[1]
            / "fixtures"
            / "boardir"
            / "stm32-environment-controller-2l-rulepack.json"
        ).read_bytes()
    )
    for kind, expected in (
        ("pcb_input_snapshot", input_snapshot.canonical_bytes()),
        ("rulepack", rulepack.canonical_bytes()),
    ):
        with g3_container.artifacts.open(
            ready.result["evidence_artifacts"][kind]
        ) as stream:
            assert stream.read() == expected
    assert g3_container.tasks.get(candidate.task_id).status is TaskStatus.SUCCEEDED

    approved = g3_container.pcb_approvals.decide_g3(
        candidate_id=ready.id,
        candidate_digest=ready.result["candidate_digest"],
        idempotency_key="g3-approve",
        actor_id="fixture-reviewer",
        decision="approve",
        comment="fixture approval",
    )
    replay = g3_container.pcb_approvals.decide_g3(
        candidate_id=ready.id,
        candidate_digest=ready.result["candidate_digest"],
        idempotency_key="g3-approve",
        actor_id="fixture-reviewer",
        decision="approve",
        comment="fixture approval",
    )

    assert replay == approved
    assert approved.status is PcbCandidateStatus.G3_APPROVED
    decision = g3_container.gate_decisions.find_by_key(project.id, "g3-approve")
    assert decision is not None
    assert decision.gate == "G3_PCB"
    assert decision.subject_type == "pcb_candidate"
    assert decision.subject_id == candidate.id
    assert decision.base_revision == _BASE_REVISION


def test_candidate_execution_reuses_canonical_board_and_rulepack_bytes(
    g3_container, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _project, candidate, _source, _fixture_root = _create_candidate(
        g3_container, tmp_path
    )
    board_calls = 0
    rulepack_calls = 0
    original_board_bytes = BoardSnapshot.canonical_bytes
    original_rulepack_bytes = ManufacturingRulePack.canonical_bytes

    def count_board_bytes(snapshot: BoardSnapshot) -> bytes:
        nonlocal board_calls
        board_calls += 1
        return original_board_bytes(snapshot)

    def count_rulepack_bytes(rulepack: ManufacturingRulePack) -> bytes:
        nonlocal rulepack_calls
        rulepack_calls += 1
        return original_rulepack_bytes(rulepack)

    monkeypatch.setattr(BoardSnapshot, "canonical_bytes", count_board_bytes)
    monkeypatch.setattr(
        ManufacturingRulePack, "canonical_bytes", count_rulepack_bytes
    )

    assert g3_container.worker.run_once() is True

    assert g3_container.pcb_candidates.get(candidate.id).status is PcbCandidateStatus.READY_FOR_G3
    assert board_calls == 2
    assert rulepack_calls == 1


def test_g3_approval_registers_its_canonical_artifact(
    g3_container, tmp_path: Path
) -> None:
    _project, candidate, _source, _fixture_root = _create_candidate(
        g3_container, tmp_path
    )

    assert g3_container.worker.run_once() is True
    ready = g3_container.pcb_candidates.get(candidate.id)
    assert ready.result is not None
    approved = g3_container.pcb_approvals.decide_g3(
        candidate_id=ready.id,
        candidate_digest=ready.result["candidate_digest"],
        idempotency_key="g3-approval-artifact",
        actor_id="fixture-reviewer",
        decision="approve",
        comment="approval artifact must be registered",
    )

    assert approved.result is not None
    approval_digest = approved.result["g3_decision"]["approval_artifact_digest"]
    with g3_container.sessions() as session:
        artifact = session.get(ArtifactRow, approval_digest)
    assert artifact is not None
    assert artifact.media_type == "application/vnd.pcbflow.g3-approval+json"
    assert g3_container.artifacts.verify(approval_digest)


def test_g3_approval_has_no_direct_candidate_store_bypass(g3_container) -> None:
    assert not hasattr(g3_container.pcb_candidates, "approve_g3")
    assert not hasattr(g3_container.pcb_candidates, "record_g3_decision")


@pytest.mark.parametrize(
    ("idempotency_key", "actor_id"),
    [(" ", "fixture-reviewer"), ("g3-invalid-actor", " ")],
)
def test_invalid_g3_inputs_leave_no_gate_decision(
    g3_container, tmp_path: Path, idempotency_key: str, actor_id: str
) -> None:
    project, candidate, _source, _fixture_root = _create_candidate(
        g3_container, tmp_path
    )

    assert g3_container.worker.run_once() is True
    ready = g3_container.pcb_candidates.get(candidate.id)
    assert ready.result is not None
    with pytest.raises(RequestInvalidError):
        g3_container.pcb_approvals.decide_g3(
            candidate_id=ready.id,
            candidate_digest=ready.result["candidate_digest"],
            idempotency_key=idempotency_key,
            actor_id=actor_id,
            decision="approve",
            comment="invalid input must not create a gate row",
        )

    assert g3_container.gate_decisions.find_by_key(project.id, idempotency_key) is None


def test_g3_artifact_failure_leaves_no_gate_decision(
    g3_container, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, candidate, _source, _fixture_root = _create_candidate(
        g3_container, tmp_path
    )

    assert g3_container.worker.run_once() is True
    ready = g3_container.pcb_candidates.get(candidate.id)
    assert ready.result is not None

    def fail_put(_data: bytes, _media_type: str):
        raise OSError("approval artifact write failed")

    monkeypatch.setattr(g3_container.artifacts, "put_bytes", fail_put)
    with pytest.raises(OSError, match="approval artifact write failed"):
        g3_container.pcb_approvals.decide_g3(
            candidate_id=ready.id,
            candidate_digest=ready.result["candidate_digest"],
            idempotency_key="g3-artifact-failure",
            actor_id="fixture-reviewer",
            decision="approve",
            comment="artifact failure must roll back the gate",
        )

    assert (
        g3_container.gate_decisions.find_by_key(project.id, "g3-artifact-failure")
        is None
    )
    assert g3_container.pcb_candidates.get(candidate.id).status is PcbCandidateStatus.READY_FOR_G3


def test_g3_rechecks_evidence_after_acquiring_decision_lock(
    g3_container, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, candidate, _source, _fixture_root = _create_candidate(
        g3_container, tmp_path
    )
    assert g3_container.worker.run_once() is True
    ready = g3_container.pcb_candidates.get(candidate.id)
    assert ready.result is not None
    original_eligible = g3_container.pcb_approvals._eligible
    checks = 0

    def delete_evidence_after_prefetch(candidate_value):
        nonlocal checks
        result = original_eligible(candidate_value)
        if checks == 0:
            with g3_container.sessions.begin() as session:
                row = session.scalar(
                    select(EvidenceRow).where(
                        EvidenceRow.task_id == candidate.task_id,
                        EvidenceRow.kind == "native_drc",
                    )
                )
                assert row is not None
                session.delete(row)
        checks += 1
        return result

    monkeypatch.setattr(
        g3_container.pcb_approvals, "_eligible", delete_evidence_after_prefetch
    )
    with pytest.raises(PcbCandidateNotReviewableError):
        g3_container.pcb_approvals.decide_g3(
            candidate_id=candidate.id,
            candidate_digest=ready.result["candidate_digest"],
            idempotency_key="g3-evidence-lock-recheck",
            actor_id="fixture-reviewer",
            decision="approve",
            comment="evidence must be checked under the decision lock",
        )

    assert checks == 2
    assert g3_container.gate_decisions.find_by_key(
        project.id, "g3-evidence-lock-recheck"
    ) is None


def test_g3_rejects_a_candidate_when_native_drc_has_a_blocker(
    g3_drc_blocked_container, tmp_path: Path
) -> None:
    _project, candidate, _source, _fixture_root = _create_candidate(
        g3_drc_blocked_container, tmp_path
    )

    assert g3_drc_blocked_container.worker.run_once() is True

    failed = g3_drc_blocked_container.pcb_candidates.get(candidate.id)
    assert failed.status is PcbCandidateStatus.VALIDATION_FAILED
    assert failed.last_error_code == "PCB_NATIVE_DRC_BLOCKED"
    assert g3_drc_blocked_container.tasks.get(candidate.task_id).status is TaskStatus.FAILED_TERMINAL
    assert failed.result is not None
    with pytest.raises(PcbCandidateNotReviewableError):
        g3_drc_blocked_container.pcb_approvals.decide_g3(
            candidate_id=candidate.id,
            candidate_digest=failed.result["candidate_digest"],
            idempotency_key="g3-blocked-approval",
            actor_id="fixture-reviewer",
            decision="approve",
            comment="must not approve",
        )


def test_empty_native_drc_report_fails_candidate_execution(
    g3_empty_native_drc_container, tmp_path: Path
) -> None:
    _project, candidate, _source, _fixture_root = _create_candidate(
        g3_empty_native_drc_container, tmp_path
    )

    assert g3_empty_native_drc_container.worker.run_once() is True

    failed = g3_empty_native_drc_container.pcb_candidates.get(candidate.id)
    assert failed.status is PcbCandidateStatus.VALIDATION_FAILED
    assert failed.last_error_code == "PCB_NATIVE_DRC_REPORT_MISSING"
    assert (
        g3_empty_native_drc_container.tasks.get(candidate.task_id).status
        is TaskStatus.FAILED_TERMINAL
    )


def test_malformed_native_drc_report_does_not_leave_candidate_executing(
    g3_malformed_native_drc_container, tmp_path: Path
) -> None:
    _project, candidate, _source, _fixture_root = _create_candidate(
        g3_malformed_native_drc_container, tmp_path
    )

    assert g3_malformed_native_drc_container.worker.run_once() is True

    failed = g3_malformed_native_drc_container.pcb_candidates.get(candidate.id)
    assert failed.status is PcbCandidateStatus.VALIDATION_FAILED
    assert failed.last_error_code == "PCB_NATIVE_DRC_REPORT_INVALID"
    assert (
        g3_malformed_native_drc_container.tasks.get(candidate.task_id).status
        is TaskStatus.FAILED_TERMINAL
    )


def test_ready_transition_failure_does_not_leave_candidate_executing(
    g3_container, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _project, candidate, _source, _fixture_root = _create_candidate(g3_container, tmp_path)

    def fail_ready(*_args, **_kwargs):
        raise RuntimeError("ready write failed")

    monkeypatch.setattr(g3_container.pcb_candidates, "mark_ready_for_g3", fail_ready)
    assert g3_container.worker.run_once() is True

    failed = g3_container.pcb_candidates.get(candidate.id)
    assert failed.status is PcbCandidateStatus.VALIDATION_FAILED
    assert failed.last_error_code == "PCB_CANDIDATE_FINALIZATION_FAILED"
    assert g3_container.tasks.get(candidate.task_id).status is TaskStatus.FAILED_TERMINAL


def test_expired_execution_can_be_reclaimed_by_a_new_lease(
    g3_container, tmp_path: Path
) -> None:
    _project, candidate, _source, _fixture_root = _create_candidate(g3_container, tmp_path)
    now = utc_now()
    first_lease = g3_container.tasks.claim_next("expired-worker", now, 30)
    assert first_lease is not None and first_lease.task_id == candidate.task_id
    g3_container.tasks.start(first_lease.task_id, first_lease.lease_token, now)
    g3_container.pcb_candidates.mark_executing(
        candidate.id, first_lease.task_id, first_lease.lease_token, now
    )

    with g3_container.sessions.begin() as session:
        task_row = session.get(TaskRow, candidate.task_id)
        assert task_row is not None
        task_row.lease_expires_at = now - timedelta(seconds=1)

    assert g3_container.worker.run_once() is True

    recovered = g3_container.pcb_candidates.get(candidate.id)
    assert recovered.status is PcbCandidateStatus.READY_FOR_G3
    assert g3_container.tasks.get(candidate.task_id).status is TaskStatus.SUCCEEDED


def test_capability_blocked_candidate_never_invokes_apply_operations(
    g3_unverified_container, tmp_path: Path
) -> None:
    services, adapter = g3_unverified_container
    _project, candidate, _source, _fixture_root = _create_candidate(
        services, tmp_path, verify_capability=False
    )

    assert services.worker.run_once() is True

    blocked = services.pcb_candidates.get(candidate.id)
    assert blocked.status is PcbCandidateStatus.BLOCKED
    assert blocked.last_error_code == "PCB_CAPABILITY_GATE_BLOCKED"
    assert adapter.apply_calls == 0


def test_tampered_operations_payload_never_reaches_the_adapter(
    g3_unverified_container, tmp_path: Path
) -> None:
    services, adapter = g3_unverified_container
    _project, candidate, _source, _fixture_root = _create_candidate(services, tmp_path)

    with services.sessions.begin() as session:
        row = session.get(PcbCandidateRow, candidate.id)
        assert row is not None
        operations = [dict(item) for item in row.operations_json]
        operations[0]["idempotency_key"] = "g3-place-mcu-tampered"
        row.operations_json = operations

    assert services.worker.run_once() is True

    failed = services.pcb_candidates.get(candidate.id)
    assert failed.status is PcbCandidateStatus.VALIDATION_FAILED
    assert failed.last_error_code == "PCB_CANDIDATE_OPERATION_DIGEST_MISMATCH"
    assert services.tasks.get(candidate.task_id).status is TaskStatus.FAILED_TERMINAL
    assert adapter.load_calls == 0
    assert adapter.apply_calls == 0


def test_missing_snapshot_capability_prevents_any_candidate_read(
    g3_unverified_container, tmp_path: Path
) -> None:
    services, adapter = g3_unverified_container
    _project, candidate, _source, _fixture_root = _create_candidate(
        services,
        tmp_path,
        capability_operations=frozenset(
            {
                EdaOperation.CREATE_CANDIDATE,
                EdaOperation.APPLY_OPERATIONS,
                EdaOperation.RUN_DRC,
            }
        ),
    )

    assert services.worker.run_once() is True

    blocked = services.pcb_candidates.get(candidate.id)
    assert blocked.status is PcbCandidateStatus.BLOCKED
    assert blocked.last_error_code == "PCB_CAPABILITY_GATE_BLOCKED"
    assert adapter.load_calls == 0
    assert adapter.apply_calls == 0


def test_missing_native_drc_capability_prevents_any_candidate_execution(
    g3_unverified_container, tmp_path: Path
) -> None:
    services, adapter = g3_unverified_container
    _project, candidate, _source, _fixture_root = _create_candidate(
        services,
        tmp_path,
        capability_operations=frozenset(
            {
                EdaOperation.SNAPSHOT,
                EdaOperation.CREATE_CANDIDATE,
                EdaOperation.APPLY_OPERATIONS,
            }
        ),
    )

    assert services.worker.run_once() is True

    blocked = services.pcb_candidates.get(candidate.id)
    assert blocked.status is PcbCandidateStatus.BLOCKED
    assert blocked.last_error_code == "PCB_CAPABILITY_GATE_BLOCKED"
    assert adapter.load_calls == 0
    assert adapter.apply_calls == 0


def test_g3_rejects_a_stale_candidate_digest(g3_container, tmp_path: Path) -> None:
    project, candidate, _source, _fixture_root = _create_candidate(g3_container, tmp_path)

    assert g3_container.worker.run_once() is True
    ready = g3_container.pcb_candidates.get(candidate.id)
    assert ready.result is not None
    expected_digest = ready.result["candidate_digest"]
    stale_digest = "sha256:" + (
        "0" if expected_digest[7] != "0" else "1"
    ) + expected_digest[8:]

    with pytest.raises(ApprovalDigestMismatchError) as raised:
        g3_container.pcb_approvals.decide_g3(
            candidate_id=candidate.id,
            candidate_digest=stale_digest,
            idempotency_key="g3-stale-digest",
            actor_id="fixture-reviewer",
            decision="approve",
            comment="stale digest must not approve",
        )

    assert raised.value.expected == expected_digest
    assert raised.value.actual == stale_digest
    assert g3_container.gate_decisions.find_by_key(project.id, "g3-stale-digest") is None
    assert g3_container.pcb_candidates.get(candidate.id).status is PcbCandidateStatus.READY_FOR_G3


def test_g3_rejects_an_evidence_set_with_a_missing_required_item(
    g3_container, tmp_path: Path
) -> None:
    project, candidate, _source, _fixture_root = _create_candidate(g3_container, tmp_path)

    assert g3_container.worker.run_once() is True
    ready = g3_container.pcb_candidates.get(candidate.id)
    assert ready.result is not None
    with g3_container.sessions.begin() as session:
        row = session.get(PcbCandidateRow, candidate.id)
        assert row is not None and isinstance(row.result_json, dict)
        result = dict(row.result_json)
        artifacts = dict(result["evidence_artifacts"])
        artifacts.pop("native_drc")
        result["evidence_artifacts"] = artifacts
        row.result_json = result

    with pytest.raises(PcbCandidateNotReviewableError):
        g3_container.pcb_approvals.decide_g3(
            candidate_id=candidate.id,
            candidate_digest=ready.result["candidate_digest"],
            idempotency_key="g3-evidence-omission",
            actor_id="fixture-reviewer",
            decision="approve",
            comment="missing native DRC evidence must not approve",
        )

    assert g3_container.gate_decisions.find_by_key(project.id, "g3-evidence-omission") is None
    assert g3_container.pcb_candidates.get(candidate.id).status is PcbCandidateStatus.READY_FOR_G3


def test_g3_rejects_a_tampered_published_candidate_digest(
    g3_container, tmp_path: Path
) -> None:
    _project, candidate, _source, _fixture_root = _create_candidate(g3_container, tmp_path)

    assert g3_container.worker.run_once() is True
    ready = g3_container.pcb_candidates.get(candidate.id)
    assert ready.result is not None
    tampered_digest = "sha256:" + "a" * 64
    assert tampered_digest != ready.result["candidate_digest"]
    with g3_container.sessions.begin() as session:
        row = session.get(PcbCandidateRow, candidate.id)
        assert row is not None and isinstance(row.result_json, dict)
        result = dict(row.result_json)
        result["candidate_digest"] = tampered_digest
        row.result_json = result

    with pytest.raises(PcbCandidateNotReviewableError):
        g3_container.pcb_approvals.decide_g3(
            candidate_id=candidate.id,
            candidate_digest=tampered_digest,
            idempotency_key="g3-tampered-result-digest",
            actor_id="fixture-reviewer",
            decision="approve",
            comment="tampered candidate digest must not approve",
        )


def test_g3_rejects_a_candidate_without_native_verification_marker(
    g3_container, tmp_path: Path
) -> None:
    project, candidate, _source, _fixture_root = _create_candidate(g3_container, tmp_path)

    assert g3_container.worker.run_once() is True
    ready = g3_container.pcb_candidates.get(candidate.id)
    assert ready.result is not None
    with g3_container.sessions.begin() as session:
        row = session.get(PcbCandidateRow, candidate.id)
        assert row is not None and isinstance(row.result_json, dict)
        result = dict(row.result_json)
        result["native_candidate_verified"] = False
        row.result_json = result

    with pytest.raises(PcbCandidateNotReviewableError):
        g3_container.pcb_approvals.decide_g3(
            candidate_id=candidate.id,
            candidate_digest=ready.result["candidate_digest"],
            idempotency_key="g3-native-marker-missing",
            actor_id="fixture-reviewer",
            decision="approve",
            comment="native verification marker is required",
        )

    assert g3_container.gate_decisions.find_by_key(project.id, "g3-native-marker-missing") is None


def test_g3_rejects_evidence_without_a_registered_evidence_row(
    g3_container, tmp_path: Path
) -> None:
    project, candidate, _source, _fixture_root = _create_candidate(g3_container, tmp_path)

    assert g3_container.worker.run_once() is True
    ready = g3_container.pcb_candidates.get(candidate.id)
    assert ready.result is not None
    evidence = next(
        item
        for item in g3_container.evidence.list_for_project(project.id)
        if item.task_id == candidate.task_id and item.kind == "native_drc"
    )
    with g3_container.sessions.begin() as session:
        row = session.get(EvidenceRow, evidence.id)
        assert row is not None
        session.delete(row)

    with pytest.raises(PcbCandidateNotReviewableError):
        g3_container.pcb_approvals.decide_g3(
            candidate_id=candidate.id,
            candidate_digest=ready.result["candidate_digest"],
            idempotency_key="g3-unregistered-evidence",
            actor_id="fixture-reviewer",
            decision="approve",
            comment="unregistered evidence must not approve",
        )


def test_g3_rejects_an_evidence_set_with_a_rebound_input_snapshot(
    g3_container, tmp_path: Path
) -> None:
    project, candidate, _source, _fixture_root = _create_candidate(g3_container, tmp_path)

    assert g3_container.worker.run_once() is True
    rebound = _rebind_published_evidence(
        g3_container,
        candidate.id,
        kind="pcb_input_snapshot",
        artifact_bytes=b'{"schema_version":"1.0","replacement":true}',
        media_type="application/vnd.pcbflow.boardir+json",
    )

    with pytest.raises(PcbCandidateNotReviewableError):
        g3_container.pcb_approvals.decide_g3(
            candidate_id=candidate.id,
            candidate_digest=rebound.result["candidate_digest"],
            idempotency_key="g3-rebound-input-snapshot",
            actor_id="fixture-reviewer",
            decision="approve",
            comment="input snapshot must stay bound to the frozen candidate",
        )

    assert (
        g3_container.gate_decisions.find_by_key(project.id, "g3-rebound-input-snapshot")
        is None
    )


def test_g3_rejects_native_drc_evidence_with_no_reports(
    g3_container, tmp_path: Path
) -> None:
    project, candidate, _source, _fixture_root = _create_candidate(g3_container, tmp_path)

    assert g3_container.worker.run_once() is True
    rebound = _rebind_published_evidence(
        g3_container,
        candidate.id,
        kind="native_drc",
        artifact_bytes=canonical_json_bytes({"schema_version": "1.0", "reports": []}),
        media_type="application/vnd.pcbflow.pcb-native-drc+json",
    )

    with pytest.raises(PcbCandidateNotReviewableError):
        g3_container.pcb_approvals.decide_g3(
            candidate_id=candidate.id,
            candidate_digest=rebound.result["candidate_digest"],
            idempotency_key="g3-empty-native-evidence",
            actor_id="fixture-reviewer",
            decision="approve",
            comment="native DRC evidence must contain a report",
        )

    assert (
        g3_container.gate_decisions.find_by_key(project.id, "g3-empty-native-evidence")
        is None
    )


def test_g3_rejects_native_drc_evidence_with_a_blocker(
    g3_container, tmp_path: Path
) -> None:
    project, candidate, _source, _fixture_root = _create_candidate(g3_container, tmp_path)

    assert g3_container.worker.run_once() is True
    rebound = _rebind_published_evidence(
        g3_container,
        candidate.id,
        kind="native_drc",
        artifact_bytes=canonical_json_bytes(
            {
                "schema_version": "1.0",
                "reports": [
                    {
                        "kind": "drc",
                        "findings": [
                            {
                                "rule_id": "LCEDA.DRC.CLEARANCE",
                                "severity": "error",
                                "subject": "seg_1",
                                "message": "clearance",
                            }
                        ],
                    }
                ],
            }
        ),
        media_type="application/vnd.pcbflow.pcb-native-drc+json",
    )

    with pytest.raises(PcbCandidateNotReviewableError):
        g3_container.pcb_approvals.decide_g3(
            candidate_id=candidate.id,
            candidate_digest=rebound.result["candidate_digest"],
            idempotency_key="g3-native-blocking-evidence",
            actor_id="fixture-reviewer",
            decision="approve",
            comment="native DRC evidence must be clean",
        )

    assert (
        g3_container.gate_decisions.find_by_key(project.id, "g3-native-blocking-evidence")
        is None
    )


def test_g3_rejects_boardir_validation_evidence_with_a_blocker(
    g3_container, tmp_path: Path
) -> None:
    project, candidate, _source, _fixture_root = _create_candidate(g3_container, tmp_path)

    assert g3_container.worker.run_once() is True
    ready = g3_container.pcb_candidates.get(candidate.id)
    assert ready.result is not None
    with g3_container.artifacts.open(
        ready.result["evidence_artifacts"]["boardir_validation"]
    ) as stream:
        validation = json.loads(stream.read().decode("utf-8"))
    validation["findings"] = [
        {
            "rule_id": "PCB.CLEARANCE",
            "severity": "error",
            "subject": "U_MCU",
            "message": "clearance",
        }
    ]
    rebound = _rebind_published_evidence(
        g3_container,
        candidate.id,
        kind="boardir_validation",
        artifact_bytes=canonical_json_bytes(validation),
        media_type="application/vnd.pcbflow.pcb-boardir-validation+json",
    )

    with pytest.raises(PcbCandidateNotReviewableError):
        g3_container.pcb_approvals.decide_g3(
            candidate_id=candidate.id,
            candidate_digest=rebound.result["candidate_digest"],
            idempotency_key="g3-boardir-blocking-evidence",
            actor_id="fixture-reviewer",
            decision="approve",
            comment="BoardIR evidence must be clean",
        )

    assert (
        g3_container.gate_decisions.find_by_key(project.id, "g3-boardir-blocking-evidence")
        is None
    )


def test_g3_rejects_a_candidate_summary_not_bound_to_frozen_inputs(
    g3_container, tmp_path: Path
) -> None:
    project, candidate, _source, _fixture_root = _create_candidate(g3_container, tmp_path)

    assert g3_container.worker.run_once() is True
    rebound = _rebind_published_evidence(
        g3_container,
        candidate.id,
        kind="candidate_summary",
        artifact_bytes=canonical_json_bytes(
            {
                "schema_version": "1.0",
                "candidate_id": "wrong-candidate",
            }
        ),
        media_type="application/vnd.pcbflow.pcb-candidate-summary+json",
    )

    with pytest.raises(PcbCandidateNotReviewableError):
        g3_container.pcb_approvals.decide_g3(
            candidate_id=candidate.id,
            candidate_digest=rebound.result["candidate_digest"],
            idempotency_key="g3-summary-binding",
            actor_id="fixture-reviewer",
            decision="approve",
            comment="candidate summary must match frozen inputs",
        )

    assert g3_container.gate_decisions.find_by_key(project.id, "g3-summary-binding") is None


def test_cancellation_after_native_drc_prevents_evidence_publication(
    g3_cancellation_container, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    services, adapter = g3_cancellation_container
    project, candidate, _source, _fixture_root = _create_candidate(services, tmp_path)
    artifact_writes = 0
    original_put = services.artifacts.put_bytes

    def record_put(data: bytes, media_type: str):
        nonlocal artifact_writes
        artifact_writes += 1
        return original_put(data, media_type)

    monkeypatch.setattr(services.artifacts, "put_bytes", record_put)
    adapter.cancel_after_drc(
        lambda: services.tasks.cancel(
            candidate.task_id, "cancel after native DRC", utc_now()
        )
    )

    assert services.worker.run_once() is True

    cancelled = services.pcb_candidates.get(candidate.id)
    assert adapter.drc_calls == 1
    assert cancelled.status is PcbCandidateStatus.CANCELLED
    assert cancelled.result is None
    assert services.tasks.get(candidate.task_id).status is TaskStatus.CANCELLED
    assert artifact_writes == 0
    assert all(
        evidence.task_id != candidate.task_id
        for evidence in services.evidence.list_for_project(project.id)
    )


def test_cancellation_during_evidence_staging_publishes_no_candidate_objects(
    g3_container, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, candidate, _source, _fixture_root = _create_candidate(g3_container, tmp_path)

    def object_paths() -> set[Path]:
        objects = g3_container.artifacts.root / "objects"
        return {path for path in objects.rglob("*") if path.is_file()}

    before_paths = object_paths()
    original_stage = g3_container.artifacts.stage_stream
    staged_calls = 0

    def cancel_after_first_stage(stream, media_type, *args, **kwargs):
        nonlocal staged_calls
        staged = original_stage(stream, media_type, *args, **kwargs)
        staged_calls += 1
        if staged_calls == 1:
            g3_container.tasks.cancel(candidate.task_id, "cancel during evidence staging", utc_now())
        return staged

    monkeypatch.setattr(g3_container.artifacts, "stage_stream", cancel_after_first_stage)
    assert g3_container.worker.run_once() is True

    cancelled = g3_container.pcb_candidates.get(candidate.id)
    assert cancelled.status is PcbCandidateStatus.CANCELLED
    assert g3_container.tasks.get(candidate.task_id).status is TaskStatus.CANCELLED
    assert staged_calls == 1
    assert object_paths() == before_paths
    assert all(
        evidence.task_id != candidate.task_id
        for evidence in g3_container.evidence.list_for_project(project.id)
    )


def test_cancellation_during_evidence_publication_removes_new_objects(
    g3_container, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, candidate, _source, _fixture_root = _create_candidate(g3_container, tmp_path)

    def object_paths() -> set[Path]:
        objects = g3_container.artifacts.root / "objects"
        return {path for path in objects.rglob("*") if path.is_file()}

    before_paths = object_paths()
    original_publish = g3_container.artifacts._publish_staged
    published_calls = 0

    def cancel_after_first_publish(staged):
        nonlocal published_calls
        descriptor = original_publish(staged)
        published_calls += 1
        if published_calls == 1:
            g3_container.tasks.cancel(
                candidate.task_id, "cancel during evidence publication", utc_now()
            )
        return descriptor

    monkeypatch.setattr(
        g3_container.artifacts, "_publish_staged", cancel_after_first_publish
    )
    assert g3_container.worker.run_once() is True

    cancelled = g3_container.pcb_candidates.get(candidate.id)
    assert cancelled.status is PcbCandidateStatus.CANCELLED
    assert published_calls == 1
    assert object_paths() == before_paths
    assert all(
        evidence.task_id != candidate.task_id
        for evidence in g3_container.evidence.list_for_project(project.id)
    )


def test_evidence_publication_cleanup_preserves_shared_registered_object(
    g3_container, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _project, candidate, _source, _fixture_root = _create_candidate(g3_container, tmp_path)

    def object_paths() -> set[Path]:
        objects = g3_container.artifacts.root / "objects"
        return {path.resolve() for path in objects.rglob("*") if path.is_file()}

    before_paths = object_paths()
    original_publish = g3_container.artifacts._publish_staged
    published_descriptor = None

    def register_shared_object_then_cancel(staged):
        nonlocal published_descriptor
        descriptor = original_publish(staged)
        if published_descriptor is None:
            published_descriptor = descriptor
            with g3_container.sessions.begin() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                session.add(
                    ArtifactRow(
                        digest=descriptor.digest,
                        size=descriptor.size,
                        media_type=descriptor.media_type,
                        storage_path=str(descriptor.path),
                        created_at=utc_now(),
                    )
                )
            g3_container.tasks.cancel(
                candidate.task_id,
                "cancel after another candidate registered shared evidence",
                utc_now(),
            )
        return descriptor

    monkeypatch.setattr(
        g3_container.artifacts, "_publish_staged", register_shared_object_then_cancel
    )
    assert g3_container.worker.run_once() is True

    assert published_descriptor is not None
    cancelled = g3_container.pcb_candidates.get(candidate.id)
    assert cancelled.status is PcbCandidateStatus.CANCELLED
    with g3_container.sessions() as session:
        registered = session.get(ArtifactRow, published_descriptor.digest)
    assert registered is not None
    assert Path(registered.storage_path).is_file()
    assert object_paths() == before_paths | {published_descriptor.path.resolve()}


def test_evidence_publication_failure_does_not_leave_candidate_executing(
    g3_container, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _project, candidate, _source, _fixture_root = _create_candidate(g3_container, tmp_path)

    def fail_batch(**_kwargs):
        raise EvidenceConflictError("simulated evidence conflict")

    monkeypatch.setattr(g3_container.evidence, "add_reports_and_findings", fail_batch)
    assert g3_container.worker.run_once() is True

    failed = g3_container.pcb_candidates.get(candidate.id)
    assert failed.status is PcbCandidateStatus.VALIDATION_FAILED
    assert failed.last_error_code == "PCB_CANDIDATE_EXECUTION_FAILED"
    assert g3_container.tasks.get(candidate.task_id).status is TaskStatus.FAILED_TERMINAL

from __future__ import annotations

import io
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Event, Lock

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from pcbflow.approvals import ApprovalDigestMismatchError
from pcbflow.board import (
    BoardObjectId,
    BoardSnapshot,
    FixtureBoardAdapter,
    PlaceFootprints,
    PointUm,
    ReleaseArtifacts,
)
from pcbflow.board.operations import FootprintPlacement
from pcbflow.board.rulepack import ManufacturingRulePack
from pcbflow.config import Settings
from pcbflow.cancellation import TaskCancelledError
from pcbflow.container import build_container
from pcbflow.domain import EdaKind, EdaOperation, RequestInvalidError, TaskStatus, utc_now
from pcbflow.eda import EdaCapability, ProjectEdaAuthorityInput
from pcbflow.lceda_pro import LcedaProCapabilityError
from pcbflow.pcb_candidates import PcbCandidateNotReviewableError, PcbCandidateStatus
from pcbflow.pcb_release import (
    PCB_EXPORT_RELEASE_TASK_KIND,
    PcbReleaseApprovalService,
    _ReleasePublication,
)
from pcbflow.repositories import StaleLeaseError
from pcbflow.tables import ArtifactRow
from pcbflow.pcb_workflow import (
    CAPABILITY_EVIDENCE_KIND,
    CAPABILITY_MEDIA_TYPE,
    LCEDA_CAPABILITY_TASK_KIND,
    capability_artifact_bytes,
)


_BASE_REVISION = "git:" + "b" * 40


class ReleaseFixtureAdapter(FixtureBoardAdapter):
    def export_release(self, candidate, rulepack):
        findings = self.run_drc(candidate, rulepack)[0]
        if findings.findings:
            raise ValueError("cannot export fixture with DRC findings")
        files = {
            "gerber": b"G04 fixture gerber*\nM02*\n",
            "drill": b"M48\n; fixture drill\nM30\n",
            "bom": (
                b"Designator,Comment,DNP,HandSolder\n"
                b"R1,10k,no,no\n"
                b"C1,100nF,yes,no\n"
            ),
            "cpl": b"Designator,Mid X,Mid Y\nR1,10,10\n",
            "assembly": b"%PDF-1.4\nfixture assembly drawing\n",
        }
        output: list[tuple[str, Path]] = []
        for kind, content in files.items():
            path = candidate.output_dir / f"{kind}.out"
            path.write_bytes(content)
            output.append((kind, path))
        return ReleaseArtifacts(files=tuple(output))


class IncompleteReleaseFixtureAdapter(ReleaseFixtureAdapter):
    def export_release(self, candidate, rulepack):
        complete = super().export_release(candidate, rulepack)
        return ReleaseArtifacts(
            files=tuple(item for item in complete.files if item[0] != "drill")
        )


class SourceMutatingReleaseFixtureAdapter(ReleaseFixtureAdapter):
    def __init__(self) -> None:
        super().__init__()
        self._source_dir: Path | None = None

    def create_candidate(self, source_dir, destination_dir):
        self._source_dir = source_dir
        return super().create_candidate(source_dir, destination_dir)

    def export_release(self, candidate, rulepack):
        release = super().export_release(candidate, rulepack)
        assert self._source_dir is not None
        (self._source_dir / "adapter-mutated-source.txt").write_text(
            "unexpected", encoding="utf-8"
        )
        return release


class MismatchedReadbackReleaseFixtureAdapter(ReleaseFixtureAdapter):
    def apply_operations(self, candidate, operations, expected_snapshot):
        applied = super().apply_operations(candidate, operations, expected_snapshot)
        if "pcb-release-" in str(candidate.path):
            return replace(applied, profile_id=applied.profile_id + "-reported")
        return applied


class ToggleSourceSnapshotReleaseFixtureAdapter(ReleaseFixtureAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.drift_source_snapshot = False
        self.source_dir: Path | None = None

    def create_candidate(self, source_dir, destination_dir):
        self.source_dir = source_dir
        return super().create_candidate(source_dir, destination_dir)

    def load_snapshot(self, project_dir):
        snapshot = super().load_snapshot(project_dir)
        if (
            self.drift_source_snapshot
            and self.source_dir is not None
            and project_dir.resolve() == self.source_dir.resolve()
        ):
            return replace(snapshot, profile_id=snapshot.profile_id + "-drift")
        return snapshot


@pytest.fixture
def release_container(database_url: str, tmp_path: Path):
    settings = Settings.from_env(
        {
            "PCBFLOW_DATA_DIR": str(tmp_path / "service-data"),
            "PCBFLOW_DATABASE_URL": database_url,
        }
    )
    services = build_container(settings, pcb_adapter_override=ReleaseFixtureAdapter())
    try:
        yield services
    finally:
        services.dispose()


@pytest.fixture
def incomplete_release_container(database_url: str, tmp_path: Path):
    settings = Settings.from_env(
        {
            "PCBFLOW_DATA_DIR": str(tmp_path / "service-data"),
            "PCBFLOW_DATABASE_URL": database_url,
        }
    )
    services = build_container(
        settings, pcb_adapter_override=IncompleteReleaseFixtureAdapter()
    )
    try:
        yield services
    finally:
        services.dispose()


def _verified_capability(services, project_id: str, operations: frozenset[EdaOperation]) -> str:
    capability = EdaCapability(
        available=True,
        executable=Path("fixture-lceda.exe"),
        version="fixture",
        executable_digest="sha256:" + "1" * 64,
        profile_id="lceda-pro-v1",
        profile_revision=1,
        operations=operations,
        write_verified=True,
        reason=None,
    )
    descriptor = services.artifacts.put_bytes(
        capability_artifact_bytes(capability), CAPABILITY_MEDIA_TYPE
    )
    task = services.tasks.enqueue(
        LCEDA_CAPABILITY_TASK_KIND,
        {"project_id": project_id},
        f"release-capability-{len(operations)}",
        project_id,
    )
    lease = services.tasks.claim_next("release-capability-worker", utc_now(), 30)
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


def _approved_candidate(
    services,
    tmp_path: Path,
    *,
    capability_operations: frozenset[EdaOperation] = frozenset(EdaOperation),
    approve: bool = True,
    managed: bool = False,
):
    fixture_root = Path(__file__).parents[1] / "fixtures" / "lceda-pro" / "minimal"
    source = tmp_path / "release-source"
    source.mkdir()
    (source / "expected-boardir.json").write_bytes(
        (fixture_root / "expected-boardir.json").read_bytes()
    )
    snapshot = BoardSnapshot.load_json((source / "expected-boardir.json").read_bytes())
    rulepack = ManufacturingRulePack.load_json(
        (
            Path(__file__).parents[1]
            / "fixtures"
            / "boardir"
            / "stm32-environment-controller-2l-rulepack.json"
        ).read_bytes()
    )
    project = services.projects.create("Release fixture", source, "release-project")
    if managed:
        project = services.revisions.adopt(project.id, "release-managed-adopt")
    services.eda_authorities.configure(
        project.id,
        ProjectEdaAuthorityInput(
            eda_kind=EdaKind.LCEDA_PRO,
            eda_profile_id="lceda-pro-v1",
            board_profile_id=rulepack.profile_id,
            rulepack_digest=rulepack.canonical_digest(),
        ),
        "release-authority",
    )
    capability_digest = _verified_capability(
        services, project.id, capability_operations
    )
    operation = PlaceFootprints(
        project_id=project.id,
        baseline_revision=project.current_revision or _BASE_REVISION,
        risk="low",
        rulepack_digest=rulepack.canonical_digest(),
        target_object_ids=(BoardObjectId("U_MCU"),),
        idempotency_key="release-place-mcu",
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
        algorithm_evidence={
            "rulepack": rulepack.to_canonical_dict(),
            "placement": {"objective_version": "fixture-placement-v1"},
            "routing": {"final_unconnected_nets": []},
            "copper": {"objective_version": "fixture-copper-v1"},
        },
        idempotency_key="release-candidate",
    )
    assert services.worker.run_once() is True
    ready = services.pcb_candidates.get(candidate.id)
    assert ready.status is PcbCandidateStatus.READY_FOR_G3
    if not approve:
        return ready
    approved = services.pcb_approvals.decide_g3(
        candidate_id=ready.id,
        candidate_digest=ready.result["candidate_digest"],
        idempotency_key="release-g3",
        actor_id="release-reviewer",
        decision="approve",
        comment="G3 approved",
    )
    assert approved.status is PcbCandidateStatus.G3_APPROVED
    return approved


def _container_with_adapter(tmp_path: Path, adapter):
    settings = Settings.from_env(
        {
            "PCBFLOW_DATA_DIR": str(tmp_path / "service-data"),
            "PCBFLOW_DATABASE_URL": (
                f"sqlite+pysqlite:///{(tmp_path / 'pcbflow.db').as_posix()}"
            ),
        }
    )
    return build_container(settings, pcb_adapter_override=adapter)


def _artifact_object_paths(services) -> set[Path]:
    objects = services.artifacts.root / "objects"
    return {path.resolve() for path in objects.rglob("*") if path.is_file()}


def _registered_artifacts(services, digests: set[str]) -> dict[str, ArtifactRow]:
    with services.sessions() as session:
        rows = session.scalars(
            select(ArtifactRow).where(ArtifactRow.digest.in_(digests))
        ).all()
        return {row.digest: row for row in rows}


def test_release_publication_settlement_preserves_partially_registered_shared_object(
    release_container,
) -> None:
    shared_a = release_container.artifacts.stage_stream(
        io.BytesIO(b"shared release object"), "application/x-shared-release"
    )
    unique_a = release_container.artifacts.stage_stream(
        io.BytesIO(b"unique release object"), "application/x-unique-release"
    )
    shared_descriptor = shared_a.publish()
    unique_descriptor = unique_a.publish()
    shared_b = release_container.artifacts.stage_stream(
        io.BytesIO(b"shared release object"), "application/x-shared-release"
    )
    reused_descriptor = shared_b.publish()
    assert reused_descriptor.digest == shared_descriptor.digest

    with release_container.sessions.begin() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        release_container.pcb_candidates._register_release_artifacts(
            session, (reused_descriptor,), utc_now()
        )
    shared_b.discard()

    handler = release_container.worker._handlers[PCB_EXPORT_RELEASE_TASK_KIND]
    handler._discard_or_rollback(
        _ReleasePublication(
            descriptors=(shared_descriptor, unique_descriptor),
            staged=(shared_a, unique_a),
        )
    )

    rows = _registered_artifacts(release_container, {shared_descriptor.digest})
    assert set(rows) == {shared_descriptor.digest}
    assert shared_descriptor.path.is_file()
    assert not unique_descriptor.path.exists()


def test_release_artifact_registration_rejects_descriptor_deleted_by_other_publication(
    release_container,
) -> None:
    owner = release_container.artifacts.stage_stream(
        io.BytesIO(b"release registration race"), "application/x-release-race"
    )
    owner_descriptor = owner.publish()
    reuser = release_container.artifacts.stage_stream(
        io.BytesIO(b"release registration race"), "application/x-release-race"
    )
    reused_descriptor = reuser.publish()
    owner.rollback()
    assert not reused_descriptor.path.exists()

    with pytest.raises(RequestInvalidError, match="PCB_RELEASE_ARTIFACT_UNAVAILABLE"):
        with release_container.sessions.begin() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            release_container.pcb_candidates._register_release_artifacts(
                session, (reused_descriptor,), utc_now()
            )

    with release_container.sessions() as session:
        assert session.get(ArtifactRow, reused_descriptor.digest) is None
    reuser.discard()


def test_export_release_requires_g3_approval(release_container, tmp_path: Path) -> None:
    candidate = _approved_candidate(release_container, tmp_path, approve=False)

    with pytest.raises(PcbCandidateNotReviewableError):
        release_container.pcb_release.enqueue_export(candidate.id, "release-before-g3")


def test_export_release_requires_verified_export_capability(release_container, tmp_path: Path) -> None:
    operations = frozenset(EdaOperation) - {EdaOperation.EXPORT_RELEASE}
    candidate = _approved_candidate(
        release_container, tmp_path, capability_operations=operations
    )

    with pytest.raises(LcedaProCapabilityError) as raised:
        release_container.pcb_release.enqueue_export(candidate.id, "release-no-export")

    assert raised.value.code == "PCB_RELEASE_CAPABILITY_BLOCKED"


def test_fixture_release_builds_complete_manifest(release_container, tmp_path: Path) -> None:
    candidate = _approved_candidate(release_container, tmp_path)

    task = release_container.pcb_release.enqueue_export(candidate.id, "release-export")

    assert task.kind == PCB_EXPORT_RELEASE_TASK_KIND
    assert release_container.pcb_candidates.get(candidate.id).status is PcbCandidateStatus.RELEASE_PENDING
    assert release_container.worker.run_once() is True
    released_candidate = release_container.pcb_candidates.get(candidate.id)
    assert released_candidate.status is PcbCandidateStatus.READY_FOR_G4
    assert released_candidate.output_kind == "release_candidate"
    manifest_digest = released_candidate.result["release"]["manifest_digest"]
    with release_container.artifacts.open(manifest_digest) as stream:
        manifest = json.loads(stream.read().decode("utf-8"))
    assert {item["kind"] for item in manifest["artifacts"]} == {
        "gerber",
        "drill",
        "bom",
        "cpl",
        "assembly",
        "native_drc",
        "rulepack",
        "candidate_summary",
    }


def test_release_export_reuses_canonical_board_and_rulepack_bytes(
    release_container, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = _approved_candidate(release_container, tmp_path)
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

    release_container.pcb_release.enqueue_export(candidate.id, "release-reuse-bytes")

    assert release_container.worker.run_once() is True
    assert board_calls == 2
    assert rulepack_calls == 1


def test_g4_requires_matching_manifest_and_is_idempotent(release_container, tmp_path: Path) -> None:
    candidate = _approved_candidate(release_container, tmp_path)
    release_container.pcb_release.enqueue_export(candidate.id, "release-g4-export")
    assert release_container.worker.run_once() is True
    ready = release_container.pcb_candidates.get(candidate.id)
    manifest_digest = ready.result["release"]["manifest_digest"]

    with pytest.raises(ApprovalDigestMismatchError):
        release_container.pcb_release_approvals.decide_g4(
            candidate_id=ready.id,
            manifest_digest="sha256:" + "f" * 64,
            idempotency_key="release-g4-wrong-digest",
            actor_id="release-reviewer",
            decision="approve",
            comment="approve release",
        )
    approved = release_container.pcb_release_approvals.decide_g4(
        candidate_id=ready.id,
        manifest_digest=manifest_digest,
        idempotency_key="release-g4-approve",
        actor_id="release-reviewer",
        decision="approve",
        comment="approve release",
    )
    replay = release_container.pcb_release_approvals.decide_g4(
        candidate_id=ready.id,
        manifest_digest=manifest_digest,
        idempotency_key="release-g4-approve",
        actor_id="release-reviewer",
        decision="approve",
        comment="approve release",
    )

    assert approved == replay
    assert approved.status is PcbCandidateStatus.RELEASED


def test_concurrent_g4_replay_serializes_before_creating_approval_artifact(
    release_container, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = _approved_candidate(release_container, tmp_path)
    release_container.pcb_release.enqueue_export(candidate.id, "release-g4-concurrent-export")
    assert release_container.worker.run_once() is True
    ready = release_container.pcb_candidates.get(candidate.id)
    request = {
        "candidate_id": ready.id,
        "manifest_digest": ready.result["release"]["manifest_digest"],
        "idempotency_key": "release-g4-concurrent-replay",
        "actor_id": "release-reviewer",
        "decision": "approve",
        "comment": "concurrent G4 idempotency replay",
    }
    first_artifact_started = Event()
    release_first_artifact = Event()
    second_transaction_attempted = Event()
    calls_lock = Lock()
    artifact_calls = 0
    original_approval_artifact = PcbReleaseApprovalService._approval_artifact

    def pause_first_approval_artifact(service, *args, **kwargs):
        nonlocal artifact_calls
        with calls_lock:
            artifact_calls += 1
            call_number = artifact_calls
        if call_number == 1:
            first_artifact_started.set()
            assert release_first_artifact.wait(timeout=5)
        return original_approval_artifact(service, *args, **kwargs)

    monkeypatch.setattr(
        PcbReleaseApprovalService,
        "_approval_artifact",
        pause_first_approval_artifact,
    )
    second_container = build_container(
        release_container.settings, pcb_adapter_override=ReleaseFixtureAdapter()
    )
    original_execute = Session.execute

    def observe_begin_immediate(session, statement, *args, **kwargs):
        if getattr(statement, "text", None) == "BEGIN IMMEDIATE":
            second_transaction_attempted.set()
        return original_execute(session, statement, *args, **kwargs)

    monkeypatch.setattr(Session, "execute", observe_begin_immediate)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(
                release_container.pcb_release_approvals.decide_g4, **request
            )
            assert first_artifact_started.wait(timeout=5)
            second = executor.submit(
                second_container.pcb_release_approvals.decide_g4, **request
            )
            assert second_transaction_attempted.wait(timeout=5)
            release_first_artifact.set()
            first_result = first.result(timeout=10)
            second_result = second.result(timeout=10)

        assert first_result == second_result
        assert artifact_calls == 1
        assert first_result.status is PcbCandidateStatus.RELEASED
        with release_container.sessions() as session:
            approval_rows = [
                row
                for row in session.scalars(select(ArtifactRow))
                if row.media_type == "application/vnd.pcbflow.g4-approval+json"
            ]
        assert len(approval_rows) == 1
        approval_digest = first_result.result["g4_decision"][
            "approval_artifact_digest"
        ]
        assert approval_rows[0].digest == approval_digest
        approval_objects = []
        for artifact_path in release_container.artifacts.root.glob(
            "objects/sha256/*/*/*"
        ):
            try:
                artifact = json.loads(artifact_path.read_bytes())
            except json.JSONDecodeError:
                continue
            if artifact.get("gate") == "G4_RELEASE":
                approval_objects.append(artifact_path)
        assert approval_objects == [release_container.artifacts._path(approval_digest)]
    finally:
        release_first_artifact.set()
        second_container.dispose()


def test_g4_rejects_a_tampered_manifest(release_container, tmp_path: Path) -> None:
    candidate = _approved_candidate(release_container, tmp_path)
    release_container.pcb_release.enqueue_export(candidate.id, "release-tamper-export")
    assert release_container.worker.run_once() is True
    ready = release_container.pcb_candidates.get(candidate.id)
    manifest_digest = ready.result["release"]["manifest_digest"]
    release_container.artifacts._path(manifest_digest).write_bytes(b"tampered")

    with pytest.raises(PcbCandidateNotReviewableError):
        release_container.pcb_release_approvals.decide_g4(
            candidate_id=ready.id,
            manifest_digest=manifest_digest,
            idempotency_key="release-g4-tampered",
            actor_id="release-reviewer",
            decision="approve",
            comment="approve release",
        )


def test_failed_export_reopens_g3_candidate_for_a_new_release_key(
    incomplete_release_container, tmp_path: Path
) -> None:
    candidate = _approved_candidate(incomplete_release_container, tmp_path)
    first = incomplete_release_container.pcb_release.enqueue_export(
        candidate.id, "release-incomplete"
    )

    assert incomplete_release_container.worker.run_once() is True
    failed = incomplete_release_container.pcb_candidates.get(candidate.id)
    assert incomplete_release_container.tasks.get(first.id).status is TaskStatus.FAILED_TERMINAL
    assert failed.status is PcbCandidateStatus.G3_APPROVED
    assert failed.result["release"]["status"] == "failed"

    retry = incomplete_release_container.pcb_release.enqueue_export(
        candidate.id, "release-retry"
    )
    assert retry.id != first.id
    assert incomplete_release_container.pcb_candidates.get(candidate.id).status is PcbCandidateStatus.RELEASE_PENDING


def test_release_task_without_candidate_reference_fails_with_stable_code(
    release_container, tmp_path: Path
) -> None:
    candidate = _approved_candidate(release_container, tmp_path)
    task = release_container.tasks.enqueue(
        PCB_EXPORT_RELEASE_TASK_KIND,
        {"project_id": candidate.project_id},
        "release-missing-candidate-reference",
        candidate.project_id,
    )

    assert release_container.worker.run_once() is True

    failed = release_container.tasks.get(task.id)
    assert failed.status is TaskStatus.FAILED_TERMINAL
    assert failed.last_error_code == "PCB_RELEASE_NOT_FOUND"


def test_release_task_with_unknown_candidate_fails_with_stable_code(
    release_container, tmp_path: Path
) -> None:
    candidate = _approved_candidate(release_container, tmp_path)
    task = release_container.tasks.enqueue(
        PCB_EXPORT_RELEASE_TASK_KIND,
        {
            "project_id": candidate.project_id,
            "candidate_id": "pcbcand_missing",
        },
        "release-unknown-candidate",
        candidate.project_id,
    )

    assert release_container.worker.run_once() is True

    failed = release_container.tasks.get(task.id)
    assert failed.status is TaskStatus.FAILED_TERMINAL
    assert failed.last_error_code == "PCB_RELEASE_NOT_FOUND"


def test_release_task_must_own_the_candidate_reservation(
    release_container, tmp_path: Path
) -> None:
    candidate = _approved_candidate(release_container, tmp_path)
    task = release_container.tasks.enqueue(
        PCB_EXPORT_RELEASE_TASK_KIND,
        {"project_id": candidate.project_id, "candidate_id": candidate.id},
        "release-without-reservation",
        candidate.project_id,
    )

    assert release_container.worker.run_once() is True

    failed = release_container.tasks.get(task.id)
    assert failed.status is TaskStatus.FAILED_TERMINAL
    assert failed.last_error_code == "PCB_RELEASE_NOT_REVIEWABLE"


def test_release_rejects_a_source_tree_changed_after_g3(
    release_container, tmp_path: Path
) -> None:
    candidate = _approved_candidate(release_container, tmp_path)
    task = release_container.pcb_release.enqueue_export(
        candidate.id, "release-after-source-change"
    )
    project = release_container.projects.get(candidate.project_id)
    (project.source_path / "post-g3-change.txt").write_text(
        "changed", encoding="utf-8"
    )

    assert release_container.worker.run_once() is True

    failed = release_container.tasks.get(task.id)
    restored = release_container.pcb_candidates.get(candidate.id)
    assert failed.status is TaskStatus.FAILED_TERMINAL
    assert failed.last_error_code == "PCB_CANDIDATE_SOURCE_CHANGED"
    assert restored.status is PcbCandidateStatus.G3_APPROVED


def test_managed_release_uses_frozen_revision_after_source_drift(
    release_container, tmp_path: Path
) -> None:
    candidate = _approved_candidate(release_container, tmp_path, managed=True)
    task = release_container.pcb_release.enqueue_export(
        candidate.id, "managed-release-after-source-drift"
    )
    project = release_container.projects.get(candidate.project_id)
    (project.source_path / "post-g3-change.txt").write_text(
        "changed", encoding="utf-8"
    )

    assert release_container.worker.run_once() is True

    assert release_container.tasks.get(task.id).status is TaskStatus.SUCCEEDED
    assert release_container.pcb_candidates.get(candidate.id).status is PcbCandidateStatus.READY_FOR_G4


def test_g4_rejects_invalid_decision_actor_and_comment(
    release_container, tmp_path: Path
) -> None:
    candidate = _approved_candidate(release_container, tmp_path)
    release_container.pcb_release.enqueue_export(candidate.id, "release-g4-invalid")
    assert release_container.worker.run_once() is True
    ready = release_container.pcb_candidates.get(candidate.id)
    manifest_digest = ready.result["release"]["manifest_digest"]
    common = {
        "candidate_id": ready.id,
        "manifest_digest": manifest_digest,
        "idempotency_key": "release-g4-invalid-input",
        "actor_id": "reviewer",
        "decision": "approve",
        "comment": "reviewed",
    }

    with pytest.raises(RequestInvalidError, match="unsupported G4 decision"):
        release_container.pcb_release_approvals.decide_g4(
            **{**common, "decision": "defer"}
        )
    with pytest.raises(RequestInvalidError, match="actor_id must not be blank"):
        release_container.pcb_release_approvals.decide_g4(
            **{**common, "actor_id": " "}
        )
    with pytest.raises(RequestInvalidError, match="comment must be a string"):
        release_container.pcb_release_approvals.decide_g4(
            **{**common, "comment": 1}  # type: ignore[arg-type]
        )


def test_g4_rejection_is_durable_and_idempotent(
    release_container, tmp_path: Path
) -> None:
    candidate = _approved_candidate(release_container, tmp_path)
    release_container.pcb_release.enqueue_export(candidate.id, "release-g4-reject")
    assert release_container.worker.run_once() is True
    ready = release_container.pcb_candidates.get(candidate.id)
    manifest_digest = ready.result["release"]["manifest_digest"]
    values = {
        "candidate_id": ready.id,
        "manifest_digest": manifest_digest,
        "idempotency_key": "release-g4-rejection",
        "actor_id": "release-reviewer",
        "decision": "reject",
        "comment": "manufacturing review requested changes",
    }

    rejected = release_container.pcb_release_approvals.decide_g4(**values)
    replay = release_container.pcb_release_approvals.decide_g4(**values)

    assert rejected == replay
    assert rejected.status is PcbCandidateStatus.READY_FOR_G4
    assert rejected.last_error_code == "G4_REJECTED"
    assert rejected.result["release"]["status"] == "ready_for_g4"


def test_release_worker_rechecks_capability_after_reservation(
    release_container, tmp_path: Path, monkeypatch
) -> None:
    candidate = _approved_candidate(release_container, tmp_path)
    task = release_container.pcb_release.enqueue_export(
        candidate.id, "release-capability-drift"
    )

    def reject_operations(*_args, **_kwargs):
        raise LcedaProCapabilityError("LCEDA_PRO_WRITE_CAPABILITY_UNVERIFIED")

    monkeypatch.setattr(
        release_container.capability_gate,
        "require_operations",
        reject_operations,
    )

    assert release_container.worker.run_once() is True

    failed = release_container.tasks.get(task.id)
    restored = release_container.pcb_candidates.get(candidate.id)
    assert failed.status is TaskStatus.FAILED_TERMINAL
    assert failed.last_error_code == "PCB_RELEASE_CAPABILITY_BLOCKED"
    assert restored.status is PcbCandidateStatus.G3_APPROVED


def test_release_publication_rolls_back_when_ready_transition_fails(
    release_container, tmp_path: Path, monkeypatch
) -> None:
    candidate = _approved_candidate(release_container, tmp_path)
    task = release_container.pcb_release.enqueue_export(
        candidate.id, "release-ready-transition-failure"
    )

    def fail_ready_transition(*_args, **_kwargs):
        raise ValueError("injected ready-for-g4 persistence failure")

    monkeypatch.setattr(
        release_container.pcb_candidates,
        "mark_ready_for_g4",
        fail_ready_transition,
    )

    assert release_container.worker.run_once() is True

    failed = release_container.tasks.get(task.id)
    restored = release_container.pcb_candidates.get(candidate.id)
    assert failed.status is TaskStatus.FAILED_TERMINAL
    assert failed.last_error_code == "PCB_RELEASE_EXPORT_FAILED"
    assert restored.status is PcbCandidateStatus.G3_APPROVED


def test_release_cancellation_before_ready_fence_rolls_back_publication(
    release_container, tmp_path: Path, monkeypatch
) -> None:
    candidate = _approved_candidate(release_container, tmp_path)
    task = release_container.pcb_release.enqueue_export(
        candidate.id, "release-cancel-before-ready-fence"
    )
    objects_before = _artifact_object_paths(release_container)
    original_mark_ready = release_container.pcb_candidates.mark_ready_for_g4

    def cancel_before_ready_fence(*args, **kwargs):
        release_container.tasks.cancel(
            task.id, "cancel before ready fence", utc_now()
        )
        return original_mark_ready(*args, **kwargs)

    monkeypatch.setattr(
        release_container.pcb_candidates,
        "mark_ready_for_g4",
        cancel_before_ready_fence,
    )

    assert release_container.worker.run_once() is True

    cancelled_task = release_container.tasks.get(task.id)
    cancelled = release_container.pcb_candidates.get(candidate.id)
    assert cancelled_task.status is TaskStatus.CANCELLED
    assert cancelled.status is PcbCandidateStatus.CANCELLED
    assert cancelled.output_kind == "boardir_only"
    assert cancelled.result["release"] == {
        "task_id": task.id,
        "idempotency_key": "release-cancel-before-ready-fence",
        "status": "cancelled",
        "error_code": "TASK_CANCELLED",
    }
    assert _artifact_object_paths(release_container) == objects_before


def test_release_cancellation_after_initial_ready_fence_is_not_unhandled(
    release_container, tmp_path: Path, monkeypatch, caplog
) -> None:
    candidate = _approved_candidate(release_container, tmp_path)
    task = release_container.pcb_release.enqueue_export(
        candidate.id, "release-cancel-after-initial-ready-fence"
    )
    objects_before = _artifact_object_paths(release_container)
    original_fence = (
        release_container.pcb_candidates._assert_active_or_mirror_cancellation
    )
    original_handler = release_container.worker._handlers[PCB_EXPORT_RELEASE_TASK_KIND]
    handler_errors: list[BaseException] = []
    cancelled_after_fence = False

    def observe_handler_error(lease):
        try:
            return original_handler(lease)
        except BaseException as error:
            handler_errors.append(error)
            raise

    def cancel_after_initial_fence(candidate_id, task_id, lease_token, now):
        nonlocal cancelled_after_fence
        original_fence(candidate_id, task_id, lease_token, now)
        if not cancelled_after_fence:
            cancelled_after_fence = True
            release_container.tasks.cancel(
                task.id, "cancel after initial ready fence", utc_now()
            )

    monkeypatch.setattr(
        release_container.pcb_candidates,
        "_assert_active_or_mirror_cancellation",
        cancel_after_initial_fence,
    )
    monkeypatch.setitem(
        release_container.worker._handlers,
        PCB_EXPORT_RELEASE_TASK_KIND,
        observe_handler_error,
    )

    assert release_container.worker.run_once() is True

    cancelled_task = release_container.tasks.get(task.id)
    cancelled = release_container.pcb_candidates.get(candidate.id)
    assert cancelled_task.status is TaskStatus.CANCELLED
    assert cancelled.status is PcbCandidateStatus.CANCELLED
    assert cancelled.output_kind == "boardir_only"
    assert cancelled.result["release"] == {
        "task_id": task.id,
        "idempotency_key": "release-cancel-after-initial-ready-fence",
        "status": "cancelled",
        "error_code": "TASK_CANCELLED",
    }
    assert _artifact_object_paths(release_container) == objects_before
    assert len(handler_errors) == 1
    assert isinstance(handler_errors[0], TaskCancelledError)
    assert not any(
        record.getMessage() == "task.unhandled_error" for record in caplog.records
    )


def test_release_cancellation_during_final_fence_removes_candidate_association(
    release_container, tmp_path: Path, monkeypatch
) -> None:
    candidate = _approved_candidate(release_container, tmp_path)
    task = release_container.pcb_release.enqueue_export(
        candidate.id, "release-cancel-during-final-fence"
    )
    registered = False
    published_digests: set[str] = set()
    original_register = release_container.pcb_candidates._register_release_artifacts
    original_assert_active = release_container.tasks.assert_active

    def observe_registration(session, descriptors, now):
        nonlocal registered
        original_register(session, descriptors, now)
        published_digests.update(descriptor.digest for descriptor in descriptors)
        registered = True

    def cancel_during_final_fence(task_id, lease_token, now):
        if registered:
            release_container.tasks.cancel(
                task.id, "cancel during final release fence", utc_now()
            )
        return original_assert_active(task_id, lease_token, now)

    monkeypatch.setattr(
        release_container.pcb_candidates,
        "_register_release_artifacts",
        observe_registration,
    )
    monkeypatch.setattr(
        release_container.tasks,
        "assert_active",
        cancel_during_final_fence,
    )

    assert release_container.worker.run_once() is True

    cancelled_task = release_container.tasks.get(task.id)
    cancelled = release_container.pcb_candidates.get(candidate.id)
    assert cancelled_task.status is TaskStatus.CANCELLED
    assert cancelled.status is PcbCandidateStatus.CANCELLED
    assert cancelled.output_kind == "boardir_only"
    assert cancelled.result["release"] == {
        "task_id": task.id,
        "idempotency_key": "release-cancel-during-final-fence",
        "status": "cancelled",
        "error_code": "TASK_CANCELLED",
    }
    rows = _registered_artifacts(release_container, published_digests)
    assert set(rows) == published_digests
    assert all(Path(row.storage_path).is_file() for row in rows.values())
    with pytest.raises(PcbCandidateNotReviewableError):
        release_container.pcb_release_approvals.decide_g4(
            candidate_id=candidate.id,
            manifest_digest=next(
                digest
                for digest, row in rows.items()
                if row.media_type == "application/vnd.pcbflow.release-manifest+json"
            ),
            idempotency_key="release-cancelled-g4",
            actor_id="release-reviewer",
            decision="approve",
            comment="must remain cancelled",
        )


def test_release_stale_final_fence_reverts_exact_pending_transition(
    release_container, tmp_path: Path, monkeypatch
) -> None:
    candidate = _approved_candidate(release_container, tmp_path)
    task = release_container.pcb_release.enqueue_export(
        candidate.id, "release-stale-during-final-fence"
    )
    pending = release_container.pcb_candidates.get(candidate.id)
    registered = False
    published_digests: set[str] = set()
    original_register = release_container.pcb_candidates._register_release_artifacts
    original_assert_active = release_container.tasks.assert_active

    def observe_registration(session, descriptors, now):
        nonlocal registered
        original_register(session, descriptors, now)
        published_digests.update(descriptor.digest for descriptor in descriptors)
        registered = True

    def lose_lease_during_final_fence(task_id, lease_token, now):
        if registered:
            raise StaleLeaseError(task_id)
        return original_assert_active(task_id, lease_token, now)

    monkeypatch.setattr(
        release_container.pcb_candidates,
        "_register_release_artifacts",
        observe_registration,
    )
    monkeypatch.setattr(
        release_container.tasks,
        "assert_active",
        lose_lease_during_final_fence,
    )

    assert release_container.worker.run_once() is True

    reverted = release_container.pcb_candidates.get(candidate.id)
    assert reverted.status is PcbCandidateStatus.RELEASE_PENDING
    assert reverted.result == pending.result
    assert reverted.last_error_code == pending.last_error_code
    assert reverted.version == pending.version + 2
    rows = _registered_artifacts(release_container, published_digests)
    assert set(rows) == published_digests
    assert all(Path(row.storage_path).is_file() for row in rows.values())


def test_release_rejects_native_candidate_readback_mismatch(tmp_path: Path) -> None:
    services = _container_with_adapter(
        tmp_path, MismatchedReadbackReleaseFixtureAdapter()
    )
    try:
        candidate = _approved_candidate(services, tmp_path)
        task = services.pcb_release.enqueue_export(
            candidate.id, "release-readback-mismatch"
        )

        assert services.worker.run_once() is True

        failed = services.tasks.get(task.id)
        restored = services.pcb_candidates.get(candidate.id)
        assert failed.status is TaskStatus.FAILED_TERMINAL
        assert failed.last_error_code == "PCB_RELEASE_CANDIDATE_MISMATCH"
        assert restored.status is PcbCandidateStatus.G3_APPROVED
    finally:
        services.dispose()


def test_release_rejects_adapter_mutation_of_registered_source(tmp_path: Path) -> None:
    services = _container_with_adapter(tmp_path, SourceMutatingReleaseFixtureAdapter())
    try:
        candidate = _approved_candidate(services, tmp_path)
        task = services.pcb_release.enqueue_export(
            candidate.id, "release-source-mutation"
        )

        assert services.worker.run_once() is True

        failed = services.tasks.get(task.id)
        restored = services.pcb_candidates.get(candidate.id)
        assert failed.status is TaskStatus.FAILED_TERMINAL
        assert failed.last_error_code == "PCB_CANDIDATE_SOURCE_CHANGED"
        assert restored.status is PcbCandidateStatus.G3_APPROVED
    finally:
        services.dispose()


def test_release_rejects_adapter_source_snapshot_drift(tmp_path: Path) -> None:
    adapter = ToggleSourceSnapshotReleaseFixtureAdapter()
    services = _container_with_adapter(tmp_path, adapter)
    try:
        candidate = _approved_candidate(services, tmp_path)
        task = services.pcb_release.enqueue_export(
            candidate.id, "release-source-snapshot-drift"
        )
        adapter.drift_source_snapshot = True

        assert services.worker.run_once() is True

        failed = services.tasks.get(task.id)
        restored = services.pcb_candidates.get(candidate.id)
        assert failed.status is TaskStatus.FAILED_TERMINAL
        assert failed.last_error_code == "PCB_CANDIDATE_INPUT_DIGEST_MISMATCH"
        assert restored.status is PcbCandidateStatus.G3_APPROVED
    finally:
        services.dispose()

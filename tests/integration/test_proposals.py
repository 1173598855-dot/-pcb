from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pcbflow.config import Settings
from pcbflow.container import build_container
from pcbflow.domain import TaskLease, TaskStatus
from pcbflow.kicad import KicadCapability, RawValidationReport
from pcbflow.proposals import ProposalStatus
from pcbflow.repositories import StaleLeaseError
from pcbflow.validation import ProjectCopyLimitError, assert_project_tree_safe
from pcbflow.tables import TaskRow
from sqlalchemy import update
from pcbflow.tasks import TerminalTaskError

NOW = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
PASSING_ERC = b'{"version":"1.0","source":"board.kicad_sch","violations":[]}'
FAILING_ERC = (b'{"version":"1.0","source":"board.kicad_sch","violations":[' b'{"type":"pin_not_connected","severity":"error","description":"pin is not connected","items":[]}]}')


class FakeProposalKicad:
    def __init__(self, report: bytes = PASSING_ERC) -> None:
        self.report = report

    def probe(self) -> KicadCapability:
        return KicadCapability(True, Path("kicad-cli"), "9.0.2", "sha256:" + "9" * 64, None)

    def validate(self, project_dir: Path, output_dir: Path) -> tuple[RawValidationReport, ...]:
        assert project_dir.is_dir()
        output_dir.mkdir(parents=True, exist_ok=True)
        return (RawValidationReport("erc", self.report, ("kicad-cli", "sch", "erc"), 0, "9.0.2"),)


def test_container_exposes_the_injected_kicad_port(tmp_path: Path) -> None:
    fixtures = Path(__file__).resolve().parents[1] / "fixtures"
    fake = FakeProposalKicad()
    container = build_container(_settings(tmp_path, fixtures / "modules"), kicad_override=fake, clock=lambda: NOW)
    try:
        assert container.kicad is fake
    finally:
        container.dispose()


def test_candidate_tree_limits_are_enforced_before_persistence(tmp_path: Path) -> None:
    (tmp_path / "one").write_bytes(b"1")
    (tmp_path / "two").write_bytes(b"2")
    with pytest.raises(ProjectCopyLimitError, match="more than 1 files"):
        assert_project_tree_safe(tmp_path, max_files=1, max_bytes=100)


def _settings(tmp_path: Path, module_catalog: Path) -> Settings:
    return Settings.from_env({"PCBFLOW_DATA_DIR": str(tmp_path / "data"), "PCBFLOW_MODULE_CATALOG_DIR": str(module_catalog)})


def _snapshot(root: Path) -> dict[Path, bytes]:
    return {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def _prepare(container, tmp_path: Path):
    fixtures = Path(__file__).resolve().parents[1] / "fixtures"
    source = tmp_path / "import-source"
    shutil.copytree(fixtures / "kicad" / "controlled-design", source)
    project = container.projects.create("Controller", source, "proposal-project")
    managed = container.revisions.adopt(project.id, "proposal-adopt")
    requirements = (fixtures / "requirements" / "reference-controller.yaml").read_bytes()
    draft = container.requirements.import_draft(managed.id, requirements, "proposal-requirements")
    pending = container.requirements.submit(draft.id, "proposal-requirements-submit")
    frozen = container.approvals.decide_g1(requirement_set_id=pending.id, subject_digest=pending.subject_digest(), decision="approve", actor_type="human", actor_id="local-user", comment="approved", idempotency_key="proposal-g1")
    return source, container.projects.get(managed.id), frozen


def _instantiate_batch(project, requirement_set) -> bytes:
    actor = {"type": "human", "id": "local-user"}
    value = {"schema_version": "1.0", "batch_id": "bat_execute_status_led", "project_id": project.id, "base_revision": project.current_revision, "requirement_set_id": requirement_set.id, "idempotency_key": "execute-status-led", "actor": actor, "intent": "Instantiate the verified status LED", "risk": "medium", "commands": [{"schema_version": "1.0", "command_id": "cmd_execute_status_led", "batch_id": "bat_execute_status_led", "project_id": project.id, "base_revision": project.current_revision, "idempotency_key": "execute-status-led:1", "actor": actor, "intent": "Instantiate the verified status LED", "risk": "medium", "preconditions": [{"type": "project.revision_equals", "revision": project.current_revision}, {"type": "requirements.digest_equals", "digest": requirement_set.canonical_digest}, {"type": "schematic.module_absent", "instance_name": "STATUS_LED"}, {"type": "tool.capability_available", "capability": "kicad.cst.write.v1"}], "operation": {"type": "schematic.instantiate_module", "payload": {"module_revision_id": "modrev_status_led_v1", "instance_name": "STATUS_LED", "target_sheet_ref": {"kind": "sheet", "sheet_uuid": "00000000-0000-0000-0000-000000000001", "object_uuid": "00000000-0000-0000-0000-000000000001", "pin_number": None}, "parameter_bindings": {"LED_VALUE": "GREEN"}, "port_bindings": {}, "placement_slot": "auto"}}, "required_validations": ["semantic_diff"], "provenance": {"requirement_ids": ["REQ-FUNC-001"], "evidence_ids": [], "module_revision_ids": ["modrev_status_led_v1"]}}]}
    return json.dumps(value, separators=(",", ":")).encode()


def test_worker_builds_one_reviewable_candidate_and_complete_evidence(tmp_path: Path) -> None:
    fixtures = Path(__file__).resolve().parents[1] / "fixtures"
    container = build_container(_settings(tmp_path, fixtures / "modules"), kicad_override=FakeProposalKicad(), clock=lambda: NOW)
    try:
        source, project, requirement_set = _prepare(container, tmp_path)
        source_before = _snapshot(source)
        proposal = container.proposals.create(_instantiate_batch(project, requirement_set), "execute-status-led")
        assert container.worker.run_once()
        ready = container.proposal_store.get(proposal.id)
        task = container.tasks.get(proposal.task_id)
        assert ready.status is ProposalStatus.READY_FOR_REVIEW
        assert ready.candidate_revision is not None
        assert ready.review_digest is not None
        assert task.status is TaskStatus.SUCCEEDED
        assert container.revisions.resolve_proposal_ref(project.id, proposal.id) == ready.candidate_revision
        commit_metadata = container.revisions.git._invoke(
            ["git", f"--git-dir={container.revisions.repo_path(project.id)}", "show", "-s", "--format=%an <%ae>", ready.candidate_revision.removeprefix("git:")],
            container.revisions.repo_path(project.id).parent,
        )
        assert "local-user" in commit_metadata.stdout
        evidence = container.evidence.list_for_project(project.id)
        assert {"design_command_batch", "project_snapshot_before", "project_snapshot_after", "git_text_diff", "schematic_semantic_diff", "kicad_erc", "command_execution_log", "adapter_capability_report", "proposal_evidence_set"} <= {item.kind for item in evidence}
        assert all(container.artifacts.verify(item.artifact_digest) for item in evidence)
        assert _snapshot(source) == source_before
    finally:
        container.dispose()


def test_erc_failure_never_becomes_reviewable_or_advances_revision(tmp_path: Path) -> None:
    fixtures = Path(__file__).resolve().parents[1] / "fixtures"
    container = build_container(_settings(tmp_path, fixtures / "modules"), kicad_override=FakeProposalKicad(FAILING_ERC), clock=lambda: NOW)
    try:
        _source, project, requirement_set = _prepare(container, tmp_path)
        before_revision = project.current_revision
        proposal = container.proposals.create(_instantiate_batch(project, requirement_set), "execute-status-led")
        assert container.worker.run_once()
        failed = container.proposal_store.get(proposal.id)
        assert failed.status is ProposalStatus.VALIDATION_FAILED
        assert failed.candidate_revision is None
        assert failed.evidence_set_digest is not None
        assert failed.semantic_diff_digest is not None
        assert failed.result is not None
        assert "adapter_capability_report" in failed.result["artifact_digests"]
        assert all(container.artifacts.verify(item.artifact_digest) for item in container.evidence.list_for_project(project.id) if item.task_id == proposal.task_id)
        assert container.projects.get(project.id).current_revision == before_revision
    finally:
        container.dispose()


def test_expired_lease_cannot_mark_proposal_executing(tmp_path: Path) -> None:
    fixtures = Path(__file__).resolve().parents[1] / "fixtures"
    container = build_container(_settings(tmp_path, fixtures / "modules"), kicad_override=FakeProposalKicad(), clock=lambda: NOW)
    try:
        _source, project, requirement_set = _prepare(container, tmp_path)
        proposal = container.proposals.create(_instantiate_batch(project, requirement_set), "execute-status-led")
        lease = container.tasks.claim_next("worker-a", NOW, 1)
        assert lease is not None
        container.tasks.start(lease.task_id, lease.lease_token, NOW)
        with pytest.raises(StaleLeaseError):
            container.proposal_executor.begin(lease, now=NOW + timedelta(seconds=1))
        assert container.proposal_store.get(proposal.id).status is ProposalStatus.QUEUED
    finally:
        container.dispose()


def test_fence_is_rechecked_after_execution_begins(tmp_path: Path) -> None:
    fixtures = Path(__file__).resolve().parents[1] / "fixtures"
    container = build_container(_settings(tmp_path, fixtures / "modules"), kicad_override=FakeProposalKicad(), clock=lambda: NOW)
    try:
        _source, project, requirement_set = _prepare(container, tmp_path)
        proposal = container.proposals.create(_instantiate_batch(project, requirement_set), "execute-status-led")
        lease = container.tasks.claim_next("worker-a", NOW, 1)
        assert lease is not None
        container.tasks.start(lease.task_id, lease.lease_token, NOW)
        container.proposal_executor.begin(lease, now=NOW)
        with pytest.raises(StaleLeaseError):
            container.tasks.assert_active(lease.task_id, lease.lease_token, NOW + timedelta(seconds=1))
        assert container.proposal_store.get(proposal.id).status is ProposalStatus.EXECUTING
    finally:
        container.dispose()


def test_ready_replay_rejects_corrupted_evidence_object(tmp_path: Path) -> None:
    fixtures = Path(__file__).resolve().parents[1] / "fixtures"
    container = build_container(_settings(tmp_path, fixtures / "modules"), kicad_override=FakeProposalKicad(), clock=lambda: NOW)
    try:
        _source, project, requirement_set = _prepare(container, tmp_path)
        proposal = container.proposals.create(_instantiate_batch(project, requirement_set), "execute-status-led")
        assert container.worker.run_once()
        ready = container.proposal_store.get(proposal.id)
        evidence = next(item for item in container.evidence.list_for_project(project.id) if item.task_id == proposal.task_id)
        Path(container.artifacts._path(evidence.artifact_digest)).write_bytes(b"corrupt replay evidence")
        replay_token = "replay-token"
        with container.sessions.begin() as session:
            session.execute(update(TaskRow).where(TaskRow.id == proposal.task_id).values(
                status=TaskStatus.RUNNING.value, lease_token=replay_token,
                lease_expires_at=NOW + timedelta(seconds=60)))
        lease = TaskLease(proposal.task_id, "design.execute_proposal", {"proposal_id": proposal.id}, replay_token, NOW + timedelta(seconds=60), 2)
        with pytest.raises(TerminalTaskError, match="stored proposal evidence integrity"):
            container.proposal_executor(lease)
    finally:
        container.dispose()


def _no_effect_batch(project, requirement_set) -> bytes:
    value = json.loads(_instantiate_batch(project, requirement_set))
    value["batch_id"] = "bat_no_effect"; value["idempotency_key"] = "proposal-no-effect"; value["intent"] = "Write the existing value"
    command = value["commands"][0]
    command["batch_id"] = "bat_no_effect"; command["command_id"] = "cmd_no_effect"; command["idempotency_key"] = "proposal-no-effect:1"; command["intent"] = "Write the existing value"; command["preconditions"] = []
    command["operation"] = {"type": "schematic.set_property", "payload": {"subject_ref": {"kind": "symbol", "sheet_uuid": "00000000-0000-0000-0000-000000000001", "object_uuid": "00000000-0000-0000-0000-000000000002", "pin_number": None}, "property_name": "Value", "value": "状态LED", "expected_old_value": "状态LED"}}
    command["provenance"]["module_revision_ids"] = []
    return json.dumps(value, separators=(",", ":")).encode()


def test_no_effect_batch_fails_without_candidate_or_revision_change(tmp_path: Path) -> None:
    fixtures = Path(__file__).resolve().parents[1] / "fixtures"
    container = build_container(_settings(tmp_path, fixtures / "modules"), kicad_override=FakeProposalKicad(), clock=lambda: NOW)
    try:
        _source, project, requirement_set = _prepare(container, tmp_path)
        proposal = container.proposals.create(_no_effect_batch(project, requirement_set), "proposal-no-effect")
        assert container.worker.run_once()
        failed = container.proposal_store.get(proposal.id); task = container.tasks.get(proposal.task_id)
        assert failed.status is ProposalStatus.VALIDATION_FAILED; assert failed.candidate_revision is None; assert failed.evidence_set_digest is not None; assert task.last_error_code == "DESIGN_COMMAND_NO_EFFECT"
        assert container.revisions.resolve_proposal_ref(project.id, proposal.id) is None
        assert container.projects.get(project.id).current_revision == project.current_revision
    finally:
        container.dispose()

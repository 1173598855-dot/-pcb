from __future__ import annotations

import importlib
import io
import json
from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest
from sqlalchemy.orm import Session

from pcbflow.cancellation import TaskCancelledError
from pcbflow.canonical import canonical_json_bytes
from pcbflow.domain import EdaKind, EdaOperation, RequestInvalidError, TaskStatus, utc_now
from pcbflow.eda import EdaCapability, ProjectEdaAuthorityInput
from pcbflow.lceda_pro import LcedaProCapabilityError
from pcbflow.repositories import ProjectNotFoundError
from pcbflow.tables import ArtifactRow


CAPABILITY_MEDIA_TYPE = "application/vnd.pcbflow.eda-capability+json"


def _workflow_module() -> ModuleType:
    try:
        return importlib.import_module("pcbflow.pcb_workflow")
    except ModuleNotFoundError as error:
        pytest.fail(f"persistent LCEDA capability workflow is missing: {error}")


@pytest.fixture
def lceda_project(container, tmp_path: Path):
    source = tmp_path / "lceda-project"
    source.mkdir()
    project = container.projects.create("LCEDA Controller", source, "lceda-project")
    container.eda_authorities.configure(
        project.id,
        ProjectEdaAuthorityInput(
            eda_kind=EdaKind.LCEDA_PRO,
            eda_profile_id="lceda-pro-v1",
            board_profile_id="stm32-environment-controller-2l-v1",
            rulepack_digest="sha256:" + "1" * 64,
        ),
        "lceda-authority",
    )
    return project


def _verified_capability(executable: Path) -> EdaCapability:
    return EdaCapability(
        available=True,
        executable=executable,
        version="3.2.166",
        executable_digest="sha256:" + "2" * 64,
        profile_id="lceda-pro-v1",
        profile_revision=1,
        operations=frozenset(
            {
                EdaOperation.APPLY_OPERATIONS,
                EdaOperation.SNAPSHOT,
                EdaOperation.CREATE_CANDIDATE,
            }
        ),
        write_verified=True,
        reason=None,
    )


def test_capability_task_persists_a_digest_bound_negative_result(
    container, lceda_project
) -> None:
    workflow = _workflow_module()

    task = container.capability_gate.enqueue(lceda_project.id, "probe-v1")
    assert task.kind == workflow.LCEDA_CAPABILITY_TASK_KIND
    assert container.worker.run_once() is True

    completed = container.tasks.get(task.id)
    assert completed.status is TaskStatus.SUCCEEDED
    assert completed.result is not None
    assert completed.result["write_verified"] is False
    evidence = container.evidence.list_for_project(lceda_project.id)
    assert len(evidence) == 1
    assert evidence[0].kind == "lceda_pro_capability"
    assert evidence[0].subject == "lceda-pro-v1"
    assert evidence[0].verdict == "blocked"
    assert completed.result["capability_digest"] == evidence[0].artifact_digest
    assert container.artifacts.verify(evidence[0].artifact_digest)

    expected = canonical_json_bytes(
        {
            "available": False,
            "executable": None,
            "executable_digest": None,
            "operations": [],
            "profile_id": None,
            "profile_revision": None,
            "reason": "lceda_pro_not_found",
            "version": None,
            "write_verified": False,
        }
    )
    with container.artifacts.open(evidence[0].artifact_digest) as stream:
        assert stream.read() == expected
    with container.sessions() as session:
        artifact = session.get(ArtifactRow, evidence[0].artifact_digest)
        assert artifact is not None
        assert artifact.media_type == CAPABILITY_MEDIA_TYPE


def test_capability_enqueue_replays_and_rejects_kicad_authority(
    container, lceda_project, tmp_path: Path
) -> None:
    first = container.capability_gate.enqueue(lceda_project.id, "probe-replay")
    assert container.capability_gate.enqueue(lceda_project.id, "probe-replay") == first

    source = tmp_path / "kicad-project"
    source.mkdir()
    kicad_project = container.projects.create("KiCad", source, "kicad-project")
    container.eda_authorities.configure(
        kicad_project.id,
        ProjectEdaAuthorityInput(
            eda_kind=EdaKind.KICAD,
            eda_profile_id="kicad-9-v1",
            board_profile_id="controller-2l-v1",
            rulepack_digest="sha256:" + "3" * 64,
        ),
        "kicad-authority",
    )

    with pytest.raises(ValueError, match="lceda_pro"):
        container.capability_gate.enqueue(kicad_project.id, "probe-kicad")


def test_cancelled_handler_fences_probe_before_any_external_call(
    container, lceda_project
) -> None:
    workflow = _workflow_module()

    class ProbeMustNotRun:
        def probe(self) -> EdaCapability:
            raise AssertionError("adapter probe must not run after cancellation")

    task = container.capability_gate.enqueue(lceda_project.id, "probe-cancelled")
    lease = container.tasks.claim_next("worker-test", utc_now(), 30)
    assert lease is not None and lease.task_id == task.id
    container.tasks.start(lease.task_id, lease.lease_token, utc_now())
    container.tasks.cancel(task.id, "cancel before probe", utc_now())
    handler = workflow.CapabilityProbeTaskHandler(
        container.eda_authorities,
        container.tasks,
        container.evidence,
        container.artifacts,
        ProbeMustNotRun(),
    )

    with pytest.raises(TaskCancelledError):
        handler(lease)


def test_handler_fences_artifact_and_evidence_publication(
    container, lceda_project, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow = _workflow_module()
    artifact_calls = 0

    class CancelAfterProbe:
        def probe(self) -> EdaCapability:
            container.tasks.cancel(task.id, "cancel after probe", utc_now())
            return _verified_capability(tmp_path / "lceda-pro.exe")

    task = container.capability_gate.enqueue(lceda_project.id, "probe-fence-artifact")
    lease = container.tasks.claim_next("worker-artifact", utc_now(), 30)
    assert lease is not None and lease.task_id == task.id
    container.tasks.start(lease.task_id, lease.lease_token, utc_now())
    original_put = container.artifacts.put_bytes

    def record_put(data: bytes, media_type: str):
        nonlocal artifact_calls
        artifact_calls += 1
        return original_put(data, media_type)

    monkeypatch.setattr(container.artifacts, "put_bytes", record_put)
    handler = workflow.CapabilityProbeTaskHandler(
        container.eda_authorities,
        container.tasks,
        container.evidence,
        container.artifacts,
        CancelAfterProbe(),
    )

    with pytest.raises(TaskCancelledError):
        handler(lease)
    assert artifact_calls == 0
    assert container.evidence.list_for_project(lceda_project.id) == []

    second = container.capability_gate.enqueue(
        lceda_project.id, "probe-fence-evidence"
    )
    second_lease = container.tasks.claim_next("worker-evidence", utc_now(), 30)
    assert second_lease is not None and second_lease.task_id == second.id
    container.tasks.start(second_lease.task_id, second_lease.lease_token, utc_now())

    class VerifiedAdapter:
        def probe(self) -> EdaCapability:
            return _verified_capability(tmp_path / "lceda-pro.exe")

    def object_paths() -> set[Path]:
        objects = container.artifacts.root / "objects"
        return {path.resolve() for path in objects.rglob("*") if path.is_file()}

    before_paths = object_paths()
    published_descriptor = None

    original_publish = container.artifacts._publish_staged

    def cancel_after_publish(staged):
        nonlocal published_descriptor
        descriptor = original_publish(staged)
        published_descriptor = descriptor
        container.tasks.cancel(second.id, "cancel before evidence", utc_now())
        return descriptor

    monkeypatch.setattr(container.artifacts, "_publish_staged", cancel_after_publish)

    handler = workflow.CapabilityProbeTaskHandler(
        container.eda_authorities,
        container.tasks,
        container.evidence,
        container.artifacts,
        VerifiedAdapter(),
    )
    with pytest.raises(TaskCancelledError):
        handler(second_lease)
    assert container.evidence.list_for_project(lceda_project.id) == []
    assert published_descriptor is not None
    assert object_paths() == before_paths
    with container.sessions() as session:
        assert session.get(ArtifactRow, published_descriptor.digest) is None


def test_saved_negative_capability_stably_blocks_lceda_candidate_write(
    container, lceda_project
) -> None:
    container.capability_gate.enqueue(lceda_project.id, "probe-for-candidate")
    assert container.worker.run_once() is True

    with pytest.raises(LcedaProCapabilityError) as raised:
        container.capability_gate.require_operation(
            lceda_project.id,
            EdaKind.LCEDA_PRO,
            EdaOperation.CREATE_CANDIDATE,
        )

    assert raised.value.code == "LCEDA_PRO_WRITE_CAPABILITY_UNVERIFIED"


def _run_verified_probe(container, project, tmp_path: Path):
    workflow = _workflow_module()

    class VerifiedAdapter:
        def probe(self) -> EdaCapability:
            return _verified_capability(tmp_path / "lceda-pro.exe")

    task = container.capability_gate.enqueue(project.id, "verified-probe")
    lease = container.tasks.claim_next("verified-worker", utc_now(), 30)
    assert lease is not None and lease.task_id == task.id
    container.tasks.start(lease.task_id, lease.lease_token, utc_now())
    result = workflow.CapabilityProbeTaskHandler(
        container.eda_authorities,
        container.tasks,
        container.evidence,
        container.artifacts,
        VerifiedAdapter(),
    )(lease)
    container.tasks.complete(task.id, lease.lease_token, result, utc_now())
    return task, result


def test_require_operation_requires_verified_canonical_succeeded_evidence(
    container, lceda_project, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task, result = _run_verified_probe(container, lceda_project, tmp_path)
    evidence = container.evidence.list_for_project(lceda_project.id)[0]
    assert result["capability_digest"] == evidence.artifact_digest
    container.capability_gate.require_operation(
        lceda_project.id, EdaKind.LCEDA_PRO, EdaOperation.CREATE_CANDIDATE
    )

    artifact_path = container.artifacts.root / "objects" / "sha256" / evidence.artifact_digest[7:9] / evidence.artifact_digest[9:11] / evidence.artifact_digest[7:]
    original = artifact_path.read_bytes()
    artifact_path.write_bytes(original + b"\n")
    with pytest.raises(LcedaProCapabilityError):
        container.capability_gate.require_operation(
            lceda_project.id, EdaKind.LCEDA_PRO, EdaOperation.CREATE_CANDIDATE
        )
    artifact_path.write_bytes(original)

    monkeypatch.setattr(container.artifacts, "verify", lambda digest: True)
    artifact_path.write_bytes(b'{"operations":[]}')
    with pytest.raises(LcedaProCapabilityError):
        container.capability_gate.require_operation(
            lceda_project.id, EdaKind.LCEDA_PRO, EdaOperation.CREATE_CANDIDATE
        )
    artifact_path.write_bytes(original + b"\n")
    with pytest.raises(LcedaProCapabilityError):
        container.capability_gate.require_operation(
            lceda_project.id, EdaKind.LCEDA_PRO, EdaOperation.CREATE_CANDIDATE
        )


def test_require_operations_validates_a_frozen_operation_set_once(
    container, lceda_project, tmp_path: Path
) -> None:
    task, result = _run_verified_probe(container, lceda_project, tmp_path)
    operations = (
        EdaOperation.SNAPSHOT,
        EdaOperation.CREATE_CANDIDATE,
        EdaOperation.APPLY_OPERATIONS,
    )

    digest = container.capability_gate.require_operations(
        lceda_project.id, EdaKind.LCEDA_PRO, operations
    )
    assert digest == result["capability_digest"]
    with pytest.raises(LcedaProCapabilityError):
        container.capability_gate.require_operations(
            lceda_project.id,
            EdaKind.LCEDA_PRO,
            (*operations, EdaOperation.RUN_DRC),
        )


def test_gate_rejects_wrong_media_and_nonterminal_or_mismatched_task_binding(
    container, lceda_project, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task, result = _run_verified_probe(container, lceda_project, tmp_path)
    evidence = container.evidence.list_for_project(lceda_project.id)[0]
    monkeypatch.setattr(container.evidence, "artifact_media_type", lambda digest: "text/plain")
    with pytest.raises(LcedaProCapabilityError):
        container.capability_gate.require_operation(
            lceda_project.id, EdaKind.LCEDA_PRO, EdaOperation.CREATE_CANDIDATE
        )
    monkeypatch.undo()

    # A completed task result is the binding between the immutable evidence and gate.
    with container.sessions.begin() as session:
        row = session.get(ArtifactRow, evidence.artifact_digest)
        assert row is not None
        row.media_type = CAPABILITY_MEDIA_TYPE
    with container.sessions.begin() as session:
        from pcbflow.tables import TaskRow
        row = session.get(TaskRow, task.id)
        assert row is not None
        row.result_json = {"capability_digest": "sha256:" + "f" * 64, "write_verified": True}
    with pytest.raises(LcedaProCapabilityError):
        container.capability_gate.require_operation(
            lceda_project.id, EdaKind.LCEDA_PRO, EdaOperation.CREATE_CANDIDATE
        )


def test_handler_blocks_profile_mismatch_and_reuses_existing_evidence(
    container, lceda_project, tmp_path: Path
) -> None:
    workflow = _workflow_module()
    task = container.capability_gate.enqueue(lceda_project.id, "recovery-probe")
    lease = container.tasks.claim_next("recovery-worker", utc_now(), 30)
    assert lease is not None and lease.task_id == task.id
    container.tasks.start(lease.task_id, lease.lease_token, utc_now())

    calls = 0

    class VerifiedAdapter:
        def probe(self) -> EdaCapability:
            nonlocal calls
            calls += 1
            return _verified_capability(tmp_path / "lceda-pro.exe")

    handler = workflow.CapabilityProbeTaskHandler(
        container.eda_authorities, container.tasks, container.evidence,
        container.artifacts, VerifiedAdapter(),
    )
    first = handler(lease)
    second = handler(lease)
    assert first == second
    assert calls == 1
    assert len(container.evidence.list_for_project(lceda_project.id)) == 1

    mismatched = replace(
        _verified_capability(tmp_path / "lceda-pro.exe"), profile_id="wrong-profile"
    )
    # The constructor is deliberately called on a fresh task so a mismatch cannot
    # reuse the prior evidence.
    other = container.capability_gate.enqueue(lceda_project.id, "wrong-profile")
    other_lease = container.tasks.claim_next("profile-worker", utc_now(), 30)
    assert other_lease is not None and other_lease.task_id == other.id
    container.tasks.start(other.id, other_lease.lease_token, utc_now())

    class WrongProfileAdapter:
        def probe(self) -> EdaCapability:
            return mismatched

    result = workflow.CapabilityProbeTaskHandler(
        container.eda_authorities, container.tasks, container.evidence,
        container.artifacts, WrongProfileAdapter(),
    )(other_lease)
    assert result["write_verified"] is False
    assert container.evidence.list_for_project(lceda_project.id)[-1].verdict == "blocked"


def test_enqueue_validates_authority_and_idempotency_key(container, tmp_path: Path) -> None:
    source = tmp_path / "unconfigured"
    source.mkdir()
    project = container.projects.create("Unconfigured", source, "unconfigured")
    for key in (" ", "x" * 256):
        with pytest.raises(RequestInvalidError):
            container.capability_gate.enqueue(project.id, key)
    with pytest.raises(RequestInvalidError):
        container.capability_gate.enqueue(project.id, "valid-key")
    with pytest.raises(ProjectNotFoundError):
        container.capability_gate.enqueue("prj_missing", "missing-project")


def test_handler_recovers_precomplete_unavailable_evidence_without_reprobing(
    container, lceda_project, tmp_path: Path
) -> None:
    workflow = _workflow_module()
    task = container.capability_gate.enqueue(lceda_project.id, "unavailable-recovery")
    lease = container.tasks.claim_next("unavailable-worker", utc_now(), 30)
    assert lease is not None and lease.task_id == task.id
    container.tasks.start(task.id, lease.lease_token, utc_now())
    calls = 0

    class UnavailableAdapter:
        def probe(self) -> EdaCapability:
            nonlocal calls
            calls += 1
            return EdaCapability(
                available=False,
                executable=tmp_path / "known-lceda.exe",
                version="3.2.166",
                executable_digest="sha256:" + "3" * 64,
                profile_id=None,
                profile_revision=None,
                operations=frozenset(),
                write_verified=False,
                reason="version_command_failed",
            )

    handler = workflow.CapabilityProbeTaskHandler(
        container.eda_authorities, container.tasks, container.evidence,
        container.artifacts, UnavailableAdapter(),
    )
    first = handler(lease)
    second = handler(lease)
    assert first == second
    assert first["write_verified"] is False
    assert calls == 1
    assert container.evidence.list_for_project(lceda_project.id)[0].verdict == "blocked"


def test_gate_rejects_running_cancelled_and_verdict_mismatched_evidence(
    container, lceda_project, tmp_path: Path
) -> None:
    task, result = _run_verified_probe(container, lceda_project, tmp_path)
    evidence = container.evidence.list_for_project(lceda_project.id)[0]
    from pcbflow.tables import EvidenceRow, TaskRow

    for status in (TaskStatus.RUNNING.value, TaskStatus.CANCELLED.value):
        with container.sessions.begin() as session:
            row = session.get(TaskRow, task.id)
            assert row is not None
            row.status = status
        with pytest.raises(LcedaProCapabilityError):
            container.capability_gate.require_operation(
                lceda_project.id, EdaKind.LCEDA_PRO, EdaOperation.CREATE_CANDIDATE
            )
    with container.sessions.begin() as session:
        task_row = session.get(TaskRow, task.id)
        evidence_row = session.get(EvidenceRow, evidence.id)
        assert task_row is not None and evidence_row is not None
        task_row.status = TaskStatus.SUCCEEDED.value
        evidence_row.verdict = "blocked"
    with pytest.raises(LcedaProCapabilityError):
        container.capability_gate.require_operation(
            lceda_project.id, EdaKind.LCEDA_PRO, EdaOperation.CREATE_CANDIDATE
        )


def test_gate_hashes_the_same_raw_bytes_it_parses_after_object_replacement(
    container, lceda_project, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run_verified_probe(container, lceda_project, tmp_path)
    evidence = container.evidence.list_for_project(lceda_project.id)[0]
    path = container.artifacts.root / "objects" / "sha256" / evidence.artifact_digest[7:9] / evidence.artifact_digest[9:11] / evidence.artifact_digest[7:]
    replacement = canonical_json_bytes(
        {
            "available": True,
            "executable": "C:/replacement/lceda-pro.exe",
            "version": "3.2.166",
            "executable_digest": "sha256:" + "4" * 64,
            "profile_id": "lceda-pro-v1",
            "profile_revision": 1,
            "operations": ["apply_operations", "create_candidate", "snapshot"],
            "write_verified": True,
            "reason": None,
        }
    )
    path.write_bytes(replacement)
    with pytest.raises(LcedaProCapabilityError):
        container.capability_gate.require_operation(
            lceda_project.id, EdaKind.LCEDA_PRO, EdaOperation.CREATE_CANDIDATE
        )


def _complete_bound_capability_artifact(container, project, raw: bytes, key: str):
    workflow = _workflow_module()
    task = container.capability_gate.enqueue(project.id, key)
    lease = container.tasks.claim_next("artifact-worker", utc_now(), 30)
    assert lease is not None and lease.task_id == task.id
    container.tasks.start(task.id, lease.lease_token, utc_now())
    descriptor = container.artifacts.put_bytes(raw, CAPABILITY_MEDIA_TYPE)
    container.evidence.add_report(
        project.id,
        task.id,
        descriptor,
        workflow.CAPABILITY_EVIDENCE_KIND,
        "lceda-pro-v1",
        "pass",
        lease_token=lease.lease_token,
        now=utc_now(),
    )
    result = {"capability_digest": descriptor.digest, "write_verified": True}
    container.tasks.complete(task.id, lease.lease_token, result, utc_now())
    return descriptor


def test_gate_reads_one_digest_bound_payload_instead_of_verify_then_reopen(
    container, lceda_project, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow = _workflow_module()
    no_candidate = replace(
        _verified_capability(tmp_path / "lceda-pro.exe"),
        operations=frozenset({EdaOperation.APPLY_OPERATIONS, EdaOperation.SNAPSHOT}),
    )
    payload_a = workflow.capability_artifact_bytes(no_candidate)
    descriptor = _complete_bound_capability_artifact(
        container, lceda_project, payload_a, "toctou-bound-payload"
    )
    payload_b = workflow.capability_artifact_bytes(
        _verified_capability(tmp_path / "replacement-lceda-pro.exe")
    )
    calls = 0

    def sequential_open(digest: str):
        nonlocal calls
        assert digest == descriptor.digest
        calls += 1
        return io.BytesIO(payload_a if calls == 1 else payload_b)

    monkeypatch.setattr(container.artifacts, "open", sequential_open)
    with pytest.raises(LcedaProCapabilityError):
        container.capability_gate.require_operation(
            lceda_project.id, EdaKind.LCEDA_PRO, EdaOperation.CREATE_CANDIDATE
        )
    assert calls == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("executable", ""),
        ("version", ""),
        ("executable_digest", "sha256:" + "A" * 64),
    ],
)
def test_gate_rejects_invalid_verified_identity_fields(
    container, lceda_project, tmp_path: Path, field: str, value: str
) -> None:
    workflow = _workflow_module()
    payload = json.loads(
        workflow.capability_artifact_bytes(_verified_capability(tmp_path / "lceda-pro.exe"))
    )
    payload[field] = value
    _complete_bound_capability_artifact(
        container,
        lceda_project,
        canonical_json_bytes(payload),
        f"invalid-verified-{field}",
    )
    with pytest.raises(LcedaProCapabilityError):
        container.capability_gate.require_operation(
            lceda_project.id, EdaKind.LCEDA_PRO, EdaOperation.CREATE_CANDIDATE
        )

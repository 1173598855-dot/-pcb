from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.encoders import jsonable_encoder
from sqlalchemy import func, select

from pcbflow.api import create_app
from pcbflow.config import Settings
from pcbflow.container import Container, build_container
from pcbflow.design_tables import GateDecisionRow, ProjectRevisionRow
from pcbflow.kicad import KicadCapability, RawValidationReport
from pcbflow.proposals import (
    ChangeProposal,
    FaultInjector,
    FaultPoint,
    NoFaults,
    _fault_active,
)
from tests.component_fixtures import build_component_directory

PASSING_ERC = b'{"version":"1.0","source":"board.kicad_sch","violations":[]}'
NOW = datetime(2026, 7, 29, 14, 0, tzinfo=UTC)


class FakeKicad9:
    def probe(self) -> KicadCapability:
        return KicadCapability(
            True,
            Path("kicad-cli"),
            "9.0.2",
            "sha256:" + "9" * 64,
            None,
            9,
            "kicad-9-v1",
            1,
        )

    def validate(
        self, project_dir: Path, output_dir: Path
    ) -> tuple[RawValidationReport, ...]:
        output_dir.mkdir(parents=True, exist_ok=True)
        return (
            RawValidationReport(
                kind="erc",
                data=PASSING_ERC,
                argv=("kicad-cli", "sch", "erc"),
                returncode=0,
                tool_version="9.0.2",
                executable_digest="sha256:" + "9" * 64,
                profile_id="kicad-9-v1",
                profile_revision=1,
            ),
        )


def _client(container) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(container)),
        base_url="http://testserver",
    )


def _command_batch(project: dict, requirement_set: dict) -> dict:
    actor = {"type": "human", "id": "local-user"}
    return {
        "schema_version": "1.0",
        "batch_id": "bat_api_status_led",
        "project_id": project["id"],
        "base_revision": project["current_revision"],
        "requirement_set_id": requirement_set["id"],
        "idempotency_key": "api-proposal",
        "actor": actor,
        "intent": "Add the verified status LED",
        "risk": "medium",
        "commands": [
            {
                "schema_version": "1.0",
                "command_id": "cmd_api_status_led",
                "batch_id": "bat_api_status_led",
                "project_id": project["id"],
                "base_revision": project["current_revision"],
                "idempotency_key": "api-proposal:1",
                "actor": actor,
                "intent": "Add the verified status LED",
                "risk": "medium",
                "preconditions": [],
                "operation": {
                    "type": "schematic.instantiate_module",
                    "payload": {
                        "module_revision_id": "modrev_status_led_v1",
                        "instance_name": "STATUS_LED",
                        "target_sheet_ref": {
                            "kind": "sheet",
                            "sheet_uuid": "00000000-0000-0000-0000-000000000001",
                            "object_uuid": "00000000-0000-0000-0000-000000000001",
                            "pin_number": None,
                        },
                        "parameter_bindings": {"LED_VALUE": "GREEN"},
                        "port_bindings": {},
                        "placement_slot": "auto",
                    },
                },
                "required_validations": ["semantic_diff", "kicad_erc"],
                "provenance": {
                    "requirement_ids": ["REQ-FUNC-001"],
                    "evidence_ids": [],
                    "module_revision_ids": ["modrev_status_led_v1"],
                },
            }
        ],
    }


def _bound_command_batch(
    project: dict, requirement_set: dict, binding_id: str
) -> dict:
    batch = _command_batch(project, requirement_set)
    command = batch["commands"][0]
    payload = command["operation"]["payload"]
    command["operation"] = {
        "type": "schematic.instantiate_bound_module",
        "payload": {
            "component_module_binding_id": binding_id,
            "instance_name": "STATUS_LED",
            "target_sheet_ref": payload["target_sheet_ref"],
            "parameter_bindings": payload["parameter_bindings"],
            "port_bindings": payload["port_bindings"],
            "placement_slot": payload["placement_slot"],
        },
    }
    return batch


def test_rest_contract_covers_adopt_requirements_g1_and_proposal_queue(
    tmp_path: Path,
) -> None:
    fixtures = Path(__file__).resolve().parents[1] / "fixtures"
    source = tmp_path / "source"
    shutil.copytree(fixtures / "kicad" / "controlled-design", source)
    settings = Settings.from_env(
        {
            "PCBFLOW_DATA_DIR": str(tmp_path / "data"),
            "PCBFLOW_MODULE_CATALOG_DIR": str(fixtures / "modules"),
        }
    )
    container = build_container(settings, kicad_override=FakeKicad9())

    async def exercise() -> None:
        async with _client(container) as client:
            created = await client.post(
                "/api/v1/projects",
                headers={"Idempotency-Key": "api-create-project"},
                json={"name": "Controller", "source_path": str(source)},
            )
            assert created.status_code == 201
            project_id = created.json()["id"]
            adopted = await client.post(
                f"/api/v1/projects/{project_id}:adopt",
                headers={"Idempotency-Key": "api-adopt-project"},
            )
            assert adopted.status_code == 200
            project = adopted.json()
            assert project["mode"] == "managed"

            requirement_payload = yaml.safe_load(
                (fixtures / "requirements" / "reference-controller.yaml").read_text(
                    encoding="utf-8"
                )
            )
            imported = await client.post(
                f"/api/v1/projects/{project_id}/requirement-sets",
                headers={"Idempotency-Key": "api-import-requirements"},
                json=requirement_payload,
            )
            assert imported.status_code == 201
            assert imported.json()["subject_digest"] is None
            submitted = await client.post(
                f"/api/v1/requirement-sets/{imported.json()['id']}:submit",
                headers={"Idempotency-Key": "api-submit-requirements"},
            )
            assert submitted.status_code == 200
            assert submitted.json()["subject_digest"].startswith("sha256:")
            approved = await client.post(
                "/api/v1/approvals",
                headers={"Idempotency-Key": "api-approve-g1"},
                json={
                    "subject_type": "requirement_set",
                    "subject_id": submitted.json()["id"],
                    "subject_digest": submitted.json()["subject_digest"],
                    "decision": "approve",
                    "actor": {"type": "human", "id": "local-user"},
                    "comment": "approved",
                },
            )
            assert approved.status_code == 200
            assert approved.json()["status"] == "frozen"
            assert approved.json()["subject_digest"] == submitted.json()[
                "subject_digest"
            ]
            project = (await client.get("/api/v1/projects")).json()[0]

            batch = _command_batch(project, approved.json())
            queued = await client.post(
                f"/api/v1/projects/{project_id}/proposals",
                headers={"Idempotency-Key": "api-proposal"},
                json=batch,
            )
            assert queued.status_code == 202
            proposal_id = queued.json()["id"]
            assert (
                await client.get(f"/api/v1/proposals/{proposal_id}")
            ).status_code == 200

            missing_header = await client.post(
                f"/api/v1/projects/{project_id}/proposals", json=batch
            )
            assert missing_header.status_code == 422
            invalid = dict(batch)
            invalid["extra"] = True
            invalid_response = await client.post(
                f"/api/v1/projects/{project_id}/proposals",
                headers={"Idempotency-Key": "api-invalid-proposal"},
                json=invalid,
            )
            assert invalid_response.status_code == 422
            assert invalid_response.json()["error"]["code"] == (
                "DESIGN_COMMAND_SCHEMA_INVALID"
            )

            schema = create_app(container).openapi()
            routes = {
                f"{method.upper()} {path}"
                for path, operations in schema["paths"].items()
                for method in operations
                if method.lower() in {"get", "post", "put", "patch", "delete"}
            }
            assert {
                "POST /api/v1/projects/{project_id}:adopt",
                "POST /api/v1/projects/{project_id}/requirement-sets",
                "GET /api/v1/requirement-sets/{requirement_set_id}",
                "POST /api/v1/requirement-sets/{requirement_set_id}:submit",
                "POST /api/v1/approvals",
                "POST /api/v1/projects/{project_id}/proposals",
                "GET /api/v1/proposals/{proposal_id}",
                "GET /api/v1/proposals/{proposal_id}/diff",
                "POST /api/v1/proposals/{proposal_id}:accept",
                "POST /api/v1/proposals/{proposal_id}:reject",
            } <= routes

    try:
        asyncio.run(exercise())
    finally:
        container.dispose()


def test_rest_proposal_worker_diff_and_accept(
    tmp_path: Path,
) -> None:
    fixtures = Path(__file__).resolve().parents[1] / "fixtures"
    source = tmp_path / "source"
    shutil.copytree(fixtures / "kicad" / "controlled-design", source)
    settings = Settings.from_env(
        {
            "PCBFLOW_DATA_DIR": str(tmp_path / "data"),
            "PCBFLOW_MODULE_CATALOG_DIR": str(fixtures / "modules"),
        }
    )
    container = build_container(settings, kicad_override=FakeKicad9())

    async def exercise() -> None:
        async with _client(container) as client:
            created = await client.post(
                "/api/v1/projects",
                headers={"Idempotency-Key": "api-create-project"},
                json={"name": "Controller", "source_path": str(source)},
            )
            project_id = created.json()["id"]
            adopted = await client.post(
                f"/api/v1/projects/{project_id}:adopt",
                headers={"Idempotency-Key": "api-adopt-project"},
            )
            requirements = yaml.safe_load(
                (fixtures / "requirements" / "reference-controller.yaml").read_text(
                    encoding="utf-8"
                )
            )
            imported = await client.post(
                f"/api/v1/projects/{project_id}/requirement-sets",
                headers={"Idempotency-Key": "api-import-requirements"},
                json=requirements,
            )
            submitted = await client.post(
                f"/api/v1/requirement-sets/{imported.json()['id']}:submit",
                headers={"Idempotency-Key": "api-submit-requirements"},
            )
            approved = await client.post(
                "/api/v1/approvals",
                headers={"Idempotency-Key": "api-approve-g1"},
                json={
                    "subject_type": "requirement_set",
                    "subject_id": submitted.json()["id"],
                    "subject_digest": submitted.json()["subject_digest"],
                    "decision": "approve",
                    "actor": {"type": "human", "id": "local-user"},
                    "comment": "approved",
                },
            )
            assert adopted.status_code == 200
            assert approved.status_code == 200
            project = (await client.get("/api/v1/projects")).json()[0]
            queued = await client.post(
                f"/api/v1/projects/{project_id}/proposals",
                headers={"Idempotency-Key": "api-proposal"},
                json=_command_batch(project, approved.json()),
            )
            assert queued.status_code == 202
            proposal_id = queued.json()["id"]

            worked = await client.post("/api/v1/worker:run-once")
            assert worked.json() == {"handled": True}
            proposal = (await client.get(f"/api/v1/proposals/{proposal_id}")).json()
            assert proposal["status"] == "ready_for_review"
            diff = await client.get(f"/api/v1/proposals/{proposal_id}/diff")
            assert diff.status_code == 200
            assert diff.json()["changes"]

            rejected = await client.post(
                f"/api/v1/proposals/{proposal_id}:reject",
                headers={"Idempotency-Key": "api-reject-proposal"},
                json={
                    "actor": {"type": "human", "id": "local-user"},
                    "reason": "not ready for this review window",
                },
            )
            assert rejected.status_code == 200, rejected.text
            assert rejected.json()["status"] == "rejected"

            next_project = (await client.get("/api/v1/projects")).json()[0]
            next_batch = _command_batch(next_project, approved.json())
            next_batch["batch_id"] = "bat_api_status_led_next"
            next_batch["idempotency_key"] = "api-proposal-next"
            next_command = next_batch["commands"][0]
            next_command["batch_id"] = "bat_api_status_led_next"
            next_command["command_id"] = "cmd_api_status_led_next"
            next_command["idempotency_key"] = "api-proposal-next:1"
            queued_next = await client.post(
                f"/api/v1/projects/{project_id}/proposals",
                headers={"Idempotency-Key": "api-proposal-next"},
                json=next_batch,
            )
            assert queued_next.status_code == 202
            next_id = queued_next.json()["id"]
            assert (await client.post("/api/v1/worker:run-once")).json() == {
                "handled": True
            }
            next_proposal = (await client.get(f"/api/v1/proposals/{next_id}")).json()
            accepted = await client.post(
                f"/api/v1/proposals/{next_id}:accept",
                headers={"Idempotency-Key": "api-accept-proposal"},
                json={
                    "candidate_digest": next_proposal["review_digest"],
                    "actor": {"type": "human", "id": "local-user"},
                    "comment": "accepted",
                },
            )
            assert accepted.status_code == 200, accepted.text
            assert accepted.json()["status"] == "accepted"

    try:
        asyncio.run(exercise())
    finally:
        container.dispose()


def test_rest_worker_executes_a_bound_module_proposal(tmp_path: Path) -> None:
    fixtures = Path(__file__).resolve().parents[1] / "fixtures"
    source = tmp_path / "bound-source"
    shutil.copytree(fixtures / "kicad" / "controlled-design", source)
    settings = Settings.from_env(
        {
            "PCBFLOW_DATA_DIR": str(tmp_path / "data"),
            "PCBFLOW_MODULE_CATALOG_DIR": str(fixtures / "modules"),
        }
    )
    container = build_container(settings, kicad_override=FakeKicad9())
    component_directory = build_component_directory(
        tmp_path / "bound-component", include_model=True
    )
    component = container.components.import_revision(
        component_directory / "component.yaml", "rest-bound-component"
    )
    binding = container.component_module_bindings.bind(
        component.id, 9, "modrev_status_led_v1", "rest-bound-module-binding"
    )

    async def exercise() -> tuple[str, str]:
        async with _client(container) as client:
            created = await client.post(
                "/api/v1/projects",
                headers={"Idempotency-Key": "rest-bound-project"},
                json={"name": "Controller", "source_path": str(source)},
            )
            project_id = created.json()["id"]
            adopted = await client.post(
                f"/api/v1/projects/{project_id}:adopt",
                headers={"Idempotency-Key": "rest-bound-adopt"},
            )
            requirement_payload = yaml.safe_load(
                (fixtures / "requirements" / "reference-controller.yaml").read_text(
                    encoding="utf-8"
                )
            )
            imported = await client.post(
                f"/api/v1/projects/{project_id}/requirement-sets",
                headers={"Idempotency-Key": "rest-bound-requirements"},
                json=requirement_payload,
            )
            submitted = await client.post(
                f"/api/v1/requirement-sets/{imported.json()['id']}:submit",
                headers={"Idempotency-Key": "rest-bound-submit"},
            )
            approved = await client.post(
                "/api/v1/approvals",
                headers={"Idempotency-Key": "rest-bound-approval"},
                json={
                    "subject_type": "requirement_set",
                    "subject_id": submitted.json()["id"],
                    "subject_digest": submitted.json()["subject_digest"],
                    "decision": "approve",
                    "actor": {"type": "human", "id": "local-user"},
                    "comment": "approved",
                },
            )
            project = (await client.get("/api/v1/projects")).json()[0]
            queued = await client.post(
                f"/api/v1/projects/{project_id}/proposals",
                headers={"Idempotency-Key": "api-proposal"},
                json=_bound_command_batch(project, approved.json(), binding.id),
            )

            assert created.status_code == 201
            assert adopted.status_code == 200
            assert imported.status_code == 201
            assert submitted.status_code == 200
            assert approved.status_code == 200
            assert queued.status_code == 202
            proposal_id = queued.json()["id"]
            assert (await client.post("/api/v1/worker:run-once")).json() == {
                "handled": True
            }
            proposal = (await client.get(f"/api/v1/proposals/{proposal_id}")).json()
            assert proposal["status"] == "ready_for_review"
            return project_id, proposal_id

    try:
        project_id, proposal_id = asyncio.run(exercise())
        evidence = next(
            item
            for item in container.evidence.list_for_project(project_id)
            if item.task_id == container.proposal_store.get(proposal_id).task_id
            and item.kind == "adapter_capability_report"
        )
        with container.artifacts.open(evidence.artifact_digest) as artifact:
            report = json.loads(artifact.read())
        assert report["bound_module_resolutions"][0]["binding_id"] == binding.id
    finally:
        container.dispose()


def _snapshot(root: Path) -> dict[Path, bytes]:
    return {
        path.relative_to(root): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class CrashOnce:
    def __init__(self, point: FaultPoint) -> None:
        self.point = point
        self.triggered = False

    def hit(self, point: FaultPoint) -> None:
        if point is self.point and not self.triggered:
            self.triggered = True
            raise RuntimeError(f"injected crash at {point.value}")


class InterruptOnce:
    def __init__(self, point: FaultPoint) -> None:
        self.point = point

    def hit(self, point: FaultPoint) -> None:
        if point is self.point:
            raise KeyboardInterrupt(f"injected interrupt at {point.value}")


@dataclass(slots=True)
class MutableClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value


@dataclass(slots=True)
class ProposalScenario:
    container: Container
    settings: Settings
    clock: MutableClock
    source: Path
    source_before: dict[Path, bytes]
    project_id: str
    proposal: ChangeProposal
    batch_bytes: bytes


def _proposal_scenario(tmp_path: Path, faults: FaultInjector) -> ProposalScenario:
    fixtures = Path(__file__).resolve().parents[1] / "fixtures"
    settings = Settings.from_env(
        {
            "PCBFLOW_DATA_DIR": str(tmp_path / "fault-data"),
            "PCBFLOW_MODULE_CATALOG_DIR": str(fixtures / "modules"),
        }
    )
    source = tmp_path / "fault-source"
    shutil.copytree(fixtures / "kicad" / "controlled-design", source)
    source_before = _snapshot(source)
    clock = MutableClock(NOW)
    container = build_container(
        settings,
        kicad_override=FakeKicad9(),
        clock=clock,
        faults=faults,
    )
    project = container.projects.create("Controller", source, "fault-project")
    managed = container.revisions.adopt(project.id, "fault-adopt")
    payload = (fixtures / "requirements" / "reference-controller.yaml").read_bytes()
    draft = container.requirements.import_draft(
        managed.id, payload, "fault-requirements"
    )
    pending = container.requirements.submit(draft.id, "fault-submit")
    frozen = container.approvals.decide_g1(
        requirement_set_id=pending.id,
        subject_digest=pending.subject_digest(),
        decision="approve",
        actor_type="human",
        actor_id="local-user",
        comment="approved",
        idempotency_key="fault-g1",
    )
    current = container.projects.get(managed.id)
    batch_bytes = json.dumps(
        _command_batch(jsonable_encoder(current), jsonable_encoder(frozen)),
        separators=(",", ":"),
    ).encode()
    proposal = container.proposals.create(batch_bytes, "api-proposal")
    return ProposalScenario(
        container=container,
        settings=settings,
        clock=clock,
        source=source,
        source_before=source_before,
        project_id=managed.id,
        proposal=proposal,
        batch_bytes=batch_bytes,
    )


def test_restart_after_accept_keeps_database_git_evidence_and_validation_consistent(
    tmp_path: Path,
) -> None:
    fixtures = Path(__file__).resolve().parents[1] / "fixtures"
    settings = Settings.from_env(
        {
            "PCBFLOW_DATA_DIR": str(tmp_path / "restart-data"),
            "PCBFLOW_MODULE_CATALOG_DIR": str(fixtures / "modules"),
        }
    )
    source = tmp_path / "restart-source"
    shutil.copytree(fixtures / "kicad" / "controlled-design", source)
    source_before = _snapshot(source)
    first = build_container(settings, kicad_override=FakeKicad9())
    try:
        project = first.projects.create("Controller", source, "restart-project")
        managed = first.revisions.adopt(project.id, "restart-adopt")
        payload = (fixtures / "requirements" / "reference-controller.yaml").read_bytes()
        draft = first.requirements.import_draft(managed.id, payload, "restart-requirements")
        pending = first.requirements.submit(draft.id, "restart-submit")
        frozen = first.approvals.decide_g1(
            requirement_set_id=pending.id,
            subject_digest=pending.subject_digest(),
            decision="approve",
            actor_type="human",
            actor_id="local-user",
            comment="approved",
            idempotency_key="restart-g1",
        )
        current = first.projects.get(managed.id)
        batch = _command_batch(jsonable_encoder(current), jsonable_encoder(frozen))
        proposal = first.proposals.create(json.dumps(batch).encode(), "api-proposal")
        assert first.worker.run_once()
        ready = first.proposal_store.get(proposal.id)
        first.proposal_decisions.accept(
            proposal_id=ready.id,
            candidate_digest=ready.review_digest,
            actor_type="human",
            actor_id="local-user",
            comment="accepted",
            idempotency_key="restart-accept",
        )
        accepted_revision = first.projects.get(managed.id).current_revision
    finally:
        first.dispose()

    second = build_container(settings, kicad_override=FakeKicad9())
    try:
        assert second.reconciler.run_once() == 0
        project = second.projects.get(managed.id)
        assert project.current_revision == accepted_revision
        assert second.revisions.resolve_design_ref(project.id) == accepted_revision
        assert second.proposal_store.get(proposal.id).status.value == "accepted"
        assert all(
            second.artifacts.verify(item.artifact_digest)
            for item in second.evidence.list_for_project(project.id)
        )
        validation = second.validation.enqueue(project.id, "restart-read-only-validation")
        assert second.worker.run_once()
        assert second.tasks.get(validation.id).status.value == "succeeded"
        assert _snapshot(source) == source_before
    finally:
        second.dispose()


def test_reconciliation_accepts_frozen_requirement_ancestor_of_batch_base(
    tmp_path: Path,
) -> None:
    scenario = _proposal_scenario(tmp_path, faults=NoFaults())
    container = scenario.container
    try:
        assert container.worker.run_once()
        first_ready = container.proposal_store.get(scenario.proposal.id)
        container.proposal_decisions.accept(
            proposal_id=first_ready.id,
            candidate_digest=first_ready.review_digest,
            actor_type="human",
            actor_id="local-user",
            comment="first accepted change",
            idempotency_key="first-sequential-accept",
        )
        first_revision = container.projects.get(scenario.project_id).current_revision
        assert first_revision is not None

        second_batch = json.loads(scenario.batch_bytes)
        second_batch["batch_id"] = "bat_second_sequential_change"
        second_batch["base_revision"] = first_revision
        second_batch["idempotency_key"] = "second-sequential-change"
        command = second_batch["commands"][0]
        command["command_id"] = "cmd_second_sequential_change"
        command["batch_id"] = second_batch["batch_id"]
        command["base_revision"] = first_revision
        command["idempotency_key"] = "second-sequential-change:1"
        command["preconditions"] = []
        command["operation"] = {
            "type": "schematic.set_property",
            "payload": {
                "subject_ref": {
                    "kind": "symbol",
                    "sheet_uuid": "00000000-0000-0000-0000-000000000001",
                    "object_uuid": "00000000-0000-0000-0000-000000000002",
                    "pin_number": None,
                },
                "property_name": "Value",
                "value": "LED-SECOND",
                "expected_old_value": "\u72b6\u6001LED",
            },
        }
        second = container.proposals.create(
            json.dumps(second_batch, separators=(",", ":")).encode(),
            "second-sequential-change",
        )
        assert container.worker.run_once()
        second_ready = container.proposal_store.get(second.id)
        container.proposal_decisions.accept(
            proposal_id=second_ready.id,
            candidate_digest=second_ready.review_digest,
            actor_type="human",
            actor_id="local-user",
            comment="second accepted change",
            idempotency_key="second-sequential-accept",
        )
        second_revision = container.projects.get(scenario.project_id).current_revision
        assert second_revision is not None and second_revision != first_revision

        container.revisions.git.update_ref(
            container.revisions.repo_path(scenario.project_id),
            "refs/heads/design",
            first_revision,
            expected_revision=second_revision,
        )

        assert container.reconciler.run_once() == 1
        assert container.revisions.resolve_design_ref(scenario.project_id) == (
            second_revision
        )
    finally:
        container.dispose()


@pytest.mark.parametrize(
    "point",
    [
        FaultPoint.BEFORE_CANDIDATE_COMMIT,
        FaultPoint.AFTER_PROPOSAL_REF_BEFORE_DATABASE,
        FaultPoint.DURING_DIFF_ARTIFACT_SAVE,
        FaultPoint.AFTER_EXECUTION_BEFORE_FINAL_FENCE,
    ],
)
def test_crashed_proposal_execution_retries_without_duplicate_candidate(
    tmp_path: Path, point: FaultPoint
) -> None:
    scenario = _proposal_scenario(tmp_path, faults=CrashOnce(point))
    try:
        lease = scenario.container.tasks.claim_next("crashing-worker", NOW, 1)
        assert lease is not None
        scenario.container.tasks.start(lease.task_id, lease.lease_token, NOW)
        with pytest.raises(RuntimeError, match="injected crash"):
            scenario.container.proposal_executor(lease)

        after_expiry = lease.lease_expires_at + timedelta(seconds=1)
        scenario.clock.value = after_expiry
        assert scenario.container.worker.run_once()
        ready = scenario.container.proposal_store.get(scenario.proposal.id)
        assert ready.status.value == "ready_for_review"
        assert scenario.container.revisions.resolve_proposal_ref(
            ready.project_id, ready.id
        ) == ready.candidate_revision
        repeated = scenario.container.proposals.create(
            scenario.batch_bytes, "api-proposal"
        )
        assert repeated.id == ready.id
        assert _snapshot(scenario.source) == scenario.source_before
    finally:
        scenario.container.dispose()


def test_fault_context_is_restored_after_base_exception(tmp_path: Path) -> None:
    scenario = _proposal_scenario(
        tmp_path,
        faults=InterruptOnce(FaultPoint.DURING_DIFF_ARTIFACT_SAVE),
    )
    try:
        lease = scenario.container.tasks.claim_next("interrupt-worker", NOW, 1)
        assert lease is not None
        scenario.container.tasks.start(lease.task_id, lease.lease_token, NOW)

        with pytest.raises(KeyboardInterrupt, match="injected interrupt"):
            scenario.container.proposal_executor(lease)

        assert _fault_active.get() is False
    finally:
        scenario.container.dispose()


def test_acceptance_crash_is_repaired_without_duplicate_database_rows(
    tmp_path: Path,
) -> None:
    scenario = _proposal_scenario(
        tmp_path,
        faults=CrashOnce(FaultPoint.AFTER_ACCEPT_DATABASE_BEFORE_DESIGN_REF),
    )
    first = scenario.container
    try:
        base_revision = first.projects.get(scenario.project_id).current_revision
        assert first.worker.run_once()
        ready = first.proposal_store.get(scenario.proposal.id)
        with pytest.raises(RuntimeError, match="injected crash"):
            first.proposal_decisions.accept(
                proposal_id=ready.id,
                candidate_digest=ready.review_digest,
                actor_type="human",
                actor_id="local-user",
                comment="accepted before crash",
                idempotency_key="fault-accept",
            )
        accepted_revision = first.projects.get(scenario.project_id).current_revision
        assert accepted_revision == ready.candidate_revision
        assert first.revisions.resolve_design_ref(scenario.project_id) == base_revision
        with first.sessions() as session:
            decision_count = session.scalar(
                select(func.count()).select_from(GateDecisionRow)
            )
            revision_count = session.scalar(
                select(func.count()).select_from(ProjectRevisionRow)
            )
    finally:
        first.dispose()

    second = build_container(
        scenario.settings,
        kicad_override=FakeKicad9(),
        clock=scenario.clock,
        faults=NoFaults(),
    )
    try:
        assert second.reconciler.run_once() == 0
        assert second.revisions.resolve_design_ref(scenario.project_id) == (
            accepted_revision
        )
        with second.sessions() as session:
            assert session.scalar(
                select(func.count()).select_from(GateDecisionRow)
            ) == decision_count
            assert session.scalar(
                select(func.count()).select_from(ProjectRevisionRow)
            ) == revision_count
        assert _snapshot(scenario.source) == scenario.source_before
    finally:
        second.dispose()

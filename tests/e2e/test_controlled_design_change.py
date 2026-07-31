from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import httpx
import yaml

from pcbflow.api import create_app
from pcbflow.config import Settings
from pcbflow.container import build_container
from pcbflow.kicad import KicadCapability, RawValidationReport


PASSING_ERC = b'{"version":"1.0","source":"board.kicad_sch","violations":[]}'


class FakeKicad9:
    def probe(self) -> KicadCapability:
        return KicadCapability(
            True,
            Path("kicad-cli"),
            "9.0.2",
            "sha256:" + "9" * 64,
            None,
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

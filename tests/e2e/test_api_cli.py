from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import httpx
import yaml
from typer.testing import CliRunner

from pcbflow.api import create_app
from pcbflow.cli import app
from pcbflow.config import Settings
from pcbflow.container import build_container
from pcbflow.kicad import KicadCapability, RawValidationReport


class FakeKicad:
    def __init__(self, fixture_dir: Path) -> None:
        self._fixture_dir = fixture_dir

    def __bool__(self) -> bool:
        return False

    def validate(
        self, project_dir: Path, output_dir: Path
    ) -> tuple[RawValidationReport, ...]:
        assert project_dir.is_dir()
        assert not output_dir.resolve().is_relative_to(project_dir.resolve())
        output_dir.mkdir(parents=True, exist_ok=True)
        return (
            RawValidationReport(
                kind="erc",
                data=(self._fixture_dir / "erc.json").read_bytes(),
                argv=("kicad-cli", "sch", "erc"),
                returncode=0,
                tool_version="9.0.2",
                executable_digest="sha256:" + "9" * 64,
                profile_id="kicad-9-v1",
                profile_revision=1,
            ),
            RawValidationReport(
                kind="drc",
                data=(self._fixture_dir / "drc.json").read_bytes(),
                argv=("kicad-cli", "pcb", "drc"),
                returncode=0,
                tool_version="9.0.2",
                executable_digest="sha256:" + "9" * 64,
                profile_id="kicad-9-v1",
                profile_revision=1,
            ),
        )


def _settings(tmp_path: Path, *, remote_mode: bool = False) -> Settings:
    environ = {"PCBFLOW_DATA_DIR": str(tmp_path / "data")}
    if remote_mode:
        environ["PCBFLOW_REMOTE_MODE"] = "true"
    return Settings.from_env(environ)


def _fake_kicad() -> FakeKicad:
    fixtures = Path(__file__).resolve().parents[1] / "fixtures" / "kicad"
    return FakeKicad(fixtures)


def _client(
    container, *, raise_app_exceptions: bool = True
) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(
        app=create_app(container), raise_app_exceptions=raise_app_exceptions
    )
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


def test_api_registers_project_runs_worker_and_returns_results(
    tmp_path: Path,
) -> None:
    source = tmp_path / "project"
    source.mkdir()
    (source / "board.kicad_sch").write_text("schematic", encoding="utf-8")
    (source / "board.kicad_pcb").write_text("board", encoding="utf-8")
    original = {path.name: path.read_bytes() for path in source.iterdir()}
    container = build_container(_settings(tmp_path), kicad_override=_fake_kicad())

    async def exercise() -> None:
        async with _client(container) as client:
            assert (await client.get("/health")).json() == {"status": "ok"}
            request = {
                "headers": {"Idempotency-Key": "create-api-project"},
                "json": {"name": "Controller", "source_path": str(source)},
            }
            created = await client.post("/api/v1/projects", **request)
            assert created.status_code == 201
            project_id = created.json()["id"]

            repeated = await client.post("/api/v1/projects", **request)
            assert repeated.status_code == 200
            assert repeated.json()["id"] == project_id
            projects = (await client.get("/api/v1/projects")).json()
            assert [item["id"] for item in projects] == [project_id]

            queued = await client.post(
                f"/api/v1/projects/{project_id}/validations",
                headers={"Idempotency-Key": "validate-api-project-r1"},
            )
            assert queued.status_code == 202
            task_id = queued.json()["id"]

            worked = await client.post("/api/v1/worker:run-once")
            assert worked.json() == {"handled": True}
            task = (await client.get(f"/api/v1/tasks/{task_id}")).json()
            assert task["status"] == "succeeded"
            assert task["result"]["finding_count"] == 2

            evidence = (
                await client.get(f"/api/v1/projects/{project_id}/evidence")
            ).json()
            findings = (
                await client.get(f"/api/v1/projects/{project_id}/findings")
            ).json()
            assert [item["kind"] for item in evidence] == [
                "kicad_erc",
                "kicad_drc",
            ]
            assert [item["rule_id"] for item in findings] == [
                "KICAD.ERC.PIN_NOT_CONNECTED",
                "KICAD.DRC.CLEARANCE",
            ]

    asyncio.run(exercise())

    assert {path.name: path.read_bytes() for path in source.iterdir()} == original
    container.engine.dispose()


def test_api_rejects_unknown_fields_and_returns_stable_not_found_error(
    tmp_path: Path,
) -> None:
    container = build_container(_settings(tmp_path), kicad_override=_fake_kicad())

    async def exercise() -> None:
        async with _client(container) as client:
            invalid = await client.post(
                "/api/v1/projects",
                headers={"Idempotency-Key": "unknown-field"},
                json={
                    "name": "Controller",
                    "source_path": str(tmp_path),
                    "extra": True,
                },
            )
            assert invalid.status_code == 422

            missing = await client.get("/api/v1/tasks/tsk_missing")
            assert missing.status_code == 404
            error = missing.json()["error"]
            assert set(error) == {
                "code",
                "message",
                "retryable",
                "correlation_id",
                "details",
                "actions",
            }
            assert error["code"] == "TASK_NOT_FOUND"
            assert error["retryable"] is False
            assert error["correlation_id"]

    asyncio.run(exercise())

    container.engine.dispose()


def test_remote_api_rejects_local_source_paths(tmp_path: Path) -> None:
    container = build_container(
        _settings(tmp_path, remote_mode=True), kicad_override=_fake_kicad()
    )

    async def exercise() -> httpx.Response:
        async with _client(container) as client:
            return await client.post(
                "/api/v1/projects",
                headers={"Idempotency-Key": "remote-project"},
                json={"name": "Controller", "source_path": str(tmp_path)},
            )

    response = asyncio.run(exercise())
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "LOCAL_SOURCE_PATHS_DISABLED"
    container.engine.dispose()


def test_remote_api_disables_worker_execution_endpoint(tmp_path: Path) -> None:
    container = build_container(
        _settings(tmp_path, remote_mode=True), kicad_override=_fake_kicad()
    )

    async def exercise() -> httpx.Response:
        async with _client(container, raise_app_exceptions=False) as client:
            return await client.post("/api/v1/worker:run-once")

    response = asyncio.run(exercise())
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "REMOTE_WORKER_DISABLED"
    container.engine.dispose()


def test_api_returns_stable_error_for_invalid_project_source(tmp_path: Path) -> None:
    container = build_container(_settings(tmp_path), kicad_override=_fake_kicad())

    async def exercise() -> httpx.Response:
        async with _client(container, raise_app_exceptions=False) as client:
            return await client.post(
                "/api/v1/projects",
                headers={"Idempotency-Key": "missing-source"},
                json={
                    "name": "Controller",
                    "source_path": str(tmp_path / "missing"),
                },
            )

    response = asyncio.run(exercise())
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "PROJECT_SOURCE_INVALID"
    assert error["retryable"] is False
    container.engine.dispose()


def test_doctor_json_has_stable_shape(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["doctor", "--json"],
        env={"PCBFLOW_DATA_DIR": str(tmp_path / "cli-data")},
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert set(payload["kicad_cli"]) == {
        "available",
        "path",
        "version",
        "executable_digest",
        "reason",
        "major",
        "profile_id",
        "profile_revision",
    }


def test_cli_commands_share_persisted_services(tmp_path: Path) -> None:
    source = tmp_path / "cli-project"
    source.mkdir()
    (source / "board.kicad_sch").write_text("schematic", encoding="utf-8")
    cli_data = tmp_path / "cli-workflow-data"
    env = {
        "PCBFLOW_DATA_DIR": str(cli_data),
        "PCBFLOW_KICAD_CLI": str(tmp_path / "missing-kicad-cli.exe"),
    }
    runner = CliRunner()

    created = runner.invoke(
        app,
        [
            "project",
            "add",
            str(source),
            "--name",
            "Controller",
            "--idempotency-key",
            "cli-project-1",
            "--json",
        ],
        env=env,
    )
    assert created.exit_code == 0, created.output
    project_id = json.loads(created.stdout)["id"]

    listed = runner.invoke(app, ["project", "list", "--json"], env=env)
    assert listed.exit_code == 0, listed.output
    assert [item["id"] for item in json.loads(listed.stdout)] == [project_id]

    queued = runner.invoke(
        app,
        [
            "validate",
            project_id,
            "--idempotency-key",
            "cli-validation-1",
            "--json",
        ],
        env=env,
    )
    assert queued.exit_code == 0, queued.output
    task_id = json.loads(queued.stdout)["id"]

    worked = runner.invoke(app, ["worker", "--once", "--json"], env=env)
    assert worked.exit_code == 0, worked.output
    assert json.loads(worked.stdout) == {"handled": True}

    shown = runner.invoke(app, ["task", "show", task_id, "--json"], env=env)
    assert shown.exit_code == 0, shown.output
    task = json.loads(shown.stdout)
    assert task["status"] == "failed_terminal"
    assert task["last_error_code"] == "KICAD_CLI_UNAVAILABLE"

    findings = runner.invoke(app, ["findings", project_id, "--json"], env=env)
    assert findings.exit_code == 0, findings.output
    assert json.loads(findings.stdout) == []

    evidence = runner.invoke(app, ["evidence", project_id, "--json"], env=env)
    assert evidence.exit_code == 0, evidence.output
    assert json.loads(evidence.stdout) == []


class FakeCliKicad:
    def probe(self) -> KicadCapability:
        return KicadCapability(
            True,
            Path("kicad-cli"),
            "9.0.2",
            "sha256:" + "9" * 64,
            None,
        )

    def validate(self, project_dir: Path, output_dir: Path):
        output_dir.mkdir(parents=True, exist_ok=True)
        return (
            RawValidationReport(
                kind="erc",
                data=b'{"version":"1.0","source":"board.kicad_sch","violations":[]}',
                argv=("kicad-cli", "sch", "erc"),
                returncode=0,
                tool_version="9.0.2",
                executable_digest="sha256:" + "9" * 64,
                profile_id="kicad-9-v1",
                profile_revision=1,
            ),
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


def test_phase_2a_cli_help_lists_all_command_groups() -> None:
    runner = CliRunner()
    root = runner.invoke(app, ["--help"])
    assert root.exit_code == 0
    for name in ("project", "requirements", "approval", "proposal", "worker"):
        assert name in root.output

    assert "adopt" in runner.invoke(app, ["project", "--help"]).output
    assert "import" in runner.invoke(app, ["requirements", "--help"]).output
    assert "decide" in runner.invoke(app, ["approval", "--help"]).output
    proposal_help = runner.invoke(app, ["proposal", "--help"]).output
    for name in ("create", "show", "diff", "accept", "reject"):
        assert name in proposal_help


def test_cli_runs_the_controlled_change_workflow(
    tmp_path: Path, monkeypatch
) -> None:
    fixtures = Path(__file__).resolve().parents[1] / "fixtures"
    source = tmp_path / "cli controlled source"
    shutil.copytree(fixtures / "kicad" / "controlled-design", source)
    data_dir = tmp_path / "cli-controlled-data"
    env = {
        "PCBFLOW_DATA_DIR": str(data_dir),
        "PCBFLOW_MODULE_CATALOG_DIR": str(fixtures / "modules"),
    }

    def build_for_test():
        return build_container(
            Settings.from_env(env),
            kicad_override=FakeCliKicad(),
        )

    monkeypatch.setattr("pcbflow.cli._build", build_for_test)
    runner = CliRunner()
    created = runner.invoke(
        app,
        [
            "project", "add", str(source),
            "--name", "Controller",
            "--idempotency-key", "cli-controlled-project",
            "--json",
        ],
        env=env,
    )
    assert created.exit_code == 0, created.output
    project = json.loads(created.stdout)

    adopted = runner.invoke(
        app,
        [
            "project", "adopt", project["id"],
            "--idempotency-key", "cli-adopt",
            "--json",
        ],
        env=env,
    )
    assert adopted.exit_code == 0, adopted.output

    requirements = runner.invoke(
        app,
        [
            "requirements", "import", project["id"],
            "--file", str(fixtures / "requirements" / "reference-controller.yaml"),
            "--idempotency-key", "cli-requirements",
            "--json",
        ],
        env=env,
    )
    assert requirements.exit_code == 0, requirements.output
    requirement_set = json.loads(requirements.stdout)
    assert requirement_set["subject_digest"] is None
    submitted = runner.invoke(
        app,
        [
            "requirements", "submit", requirement_set["id"],
            "--idempotency-key", "cli-submit-requirements",
            "--json",
        ],
        env=env,
    )
    assert submitted.exit_code == 0, submitted.output
    pending = json.loads(submitted.stdout)
    assert pending["subject_digest"].startswith("sha256:")
    approved = runner.invoke(
        app,
        [
            "approval", "decide", pending["id"],
            "--subject-digest", pending["subject_digest"],
            "--approve",
            "--actor-id", "local-user",
            "--comment", "approved",
            "--idempotency-key", "cli-approve-g1",
            "--json",
        ],
        env=env,
    )
    assert approved.exit_code == 0, approved.output
    frozen = json.loads(approved.stdout)
    assert frozen["subject_digest"] == pending["subject_digest"]

    managed = json.loads(
        runner.invoke(app, ["project", "list", "--json"], env=env).stdout
    )[0]
    batch = _command_batch(managed, frozen)
    batch_file = tmp_path / "commands.json"
    batch_file.write_text(json.dumps(batch), encoding="utf-8")
    queued = runner.invoke(
        app,
        [
            "proposal", "create", managed["id"],
            "--file", str(batch_file),
            "--idempotency-key", "api-proposal",
            "--json",
        ],
        env=env,
    )
    assert queued.exit_code == 0, queued.output
    proposal = json.loads(queued.stdout)
    worked = runner.invoke(app, ["worker", "--once", "--json"], env=env)
    assert worked.exit_code == 0, worked.output
    assert json.loads(worked.stdout) == {"handled": True}
    shown = runner.invoke(
        app, ["proposal", "show", proposal["id"], "--json"], env=env
    )
    assert shown.exit_code == 0, shown.output
    shown_payload = json.loads(shown.stdout)
    assert shown_payload["status"] == "ready_for_review"
    diff = runner.invoke(
        app, ["proposal", "diff", proposal["id"], "--json"], env=env
    )
    assert diff.exit_code == 0, diff.output
    assert json.loads(diff.stdout)["changes"]
    accepted = runner.invoke(
        app,
        [
            "proposal", "accept", proposal["id"],
            "--candidate-digest", shown_payload["review_digest"],
            "--actor-id", "local-user",
            "--comment", "accepted",
            "--idempotency-key", "cli-accept-proposal",
            "--json",
        ],
        env=env,
    )
    assert accepted.exit_code == 0, accepted.output
    assert json.loads(accepted.stdout)["status"] == "accepted"

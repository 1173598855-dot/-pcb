from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
from typer.testing import CliRunner

from pcbflow.api import create_app
from pcbflow.cli import app
from pcbflow.config import Settings
from pcbflow.container import build_container
from pcbflow.kicad import RawValidationReport


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
            ),
            RawValidationReport(
                kind="drc",
                data=(self._fixture_dir / "drc.json").read_bytes(),
                argv=("kicad-cli", "pcb", "drc"),
                returncode=0,
                tool_version="9.0.2",
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

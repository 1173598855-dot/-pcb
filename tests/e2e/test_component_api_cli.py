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
from tests.component_fixtures import build_component_directory


def _settings(
    tmp_path: Path, *, remote_mode: bool = False, api_token: str | None = None
) -> Settings:
    environ = {"PCBFLOW_DATA_DIR": str(tmp_path / "component-api-data")}
    if remote_mode:
        environ["PCBFLOW_REMOTE_MODE"] = "true"
    if api_token is not None:
        environ["PCBFLOW_API_TOKEN"] = api_token
    return Settings.from_env(environ)


def test_component_revision_api_import_show_list_and_replay(tmp_path: Path) -> None:
    container = build_container(_settings(tmp_path))
    manifest_path = build_component_directory(tmp_path / "api-component") / "component.yaml"

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=create_app(container))
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            request = {
                "json": {"manifest_path": str(manifest_path)},
                "headers": {"Idempotency-Key": "api-component-1"},
            }
            created = await client.post("/api/v1/component-revisions", **request)
            assert created.status_code == 201, created.text
            revision = created.json()
            assert "manifest_path" not in revision
            assert "storage_path" not in revision

            replayed = await client.post("/api/v1/component-revisions", **request)
            assert replayed.status_code == 200
            assert replayed.json() == revision

            shown = await client.get(
                f"/api/v1/component-revisions/{revision['id']}"
            )
            assert shown.status_code == 200
            assert shown.json() == revision

            listed = await client.get(
                "/api/v1/component-revisions",
                params={"component_key": "Acme:LED-0603-RED"},
            )
            assert listed.status_code == 200
            assert listed.json() == [revision]

            missing = await client.get("/api/v1/component-revisions/missing")
            assert missing.status_code == 404
            assert (
                missing.json()["error"]["code"]
                == "COMPONENT_REVISION_NOT_FOUND"
            )

    try:
        asyncio.run(exercise())
    finally:
        container.dispose()


def test_remote_component_api_rejects_import_but_allows_reads(tmp_path: Path) -> None:
    token = "remote-component-token"
    container = build_container(
        _settings(tmp_path, remote_mode=True, api_token=token)
    )
    manifest_path = build_component_directory(tmp_path / "remote-component") / "component.yaml"
    revision = container.components.import_revision(manifest_path, "remote-seed-1")

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=create_app(container))
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            authorization = {"Authorization": f"Bearer {token}"}
            rejected = await client.post(
                "/api/v1/component-revisions",
                json={"manifest_path": str(manifest_path)},
                headers={
                    **authorization,
                    "Idempotency-Key": "remote-api-component-1",
                },
            )
            assert rejected.status_code == 403
            assert rejected.json()["error"]["code"] == "LOCAL_SOURCE_PATHS_DISABLED"

            shown = await client.get(
                f"/api/v1/component-revisions/{revision.id}", headers=authorization
            )
            assert shown.status_code == 200
            listed = await client.get(
                "/api/v1/component-revisions",
                params={"component_key": revision.component_key},
                headers=authorization,
            )
            assert listed.status_code == 200
            assert [item["id"] for item in listed.json()] == [revision.id]

    try:
        asyncio.run(exercise())
    finally:
        container.dispose()


def test_component_commands_return_stable_json_and_missing_code(
    tmp_path: Path,
) -> None:
    manifest_path = build_component_directory(tmp_path / "cli-component") / "component.yaml"
    env = {"PCBFLOW_DATA_DIR": str(tmp_path / "component-cli-data")}
    runner = CliRunner()

    created = runner.invoke(
        app,
        [
            "component",
            "import",
            str(manifest_path),
            "--idempotency-key",
            "cli-component-1",
            "--json",
        ],
        env=env,
    )
    assert created.exit_code == 0, created.output
    revision = json.loads(created.stdout)
    assert "manifest_path" not in revision

    replayed = runner.invoke(
        app,
        [
            "component",
            "import",
            str(manifest_path),
            "--idempotency-key",
            "cli-component-1",
            "--json",
        ],
        env=env,
    )
    assert replayed.exit_code == 0, replayed.output
    assert json.loads(replayed.stdout)["id"] == revision["id"]

    shown = runner.invoke(
        app, ["component", "show", revision["id"], "--json"], env=env
    )
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.stdout) == revision

    listed = runner.invoke(
        app,
        [
            "component",
            "list",
            "--component-key",
            "Acme:LED-0603-RED",
            "--json",
        ],
        env=env,
    )
    assert listed.exit_code == 0, listed.output
    assert json.loads(listed.stdout) == [revision]

    missing = runner.invoke(
        app, ["component", "show", "missing", "--json"], env=env
    )
    assert missing.exit_code == 2
    assert "COMPONENT_REVISION_NOT_FOUND" in missing.output

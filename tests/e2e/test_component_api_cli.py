from __future__ import annotations

import asyncio
import json
from pathlib import Path
import shutil

import httpx
from typer.testing import CliRunner

from pcbflow.api import create_app
from pcbflow.cli import app
from pcbflow.config import Settings
from pcbflow.container import build_container
from tests.component_fixtures import build_component_directory


def _settings(
    tmp_path: Path,
    *,
    remote_mode: bool = False,
    api_token: str | None = None,
    module_catalog_dir: Path | None = None,
) -> Settings:
    environ = {"PCBFLOW_DATA_DIR": str(tmp_path / "component-api-data")}
    if remote_mode:
        environ["PCBFLOW_REMOTE_MODE"] = "true"
    if api_token is not None:
        environ["PCBFLOW_API_TOKEN"] = api_token
    if module_catalog_dir is not None:
        environ["PCBFLOW_MODULE_CATALOG_DIR"] = str(module_catalog_dir)
    return Settings.from_env(environ)


def _module_fixture_root() -> Path:
    return Path(__file__).parents[1] / "fixtures" / "modules"


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


def test_component_module_binding_api_creates_replays_and_lists(
    tmp_path: Path,
) -> None:
    container = build_container(
        _settings(tmp_path, module_catalog_dir=_module_fixture_root())
    )
    manifest_path = (
        build_component_directory(tmp_path / "binding-api-component") / "component.yaml"
    )

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=create_app(container))
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            imported = await client.post(
                "/api/v1/component-revisions",
                json={"manifest_path": str(manifest_path)},
                headers={"Idempotency-Key": "binding-api-import"},
            )
            assert imported.status_code == 201, imported.text
            revision = imported.json()
            binding_url = (
                f"/api/v1/component-revisions/{revision['id']}/module-bindings"
            )
            request = {
                "json": {
                    "kicad_major": 10,
                    "module_revision_id": "modrev_status_led_v1",
                },
                "headers": {"Idempotency-Key": "binding-api-create"},
            }

            created = await client.post(binding_url, **request)
            assert created.status_code == 201, created.text
            binding = created.json()
            assert {
                "id",
                "component_revision_id",
                "kicad_major",
                "module_revision_id",
                "module_manifest_digest",
                "idempotency_key",
                "created_at",
            } <= binding.keys()

            replayed = await client.post(binding_url, **request)
            assert replayed.status_code == 200
            assert replayed.json() == binding

            listed = await client.get(binding_url)
            assert listed.status_code == 200
            assert listed.json() == [binding]

            strict = await client.post(
                binding_url,
                json={
                    "kicad_major": 10,
                    "module_revision_id": "modrev_status_led_v1",
                    "unexpected": True,
                },
                headers={"Idempotency-Key": "binding-api-strict"},
            )
            assert strict.status_code == 422
            assert strict.json()["error"]["code"] == "REQUEST_SCHEMA_INVALID"

    try:
        asyncio.run(exercise())
    finally:
        container.dispose()


def test_component_module_binding_api_reports_domain_errors(tmp_path: Path) -> None:
    container = build_container(
        _settings(tmp_path, module_catalog_dir=_module_fixture_root())
    )
    revision = container.components.import_revision(
        build_component_directory(tmp_path / "binding-api-errors") / "component.yaml",
        "binding-api-errors-import",
    )

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=create_app(container))
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            missing_component = await client.post(
                "/api/v1/component-revisions/missing/module-bindings",
                json={
                    "kicad_major": 10,
                    "module_revision_id": "modrev_status_led_v1",
                },
                headers={"Idempotency-Key": "binding-api-missing-component"},
            )
            assert missing_component.status_code == 404
            assert (
                missing_component.json()["error"]["code"]
                == "COMPONENT_REVISION_NOT_FOUND"
            )

            missing_module = await client.post(
                f"/api/v1/component-revisions/{revision.id}/module-bindings",
                json={"kicad_major": 10, "module_revision_id": "modrev_missing"},
                headers={"Idempotency-Key": "binding-api-missing-module"},
            )
            assert missing_module.status_code == 404
            assert (
                missing_module.json()["error"]["code"]
                == "MODULE_REVISION_NOT_FOUND"
            )

            unsupported_major = await client.post(
                f"/api/v1/component-revisions/{revision.id}/module-bindings",
                json={
                    "kicad_major": 11,
                    "module_revision_id": "modrev_status_led_v1",
                },
                headers={"Idempotency-Key": "binding-api-unsupported-major"},
            )
            assert unsupported_major.status_code == 422
            assert (
                unsupported_major.json()["error"]["code"]
                == "MODULE_KICAD_MAJOR_UNSUPPORTED"
            )

    try:
        asyncio.run(exercise())
    finally:
        container.dispose()


def test_component_module_binding_api_rejects_an_unconfigured_catalog(
    tmp_path: Path,
) -> None:
    container = build_container(_settings(tmp_path))
    revision = container.components.import_revision(
        build_component_directory(tmp_path / "binding-api-no-catalog") / "component.yaml",
        "binding-api-no-catalog-import",
    )

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=create_app(container))
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            unavailable = await client.post(
                f"/api/v1/component-revisions/{revision.id}/module-bindings",
                json={
                    "kicad_major": 10,
                    "module_revision_id": "modrev_status_led_v1",
                },
                headers={"Idempotency-Key": "binding-api-no-catalog"},
            )
            assert unavailable.status_code == 409
            assert (
                unavailable.json()["error"]["code"]
                == "MODULE_CATALOG_UNAVAILABLE"
            )

    try:
        asyncio.run(exercise())
    finally:
        container.dispose()


def test_component_module_binding_api_rejects_a_conflicting_slot(
    tmp_path: Path,
) -> None:
    catalog_root = tmp_path / "modules"
    shutil.copytree(_module_fixture_root(), catalog_root)
    alternate = catalog_root / "status-led-alt"
    shutil.copytree(catalog_root / "status-led-v1", alternate)
    manifest = alternate / "module.yaml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "module_revision_id: modrev_status_led_v1",
            "module_revision_id: modrev_status_led_alt_v1",
        ),
        encoding="utf-8",
    )

    container = build_container(_settings(tmp_path, module_catalog_dir=catalog_root))
    revision = container.components.import_revision(
        build_component_directory(tmp_path / "binding-api-conflict") / "component.yaml",
        "binding-api-conflict-import",
    )

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=create_app(container))
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            binding_url = (
                f"/api/v1/component-revisions/{revision.id}/module-bindings"
            )
            first = await client.post(
                binding_url,
                json={
                    "kicad_major": 10,
                    "module_revision_id": "modrev_status_led_v1",
                },
                headers={"Idempotency-Key": "binding-api-slot-first"},
            )
            assert first.status_code == 201, first.text

            conflict = await client.post(
                binding_url,
                json={
                    "kicad_major": 10,
                    "module_revision_id": "modrev_status_led_alt_v1",
                },
                headers={"Idempotency-Key": "binding-api-slot-conflict"},
            )
            assert conflict.status_code == 409
            assert (
                conflict.json()["error"]["code"]
                == "COMPONENT_MODULE_BINDING_CONFLICT"
            )

    try:
        asyncio.run(exercise())
    finally:
        container.dispose()


def test_remote_component_api_rejects_import_but_allows_reads_and_bindings(
    tmp_path: Path,
) -> None:
    token = "remote-component-token"
    container = build_container(
        _settings(
            tmp_path,
            remote_mode=True,
            api_token=token,
            module_catalog_dir=_module_fixture_root(),
        )
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

            binding = await client.post(
                f"/api/v1/component-revisions/{revision.id}/module-bindings",
                json={
                    "kicad_major": 10,
                    "module_revision_id": "modrev_status_led_v1",
                },
                headers={
                    **authorization,
                    "Idempotency-Key": "remote-api-component-binding-1",
                },
            )
            assert binding.status_code == 201, binding.text

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

    binding_env = {
        **env,
        "PCBFLOW_MODULE_CATALOG_DIR": str(_module_fixture_root()),
    }
    bound = runner.invoke(
        app,
        [
            "component",
            "bind-module",
            revision["id"],
            "--kicad-major",
            "10",
            "--module-revision-id",
            "modrev_status_led_v1",
            "--idempotency-key",
            "cli-binding-1",
            "--json",
        ],
        env=binding_env,
    )
    assert bound.exit_code == 0, bound.output
    binding = json.loads(bound.stdout)

    bindings = runner.invoke(
        app,
        ["component", "bindings", revision["id"], "--json"],
        env=binding_env,
    )
    assert bindings.exit_code == 0, bindings.output
    assert json.loads(bindings.stdout) == [binding]

    unsupported_major = runner.invoke(
        app,
        [
            "component",
            "bind-module",
            revision["id"],
            "--kicad-major",
            "11",
            "--module-revision-id",
            "modrev_status_led_v1",
            "--idempotency-key",
            "cli-binding-unsupported-major",
            "--json",
        ],
        env=binding_env,
    )
    assert unsupported_major.exit_code == 2
    assert "MODULE_KICAD_MAJOR_UNSUPPORTED" in unsupported_major.output

    missing = runner.invoke(
        app, ["component", "show", "missing", "--json"], env=env
    )
    assert missing.exit_code == 2
    assert "COMPONENT_REVISION_NOT_FOUND" in missing.output

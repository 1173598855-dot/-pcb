from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import httpx
from tests.component_fixtures import build_component_directory
from typer.testing import CliRunner

import pcbflow.api as api_module
from pcbflow.api import RequestBodyLimitMiddleware, create_app
from pcbflow.cli import app
from pcbflow.config import Settings
from pcbflow.container import build_container
from pcbflow.domain import EdaKind
from pcbflow.eda import ProjectEdaAuthorityInput
from pcbflow.kicad import KicadCapability, RawValidationReport
from pcbflow.process import ProcessResult


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


def _settings(
    tmp_path: Path,
    *,
    remote_mode: bool = False,
    api_token: str | None = None,
    max_api_body_bytes: int | None = None,
) -> Settings:
    environ = {"PCBFLOW_DATA_DIR": str(tmp_path / "data")}
    if remote_mode:
        environ["PCBFLOW_REMOTE_MODE"] = "true"
    if api_token is not None:
        environ["PCBFLOW_API_TOKEN"] = api_token
    if max_api_body_bytes is not None:
        environ["PCBFLOW_MAX_API_BODY_BYTES"] = str(max_api_body_bytes)
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


def test_api_replays_pcb_candidate_from_frozen_inputs_when_capability_evidence_changes(
    tmp_path: Path,
) -> None:
    source = tmp_path / "candidate-replay-project"
    source.mkdir()
    settings = _settings(tmp_path)
    container = build_container(settings, kicad_override=_fake_kicad())
    project = container.projects.create("LCEDA", source, "candidate-replay-project")
    container.eda_authorities.configure(
        project.id,
        ProjectEdaAuthorityInput(
            eda_kind=EdaKind.LCEDA_PRO,
            eda_profile_id="lceda-pro-v1",
            board_profile_id="stm32-environment-controller-2l-v1",
            rulepack_digest="sha256:" + "a" * 64,
        ),
        "candidate-replay-authority",
    )
    managed = container.revisions.adopt(project.id, "candidate-replay-adopt")
    candidate = container.pcb_candidates.create(
        project_id=managed.id,
        base_revision=managed.current_revision or "",
        base_snapshot_digest=managed.project_snapshot_digest,
        board_snapshot_digest=managed.project_snapshot_digest or "sha256:" + "b" * 64,
        rulepack_digest="sha256:" + "a" * 64,
        capability_digest="sha256:" + "c" * 64,
        operations=(),
        algorithm_evidence={
            "algorithm_version": "boardir-only-v1",
            "seed": 7,
            "net_ids": ["I2C_SCL"],
            "output_kind": "boardir_only",
        },
        idempotency_key="candidate-replay-api",
        require_capability=False,
    )
    current = container.projects.get(managed.id)
    container.projects.compare_and_set_revision(
        managed.id,
        expected_revision=current.current_revision or "",
        new_revision="git:" + "d" * 40,
        snapshot_digest="sha256:" + "e" * 64,
        expected_version=current.version,
    )

    async def exercise() -> httpx.Response:
        async with _client(container) as client:
            return await client.post(
                f"/api/v1/projects/{managed.id}/pcb-candidates",
                headers={"Idempotency-Key": "candidate-replay-api"},
                json={"seed": 7, "net_ids": ["I2C_SCL"]},
            )

    replayed = asyncio.run(exercise())
    assert replayed.status_code == 202, replayed.text
    assert replayed.json()["id"] == candidate.id
    container.engine.dispose()


def test_api_and_cli_reject_explicit_pcb_candidate_input_changes(
    tmp_path: Path, monkeypatch
) -> None:
    """公共入口必须拒绝同一幂等键下的 BoardIR、能力和随机种子变更。"""
    source = tmp_path / "candidate-conflict-project"
    source.mkdir()
    settings = _settings(tmp_path)
    container = build_container(settings, kicad_override=_fake_kicad())
    project = container.projects.create("LCEDA", source, "candidate-conflict-project")
    container.eda_authorities.configure(
        project.id,
        ProjectEdaAuthorityInput(
            eda_kind=EdaKind.LCEDA_PRO,
            eda_profile_id="lceda-pro-v1",
            board_profile_id="stm32-environment-controller-2l-v1",
            rulepack_digest="sha256:" + "a" * 64,
        ),
        "candidate-conflict-authority",
    )
    managed = container.revisions.adopt(project.id, "candidate-conflict-adopt")
    board_digest = managed.project_snapshot_digest or "sha256:" + "b" * 64
    capability_digest = "sha256:" + "c" * 64
    container.pcb_candidates.create(
        project_id=managed.id,
        base_revision=managed.current_revision or "",
        base_snapshot_digest=managed.project_snapshot_digest,
        board_snapshot_digest=board_digest,
        rulepack_digest="sha256:" + "a" * 64,
        capability_digest=capability_digest,
        operations=(),
        algorithm_evidence={
            "algorithm_version": "boardir-only-v1",
            "seed": 7,
            "net_ids": ["I2C_SCL"],
            "output_kind": "boardir_only",
        },
        idempotency_key="candidate-conflict",
        require_capability=False,
    )
    changed_inputs = (
        {
            "board_snapshot_digest": "sha256:" + "d" * 64,
            "capability_digest": capability_digest,
            "seed": 7,
        },
        {
            "board_snapshot_digest": board_digest,
            "capability_digest": "sha256:" + "e" * 64,
            "seed": 7,
        },
        {
            "board_snapshot_digest": board_digest,
            "capability_digest": capability_digest,
            "seed": 8,
        },
    )

    async def exercise_api() -> list[httpx.Response]:
        async with _client(container) as client:
            return [
                await client.post(
                    f"/api/v1/projects/{managed.id}/pcb-candidates",
                    headers={"Idempotency-Key": "candidate-conflict"},
                    json={**changed, "net_ids": ["I2C_SCL"]},
                )
                for changed in changed_inputs
            ]

    api_responses = asyncio.run(exercise_api())
    assert all(response.status_code == 409 for response in api_responses)
    assert [response.json()["error"]["code"] for response in api_responses] == [
        "IDEMPOTENCY_CONFLICT",
        "IDEMPOTENCY_CONFLICT",
        "IDEMPOTENCY_CONFLICT",
    ]

    monkeypatch.setattr(
        "pcbflow.cli._build",
        lambda: build_container(settings, kicad_override=_fake_kicad()),
    )
    runner = CliRunner()
    for changed in changed_inputs:
        result = runner.invoke(
            app,
            [
                "pcb",
                "candidate",
                "create",
                managed.id,
                "--seed",
                str(changed["seed"]),
                "--net-id",
                "I2C_SCL",
                "--board-snapshot-digest",
                changed["board_snapshot_digest"],
                "--capability-digest",
                changed["capability_digest"],
                "--idempotency-key",
                "candidate-conflict",
                "--json",
            ],
        )
        assert result.exit_code == 2
        assert "IDEMPOTENCY_CONFLICT" in result.output
    container.dispose()


def test_api_and_cli_map_unmanaged_candidate_to_the_same_stale_error(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "unmanaged-candidate-project"
    source.mkdir()
    settings = _settings(tmp_path)
    container = build_container(settings, kicad_override=_fake_kicad())
    project = container.projects.create("LCEDA", source, "unmanaged-candidate-project")
    container.eda_authorities.configure(
        project.id,
        ProjectEdaAuthorityInput(
            eda_kind=EdaKind.LCEDA_PRO,
            eda_profile_id="lceda-pro-v1",
            board_profile_id="stm32-environment-controller-2l-v1",
            rulepack_digest="sha256:" + "a" * 64,
        ),
        "unmanaged-candidate-authority",
    )

    async def exercise_api() -> httpx.Response:
        async with _client(container) as client:
            return await client.post(
                f"/api/v1/projects/{project.id}/pcb-candidates",
                headers={"Idempotency-Key": "unmanaged-candidate-api"},
                json={"seed": 7},
            )

    api_response = asyncio.run(exercise_api())
    assert api_response.status_code == 409, api_response.text
    assert api_response.json()["error"]["code"] == "PCB_CANDIDATE_STALE"

    monkeypatch.setattr(
        "pcbflow.cli._build",
        lambda: build_container(settings, kicad_override=_fake_kicad()),
    )
    cli_result = CliRunner().invoke(
        app,
        [
            "pcb",
            "candidate",
            "create",
            project.id,
            "--seed",
            "7",
            "--idempotency-key",
            "unmanaged-candidate-cli",
            "--json",
        ],
    )
    assert cli_result.exit_code == 2
    assert "PCB_CANDIDATE_STALE" in cli_result.output
    container.dispose()


def test_api_and_cli_map_missing_pcb_candidate_to_stable_error(
    tmp_path: Path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    container = build_container(settings, kicad_override=_fake_kicad())

    async def exercise() -> httpx.Response:
        async with _client(container) as client:
            return await client.get("/api/v1/pcb-candidates/pcb_missing")

    missing = asyncio.run(exercise())
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "PCB_CANDIDATE_NOT_FOUND"

    monkeypatch.setattr(
        "pcbflow.cli._build",
        lambda: build_container(settings, kicad_override=_fake_kicad()),
    )
    cli_missing = CliRunner().invoke(
        app, ["pcb", "candidate", "show", "pcb_missing", "--json"]
    )
    assert cli_missing.exit_code == 2
    assert "PCB_CANDIDATE_NOT_FOUND" in cli_missing.output
    container.dispose()


def test_api_and_cli_expose_strict_release_export_and_g4_routes(
    tmp_path: Path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    container = build_container(settings, kicad_override=_fake_kicad())

    async def exercise() -> tuple[httpx.Response, httpx.Response, httpx.Response]:
        async with _client(container) as client:
            unknown = await client.post(
                "/api/v1/pcb-candidates/pcb_missing:export-release",
                headers={"Idempotency-Key": "release-api-unknown"},
                json={"unexpected": True},
            )
            missing_export = await client.post(
                "/api/v1/pcb-candidates/pcb_missing:export-release",
                headers={"Idempotency-Key": "release-api-missing"},
                json={},
            )
            missing_g4 = await client.post(
                "/api/v1/pcb-candidates/pcb_missing:approve-g4",
                headers={"Idempotency-Key": "g4-api-missing"},
                json={
                    "manifest_digest": "sha256:" + "a" * 64,
                    "decision": "approve",
                    "actor": {"type": "human", "id": "local-user"},
                    "comment": "release",
                },
            )
            return unknown, missing_export, missing_g4

    unknown, missing_export, missing_g4 = asyncio.run(exercise())
    assert unknown.status_code == 422
    assert unknown.json()["error"]["code"] == "REQUEST_SCHEMA_INVALID"
    assert missing_export.status_code == 404
    assert missing_export.json()["error"]["code"] == "PCB_CANDIDATE_NOT_FOUND"
    assert missing_g4.status_code == 404
    assert missing_g4.json()["error"]["code"] == "PCB_CANDIDATE_NOT_FOUND"

    monkeypatch.setattr(
        "pcbflow.cli._build",
        lambda: build_container(settings, kicad_override=_fake_kicad()),
    )
    cli_export = CliRunner().invoke(
        app,
        [
            "pcb",
            "release",
            "export",
            "pcb_missing",
            "--idempotency-key",
            "release-cli-missing",
            "--json",
        ],
    )
    cli_g4 = CliRunner().invoke(
        app,
        [
            "pcb",
            "release",
            "approve-g4",
            "pcb_missing",
            "--manifest-digest",
            "sha256:" + "a" * 64,
            "--idempotency-key",
            "g4-cli-missing",
            "--actor-id",
            "local-user",
            "--comment",
            "release",
            "--approve",
            "--json",
        ],
    )
    assert cli_export.exit_code == 2
    assert "PCB_CANDIDATE_NOT_FOUND" in cli_export.output
    assert cli_g4.exit_code == 2
    assert "PCB_CANDIDATE_NOT_FOUND" in cli_g4.output
    container.dispose()


def test_api_and_cli_show_candidates_as_boardir_only_without_native_evidence(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "candidate-output-kind-project"
    source.mkdir()
    settings = _settings(tmp_path)
    container = build_container(settings, kicad_override=_fake_kicad())
    project = container.projects.create("LCEDA", source, "candidate-output-kind-project")
    container.eda_authorities.configure(
        project.id,
        ProjectEdaAuthorityInput(
            eda_kind=EdaKind.LCEDA_PRO,
            eda_profile_id="lceda-pro-v1",
            board_profile_id="stm32-environment-controller-2l-v1",
            rulepack_digest="sha256:" + "a" * 64,
        ),
        "candidate-output-kind-authority",
    )
    candidate = container.pcb_candidates.create(
        project_id=project.id,
        base_revision="git:" + "b" * 40,
        base_snapshot_digest="sha256:" + "c" * 64,
        board_snapshot_digest="sha256:" + "d" * 64,
        rulepack_digest="sha256:" + "a" * 64,
        capability_digest="sha256:" + "e" * 64,
        idempotency_key="candidate-output-kind",
        require_capability=False,
    )

    async def exercise() -> httpx.Response:
        async with _client(container) as client:
            return await client.get(f"/api/v1/pcb-candidates/{candidate.id}")

    shown = asyncio.run(exercise())
    assert shown.status_code == 200
    assert shown.json()["output_kind"] == "boardir_only"
    monkeypatch.setattr(
        "pcbflow.cli._build",
        lambda: build_container(settings, kicad_override=_fake_kicad()),
    )
    cli_shown = CliRunner().invoke(
        app, ["pcb", "candidate", "show", candidate.id, "--json"]
    )
    assert cli_shown.exit_code == 0, cli_shown.output
    assert json.loads(cli_shown.stdout)["output_kind"] == "boardir_only"
    container.dispose()


def test_api_and_cli_map_unready_g3_candidate_to_stable_error(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "g3-unready-project"
    source.mkdir()
    settings = _settings(tmp_path)
    container = build_container(settings, kicad_override=_fake_kicad())
    project = container.projects.create("LCEDA", source, "g3-unready-project")
    container.eda_authorities.configure(
        project.id,
        ProjectEdaAuthorityInput(
            eda_kind=EdaKind.LCEDA_PRO,
            eda_profile_id="lceda-pro-v1",
            board_profile_id="stm32-environment-controller-2l-v1",
            rulepack_digest="sha256:" + "a" * 64,
        ),
        "g3-unready-authority",
    )
    candidate = container.pcb_candidates.create(
        project_id=project.id,
        base_revision="git:" + "b" * 40,
        base_snapshot_digest="sha256:" + "c" * 64,
        board_snapshot_digest="sha256:" + "d" * 64,
        rulepack_digest="sha256:" + "a" * 64,
        capability_digest="sha256:" + "e" * 64,
        idempotency_key="g3-unready-candidate",
        require_capability=False,
    )
    candidate_digest = "sha256:" + "f" * 64

    async def exercise() -> httpx.Response:
        async with _client(container) as client:
            return await client.post(
                f"/api/v1/pcb-candidates/{candidate.id}:approve-g3",
                headers={"Idempotency-Key": "g3-unready-api"},
                json={
                    "candidate_digest": candidate_digest,
                    "decision": "approve",
                    "actor": {"type": "human", "id": "local-user"},
                    "comment": "candidate is not ready",
                },
            )

    rejected = asyncio.run(exercise())
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "PCB_CANDIDATE_NOT_REVIEWABLE"
    monkeypatch.setattr(
        "pcbflow.cli._build",
        lambda: build_container(settings, kicad_override=_fake_kicad()),
    )
    cli_rejected = CliRunner().invoke(
        app,
        [
            "pcb",
            "candidate",
            "approve-g3",
            candidate.id,
            "--candidate-digest",
            candidate_digest,
            "--idempotency-key",
            "g3-unready-cli",
            "--actor-id",
            "local-user",
            "--comment",
            "candidate is not ready",
            "--approve",
            "--json",
        ],
    )
    assert cli_rejected.exit_code == 2
    assert "PCB_CANDIDATE_NOT_REVIEWABLE" in cli_rejected.output
    container.dispose()


def test_api_and_cli_reject_kicad_authority_for_pcb_candidates(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "kicad-candidate-project"
    source.mkdir()
    settings = _settings(tmp_path)
    container = build_container(settings, kicad_override=_fake_kicad())
    project = container.projects.create("KiCad", source, "kicad-candidate-project")
    container.eda_authorities.configure(
        project.id,
        ProjectEdaAuthorityInput(
            eda_kind=EdaKind.KICAD,
            eda_profile_id="kicad-9-v1",
            board_profile_id="controller-2l-v1",
            rulepack_digest="sha256:" + "a" * 64,
        ),
        "kicad-candidate-authority",
    )
    managed = container.revisions.adopt(project.id, "kicad-candidate-adopt")

    async def exercise() -> httpx.Response:
        async with _client(container) as client:
            return await client.post(
                f"/api/v1/projects/{managed.id}/pcb-candidates",
                headers={"Idempotency-Key": "kicad-candidate-create"},
                json={
                    "board_snapshot_digest": "sha256:" + "b" * 64,
                    "capability_digest": "sha256:" + "c" * 64,
                },
            )

    rejected = asyncio.run(exercise())
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "PCB_CAPABILITY_GATE_BLOCKED"
    monkeypatch.setattr(
        "pcbflow.cli._build",
        lambda: build_container(settings, kicad_override=_fake_kicad()),
    )
    cli_rejected = CliRunner().invoke(
        app,
        [
            "pcb",
            "candidate",
            "create",
            managed.id,
            "--board-snapshot-digest",
            "sha256:" + "b" * 64,
            "--capability-digest",
            "sha256:" + "c" * 64,
            "--idempotency-key",
            "kicad-candidate-cli",
        ],
    )
    assert cli_rejected.exit_code == 2
    assert "PCB_CAPABILITY_GATE_BLOCKED" in cli_rejected.output
    container.dispose()


def test_cli_rejects_invalid_pcb_candidate_inputs_before_build(monkeypatch) -> None:
    def unexpected_build():
        raise AssertionError("invalid CLI input must not build a container")

    monkeypatch.setattr("pcbflow.cli._build", unexpected_build)
    runner = CliRunner()
    invalid_commands = (
        [
            "pcb",
            "candidate",
            "create",
            "prj_unused",
            "--seed",
            "-1",
            "--idempotency-key",
            "invalid-seed",
        ],
        [
            "pcb",
            "candidate",
            "create",
            "prj_unused",
            "--net-id",
            "I2C_SCL",
            "--net-id",
            "I2C_SCL",
            "--idempotency-key",
            "duplicate-net",
        ],
        [
            "pcb",
            "candidate",
            "create",
            "prj_unused",
            "--board-snapshot-digest",
            "not-a-digest",
            "--idempotency-key",
            "invalid-digest",
        ],
    )

    for command in invalid_commands:
        result = runner.invoke(app, command)
        assert result.exit_code == 2
        assert "REQUEST_INVALID" in result.output


def test_api_creates_lceda_project_with_a_complete_immutable_authority(
    tmp_path: Path,
) -> None:
    source = tmp_path / "lceda-project"
    source.mkdir()
    container = build_container(_settings(tmp_path), kicad_override=_fake_kicad())
    payload = {
        "name": "LCEDA Controller",
        "source_path": str(source),
        "eda_kind": "lceda_pro",
        "eda_profile_id": "lceda-pro-v1",
        "board_profile_id": "stm32-environment-controller-2l-v1",
        "rulepack_digest": "sha256:" + "1" * 64,
    }

    async def exercise() -> tuple[httpx.Response, httpx.Response]:
        async with _client(container) as client:
            created = await client.post(
                "/api/v1/projects",
                headers={"Idempotency-Key": "lceda-create"},
                json=payload,
            )
            incomplete = await client.post(
                "/api/v1/projects",
                headers={"Idempotency-Key": "lceda-incomplete"},
                json={key: value for key, value in payload.items() if key != "rulepack_digest"},
            )
            return created, incomplete

    created, incomplete = asyncio.run(exercise())

    assert created.status_code == 201
    authority = container.eda_authorities.find_by_project_id(created.json()["id"])
    assert authority is not None
    assert authority.eda_kind.value == "lceda_pro"
    assert incomplete.status_code == 422
    assert incomplete.json()["error"]["code"] == "REQUEST_SCHEMA_INVALID"
    container.engine.dispose()


def test_api_enqueues_lceda_capability_probe_without_holding_request_open(
    tmp_path: Path,
) -> None:
    source = tmp_path / "api-capability-project"
    source.mkdir()
    container = build_container(_settings(tmp_path), kicad_override=_fake_kicad())

    async def exercise() -> tuple[httpx.Response, httpx.Response]:
        async with _client(container) as client:
            created = await client.post(
                "/api/v1/projects",
                headers={"Idempotency-Key": "api-capability-project"},
                json={
                    "name": "LCEDA Controller",
                    "source_path": str(source),
                    "eda_kind": "lceda_pro",
                    "eda_profile_id": "lceda-pro-v1",
                    "board_profile_id": "stm32-environment-controller-2l-v1",
                    "rulepack_digest": "sha256:" + "2" * 64,
                },
            )
            queued = await client.post(
                f"/api/v1/projects/{created.json()['id']}/eda-capability-probes",
                headers={"Idempotency-Key": "api-capability-probe"},
            )
            replayed = await client.post(
                f"/api/v1/projects/{created.json()['id']}/eda-capability-probes",
                headers={"Idempotency-Key": "api-capability-probe"},
            )
            return queued, replayed

    queued, replayed = asyncio.run(exercise())

    assert queued.status_code == 202
    task = queued.json()
    assert task["kind"] == "pcb.lceda_pro_capability_probe"
    assert task["status"] == "queued"
    assert replayed.status_code == 202
    assert replayed.json()["id"] == task["id"]
    container.engine.dispose()


def test_api_and_cli_reject_invalid_capability_probe_targets_and_keys(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "invalid-capability-target"
    source.mkdir()
    settings = _settings(tmp_path)
    container = build_container(settings, kicad_override=_fake_kicad())
    project = container.projects.create("Unconfigured", source, "unconfigured-probe")

    async def exercise() -> tuple[httpx.Response, httpx.Response, httpx.Response]:
        async with _client(container) as client:
            missing = await client.post(
                "/api/v1/projects/prj_missing/eda-capability-probes",
                headers={"Idempotency-Key": "missing-probe"},
            )
            unconfigured = await client.post(
                f"/api/v1/projects/{project.id}/eda-capability-probes",
                headers={"Idempotency-Key": "unconfigured-probe"},
            )
            invalid_key = await client.post(
                f"/api/v1/projects/{project.id}/eda-capability-probes",
                headers={"Idempotency-Key": " "},
            )
            return missing, unconfigured, invalid_key

    missing, unconfigured, invalid_key = asyncio.run(exercise())
    assert missing.status_code == 404
    assert unconfigured.status_code == 422
    assert unconfigured.json()["error"]["code"] == "REQUEST_INVALID"
    assert invalid_key.status_code == 422
    assert invalid_key.json()["error"]["code"] == "REQUEST_INVALID"

    monkeypatch.setattr(
        "pcbflow.cli._build", lambda: build_container(settings, kicad_override=_fake_kicad())
    )
    runner = CliRunner()
    rejected = runner.invoke(
        app,
        ["eda", "probe", "lceda-pro", project.id, "--idempotency-key", "valid-key"],
    )
    assert rejected.exit_code == 2
    assert "REQUEST_INVALID" in rejected.output
    container.dispose()


def test_api_and_cli_reject_kicad_and_missing_capability_probe_targets(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "kicad-capability-target"
    source.mkdir()
    settings = _settings(tmp_path)
    container = build_container(settings, kicad_override=_fake_kicad())
    project = container.projects.create("KiCad", source, "kicad-probe")
    container.eda_authorities.configure(
        project.id,
        ProjectEdaAuthorityInput(
            eda_kind=EdaKind.KICAD,
            eda_profile_id="kicad-9-v1",
            board_profile_id="controller-2l-v1",
            rulepack_digest="sha256:" + "6" * 64,
        ),
        "kicad-probe-authority",
    )

    async def exercise() -> httpx.Response:
        async with _client(container) as client:
            return await client.post(
                f"/api/v1/projects/{project.id}/eda-capability-probes",
                headers={"Idempotency-Key": "kicad-probe"},
            )

    rejected = asyncio.run(exercise())
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "REQUEST_INVALID"
    monkeypatch.setattr(
        "pcbflow.cli._build", lambda: build_container(settings, kicad_override=_fake_kicad())
    )
    runner = CliRunner()
    kicad = runner.invoke(
        app, ["eda", "probe", "lceda-pro", project.id, "--idempotency-key", "cli-kicad"]
    )
    missing = runner.invoke(
        app, ["eda", "probe", "lceda-pro", "prj_missing", "--idempotency-key", "cli-missing"]
    )
    assert kicad.exit_code == 2
    assert "REQUEST_INVALID" in kicad.output
    assert missing.exit_code == 2
    assert "PROJECT_NOT_FOUND" in missing.output
    container.dispose()


def test_api_invalid_lceda_create_leaves_no_project_for_corrected_retry(
    tmp_path: Path,
) -> None:
    source = tmp_path / "atomic-api-lceda-project"
    source.mkdir()
    container = build_container(_settings(tmp_path), kicad_override=_fake_kicad())
    invalid = {
        "name": "LCEDA Controller",
        "source_path": str(source),
        "eda_kind": "lceda_pro",
        "eda_profile_id": " lceda-pro-v1",
        "board_profile_id": "stm32-environment-controller-2l-v1",
        "rulepack_digest": "sha256:" + "7" * 64,
    }

    async def exercise() -> tuple[httpx.Response, httpx.Response]:
        async with _client(container) as client:
            first = await client.post(
                "/api/v1/projects",
                headers={"Idempotency-Key": "atomic-api-lceda"},
                json=invalid,
            )
            corrected = await client.post(
                "/api/v1/projects",
                headers={"Idempotency-Key": "atomic-api-lceda"},
                json={**invalid, "eda_profile_id": "lceda-pro-v1"},
            )
            return first, corrected

    first, corrected = asyncio.run(exercise())

    assert first.status_code == 422
    assert corrected.status_code == 201
    assert len(container.projects.list()) == 1
    assert container.eda_authorities.find_by_project_id(corrected.json()["id"])
    container.engine.dispose()


def test_api_rejects_unknown_eda_kind_and_extra_authority_field_separately(
    tmp_path: Path,
) -> None:
    source = tmp_path / "strict-api-lceda-project"
    source.mkdir()
    container = build_container(_settings(tmp_path), kicad_override=_fake_kicad())
    authority = {
        "eda_kind": "lceda_pro",
        "eda_profile_id": "lceda-pro-v1",
        "board_profile_id": "stm32-environment-controller-2l-v1",
        "rulepack_digest": "sha256:" + "8" * 64,
    }

    async def exercise() -> tuple[httpx.Response, httpx.Response]:
        async with _client(container) as client:
            unknown = await client.post(
                "/api/v1/projects",
                headers={"Idempotency-Key": "strict-api-kind"},
                json={"name": "Controller", "source_path": str(source), **authority, "eda_kind": "unknown"},
            )
            extra = await client.post(
                "/api/v1/projects",
                headers={"Idempotency-Key": "strict-api-extra"},
                json={"name": "Controller", "source_path": str(source), **authority, "extra": True},
            )
            return unknown, extra

    unknown, extra = asyncio.run(exercise())
    assert unknown.status_code == 422
    assert extra.status_code == 422
    assert container.projects.list() == []
    container.engine.dispose()


def test_api_rejects_authority_added_to_a_project_create_replay(
    tmp_path: Path,
) -> None:
    source = tmp_path / "replayed-project"
    source.mkdir()
    container = build_container(_settings(tmp_path), kicad_override=_fake_kicad())
    initial = {"name": "Controller", "source_path": str(source)}
    authority = {
        "eda_kind": "lceda_pro",
        "eda_profile_id": "lceda-pro-v1",
        "board_profile_id": "stm32-environment-controller-2l-v1",
        "rulepack_digest": "sha256:" + "6" * 64,
    }

    async def exercise() -> tuple[httpx.Response, httpx.Response]:
        async with _client(container) as client:
            created = await client.post(
                "/api/v1/projects",
                headers={"Idempotency-Key": "replayed-project"},
                json=initial,
            )
            replayed = await client.post(
                "/api/v1/projects",
                headers={"Idempotency-Key": "replayed-project"},
                json={**initial, **authority},
            )
            return created, replayed

    created, replayed = asyncio.run(exercise())

    assert created.status_code == 201
    assert replayed.status_code == 409
    assert replayed.json()["error"]["code"] == "EDA_AUTHORITY_CONFLICT"
    assert container.eda_authorities.find_by_project_id(created.json()["id"]) is None
    container.engine.dispose()


def test_api_create_replay_uses_original_authority_presence(
    tmp_path: Path,
) -> None:
    source = tmp_path / "api-registration-replay-project"
    source.mkdir()
    container = build_container(_settings(tmp_path), kicad_override=_fake_kicad())
    create = {"name": "Controller", "source_path": str(source)}
    authority = {
        "eda_kind": "lceda_pro",
        "eda_profile_id": "lceda-pro-v1",
        "board_profile_id": "stm32-environment-controller-2l-v1",
        "rulepack_digest": "sha256:" + "b" * 64,
    }

    async def exercise() -> tuple[httpx.Response, httpx.Response, httpx.Response, httpx.Response]:
        async with _client(container) as client:
            created = await client.post(
                "/api/v1/projects",
                headers={"Idempotency-Key": "api-registration-replay"},
                json=create,
            )
            configured = await client.post(
                f"/api/v1/projects/{created.json()['id']}/eda-authority",
                headers={"Idempotency-Key": "api-registration-authority"},
                json=authority,
            )
            replayed = await client.post(
                "/api/v1/projects",
                headers={"Idempotency-Key": "api-registration-replay"},
                json=create,
            )
            changed = await client.post(
                "/api/v1/projects",
                headers={"Idempotency-Key": "api-registration-replay"},
                json={**create, **authority},
            )
            return created, configured, replayed, changed

    created, configured, replayed, changed = asyncio.run(exercise())
    assert created.status_code == 201
    assert configured.status_code == 201
    assert replayed.status_code == 200
    assert replayed.json()["id"] == created.json()["id"]
    assert changed.status_code == 409
    assert changed.json()["error"]["code"] == "EDA_AUTHORITY_CONFLICT"
    container.engine.dispose()


def test_api_configures_eda_authority_with_strict_immutable_contract(
    tmp_path: Path,
) -> None:
    source = tmp_path / "legacy-project"
    source.mkdir()
    container = build_container(_settings(tmp_path), kicad_override=_fake_kicad())
    project = container.projects.create("Legacy", source, "legacy-project")
    authority = {
        "eda_kind": "lceda_pro",
        "eda_profile_id": "lceda-pro-v1",
        "board_profile_id": "stm32-environment-controller-2l-v1",
        "rulepack_digest": "sha256:" + "2" * 64,
    }

    async def exercise() -> tuple[
        httpx.Response, httpx.Response, httpx.Response, httpx.Response
    ]:
        async with _client(container) as client:
            created = await client.post(
                f"/api/v1/projects/{project.id}/eda-authority",
                headers={"Idempotency-Key": "legacy-authority"},
                json=authority,
            )
            conflicted = await client.post(
                f"/api/v1/projects/{project.id}/eda-authority",
                headers={"Idempotency-Key": "legacy-authority-conflict"},
                json={**authority, "eda_profile_id": "lceda-pro-v2"},
            )
            unknown_kind = await client.post(
                f"/api/v1/projects/{project.id}/eda-authority",
                headers={"Idempotency-Key": "legacy-authority-invalid"},
                json={**authority, "eda_kind": "unknown"},
            )
            extra_field = await client.post(
                f"/api/v1/projects/{project.id}/eda-authority",
                headers={"Idempotency-Key": "legacy-authority-extra"},
                json={**authority, "extra": True},
            )
            return created, conflicted, unknown_kind, extra_field

    created, conflicted, unknown_kind, extra_field = asyncio.run(exercise())

    assert created.status_code == 201
    assert created.json()["canonical_digest"].startswith("sha256:")
    assert conflicted.status_code == 409
    assert conflicted.json()["error"]["code"] == "EDA_AUTHORITY_CONFLICT"
    assert unknown_kind.status_code == 422
    assert unknown_kind.json()["error"]["code"] == "REQUEST_SCHEMA_INVALID"
    assert extra_field.status_code == 422
    assert extra_field.json()["error"]["code"] == "REQUEST_SCHEMA_INVALID"
    container.engine.dispose()


def test_cli_configures_project_eda_authority(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "cli-lceda-project"
    source.mkdir()
    settings = _settings(tmp_path)
    container = build_container(settings, kicad_override=_fake_kicad())
    project = container.projects.create("Legacy", source, "cli-legacy-project")

    def build_for_cli():
        return build_container(settings, kicad_override=_fake_kicad())

    monkeypatch.setattr("pcbflow.cli._build", build_for_cli)
    result = CliRunner().invoke(
        app,
        [
            "project",
            "eda-authority",
            project.id,
            "--eda-kind",
            "lceda_pro",
            "--eda-profile-id",
            "lceda-pro-v1",
            "--board-profile-id",
            "stm32-environment-controller-2l-v1",
            "--rulepack-digest",
            "sha256:" + "3" * 64,
            "--idempotency-key",
            "cli-lceda-authority",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["project_id"] == project.id
    assert payload["eda_kind"] == "lceda_pro"
    unknown_kind = CliRunner().invoke(
        app,
        [
            "project", "eda-authority", project.id, "--eda-kind", "unknown",
            "--eda-profile-id", "lceda-pro-v1", "--board-profile-id",
            "stm32-environment-controller-2l-v1", "--rulepack-digest",
            "sha256:" + "d" * 64, "--idempotency-key", "unknown-authority",
        ],
    )
    assert unknown_kind.exit_code == 2
    assert "EDA_AUTHORITY_INVALID" in unknown_kind.output
    container.engine.dispose()


def test_cli_project_add_freezes_a_complete_eda_authority(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "cli-new-lceda-project"
    source.mkdir()
    settings = _settings(tmp_path)

    def build_for_cli():
        return build_container(settings, kicad_override=_fake_kicad())

    monkeypatch.setattr("pcbflow.cli._build", build_for_cli)
    result = CliRunner().invoke(
        app,
        [
            "project",
            "add",
            str(source),
            "--name",
            "LCEDA Controller",
            "--idempotency-key",
            "cli-new-lceda-project",
            "--eda-kind",
            "lceda_pro",
            "--eda-profile-id",
            "lceda-pro-v1",
            "--board-profile-id",
            "stm32-environment-controller-2l-v1",
            "--rulepack-digest",
            "sha256:" + "4" * 64,
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    project = json.loads(result.stdout)
    container = build_container(settings, kicad_override=_fake_kicad())
    try:
        authority = container.eda_authorities.find_by_project_id(project["id"])
        assert authority is not None
        assert authority.rulepack_digest == "sha256:" + "4" * 64
    finally:
        container.engine.dispose()


def test_cli_enqueues_project_lceda_capability_probe(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "cli-capability-project"
    source.mkdir()
    settings = _settings(tmp_path)

    def build_for_cli():
        return build_container(settings, kicad_override=_fake_kicad())

    monkeypatch.setattr("pcbflow.cli._build", build_for_cli)
    runner = CliRunner()
    created = runner.invoke(
        app,
        [
            "project", "add", str(source), "--name", "LCEDA Controller",
            "--idempotency-key", "cli-capability-project", "--eda-kind",
            "lceda_pro", "--eda-profile-id", "lceda-pro-v1",
            "--board-profile-id", "stm32-environment-controller-2l-v1",
            "--rulepack-digest", "sha256:" + "5" * 64, "--json",
        ],
    )
    assert created.exit_code == 0, created.output
    project_id = json.loads(created.stdout)["id"]

    queued = runner.invoke(
        app,
        [
            "eda", "probe", "lceda-pro", project_id,
            "--idempotency-key", "cli-capability-probe", "--json",
        ],
    )

    assert queued.exit_code == 0, queued.output
    task = json.loads(queued.stdout)
    assert task["kind"] == "pcb.lceda_pro_capability_probe"
    assert task["status"] == "queued"


def test_cli_invalid_lceda_create_leaves_no_project_for_corrected_retry(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "atomic-cli-lceda-project"
    source.mkdir()
    settings = _settings(tmp_path)

    def build_for_cli():
        return build_container(settings, kicad_override=_fake_kicad())

    monkeypatch.setattr("pcbflow.cli._build", build_for_cli)
    runner = CliRunner()
    arguments = [
        "project", "add", str(source), "--name", "LCEDA Controller",
        "--idempotency-key", "atomic-cli-lceda", "--eda-kind", "lceda_pro",
        "--eda-profile-id", " lceda-pro-v1", "--board-profile-id",
        "stm32-environment-controller-2l-v1", "--rulepack-digest",
        "sha256:" + "9" * 64,
    ]
    first = runner.invoke(app, arguments)
    corrected = runner.invoke(app, arguments[:10] + ["lceda-pro-v1"] + arguments[11:])

    assert first.exit_code == 2
    assert "EDA_AUTHORITY_INVALID" in first.output
    assert corrected.exit_code == 0, corrected.output
    container = build_container(settings, kicad_override=_fake_kicad())
    try:
        assert len(container.projects.list()) == 1
    finally:
        container.engine.dispose()


def test_cli_rejects_whitespace_and_overflow_authority_identifiers(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "strict-cli-lceda-project"
    source.mkdir()
    settings = _settings(tmp_path)

    def build_for_cli():
        return build_container(settings, kicad_override=_fake_kicad())

    monkeypatch.setattr("pcbflow.cli._build", build_for_cli)
    runner = CliRunner()
    base = [
        "project", "add", str(source), "--name", "LCEDA Controller",
        "--eda-kind", "lceda_pro", "--board-profile-id",
        "stm32-environment-controller-2l-v1", "--rulepack-digest",
        "sha256:" + "a" * 64,
    ]
    whitespace = runner.invoke(
        app,
        base + ["--eda-profile-id", " lceda-pro-v1", "--idempotency-key", "strict-cli-space"],
    )
    overflow = runner.invoke(
        app,
        base + ["--eda-profile-id", "x" * 129, "--idempotency-key", "strict-cli-overflow"],
    )

    assert whitespace.exit_code == 2
    assert overflow.exit_code == 2
    assert "EDA_AUTHORITY_INVALID" in whitespace.output
    assert "EDA_AUTHORITY_INVALID" in overflow.output


def test_cli_create_replay_uses_original_authority_presence(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "cli-registration-replay-project"
    source.mkdir()
    settings = _settings(tmp_path)

    def build_for_cli():
        return build_container(settings, kicad_override=_fake_kicad())

    monkeypatch.setattr("pcbflow.cli._build", build_for_cli)
    runner = CliRunner()
    create = [
        "project", "add", str(source), "--name", "Controller",
        "--idempotency-key", "cli-registration-replay", "--json",
    ]
    created = runner.invoke(app, create)
    project_id = json.loads(created.stdout)["id"]
    configured = runner.invoke(
        app,
        [
            "project", "eda-authority", project_id, "--eda-kind", "lceda_pro",
            "--eda-profile-id", "lceda-pro-v1", "--board-profile-id",
            "stm32-environment-controller-2l-v1", "--rulepack-digest",
            "sha256:" + "c" * 64, "--idempotency-key",
            "cli-registration-authority",
        ],
    )
    replayed = runner.invoke(app, create)
    changed = runner.invoke(
        app,
        create[:-1]
        + [
            "--eda-kind", "lceda_pro", "--eda-profile-id", "lceda-pro-v1",
            "--board-profile-id", "stm32-environment-controller-2l-v1",
            "--rulepack-digest", "sha256:" + "c" * 64, "--json",
        ],
    )

    assert created.exit_code == 0, created.output
    assert configured.exit_code == 0, configured.output
    assert replayed.exit_code == 0, replayed.output
    assert json.loads(replayed.stdout)["id"] == project_id
    assert changed.exit_code == 2
    assert "EDA_AUTHORITY_CONFLICT" in changed.output


def test_cli_reports_an_eda_authority_conflict(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "cli-conflicting-lceda-project"
    source.mkdir()
    settings = _settings(tmp_path)
    container = build_container(settings, kicad_override=_fake_kicad())
    project = container.projects.create("Legacy", source, "cli-conflict-project")

    def build_for_cli():
        return build_container(settings, kicad_override=_fake_kicad())

    monkeypatch.setattr("pcbflow.cli._build", build_for_cli)
    runner = CliRunner()
    base_arguments = [
        "project",
        "eda-authority",
        project.id,
        "--eda-kind",
        "lceda_pro",
        "--board-profile-id",
        "stm32-environment-controller-2l-v1",
        "--rulepack-digest",
        "sha256:" + "5" * 64,
    ]
    first = runner.invoke(
        app,
        base_arguments
        + [
            "--eda-profile-id",
            "lceda-pro-v1",
            "--idempotency-key",
            "cli-authority-first",
        ],
    )
    conflicting = runner.invoke(
        app,
        base_arguments
        + [
            "--eda-profile-id",
            "lceda-pro-v2",
            "--idempotency-key",
            "cli-authority-conflict",
        ],
    )

    assert first.exit_code == 0, first.output
    assert conflicting.exit_code == 2
    assert "EDA_AUTHORITY_CONFLICT" in conflicting.output
    container.engine.dispose()


def test_api_and_cli_cancel_tasks(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    container = build_container(settings, kicad_override=_fake_kicad())
    api_task = container.tasks.enqueue("pending", {}, "cancel-api-task", None)

    async def exercise_api() -> None:
        async with _client(container) as client:
            invalid = await client.post(
                f"/api/v1/tasks/{api_task.id}:cancel",
                headers={"Idempotency-Key": "cancel-api-invalid"},
                json={"reason": "operator requested cancellation", "extra": True},
            )
            assert invalid.status_code == 422
            assert invalid.json()["error"]["code"] == "REQUEST_SCHEMA_INVALID"

            blank_reason = await client.post(
                f"/api/v1/tasks/{api_task.id}:cancel",
                headers={"Idempotency-Key": "cancel-api-blank-reason"},
                json={"reason": "   "},
            )
            assert blank_reason.status_code == 422
            assert blank_reason.json()["error"]["code"] == "REQUEST_SCHEMA_INVALID"

            cancelled = await client.post(
                f"/api/v1/tasks/{api_task.id}:cancel",
                headers={"Idempotency-Key": "cancel-api-task"},
                json={"reason": "operator requested cancellation"},
            )
            assert cancelled.status_code == 200
            assert cancelled.json()["status"] == "cancelled"
            assert cancelled.json()["last_error_code"] == "TASK_CANCELLED"
            assert cancelled.json()["cancellation_reason"] == "operator requested cancellation"

            repeated = await client.post(
                f"/api/v1/tasks/{api_task.id}:cancel",
                headers={"Idempotency-Key": "cancel-api-task-repeat"},
                json={"reason": "later request"},
            )
            assert repeated.status_code == 200
            assert repeated.json()["cancellation_reason"] == "operator requested cancellation"

            terminal = container.tasks.enqueue("terminal", {}, "cancel-api-terminal", None)
            lease = container.tasks.claim_next("worker-a", api_module.utc_now(), 30)
            assert lease is not None and lease.task_id == terminal.id
            container.tasks.start(terminal.id, lease.lease_token, api_module.utc_now())
            container.tasks.complete(
                terminal.id, lease.lease_token, {"ok": True}, api_module.utc_now()
            )
            rejected = await client.post(
                f"/api/v1/tasks/{terminal.id}:cancel",
                headers={"Idempotency-Key": "cancel-api-terminal"},
                json={"reason": "too late"},
            )
            assert rejected.status_code == 409
            assert rejected.json()["error"]["code"] == "TASK_NOT_CANCELLABLE"

    def build_for_cli():
        return build_container(settings, kicad_override=_fake_kicad())

    try:
        asyncio.run(exercise_api())
        cli_task = container.tasks.enqueue("pending", {}, "cancel-cli-task", None)
        monkeypatch.setattr("pcbflow.cli._build", build_for_cli)
        result = CliRunner().invoke(
            app,
            [
                "task",
                "cancel",
                cli_task.id,
                "--reason",
                "operator requested cancellation",
                "--idempotency-key",
                "cancel-cli-task",
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["status"] == "cancelled"
    finally:
        container.dispose()


def test_remote_api_rejects_local_source_paths(tmp_path: Path) -> None:
    container = build_container(
        _settings(tmp_path, remote_mode=True, api_token="remote-test-token"),
        kicad_override=_fake_kicad(),
    )

    async def exercise() -> httpx.Response:
        async with _client(container) as client:
            return await client.post(
                "/api/v1/projects",
                headers={
                    "Authorization": "Bearer remote-test-token",
                    "Idempotency-Key": "remote-project",
                },
                json={"name": "Controller", "source_path": str(tmp_path)},
            )

    response = asyncio.run(exercise())
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "LOCAL_SOURCE_PATHS_DISABLED"
    container.engine.dispose()


def test_remote_api_disables_worker_execution_endpoint(tmp_path: Path) -> None:
    container = build_container(
        _settings(tmp_path, remote_mode=True, api_token="remote-test-token"),
        kicad_override=_fake_kicad(),
    )

    async def exercise() -> httpx.Response:
        async with _client(container, raise_app_exceptions=False) as client:
            return await client.post(
                "/api/v1/worker:run-once",
                headers={"Authorization": "Bearer remote-test-token"},
            )

    response = asyncio.run(exercise())
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "REMOTE_WORKER_DISABLED"
    container.engine.dispose()


def test_remote_api_requires_a_bearer_token_for_controlled_writes(
    tmp_path: Path,
) -> None:
    container = build_container(
        _settings(tmp_path, remote_mode=True, api_token="remote-test-token"),
        kicad_override=_fake_kicad(),
    )

    async def exercise() -> tuple[httpx.Response, httpx.Response]:
        async with _client(container, raise_app_exceptions=False) as client:
            missing = await client.post(
                "/api/v1/projects/prj_missing:adopt",
                headers={"Idempotency-Key": "remote-adopt-missing"},
            )
            authorized = await client.post(
                "/api/v1/projects/prj_missing:adopt",
                headers={
                    "Authorization": "Bearer remote-test-token",
                    "Idempotency-Key": "remote-adopt-missing",
                },
            )
            return missing, authorized

    missing, authorized = asyncio.run(exercise())
    assert missing.status_code == 401
    assert missing.json()["error"]["code"] == "REMOTE_AUTH_REQUIRED"
    assert authorized.status_code == 404
    assert authorized.json()["error"]["code"] == "PROJECT_NOT_FOUND"
    container.engine.dispose()


def test_remote_api_rejects_non_ascii_bearer_tokens(tmp_path: Path) -> None:
    container = build_container(
        _settings(tmp_path, remote_mode=True, api_token="remote-test-token"),
        kicad_override=_fake_kicad(),
    )

    async def exercise() -> httpx.Response:
        async with _client(container, raise_app_exceptions=False) as client:
            request = httpx.Request(
                "GET",
                "http://testserver/api/v1/projects",
                headers=[
                    (b"authorization", b"Bearer remote-" + bytes([255]))
                ],
            )
            return await client.send(request)

    response = asyncio.run(exercise())
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "REMOTE_AUTH_REQUIRED"
    container.engine.dispose()


def test_remote_api_uses_authenticated_actor_not_request_actor(tmp_path: Path) -> None:
    fixtures = Path(__file__).resolve().parents[1] / "fixtures"
    source = tmp_path / "remote-controlled-source"
    source.mkdir()
    (source / "board.kicad_sch").write_text("(kicad_sch)\n", encoding="utf-8")
    container = build_container(
        _settings(tmp_path, remote_mode=True, api_token="remote-test-token"),
        kicad_override=_fake_kicad(),
    )
    project = container.projects.create("Controller", source, "remote-project")
    managed = container.revisions.adopt(project.id, "remote-adopt")
    draft = container.requirements.import_draft(
        managed.id,
        (fixtures / "requirements" / "reference-controller.yaml").read_bytes(),
        "remote-requirements",
    )
    pending = container.requirements.submit(draft.id, "remote-requirements-submit")

    async def exercise() -> httpx.Response:
        async with _client(container, raise_app_exceptions=False) as client:
            return await client.post(
                "/api/v1/approvals",
                headers={
                    "Authorization": "Bearer remote-test-token",
                    "Idempotency-Key": "remote-g1",
                },
                json={
                    "subject_type": "requirement_set",
                    "subject_id": pending.id,
                    "subject_digest": pending.subject_digest(),
                    "decision": "approve",
                    "actor": {"type": "human", "id": "forged-user"},
                    "comment": "approved remotely",
                },
            )

    response = asyncio.run(exercise())
    assert response.status_code == 200
    decision = container.gate_decisions.find_by_key(managed.id, "remote-g1")
    assert decision is not None
    assert decision.actor_type == "service"
    assert decision.actor_id == "remote-api"

    current_project = container.projects.get(managed.id)
    batch = _command_batch(
        {"id": current_project.id, "current_revision": current_project.current_revision},
        {"id": pending.id},
    )
    batch["actor"] = {"type": "human", "id": "forged-user"}
    batch["commands"][0]["actor"] = {"type": "human", "id": "forged-user"}

    async def create_proposal() -> httpx.Response:
        async with _client(container, raise_app_exceptions=False) as client:
            return await client.post(
                f"/api/v1/projects/{managed.id}/proposals",
                headers={
                    "Authorization": "Bearer remote-test-token",
                    "Idempotency-Key": "api-proposal",
                },
                json=batch,
            )

    queued = asyncio.run(create_proposal())
    assert queued.status_code == 202
    stored = container.command_batches.get(queued.json()["command_batch_id"])
    assert stored.actor.type == "service"
    assert stored.actor.id == "remote-api"
    assert {(command.actor.type, command.actor.id) for command in stored.commands} == {
        ("service", "remote-api")
    }
    container.engine.dispose()


def test_api_rejects_actor_ids_larger_than_the_persistence_contract(
    tmp_path: Path,
) -> None:
    container = build_container(_settings(tmp_path), kicad_override=_fake_kicad())

    async def exercise() -> httpx.Response:
        async with _client(container, raise_app_exceptions=False) as client:
            return await client.post(
                "/api/v1/approvals",
                headers={"Idempotency-Key": "oversized-actor"},
                json={
                    "subject_type": "requirement_set",
                    "subject_id": "reqset_missing",
                    "subject_digest": "sha256:" + "0" * 64,
                    "decision": "approve",
                    "actor": {"type": "human", "id": "a" * 256},
                    "comment": "oversized actor must fail validation",
                },
            )

    response = asyncio.run(exercise())
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "REQUEST_SCHEMA_INVALID"
    container.engine.dispose()


def test_api_rejects_oversized_json_before_decoding_body(tmp_path: Path) -> None:
    container = build_container(
        _settings(tmp_path, max_api_body_bytes=32), kicad_override=_fake_kicad()
    )
    body = b'{"payload":"' + (b"x" * 64) + b'"}'

    async def streamed_body():
        yield body[:17]
        yield body[17:]

    async def exercise() -> tuple[httpx.Response, httpx.Response]:
        async with _client(container, raise_app_exceptions=False) as client:
            fixed_length = await client.post(
                "/api/v1/projects/prj_missing/requirement-sets",
                headers={
                    "Content-Type": "application/json",
                    "Content-Length": str(len(body)),
                    "Idempotency-Key": "body-limit-length",
                },
                content=body,
            )
            streamed = await client.post(
                "/api/v1/projects/prj_missing/requirement-sets",
                headers={
                    "Content-Type": "application/json",
                    "Idempotency-Key": "body-limit-stream",
                },
                content=streamed_body(),
            )
            return fixed_length, streamed

    fixed_length, streamed = asyncio.run(exercise())
    for response in (fixed_length, streamed):
        assert response.status_code == 413
        error = response.json()["error"]
        assert error["code"] == "REQUEST_BODY_TOO_LARGE"
        assert response.headers["X-Correlation-ID"] == error["correlation_id"]
    container.engine.dispose()


def test_api_replaces_blank_correlation_id_on_body_limit(tmp_path: Path) -> None:
    container = build_container(
        _settings(tmp_path, max_api_body_bytes=1), kicad_override=_fake_kicad()
    )

    async def exercise() -> httpx.Response:
        async with _client(container, raise_app_exceptions=False) as client:
            return await client.post(
                "/api/v1/projects/prj_missing/requirement-sets",
                headers={
                    "Content-Type": "application/json",
                    "Idempotency-Key": "blank-correlation-id",
                    "X-Correlation-ID": "",
                },
                content=b"{}",
            )

    response = asyncio.run(exercise())
    assert response.status_code == 413
    assert response.headers["X-Correlation-ID"]
    assert response.headers["X-Correlation-ID"] == response.json()["error"][
        "correlation_id"
    ]
    container.engine.dispose()


def test_request_body_limit_checks_chunk_size_before_buffering(monkeypatch) -> None:
    class GuardedBuffer(bytearray):
        def extend(self, values) -> None:
            raise AssertionError("oversized chunk was buffered")

    monkeypatch.setattr(api_module, "bytearray", GuardedBuffer, raising=False)
    sent: list[dict[str, object]] = []
    downstream_calls: list[object] = []
    messages = iter(({"type": "http.request", "body": b"xx", "more_body": False},))

    async def downstream(scope, receive, send) -> None:
        downstream_calls.append(scope)

    async def receive() -> dict[str, object]:
        return next(messages)

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    middleware = RequestBodyLimitMiddleware(downstream, max_bytes=1)
    asyncio.run(
        middleware(
            {"type": "http", "method": "POST", "headers": (), "state": {}},
            receive,
            send,
        )
    )

    assert downstream_calls == []
    assert sent[0]["status"] == 413


def test_api_returns_schema_errors_for_non_finite_json_values(tmp_path: Path) -> None:
    container = build_container(_settings(tmp_path), kicad_override=_fake_kicad())

    async def exercise() -> tuple[httpx.Response, httpx.Response]:
        async with _client(container, raise_app_exceptions=False) as client:
            requirements = await client.post(
                "/api/v1/projects/prj_missing/requirement-sets",
                headers={"Idempotency-Key": "non-finite-requirements"},
                content=b'{"schema_version":NaN}',
            )
            proposal = await client.post(
                "/api/v1/projects/prj_missing/proposals",
                headers={"Idempotency-Key": "non-finite-proposal"},
                content=b'{"schema_version":NaN}',
            )
            return requirements, proposal

    requirements, proposal = asyncio.run(exercise())
    assert requirements.status_code == 422
    assert requirements.json()["error"]["code"] == "REQUIREMENTS_SCHEMA_INVALID"
    assert proposal.status_code == 422
    assert proposal.json()["error"]["code"] == "DESIGN_COMMAND_SCHEMA_INVALID"
    container.engine.dispose()


def test_api_returns_stable_error_when_g1_is_no_longer_pending(tmp_path: Path) -> None:
    fixtures = Path(__file__).resolve().parents[1] / "fixtures"
    source = tmp_path / "g1-source"
    source.mkdir()
    (source / "board.kicad_sch").write_text("(kicad_sch)\n", encoding="utf-8")
    container = build_container(_settings(tmp_path), kicad_override=_fake_kicad())
    project = container.projects.create("Controller", source, "g1-project")
    managed = container.revisions.adopt(project.id, "g1-adopt")
    draft = container.requirements.import_draft(
        managed.id,
        (fixtures / "requirements" / "reference-controller.yaml").read_bytes(),
        "g1-requirements",
    )
    pending = container.requirements.submit(draft.id, "g1-requirements-submit")
    frozen = container.approvals.decide_g1(
        requirement_set_id=pending.id,
        subject_digest=pending.subject_digest(),
        decision="approve",
        actor_type="human",
        actor_id="local-user",
        comment="approved",
        idempotency_key="g1-first-decision",
    )

    async def exercise() -> httpx.Response:
        async with _client(container, raise_app_exceptions=False) as client:
            return await client.post(
                "/api/v1/approvals",
                headers={"Idempotency-Key": "g1-second-decision"},
                json={
                    "subject_type": "requirement_set",
                    "subject_id": frozen.id,
                    "subject_digest": frozen.subject_digest(),
                    "decision": "approve",
                    "actor": {"type": "human", "id": "local-user"},
                    "comment": "repeated approval",
                },
            )

    response = asyncio.run(exercise())
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "REQUEST_INVALID"
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
    assert set(payload["lceda_pro"]) == {
        "available",
        "executable",
        "version",
        "executable_digest",
        "profile_id",
        "profile_revision",
        "operations",
        "write_verified",
        "reason",
    }
    assert payload["lceda_pro"]["write_verified"] is False
    assert payload["lceda_pro"]["reason"] == "lceda_pro_not_found"


def test_lceda_probe_json_has_stable_shape_and_does_not_execute_a_configured_bridge(
    monkeypatch, tmp_path: Path
) -> None:
    gui = tmp_path / "lceda-pro.exe"
    gui.write_bytes(b"gui fixture")
    bridge = tmp_path / "official-bridge.exe"
    bridge.write_bytes(b"not an executable")
    observed_argv: list[tuple[str, ...]] = []

    def gui_only_runner(self, argv, *args, **kwargs):
        observed_argv.append(tuple(argv))
        if argv[0] == str(bridge.resolve()):
            raise AssertionError("configured bridge must not be dynamically executed")
        assert tuple(argv) == (str(gui.resolve()), "--version")
        return ProcessResult(tuple(argv), 0, "3.2.166", "", False)

    monkeypatch.setattr("pcbflow.process.ProcessRunner.run", gui_only_runner)
    result = CliRunner().invoke(
        app,
        ["eda", "probe", "lceda-pro", "--json"],
        env={
            "PCBFLOW_DATA_DIR": str(tmp_path / "cli-data"),
            "PCBFLOW_LCEDA_PRO_EXECUTABLE": str(gui),
            "PCBFLOW_LCEDA_PRO_OFFICIAL_BRIDGE": str(bridge),
        },
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert set(payload) == {
        "available",
        "executable",
        "version",
        "executable_digest",
        "profile_id",
        "profile_revision",
        "operations",
        "write_verified",
        "reason",
    }
    assert payload["write_verified"] is False
    assert payload["operations"] == []
    assert payload["available"] is True
    assert payload["version"] == "3.2.166"
    assert payload["reason"] == "official_bridge_not_supported"
    assert observed_argv == [(str(gui.resolve()), "--version")]


def test_cli_serve_rejects_non_loopback_bindings(monkeypatch) -> None:
    def unexpected_build():
        raise AssertionError("serve should validate its host before building services")

    monkeypatch.setattr("pcbflow.cli._build", unexpected_build)
    result = CliRunner().invoke(app, ["serve", "--host", "0.0.0.0"])

    assert result.exit_code != 0
    assert "loopback" in result.output


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
            9,
            "kicad-9-v1",
            1,
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


def test_phase_2a_cli_help_lists_all_command_groups() -> None:
    runner = CliRunner()
    root = runner.invoke(app, ["--help"])
    assert root.exit_code == 0
    for name in ("project", "requirements", "approval", "proposal", "worker"):
        assert name in root.output

    assert "adopt" in runner.invoke(app, ["project", "--help"]).output
    assert "cancel" in runner.invoke(app, ["task", "--help"]).output
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


def test_api_and_cli_submit_and_execute_bound_module_batches(
    tmp_path: Path, monkeypatch
) -> None:
    fixtures = Path(__file__).resolve().parents[1] / "fixtures"
    source = tmp_path / "bound-api-cli-source"
    shutil.copytree(fixtures / "kicad" / "controlled-design", source)
    env = {
        "PCBFLOW_DATA_DIR": str(tmp_path / "data"),
        "PCBFLOW_MODULE_CATALOG_DIR": str(fixtures / "modules"),
    }
    settings = Settings.from_env(env)
    container = build_container(settings, kicad_override=FakeCliKicad())
    component_directory = build_component_directory(
        tmp_path / "bound-api-cli-component", include_model=True
    )
    component = container.components.import_revision(
        component_directory / "component.yaml", "api-cli-bound-component"
    )
    binding = container.component_module_bindings.bind(
        component.id, 9, "modrev_status_led_v1", "api-cli-bound-module-binding"
    )
    mismatched_binding = container.component_module_bindings.bind(
        component.id,
        10,
        "modrev_status_led_v1",
        "api-cli-bound-module-binding-mismatch",
    )
    project = container.projects.create("Controller", source, "api-cli-bound-project")
    managed = container.revisions.adopt(project.id, "api-cli-bound-adopt")
    requirements = (fixtures / "requirements" / "reference-controller.yaml").read_bytes()
    draft = container.requirements.import_draft(
        managed.id, requirements, "api-cli-bound-requirements"
    )
    pending = container.requirements.submit(
        draft.id, "api-cli-bound-requirements-submit"
    )
    frozen = container.approvals.decide_g1(
        requirement_set_id=pending.id,
        subject_digest=pending.subject_digest(),
        decision="approve",
        actor_type="human",
        actor_id="local-user",
        comment="approved",
        idempotency_key="api-cli-bound-g1",
    )
    current = container.projects.get(managed.id)
    project_view = {"id": current.id, "current_revision": current.current_revision}
    requirement_set_view = {"id": frozen.id}

    async def submit_with_api() -> str:
        async with _client(container) as client:
            batch = _bound_command_batch(project_view, requirement_set_view, binding.id)
            queued = await client.post(
                f"/api/v1/projects/{managed.id}/proposals",
                headers={"Idempotency-Key": "api-proposal"},
                json=batch,
            )
            invalid = _bound_command_batch(
                project_view, requirement_set_view, binding.id
            )
            invalid["batch_id"] = "bat_api_bound_invalid"
            invalid["idempotency_key"] = "api-bound-invalid"
            invalid_command = invalid["commands"][0]
            invalid_command["batch_id"] = "bat_api_bound_invalid"
            invalid_command["command_id"] = "cmd_api_bound_invalid"
            invalid_command["idempotency_key"] = "api-bound-invalid:1"
            invalid_command["operation"]["payload"]["module_revision_id"] = (
                "modrev_status_led_v1"
            )
            rejected = await client.post(
                f"/api/v1/projects/{managed.id}/proposals",
                headers={"Idempotency-Key": "api-bound-invalid"},
                json=invalid,
            )

            assert queued.status_code == 202, queued.text
            assert queued.json()["id"].startswith("prop_")
            assert rejected.status_code == 422
            assert rejected.json()["error"]["code"] == "DESIGN_COMMAND_SCHEMA_INVALID"
            assert (await client.post("/api/v1/worker:run-once")).json() == {
                "handled": True
            }
            shown = await client.get(f"/api/v1/proposals/{queued.json()['id']}")
            assert shown.json()["status"] == "ready_for_review"

            failing_batch = _bound_command_batch(
                project_view, requirement_set_view, mismatched_binding.id
            )
            failing_batch["batch_id"] = "bat_api_bound_mismatch"
            failing_batch["idempotency_key"] = "api-bound-mismatch"
            failing_command = failing_batch["commands"][0]
            failing_command["batch_id"] = "bat_api_bound_mismatch"
            failing_command["command_id"] = "cmd_api_bound_mismatch"
            failing_command["idempotency_key"] = "api-bound-mismatch:1"
            failed = await client.post(
                f"/api/v1/projects/{managed.id}/proposals",
                headers={"Idempotency-Key": "api-bound-mismatch"},
                json=failing_batch,
            )
            assert failed.status_code == 202, failed.text
            assert (await client.post("/api/v1/worker:run-once")).json() == {
                "handled": True
            }
            failed_status = await client.get(
                f"/api/v1/proposals/{failed.json()['id']}"
            )
            assert failed_status.json()["status"] == "validation_failed"
            assert (
                failed_status.json()["last_error_code"]
                == "COMPONENT_MODULE_BINDING_KICAD_MAJOR_MISMATCH"
            )
            return queued.json()["id"]

    def build_for_test():
        return build_container(settings, kicad_override=FakeCliKicad())

    try:
        api_proposal_id = asyncio.run(submit_with_api())
        assert api_proposal_id.startswith("prop_")
        monkeypatch.setattr("pcbflow.cli._build", build_for_test)
        cli_batch = _bound_command_batch(project_view, requirement_set_view, binding.id)
        cli_batch["batch_id"] = "bat_cli_bound_status_led"
        cli_batch["idempotency_key"] = "cli-bound-status-led"
        cli_command = cli_batch["commands"][0]
        cli_command["batch_id"] = "bat_cli_bound_status_led"
        cli_command["command_id"] = "cmd_cli_bound_status_led"
        cli_command["idempotency_key"] = "cli-bound-status-led:1"
        batch_file = tmp_path / "bound-cli-commands.json"
        batch_file.write_text(json.dumps(cli_batch), encoding="utf-8")
        runner = CliRunner()
        queued = runner.invoke(
            app,
            [
                "proposal",
                "create",
                managed.id,
                "--file",
                str(batch_file),
                "--idempotency-key",
                "cli-bound-status-led",
                "--json",
            ],
            env=env,
        )
        assert queued.exit_code == 0, queued.output
        cli_proposal = json.loads(queued.stdout)
        assert cli_proposal["id"].startswith("prop_")
        worked = runner.invoke(app, ["worker", "--once", "--json"], env=env)
        assert worked.exit_code == 0, worked.output
        shown = runner.invoke(
            app, ["proposal", "show", cli_proposal["id"], "--json"], env=env
        )
        assert shown.exit_code == 0, shown.output
        assert json.loads(shown.stdout)["status"] == "ready_for_review"

        cli_failure_batch = _bound_command_batch(
            project_view, requirement_set_view, mismatched_binding.id
        )
        cli_failure_batch["batch_id"] = "bat_cli_bound_mismatch"
        cli_failure_batch["idempotency_key"] = "cli-bound-mismatch"
        cli_failure_command = cli_failure_batch["commands"][0]
        cli_failure_command["batch_id"] = "bat_cli_bound_mismatch"
        cli_failure_command["command_id"] = "cmd_cli_bound_mismatch"
        cli_failure_command["idempotency_key"] = "cli-bound-mismatch:1"
        failure_batch_file = tmp_path / "bound-cli-failure-commands.json"
        failure_batch_file.write_text(
            json.dumps(cli_failure_batch), encoding="utf-8"
        )
        failed = runner.invoke(
            app,
            [
                "proposal",
                "create",
                managed.id,
                "--file",
                str(failure_batch_file),
                "--idempotency-key",
                "cli-bound-mismatch",
                "--json",
            ],
            env=env,
        )
        assert failed.exit_code == 0, failed.output
        failed_proposal = json.loads(failed.stdout)
        worked = runner.invoke(app, ["worker", "--once", "--json"], env=env)
        assert worked.exit_code == 0, worked.output
        failed_status = runner.invoke(
            app, ["proposal", "show", failed_proposal["id"], "--json"], env=env
        )
        assert failed_status.exit_code == 0, failed_status.output
        assert json.loads(failed_status.stdout)["status"] == "validation_failed"
        assert (
            json.loads(failed_status.stdout)["last_error_code"]
            == "COMPONENT_MODULE_BINDING_KICAD_MAJOR_MISMATCH"
        )
    finally:
        container.dispose()

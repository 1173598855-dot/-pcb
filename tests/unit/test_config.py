from pathlib import Path

import pytest

from pcbflow.config import Settings


def test_settings_derive_local_paths(tmp_path: Path) -> None:
    settings = Settings.from_env({"PCBFLOW_DATA_DIR": str(tmp_path)})

    assert settings.data_dir == tmp_path.resolve()
    assert (
        settings.database_url
        == f"sqlite+pysqlite:///{tmp_path.resolve().as_posix()}/pcbflow.db"
    )
    assert settings.artifact_dir == tmp_path.resolve() / "artifacts"
    assert settings.kicad_cli is None


def test_settings_accept_explicit_kicad_cli(tmp_path: Path) -> None:
    executable = tmp_path / "kicad-cli.exe"
    settings = Settings.from_env(
        {
            "PCBFLOW_DATA_DIR": str(tmp_path / "data"),
            "PCBFLOW_KICAD_CLI": str(executable),
        }
    )

    assert settings.kicad_cli == executable.resolve()


def test_settings_accept_explicit_lceda_pro_paths(tmp_path: Path) -> None:
    executable = tmp_path / "lceda-pro.exe"
    bridge = tmp_path / "official-bridge.exe"

    settings = Settings.from_env(
        {
            "PCBFLOW_DATA_DIR": str(tmp_path / "data"),
            "PCBFLOW_LCEDA_PRO_EXECUTABLE": str(executable),
            "PCBFLOW_LCEDA_PRO_OFFICIAL_BRIDGE": str(bridge),
        }
    )

    assert settings.lceda_pro_executable == executable.resolve()
    assert settings.lceda_pro_official_bridge == bridge.resolve()


def test_settings_create_runtime_directories(tmp_path: Path) -> None:
    settings = Settings.from_env({"PCBFLOW_DATA_DIR": str(tmp_path / "runtime")})

    settings.ensure_directories()

    assert settings.data_dir.is_dir()
    assert settings.artifact_dir.is_dir()
    assert settings.projects_dir.is_dir()
    assert settings.workspaces_dir.is_dir()


def test_settings_parse_validation_limits_and_remote_mode(tmp_path: Path) -> None:
    settings = Settings.from_env(
        {
            "PCBFLOW_DATA_DIR": str(tmp_path / "runtime"),
            "PCBFLOW_MAX_PROJECT_FILES": "321",
            "PCBFLOW_MAX_PROJECT_BYTES": "654321",
            "PCBFLOW_REMOTE_MODE": "true",
            "PCBFLOW_API_TOKEN": "remote-test-token",
            "PCBFLOW_API_ACTOR_ID": "remote-service",
            "PCBFLOW_MAX_API_BODY_BYTES": "12345",
            "PCBFLOW_MAX_KICAD_DESIGN_FILE_BYTES": "45678",
            "PCBFLOW_MAX_KICAD_REPORT_BYTES": "56789",
        }
    )

    assert settings.max_project_files == 321
    assert settings.max_project_bytes == 654321
    assert settings.remote_mode is True
    assert settings.api_token == "remote-test-token"
    assert settings.api_actor_id == "remote-service"
    assert settings.max_api_body_bytes == 12345
    assert settings.max_kicad_design_file_bytes == 45678
    assert settings.max_kicad_report_bytes == 56789


def test_settings_parse_task_retry_policy(tmp_path: Path) -> None:
    settings = Settings.from_env(
        {
            "PCBFLOW_DATA_DIR": str(tmp_path / "runtime"),
            "PCBFLOW_TASK_RETRY_MAX_ATTEMPTS": "7",
            "PCBFLOW_TASK_RETRY_BASE_SECONDS": "9",
            "PCBFLOW_TASK_RETRY_MAX_DELAY_SECONDS": "90",
        }
    )

    assert settings.task_retry_max_attempts == 7
    assert settings.task_retry_base_seconds == 9
    assert settings.task_retry_max_delay_seconds == 90


@pytest.mark.parametrize(
    ("environment", "message"),
    [
        (
            {"PCBFLOW_TASK_RETRY_MAX_ATTEMPTS": "0"},
            "task_retry_max_attempts must be positive",
        ),
        (
            {"PCBFLOW_TASK_RETRY_BASE_SECONDS": "0"},
            "task_retry_base_seconds must be positive",
        ),
        (
            {"PCBFLOW_TASK_RETRY_MAX_DELAY_SECONDS": "0"},
            "task_retry_max_delay_seconds must be positive",
        ),
        (
            {
                "PCBFLOW_TASK_RETRY_BASE_SECONDS": "31",
                "PCBFLOW_TASK_RETRY_MAX_DELAY_SECONDS": "30",
            },
            "task retry base delay must not exceed retry maximum delay",
        ),
    ],
)
def test_settings_reject_invalid_task_retry_policy(
    tmp_path: Path, environment: dict[str, str], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        Settings.from_env({"PCBFLOW_DATA_DIR": str(tmp_path), **environment})


def test_settings_require_a_token_in_remote_mode(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="remote_mode requires api_token"):
        Settings.from_env(
            {
                "PCBFLOW_DATA_DIR": str(tmp_path / "runtime"),
                "PCBFLOW_REMOTE_MODE": "true",
            }
        )


@pytest.mark.parametrize(
    ("environment", "message"),
    [
        (
            {
                "PCBFLOW_API_TOKEN": "remote-token",
                "PCBFLOW_API_ACTOR_ID": "a" * 256,
            },
            "api_actor_id must not exceed 255 characters",
        ),
        (
            {"PCBFLOW_API_TOKEN": "remote-tok\u00e9n"},
            "api_token must contain only ASCII characters",
        ),
    ],
)
def test_settings_reject_unsafe_remote_api_configuration(
    tmp_path: Path, environment: dict[str, str], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        Settings.from_env(
            {
                "PCBFLOW_DATA_DIR": str(tmp_path / "runtime"),
                "PCBFLOW_REMOTE_MODE": "true",
                **environment,
            }
        )


def test_settings_preserve_legacy_positional_module_catalog_argument(
    tmp_path: Path,
) -> None:
    catalog = tmp_path / "catalog"
    settings = Settings(
        tmp_path / "data",
        "sqlite+pysqlite:///legacy.db",
        tmp_path / "artifacts",
        None,
        180,
        120,
        2_000_000,
        10_000,
        1_000_000_000,
        False,
        catalog,
    )

    assert settings.module_catalog_dir == catalog
    assert settings.api_token is None


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("PCBFLOW_TASK_LEASE_SECONDS", "0"),
        ("PCBFLOW_PROCESS_TIMEOUT_SECONDS", "0"),
        ("PCBFLOW_MAX_PROCESS_OUTPUT_BYTES", "0"),
        ("PCBFLOW_MAX_PROJECT_FILES", "0"),
        ("PCBFLOW_MAX_PROJECT_BYTES", "0"),
        ("PCBFLOW_MAX_API_BODY_BYTES", "0"),
        ("PCBFLOW_MAX_KICAD_DESIGN_FILE_BYTES", "0"),
        ("PCBFLOW_MAX_KICAD_REPORT_BYTES", "0"),
    ],
)
def test_settings_reject_non_positive_runtime_limits(
    tmp_path: Path, name: str, value: str
) -> None:
    with pytest.raises(ValueError, match="must be positive"):
        Settings.from_env(
            {
                "PCBFLOW_DATA_DIR": str(tmp_path),
                name: value,
            }
        )


def test_settings_rejects_a_lease_shorter_than_process_timeout(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="task lease"):
        Settings.from_env(
            {
                "PCBFLOW_DATA_DIR": str(tmp_path),
                "PCBFLOW_TASK_LEASE_SECONDS": "30",
                "PCBFLOW_PROCESS_TIMEOUT_SECONDS": "60",
            }
        )


def test_settings_derive_managed_paths_and_optional_module_catalog(
    tmp_path: Path,
) -> None:
    settings = Settings.from_env(
        {
            "PCBFLOW_DATA_DIR": str(tmp_path / "data"),
            "PCBFLOW_MODULE_CATALOG_DIR": str(tmp_path / "modules"),
        }
    )

    assert settings.projects_dir == (tmp_path / "data" / "projects").resolve()
    assert settings.workspaces_dir == (tmp_path / "data" / "workspaces").resolve()
    assert settings.module_catalog_dir == (tmp_path / "modules").resolve()


@pytest.mark.parametrize("via_environment", [True, False])
def test_settings_reject_non_sqlite_database_urls(
    tmp_path: Path, via_environment: bool
) -> None:
    database_url = "postgresql+psycopg://localhost/pcbflow"

    with pytest.raises(ValueError, match="^database_url must use SQLite$"):
        if via_environment:
            Settings.from_env(
                {
                    "PCBFLOW_DATA_DIR": str(tmp_path),
                    "PCBFLOW_DATABASE_URL": database_url,
                }
            )
        else:
            Settings(
                data_dir=tmp_path,
                database_url=database_url,
                artifact_dir=tmp_path / "artifacts",
                kicad_cli=None,
            )


def test_worker_settings_have_sensible_defaults(tmp_path: Path) -> None:
    settings = Settings.from_env({"PCBFLOW_DATA_DIR": str(tmp_path)})
    assert settings.worker_slots == 1
    assert settings.worker_poll_seconds == 5
    assert settings.worker_poll_max_seconds == 60
    assert settings.worker_heartbeat_seconds == 30
    assert settings.worker_shutdown_timeout_seconds == 300
    assert settings.worker_id is None


def test_worker_settings_parse_from_environment(tmp_path: Path) -> None:
    settings = Settings.from_env(
        {
            "PCBFLOW_DATA_DIR": str(tmp_path),
            "PCBFLOW_WORKER_SLOTS": "3",
            "PCBFLOW_WORKER_POLL_SECONDS": "10",
            "PCBFLOW_WORKER_POLL_MAX_SECONDS": "120",
            "PCBFLOW_WORKER_HEARTBEAT_SECONDS": "45",
            "PCBFLOW_WORKER_SHUTDOWN_TIMEOUT_SECONDS": "600",
            "PCBFLOW_WORKER_ID": "test-worker-1",
        }
    )
    assert settings.worker_slots == 3
    assert settings.worker_poll_seconds == 10
    assert settings.worker_poll_max_seconds == 120
    assert settings.worker_heartbeat_seconds == 45
    assert settings.worker_shutdown_timeout_seconds == 600
    assert settings.worker_id == "test-worker-1"


def test_worker_settings_adapt_default_heartbeat_to_a_short_lease(
    tmp_path: Path,
) -> None:
    settings = Settings.from_env(
        {
            "PCBFLOW_DATA_DIR": str(tmp_path),
            "PCBFLOW_TASK_LEASE_SECONDS": "30",
            "PCBFLOW_PROCESS_TIMEOUT_SECONDS": "20",
        }
    )

    assert settings.worker_heartbeat_seconds == 10


def test_worker_settings_reject_invalid_slot_count(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="worker_slots must be between 1 and 10"):
        Settings.from_env(
            {
                "PCBFLOW_DATA_DIR": str(tmp_path),
                "PCBFLOW_WORKER_SLOTS": "0",
            }
        )
    with pytest.raises(ValueError, match="worker_slots must be between 1 and 10"):
        Settings.from_env(
            {
                "PCBFLOW_DATA_DIR": str(tmp_path),
                "PCBFLOW_WORKER_SLOTS": "11",
            }
        )


def test_worker_settings_reject_invalid_poll_intervals(tmp_path: Path) -> None:
    with pytest.raises(
        ValueError, match="worker_poll_seconds must not exceed worker_poll_max_seconds"
    ):
        Settings.from_env(
            {
                "PCBFLOW_DATA_DIR": str(tmp_path),
                "PCBFLOW_WORKER_POLL_SECONDS": "70",
                "PCBFLOW_WORKER_POLL_MAX_SECONDS": "60",
            }
        )


def test_worker_settings_reject_excessive_heartbeat(tmp_path: Path) -> None:
    with pytest.raises(
        ValueError, match="worker_heartbeat_seconds must be less than half"
    ):
        Settings.from_env(
            {
                "PCBFLOW_DATA_DIR": str(tmp_path),
                "PCBFLOW_TASK_LEASE_SECONDS": "120",
                "PCBFLOW_PROCESS_TIMEOUT_SECONDS": "60",
                "PCBFLOW_WORKER_HEARTBEAT_SECONDS": "70",
            }
        )


def test_worker_settings_reject_invalid_shutdown_timeout(tmp_path: Path) -> None:
    with pytest.raises(
        ValueError, match="worker_shutdown_timeout_seconds must be between 60 and 600"
    ):
        Settings.from_env(
            {
                "PCBFLOW_DATA_DIR": str(tmp_path),
                "PCBFLOW_WORKER_SHUTDOWN_TIMEOUT_SECONDS": "30",
            }
        )
    with pytest.raises(
        ValueError, match="worker_shutdown_timeout_seconds must be between 60 and 600"
    ):
        Settings.from_env(
            {
                "PCBFLOW_DATA_DIR": str(tmp_path),
                "PCBFLOW_WORKER_SHUTDOWN_TIMEOUT_SECONDS": "700",
            }
        )

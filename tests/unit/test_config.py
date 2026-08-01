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
        }
    )

    assert settings.max_project_files == 321
    assert settings.max_project_bytes == 654321
    assert settings.remote_mode is True


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("PCBFLOW_TASK_LEASE_SECONDS", "0"),
        ("PCBFLOW_PROCESS_TIMEOUT_SECONDS", "0"),
        ("PCBFLOW_MAX_PROCESS_OUTPUT_BYTES", "0"),
        ("PCBFLOW_MAX_PROJECT_FILES", "0"),
        ("PCBFLOW_MAX_PROJECT_BYTES", "0"),
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

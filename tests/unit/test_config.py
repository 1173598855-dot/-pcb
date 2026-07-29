from pathlib import Path

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

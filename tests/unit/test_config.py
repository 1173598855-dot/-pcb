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

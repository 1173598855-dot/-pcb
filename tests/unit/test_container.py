from pathlib import Path

import pytest

from pcbflow.config import Settings
from pcbflow.container import build_container
from pcbflow.kicad import KicadCapability, KicadCli, KicadPort, RawValidationReport


class _KicadOverride:
    def probe(self) -> KicadCapability:
        return KicadCapability(False, None, None, None, "fixture")

    def validate(
        self, project_dir: Path, output_dir: Path
    ) -> tuple[RawValidationReport, ...]:
        return ()


def _settings(tmp_path: Path) -> Settings:
    return Settings.from_env(
        {
            "PCBFLOW_DATA_DIR": str(tmp_path / "service-data"),
            "PCBFLOW_DATABASE_URL": (
                f"sqlite+pysqlite:///{(tmp_path / 'pcbflow.db').as_posix()}"
            ),
        }
    )


def test_build_container_does_not_locate_kicad_when_an_override_is_supplied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_if_constructed(*args: object, **kwargs: object) -> None:
        raise AssertionError("KicadCli must not be constructed for an injected port")

    def fail_if_located(*args: object, **kwargs: object) -> Path | None:
        raise AssertionError("KicadCli.locate must not run for an injected port")

    monkeypatch.setattr(KicadCli, "__init__", fail_if_constructed)
    monkeypatch.setattr(KicadCli, "locate", fail_if_located)
    override: KicadPort = _KicadOverride()

    services = build_container(_settings(tmp_path), kicad_override=override)
    try:
        assert services.kicad is override
    finally:
        services.dispose()

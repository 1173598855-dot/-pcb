from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True, slots=True)
class Settings:
    data_dir: Path
    database_url: str
    artifact_dir: Path
    kicad_cli: Path | None
    task_lease_seconds: int = 60
    process_timeout_seconds: int = 120
    max_process_output_bytes: int = 2_000_000

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> Settings:
        values = os.environ if environ is None else environ
        default_data_dir = (cwd or Path.cwd()) / ".pcbflow-data"
        data_dir = Path(values.get("PCBFLOW_DATA_DIR", str(default_data_dir))).resolve()
        configured_cli = values.get("PCBFLOW_KICAD_CLI")

        return cls(
            data_dir=data_dir,
            database_url=values.get(
                "PCBFLOW_DATABASE_URL",
                f"sqlite+pysqlite:///{data_dir.as_posix()}/pcbflow.db",
            ),
            artifact_dir=Path(
                values.get("PCBFLOW_ARTIFACT_DIR", str(data_dir / "artifacts"))
            ).resolve(),
            kicad_cli=Path(configured_cli).resolve() if configured_cli else None,
            task_lease_seconds=int(values.get("PCBFLOW_TASK_LEASE_SECONDS", "60")),
            process_timeout_seconds=int(
                values.get("PCBFLOW_PROCESS_TIMEOUT_SECONDS", "120")
            ),
            max_process_output_bytes=int(
                values.get("PCBFLOW_MAX_PROCESS_OUTPUT_BYTES", "2000000")
            ),
        )

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)

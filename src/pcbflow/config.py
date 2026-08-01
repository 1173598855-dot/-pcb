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
    task_lease_seconds: int = 180
    process_timeout_seconds: int = 120
    max_process_output_bytes: int = 2_000_000
    max_project_files: int = 10_000
    max_project_bytes: int = 1_000_000_000
    remote_mode: bool = False
    module_catalog_dir: Path | None = None
    api_token: str | None = None
    api_actor_id: str = "remote-api"
    max_api_body_bytes: int = 1_000_000

    def __post_init__(self) -> None:
        scheme = self.database_url.split(":", 1)[0]
        if scheme.split("+", 1)[0] != "sqlite":
            raise ValueError("database_url must use SQLite")
        limits = {
            "task_lease_seconds": self.task_lease_seconds,
            "process_timeout_seconds": self.process_timeout_seconds,
            "max_process_output_bytes": self.max_process_output_bytes,
            "max_project_files": self.max_project_files,
            "max_project_bytes": self.max_project_bytes,
            "max_api_body_bytes": self.max_api_body_bytes,
        }
        for name, value in limits.items():
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.task_lease_seconds < self.process_timeout_seconds:
            raise ValueError(
                "task lease must be at least as long as process timeout"
            )
        if self.remote_mode and not self.api_token:
            raise ValueError("remote_mode requires api_token")
        if not self.api_actor_id:
            raise ValueError("api_actor_id must not be empty")

    @property
    def projects_dir(self) -> Path:
        return (self.data_dir / "projects").resolve()

    @property
    def workspaces_dir(self) -> Path:
        return (self.data_dir / "workspaces").resolve()

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
        configured_catalog = values.get("PCBFLOW_MODULE_CATALOG_DIR")

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
            module_catalog_dir=(
                Path(configured_catalog).resolve() if configured_catalog else None
            ),
            task_lease_seconds=int(values.get("PCBFLOW_TASK_LEASE_SECONDS", "180")),
            process_timeout_seconds=int(
                values.get("PCBFLOW_PROCESS_TIMEOUT_SECONDS", "120")
            ),
            max_process_output_bytes=int(
                values.get("PCBFLOW_MAX_PROCESS_OUTPUT_BYTES", "2000000")
            ),
            max_project_files=int(
                values.get("PCBFLOW_MAX_PROJECT_FILES", "10000")
            ),
            max_project_bytes=int(
                values.get("PCBFLOW_MAX_PROJECT_BYTES", "1000000000")
            ),
            remote_mode=values.get("PCBFLOW_REMOTE_MODE", "false").lower()
            in {"1", "true", "yes", "on"},
            api_token=values.get("PCBFLOW_API_TOKEN") or None,
            api_actor_id=values.get("PCBFLOW_API_ACTOR_ID", "remote-api"),
            max_api_body_bytes=int(
                values.get("PCBFLOW_MAX_API_BODY_BYTES", "1000000")
            ),
        )

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.projects_dir.mkdir(parents=True, exist_ok=True)
        self.workspaces_dir.mkdir(parents=True, exist_ok=True)

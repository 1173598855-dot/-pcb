from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


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
    lceda_pro_executable: Path | None = None
    lceda_pro_official_bridge: Path | None = None
    api_token: str | None = None
    api_actor_id: str = "remote-api"
    max_api_body_bytes: int = 1_000_000
    task_retry_max_attempts: int = 5
    task_retry_base_seconds: int = 5
    task_retry_max_delay_seconds: int = 300
    max_kicad_design_file_bytes: int = 50_000_000
    max_kicad_report_bytes: int = 10_000_000
    worker_slots: int = 1
    worker_poll_seconds: int = 5
    worker_poll_max_seconds: int = 60
    worker_heartbeat_seconds: float = 30
    worker_shutdown_timeout_seconds: int = 300
    worker_id: str | None = None

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
            "task_retry_max_attempts": self.task_retry_max_attempts,
            "task_retry_base_seconds": self.task_retry_base_seconds,
            "task_retry_max_delay_seconds": self.task_retry_max_delay_seconds,
            "max_kicad_design_file_bytes": self.max_kicad_design_file_bytes,
            "max_kicad_report_bytes": self.max_kicad_report_bytes,
        }
        for name, value in limits.items():
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.task_lease_seconds < self.process_timeout_seconds:
            raise ValueError(
                "task lease must be at least as long as process timeout"
            )
        if self.task_retry_base_seconds > self.task_retry_max_delay_seconds:
            raise ValueError(
                "task retry base delay must not exceed retry maximum delay"
            )
        if not (1 <= self.worker_slots <= 10):
            raise ValueError("worker_slots must be between 1 and 10")
        if self.worker_poll_seconds > self.worker_poll_max_seconds:
            raise ValueError(
                "worker_poll_seconds must not exceed worker_poll_max_seconds"
            )
        if self.worker_heartbeat_seconds <= 0:
            raise ValueError("worker_heartbeat_seconds must be positive")
        if self.worker_heartbeat_seconds >= self.task_lease_seconds / 2:
            raise ValueError(
                "worker_heartbeat_seconds must be less than half the lease duration"
            )
        if not (60 <= self.worker_shutdown_timeout_seconds <= 600):
            raise ValueError(
                "worker_shutdown_timeout_seconds must be between 60 and 600"
            )
        if self.remote_mode and not self.api_token:
            raise ValueError("remote_mode requires api_token")
        if not self.api_actor_id:
            raise ValueError("api_actor_id must not be empty")
        if len(self.api_actor_id) > 255:
            raise ValueError("api_actor_id must not exceed 255 characters")
        if self.api_token is not None and not self.api_token.isascii():
            raise ValueError("api_token must contain only ASCII characters")

    @property
    def projects_dir(self) -> Path:
        return (self.data_dir / "projects").resolve()

    @property
    def workspaces_dir(self) -> Path:
        return (self.data_dir / "workspaces").resolve()

    @property
    def worker_state_file(self) -> Path:
        return (self.data_dir / "worker-state.json").resolve()

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
        configured_lceda_pro = values.get("PCBFLOW_LCEDA_PRO_EXECUTABLE")
        configured_lceda_bridge = values.get("PCBFLOW_LCEDA_PRO_OFFICIAL_BRIDGE")
        task_lease_seconds = int(values.get("PCBFLOW_TASK_LEASE_SECONDS", "180"))
        configured_heartbeat = values.get("PCBFLOW_WORKER_HEARTBEAT_SECONDS")
        worker_heartbeat_seconds = (
            float(configured_heartbeat)
            if configured_heartbeat is not None
            else min(30.0, task_lease_seconds / 3)
        )

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
            lceda_pro_executable=(
                Path(configured_lceda_pro).resolve() if configured_lceda_pro else None
            ),
            lceda_pro_official_bridge=(
                Path(configured_lceda_bridge).resolve()
                if configured_lceda_bridge
                else None
            ),
            task_lease_seconds=task_lease_seconds,
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
            task_retry_max_attempts=int(
                values.get("PCBFLOW_TASK_RETRY_MAX_ATTEMPTS", "5")
            ),
            task_retry_base_seconds=int(
                values.get("PCBFLOW_TASK_RETRY_BASE_SECONDS", "5")
            ),
            task_retry_max_delay_seconds=int(
                values.get("PCBFLOW_TASK_RETRY_MAX_DELAY_SECONDS", "300")
            ),
            max_kicad_design_file_bytes=int(
                values.get("PCBFLOW_MAX_KICAD_DESIGN_FILE_BYTES", "50000000")
            ),
            max_kicad_report_bytes=int(
                values.get("PCBFLOW_MAX_KICAD_REPORT_BYTES", "10000000")
            ),
            worker_slots=int(values.get("PCBFLOW_WORKER_SLOTS", "1")),
            worker_poll_seconds=int(values.get("PCBFLOW_WORKER_POLL_SECONDS", "5")),
            worker_poll_max_seconds=int(
                values.get("PCBFLOW_WORKER_POLL_MAX_SECONDS", "60")
            ),
            worker_heartbeat_seconds=worker_heartbeat_seconds,
            worker_shutdown_timeout_seconds=int(
                values.get("PCBFLOW_WORKER_SHUTDOWN_TIMEOUT_SECONDS", "300")
            ),
            worker_id=values.get("PCBFLOW_WORKER_ID") or None,
        )

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.projects_dir.mkdir(parents=True, exist_ok=True)
        self.workspaces_dir.mkdir(parents=True, exist_ok=True)

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from pcbflow.artifacts import ContentAddressedStore
from pcbflow.config import Settings
from pcbflow.db import create_engine_and_session
from pcbflow.domain import new_id, utc_now
from pcbflow.kicad import KicadCli, KicadPort
from pcbflow.process import ProcessRunner
from pcbflow.repositories import (
    EvidenceRepository,
    FindingRepository,
    ProjectRepository,
    TaskRepository,
)
from pcbflow.tasks import Worker
from pcbflow.validation import (
    VALIDATION_TASK_KIND,
    ValidationService,
    ValidationTaskHandler,
)


@dataclass(frozen=True, slots=True)
class Container:
    settings: Settings
    engine: Engine
    sessions: sessionmaker[Session]
    projects: ProjectRepository
    tasks: TaskRepository
    evidence: EvidenceRepository
    findings: FindingRepository
    artifacts: ContentAddressedStore
    kicad: KicadCli
    validation: ValidationService
    worker: Worker

    def dispose(self) -> None:
        self.engine.dispose()


def _run_migrations(database_url: str) -> None:
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    command.upgrade(config, "head")


def build_container(
    settings: Settings,
    kicad_override: KicadPort | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> Container:
    settings.ensure_directories()
    _run_migrations(settings.database_url)
    engine, sessions = create_engine_and_session(settings.database_url)

    projects = ProjectRepository(sessions)
    tasks = TaskRepository(sessions)
    evidence = EvidenceRepository(sessions)
    findings = FindingRepository(sessions)
    artifacts = ContentAddressedStore(settings.artifact_dir)
    runner = ProcessRunner(settings.max_process_output_bytes)
    kicad = KicadCli(
        runner,
        KicadCli.locate(settings.kicad_cli),
        settings.process_timeout_seconds,
    )
    handler = ValidationTaskHandler(
        projects,
        evidence,
        findings,
        artifacts,
        kicad_override if kicad_override is not None else kicad,
        max_files=settings.max_project_files,
        max_bytes=settings.max_project_bytes,
    )
    validation = ValidationService(projects, tasks)
    worker = Worker(
        tasks,
        new_id("wrk"),
        {VALIDATION_TASK_KIND: handler},
        clock,
        settings.task_lease_seconds,
    )
    return Container(
        settings=settings,
        engine=engine,
        sessions=sessions,
        projects=projects,
        tasks=tasks,
        evidence=evidence,
        findings=findings,
        artifacts=artifacts,
        kicad=kicad,
        validation=validation,
        worker=worker,
    )

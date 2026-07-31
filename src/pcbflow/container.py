from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from pcbflow.approvals import ApprovalService, GateDecisionStore
from pcbflow.artifacts import ContentAddressedStore
from pcbflow.config import Settings
from pcbflow.db import create_engine_and_session
from pcbflow.domain import new_id, utc_now
from pcbflow.kicad import KicadCli, KicadPort
from pcbflow.process import ProcessRunner
from pcbflow.proposal_store import CommandBatchStore, ProposalStore
from pcbflow.proposals import ProposalService, ProposalExecutor, ProposalDecisionService, DESIGN_PROPOSAL_TASK_KIND
from pcbflow.schematic.adapter import CstSchematicAdapter
from pcbflow.schematic.modules import FileModuleCatalog
from pcbflow.revision_store import ProjectRevisionStore
from pcbflow.repositories import (
    EvidenceRepository,
    FindingRepository,
    ProjectRepository,
    TaskRepository,
)
from pcbflow.requirement_store import RequirementService, RequirementStore
from pcbflow.revisions import GitCli, RevisionReconciler, RevisionService
from pcbflow.tasks import Worker
from pcbflow.validation import (
    VALIDATION_TASK_KIND,
    ValidationService,
    ValidationTaskHandler,
)
from pcbflow.workspaces import WorkspaceCopier


@dataclass(frozen=True, slots=True)
class Container:
    settings: Settings
    engine: Engine
    sessions: sessionmaker[Session]
    projects: ProjectRepository
    revision_store: ProjectRevisionStore
    revisions: RevisionService
    requirement_store: RequirementStore
    gate_decisions: GateDecisionStore
    requirements: RequirementService
    approvals: ApprovalService
    reconciler: RevisionReconciler
    tasks: TaskRepository
    command_batches: CommandBatchStore
    proposal_store: ProposalStore
    proposals: ProposalService
    evidence: EvidenceRepository
    findings: FindingRepository
    artifacts: ContentAddressedStore
    kicad: KicadPort
    validation: ValidationService
    worker: Worker
    proposal_executor: ProposalExecutor
    proposal_decisions: ProposalDecisionService

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
    command_batches = CommandBatchStore(sessions)
    proposal_store = ProposalStore(sessions, clock)
    evidence = EvidenceRepository(sessions)
    findings = FindingRepository(sessions)
    artifacts = ContentAddressedStore(settings.artifact_dir)
    runner = ProcessRunner(settings.max_process_output_bytes)
    revision_store = ProjectRevisionStore(sessions)
    git = GitCli(runner, timeout_seconds=settings.process_timeout_seconds)
    workspace_copier = WorkspaceCopier(
        max_files=settings.max_project_files,
        max_bytes=settings.max_project_bytes,
    )
    revisions = RevisionService(
        projects=projects,
        revision_store=revision_store,
        git=git,
        copier=workspace_copier,
        projects_dir=settings.projects_dir,
        workspaces_dir=settings.workspaces_dir,
    )
    requirement_store = RequirementStore(sessions, artifacts)
    gate_decisions = GateDecisionStore(sessions, artifacts)
    reconciler = RevisionReconciler(
        projects, revision_store, revisions,
        requirements=requirement_store,
        proposals=proposal_store,
        sessions=sessions,
        clock=clock,
    )
    requirements = RequirementService(
        requirement_store, projects, revisions, reconciler=reconciler
    )
    proposals = ProposalService(
        projects, requirement_store, proposal_store, reconciler=reconciler
    )
    approvals = ApprovalService(
        requirement_store,
        projects,
        gate_decisions,
        reconciler,
    )
    kicad = KicadCli(
        runner,
        KicadCli.locate(settings.kicad_cli),
        settings.process_timeout_seconds,
    )
    selected_kicad: KicadPort = kicad_override if kicad_override is not None else kicad
    module_catalog = (FileModuleCatalog(settings.module_catalog_dir, max_files=settings.max_project_files, max_bytes=settings.max_project_bytes) if settings.module_catalog_dir is not None else None)
    adapter = CstSchematicAdapter(module_catalog)
    proposal_executor = ProposalExecutor(proposal_store=proposal_store, command_batches=command_batches, projects=projects, requirements=requirement_store, tasks=tasks, revisions=revisions, adapter=adapter, kicad=selected_kicad, artifacts=artifacts, evidence=evidence, clock=clock, max_files=settings.max_project_files, max_bytes=settings.max_project_bytes)
    proposal_decisions = ProposalDecisionService(
        proposal_store=proposal_store,
        command_batches=command_batches,
        projects=projects,
        requirements=requirement_store,
        revisions=revisions,
        artifacts=artifacts,
        evidence=evidence,
        reconciler=reconciler,
        clock=clock,
    )
    handler = ValidationTaskHandler(
        projects,
        evidence,
        findings,
        artifacts,
        selected_kicad,
        max_files=settings.max_project_files,
        max_bytes=settings.max_project_bytes,
    )
    validation = ValidationService(projects, tasks)
    worker = Worker(
        tasks,
        new_id("wrk"),
        {VALIDATION_TASK_KIND: handler, DESIGN_PROPOSAL_TASK_KIND: proposal_executor},
        clock,
        settings.task_lease_seconds,
    )
    container = Container(
        settings=settings,
        engine=engine,
        sessions=sessions,
        projects=projects,
        revision_store=revision_store,
        revisions=revisions,
        requirement_store=requirement_store,
        gate_decisions=gate_decisions,
        requirements=requirements,
        approvals=approvals,
        reconciler=reconciler,
        tasks=tasks,
        command_batches=command_batches,
        proposal_store=proposal_store,
        proposals=proposals,
        evidence=evidence,
        findings=findings,
        artifacts=artifacts,
        kicad=selected_kicad,
        validation=validation,
        worker=worker,
        proposal_executor=proposal_executor,
        proposal_decisions=proposal_decisions,
    )
    try:
        reconciler.run_once()
    except Exception:
        container.dispose()
        raise
    return container

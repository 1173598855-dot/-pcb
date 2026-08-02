from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import time

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from pcbflow.approvals import ApprovalService, GateDecisionStore
from pcbflow.artifacts import ContentAddressedStore
from pcbflow.component_binding_store import ComponentModuleBindingStore
from pcbflow.component_bindings import ComponentModuleBindingService
from pcbflow.component_store import ComponentRevisionStore
from pcbflow.components import ComponentRevisionService
from pcbflow.config import Settings
from pcbflow.db import create_engine_and_session
from pcbflow.domain import new_id, utc_now
from pcbflow.observability import Metrics
from pcbflow.kicad import KicadCli, KicadPort
from pcbflow.process import ProcessRunner
from pcbflow.proposal_store import CommandBatchStore, ProposalStore
from pcbflow.proposals import (
    DESIGN_PROPOSAL_TASK_KIND,
    FaultInjector,
    NoFaults,
    ProposalDecisionService,
    ProposalExecutor,
    ProposalService,
)
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
    component_store: ComponentRevisionStore
    component_module_binding_store: ComponentModuleBindingStore
    components: ComponentRevisionService
    component_module_bindings: ComponentModuleBindingService
    kicad: KicadPort
    validation: ValidationService
    worker: Worker
    proposal_executor: ProposalExecutor
    proposal_decisions: ProposalDecisionService
    metrics: Metrics

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
    *,
    metrics: Metrics | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    faults: FaultInjector | None = None,
) -> Container:
    settings.ensure_directories()
    _run_migrations(settings.database_url)
    engine, sessions = create_engine_and_session(settings.database_url)
    metric_sink = metrics if metrics is not None else Metrics()
    fault_injector = faults if faults is not None else NoFaults()

    projects = ProjectRepository(sessions)
    tasks = TaskRepository(
        sessions,
        max_attempts=settings.task_retry_max_attempts,
        retry_base_seconds=settings.task_retry_base_seconds,
        retry_max_delay_seconds=settings.task_retry_max_delay_seconds,
    )
    command_batches = CommandBatchStore(sessions)
    proposal_store = ProposalStore(sessions, clock)
    evidence = EvidenceRepository(sessions)
    findings = FindingRepository(sessions)
    artifacts = ContentAddressedStore(settings.artifact_dir)
    component_store = ComponentRevisionStore(sessions)
    component_module_binding_store = ComponentModuleBindingStore(sessions)
    components = ComponentRevisionService(
        component_store, artifacts, max_bytes=settings.max_project_bytes
    )
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
        max_files=settings.max_project_files,
        max_bytes=settings.max_project_bytes,
    )
    requirement_store = RequirementStore(sessions, artifacts)
    gate_decisions = GateDecisionStore(sessions, artifacts, metrics=metric_sink)
    reconciler = RevisionReconciler(
        projects, revision_store, revisions,
        requirements=requirement_store,
        proposals=proposal_store,
        sessions=sessions,
        clock=clock,
        metrics=metric_sink,
    )
    requirements = RequirementService(
        requirement_store, projects, revisions, reconciler=reconciler, metrics=metric_sink
    )
    proposals = ProposalService(
        projects, requirement_store, proposal_store, reconciler=reconciler, metrics=metric_sink
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
        max_design_file_bytes=settings.max_kicad_design_file_bytes,
        max_report_bytes=settings.max_kicad_report_bytes,
    )
    selected_kicad: KicadPort = kicad_override if kicad_override is not None else kicad
    module_catalog = (FileModuleCatalog(settings.module_catalog_dir, max_files=settings.max_project_files, max_bytes=settings.max_project_bytes) if settings.module_catalog_dir is not None else None)
    component_module_bindings = ComponentModuleBindingService(
        component_store, component_module_binding_store, module_catalog
    )
    adapter = CstSchematicAdapter(
        module_catalog,
        bound_module_resolver=component_module_bindings,
        metrics=metric_sink,
        monotonic=monotonic,
    )
    proposal_executor = ProposalExecutor(proposal_store=proposal_store, command_batches=command_batches, projects=projects, requirements=requirement_store, tasks=tasks, revisions=revisions, adapter=adapter, kicad=selected_kicad, artifacts=artifacts, evidence=evidence, clock=clock, metrics=metric_sink, monotonic=monotonic, faults=fault_injector, max_files=settings.max_project_files, max_bytes=settings.max_project_bytes)
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
        metrics=metric_sink,
        faults=fault_injector,
    )
    handler = ValidationTaskHandler(
        projects,
        evidence,
        findings,
        artifacts,
        selected_kicad,
        metrics=metric_sink,
        monotonic=monotonic,
        revisions=revisions,
        copier=workspace_copier,
        tasks=tasks,
        clock=clock,
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
        component_store=component_store,
        component_module_binding_store=component_module_binding_store,
        components=components,
        component_module_bindings=component_module_bindings,
        kicad=selected_kicad,
        validation=validation,
        worker=worker,
        proposal_executor=proposal_executor,
        proposal_decisions=proposal_decisions,
        metrics=metric_sink,
    )
    try:
        reconciler.run_once()
    except Exception:
        container.dispose()
        raise
    return container

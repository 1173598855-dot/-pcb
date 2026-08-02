from __future__ import annotations

import time
import os
import stat
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from pcbflow.artifacts import ContentAddressedStore
from pcbflow.domain import ProjectMode, Task, TaskLease, utc_now
from pcbflow.observability import MetricName, Metrics, ensure_trace_id
from pcbflow.kicad import (
    KicadPort,
    KicadDesignFormatError,
    KicadInputLimitError,
    KicadOperationUnsupportedError,
    KicadProjectNotFoundError,
    KicadToolError,
    KicadUnavailableError,
    parse_kicad_report,
)
from pcbflow.repositories import (
    EvidenceRepository,
    FindingRepository,
    ProjectRepository,
    TaskRepository,
)
from pcbflow.tasks import RetryableTaskError, TerminalTaskError
from pcbflow.workspaces import (
    WorkspaceCopier,
    WorkspaceEntryError,
    WorkspaceLimitError,
    WorkspaceLinkError,
)
from pcbflow.revisions import RevisionService

VALIDATION_TASK_KIND = "kicad.read_only_validation"


class ProjectCopyLimitError(TerminalTaskError):
    pass


class ProjectLinkError(TerminalTaskError):
    pass


def _validation_tool_identity(reports) -> tuple[str, str, str, int]:
    if not reports:
        raise TerminalTaskError("KICAD_NO_REPORTS", "validation produced no reports")
    identity = (
        reports[0].tool_version,
        reports[0].executable_digest,
        reports[0].profile_id,
        reports[0].profile_revision,
    )
    if any(
        (
            report.tool_version,
            report.executable_digest,
            report.profile_id,
            report.profile_revision,
        )
        != identity
        for report in reports[1:]
    ):
        raise TerminalTaskError(
            "KICAD_TOOL_IDENTITY_MISMATCH",
            "validation reports use different KiCad tool identities",
        )
    return identity


def assert_project_tree_safe(root: Path, *, max_files: int, max_bytes: int) -> None:
    """Reject links, reparse points, escapes, and oversized candidate trees."""
    root = root.resolve(strict=True)
    file_count = 0
    total_bytes = 0
    for directory, directories, files in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        for name in [*directories, *files]:
            path = directory_path / name
            metadata = path.lstat()
            attributes = getattr(metadata, "st_file_attributes", 0)
            if path.is_symlink() or attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ProjectLinkError(
                    "PROJECT_LINK_NOT_ALLOWED", f"candidate contains link: {path}"
                )
            try:
                path.resolve(strict=True).relative_to(root)
            except ValueError as error:
                raise ProjectLinkError(
                    "PROJECT_PATH_OUTSIDE_WORKTREE", f"candidate path escapes worktree: {path}"
                ) from error
        for name in files:
            path = directory_path / name
            file_count += 1
            total_bytes += path.stat().st_size
            if file_count > max_files:
                raise ProjectCopyLimitError(
                    "PROJECT_FILE_LIMIT_EXCEEDED",
                    f"candidate contains more than {max_files} files",
                )
            if total_bytes > max_bytes:
                raise ProjectCopyLimitError(
                    "PROJECT_SIZE_LIMIT_EXCEEDED",
                    f"candidate exceeds {max_bytes} bytes",
                )


class ValidationService:
    def __init__(
        self, projects: ProjectRepository, tasks: TaskRepository
    ) -> None:
        self._projects = projects
        self._tasks = tasks

    def enqueue(self, project_id: str, idempotency_key: str) -> Task:
        self._projects.get(project_id)
        return self._tasks.enqueue(
            VALIDATION_TASK_KIND,
            {"project_id": project_id, "trace_id": ensure_trace_id()},
            idempotency_key,
            project_id,
        )


class ValidationTaskHandler:
    def __init__(
        self,
        projects: ProjectRepository,
        evidence: EvidenceRepository,
        findings: FindingRepository,
        store: ContentAddressedStore,
        kicad: KicadPort,
        *,
        max_files: int,
        max_bytes: int,
        metrics: Metrics | None = None,
        monotonic=time.monotonic,
        revisions: RevisionService | None = None,
        copier: WorkspaceCopier | None = None,
        tasks: TaskRepository | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._projects = projects
        self._evidence = evidence
        self._findings = findings
        self._store = store
        self._kicad = kicad
        self._max_files = max_files
        self._max_bytes = max_bytes
        self._metrics = metrics
        self._monotonic = monotonic
        self._revisions = revisions
        self._tasks = tasks
        self._clock = clock
        self._copier = copier or WorkspaceCopier(
            max_files=max_files, max_bytes=max_bytes
        )

    def __call__(self, lease: TaskLease) -> dict[str, object]:
        project_id = str(lease.payload["project_id"])
        project = self._projects.get(project_id)
        self._assert_active(lease)
        workspace_root = self._store.root.parent / "workspaces"
        workspace_root.mkdir(parents=True, exist_ok=True)
        with self._validation_workspace(project, lease.task_id, workspace_root) as (
            workspace,
            output,
        ):
            try:
                started = self._monotonic()
                reports = self._kicad.validate(workspace, output)
            except KicadUnavailableError as error:
                raise TerminalTaskError("KICAD_CLI_UNAVAILABLE", str(error)) from error
            except KicadProjectNotFoundError as error:
                raise TerminalTaskError("INVALID_KICAD_PROJECT", str(error)) from error
            except KicadInputLimitError as error:
                raise TerminalTaskError(error.code, str(error)) from error
            except (KicadDesignFormatError, KicadOperationUnsupportedError) as error:
                raise TerminalTaskError(error.code, str(error)) from error
            except KicadToolError as error:
                raise RetryableTaskError("KICAD_TOOL_FAILED", str(error)) from error
            finally:
                if self._metrics is not None:
                    self._metrics.observe(
                        MetricName.KICAD_ERC_SECONDS,
                        max(0.0, self._monotonic() - started),
                    )

            evidence_ids: list[str] = []
            finding_count = 0
            tool_identity = _validation_tool_identity(reports)
            for raw in reports:
                self._assert_active(lease)
                parsed = parse_kicad_report(raw.kind, raw.data)
                descriptor = self._store.put_bytes(raw.data, "application/json")
                fence = (
                    {"lease_token": lease.lease_token, "now": self._clock()}
                    if self._tasks is not None
                    else {}
                )
                record = self._evidence.add_report(
                    project_id=project_id,
                    task_id=lease.task_id,
                    descriptor=descriptor,
                    kind=f"kicad_{raw.kind}",
                    subject=project_id,
                    verdict="fail" if parsed.findings else "pass",
                    **fence,
                )
                self._findings.add_many(
                    project_id,
                    lease.task_id,
                    record.id,
                    parsed.findings,
                    **fence,
                )
                evidence_ids.append(record.id)
                finding_count += len(parsed.findings)
        return {
            "evidence_ids": evidence_ids,
            "finding_count": finding_count,
            "tool": {
                "version": tool_identity[0],
                "executable_digest": tool_identity[1],
                "profile_id": tool_identity[2],
                "profile_revision": tool_identity[3],
            },
        }

    def _assert_active(self, lease: TaskLease) -> None:
        if self._tasks is not None:
            self._tasks.assert_active(lease.task_id, lease.lease_token, self._clock())

    @contextmanager
    def _validation_workspace(
        self, project, task_id: str, workspace_root: Path
    ):
        """Select exactly one immutable input for a validation attempt."""
        if project.mode is ProjectMode.MANAGED:
            if self._revisions is None or project.current_revision is None:
                raise TerminalTaskError(
                    "REVISION_RECONCILIATION_REQUIRED", project.id
                )
            with TemporaryDirectory(
                prefix="pcbflow-validation-output-", dir=workspace_root
            ) as output_temp:
                output = Path(output_temp) / "output"
                with self._revisions.materialize(
                    project.id,
                    project.current_revision,
                    f"validation-{task_id}",
                ) as workspace:
                    yield workspace, output
            return

        try:
            with TemporaryDirectory(
                prefix="pcbflow-validation-", dir=workspace_root
            ) as temporary:
                temporary_path = Path(temporary)
                workspace = temporary_path / "project"
                output = temporary_path / "output"
                self._copier.copy(project.source_path, workspace)
                yield workspace, output
        except WorkspaceLinkError as error:
            raise ProjectLinkError(
                "PROJECT_LINK_NOT_ALLOWED", str(error)
            ) from error
        except WorkspaceLimitError as error:
            code = (
                "PROJECT_FILE_LIMIT_EXCEEDED"
                if str(error) == "file_count"
                else "PROJECT_SIZE_LIMIT_EXCEEDED"
            )
            raise ProjectCopyLimitError(code, str(error)) from error
        except WorkspaceEntryError as error:
            raise ProjectLinkError(
                "PROJECT_PATH_OUTSIDE_WORKTREE", str(error)
            ) from error

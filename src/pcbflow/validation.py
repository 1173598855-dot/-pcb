from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path
from tempfile import TemporaryDirectory

from pcbflow.artifacts import ContentAddressedStore
from pcbflow.domain import Task, TaskLease
from pcbflow.kicad import (
    KicadPort,
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

VALIDATION_TASK_KIND = "kicad.read_only_validation"


class ProjectCopyLimitError(TerminalTaskError):
    pass


class ProjectLinkError(TerminalTaskError):
    pass


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
            {"project_id": project_id},
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
    ) -> None:
        self._projects = projects
        self._evidence = evidence
        self._findings = findings
        self._store = store
        self._kicad = kicad
        self._max_files = max_files
        self._max_bytes = max_bytes

    def __call__(self, lease: TaskLease) -> dict[str, object]:
        project_id = str(lease.payload["project_id"])
        project = self._projects.get(project_id)
        workspace_root = self._store.root.parent / "workspaces"
        workspace_root.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(
            prefix="pcbflow-validation-", dir=workspace_root
        ) as temporary:
            temporary_path = Path(temporary)
            workspace = temporary_path / "project"
            output = temporary_path / "output"
            self._copy_project(project.source_path, workspace)
            try:
                reports = self._kicad.validate(workspace, output)
            except KicadUnavailableError as error:
                raise TerminalTaskError("KICAD_CLI_UNAVAILABLE", str(error)) from error
            except KicadProjectNotFoundError as error:
                raise TerminalTaskError("INVALID_KICAD_PROJECT", str(error)) from error
            except KicadToolError as error:
                raise RetryableTaskError("KICAD_TOOL_FAILED", str(error)) from error

            evidence_ids: list[str] = []
            finding_count = 0
            for raw in reports:
                descriptor = self._store.put_bytes(raw.data, "application/json")
                parsed = parse_kicad_report(raw.kind, raw.data)
                record = self._evidence.add_report(
                    project_id=project_id,
                    task_id=lease.task_id,
                    descriptor=descriptor,
                    kind=f"kicad_{raw.kind}",
                    subject=project_id,
                    verdict="fail" if parsed.findings else "pass",
                )
                self._findings.add_many(
                    project_id,
                    lease.task_id,
                    record.id,
                    parsed.findings,
                )
                evidence_ids.append(record.id)
                finding_count += len(parsed.findings)
        return {"evidence_ids": evidence_ids, "finding_count": finding_count}

    def _copy_project(self, source: Path, destination: Path) -> None:
        file_count = 0
        total_bytes = 0
        for root, directories, files in os.walk(source, followlinks=False):
            root_path = Path(root)
            for name in [*directories, *files]:
                path = root_path / name
                metadata = path.lstat()
                attributes = getattr(metadata, "st_file_attributes", 0)
                if path.is_symlink() or attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                    raise ProjectLinkError(
                        "PROJECT_LINK_NOT_ALLOWED", f"project contains link: {path}"
                    )
            for name in files:
                path = root_path / name
                file_count += 1
                total_bytes += path.stat().st_size
                if file_count > self._max_files:
                    raise ProjectCopyLimitError(
                        "PROJECT_FILE_LIMIT_EXCEEDED",
                        f"project contains more than {self._max_files} files",
                    )
                if total_bytes > self._max_bytes:
                    raise ProjectCopyLimitError(
                        "PROJECT_SIZE_LIMIT_EXCEEDED",
                        f"project exceeds {self._max_bytes} bytes",
                    )
        shutil.copytree(source, destination, symlinks=False)

from __future__ import annotations

import errno
import hashlib
import logging
import os
import re
import shutil
import stat
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from threading import Lock
from typing import BinaryIO

import yaml
from sqlalchemy import select, text

from pcbflow.canonical import canonical_digest, canonical_json_bytes
from pcbflow.design_tables import (
    ChangeProposalRow,
    DesignCommandBatchRow,
    GateDecisionRow,
    OutboxEventRow,
    RequirementSetRow,
)
from pcbflow.domain import Project, ProjectMode, new_id, utc_now
from pcbflow.observability import MetricName, Metrics, audit_payload, log_event
from pcbflow.process import ProcessPort, ProcessResult
from pcbflow.repositories import IdempotencyConflictError, ProjectRepository
from pcbflow.revision_store import ProjectRevisionStore
from pcbflow.workspaces import (
    WorkspaceCopier,
    WorkspaceEntryError,
    WorkspaceLimitError,
    _remove_readonly_entry,
    assert_supported_entry,
    is_snapshot_excluded,
    normalize_snapshot_excludes,
    open_regular_file,
)

_OBJECT_ID = re.compile(r"[0-9a-f]{40,64}")
_SNAPSHOT_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_ADOPTION_LOCKS_GUARD = Lock()
_ADOPTION_LOCKS: dict[Path, Lock] = {}
_WINDOWS_LOCK_CONTENTION_WINERRORS = frozenset({32, 33})
_SNAPSHOT_CHUNK_BYTES = 1024 * 1024


def _in_process_adoption_lock(path: Path) -> Lock:
    with _ADOPTION_LOCKS_GUARD:
        return _ADOPTION_LOCKS.setdefault(path, Lock())


def _is_windows_lock_contention(error: OSError) -> bool:
    winerror = getattr(error, "winerror", None)
    if winerror is not None:
        return winerror in _WINDOWS_LOCK_CONTENTION_WINERRORS
    return error.errno in {errno.EACCES, errno.EAGAIN}


def _acquire_windows_file_lock(
    lock_file: BinaryIO,
    *,
    locking: Callable[[int, int, int], None],
    wait: Callable[[float], None],
    lock_mode: int = 0,
) -> None:
    while True:
        try:
            locking(lock_file.fileno(), lock_mode, 1)
            return
        except OSError as error:
            if not _is_windows_lock_contention(error):
                raise
            wait(0.1)


class GitOperationError(RuntimeError):
    def __init__(self, argv: Sequence[str], returncode: int) -> None:
        super().__init__(f"git command failed with exit code {returncode}: {argv[-1]}")
        self.argv = tuple(argv)
        self.returncode = returncode


class ProjectWorktreeDirtyError(RuntimeError):
    pass


class ProjectNotManagedError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CandidateRevision:
    revision: str
    snapshot_digest: str


def _strip_revision(revision: str) -> str:
    object_id = revision.removeprefix("git:")
    if _OBJECT_ID.fullmatch(object_id) is None:
        raise ValueError(f"invalid Git revision: {revision}")
    return object_id


def _snapshot_files(
    root: Path,
    registered_excludes: frozenset[PurePosixPath],
    *,
    max_files: int | None = None,
) -> list[tuple[Path, PurePosixPath, os.stat_result]]:
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("snapshot root must be a directory")
    selected: list[tuple[Path, PurePosixPath, os.stat_result]] = []
    for directory, directories, files in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        directories.sort()
        files.sort()
        retained: list[str] = []
        for name in directories:
            path = directory_path / name
            assert_supported_entry(path)
            relative = PurePosixPath(path.relative_to(root).as_posix())
            if any(part.startswith(".pcbflow-tmp-") for part in relative.parts):
                raise WorkspaceEntryError(str(path))
            if not is_snapshot_excluded(relative, registered_excludes):
                retained.append(name)
        directories[:] = retained
        for name in files:
            path = directory_path / name
            metadata = assert_supported_entry(path)
            relative = PurePosixPath(path.relative_to(root).as_posix())
            if any(part.startswith(".pcbflow-tmp-") for part in relative.parts):
                raise WorkspaceEntryError(str(path))
            if is_snapshot_excluded(relative, registered_excludes):
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise WorkspaceEntryError(str(path))
            selected.append((path, relative, metadata))
            if max_files is not None and len(selected) > max_files:
                raise WorkspaceLimitError("file_count")
    return sorted(selected, key=lambda item: item[1].as_posix())


class GitCli:
    def __init__(self, runner: ProcessPort, timeout_seconds: float) -> None:
        self._runner = runner
        self._timeout_seconds = timeout_seconds

    @staticmethod
    def _environment(extra: dict[str, str] | None = None) -> dict[str, str]:
        environment = {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
            "LANG": "C",
        }
        if extra is not None:
            environment.update(extra)
        return environment

    @staticmethod
    def _controlled_argv(argv: Sequence[str]) -> list[str]:
        if not argv or argv[0] != "git":
            raise ValueError("Git argv must start with git")
        controlled = [
            "git",
            "-c",
            "core.autocrlf=false",
            "-c",
            f"core.attributesFile={os.devnull}",
            "-c",
            f"core.hooksPath={os.devnull}",
        ]
        if os.name == "nt":
            controlled.extend(("-c", "core.longpaths=true"))
        return [*controlled, *argv[1:]]

    def _invoke(
        self,
        argv: Sequence[str],
        cwd: Path,
        *,
        env: dict[str, str] | None = None,
    ) -> ProcessResult:
        result = self._runner.run(
            self._controlled_argv(argv),
            cwd,
            self._timeout_seconds,
            env=self._environment(env),
        )
        if result.output_truncated:
            raise GitOperationError(argv, result.returncode)
        return result

    def _run(
        self,
        argv: Sequence[str],
        cwd: Path,
        *,
        env: dict[str, str] | None = None,
    ) -> ProcessResult:
        result = self._invoke(argv, cwd, env=env)
        if result.returncode != 0:
            raise GitOperationError(argv, result.returncode)
        return result

    def init_bare(self, repo: Path) -> None:
        repo = repo.resolve()
        repo.parent.mkdir(parents=True, exist_ok=True)
        self._run(["git", "init", "--bare", str(repo)], repo.parent)

    def read_source_head(self, source: Path) -> str | None:
        source = source.resolve(strict=True)
        if not source.is_dir():
            return None
        result = self._invoke(
            ["git", "-C", str(source), "rev-parse", "--verify", "HEAD^{commit}"],
            source,
        )
        if result.returncode != 0:
            return None
        object_id = result.stdout.strip()
        return f"git:{object_id}" if _OBJECT_ID.fullmatch(object_id) else None

    def commit_snapshot(
        self,
        repo: Path,
        tree: Path,
        *,
        parent: str | None,
        message: str,
        timestamp: datetime,
        registered_excludes: frozenset[str] = frozenset(),
        author_name: str = "PCBFlow",
        author_email: str = "pcbflow@local.invalid",
        committer_name: str | None = None,
        committer_email: str | None = None,
    ) -> str:
        repo = repo.resolve(strict=True)
        tree = tree.resolve(strict=True)
        normalized = normalize_snapshot_excludes(registered_excludes)
        with TemporaryDirectory(prefix="pcbflow-index-", dir=repo.parent) as temporary:
            index_path = Path(temporary) / "index"
            env = {
                "GIT_INDEX_FILE": str(index_path),
                "GIT_AUTHOR_NAME": author_name,
                "GIT_AUTHOR_EMAIL": author_email,
                "GIT_COMMITTER_NAME": committer_name or author_name,
                "GIT_COMMITTER_EMAIL": committer_email or author_email,
                "GIT_AUTHOR_DATE": timestamp.isoformat(),
                "GIT_COMMITTER_DATE": timestamp.isoformat(),
            }
            self._run(
                ["git", f"--git-dir={repo}", "read-tree", "--empty"],
                tree,
                env=env,
            )
            for path, relative, metadata in _snapshot_files(tree, normalized):
                blob = self._run(
                    [
                        "git",
                        f"--git-dir={repo}",
                        "hash-object",
                        "-w",
                        "--no-filters",
                        "--",
                        str(path),
                    ],
                    tree,
                    env=env,
                ).stdout.strip()
                if _OBJECT_ID.fullmatch(blob) is None:
                    raise GitOperationError(("git", "hash-object"), 0)
                mode = "100755" if metadata.st_mode & stat.S_IXUSR else "100644"
                self._run(
                    [
                        "git",
                        f"--git-dir={repo}",
                        "update-index",
                        "--add",
                        "--cacheinfo",
                        mode,
                        blob,
                        relative.as_posix(),
                    ],
                    tree,
                    env=env,
                )
            tree_id = self._run(
                ["git", f"--git-dir={repo}", "write-tree"], tree, env=env
            ).stdout.strip()
            if _OBJECT_ID.fullmatch(tree_id) is None:
                raise GitOperationError(("git", "write-tree"), 0)
            argv = [
                "git",
                f"--git-dir={repo}",
                "commit-tree",
                tree_id,
                "-m",
                message,
            ]
            if parent is not None:
                argv.extend(["-p", _strip_revision(parent)])
            commit = self._run(argv, tree, env=env).stdout.strip()
            if _OBJECT_ID.fullmatch(commit) is None:
                raise GitOperationError(("git", "commit-tree"), 0)
            return f"git:{commit}"

    def resolve_ref(self, repo: Path, ref: str) -> str | None:
        repo = repo.resolve(strict=True)
        result = self._invoke(
            ["git", f"--git-dir={repo}", "rev-parse", "--verify", f"{ref}^{{commit}}"],
            repo.parent,
        )
        if result.returncode != 0:
            return None
        object_id = result.stdout.strip()
        return f"git:{object_id}" if _OBJECT_ID.fullmatch(object_id) else None

    def object_exists(self, repo: Path, revision: str) -> bool:
        object_id = _strip_revision(revision)
        result = self._invoke(
            ["git", f"--git-dir={repo.resolve(strict=True)}", "cat-file", "-e", f"{object_id}^{{commit}}"],
            repo.resolve(strict=True).parent,
        )
        return result.returncode == 0

    def update_ref(
        self,
        repo: Path,
        ref: str,
        new_revision: str,
        expected_revision: str | None,
    ) -> None:
        current = self.resolve_ref(repo, ref)
        if current == new_revision:
            return
        new_object = _strip_revision(new_revision)
        old_object = (
            _strip_revision(expected_revision)
            if expected_revision is not None
            else "0" * len(new_object)
        )
        self._run(
            [
                "git",
                f"--git-dir={repo.resolve(strict=True)}",
                "update-ref",
                ref,
                new_object,
                old_object,
            ],
            repo.resolve(strict=True).parent,
        )

    @contextmanager
    def add_worktree(
        self, repo: Path, revision: str, destination: Path
    ) -> Iterator[Path]:
        repo = repo.resolve(strict=True)
        destination = destination.resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._run(
            [
                "git",
                f"--git-dir={repo}",
                "worktree",
                "add",
                "--detach",
                str(destination),
                _strip_revision(revision),
            ],
            repo.parent,
        )
        try:
            yield destination
        finally:
            result = self._invoke(
                [
                    "git",
                    f"--git-dir={repo}",
                    "worktree",
                    "remove",
                    "--force",
                    str(destination),
                ],
                repo.parent,
            )
            if result.returncode != 0 and destination.exists():
                raise GitOperationError(("git", "worktree", "remove"), result.returncode)
            self._run(
                ["git", f"--git-dir={repo}", "worktree", "prune"], repo.parent
            )

    def list_refs(self, repo: Path, prefix: str) -> dict[str, str]:
        repo = repo.resolve(strict=True)
        result = self._run(
            [
                "git",
                f"--git-dir={repo}",
                "for-each-ref",
                "--format=%(refname)%00%(objectname)",
                prefix,
            ],
            repo.parent,
        )
        refs: dict[str, str] = {}
        for line in result.stdout.splitlines():
            try:
                ref, object_id = line.split("\0", 1)
            except ValueError as error:
                raise GitOperationError(("git", "for-each-ref"), 0) from error
            if not ref.startswith(prefix) or _OBJECT_ID.fullmatch(object_id) is None:
                raise GitOperationError(("git", "for-each-ref"), 0)
            refs[ref] = f"git:{object_id}"
        return refs

    def is_ancestor(self, repo: Path, ancestor: str, descendant: str) -> bool:
        repo = repo.resolve(strict=True)
        result = self._invoke(
            [
                "git",
                f"--git-dir={repo}",
                "merge-base",
                "--is-ancestor",
                _strip_revision(ancestor),
                _strip_revision(descendant),
            ],
            repo.parent,
        )
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False
        raise GitOperationError(("git", "merge-base"), result.returncode)

    def diff_worktree(self, workspace: Path) -> bytes:
        workspace = workspace.resolve(strict=True)
        result = self._invoke(
            ["git", "diff", "--no-ext-diff", "--binary", "--no-renames", "--", "."],
            workspace,
        )
        if result.returncode not in (0, 1):
            raise GitOperationError(("git", "diff"), result.returncode)
        return result.stdout_bytes


class RevisionService:
    def __init__(
        self,
        *,
        projects: ProjectRepository,
        revision_store: ProjectRevisionStore,
        git: GitCli,
        copier: WorkspaceCopier,
        projects_dir: Path,
        workspaces_dir: Path,
        max_files: int,
        max_bytes: int,
    ) -> None:
        self._projects = projects
        self._revision_store = revision_store
        self._git = git
        self._copier = copier
        self._projects_dir = projects_dir
        self._workspaces_dir = workspaces_dir
        if max_files <= 0:
            raise ValueError("max_files must be positive")
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self._max_files = max_files
        self._max_bytes = max_bytes

    def _repo(self, project_id: str) -> Path:
        if not project_id or any(value in project_id for value in ("/", "\\", "..")):
            raise ValueError("invalid project id")
        return self._projects_dir / project_id / "repo.git"

    @property
    def git(self) -> GitCli:
        return self._git

    def repo_path(self, project_id: str) -> Path:
        return self._repo(project_id)

    @contextmanager
    def _adoption_lock(self, project_id: str) -> Iterator[None]:
        lock_path = self._repo(project_id).parent / ".adoption.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        in_process_lock = _in_process_adoption_lock(lock_path)
        with in_process_lock, lock_path.open("a+b") as lock_file:
            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            lock_file.seek(0)
            if os.name == "nt":
                import msvcrt

                _acquire_windows_file_lock(
                    lock_file,
                    locking=msvcrt.locking,
                    wait=time.sleep,
                    lock_mode=msvcrt.LK_NBLCK,
                )
                unlock = lambda: msvcrt.locking(
                    lock_file.fileno(), msvcrt.LK_UNLCK, 1
                )
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)  # type: ignore[attr-defined]  # POSIX-only lock branch
                unlock = lambda: fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]  # POSIX-only lock branch
            try:
                yield
            finally:
                unlock()

    @staticmethod
    def _manifest(source_snapshot_digest: str) -> bytes:
        value = {
            "schema_version": "1.0",
            "snapshot_policy_version": 1,
            "snapshot_excludes": [],
            "source_snapshot_digest": source_snapshot_digest,
        }
        return yaml.safe_dump(
            value,
            allow_unicode=True,
            sort_keys=True,
            default_flow_style=False,
            line_break="\n",
        ).encode("utf-8")

    @staticmethod
    def _load_snapshot_excludes(workspace: Path) -> frozenset[str]:
        manifest = workspace / "pcbflow.yaml"
        value = yaml.safe_load(manifest.read_bytes())
        if not isinstance(value, dict):
            raise ValueError("unsupported snapshot policy")
        policy_version = value.get("snapshot_policy_version")
        if policy_version not in {None, 1}:
            raise ValueError("unsupported snapshot policy")
        raw = value.get("snapshot_excludes", [])
        if (
            not isinstance(raw, list)
            or any(not isinstance(item, str) for item in raw)
            or len(set(raw)) != len(raw)
        ):
            raise ValueError("invalid snapshot_excludes")
        normalized = normalize_snapshot_excludes(frozenset(raw))
        return frozenset(path.as_posix() for path in normalized)

    def snapshot_digest(
        self,
        root: Path,
        registered_excludes: frozenset[str] = frozenset(),
    ) -> str:
        normalized = normalize_snapshot_excludes(registered_excludes)
        files: list[dict[str, object]] = []
        total_bytes = 0
        for path, relative, metadata in _snapshot_files(
            root, normalized, max_files=self._max_files
        ):
            digest, size, mode, total_bytes = self._hash_snapshot_file(
                path, total_bytes
            )
            files.append(
                {
                    "path": relative.as_posix(),
                    "mode": mode,
                    "size": size,
                    "digest": digest,
                }
            )
        return canonical_digest({"snapshot_policy_version": 1, "files": files})

    def snapshot_manifest(self, root: Path) -> bytes:
        """Return evidence using the same file policy as revision snapshots."""
        excludes = self._load_snapshot_excludes(root)
        normalized = normalize_snapshot_excludes(excludes)
        files: list[dict[str, object]] = []
        total_bytes = 0
        for path, relative, metadata in _snapshot_files(
            root, normalized, max_files=self._max_files
        ):
            digest, size, mode, total_bytes = self._hash_snapshot_file(
                path, total_bytes
            )
            files.append(
                {
                    "path": relative.as_posix(),
                    "type": "file",
                    "mode": mode,
                    "size": size,
                    "digest": digest,
                }
            )
        return canonical_json_bytes(
            {
                "schema_version": "1.0",
                "snapshot_policy_version": 1,
                "snapshot_excludes": sorted(
                    value.as_posix() for value in normalized
                ),
                "snapshot_digest": canonical_digest(
                    {
                        "snapshot_policy_version": 1,
                        "files": [
                            {
                                key: entry[key]
                                for key in ("path", "mode", "size", "digest")
                            }
                            for entry in files
                        ],
                    }
                ),
                "files": files,
            }
        )

    def _hash_snapshot_file(
        self, path: Path, total_bytes: int
    ) -> tuple[str, int, str, int]:
        digest = hashlib.sha256()
        file_size = 0
        with open_regular_file(path) as stream:
            metadata = os.fstat(stream.fileno())
            mode = "100755" if metadata.st_mode & stat.S_IXUSR else "100644"
            while True:
                remaining = self._max_bytes - total_bytes
                chunk = stream.read(min(_SNAPSHOT_CHUNK_BYTES, remaining + 1))
                if not chunk:
                    break
                if len(chunk) > remaining:
                    raise WorkspaceLimitError("total_bytes")
                digest.update(chunk)
                chunk_size = len(chunk)
                file_size += chunk_size
                total_bytes += chunk_size
        return f"sha256:{digest.hexdigest()}", file_size, mode, total_bytes

    def adopt(self, project_id: str, idempotency_key: str) -> Project:
        if not idempotency_key:
            raise ValueError("idempotency key must not be empty")
        with self._adoption_lock(project_id):
            return self._adopt_locked(project_id, idempotency_key)

    def _adopt_locked(self, project_id: str, idempotency_key: str) -> Project:
        existing_key = self._projects.find_by_adoption_key(idempotency_key)
        if existing_key is not None and existing_key.id != project_id:
            raise IdempotencyConflictError(idempotency_key)
        project = self._projects.get(project_id)
        source_head = self._git.read_source_head(project.source_path)
        self._workspaces_dir.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(
            prefix=f"pcbflow-adopt-{project.id}-", dir=self._workspaces_dir
        ) as temporary:
            staging = Path(temporary) / "project"
            self._copier.copy(project.source_path, staging)
            source_snapshot_digest = self.snapshot_digest(staging)
            input_digest = canonical_digest(
                {
                    "schema_version": "1.0",
                    "project_id": project_id,
                    "source_snapshot_digest": source_snapshot_digest,
                }
            )
            if existing_key is not None:
                if existing_key.adoption_input_digest == input_digest:
                    return existing_key
                raise IdempotencyConflictError(idempotency_key)
            if project.mode is ProjectMode.MANAGED:
                if project.adoption_input_digest == input_digest:
                    return project
                raise IdempotencyConflictError(idempotency_key)

            (staging / "pcbflow.yaml").write_bytes(
                self._manifest(source_snapshot_digest)
            )
            project_snapshot_digest = self.snapshot_digest(staging)
            repo = self._repo(project.id)
            repo_root = repo.parent
            repo_existed = repo.exists()
            repo_root.mkdir(parents=True, exist_ok=True)
            try:
                if not repo.exists():
                    self._git.init_bare(repo)
                revision = self._git.commit_snapshot(
                    repo,
                    staging,
                    parent=None,
                    message="pcbflow: adopt project",
                    timestamp=project.created_at,
                )
                self._git.update_ref(
                    repo, "refs/heads/design", revision, expected_revision=None
                )
                with self.materialize(
                    project.id, revision, "adoption-proof"
                ) as checkout:
                    actual_digest = self.snapshot_digest(checkout)
                if actual_digest != project_snapshot_digest:
                    raise ProjectWorktreeDirtyError(project.id)
                return self._projects.mark_managed(
                    project.id,
                    repo_key=project.id,
                    revision=revision,
                    snapshot_digest=project_snapshot_digest,
                    adoption_idempotency_key=idempotency_key,
                    adoption_input_digest=input_digest,
                    source_head=source_head,
                    expected_version=project.version,
                )
            except Exception:
                if not repo_existed:
                    persisted = self._projects.get(project.id)
                    if persisted.mode is ProjectMode.REGISTERED:
                        if repo.exists():
                            shutil.rmtree(repo, onexc=_remove_readonly_entry)
                raise

    @contextmanager
    def materialize(
        self, project_id: str, revision: str, purpose: str
    ) -> Iterator[Path]:
        safe_purpose = "".join(
            character if character.isalnum() or character == "-" else "-"
            for character in purpose
        ).strip("-") or "revision"
        safe_purpose = safe_purpose[:24]
        self._workspaces_dir.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(
            prefix=f"pcbflow-{safe_purpose}-", dir=self._workspaces_dir
        ) as temporary:
            destination = Path(temporary) / "project"
            with self._git.add_worktree(
                self._repo(project_id), revision, destination
            ) as checkout:
                yield checkout

    def commit_candidate(
        self,
        project: Project,
        workspace: Path,
        base_revision: str,
        ref: str,
        message: str,
        timestamp: datetime,
        *,
        snapshot_digest: str | None = None,
        publish_ref: bool = True,
        author_name: str = "PCBFlow",
        author_email: str = "pcbflow@local.invalid",
    ) -> CandidateRevision:
        if snapshot_digest is not None and not _SNAPSHOT_DIGEST.fullmatch(
            snapshot_digest
        ):
            raise ValueError("snapshot digest must be a sha256 digest")
        excludes = self._load_snapshot_excludes(workspace)
        snapshot = (
            snapshot_digest
            if snapshot_digest is not None
            else self.snapshot_digest(workspace, excludes)
        )
        revision = self._git.commit_snapshot(
            self._repo(project.id),
            workspace,
            parent=base_revision,
            message=message,
            timestamp=timestamp,
            registered_excludes=excludes,
            author_name=author_name,
            author_email=author_email,
        )
        if publish_ref:
            self._git.update_ref(self._repo(project.id), ref, revision, expected_revision=None)
        return CandidateRevision(revision=revision, snapshot_digest=snapshot)

    def publish_candidate_ref(
        self, project_id: str, ref: str, revision: str, expected_revision: str | None = None
    ) -> None:
        self._git.update_ref(self._repo(project_id), ref, revision, expected_revision)

    def resolve_design_ref(self, project_id: str) -> str | None:
        return self._git.resolve_ref(self._repo(project_id), "refs/heads/design")

    def object_exists(self, project_id: str, revision: str) -> bool:
        try:
            return self._git.object_exists(self._repo(project_id), revision)
        except (OSError, ValueError, GitOperationError):
            return False

    def snapshot_digest_for_revision(self, project_id: str, revision: str) -> str:
        with self.materialize(project_id, revision, "reconcile-proof") as workspace:
            excludes = self._load_snapshot_excludes(workspace)
            return self.snapshot_digest(workspace, excludes)

    def promote_design_ref(
        self,
        project_id: str,
        revision: str,
        expected_revision: str | None,
    ) -> None:
        self._git.update_ref(
            self._repo(project_id),
            "refs/heads/design",
            revision,
            expected_revision=expected_revision,
        )

    def resolve_proposal_ref(
        self, project_id: str, proposal_id: str
    ) -> str | None:
        return self._git.resolve_ref(
            self._repo(project_id), f"refs/pcbflow/proposals/{proposal_id}"
        )

    def list_proposal_refs(self, project_id: str) -> dict[str, str]:
        return self._git.list_refs(
            self._repo(project_id), "refs/pcbflow/proposals/"
        )

    def list_requirement_refs(self, project_id: str) -> dict[str, str]:
        return self._git.list_refs(
            self._repo(project_id), "refs/pcbflow/requirements/"
        )

    def resolve_requirement_ref(
        self, project_id: str, requirement_set_id: str
    ) -> str | None:
        return self._git.resolve_ref(
            self._repo(project_id), f"refs/pcbflow/requirements/{requirement_set_id}"
        )

    def is_ancestor(
        self, project_id: str, ancestor: str, descendant: str
    ) -> bool:
        return self._git.is_ancestor(
            self._repo(project_id), ancestor, descendant
        )

    def assert_clean(
        self, project_id: str, revision: str, workspace: Path
    ) -> None:
        stored = self._revision_store.get(project_id, revision)
        excludes = self._load_snapshot_excludes(workspace)
        actual = self.snapshot_digest(workspace, excludes)
        if actual != stored.snapshot_digest:
            raise ProjectWorktreeDirtyError(project_id)


class RevisionReconciler:
    def __init__(
        self,
        projects: ProjectRepository,
        revision_store: ProjectRevisionStore | None = None,
        revisions: RevisionService | None = None,
        *,
        requirements=None,
        proposals=None,
        sessions=None,
        clock: Callable[[], datetime] | None = None,
        metrics: Metrics | None = None,
    ) -> None:
        self._projects = projects
        self._revision_store = revision_store
        self._revisions = revisions
        self._requirements = requirements
        self._proposals = proposals
        self._sessions = sessions
        self._clock = clock or utc_now
        self._metrics = metrics

    @staticmethod
    def _terminal(project_id: str, detail: str) -> RuntimeError:
        from pcbflow.proposals import RevisionReconciliationRequiredError

        return RevisionReconciliationRequiredError(f"{project_id}: {detail}")

    def _record_unknown_ref(
        self, project_id: str, ref_name: str, revision: str
    ) -> None:
        if self._sessions is None:
            return
        payload = audit_payload(
            actor_type="service",
            actor_id="pcbflow",
            action="revision.unknown_ref",
            object_type="project",
            object_id=project_id,
            before_digest=None,
            after_digest=revision,
            result="unknown_ref",
        )
        payload.update({
            "project_id": project_id,
            "ref_name": ref_name,
            "revision": revision,
        })
        with self._sessions.begin() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            existing = session.scalars(
                select(OutboxEventRow).where(
                    OutboxEventRow.event_type == "revision.unknown_ref"
                )
            ).all()
            if any(
                event.payload_json.get("project_id") == project_id
                and event.payload_json.get("ref_name") == ref_name
                and event.payload_json.get("revision") == revision
                for event in existing
            ):
                return
            session.add(
                OutboxEventRow(
                    id=new_id("evt"),
                    aggregate_type="project",
                    aggregate_id=project_id,
                    event_type="revision.unknown_ref",
                    payload_json=payload,
                    created_at=self._clock(),
                    processed_at=None,
                    attempt_count=0,
                    last_error_code=None,
                )
            )

    def _assert_projection_facts(self, project_id: str, revision: str) -> None:
        if self._revision_store is None:
            return
        try:
            stored = self._revision_store.get(project_id, revision)
        except Exception as error:
            raise self._terminal(project_id, "database revision is missing") from error
        project = self._projects.get(project_id)
        if (
            project.project_snapshot_digest is not None
            and stored.snapshot_digest != project.project_snapshot_digest
        ):
            raise self._terminal(project_id, "database snapshot digest is inconsistent")
        if self._sessions is None or (
            stored.requirement_set_id is None and stored.command_batch_id is None
        ):
            return
        with self._sessions() as session:
            if stored.requirement_set_id is not None:
                requirement = session.get(RequirementSetRow, stored.requirement_set_id)
                decision = session.scalar(
                    select(GateDecisionRow).where(
                        GateDecisionRow.project_id == project_id,
                        GateDecisionRow.gate == "G1",
                        GateDecisionRow.subject_type == "requirement_set",
                        GateDecisionRow.subject_id == stored.requirement_set_id,
                        GateDecisionRow.decision == "approve",
                    )
                )
                expected_frozen_revision = revision
                if stored.command_batch_id is not None:
                    batch = session.get(DesignCommandBatchRow, stored.command_batch_id)
                    if batch is None or batch.requirement_set_id != stored.requirement_set_id:
                        raise self._terminal(
                            project_id, "proposal command batch facts are inconsistent"
                        )
                    expected_frozen_revision = batch.base_revision
                frozen_revision_is_valid = (
                    requirement is not None
                    and requirement.frozen_revision == expected_frozen_revision
                )
                if (
                    not frozen_revision_is_valid
                    and stored.command_batch_id is not None
                    and requirement is not None
                    and requirement.frozen_revision is not None
                    and self._revisions is not None
                ):
                    try:
                        frozen_revision_is_valid = self._revisions.is_ancestor(
                            project_id,
                            requirement.frozen_revision,
                            expected_frozen_revision,
                        )
                    except Exception as error:
                        raise self._terminal(
                            project_id, "G1 revision ancestry cannot be verified"
                        ) from error
                if (
                    requirement is None
                    or requirement.status != "frozen"
                    or not frozen_revision_is_valid
                    or decision is None
                ):
                    raise self._terminal(project_id, "G1 acceptance facts are inconsistent")
            if stored.command_batch_id is not None:
                proposal = session.scalar(
                    select(ChangeProposalRow).where(
                        ChangeProposalRow.project_id == project_id,
                        ChangeProposalRow.command_batch_id == stored.command_batch_id,
                    )
                )
                decision = None
                if proposal is not None:
                    decision = session.scalar(
                        select(GateDecisionRow).where(
                            GateDecisionRow.project_id == project_id,
                            GateDecisionRow.gate == "DESIGN_CHANGE",
                            GateDecisionRow.subject_type == "change_proposal",
                            GateDecisionRow.subject_id == proposal.id,
                            GateDecisionRow.decision == "approve",
                        )
                    )
                if (
                    proposal is None
                    or proposal.status != "accepted"
                    or proposal.candidate_revision != revision
                    or decision is None
                ):
                    raise self._terminal(project_id, "proposal acceptance facts are inconsistent")

    def _scan_candidate_refs(self, project_id: str) -> None:
        if self._sessions is None:
            return
        assert self._revisions is not None
        with self._sessions() as session:
            requirements = {
                row.id: row for row in session.scalars(
                    select(RequirementSetRow).where(
                        RequirementSetRow.project_id == project_id
                    )
                )
            }
            proposals = {
                row.id: row for row in session.scalars(
                    select(ChangeProposalRow).where(
                        ChangeProposalRow.project_id == project_id
                    )
                )
            }
        list_requirement_refs = getattr(self._revisions, "list_requirement_refs", None)
        list_proposal_refs = getattr(self._revisions, "list_proposal_refs", None)
        requirement_refs = (
            list_requirement_refs(project_id) if list_requirement_refs is not None else {}
        )
        proposal_refs = (
            list_proposal_refs(project_id) if list_proposal_refs is not None else {}
        )
        for requirement in requirements.values():
            if requirement.status != "pending_approval":
                continue
            ref_name = f"refs/pcbflow/requirements/{requirement.id}"
            actual_ref = requirement_refs.get(ref_name)
            if list_requirement_refs is None:
                actual_ref = getattr(self._revisions, "resolve_requirement_ref", lambda *_: None)(
                    project_id, requirement.id
                )
            if (
                requirement.candidate_revision is None
                or actual_ref != requirement.candidate_revision
                or not self._revisions.object_exists(
                    project_id, requirement.candidate_revision
                )
                or not self._snapshot_matches(
                    project_id,
                    requirement.candidate_revision,
                    requirement.candidate_snapshot_digest,
                )
            ):
                raise self._terminal(project_id, "pending requirement candidate is inconsistent")
        for ref_name, revision in requirement_refs.items():
            requirement = requirements.get(
                ref_name.removeprefix("refs/pcbflow/requirements/")
            )
            if requirement is None:
                self._record_unknown_ref(project_id, ref_name, revision)
            elif requirement.status == "draft":
                # Submit publishes this ref before recording the pending candidate.
                continue

        for proposal in proposals.values():
            if proposal.status != "ready_for_review":
                continue
            ref_name = f"refs/pcbflow/proposals/{proposal.id}"
            actual_ref = proposal_refs.get(ref_name)
            if list_proposal_refs is None:
                actual_ref = self._revisions.resolve_proposal_ref(project_id, proposal.id)
            if (
                proposal.candidate_revision is None
                or actual_ref != proposal.candidate_revision
                or not self._revisions.object_exists(project_id, proposal.candidate_revision)
                or not self._snapshot_matches(
                    project_id,
                    proposal.candidate_revision,
                    proposal.candidate_snapshot_digest,
                )
            ):
                raise self._terminal(project_id, "ready proposal candidate is inconsistent")
        for ref_name, revision in proposal_refs.items():
            proposal = proposals.get(ref_name.removeprefix("refs/pcbflow/proposals/"))
            if proposal is None:
                self._record_unknown_ref(project_id, ref_name, revision)
            elif proposal.status in {"queued", "executing"}:
                # Executor replay owns these pre-commit residues.
                continue

    def _snapshot_matches(
        self,
        project_id: str,
        revision: str,
        expected_digest: str | None,
    ) -> bool:
        if expected_digest is None:
            return False
        checker = getattr(self._revisions, "snapshot_digest_for_revision", None)
        if checker is None:
            return True
        try:
            return checker(project_id, revision) == expected_digest
        except Exception:
            return False

    def run_once(self) -> int:
        assert self._revisions is not None
        repaired = 0
        for project in self._projects.list():
            if project.mode is not ProjectMode.MANAGED:
                continue
            if project.current_revision is None:
                raise ProjectNotManagedError(project.id)
            object_exists = getattr(self._revisions, "object_exists", None)
            if object_exists is not None and not object_exists(project.id, project.current_revision):
                raise self._terminal(project.id, "current revision object is missing")
            self._assert_projection_facts(project.id, project.current_revision)
            self._scan_candidate_refs(project.id)
            actual = self._revisions.resolve_design_ref(project.id)
            if actual == project.current_revision:
                continue
            if self._metrics is not None:
                self._metrics.increment(MetricName.GIT_REF_RECONCILIATION_RETRY_TOTAL)
            self._revisions.promote_design_ref(
                project.id,
                project.current_revision,
                actual,
            )
            log_event(
                logging.getLogger(__name__),
                logging.INFO,
                "revision.reconciled",
                project_id=project.id,
                base_revision=actual,
                candidate_revision=project.current_revision,
                result="repaired",
            )
            repaired += 1
        return repaired

    def assert_writable(self, project_id: str) -> None:
        self.run_once()
        project = self._projects.get(project_id)
        if project.mode is not ProjectMode.MANAGED:
            raise ProjectNotManagedError(project_id)

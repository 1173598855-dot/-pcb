from __future__ import annotations

import errno
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from pcbflow.process import ProcessResult, ProcessRunner
from pcbflow.revisions import (
    GitCli,
    GitOperationError,
    _acquire_windows_file_lock,
)
from pcbflow.workspaces import (
    WorkspaceCopier,
    WorkspaceEntryError,
    WorkspaceLimitError,
    WorkspaceLinkError,
    assert_supported_entry,
    normalize_snapshot_excludes,
)


def test_git_cli_creates_deterministic_commit_and_compare_updates_ref(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo.git"
    tree = tmp_path / "tree"
    tree.mkdir()
    original = b"(kicad_sch\r\n  (version 20250114))\r\n"
    (tree / "board.kicad_sch").write_bytes(original)
    (tree / "~board.kicad_sch.lck").write_text("volatile", encoding="utf-8")
    (tree / "build").mkdir()
    (tree / "build" / "cache.bin").write_bytes(b"cache")
    git = GitCli(ProcessRunner(100_000), timeout_seconds=20)
    created_at = datetime(2026, 7, 29, 12, tzinfo=UTC)

    git.init_bare(repo)
    first = git.commit_snapshot(
        repo,
        tree,
        parent=None,
        message="pcbflow: adopt project",
        timestamp=created_at,
        registered_excludes=frozenset({"build"}),
    )
    second = git.commit_snapshot(
        repo,
        tree,
        parent=None,
        message="pcbflow: adopt project",
        timestamp=created_at,
        registered_excludes=frozenset({"build"}),
    )
    assert first == second

    git.update_ref(repo, "refs/heads/design", first, expected_revision=None)
    assert git.resolve_ref(repo, "refs/heads/design") == first
    git.update_ref(repo, "refs/heads/design", first, expected_revision=None)
    assert git.resolve_ref(repo, "refs/heads/design") == first
    with git.add_worktree(repo, first, tmp_path / "checkout") as checkout:
        assert (checkout / "board.kicad_sch").read_bytes() == original
        assert not (checkout / "~board.kicad_sch.lck").exists()
        assert not (checkout / "build").exists()

    git.update_ref(repo, "refs/heads/master", first, expected_revision=None)
    linked_source = tmp_path / "linked-source"
    linked_source.mkdir()
    (linked_source / ".git").write_text(
        f"gitdir: {repo.as_posix()}\n", encoding="utf-8"
    )
    assert git.read_source_head(linked_source) == first

    (tree / "board.kicad_sch").write_text("changed", encoding="utf-8")
    third = git.commit_snapshot(
        repo,
        tree,
        parent=first,
        message="pcbflow: candidate",
        timestamp=created_at,
        registered_excludes=frozenset({"build"}),
    )
    proposal_ref = "refs/pcbflow/proposals/prp_test"
    git.update_ref(repo, proposal_ref, third, expected_revision=None)
    assert git.list_refs(repo, "refs/pcbflow/proposals/") == {
        proposal_ref: third
    }
    assert git.is_ancestor(repo, first, third)
    assert not git.is_ancestor(repo, third, first)
    with pytest.raises(GitOperationError):
        git.update_ref(
            repo,
            "refs/heads/design",
            third,
            expected_revision="git:" + "f" * 40,
        )


def test_workspace_copier_enforces_snapshot_exclusions_and_temp_policy(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    (source / "board.kicad_sch").write_text("board", encoding="utf-8")
    (source / "~board.kicad_sch.lck").write_text("lock", encoding="utf-8")
    (source / ".git").mkdir()
    (source / ".git" / "config").write_text("config", encoding="utf-8")
    (source / "build").mkdir()
    (source / "build" / "cache.bin").write_bytes(b"cache")

    WorkspaceCopier(max_files=1, max_bytes=10).copy(
        source, destination, registered_excludes=frozenset({"build"})
    )

    assert (destination / "board.kicad_sch").is_file()
    assert not (destination / "~board.kicad_sch.lck").exists()
    assert not (destination / ".git").exists()
    assert not (destination / "build").exists()
    with pytest.raises(ValueError):
        normalize_snapshot_excludes(frozenset({"../escape"}))

    unsafe = tmp_path / "unsafe"
    unsafe.mkdir()
    (unsafe / ".pcbflow-tmp-leftover").write_text("partial", encoding="utf-8")
    with pytest.raises(WorkspaceEntryError):
        WorkspaceCopier(max_files=10, max_bytes=100).copy(
            unsafe, tmp_path / "unsafe-copy"
        )


def test_workspace_copier_rejects_links_reparse_points_and_copy_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "one.kicad_sch").write_bytes(b"one")
    (source / "two.kicad_sch").write_bytes(b"two")

    with pytest.raises(WorkspaceLimitError, match="file_count"):
        WorkspaceCopier(max_files=1, max_bytes=100).copy(
            source, tmp_path / "too-many-files"
        )
    with pytest.raises(WorkspaceLimitError, match="total_bytes"):
        WorkspaceCopier(max_files=10, max_bytes=5).copy(
            source, tmp_path / "too-many-bytes"
        )

    link_source = tmp_path / "link-source"
    link_source.mkdir()
    target = tmp_path / "target.kicad_sch"
    target.write_text("board", encoding="utf-8")
    try:
        (link_source / "linked.kicad_sch").symlink_to(target)
    except OSError:
        pytest.skip("symbolic links are unavailable on this Windows host")
    with pytest.raises(WorkspaceLinkError):
        WorkspaceCopier(max_files=10, max_bytes=100).copy(
            link_source, tmp_path / "linked-copy"
        )

    entry = tmp_path / "reparse-entry"
    entry.write_text("entry", encoding="utf-8")
    metadata = entry.lstat()
    monkeypatch.setattr(
        Path,
        "lstat",
        lambda _path: SimpleNamespace(
            st_mode=metadata.st_mode,
            st_file_attributes=0x400,
        ),
    )
    monkeypatch.setattr("pcbflow.workspaces.stat.FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    with pytest.raises(WorkspaceLinkError):
        assert_supported_entry(entry)


def test_workspace_copier_rejects_non_regular_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "device-entry").write_text("entry", encoding="utf-8")
    monkeypatch.setattr("pcbflow.workspaces.stat.S_ISREG", lambda _mode: False)

    with pytest.raises(WorkspaceEntryError):
        WorkspaceCopier(max_files=10, max_bytes=100).copy(
            source, tmp_path / "destination"
        )


class _CapturingRunner:
    def __init__(self) -> None:
        self.argv: tuple[str, ...] | None = None
        self.env: dict[str, str] | None = None

    def run(
        self,
        argv: tuple[str, ...],
        cwd: Path,
        timeout_seconds: float,
        *,
        env: dict[str, str] | None = None,
    ) -> ProcessResult:
        self.argv = tuple(argv)
        self.env = env
        return ProcessResult(
            argv=tuple(argv),
            returncode=0,
            stdout="a" * 40 + "\n",
            stderr="",
            output_truncated=False,
        )


def test_read_source_head_uses_controlled_git_configuration(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / ".git").mkdir()
    (source / ".git" / "config").write_text(
        "[core]\n\thooksPath = untrusted-hooks\n\tautocrlf = true\n",
        encoding="utf-8",
    )
    runner = _CapturingRunner()

    assert GitCli(runner, timeout_seconds=20).read_source_head(source) == "git:" + "a" * 40

    assert runner.env is not None
    assert runner.env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert "GIT_CONFIG_GLOBAL" in runner.env
    assert runner.argv is not None
    assert "core.autocrlf=false" in runner.argv
    assert any(value.startswith("core.attributesFile=") for value in runner.argv)
    assert any(value.startswith("core.hooksPath=") for value in runner.argv)


def test_windows_file_lock_retries_contention_beyond_primitive_limit(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "adoption.lock"
    release_contention = Event()
    contention_exceeded = Event()
    attempts = 0
    waits = 0
    errors: list[BaseException] = []

    def locking(_descriptor: int, _mode: int, _length: int) -> None:
        nonlocal attempts
        attempts += 1
        if attempts <= 11:
            raise OSError(errno.EACCES, "lock contention", None, 33)

    def wait_for_retry(_seconds: float) -> None:
        nonlocal waits
        waits += 1
        if waits == 11:
            contention_exceeded.set()
            assert release_contention.wait(timeout=5)

    def acquire() -> None:
        try:
            with lock_path.open("w+b") as lock_file:
                _acquire_windows_file_lock(
                    lock_file,
                    locking=locking,
                    wait=wait_for_retry,
                )
        except BaseException as error:
            errors.append(error)

    worker = Thread(target=acquire)
    worker.start()
    assert contention_exceeded.wait(timeout=5)
    release_contention.set()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert errors == []
    assert attempts == 12
    assert waits == 11


def test_windows_file_lock_propagates_non_contention_errors(tmp_path: Path) -> None:
    lock_path = tmp_path / "adoption.lock"
    waits: list[float] = []

    def locking(_descriptor: int, _mode: int, _length: int) -> None:
        raise OSError(errno.EIO, "disk error")

    with lock_path.open("w+b") as lock_file:
        with pytest.raises(OSError, match="disk error"):
            _acquire_windows_file_lock(
                lock_file,
                locking=locking,
                wait=waits.append,
            )

    assert waits == []

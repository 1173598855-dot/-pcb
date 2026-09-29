"""Focused unit tests for the workspace copy and open guards.

The POSIX descriptor-relative copy branch cannot execute on Windows, where CI
and local verification run; those lines stay covered by design review only.
The tests here target every guard reachable on the Windows code path,
including the TOCTOU checks that race detection depends on.
"""

from __future__ import annotations

import errno
import os
import stat
from types import SimpleNamespace

import pytest

from pcbflow import workspaces
from pcbflow.workspaces import (
    WorkspaceCopier,
    WorkspaceEntryError,
    WorkspaceLinkError,
    _remove_readonly_entry,
    open_regular_file,
)


def _write(path, content: str = "payload") -> None:
    path.write_text(content, encoding="utf-8")


def test_open_regular_file_rejects_directory(tmp_path) -> None:
    directory = tmp_path / "dir"
    directory.mkdir()

    with pytest.raises(WorkspaceEntryError, match="dir"), open_regular_file(directory):
        pass


def test_open_regular_file_maps_eloop_to_link_error(tmp_path, monkeypatch) -> None:
    target = tmp_path / "design.kicad_sch"
    _write(target)

    def raise_eloop(path, flags):
        raise OSError(errno.ELOOP, "symbolic link loop")

    monkeypatch.setattr(workspaces.os, "open", raise_eloop)

    with pytest.raises(WorkspaceLinkError, match="design.kicad_sch"), open_regular_file(target):
        pass


def test_open_regular_file_reraises_unrelated_open_errors(tmp_path, monkeypatch) -> None:
    target = tmp_path / "design.kicad_sch"
    _write(target)

    def raise_denied(path, flags):
        raise OSError(errno.EACCES, "denied")

    monkeypatch.setattr(workspaces.os, "open", raise_denied)

    with pytest.raises(OSError, match="denied"), open_regular_file(target):
        pass


def test_open_regular_file_rejects_reparse_point_reported_by_fstat(
    tmp_path, monkeypatch
) -> None:
    target = tmp_path / "design.kicad_sch"
    _write(target)
    real = target.lstat()

    def fake_fstat(descriptor):
        return SimpleNamespace(
            st_mode=stat.S_IFREG | 0o644,
            st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
            st_dev=real.st_dev,
            st_ino=real.st_ino,
        )

    monkeypatch.setattr(workspaces.os, "fstat", fake_fstat)

    with pytest.raises(WorkspaceLinkError), open_regular_file(target):
        pass


def test_open_regular_file_detects_post_open_entry_replacement(
    tmp_path, monkeypatch
) -> None:
    target = tmp_path / "design.kicad_sch"
    _write(target)
    real = target.lstat()
    original = workspaces.assert_supported_entry
    calls = {"count": 0}

    def flaky(path):
        calls["count"] += 1
        if calls["count"] == 1:
            return original(path)
        return SimpleNamespace(
            st_mode=real.st_mode,
            st_dev=real.st_dev,
            st_ino=real.st_ino + 1,
            st_file_attributes=0,
        )

    monkeypatch.setattr(workspaces, "assert_supported_entry", flaky)

    with pytest.raises(WorkspaceEntryError), open_regular_file(target):
        pass


def test_copy_rejects_non_directory_source(tmp_path) -> None:
    source = tmp_path / "not-a-project.txt"
    _write(source)

    with pytest.raises(ValueError, match="directory"):
        WorkspaceCopier(max_files=10, max_bytes=1000).copy(source, tmp_path / "out")


def test_copy_recurses_into_subdirectories(tmp_path) -> None:
    source = tmp_path / "project"
    (source / "nested" / "deeper").mkdir(parents=True)
    _write(source / "nested" / "deeper" / "board.kicad_pcb", "pcb-bytes")
    _write(source / "top.txt", "top-bytes")
    destination = tmp_path / "workspace"

    WorkspaceCopier(max_files=10, max_bytes=1000).copy(source, destination)

    assert (destination / "top.txt").read_text(encoding="utf-8") == "top-bytes"
    assert (
        destination / "nested" / "deeper" / "board.kicad_pcb"
    ).read_text(encoding="utf-8") == "pcb-bytes"


def test_copy_preserves_mode_without_fchmod(tmp_path, monkeypatch) -> None:
    source = tmp_path / "project"
    source.mkdir()
    _write(source / "design.kicad_sch")
    destination = tmp_path / "workspace"
    monkeypatch.delattr(os, "fchmod", raising=False)

    WorkspaceCopier(max_files=10, max_bytes=1000).copy(source, destination)

    assert (destination / "design.kicad_sch").read_text(
        encoding="utf-8"
    ) == "payload"


def test_remove_readonly_entry_reraises_non_permission_errors() -> None:
    with pytest.raises(OSError, match="missing"):
        _remove_readonly_entry(
            lambda path: None, "unused", OSError(errno.ENOENT, "missing")
        )


def test_remove_readonly_entry_clears_attribute_and_retries(tmp_path) -> None:
    target = tmp_path / "stuck.txt"
    _write(target)
    attempts = []

    _remove_readonly_entry(attempts.append, str(target), PermissionError())

    assert attempts == [str(target)]

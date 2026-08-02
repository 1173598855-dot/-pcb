from __future__ import annotations

import errno
import os
import shutil
import stat
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterator


KICAD_LOCK_SUFFIXES = (
    ".kicad_sch.lck",
    ".kicad_pcb.lck",
    ".kicad_pro.lck",
)


def is_kicad_lock_name(name: str) -> bool:
    return name.startswith("~") and name.endswith(KICAD_LOCK_SUFFIXES)


def normalize_snapshot_excludes(
    values: frozenset[str],
) -> frozenset[PurePosixPath]:
    normalized: set[PurePosixPath] = set()
    for value in values:
        path = PurePosixPath(value)
        if (
            not value
            or path.is_absolute()
            or path.as_posix() != value
            or "\\" in value
            or (path.parts and path.parts[0].endswith(":"))
            or any(
                part in {"", ".", "..", ".git", "pcbflow.yaml"}
                for part in path.parts
            )
            or any(part.startswith(".pcbflow-tmp-") for part in path.parts)
        ):
            raise ValueError(f"invalid snapshot exclusion: {value}")
        normalized.add(path)
    return frozenset(normalized)


def is_snapshot_excluded(
    relative: PurePosixPath,
    registered_excludes: frozenset[PurePosixPath],
) -> bool:
    if ".git" in relative.parts or is_kicad_lock_name(relative.name):
        return True
    return any(
        relative == excluded or excluded in relative.parents
        for excluded in registered_excludes
    )


class WorkspaceLimitError(ValueError):
    pass


class WorkspaceLinkError(ValueError):
    pass


class WorkspaceEntryError(ValueError):
    pass


def assert_supported_entry(path: Path) -> os.stat_result:
    metadata = path.lstat()
    if _is_link_or_reparse_point(metadata):
        raise WorkspaceLinkError(str(path))
    return metadata


def _is_link_or_reparse_point(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_flag)


def _same_entry(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


@contextmanager
def open_regular_file(path: Path) -> Iterator[BinaryIO]:
    before = assert_supported_entry(path)
    if not stat.S_ISREG(before.st_mode):
        raise WorkspaceEntryError(str(path))

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if os.name != "nt":
        flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise WorkspaceLinkError(str(path)) from error
        raise

    try:
        opened = os.fstat(descriptor)
        if _is_link_or_reparse_point(opened):
            raise WorkspaceLinkError(str(path))
        if not stat.S_ISREG(opened.st_mode) or not _same_entry(before, opened):
            raise WorkspaceEntryError(str(path))
        after = assert_supported_entry(path)
        if not _same_entry(opened, after):
            raise WorkspaceEntryError(str(path))
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor = -1
            yield stream
    finally:
        if descriptor != -1:
            os.close(descriptor)


class WorkspaceCopier:
    def __init__(self, max_files: int, max_bytes: int) -> None:
        self._max_files = max_files
        self._max_bytes = max_bytes

    def copy(
        self,
        source: Path,
        destination: Path,
        *,
        exclude_names: frozenset[str] = frozenset(),
        registered_excludes: frozenset[str] = frozenset(),
    ) -> None:
        assert_supported_entry(source)
        source = source.resolve(strict=True)
        if not source.is_dir():
            raise ValueError("workspace source must be a directory")
        normalized_excludes = normalize_snapshot_excludes(registered_excludes)
        file_count = 0
        total_bytes = 0
        for root, directories, files in os.walk(source, followlinks=False):
            root_path = Path(root)
            root_relative = root_path.relative_to(source)
            directories[:] = [
                name
                for name in directories
                if name not in exclude_names
                and not is_snapshot_excluded(
                    PurePosixPath((root_relative / name).as_posix()),
                    normalized_excludes,
                )
            ]
            for name in [*directories, *files]:
                assert_supported_entry(root_path / name)
            for name in files:
                path = root_path / name
                relative = PurePosixPath(path.relative_to(source).as_posix())
                if name in exclude_names or is_snapshot_excluded(
                    relative, normalized_excludes
                ):
                    continue
                metadata = path.stat()
                if not stat.S_ISREG(metadata.st_mode):
                    raise WorkspaceEntryError(str(path))
                if any(part.startswith(".pcbflow-tmp-") for part in relative.parts):
                    raise WorkspaceEntryError(str(path))
                file_count += 1
                total_bytes += metadata.st_size
                if file_count > self._max_files:
                    raise WorkspaceLimitError("file_count")
                if total_bytes > self._max_bytes:
                    raise WorkspaceLimitError("total_bytes")

        def ignored(directory: str, names: list[str]) -> set[str]:
            root_relative = Path(directory).resolve().relative_to(source)
            return {
                name
                for name in names
                if name in exclude_names
                or is_snapshot_excluded(
                    PurePosixPath((root_relative / name).as_posix()),
                    normalized_excludes,
                )
            }

        shutil.copytree(source, destination, symlinks=False, ignore=ignored)

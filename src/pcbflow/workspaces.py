from __future__ import annotations

import errno
import os
import shutil
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import BinaryIO

KICAD_LOCK_SUFFIXES = (
    ".kicad_sch.lck",
    ".kicad_pcb.lck",
    ".kicad_pro.lck",
)
_COPY_CHUNK_BYTES = 1024 * 1024


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


def _remove_readonly_entry(
    function: Callable[[str], object], path: str, error: BaseException
) -> None:
    if not isinstance(error, PermissionError):
        raise error
    os.chmod(path, stat.S_IWRITE)
    function(path)


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
        source_metadata = assert_supported_entry(source)
        source = source.resolve(strict=True)
        resolved_metadata = assert_supported_entry(source)
        if not _same_entry(source_metadata, resolved_metadata):
            raise WorkspaceEntryError(str(source))
        if not stat.S_ISDIR(resolved_metadata.st_mode):
            raise ValueError("workspace source must be a directory")
        normalized_excludes = normalize_snapshot_excludes(registered_excludes)
        file_count = 0
        total_bytes = 0

        def should_skip(name: str, relative_parent: PurePosixPath) -> bool:
            relative = PurePosixPath((relative_parent / name).as_posix())
            if name in exclude_names or is_snapshot_excluded(
                relative, normalized_excludes
            ):
                return True
            if any(part.startswith(".pcbflow-tmp-") for part in relative.parts):
                raise WorkspaceEntryError(str(source / relative.as_posix()))
            return False

        def copy_regular_stream(
            stream: BinaryIO, destination_path: Path, source_mode: int
        ) -> None:
            nonlocal file_count, total_bytes
            file_count += 1
            if file_count > self._max_files:
                raise WorkspaceLimitError("file_count")
            remaining_bytes = self._max_bytes - total_bytes
            with destination_path.open("wb") as destination_stream:
                while True:
                    chunk = stream.read(
                        min(_COPY_CHUNK_BYTES, remaining_bytes + 1)
                    )
                    if not chunk:
                        break
                    if len(chunk) > remaining_bytes:
                        raise WorkspaceLimitError("total_bytes")
                    destination_stream.write(chunk)
                    total_bytes += len(chunk)
                    remaining_bytes -= len(chunk)
                fchmod = getattr(os, "fchmod", None)
                mode = stat.S_IMODE(source_mode)
                if fchmod is not None:
                    fchmod(destination_stream.fileno(), mode)
                else:
                    os.chmod(destination_path, mode)

        def copy_windows_directory(
            source_directory: Path,
            destination_directory: Path,
            relative_parent: PurePosixPath,
        ) -> None:
            directory_metadata = assert_supported_entry(source_directory)
            if not stat.S_ISDIR(directory_metadata.st_mode):
                raise WorkspaceEntryError(str(source_directory))
            with os.scandir(source_directory) as entries:
                names = sorted(entry.name for entry in entries)
            for name in names:
                if should_skip(name, relative_parent):
                    continue
                source_path = source_directory / name
                destination_path = destination_directory / name
                metadata = assert_supported_entry(source_path)
                if stat.S_ISDIR(metadata.st_mode):
                    destination_path.mkdir()
                    copy_windows_directory(
                        source_path,
                        destination_path,
                        PurePosixPath((relative_parent / name).as_posix()),
                    )
                    os.chmod(destination_path, stat.S_IMODE(metadata.st_mode))
                elif stat.S_ISREG(metadata.st_mode):
                    with open_regular_file(source_path) as stream:
                        copy_regular_stream(stream, destination_path, metadata.st_mode)
                else:
                    raise WorkspaceEntryError(str(source_path))
            after = assert_supported_entry(source_directory)
            if not _same_entry(directory_metadata, after):
                raise WorkspaceEntryError(str(source_directory))

        def open_at(name: str, directory_fd: int | None, flags: int) -> int:
            try:
                if directory_fd is None:
                    return os.open(name, flags)
                return os.open(name, flags, dir_fd=directory_fd)
            except OSError as error:
                if error.errno == errno.ELOOP:
                    raise WorkspaceLinkError(name) from error
                if error.errno == errno.ENOTDIR:
                    raise WorkspaceEntryError(name) from error
                raise

        def copy_posix_directory(
            directory_fd: int,
            destination_directory: Path,
            relative_parent: PurePosixPath,
        ) -> None:
            for name in sorted(os.listdir(directory_fd)):
                if should_skip(name, relative_parent):
                    continue
                try:
                    metadata = os.stat(
                        name, dir_fd=directory_fd, follow_symlinks=False
                    )
                except OSError as error:
                    if error.errno in {errno.ENOENT, errno.ENOTDIR}:
                        raise WorkspaceEntryError(name) from error
                    raise
                if _is_link_or_reparse_point(metadata):
                    raise WorkspaceLinkError(name)
                relative = PurePosixPath((relative_parent / name).as_posix())
                destination_path = destination_directory / name
                if stat.S_ISDIR(metadata.st_mode):
                    directory_flags = (
                        os.O_RDONLY
                        | getattr(os, "O_DIRECTORY", 0)
                        | getattr(os, "O_NOFOLLOW", 0)
                    )
                    child_fd = open_at(name, directory_fd, directory_flags)
                    try:
                        opened = os.fstat(child_fd)
                        if (
                            _is_link_or_reparse_point(opened)
                            or not stat.S_ISDIR(opened.st_mode)
                            or not _same_entry(metadata, opened)
                        ):
                            raise WorkspaceEntryError(name)
                        destination_path.mkdir()
                        copy_posix_directory(child_fd, destination_path, relative)
                        os.chmod(destination_path, stat.S_IMODE(opened.st_mode))
                    finally:
                        os.close(child_fd)
                elif stat.S_ISREG(metadata.st_mode):
                    file_flags = (
                        os.O_RDONLY
                        | getattr(os, "O_NOFOLLOW", 0)
                        | getattr(os, "O_NONBLOCK", 0)
                    )
                    file_fd = open_at(name, directory_fd, file_flags)
                    try:
                        opened = os.fstat(file_fd)
                        if (
                            _is_link_or_reparse_point(opened)
                            or not stat.S_ISREG(opened.st_mode)
                            or not _same_entry(metadata, opened)
                        ):
                            raise WorkspaceEntryError(name)
                        with os.fdopen(file_fd, "rb", closefd=True) as stream:
                            file_fd = -1
                            copy_regular_stream(
                                stream, destination_path, opened.st_mode
                            )
                    finally:
                        if file_fd != -1:
                            os.close(file_fd)
                else:
                    raise WorkspaceEntryError(name)

        destination_created = False
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.mkdir()
            destination_created = True
            if os.name == "nt":
                copy_windows_directory(source, destination, PurePosixPath())
            else:
                root_flags = (
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                )
                root_fd = open_at(str(source), None, root_flags)
                try:
                    opened = os.fstat(root_fd)
                    if (
                        _is_link_or_reparse_point(opened)
                        or not stat.S_ISDIR(opened.st_mode)
                        or not _same_entry(resolved_metadata, opened)
                    ):
                        raise WorkspaceEntryError(str(source))
                    copy_posix_directory(root_fd, destination, PurePosixPath())
                finally:
                    os.close(root_fd)
                after = assert_supported_entry(source)
                if not _same_entry(opened, after):
                    raise WorkspaceEntryError(str(source))
        except BaseException:
            if destination_created:
                shutil.rmtree(destination, onexc=_remove_readonly_entry)
            raise

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session, sessionmaker

from pcbflow.domain import ProjectMode
from pcbflow.repositories import (
    IdempotencyConflictError,
    ProjectNotFoundError,
    ProjectRepository,
)
from pcbflow.workspaces import WorkspaceLinkError


def test_create_project_is_idempotent(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    repository = ProjectRepository(session_factory)
    source = tmp_path / "board"
    source.mkdir()

    first = repository.create("Controller", source, "create-controller")
    second = repository.create("Controller", source, "create-controller")

    assert first == second
    assert repository.get(first.id) == first
    assert repository.list() == [first]


def test_reusing_key_with_different_project_is_rejected(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    repository = ProjectRepository(session_factory)
    first_source = tmp_path / "first"
    second_source = tmp_path / "second"
    first_source.mkdir()
    second_source.mkdir()
    repository.create("First", first_source, "same-key")

    with pytest.raises(IdempotencyConflictError):
        repository.create("Second", second_source, "same-key")


def test_concurrent_project_creation_replays_the_winning_request(
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "concurrent-board"
    source.mkdir()
    barrier = Barrier(2)
    original_execute = Session.execute

    def synchronize_idempotency_reads(session, statement, *args, **kwargs):
        result = original_execute(session, statement, *args, **kwargs)
        rendered = str(statement)
        if "FROM projects" in rendered and "projects.idempotency_key" in rendered:
            barrier.wait(timeout=5)
        return result

    monkeypatch.setattr(Session, "execute", synchronize_idempotency_reads)
    first_repository = ProjectRepository(session_factory)
    second_repository = ProjectRepository(session_factory)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            first_repository.create_with_status,
            "Controller",
            source,
            "concurrent-create",
        )
        second = executor.submit(
            second_repository.create_with_status,
            "Controller",
            source,
            "concurrent-create",
        )
        first_result = first.result(timeout=10)
        second_result = second.result(timeout=10)

    projects = [first_result[0], second_result[0]]
    assert projects[0].id == projects[1].id
    assert sorted(created for _project, created in (first_result, second_result)) == [False, True]


def test_missing_project_raises_stable_error(
    session_factory: sessionmaker[Session],
) -> None:
    repository = ProjectRepository(session_factory)

    with pytest.raises(ProjectNotFoundError):
        repository.get("prj_missing")


def test_project_source_must_be_directory(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    repository = ProjectRepository(session_factory)
    file_path = tmp_path / "board.kicad_pro"
    file_path.write_text("fixture", encoding="utf-8")

    with pytest.raises(ValueError, match="directory"):
        repository.create("Invalid", file_path, "invalid-source")


def test_project_source_cannot_be_a_top_level_symbolic_link(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    repository = ProjectRepository(session_factory)
    target = tmp_path / "project-target"
    target.mkdir()
    source_link = tmp_path / "project-link"
    try:
        source_link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links are unavailable on this Windows host")

    with pytest.raises(WorkspaceLinkError):
        repository.create("Linked", source_link, "linked-project")


def test_project_source_cannot_be_a_top_level_reparse_point(
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = ProjectRepository(session_factory)
    source = tmp_path / "reparse-source"
    source.mkdir()
    metadata = source.lstat()
    original_lstat = Path.lstat

    def reparse_source(path: Path):
        current = original_lstat(path)
        if path == source:
            return SimpleNamespace(
                st_mode=current.st_mode,
                st_file_attributes=0x400,
            )
        return current

    monkeypatch.setattr(Path, "lstat", reparse_source)
    monkeypatch.setattr("pcbflow.workspaces.stat.FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

    with pytest.raises(WorkspaceLinkError):
        repository.create("Reparse", source, "reparse-project")


def test_new_project_exposes_registered_phase_2a_defaults(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    repository = ProjectRepository(session_factory)
    source = tmp_path / "registered"
    source.mkdir()

    project = repository.create("Registered", source, "registered-defaults")

    assert project.mode is ProjectMode.REGISTERED
    assert project.managed_repo_key is None
    assert project.current_revision is None
    assert project.project_snapshot_digest is None
    assert project.active_requirement_set_id is None
    assert project.adoption_idempotency_key is None
    assert project.adoption_input_digest is None
    assert project.managed_at is None
    assert project.version == 1

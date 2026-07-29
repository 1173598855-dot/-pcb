from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from pcbflow.domain import ProjectMode
from pcbflow.repositories import (
    IdempotencyConflictError,
    ProjectNotFoundError,
    ProjectRepository,
)


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

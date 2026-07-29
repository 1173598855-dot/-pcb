from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from pcbflow.design_tables import OutboxEventRow
from pcbflow.domain import ProjectMode
from pcbflow.revision_store import (
    ProjectRevisionNotFoundError,
    ProjectRevisionStore,
)
from pcbflow.repositories import (
    IdempotencyConflictError,
    ProjectRepository,
    RevisionConflictError,
)


def test_project_can_be_marked_managed_and_revision_compared(
    session_factory, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    projects = ProjectRepository(session_factory)
    revisions = ProjectRevisionStore(session_factory)
    project = projects.create("Controller", source, "project-create")

    managed = projects.mark_managed(
        project.id,
        repo_key=project.id,
        revision="git:1111111111111111111111111111111111111111",
        snapshot_digest="sha256:" + "a" * 64,
        adoption_idempotency_key="adopt-controller",
        adoption_input_digest="sha256:" + "c" * 64,
        source_head="git:" + "9" * 40,
        expected_version=project.version,
    )
    assert managed.mode is ProjectMode.MANAGED
    assert managed.version == 2
    assert managed.adoption_idempotency_key == "adopt-controller"
    assert managed.adoption_input_digest == "sha256:" + "c" * 64
    assert projects.find_by_adoption_key("adopt-controller") == managed

    replayed = projects.mark_managed(
        project.id,
        repo_key=project.id,
        revision="git:1111111111111111111111111111111111111111",
        snapshot_digest="sha256:" + "a" * 64,
        adoption_idempotency_key="adopt-controller",
        adoption_input_digest="sha256:" + "c" * 64,
        source_head="git:" + "9" * 40,
        expected_version=project.version,
    )
    assert replayed == managed
    assert revisions.get(project.id, managed.current_revision).revision == (
        managed.current_revision
    )

    with pytest.raises(IdempotencyConflictError):
        projects.mark_managed(
            project.id,
            repo_key=project.id,
            revision="git:1111111111111111111111111111111111111111",
            snapshot_digest="sha256:" + "a" * 64,
            adoption_idempotency_key="adopt-controller",
            adoption_input_digest="sha256:" + "d" * 64,
            source_head="git:" + "9" * 40,
            expected_version=managed.version,
        )

    rekeyed = projects.mark_managed(
        project.id,
        repo_key=project.id,
        revision="git:1111111111111111111111111111111111111111",
        snapshot_digest="sha256:" + "a" * 64,
        adoption_idempotency_key="a-second-adoption-key",
        adoption_input_digest="sha256:" + "c" * 64,
        source_head="git:" + "8" * 40,
        expected_version=managed.version,
    )
    assert rekeyed == managed

    with session_factory() as session:
        adopted_event = session.scalar(
            select(OutboxEventRow).where(
                OutboxEventRow.aggregate_id == project.id,
                OutboxEventRow.event_type == "project.adopted",
            )
        )
        assert adopted_event is not None
        assert adopted_event.payload_json["source_head"] == "git:" + "9" * 40

    with pytest.raises(RevisionConflictError):
        projects.compare_and_set_revision(
            project.id,
            expected_revision="git:" + "2" * 40,
            new_revision="git:" + "3" * 40,
            snapshot_digest="sha256:" + "b" * 64,
            expected_version=managed.version,
        )

    advanced = projects.compare_and_set_revision(
        project.id,
        expected_revision=managed.current_revision,
        new_revision="git:" + "3" * 40,
        snapshot_digest="sha256:" + "b" * 64,
        expected_version=managed.version,
    )
    assert advanced.current_revision == "git:" + "3" * 40
    assert advanced.project_snapshot_digest == "sha256:" + "b" * 64
    assert advanced.version == managed.version + 1

    with pytest.raises(ProjectRevisionNotFoundError):
        revisions.get(project.id, "git:" + "f" * 40)


def test_adoption_key_and_content_are_fully_idempotent(
    session_factory, tmp_path: Path
) -> None:
    projects = ProjectRepository(session_factory)
    first_source = tmp_path / "first"
    second_source = tmp_path / "second"
    first_source.mkdir()
    second_source.mkdir()
    first = projects.create("First", first_source, "create-first")
    second = projects.create("Second", second_source, "create-second")
    managed = projects.mark_managed(
        first.id,
        repo_key=first.id,
        revision="git:" + "1" * 40,
        snapshot_digest="sha256:" + "a" * 64,
        adoption_idempotency_key="shared-adoption-key",
        adoption_input_digest="sha256:" + "c" * 64,
        source_head=None,
        expected_version=first.version,
    )

    with pytest.raises(IdempotencyConflictError):
        projects.mark_managed(
            second.id,
            repo_key=second.id,
            revision="git:" + "2" * 40,
            snapshot_digest="sha256:" + "b" * 64,
            adoption_idempotency_key="shared-adoption-key",
            adoption_input_digest="sha256:" + "d" * 64,
            source_head=None,
            expected_version=second.version,
        )

    with pytest.raises(IdempotencyConflictError):
        projects.mark_managed(
            first.id,
            repo_key=first.id,
            revision=managed.current_revision,
            snapshot_digest=managed.project_snapshot_digest,
            adoption_idempotency_key="different-key-and-content",
            adoption_input_digest="sha256:" + "d" * 64,
            source_head=None,
            expected_version=managed.version,
        )

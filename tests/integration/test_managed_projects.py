from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from pcbflow.config import Settings
from pcbflow.container import build_container
from pcbflow.design_tables import OutboxEventRow
from pcbflow.domain import ProjectMode
from pcbflow.revisions import ProjectWorktreeDirtyError
from pcbflow.revision_store import (
    ProjectRevisionNotFoundError,
    ProjectRevisionStore,
)
from pcbflow.repositories import (
    IdempotencyConflictError,
    ProjectRepository,
    RevisionConflictError,
)


def _settings(tmp_path: Path) -> Settings:
    return Settings.from_env({"PCBFLOW_DATA_DIR": str(tmp_path / "data")})


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


def test_adopt_copies_snapshot_without_mutating_source_or_importing_git_metadata(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "board.kicad_sch").write_text("(kicad_sch)", encoding="utf-8")
    (source / "~board.kicad_sch.lck").write_text("volatile", encoding="utf-8")
    (source / ".git").mkdir()
    (source / ".git" / "config").write_text("untrusted", encoding="utf-8")
    before = {
        path.relative_to(source): path.read_bytes()
        for path in source.rglob("*")
        if path.is_file()
    }
    container = build_container(_settings(tmp_path))
    project = container.projects.create("Controller", source, "create-project")

    adopted = container.revisions.adopt(project.id, "adopt-project")

    assert adopted.mode is ProjectMode.MANAGED
    assert adopted.current_revision.startswith("git:")
    assert {
        path.relative_to(source): path.read_bytes()
        for path in source.rglob("*")
        if path.is_file()
    } == before
    with container.revisions.materialize(
        project.id, adopted.current_revision, "assert-adopt"
    ) as checkout:
        assert (checkout / "board.kicad_sch").is_file()
        assert (checkout / "pcbflow.yaml").is_file()
        assert not (checkout / "~board.kicad_sch.lck").exists()
        assert not (checkout / ".git" / "config").is_file()


def test_adopt_replay_and_rekey_return_original_for_unchanged_import(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "board.kicad_sch").write_text("(kicad_sch)", encoding="utf-8")
    container = build_container(_settings(tmp_path))
    project = container.projects.create("Controller", source, "create-replay")

    first = container.revisions.adopt(project.id, "adopt-replay")
    replay = container.revisions.adopt(project.id, "adopt-replay")
    rekeyed = container.revisions.adopt(project.id, "adopt-replay-new-key")

    assert replay == first
    assert rekeyed == first
    assert first.version == 2

    (source / "board.kicad_sch").write_text("(kicad_sch changed)", encoding="utf-8")
    with pytest.raises(IdempotencyConflictError):
        container.revisions.adopt(project.id, "adopt-replay")
    with pytest.raises(IdempotencyConflictError):
        container.revisions.adopt(project.id, "adopt-after-source-change")


def test_adopt_rejects_cross_project_key_reuse(tmp_path: Path) -> None:
    first_source = tmp_path / "first"
    second_source = tmp_path / "second"
    first_source.mkdir()
    second_source.mkdir()
    (first_source / "board.kicad_sch").write_text("(kicad_sch)", encoding="utf-8")
    (second_source / "board.kicad_sch").write_text("(kicad_sch)", encoding="utf-8")
    container = build_container(_settings(tmp_path))
    first = container.projects.create("First", first_source, "create-first")
    second = container.projects.create("Second", second_source, "create-second")

    container.revisions.adopt(first.id, "one-adoption-key")

    with pytest.raises(IdempotencyConflictError):
        container.revisions.adopt(second.id, "one-adoption-key")


def test_candidate_refs_and_snapshot_integrity_use_exact_revisions(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "board.kicad_sch").write_text("(kicad_sch)", encoding="utf-8")
    container = build_container(_settings(tmp_path))
    project = container.projects.create("Controller", source, "create-candidate")
    managed = container.revisions.adopt(project.id, "adopt-candidate")

    with container.revisions.materialize(
        project.id, managed.current_revision, "candidate"
    ) as workspace:
        container.revisions.assert_clean(
            project.id, managed.current_revision, workspace
        )
        (workspace / "board.kicad_sch").write_text(
            "(kicad_sch changed)", encoding="utf-8"
        )
        with pytest.raises(ProjectWorktreeDirtyError):
            container.revisions.assert_clean(
                project.id, managed.current_revision, workspace
            )
        candidate = container.revisions.commit_candidate(
            managed,
            workspace,
            managed.current_revision,
            "refs/pcbflow/proposals/prp_test",
            "pcbflow: proposal prp_test",
            managed.created_at,
        )

    assert container.revisions.resolve_proposal_ref(project.id, "prp_test") == (
        candidate.revision
    )
    assert container.revisions.list_proposal_refs(project.id) == {
        "refs/pcbflow/proposals/prp_test": candidate.revision
    }
    assert container.revisions.is_ancestor(
        project.id, managed.current_revision, candidate.revision
    )
    with container.revisions.materialize(
        project.id, candidate.revision, "candidate-proof"
    ) as candidate_workspace:
        assert container.revisions.snapshot_digest(candidate_workspace) == (
            candidate.snapshot_digest
        )


def test_adopt_removes_new_repo_after_proof_failure_when_project_dir_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "board.kicad_sch").write_text("(kicad_sch)", encoding="utf-8")
    container = build_container(_settings(tmp_path))
    project = container.projects.create("Controller", source, "create-cleanup")
    repo_root = container.revisions._repo(project.id).parent
    repo_root.mkdir(parents=True)
    original_digest = container.revisions.snapshot_digest
    calls = 0

    def mismatched_checkout_digest(
        root: Path, registered_excludes: frozenset[str] = frozenset()
    ) -> str:
        nonlocal calls
        calls += 1
        digest = original_digest(root, registered_excludes)
        return "sha256:" + "0" * 64 if calls == 3 else digest

    monkeypatch.setattr(container.revisions, "snapshot_digest", mismatched_checkout_digest)

    with pytest.raises(ProjectWorktreeDirtyError):
        container.revisions.adopt(project.id, "adopt-cleanup")

    assert repo_root.is_dir()
    assert not (repo_root / "repo.git").exists()

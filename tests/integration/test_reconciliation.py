from __future__ import annotations

import pytest


@pytest.fixture
def approved_requirement_set(container, managed_project, requirement_yaml: bytes):
    draft = container.requirements.import_draft(
        managed_project.id,
        requirement_yaml,
        "reconciliation-requirements",
    )
    pending = container.requirements.submit(
        draft.id, "reconciliation-requirements-submit"
    )
    return container.approvals.decide_g1(
        requirement_set_id=pending.id,
        subject_digest=pending.subject_digest(),
        decision="approve",
        actor_type="human",
        actor_id="local-user",
        comment="approved for reconciliation",
        idempotency_key="reconciliation-g1",
    )


def test_reconciler_repairs_design_ref_from_database_revision(
    container, approved_requirement_set
) -> None:
    project = container.projects.get(approved_requirement_set.project_id)
    container.revisions.git.update_ref(
        container.revisions.repo_path(project.id),
        "refs/heads/design",
        approved_requirement_set.base_revision,
        expected_revision=project.current_revision,
    )

    assert container.reconciler.run_once() == 1
    assert container.revisions.resolve_design_ref(project.id) == (
        project.current_revision
    )

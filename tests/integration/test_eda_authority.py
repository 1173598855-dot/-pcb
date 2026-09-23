from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from pcbflow.domain import EdaKind, RequestInvalidError
from pcbflow.eda import EdaAuthorityConflictError, ProjectEdaAuthorityInput
from pcbflow.eda_authority_store import ProjectEdaAuthorityStore


@pytest.fixture
def registered_project(container, tmp_path: Path):
    source = tmp_path / "registered-project"
    source.mkdir()
    return container.projects.create("Controller", source, "authority-project")


def _lceda_authority(
    *, profile_id: str = "lceda-pro-v1"
) -> ProjectEdaAuthorityInput:
    return ProjectEdaAuthorityInput(
        eda_kind=EdaKind.LCEDA_PRO,
        eda_profile_id=profile_id,
        board_profile_id="stm32-environment-controller-2l-v1",
        rulepack_digest="sha256:" + "1" * 64,
    )


def test_authority_is_immutable_and_replays_identical_input(
    container, registered_project
) -> None:
    authority = _lceda_authority()

    first = container.eda_authorities.configure(
        project_id=registered_project.id,
        authority=authority,
        idempotency_key="authority-v1",
    )

    assert container.eda_authorities.configure(
        registered_project.id, authority, "authority-v1"
    ) == first
    assert container.eda_authorities.configure(
        registered_project.id, authority, "authority-v2"
    ) == first
    with pytest.raises(EdaAuthorityConflictError):
        container.eda_authorities.configure(
            registered_project.id,
            ProjectEdaAuthorityInput(
                eda_kind=authority.eda_kind,
                eda_profile_id="lceda-pro-v2",
                board_profile_id=authority.board_profile_id,
                rulepack_digest=authority.rulepack_digest,
            ),
            "authority-v3",
        )


def test_legacy_kicad_project_has_no_authority_row(
    container, registered_project
) -> None:
    assert (
        container.eda_authorities.find_by_project_id(registered_project.id) is None
    )
    assert container.projects.get(registered_project.id) == registered_project


def test_concurrent_identical_authority_configuration_replays_the_winner(
    container, registered_project
) -> None:
    authority = _lceda_authority()
    barrier = Barrier(2)

    def configure(idempotency_key: str):
        barrier.wait(timeout=5)
        return ProjectEdaAuthorityStore(container.sessions).configure(
            registered_project.id, authority, idempotency_key
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(configure, "authority-concurrent-first")
        second = executor.submit(configure, "authority-concurrent-second")
        first_result = first.result(timeout=10)
        second_result = second.result(timeout=10)

    assert first_result == second_result
    assert (
        container.eda_authorities.find_by_project_id(registered_project.id)
        == first_result
    )


@pytest.mark.parametrize(
    ("authority", "idempotency_key"),
    (
        (
            ProjectEdaAuthorityInput(
                eda_kind=EdaKind.LCEDA_PRO,
                eda_profile_id=" lceda-pro-v1",
                board_profile_id="stm32-environment-controller-2l-v1",
                rulepack_digest="sha256:" + "1" * 64,
            ),
            "authority-v1",
        ),
        (
            ProjectEdaAuthorityInput(
                eda_kind=EdaKind.LCEDA_PRO,
                eda_profile_id="x" * 129,
                board_profile_id="stm32-environment-controller-2l-v1",
                rulepack_digest="sha256:" + "1" * 64,
            ),
            "authority-v1",
        ),
        (_lceda_authority(), " authority-v1"),
        (_lceda_authority(), "x" * 256),
    ),
)
def test_store_rejects_noncanonical_authority_fields_and_idempotency_keys(
    container, registered_project, authority, idempotency_key
) -> None:
    with pytest.raises(RequestInvalidError):
        container.eda_authorities.configure(
            registered_project.id, authority, idempotency_key
        )

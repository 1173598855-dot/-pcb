"""Edge-path tests for PcbCandidateStore: public-input resolution, capability
gate blocks, stale-revision guards, and IntegrityError recovery.

The happy paths live in test_pcb_candidates.py and the G3/G4 suites; this file
exercises the store's defensive branches directly so the idempotency and
capability-gate contracts hold even when callers misbehave or lose a race.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from pcbflow.board import BoardObjectId, PlaceFootprints, PointUm
from pcbflow.board.operations import FootprintPlacement
from pcbflow.design_tables import PcbCandidateRow
from pcbflow.domain import EdaKind, RequestInvalidError
from pcbflow.eda import ProjectEdaAuthorityInput
from pcbflow.pcb_workflow import CAPABILITY_EVIDENCE_KIND
from pcbflow.repositories import IdempotencyConflictError
from pcbflow.tables import ProjectRow

_DIGEST = "sha256:" + "a" * 64
_BASE = "git:" + "b" * 40
_BOARD = "sha256:" + "c" * 64
_BASE_SNAPSHOT = "sha256:" + "d" * 64
_CAPABILITY = "sha256:" + "e" * 64


@pytest.fixture
def lceda_project(container, tmp_path: Path):
    source = tmp_path / "lceda-edge-project"
    source.mkdir()
    (source / "board.kicad_pcb").write_text("fixture", encoding="utf-8")
    project = container.projects.create("LCEDA edge board", source, "lceda-edge-project")
    _configure_authority(container, project.id)
    return container.projects.get(project.id)


def _configure_authority(container, project_id: str) -> None:
    container.eda_authorities.configure(
        project_id,
        ProjectEdaAuthorityInput(
            eda_kind=EdaKind.LCEDA_PRO,
            eda_profile_id="lceda-pro-v1",
            board_profile_id="stm32-environment-controller-2l-v1",
            rulepack_digest=_DIGEST,
        ),
        f"{project_id}-authority",
    )


def _managed_project(container, tmp_path: Path, name: str):
    source = tmp_path / name
    source.mkdir()
    (source / "board.kicad_pcb").write_text("fixture", encoding="utf-8")
    project = container.projects.create(name, source, name)
    return container.revisions.adopt(project.id, f"{name}-adopt")


def _operation(project_id: str) -> PlaceFootprints:
    return PlaceFootprints(
        project_id=project_id,
        baseline_revision=_BASE,
        risk="low",
        rulepack_digest=_DIGEST,
        target_object_ids=(BoardObjectId("U_MCU"),),
        idempotency_key="edge-operation-1",
        expected_snapshot_digest=_BOARD,
        placements=(
            FootprintPlacement(
                BoardObjectId("U_MCU"), PointUm(50_000, 40_000), "F.Cu"
            ),
        ),
    )


def _candidate_kwargs(project_id: str, key: str, **overrides: object) -> dict:
    kwargs: dict = {
        "project_id": project_id,
        "base_revision": _BASE,
        "base_snapshot_digest": _BASE_SNAPSHOT,
        "board_snapshot_digest": _BOARD,
        "rulepack_digest": _DIGEST,
        "capability_digest": _CAPABILITY,
        "operations": (),
        "idempotency_key": key,
        "require_capability": False,
    }
    kwargs.update(overrides)
    return kwargs


def _set_current_revision(container, project_id: str, revision: str) -> None:
    with container.sessions.begin() as session:
        session.execute(
            update(ProjectRow)
            .where(ProjectRow.id == project_id)
            .values(current_revision=revision)
        )


def _hide_candidate_lookups(monkeypatch: pytest.MonkeyPatch, *, hide_first_n: int) -> None:
    """Hide the first candidate-row lookups so the store behaves as if a
    concurrent transaction had inserted the row between them."""
    hidden = {"count": 0}
    original = Session.scalar

    def scalar(self, statement, *args, **kwargs):
        if hidden["count"] < hide_first_n and "pcb_candidates" in str(statement):
            hidden["count"] += 1
            return None
        return original(self, statement, *args, **kwargs)

    monkeypatch.setattr(Session, "scalar", scalar)


def _fail_candidate_row_flush(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the candidate-row INSERT to fail once, as a lost race would."""
    original = Session.flush

    def flush(self, *args, **kwargs):
        if any(
            isinstance(obj, PcbCandidateRow) for obj in (*self.new, *self.dirty)
        ):
            raise IntegrityError("INSERT failed", {}, RuntimeError("forced duplicate"))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Session, "flush", flush)


def test_public_inputs_reject_a_project_without_a_base_revision(
    container, lceda_project
) -> None:
    with pytest.raises(RequestInvalidError, match="PCB_CANDIDATE_STALE"):
        container.pcb_candidates.create_from_public_inputs(
            project_id=lceda_project.id,
            seed=0,
            net_ids=(),
            board_snapshot_digest=_BOARD,
            capability_digest=_CAPABILITY,
            idempotency_key="public-unadopted",
        )


def test_public_inputs_require_an_lceda_authority(container, tmp_path: Path) -> None:
    project = _managed_project(container, tmp_path, "no-authority-project")

    with pytest.raises(RequestInvalidError, match="PCB_CAPABILITY_GATE_BLOCKED"):
        container.pcb_candidates.create_from_public_inputs(
            project_id=project.id,
            seed=0,
            net_ids=(),
            board_snapshot_digest=_BOARD,
            capability_digest=_CAPABILITY,
            idempotency_key="public-no-authority",
        )


def test_public_inputs_block_when_no_board_digest_is_available(
    container, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _managed_project(container, tmp_path, "no-board-digest-project")
    _configure_authority(container, project.id)
    with container.sessions.begin() as session:
        session.execute(
            update(ProjectRow)
            .where(ProjectRow.id == project.id)
            .values(project_snapshot_digest=None)
        )
    monkeypatch.setattr(
        container.pcb_candidates,
        "_revisions",
        SimpleNamespace(snapshot_digest=lambda _path: _BASE_SNAPSHOT),
    )

    with pytest.raises(RequestInvalidError, match="PCB_CAPABILITY_GATE_BLOCKED"):
        container.pcb_candidates.create_from_public_inputs(
            project_id=project.id,
            seed=0,
            net_ids=(),
            board_snapshot_digest=None,
            capability_digest=_CAPABILITY,
            idempotency_key="public-no-board-digest",
        )


def test_public_inputs_resolve_the_capability_digest_from_evidence(
    container, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _managed_project(container, tmp_path, "evidence-resolution-project")
    _configure_authority(container, project.id)
    monkeypatch.setattr(
        container.pcb_candidates,
        "_revisions",
        SimpleNamespace(snapshot_digest=lambda _path: _BASE_SNAPSHOT),
    )
    monkeypatch.setattr(
        container.evidence,
        "list_for_project",
        lambda _project_id: [
            SimpleNamespace(
                kind=CAPABILITY_EVIDENCE_KIND,
                verdict="pass",
                artifact_digest=_CAPABILITY,
            )
        ],
    )
    monkeypatch.setattr(
        container.capability_gate,
        "require_operation",
        lambda *_args: _CAPABILITY,
    )

    candidate = container.pcb_candidates.create_from_public_inputs(
        project_id=project.id,
        seed=3,
        net_ids=("I2C_SCL",),
        board_snapshot_digest=_BOARD,
        capability_digest=None,
        idempotency_key="evidence-resolution",
    )

    assert candidate.capability_digest == _CAPABILITY


def test_public_inputs_block_without_capability_evidence(
    container, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _managed_project(container, tmp_path, "no-evidence-project")
    _configure_authority(container, project.id)
    monkeypatch.setattr(
        container.pcb_candidates,
        "_revisions",
        SimpleNamespace(snapshot_digest=lambda _path: _BASE_SNAPSHOT),
    )
    monkeypatch.setattr(
        container.evidence,
        "list_for_project",
        lambda _project_id: [],
    )

    with pytest.raises(RequestInvalidError, match="PCB_CAPABILITY_GATE_BLOCKED"):
        container.pcb_candidates.create_from_public_inputs(
            project_id=project.id,
            seed=0,
            net_ids=(),
            board_snapshot_digest=_BOARD,
            capability_digest=None,
            idempotency_key="no-evidence",
        )


def test_source_snapshot_digest_fails_closed_without_revisions(
    container, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(container.pcb_candidates, "_revisions", None)

    with pytest.raises(RequestInvalidError, match="PCB_CANDIDATE_SOURCE_UNAVAILABLE"):
        container.pcb_candidates._source_snapshot_digest(Path("source"))


def test_create_requires_an_lceda_authority(container, tmp_path: Path) -> None:
    project = _managed_project(container, tmp_path, "create-no-authority-project")

    with pytest.raises(RequestInvalidError, match="project EDA authority is required"):
        container.pcb_candidates.create(
            **_candidate_kwargs(project.id, "create-no-authority")
        )


def test_create_rejects_operations_that_are_not_a_tuple(
    container, lceda_project
) -> None:
    with pytest.raises(RequestInvalidError, match="tuple of typed board operations"):
        container.pcb_candidates.create(
            **_candidate_kwargs(
                lceda_project.id,
                "non-tuple-operations",
                operations=[_operation(lceda_project.id)],
            )
        )


def test_create_rejects_unverified_output_kinds(container, lceda_project) -> None:
    with pytest.raises(RequestInvalidError, match="boardir_only"):
        container.pcb_candidates.create(
            **_candidate_kwargs(
                lceda_project.id,
                "native-output-kind",
                algorithm_evidence={"output_kind": "native"},
            )
        )


def test_create_validates_the_seed_found_in_algorithm_evidence(
    container, lceda_project
) -> None:
    with pytest.raises(RequestInvalidError, match="non-negative"):
        container.pcb_candidates.create(
            **_candidate_kwargs(
                lceda_project.id, "negative-seed", algorithm_evidence={"seed": -1}
            )
        )


def test_create_rejects_a_stale_base_revision(container, lceda_project) -> None:
    _set_current_revision(container, lceda_project.id, "git:" + "c" * 40)

    with pytest.raises(RequestInvalidError, match="PCB_CANDIDATE_STALE"):
        container.pcb_candidates.create(**_candidate_kwargs(lceda_project.id, "stale-base"))


def test_create_enforces_the_stale_guard_inside_the_transaction(
    container, lceda_project, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_current_revision(container, lceda_project.id, "git:" + "c" * 40)
    # The pre-transaction check reads the caller's stale snapshot; the
    # re-read inside the insert transaction is the real defense.
    monkeypatch.setattr(
        container.pcb_candidates,
        "_projects",
        SimpleNamespace(get=lambda _pid: SimpleNamespace(current_revision=None)),
    )

    with pytest.raises(RequestInvalidError, match="PCB_CANDIDATE_STALE"):
        container.pcb_candidates.create(**_candidate_kwargs(lceda_project.id, "stale-in-tx"))


def test_create_replays_inside_the_transaction_after_a_lost_race(
    container, lceda_project, monkeypatch: pytest.MonkeyPatch
) -> None:
    kwargs = _candidate_kwargs(lceda_project.id, "in-tx-replay")
    original = container.pcb_candidates.create(**kwargs)
    _hide_candidate_lookups(monkeypatch, hide_first_n=1)

    replayed = container.pcb_candidates.create(**kwargs)

    assert replayed.id == original.id


def test_create_recovers_a_replay_after_an_integrity_error(
    container, lceda_project, monkeypatch: pytest.MonkeyPatch
) -> None:
    kwargs = _candidate_kwargs(lceda_project.id, "integrity-replay")
    original = container.pcb_candidates.create(**kwargs)
    _hide_candidate_lookups(monkeypatch, hide_first_n=2)
    _fail_candidate_row_flush(monkeypatch)

    replayed = container.pcb_candidates.create(**kwargs)

    assert replayed.id == original.id


def test_create_maps_an_unreplayable_concurrent_insert_to_a_conflict(
    container, lceda_project, monkeypatch: pytest.MonkeyPatch
) -> None:
    kwargs = _candidate_kwargs(lceda_project.id, "integrity-conflict")
    original = container.pcb_candidates.create(**kwargs)
    _hide_candidate_lookups(monkeypatch, hide_first_n=2)
    _fail_candidate_row_flush(monkeypatch)
    monkeypatch.setattr(
        container.pcb_candidates, "_replay_matches", lambda *_args, **_kw: False
    )

    with pytest.raises(IdempotencyConflictError, match="integrity-conflict"):
        container.pcb_candidates.create(**kwargs)

    assert container.pcb_candidates.get(original.id).id == original.id

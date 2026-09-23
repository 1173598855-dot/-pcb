from __future__ import annotations

import json
from pathlib import Path

import pytest

from pcbflow.board import (
    BoardObjectId,
    FootprintPlacement,
    PlaceFootprints,
    PointUm,
    RouteNets,
)
from pcbflow.board.adapter import BoardSemanticMismatchError
from pcbflow.board.fixture_adapter import FixtureBoardAdapter

FIXTURE = Path(__file__).parents[1] / "fixtures" / "boardir" / "stm32-environment-controller-2l-v1.json"


def _operation() -> PlaceFootprints:
    return PlaceFootprints(
        project_id="fixture",
        baseline_revision="r1",
        risk="low",
        rulepack_digest="sha256:" + "0" * 64,
        target_object_ids=(BoardObjectId("U_MCU"),),
        idempotency_key="place-u-mcu",
        expected_snapshot_digest="sha256:" + "0" * 64,
        placements=(FootprintPlacement(BoardObjectId("U_MCU"), PointUm(50_000, 40_000), "F.Cu"),),
    )


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    (source / "expected-boardir.json").write_bytes(FIXTURE.read_bytes())
    return source


def test_candidate_isolated_and_output_created_inside_candidate(tmp_path: Path) -> None:
    adapter = FixtureBoardAdapter()
    source = _source(tmp_path)
    candidate = adapter.create_candidate(source, tmp_path / "candidate")

    assert candidate.path != source
    assert candidate.path.parent == tmp_path
    assert candidate.output_dir == candidate.path / "output"
    assert candidate.output_dir.is_dir()
    assert (candidate.path / "expected-boardir.json").read_bytes() == (source / "expected-boardir.json").read_bytes()


def test_create_candidate_rejects_source_or_nested_destination(tmp_path: Path) -> None:
    adapter = FixtureBoardAdapter()
    source = _source(tmp_path)
    with pytest.raises(ValueError, match="isolated"):
        adapter.create_candidate(source, source)
    with pytest.raises(ValueError, match="isolated"):
        adapter.create_candidate(source, source / "nested")


def test_adapter_rejects_unannounced_native_changes(tmp_path: Path) -> None:
    adapter = FixtureBoardAdapter(extra_native_change=True)
    candidate = adapter.create_candidate(_source(tmp_path), tmp_path / "candidate")
    before = adapter.load_snapshot(candidate.path)

    with pytest.raises(BoardSemanticMismatchError):
        adapter.apply_operations(candidate, (_operation(),), before)


def test_apply_operations_reopens_snapshot_and_preserves_opaque_nodes(tmp_path: Path) -> None:
    adapter = FixtureBoardAdapter()
    candidate = adapter.create_candidate(_source(tmp_path), tmp_path / "candidate")
    before = adapter.load_snapshot(candidate.path)

    after = adapter.apply_operations(candidate, (_operation(),), before)

    assert after.footprint("U_MCU").position == PointUm(50_000, 40_000)
    assert after.opaque_nodes == before.opaque_nodes
    assert adapter.load_snapshot(candidate.path) == after


def test_route_operation_does_not_allow_requested_net_object_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def mutate_requested_net(project_dir: Path) -> None:
        path = project_dir / "expected-boardir.json"
        data = json.loads(path.read_bytes())
        next(item for item in data["nets"] if item["id"] == "GPIO")["net_class"] = "quiet_signal"
        path.write_bytes(json.dumps(data, separators=(",", ":")).encode())

    monkeypatch.setattr(FixtureBoardAdapter, "_inject_native_change", staticmethod(mutate_requested_net))
    adapter = FixtureBoardAdapter(extra_native_change=True)
    candidate = adapter.create_candidate(_source(tmp_path), tmp_path / "candidate")
    before = adapter.load_snapshot(candidate.path)
    operation = RouteNets(
        project_id=before.profile_id,
        baseline_revision=before.canonical_digest(),
        risk="medium",
        rulepack_digest="sha256:" + "0" * 64,
        target_object_ids=(BoardObjectId("GPIO"),),
        idempotency_key="route-gpio-semantic-allowance",
        expected_snapshot_digest=before.canonical_digest(),
        net_ids=(BoardObjectId("GPIO"),),
    )

    with pytest.raises(BoardSemanticMismatchError):
        adapter.apply_operations(candidate, (operation,), before)

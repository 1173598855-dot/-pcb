from __future__ import annotations

import json
from pathlib import Path

from pcbflow.board import Autorouter, BoardSnapshot, FixtureBoardAdapter, ManufacturingRulePack


FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "boardir"


def test_route_operation_replays_additions_removals_and_preserves_locked_routes(tmp_path: Path) -> None:
    snapshot = BoardSnapshot.load_json((FIXTURE_ROOT / "stm32-environment-controller-2l-v1.json").read_bytes())
    rulepack = ManufacturingRulePack.load_json((FIXTURE_ROOT / "stm32-environment-controller-2l-rulepack.json").read_bytes())
    result = Autorouter().route(snapshot, rulepack, ("GPIO",), seed=17)
    source = tmp_path / "source"
    source.mkdir()
    (source / "expected-boardir.json").write_bytes(json.dumps(snapshot.to_canonical_dict()).encode())
    adapter = FixtureBoardAdapter()
    candidate = adapter.create_candidate(source, tmp_path / "candidate")

    applied = adapter.apply_operations(candidate, result.operations, snapshot)

    assert {item.id for item in applied.routes}.isdisjoint({item.id for item in snapshot.routes if str(item.id) == "route_gpio"})
    assert all(item in applied.routes for item in result.segments)
    assert all(item in applied.vias for item in result.vias)
    for locked in (item for item in snapshot.routes if item.route_lock):
        assert next(item for item in applied.routes if item.id == locked.id) == locked

from __future__ import annotations

from pathlib import Path

import pytest

from pcbflow.domain import EdaOperation
from pcbflow.lceda_pro import LcedaProAdapter, LcedaProCapabilityError


@pytest.mark.lceda_pro
def test_real_lceda_contract_requires_verified_official_bridge() -> None:
    adapter = LcedaProAdapter(runner=_NoopRunner(), executable=None)
    capability = adapter.probe()
    if not capability.write_verified:
        pytest.skip(f"LCEDA Pro write capability unverified: {capability.reason}")
    pytest.fail("P0 official bridge is not wired into this contract test")


class _NoopRunner:
    def run(self, argv: list[str], cwd: Path, timeout_seconds: float):  # pragma: no cover
        raise AssertionError("the unavailable executable should not be invoked")


def test_lceda_capability_gate_is_stable() -> None:
    adapter = LcedaProAdapter(runner=_NoopRunner(), executable=None)
    with pytest.raises(LcedaProCapabilityError, match="LCEDA_PRO_WRITE_CAPABILITY_UNVERIFIED"):
        adapter.require(EdaOperation.APPLY_OPERATIONS, adapter.probe())

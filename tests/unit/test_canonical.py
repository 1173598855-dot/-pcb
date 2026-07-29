from __future__ import annotations

import pytest

from pcbflow.canonical import canonical_digest, canonical_json_bytes


def test_canonical_json_is_utf8_sorted_compact_and_stable() -> None:
    left = {"中文": "值", "b": [2, 1], "a": {"z": True, "x": None}}
    right = {"a": {"x": None, "z": True}, "b": [2, 1], "中文": "值"}

    assert canonical_json_bytes(left) == canonical_json_bytes(right)
    assert canonical_json_bytes(left) == (
        b'{"a":{"x":null,"z":true},"b":[2,1],'
        b'"\xe4\xb8\xad\xe6\x96\x87":"\xe5\x80\xbc"}'
    )
    assert canonical_digest(left) == canonical_digest(right)
    assert canonical_digest(left).startswith("sha256:")


def test_canonical_json_rejects_non_finite_numbers() -> None:
    with pytest.raises(ValueError):
        canonical_json_bytes({"value": float("nan")})

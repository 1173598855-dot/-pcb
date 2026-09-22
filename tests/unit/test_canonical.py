from __future__ import annotations

import pytest

from pcbflow.canonical import canonical_digest, canonical_json_bytes, sha256_digest


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


def test_sha256_digest_hashes_exact_bytes() -> None:
    assert sha256_digest(b"PCBFlow\x00canonical") == (
        "sha256:628e002e9f8acbf117d027ce2d6bdfd1c7930c6ec73b79a4d06c53375aad3898"
    )


def test_canonical_json_rejects_non_finite_numbers() -> None:
    with pytest.raises(ValueError):
        canonical_json_bytes({"value": float("nan")})

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from pcbflow.canonical import canonical_json_bytes
from pcbflow.requirements import (
    RequirementSetPayload,
    g1_subject_digest,
    load_rendered_requirement_files,
    load_requirement_payload,
    render_requirement_files,
    requirement_digest,
)


VALID = b"""
schema_version: "1.0"
requirements:
  - id: REQ-FUNC-001
    kind: functional
    statement: The board shall expose one status LED.
    rationale: Local diagnostics.
    priority: must
    source: user
    verification_method: inspection
    acceptance_criteria: LED D1 is present in the schematic.
interfaces: []
power_rails: []
assumptions: []
verification_items:
  - id: VER-001
    requirement_ids: [REQ-FUNC-001]
    method: inspection
    acceptance_criteria: Semantic diff contains the verified LED module.
"""


def test_requirement_payload_is_strict_and_has_stable_digest() -> None:
    payload = load_requirement_payload(VALID)
    round_trip = RequirementSetPayload.model_validate_json(
        canonical_json_bytes(payload.model_dump(mode="json")), strict=True
    )
    assert requirement_digest(payload) == requirement_digest(round_trip)
    files = render_requirement_files(payload)
    assert set(files) == {
        Path("requirements/product.yaml"),
        Path("requirements/interfaces.yaml"),
        Path("requirements/power-tree.yaml"),
        Path("requirements/assumptions.yaml"),
        Path("requirements/verification.yaml"),
    }
    assert load_rendered_requirement_files(files) == payload


def test_requirement_payload_rejects_unknown_fields_and_duplicate_ids() -> None:
    with pytest.raises(ValidationError):
        load_requirement_payload(
            VALID.replace(b"source: user", b"source: user\n    x: 1")
        )

    duplicate = VALID.replace(
        b"interfaces: []",
        b"  - id: REQ-FUNC-001\n    kind: functional\n"
        b"    statement: Duplicate\n    rationale: Duplicate\n"
        b"    priority: must\n    source: user\n"
        b"    verification_method: inspection\n"
        b"    acceptance_criteria: duplicate\ninterfaces: []",
    )
    with pytest.raises(ValidationError):
        load_requirement_payload(duplicate)

    no_verification = VALID.split(b"verification_items:", 1)[0] + (
        b"verification_items: []\n"
    )
    with pytest.raises(ValidationError):
        load_requirement_payload(no_verification)


def test_requirement_payload_rejects_dangling_and_duplicate_references() -> None:
    dangling = VALID.replace(b"REQ-FUNC-001]", b"REQ-MISSING-001]")
    with pytest.raises(ValidationError):
        load_requirement_payload(dangling)

    duplicate = VALID.replace(
        b"requirement_ids: [REQ-FUNC-001]",
        b"requirement_ids: [REQ-FUNC-001, REQ-FUNC-001]",
    )
    with pytest.raises(ValidationError):
        load_requirement_payload(duplicate)


def test_requirement_payload_rejects_strict_coercion_and_non_finite_values() -> None:
    coerced = VALID.replace(b'schema_version: "1.0"', b"schema_version: 1.0")
    with pytest.raises((ValidationError, ValueError)):
        load_requirement_payload(coerced)

    non_finite = VALID.replace(
        b"interfaces: []",
        b"interfaces:\n"
        b"  - id: IF-STATUS\n"
        b"    name: STATUS\n"
        b"    direction: output\n"
        b"    nominal_voltage_v: .nan\n"
        b"    absolute_max_voltage_v: 3.3",
    )
    with pytest.raises((ValidationError, ValueError)):
        load_requirement_payload(non_finite)


def test_rendered_requirement_files_require_the_exact_view_contract() -> None:
    files = render_requirement_files(load_requirement_payload(VALID))
    missing = dict(files)
    missing.pop(Path("requirements/interfaces.yaml"))
    with pytest.raises(ValueError, match="requirement files"):
        load_rendered_requirement_files(missing)

    duplicate_section = dict(files)
    duplicate_section[Path("requirements/interfaces.yaml")] = (
        b"requirements: []\ninterfaces: []\n"
    )
    with pytest.raises(ValueError, match="sections"):
        load_rendered_requirement_files(duplicate_section)

    duplicate_yaml_key = dict(files)
    product = Path("requirements/product.yaml")
    duplicate_yaml_key[product] = (
        b'schema_version: "1.0"\n' + duplicate_yaml_key[product]
    )
    with pytest.raises(ValueError, match="duplicate YAML key"):
        load_rendered_requirement_files(duplicate_yaml_key)


def test_g1_digest_changes_when_candidate_snapshot_changes() -> None:
    payload = load_requirement_payload(VALID)
    left = g1_subject_digest(
        "prj_1",
        "reqset_1",
        requirement_digest(payload),
        "git:" + "1" * 40,
        "git:" + "2" * 40,
        "sha256:" + "a" * 64,
    )
    right = g1_subject_digest(
        "prj_1",
        "reqset_1",
        requirement_digest(payload),
        "git:" + "1" * 40,
        "git:" + "2" * 40,
        "sha256:" + "b" * 64,
    )
    assert left != right

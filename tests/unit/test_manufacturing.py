from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from pcbflow.manufacturing import (
    ManufacturingValidator,
    ReleaseArtifact,
    ReleaseManifest,
    validate_release_artifacts,
)


def test_release_rejects_mismatched_bom_and_cpl_references() -> None:
    validation = ManufacturingValidator().validate(
        bom=b"Designator,Comment\nR1,10k\n",
        cpl=b"Designator,Mid X,Mid Y\nR2,10,10\n",
        required_artifacts={"gerber", "drill", "assembly"},
    )

    assert validation.ok is False
    assert validation.findings[0].rule_id == "PCB.MFG.REFERENCE_SET_MISMATCH"


def test_release_reports_missing_required_manufacturing_artifacts() -> None:
    validation = ManufacturingValidator().validate(
        artifacts={"bom": b"Designator,Comment\nR1,10k\n"},
        required_artifacts={"gerber", "drill", "bom", "cpl", "assembly"},
    )

    assert validation.ok is False
    assert {finding.subject for finding in validation.findings} == {
        "gerber",
        "drill",
        "cpl",
        "assembly",
    }
    assert all(finding.rule_id == "PCB.MFG.ARTIFACT_MISSING" for finding in validation.findings)


def test_release_requires_explicit_dnp_and_hand_solder_declarations() -> None:
    validation = ManufacturingValidator().validate(
        bom=(
            b"Designator,Comment,DNP,HandSolder\n"
            b"R1,10k,no,no\n"
            b"C1,100nF,no,no\n"
        ),
        cpl=b"Designator,Mid X,Mid Y\nR1,10,10\n",
        required_artifacts={"bom", "cpl"},
        dnp_designators={"C1"},
        hand_solder_designators={"R1"},
    )

    assert validation.ok is False
    assert {finding.rule_id for finding in validation.findings} == {
        "PCB.MFG.DNP_DECLARATION_INVALID",
        "PCB.MFG.HAND_SOLDER_DECLARATION_INVALID",
    }


def test_release_manifest_digest_is_deterministic_for_artifact_order() -> None:
    left = ReleaseManifest(
        schema_version="1.0",
        candidate_id="pcb_candidate_1",
        candidate_digest="sha256:" + "1" * 64,
        authority_digest="sha256:" + "2" * 64,
        capability_digest="sha256:" + "3" * 64,
        rulepack_digest="sha256:" + "4" * 64,
        artifacts=(
            ReleaseArtifact("bom", "sha256:" + "a" * 64, "text/csv", 5),
            ReleaseArtifact("gerber", "sha256:" + "b" * 64, "application/gerber", 6),
        ),
    )
    right = replace(left, artifacts=tuple(reversed(left.artifacts)))

    assert left.canonical_digest() == right.canonical_digest()


def test_release_artifact_validation_checks_content_and_registered_descriptor() -> None:
    artifact = ReleaseArtifact(
        "bom",
        "sha256:" + hashlib.sha256(b"abc").hexdigest(),
        "text/csv",
        3,
    )

    assert validate_release_artifacts(
        {artifact: b"abc"},
        registered={artifact.digest: (artifact.media_type, artifact.size)},
    ) == ()

    invalid = validate_release_artifacts(
        {artifact: b"abd"},
        registered={artifact.digest: (artifact.media_type, artifact.size)},
    )
    assert invalid[0].rule_id == "PCB.MFG.ARTIFACT_TAMPERED"


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"kind": " "}, "kind"),
        ({"digest": "not-a-digest"}, "digest"),
        ({"media_type": ""}, "media type"),
        ({"size": -1}, "size"),
        ({"size": True}, "size"),
    ],
)
def test_release_artifact_rejects_invalid_descriptor_fields(
    changes: dict[str, object], message: str
) -> None:
    values: dict[str, object] = {
        "kind": "bom",
        "digest": "sha256:" + "a" * 64,
        "media_type": "text/csv",
        "size": 3,
    }
    values.update(changes)

    with pytest.raises(ValueError, match=message):
        ReleaseArtifact(**values)  # type: ignore[arg-type]


def _manifest(**changes: object) -> ReleaseManifest:
    values: dict[str, object] = {
        "schema_version": "1.0",
        "candidate_id": "pcb_candidate_1",
        "candidate_digest": "sha256:" + "1" * 64,
        "authority_digest": "sha256:" + "2" * 64,
        "capability_digest": "sha256:" + "3" * 64,
        "rulepack_digest": "sha256:" + "4" * 64,
        "artifacts": (
            ReleaseArtifact("bom", "sha256:" + "a" * 64, "text/csv", 3),
        ),
    }
    values.update(changes)
    return ReleaseManifest(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("changes", "exception", "message"),
    [
        ({"schema_version": "2.0"}, ValueError, "schema version"),
        ({"candidate_id": ""}, ValueError, "candidate id"),
        ({"candidate_digest": "bad"}, ValueError, "candidate digest"),
        ({"authority_digest": "bad"}, ValueError, "authority digest"),
        ({"capability_digest": "bad"}, ValueError, "capability digest"),
        ({"rulepack_digest": "bad"}, ValueError, "rulepack digest"),
        ({"artifacts": []}, TypeError, "tuple"),
        ({"artifacts": (object(),)}, TypeError, "ReleaseArtifact"),
    ],
)
def test_release_manifest_rejects_invalid_frozen_contract(
    changes: dict[str, object], exception: type[Exception], message: str
) -> None:
    with pytest.raises(exception, match=message):
        _manifest(**changes)


def test_release_manifest_rejects_duplicate_artifact_kinds() -> None:
    with pytest.raises(ValueError, match="duplicate artifact kinds"):
        _manifest(
            artifacts=(
                ReleaseArtifact("bom", "sha256:" + "a" * 64, "text/csv", 3),
                ReleaseArtifact("bom", "sha256:" + "b" * 64, "text/csv", 4),
            )
        )


@pytest.mark.parametrize(
    "bom",
    [
        b"",
        b"Comment\n10k\n",
        b"Designator,Comment\n,10k\n",
        b"Designator,Comment\nR1,10k\nR1,20k\n",
        b"\xff\xfe\xff",
    ],
)
def test_release_reports_invalid_bom_csv_contracts(bom: bytes) -> None:
    validation = ManufacturingValidator().validate(bom=bom)

    assert validation.ok is False
    assert validation.findings[0].rule_id == "PCB.MFG.CSV_INVALID"
    assert validation.findings[0].subject == "bom"


def test_release_accepts_a_complete_declared_population() -> None:
    validation = ManufacturingValidator().validate(
        artifacts={
            "gerber": b"gerber",
            "drill": b"drill",
            "assembly": b"assembly",
        },
        bom=(
            b"Designator,Comment,DNP,HandSolder\n"
            b"R1,10k,no,yes\n"
            b"C1,100nF,yes,no\n"
        ),
        cpl=b"Designator,Mid X,Mid Y\nR1,10,10\n",
        required_artifacts={"gerber", "drill", "bom", "cpl", "assembly"},
        dnp_designators={"C1"},
        hand_solder_designators={"R1"},
    )

    assert validation.ok is True
    assert validation.findings == ()


def test_release_reports_empty_and_nonbyte_artifacts() -> None:
    validation = ManufacturingValidator().validate(
        artifacts={"gerber": b"", "drill": "not-bytes"},  # type: ignore[dict-item]
    )

    assert {finding.subject for finding in validation.findings} == {
        "gerber",
        "drill",
    }
    assert all(
        finding.rule_id == "PCB.MFG.ARTIFACT_INVALID"
        for finding in validation.findings
    )


def test_release_artifact_validation_rejects_duplicate_kinds_and_registry_drift() -> None:
    left = ReleaseArtifact(
        "bom", "sha256:" + hashlib.sha256(b"abc").hexdigest(), "text/csv", 3
    )
    duplicate = ReleaseArtifact(
        "bom",
        "sha256:" + hashlib.sha256(b"def").hexdigest(),
        "application/csv",
        3,
    )

    findings = validate_release_artifacts(
        {left: b"abc", duplicate: b"def"},
        registered={
            left.digest: ("application/octet-stream", left.size),
            duplicate.digest: (duplicate.media_type, duplicate.size),
        },
    )

    assert [finding.rule_id for finding in findings] == [
        "PCB.MFG.ARTIFACT_DESCRIPTOR_MISMATCH",
        "PCB.MFG.ARTIFACT_DESCRIPTOR_MISMATCH",
    ]

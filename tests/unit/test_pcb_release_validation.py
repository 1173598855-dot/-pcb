from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from pcbflow.board import CandidateWorkspace, ReleaseArtifacts
from pcbflow.canonical import canonical_json_bytes
from pcbflow.domain import EdaOperation, NormalizedFinding, ValidationReport
from pcbflow.manufacturing import ManufacturingValidation
from pcbflow.pcb_candidates import PcbCandidateNotReviewableError
from pcbflow.pcb_release import (
    PcbReleaseCapabilityError,
    PcbReleaseService,
    PcbReleaseTaskHandler,
    _canonical_json,
    _existing_descriptor,
    _flagged_designators,
    _frozen_result,
    _manufacturing_error,
    _read_release_files,
    _release_payload,
    _ReleaseError,
    _ReleasePublication,
    _validate_frozen_native_drc,
    _validate_native_reports,
)


def _workspace(tmp_path: Path) -> CandidateWorkspace:
    output = tmp_path / "output"
    output.mkdir()
    return CandidateWorkspace(tmp_path / "candidate", output)


def test_release_publication_forwards_discard_and_rollback() -> None:
    actions: list[str] = []

    class Staged:
        def discard(self) -> None:
            actions.append("discard")

        def rollback(self) -> None:
            actions.append("rollback")

    publication = _ReleasePublication(descriptors=(), staged=(Staged(),))

    publication.discard()
    publication.rollback()

    assert actions == ["discard", "rollback"]


@pytest.mark.parametrize(
    ("release_factory", "message"),
    [
        (lambda _workspace: object(), "invalid release artifacts"),
        (
            lambda _workspace: ReleaseArtifacts(files=(("gerber",),)),
            "entry is invalid",
        ),
        (
            lambda workspace: ReleaseArtifacts(
                files=(("unsupported", workspace.output_dir / "unknown.out"),)
            ),
            "kind is unsupported",
        ),
        (
            lambda _workspace: ReleaseArtifacts(
                files=(("gerber", Path(__file__).resolve()),)
            ),
            "outside the candidate output",
        ),
    ],
)
def test_release_reader_rejects_unsafe_adapter_output(
    tmp_path: Path, release_factory, message: str
) -> None:
    workspace = _workspace(tmp_path)

    with pytest.raises(_ReleaseError, match=message) as raised:
        _read_release_files(release_factory(workspace), workspace)

    assert raised.value.code == "PCB_RELEASE_EXPORT_INVALID"


def test_release_reader_rejects_duplicate_output_kinds(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    first = workspace.output_dir / "first.gbr"
    second = workspace.output_dir / "second.gbr"
    first.write_bytes(b"first")
    second.write_bytes(b"second")

    with pytest.raises(_ReleaseError, match="outside the candidate output"):
        _read_release_files(
            ReleaseArtifacts(files=(("gerber", first), ("gerber", second))),
            workspace,
        )


@pytest.mark.parametrize(
    ("data", "field", "expected"),
    [
        (b"", "dnp", frozenset()),
        (b"Comment\n10k\n", "dnp", frozenset()),
        (b"Designator,DNP\nR1,yes\nR2,no\n", "dnp", frozenset({"R1"})),
        (b"\xff\xfe", "dnp", frozenset()),
    ],
)
def test_flagged_designators_fail_closed(
    data: bytes, field: str, expected: frozenset[str]
) -> None:
    assert _flagged_designators(data, field) == expected


@pytest.mark.parametrize(
    ("rule_id", "expected_code"),
    [
        ("PCB.MFG.ARTIFACT_MISSING", "MANUFACTURING_ARTIFACT_MISSING"),
        (
            "PCB.MFG.REFERENCE_SET_MISMATCH",
            "MANUFACTURING_REFERENCE_SET_MISMATCH",
        ),
        ("PCB.MFG.CSV_INVALID", "MANUFACTURING_VALIDATION_FAILED"),
    ],
)
def test_manufacturing_findings_map_to_stable_release_errors(
    rule_id: str, expected_code: str
) -> None:
    finding = NormalizedFinding(rule_id, "error", "fixture", "invalid fixture")

    error = _manufacturing_error(ManufacturingValidation((finding,)).findings)

    assert error.code == expected_code
    assert error.message == "invalid fixture"


@pytest.mark.parametrize(
    "reports",
    [
        object(),
        (),
        (ValidationReport("", ()),),
        (ValidationReport("drc", (object(),)),),
    ],
)
def test_native_drc_rejects_malformed_reports(reports: object) -> None:
    with pytest.raises(_ReleaseError) as raised:
        _validate_native_reports(reports)

    assert raised.value.code == "PCB_NATIVE_DRC_REPORT_INVALID"


def test_native_drc_rejects_blocking_findings() -> None:
    reports = (
        ValidationReport(
            "drc",
            (NormalizedFinding("LCEDA.DRC.CLEARANCE", "error", "seg_1", "blocked"),),
        ),
    )

    with pytest.raises(_ReleaseError) as raised:
        _validate_native_reports(reports)

    assert raised.value.code == "PCB_NATIVE_DRC_BLOCKED"


def test_frozen_release_result_requires_complete_g3_evidence() -> None:
    with pytest.raises(_ReleaseError, match="result is missing"):
        _frozen_result(SimpleNamespace(result=None))
    with pytest.raises(_ReleaseError, match="G3 evidence is incomplete"):
        _frozen_result(SimpleNamespace(result={"g3_decision": {"decision": "reject"}}))


def test_release_payload_requires_a_manifest_digest() -> None:
    with pytest.raises(PcbCandidateNotReviewableError):
        _release_payload(SimpleNamespace(result=None))
    with pytest.raises(PcbCandidateNotReviewableError):
        _release_payload(SimpleNamespace(result={"release": {}}))


def test_canonical_release_json_rejects_noncanonical_bytes(artifact_store) -> None:
    descriptor = artifact_store.put_bytes(
        b'{"schema_version": "1.0"}', "application/json"
    )

    with pytest.raises(_ReleaseError) as raised:
        _canonical_json(artifact_store, descriptor.digest)

    assert raised.value.code == "PCB_RELEASE_EVIDENCE_TAMPERED"


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"schema_version": "2.0", "reports": []},
        {"schema_version": "1.0", "reports": []},
        {"schema_version": "1.0", "reports": [{}]},
        {
            "schema_version": "1.0",
            "reports": [
                {
                    "findings": [
                        {
                            "rule_id": "LCEDA.DRC.CLEARANCE",
                            "severity": "blocker",
                        }
                    ]
                }
            ],
        },
    ],
)
def test_frozen_native_drc_evidence_is_strict(artifact_store, payload: object) -> None:
    descriptor = artifact_store.put_bytes(
        canonical_json_bytes(payload), "application/vnd.pcbflow.native-drc+json"
    )

    with pytest.raises(_ReleaseError):
        _validate_frozen_native_drc(artifact_store, descriptor)


def test_existing_release_evidence_requires_registered_media_type(artifact_store) -> None:
    descriptor = artifact_store.put_bytes(b"evidence", "application/json")
    evidence = SimpleNamespace(
        artifact_media_type=lambda _digest: "application/octet-stream"
    )

    with pytest.raises(_ReleaseError) as raised:
        _existing_descriptor(
            artifact_store,
            evidence,
            descriptor.digest,
            "application/json",
        )

    assert raised.value.code == "PCB_RELEASE_EVIDENCE_TAMPERED"


def test_release_service_rejects_capability_digest_drift() -> None:
    gate = SimpleNamespace(
        require_operation=lambda _project_id, _kind, _operation: "sha256:" + "a" * 64
    )
    service = PcbReleaseService(None, gate)  # type: ignore[arg-type]
    candidate = SimpleNamespace(
        project_id="prj_fixture", capability_digest="sha256:" + "b" * 64
    )

    with pytest.raises(PcbReleaseCapabilityError):
        service.require_export_capability(candidate)  # type: ignore[arg-type]


class _LegacyCapabilityGate:
    def __init__(self, digests: dict[EdaOperation, str]) -> None:
        self._digests = digests

    def require_operation(self, _project_id, _kind, operation: EdaOperation) -> str:
        return self._digests[operation]


def _release_handler_with_legacy_gate(gate: _LegacyCapabilityGate) -> PcbReleaseTaskHandler:
    return PcbReleaseTaskHandler(
        None,  # type: ignore[arg-type]
        None,
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        gate,
        None,  # type: ignore[arg-type]
        Path("."),
        None,
    )


def test_release_handler_supports_a_consistent_legacy_capability_gate() -> None:
    digest = "sha256:" + "c" * 64
    gate = _LegacyCapabilityGate({operation: digest for operation in EdaOperation})
    candidate = SimpleNamespace(project_id="prj_fixture", capability_digest=digest)

    _release_handler_with_legacy_gate(gate)._require_capabilities(candidate)  # type: ignore[arg-type]


@pytest.mark.parametrize("drift_mode", ["operations", "candidate"])
def test_release_handler_rejects_legacy_capability_digest_drift(
    drift_mode: str,
) -> None:
    digest = "sha256:" + "c" * 64
    digests = {operation: digest for operation in EdaOperation}
    candidate_digest = digest
    if drift_mode == "operations":
        digests[EdaOperation.EXPORT_RELEASE] = "sha256:" + "d" * 64
    else:
        candidate_digest = "sha256:" + "d" * 64
    candidate = SimpleNamespace(
        project_id="prj_fixture", capability_digest=candidate_digest
    )

    with pytest.raises(PcbReleaseCapabilityError):
        _release_handler_with_legacy_gate(
            _LegacyCapabilityGate(digests)
        )._require_capabilities(candidate)  # type: ignore[arg-type]

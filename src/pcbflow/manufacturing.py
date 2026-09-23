from __future__ import annotations

import csv
import hashlib
import io
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from pcbflow.canonical import canonical_digest
from pcbflow.domain import NormalizedFinding

_SHA256_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_TRUE_VALUES = frozenset({"1", "true", "yes", "y", "dnp", "hand_solder"})
_FALSE_VALUES = frozenset({"0", "false", "no", "n", ""})

# The native adapter produces the first five files. The remaining descriptors
# are frozen candidate evidence that must be bound into every release manifest.
REQUIRED_RELEASE_ARTIFACTS = frozenset(
    {
        "gerber",
        "drill",
        "bom",
        "cpl",
        "assembly",
        "native_drc",
        "rulepack",
        "candidate_summary",
    }
)


@dataclass(frozen=True, slots=True)
class ReleaseArtifact:
    kind: str
    digest: str
    media_type: str
    size: int

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not self.kind.strip():
            raise ValueError("release artifact kind must be nonblank")
        if not isinstance(self.digest, str) or _SHA256_DIGEST.fullmatch(self.digest) is None:
            raise ValueError("release artifact digest must be a sha256 digest")
        if not isinstance(self.media_type, str) or not self.media_type.strip():
            raise ValueError("release artifact media type must be nonblank")
        if type(self.size) is not int or self.size < 0:
            raise ValueError("release artifact size must be a non-negative integer")

    def to_canonical_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "digest": self.digest,
            "media_type": self.media_type,
            "size": self.size,
        }


@dataclass(frozen=True, slots=True)
class ReleaseManifest:
    schema_version: Literal["1.0"]
    candidate_id: str
    candidate_digest: str
    authority_digest: str
    capability_digest: str
    rulepack_digest: str
    artifacts: tuple[ReleaseArtifact, ...]

    def __post_init__(self) -> None:
        if self.schema_version != "1.0":
            raise ValueError("unsupported release manifest schema version")
        if not isinstance(self.candidate_id, str) or not self.candidate_id.strip():
            raise ValueError("release manifest candidate id must be nonblank")
        for field, value in (
            ("candidate digest", self.candidate_digest),
            ("authority digest", self.authority_digest),
            ("capability digest", self.capability_digest),
            ("rulepack digest", self.rulepack_digest),
        ):
            if not isinstance(value, str) or _SHA256_DIGEST.fullmatch(value) is None:
                raise ValueError(f"release manifest {field} must be a sha256 digest")
        if type(self.artifacts) is not tuple or any(
            not isinstance(item, ReleaseArtifact) for item in self.artifacts
        ):
            raise TypeError("release manifest artifacts must be a tuple of ReleaseArtifact")
        kinds = [item.kind for item in self.artifacts]
        if len(kinds) != len(set(kinds)):
            raise ValueError("release manifest must not contain duplicate artifact kinds")

    def to_canonical_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "candidate_id": self.candidate_id,
            "candidate_digest": self.candidate_digest,
            "authority_digest": self.authority_digest,
            "capability_digest": self.capability_digest,
            "rulepack_digest": self.rulepack_digest,
            "artifacts": [
                item.to_canonical_dict()
                for item in sorted(self.artifacts, key=lambda item: (item.kind, item.digest))
            ],
        }

    def canonical_digest(self) -> str:
        return canonical_digest(self.to_canonical_dict())


@dataclass(frozen=True, slots=True)
class ManufacturingValidation:
    findings: tuple[NormalizedFinding, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.findings


@dataclass(frozen=True, slots=True)
class _CsvRows:
    rows: dict[str, dict[str, str]]
    error: str | None = None


def _finding(rule_id: str, subject: str, message: str) -> NormalizedFinding:
    return NormalizedFinding(rule_id, "error", subject, message)


def _parse_csv(data: bytes | None, kind: str) -> _CsvRows:
    if data is None:
        return _CsvRows({})
    try:
        text = data.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        if reader.fieldnames is None:
            return _CsvRows({}, f"{kind} CSV header is missing")
        normalized_headers = {
            str(field).strip().casefold(): str(field).strip()
            for field in reader.fieldnames
            if field is not None
        }
        designator_column = normalized_headers.get("designator")
        if designator_column is None:
            return _CsvRows({}, f"{kind} CSV requires a Designator column")
        rows: dict[str, dict[str, str]] = {}
        for raw in reader:
            designator = str(raw.get(designator_column) or "").strip()
            if not designator:
                return _CsvRows({}, f"{kind} CSV contains a blank designator")
            if designator in rows:
                return _CsvRows({}, f"{kind} CSV contains duplicate designator {designator}")
            rows[designator] = {
                str(key).strip().casefold(): str(value or "").strip()
                for key, value in raw.items()
                if key is not None
            }
        return _CsvRows(rows)
    except (UnicodeDecodeError, csv.Error) as error:
        return _CsvRows({}, f"{kind} CSV is invalid: {error}")


def _declared(rows: Mapping[str, str], field: str) -> bool | None:
    value = rows.get(field.casefold())
    if value is None:
        return None
    normalized = value.strip().casefold()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    return None


class ManufacturingValidator:
    def validate(
        self,
        *,
        artifacts: Mapping[str, bytes] | None = None,
        bom: bytes | None = None,
        cpl: bytes | None = None,
        required_artifacts: frozenset[str] | set[str] = frozenset(),
        dnp_designators: frozenset[str] | set[str] = frozenset(),
        hand_solder_designators: frozenset[str] | set[str] = frozenset(),
    ) -> ManufacturingValidation:
        provided = dict(artifacts or {})
        if bom is not None:
            provided.setdefault("bom", bom)
        if cpl is not None:
            provided.setdefault("cpl", cpl)
        required = frozenset(required_artifacts)
        dnp = frozenset(dnp_designators)
        hand_solder = frozenset(hand_solder_designators)
        findings: list[NormalizedFinding] = []

        bom_rows = _parse_csv(provided.get("bom"), "BOM")
        cpl_rows = _parse_csv(provided.get("cpl"), "CPL")
        for kind, parsed in (("bom", bom_rows), ("cpl", cpl_rows)):
            if parsed.error is not None:
                findings.append(_finding("PCB.MFG.CSV_INVALID", kind, parsed.error))

        if bom_rows.rows and cpl_rows.rows:
            bom_population = set(bom_rows.rows) - dnp
            cpl_population = set(cpl_rows.rows)
            if bom_population != cpl_population:
                findings.append(
                    _finding(
                        "PCB.MFG.REFERENCE_SET_MISMATCH",
                        "bom/cpl",
                        "BOM/CPL designator sets differ after declared DNP exclusions",
                    )
                )

        for designator in sorted(dnp):
            row = bom_rows.rows.get(designator)
            if row is None or _declared(row, "dnp") is not True:
                findings.append(
                    _finding(
                        "PCB.MFG.DNP_DECLARATION_INVALID",
                        designator,
                        "DNP declaration must be explicitly true in the BOM",
                    )
                )
            if designator in cpl_rows.rows:
                findings.append(
                    _finding(
                        "PCB.MFG.DNP_DECLARATION_INVALID",
                        designator,
                        "DNP designator must not be present in the CPL",
                    )
                )

        for designator in sorted(hand_solder):
            row = bom_rows.rows.get(designator)
            if row is None or _declared(row, "handsolder") is not True:
                findings.append(
                    _finding(
                        "PCB.MFG.HAND_SOLDER_DECLARATION_INVALID",
                        designator,
                        "hand-solder declaration must be explicitly true in the BOM",
                    )
                )

        for kind in sorted(required - set(provided)):
            findings.append(
                _finding(
                    "PCB.MFG.ARTIFACT_MISSING",
                    kind,
                    f"required artifact missing: {kind}",
                )
            )
        for kind, data in sorted(provided.items()):
            if not isinstance(data, bytes) or not data:
                findings.append(
                    _finding(
                        "PCB.MFG.ARTIFACT_INVALID",
                        kind,
                        "manufacturing artifact must contain bytes",
                    )
                )
        return ManufacturingValidation(tuple(findings))


def validate_release_artifacts(
    artifacts: Mapping[ReleaseArtifact, bytes],
    *,
    registered: Mapping[str, tuple[str, int]] | None = None,
) -> tuple[NormalizedFinding, ...]:
    """Verify content-addressed release files before trusting a manifest."""
    findings: list[NormalizedFinding] = []
    descriptors = registered or {}
    seen_kinds: set[str] = set()
    for descriptor, content in artifacts.items():
        if descriptor.kind in seen_kinds:
            findings.append(
                _finding(
                    "PCB.MFG.ARTIFACT_DESCRIPTOR_MISMATCH",
                    descriptor.kind,
                    "release manifest contains duplicate artifact kinds",
                )
            )
            continue
        seen_kinds.add(descriptor.kind)
        actual = f"sha256:{hashlib.sha256(content).hexdigest()}"
        if actual != descriptor.digest or len(content) != descriptor.size:
            findings.append(
                _finding(
                    "PCB.MFG.ARTIFACT_TAMPERED",
                    descriptor.kind,
                    "artifact content does not match its descriptor",
                )
            )
            continue
        if registered is not None and descriptors.get(descriptor.digest) != (
            descriptor.media_type,
            descriptor.size,
        ):
            findings.append(
                _finding(
                    "PCB.MFG.ARTIFACT_DESCRIPTOR_MISMATCH",
                    descriptor.kind,
                    "registered artifact descriptor does not match manifest",
                )
            )
    return tuple(findings)


__all__ = [
    "REQUIRED_RELEASE_ARTIFACTS",
    "ManufacturingValidation",
    "ManufacturingValidator",
    "ReleaseArtifact",
    "ReleaseManifest",
    "validate_release_artifacts",
]

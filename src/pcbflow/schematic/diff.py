from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict

from pcbflow.canonical import canonical_json_bytes
from pcbflow.commands import RiskLevel, SchematicObjectRef
from pcbflow.schematic.semantic import SchematicDocument, object_ref_key


class ChangeKind(StrEnum):
    SHEET_ADDED = "sheet_added"
    SHEET_REMOVED = "sheet_removed"
    SYMBOL_ADDED = "symbol_added"
    SYMBOL_REMOVED = "symbol_removed"
    SYMBOL_PROPERTY_CHANGED = "symbol_property_changed"
    FOOTPRINT_CHANGED = "footprint_changed"
    LABEL_ADDED = "label_added"
    LABEL_REMOVED = "label_removed"
    NET_CONNECTIVITY_CHANGED = "net_connectivity_changed"


@dataclass(frozen=True, slots=True)
class ChangeSelector:
    kind: ChangeKind
    subject_ref: SchematicObjectRef
    field: str | None


@dataclass(frozen=True, slots=True)
class CommandAttribution:
    command_id: str
    requirement_ids: tuple[str, ...]
    risk: RiskLevel
    selectors: tuple[ChangeSelector, ...]


class SemanticChange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    kind: ChangeKind
    subject_ref: SchematicObjectRef
    before: object | None
    after: object | None
    field: str | None
    command_id: str
    requirement_ids: tuple[str, ...]
    risk: RiskLevel


class SemanticDiff(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    changes: tuple[SemanticChange, ...]


class UnattributedSemanticChangeError(RuntimeError):
    pass


def semantic_diff_bytes(value: SemanticDiff) -> bytes:
    return canonical_json_bytes(value.model_dump(mode="json"))


def build_semantic_diff(
    before: SchematicDocument,
    after: SchematicDocument,
    attributions: tuple[CommandAttribution, ...],
) -> SemanticDiff:
    pending: list[tuple[ChangeKind, SchematicObjectRef, object | None, object | None, str | None]] = []
    before_sheets = _index_by_ref(before.sheets)
    after_sheets = _index_by_ref(after.sheets)
    pending.extend(_added_removed(ChangeKind.SHEET_ADDED, ChangeKind.SHEET_REMOVED, before_sheets, after_sheets))

    before_symbols = _index_by_ref(before.symbols)
    after_symbols = _index_by_ref(after.symbols)
    pending.extend(_added_removed(ChangeKind.SYMBOL_ADDED, ChangeKind.SYMBOL_REMOVED, before_symbols, after_symbols))
    symbol_changes = _symbol_changes(before_symbols, after_symbols)
    pending.extend(symbol_changes)
    changed_symbol_footprints = {
        object_ref_key(reference)
        for kind, reference, _, _, _ in symbol_changes
        if kind is ChangeKind.FOOTPRINT_CHANGED
    }
    pending.extend(
        _footprint_assignment_changes(
            _index_footprints(before.footprints),
            _index_footprints(after.footprints),
            changed_symbol_footprints,
        )
    )

    before_labels = _index_by_ref(before.labels)
    after_labels = _index_by_ref(after.labels)
    pending.extend(_added_removed(ChangeKind.LABEL_ADDED, ChangeKind.LABEL_REMOVED, before_labels, after_labels))

    before_nets = _index_by_ref(before.nets)
    after_nets = _index_by_ref(after.nets)
    for key in sorted(set(before_nets) | set(after_nets)):
        previous = before_nets.get(key)
        current = after_nets.get(key)
        if previous != current:
            reference = current.ref if current is not None else previous.ref
            pending.append(
                (
                    ChangeKind.NET_CONNECTIVITY_CHANGED,
                    reference,
                    None if previous is None else asdict(previous),
                    None if current is None else asdict(current),
                    None,
                )
            )

    changes = tuple(
        _attributed_change(kind, reference, previous, current, field, attributions)
        for kind, reference, previous, current, field in pending
    )
    return SemanticDiff(
        changes=tuple(
            sorted(
                changes,
                key=lambda item: (
                    item.kind.value,
                    object_ref_key(item.subject_ref),
                    item.field or "",
                    item.command_id,
                ),
            )
        )
    )


def _index_by_ref(items: tuple[object, ...]) -> dict[str, object]:
    indexed: dict[str, object] = {}
    for item in items:
        reference = getattr(item, "ref")
        key = object_ref_key(reference)
        if key in indexed:
            raise ValueError(f"duplicate semantic object reference: {key}")
        indexed[key] = item
    return indexed


def _index_footprints(items: tuple[object, ...]) -> dict[str, object]:
    indexed: dict[str, object] = {}
    for item in items:
        key = object_ref_key(item.symbol_ref)
        if key in indexed:
            raise ValueError(f"duplicate footprint assignment: {key}")
        indexed[key] = item
    return indexed


def _added_removed(
    added_kind: ChangeKind,
    removed_kind: ChangeKind,
    before: dict[str, object],
    after: dict[str, object],
) -> list[tuple[ChangeKind, SchematicObjectRef, object | None, object | None, str | None]]:
    changes: list[tuple[ChangeKind, SchematicObjectRef, object | None, object | None, str | None]] = []
    for key in sorted(set(after) - set(before)):
        item = after[key]
        changes.append((added_kind, item.ref, None, asdict(item), None))
    for key in sorted(set(before) - set(after)):
        item = before[key]
        changes.append((removed_kind, item.ref, asdict(item), None, None))
    return changes


def _symbol_changes(
    before: dict[str, object], after: dict[str, object]
) -> list[tuple[ChangeKind, SchematicObjectRef, object | None, object | None, str | None]]:
    changes: list[tuple[ChangeKind, SchematicObjectRef, object | None, object | None, str | None]] = []
    for key in sorted(set(before) & set(after)):
        previous = before[key]
        current = after[key]
        if previous.footprint != current.footprint:
            changes.append(
                (
                    ChangeKind.FOOTPRINT_CHANGED,
                    current.ref,
                    previous.footprint,
                    current.footprint,
                    "Footprint",
                )
            )
        previous_properties = _symbol_property_values(previous)
        current_properties = _symbol_property_values(current)
        for name in sorted(set(previous_properties) | set(current_properties)):
            if name == "Footprint":
                continue
            old_value = previous_properties.get(name)
            new_value = current_properties.get(name)
            if old_value != new_value:
                changes.append(
                    (
                        ChangeKind.SYMBOL_PROPERTY_CHANGED,
                        current.ref,
                        old_value,
                        new_value,
                        name,
                    )
                )
    return changes


def _symbol_property_values(symbol: object) -> dict[str, str | None]:
    values = {property_.name: property_.value for property_ in symbol.properties}
    values["Reference"] = symbol.reference
    values["Value"] = symbol.value
    values["Footprint"] = symbol.footprint
    return values


def _footprint_assignment_changes(
    before: dict[str, object],
    after: dict[str, object],
    already_changed: set[str],
) -> list[tuple[ChangeKind, SchematicObjectRef, object | None, object | None, str | None]]:
    changes: list[tuple[ChangeKind, SchematicObjectRef, object | None, object | None, str | None]] = []
    for key in sorted(set(before) | set(after)):
        previous = before.get(key)
        current = after.get(key)
        old_library_id = None if previous is None else previous.library_id
        new_library_id = None if current is None else current.library_id
        if key not in already_changed and old_library_id != new_library_id:
            reference = current.symbol_ref if current is not None else previous.symbol_ref
            changes.append(
                (
                    ChangeKind.FOOTPRINT_CHANGED,
                    reference,
                    old_library_id,
                    new_library_id,
                    "Footprint",
                )
            )
    return changes


def _attributed_change(
    kind: ChangeKind,
    reference: SchematicObjectRef,
    previous: object | None,
    current: object | None,
    field: str | None,
    attributions: tuple[CommandAttribution, ...],
) -> SemanticChange:
    matches = [
        attribution
        for attribution in attributions
        for selector in attribution.selectors
        if selector.kind is kind
        and object_ref_key(selector.subject_ref) == object_ref_key(reference)
        and selector.field == field
    ]
    if len(matches) != 1:
        raise UnattributedSemanticChangeError(
            f"expected exactly one attribution for {kind.value}:{object_ref_key(reference)}:{field}"
        )
    attribution = matches[0]
    return SemanticChange(
        kind=kind,
        subject_ref=reference,
        before=previous,
        after=current,
        field=field,
        command_id=attribution.command_id,
        requirement_ids=attribution.requirement_ids,
        risk=attribution.risk,
    )

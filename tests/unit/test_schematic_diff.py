from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from pcbflow.commands import RiskLevel
from pcbflow.schematic.diff import (
    ChangeKind,
    ChangeSelector,
    CommandAttribution,
    UnattributedSemanticChangeError,
    build_semantic_diff,
    semantic_diff_bytes,
)
from pcbflow.schematic.semantic import FootprintAssignment, inspect_schematic


def _document():
    fixture = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "kicad"
        / "controlled-design"
    )
    return inspect_schematic(fixture)


def test_property_and_footprint_changes_are_stable_and_attributed() -> None:
    before = _document()
    symbol = before.symbols[0]
    changed_symbol = replace(
        symbol,
        value="GREEN",
        footprint="LED_SMD:LED_0603_1608Metric",
        properties=tuple(
            replace(item, value="GREEN") if item.name == "Value" else item
            for item in symbol.properties
        ),
    )
    after = replace(before, symbols=(changed_symbol,))
    attribution = CommandAttribution(
        command_id="cmd_update_led",
        requirement_ids=("REQ-FUNC-001",),
        risk=RiskLevel.LOW,
        selectors=(
            ChangeSelector(
                kind=ChangeKind.FOOTPRINT_CHANGED,
                subject_ref=symbol.ref,
                field="Footprint",
            ),
            ChangeSelector(
                kind=ChangeKind.SYMBOL_PROPERTY_CHANGED,
                subject_ref=symbol.ref,
                field="Value",
            ),
        ),
    )

    result = build_semantic_diff(before, after, (attribution,))

    assert [item.kind.value for item in result.changes] == [
        "footprint_changed",
        "symbol_property_changed",
    ]
    assert all(item.command_id == "cmd_update_led" for item in result.changes)
    assert all(item.requirement_ids == ("REQ-FUNC-001",) for item in result.changes)
    assert semantic_diff_bytes(result) == semantic_diff_bytes(result)


def test_unattributed_change_is_rejected() -> None:
    before = _document()
    after = replace(
        before,
        symbols=(replace(before.symbols[0], value="UNTRACKED"),),
    )
    with pytest.raises(UnattributedSemanticChangeError):
        build_semantic_diff(before, after, ())


def test_footprint_assignment_collection_change_is_reported() -> None:
    before = _document()
    symbol = before.symbols[0]
    after = replace(
        before,
        footprints=(
            FootprintAssignment(
                symbol_ref=symbol.ref,
                library_id="LED_SMD:LED_0603_1608Metric",
            ),
        ),
    )
    attribution = CommandAttribution(
        command_id="cmd_assign_footprint",
        requirement_ids=("REQ-FUNC-002",),
        risk=RiskLevel.LOW,
        selectors=(
            ChangeSelector(
                kind=ChangeKind.FOOTPRINT_CHANGED,
                subject_ref=symbol.ref,
                field="Footprint",
            ),
        ),
    )

    result = build_semantic_diff(before, after, (attribution,))

    assert result.changes[0].kind is ChangeKind.FOOTPRINT_CHANGED
    assert result.changes[0].before == "LED_THT:LED_D5.0mm"
    assert result.changes[0].after == "LED_SMD:LED_0603_1608Metric"


@pytest.mark.parametrize("direction", ["added", "removed"])
def test_symbol_add_remove_does_not_duplicate_derived_footprint_change(
    direction: str,
) -> None:
    before = _document()
    symbol = before.symbols[0]
    if direction == "added":
        added_ref = symbol.ref.model_copy(
            update={"object_uuid": "00000000-0000-0000-0000-000000000099"}
        )
        added_symbol = replace(symbol, ref=added_ref)
        after = replace(
            before,
            symbols=before.symbols + (added_symbol,),
            footprints=before.footprints
            + (FootprintAssignment(added_ref, symbol.footprint or ""),),
        )
        kind = ChangeKind.SYMBOL_ADDED
        reference = added_ref
    else:
        after = replace(before, symbols=(), footprints=())
        kind = ChangeKind.SYMBOL_REMOVED
        reference = symbol.ref
    attribution = CommandAttribution(
        command_id=f"cmd_symbol_{direction}",
        requirement_ids=("REQ-FUNC-003",),
        risk=RiskLevel.LOW,
        selectors=(ChangeSelector(kind=kind, subject_ref=reference, field=None),),
    )

    result = build_semantic_diff(before, after, (attribution,))

    assert [(change.kind, change.subject_ref) for change in result.changes] == [
        (kind, reference)
    ]

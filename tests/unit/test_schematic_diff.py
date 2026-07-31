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
from pcbflow.schematic.semantic import (
    FootprintAssignment,
    inspect_schematic,
)


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


def test_wire_net_name_change_is_attributed_to_one_selector(tmp_path: Path) -> None:
    before_project = tmp_path / "before"
    after_project = tmp_path / "after"
    before_project.mkdir()
    after_project.mkdir()
    for project, name in ((before_project, "NET_A"), (after_project, "NET_B")):
        (project / "board.kicad_sch").write_text(
            f"""(kicad_sch
  (version 20250114)
  (uuid 00000000-0000-0000-0000-000000000050)
  (wire (pts (xy 0 0) (xy 5 0))
    (uuid 00000000-0000-0000-0000-000000000051))
  (junction (at 5 0) (uuid 00000000-0000-0000-0000-000000000052))
  (label "{name}" (at 5 0 0)
    (uuid 00000000-0000-0000-0000-000000000053)))
""",
            encoding="utf-8",
        )
    before = inspect_schematic(before_project)
    after = inspect_schematic(after_project)
    net = before.nets[0]
    attribution = CommandAttribution(
        command_id="cmd_rename_net",
        requirement_ids=("REQ-FUNC-004",),
        risk=RiskLevel.LOW,
        selectors=(
            ChangeSelector(
                kind=ChangeKind.NET_CONNECTIVITY_CHANGED,
                subject_ref=net.ref,
                field=None,
            ),
        ),
    )

    result = build_semantic_diff(before, after, (attribution,))

    assert len(result.changes) == 1
    assert result.changes[0].kind is ChangeKind.NET_CONNECTIVITY_CHANGED
    assert result.changes[0].command_id == "cmd_rename_net"


def test_wire_member_change_requires_exactly_one_net_attribution(tmp_path: Path) -> None:
    before_project = tmp_path / "before"
    after_project = tmp_path / "after"
    before_project.mkdir()
    after_project.mkdir()
    before_source = """(kicad_sch
  (version 20250114)
  (uuid 00000000-0000-0000-0000-000000000100)
  (wire (pts (xy 0 0) (xy 4 0))
    (uuid 00000000-0000-0000-0000-000000000101)))
"""
    after_source = """(kicad_sch
  (version 20250114)
  (uuid 00000000-0000-0000-0000-000000000100)
  (wire (pts (xy 0 0) (xy 4 0))
    (uuid 00000000-0000-0000-0000-000000000101))
  (wire (pts (xy 4 0) (xy 8 0))
    (uuid 00000000-0000-0000-0000-000000000102)))
"""
    (before_project / "board.kicad_sch").write_text(before_source, encoding="utf-8")
    (after_project / "board.kicad_sch").write_text(after_source, encoding="utf-8")
    before = inspect_schematic(before_project)
    after = inspect_schematic(after_project)
    net = before.nets[0]
    attribution = CommandAttribution(
        command_id="cmd_extend_net",
        requirement_ids=("REQ-FUNC-005",),
        risk=RiskLevel.LOW,
        selectors=(
            ChangeSelector(
                kind=ChangeKind.NET_CONNECTIVITY_CHANGED,
                subject_ref=net.ref,
                field=None,
            ),
        ),
    )

    with pytest.raises(UnattributedSemanticChangeError):
        build_semantic_diff(before, after, ())
    with pytest.raises(UnattributedSemanticChangeError):
        build_semantic_diff(before, after, (attribution, attribution))

    result = build_semantic_diff(before, after, (attribution,))

    assert len(result.changes) == 1
    assert result.changes[0].before["members"] != result.changes[0].after["members"]

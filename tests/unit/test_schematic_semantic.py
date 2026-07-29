from __future__ import annotations

from pathlib import Path

import pytest

from pcbflow.schematic.semantic import (
    KicadSemanticError,
    inspect_schematic,
    parse_schematic,
)


def test_inspect_uses_kicad_uuid_as_symbol_identity() -> None:
    fixture = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "kicad"
        / "controlled-design"
    )
    parsed = parse_schematic(fixture)
    document = parsed.document

    assert document.kicad_major == 9
    assert len(document.sheets) == 1
    assert len(document.symbols) == 1
    symbol = document.symbols[0]
    assert symbol.ref.object_uuid == "00000000-0000-0000-0000-000000000002"
    assert symbol.reference == "D1"
    assert symbol.value == "\u72b6\u6001LED"
    assert parsed.location(symbol.ref).file_path.name == "board.kicad_sch"
    assert inspect_schematic(fixture) == document


def test_child_sheet_uuid_scopes_its_contained_symbols(tmp_path: Path) -> None:
    (tmp_path / "root.kicad_sch").write_text(
        """(kicad_sch
  (version 20250114)
  (uuid 00000000-0000-0000-0000-000000000010)
  (sheet
    (uuid 00000000-0000-0000-0000-000000000011)
    (property \"Sheetname\" \"Child\")
    (property \"Sheetfile\" \"child.kicad_sch\")))
""",
        encoding="utf-8",
    )
    (tmp_path / "child.kicad_sch").write_text(
        """(kicad_sch
  (version 20250114)
  (uuid 00000000-0000-0000-0000-000000000012)
  (symbol (lib_id \"Device:R\") (at 1 2 0) (unit 1)
    (uuid 00000000-0000-0000-0000-000000000013)
    (property \"Reference\" \"R1\")
    (property \"Value\" \"1k\")))
""",
        encoding="utf-8",
    )

    parsed = parse_schematic(tmp_path)
    child_sheet = next(sheet for sheet in parsed.document.sheets if sheet.name == "Child")

    assert child_sheet.ref.sheet_uuid == "00000000-0000-0000-0000-000000000010"
    assert child_sheet.ref.object_uuid == "00000000-0000-0000-0000-000000000011"
    assert parsed.document.symbols[0].ref.sheet_uuid == child_sheet.ref.object_uuid


def test_reused_child_file_creates_symbols_in_each_sheet_instance(tmp_path: Path) -> None:
    (tmp_path / "root.kicad_sch").write_text(
        """(kicad_sch
  (version 20250114)
  (uuid 00000000-0000-0000-0000-000000000020)
  (sheet
    (uuid 00000000-0000-0000-0000-000000000021)
    (property "Sheetname" "First")
    (property "Sheetfile" "child.kicad_sch"))
  (sheet
    (uuid 00000000-0000-0000-0000-000000000022)
    (property "Sheetname" "Second")
    (property "Sheetfile" "child.kicad_sch")))
""",
        encoding="utf-8",
    )
    (tmp_path / "child.kicad_sch").write_text(
        """(kicad_sch
  (version 20250114)
  (uuid 00000000-0000-0000-0000-000000000023)
  (symbol (lib_id "Device:R") (at 1 2 0) (unit 1)
    (uuid 00000000-0000-0000-0000-000000000024)
    (property "Reference" "R1")
    (property "Value" "1k")))
""",
        encoding="utf-8",
    )

    parsed = parse_schematic(tmp_path)

    assert len(parsed.document.sheets) == 3
    assert {symbol.ref.sheet_uuid for symbol in parsed.document.symbols} == {
        "00000000-0000-0000-0000-000000000021",
        "00000000-0000-0000-0000-000000000022",
    }
    assert all(parsed.location(symbol.ref).file_path.name == "child.kicad_sch" for symbol in parsed.document.symbols)


def test_wire_based_connectivity_is_rejected_until_graph_extraction_exists(
    tmp_path: Path,
) -> None:
    (tmp_path / "board.kicad_sch").write_text(
        """(kicad_sch
  (version 20250114)
  (uuid 00000000-0000-0000-0000-000000000030)
  (symbol (lib_id "Device:R") (at 1 2 0) (unit 1)
    (uuid 00000000-0000-0000-0000-000000000031)
    (property "Reference" "R1")
    (property "Value" "1k")
    (pin "1" (uuid 00000000-0000-0000-0000-000000000032)))
  (wire (pts (xy 1 2) (xy 3 2))
    (stroke (width 0) (type default))
    (uuid 00000000-0000-0000-0000-000000000033))
  (junction (at 3 2) (diameter 0) (color 0 0 0 0)
    (uuid 00000000-0000-0000-0000-000000000034))
  (label "NET" (at 3 2 0)
    (uuid 00000000-0000-0000-0000-000000000035)))
""",
        encoding="utf-8",
    )

    with pytest.raises(KicadSemanticError, match="wire/junction"):
        parse_schematic(tmp_path)

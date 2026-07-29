from __future__ import annotations

from pathlib import Path

import pytest

from pcbflow.schematic.semantic import (
    KicadSemanticError,
    Point,
    inspect_schematic,
    object_ref_key,
    parse_schematic,
)


RESISTOR_LIBRARY = """(lib_symbols
  (symbol "Test:R"
    (symbol "Test:R_1_1"
      (pin passive line (at -2 0 0)
        (name "A") (number "1")))))
"""


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
  {RESISTOR_LIBRARY}
  (symbol (lib_id "Test:R") (at 1 2 0) (unit 1)
    (uuid 00000000-0000-0000-0000-000000000024)
    (property "Reference" "R1")
    (property "Value" "1k")
    (pin "1" (uuid 00000000-0000-0000-0000-000000000025))))
""".format(RESISTOR_LIBRARY=RESISTOR_LIBRARY),
        encoding="utf-8",
    )

    parsed = parse_schematic(tmp_path)

    assert len(parsed.document.sheets) == 3
    assert {symbol.ref.sheet_uuid for symbol in parsed.document.symbols} == {
        "00000000-0000-0000-0000-000000000021",
        "00000000-0000-0000-0000-000000000022",
    }
    assert all(
        parsed.location(symbol.ref).file_path.name == "child.kicad_sch"
        for symbol in parsed.document.symbols
    )
    first_symbol, second_symbol = parsed.document.symbols
    assert parsed.location(first_symbol.ref).node is parsed.location(second_symbol.ref).node
    assert parsed.aliases_for(first_symbol.ref) == (first_symbol.ref, second_symbol.ref)
    first_pin = first_symbol.pins[0]
    second_pin = second_symbol.pins[0]
    assert parsed.aliases_for(first_pin.ref) == (first_pin.ref, second_pin.ref)
    assert parsed.aliases_for_property(first_symbol.ref, "Value") == (
        first_symbol.ref,
        second_symbol.ref,
    )
    assert parsed.aliases_for(parsed.document.sheets[0].ref) == (
        parsed.document.sheets[0].ref,
    )


def test_wire_junction_and_label_extract_stable_net_connectivity(tmp_path: Path) -> None:
    (tmp_path / "board.kicad_sch").write_text(
        """(kicad_sch
  (version 20250114)
  (uuid 00000000-0000-0000-0000-000000000030)
  {RESISTOR_LIBRARY}
  (symbol (lib_id "Test:R") (at 1 2 0) (unit 1)
    (uuid 00000000-0000-0000-0000-000000000031)
    (property "Reference" "R1")
    (property "Value" "1k")
    (pin "1" (uuid 00000000-0000-0000-0000-000000000032)))
  (wire (pts (xy -1 2) (xy 3 2))
    (stroke (width 0) (type default))
    (uuid 00000000-0000-0000-0000-000000000033))
  (junction (at 3 2) (diameter 0) (color 0 0 0 0)
    (uuid 00000000-0000-0000-0000-000000000034))
  (label "NET" (at 3 2 0)
    (uuid 00000000-0000-0000-0000-000000000035)))
""".format(RESISTOR_LIBRARY=RESISTOR_LIBRARY),
        encoding="utf-8",
    )

    parsed = parse_schematic(tmp_path)

    assert len(parsed.document.nets) == 1
    net = parsed.document.nets[0]
    assert net.ref.object_uuid == "00000000-0000-0000-0000-000000000033"
    assert net.name == "NET"
    assert any(member.endswith(":00000000-0000-0000-0000-000000000032:1") for member in net.members)
    assert any(member.endswith(":00000000-0000-0000-0000-000000000035") for member in net.members)
    assert parsed.location(net.ref).node.head == "wire"


def test_bus_and_bus_entry_are_known_but_do_not_form_wire_nets(tmp_path: Path) -> None:
    (tmp_path / "board.kicad_sch").write_text(
        """(kicad_sch
  (version 20250114)
  (uuid 00000000-0000-0000-0000-000000000040)
  (bus (pts (xy 0 0) (xy 5 0))
    (uuid 00000000-0000-0000-0000-000000000041))
  (bus_entry (at 5 0 0) (size 2 2)
    (uuid 00000000-0000-0000-0000-000000000042)))
""",
        encoding="utf-8",
    )

    assert inspect_schematic(tmp_path).nets == ()


def test_wire_component_without_a_uuid_anchor_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "board.kicad_sch").write_text(
        """(kicad_sch
  (version 20250114)
  (uuid 00000000-0000-0000-0000-000000000060)
  (wire (pts (xy 0 0) (xy 5 0))))
""",
        encoding="utf-8",
    )

    with pytest.raises(KicadSemanticError, match="no UUID anchor"):
        inspect_schematic(tmp_path)


def test_library_pin_geometry_uses_rotation_and_mirror_for_connectivity(
    tmp_path: Path,
) -> None:
    (tmp_path / "board.kicad_sch").write_text(
        """(kicad_sch
  (version 20250114)
  (uuid 00000000-0000-0000-0000-000000000080)
  (lib_symbols
    (symbol "Test:TwoPin"
      (symbol "Test:TwoPin_1_1"
        (pin passive line (at 2 0 0) (name "A") (number "1"))
        (pin passive line (at 0 3 0) (name "B") (number "2")))))
  (symbol (lib_id "Test:TwoPin") (at 10 20 90) (mirror x) (unit 1)
    (uuid 00000000-0000-0000-0000-000000000081)
    (property "Reference" "U1")
    (property "Value" "TwoPin")
    (pin "1" (uuid 00000000-0000-0000-0000-000000000082))
    (pin "2" (uuid 00000000-0000-0000-0000-000000000083)))
  (wire (pts (xy 10 22) (xy 12 22))
    (uuid 00000000-0000-0000-0000-000000000084)))
""",
        encoding="utf-8",
    )

    document = inspect_schematic(tmp_path)
    pins = {pin.number: pin for pin in document.symbols[0].pins}

    assert pins["1"].position == Point(10, 22)
    assert pins["2"].position == Point(13, 20)
    assert any(pins["1"].ref == member_ref for member_ref in _member_refs(document))
    assert all(pins["2"].ref != member_ref for member_ref in _member_refs(document))


@pytest.mark.parametrize(
    ("rotation", "mirror", "expected"),
    [
        (0, "", Point(12, 23)),
        (90, "", Point(7, 22)),
        (180, "", Point(8, 17)),
        (270, "", Point(13, 18)),
        (0, "(mirror x)", Point(12, 17)),
    ],
)
def test_library_pin_geometry_supports_right_angle_rotation_and_mirror(
    tmp_path: Path, rotation: int, mirror: str, expected: Point
) -> None:
    (tmp_path / "board.kicad_sch").write_text(
        f"""(kicad_sch
  (version 20250114)
  (uuid 00000000-0000-0000-0000-000000000085)
  (lib_symbols
    (symbol "Test:Geometry"
      (symbol "Test:Geometry_1_1"
        (pin passive line (at 2 3 0) (name "A") (number "1")))))
  (symbol (lib_id "Test:Geometry") (at 10 20 {rotation}) {mirror} (unit 1)
    (uuid 00000000-0000-0000-0000-000000000086)
    (property "Reference" "U1")
    (property "Value" "Geometry")
    (pin "1" (uuid 00000000-0000-0000-0000-000000000087))))
""",
        encoding="utf-8",
    )

    pin = inspect_schematic(tmp_path).symbols[0].pins[0]

    assert pin.position == expected


def test_wire_crossing_requires_junction_and_endpoint_touch_does_not_join(
    tmp_path: Path,
) -> None:
    for name, junction in (("crossing", ""), ("junction", "(junction (at 2 0) (uuid 00000000-0000-0000-0000-000000000093))")):
        project = tmp_path / name
        project.mkdir()
        (project / "board.kicad_sch").write_text(
            f"""(kicad_sch
  (version 20250114)
  (uuid 00000000-0000-0000-0000-000000000090)
  (wire (pts (xy 0 0) (xy 4 0))
    (uuid 00000000-0000-0000-0000-000000000091))
  (wire (pts (xy 2 -2) (xy 2 2))
    (uuid 00000000-0000-0000-0000-000000000092))
  {junction})
""",
            encoding="utf-8",
        )
    endpoint_project = tmp_path / "endpoint"
    endpoint_project.mkdir()
    (endpoint_project / "board.kicad_sch").write_text(
        """(kicad_sch
  (version 20250114)
  (uuid 00000000-0000-0000-0000-000000000094)
  (wire (pts (xy 0 0) (xy 4 0))
    (uuid 00000000-0000-0000-0000-000000000095))
  (wire (pts (xy 2 0) (xy 2 2))
    (uuid 00000000-0000-0000-0000-000000000096)))
""",
        encoding="utf-8",
    )

    assert len(inspect_schematic(tmp_path / "crossing").nets) == 2
    assert len(inspect_schematic(tmp_path / "junction").nets) == 1
    assert len(inspect_schematic(endpoint_project).nets) == 2


def _member_refs(document):
    member_keys = {member for net in document.nets for member in net.members}
    return [
        pin.ref
        for symbol in document.symbols
        for pin in symbol.pins
        if object_ref_key(pin.ref) in member_keys
    ]

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import given, strategies as st

from pcbflow.schematic.cst import (
    CstLimitError,
    CstLimits,
    CstParseError,
    TokenKind,
    apply_edits,
    insert_before_close,
    make_atom,
    make_list,
    make_string,
    parse_cst,
    replace_node,
)


SOURCE = (
    b'(kicad_sch\r\n'
    b'  (version 20250114)\r\n'
    b'  (generator "pcbflow-test")\r\n'
    b'  (uuid 00000000-0000-0000-0000-000000000001)\r\n'
    b'  (symbol (lib_id "Device:LED")\r\n'
    b'    (uuid 00000000-0000-0000-0000-000000000002)\r\n'
    b'    (property "Reference" "D1")\r\n'
    b'    (property "Value" "\xe7\x8a\xb6\xe6\x80\x81LED"))\r\n'
    b'  (unknown_future_node (nested "preserve me")))\r\n'
)


def test_no_change_roundtrip_is_byte_exact() -> None:
    document = parse_cst(SOURCE)
    assert apply_edits(document, ()) == SOURCE
    assert document.root.head == "kicad_sch"
    assert document.root.find_children("unknown_future_node")


def test_replacing_one_string_preserves_all_other_bytes() -> None:
    document = parse_cst(SOURCE)
    symbol = document.root.find_children("symbol")[0]
    value_property = [
        node
        for node in symbol.find_children("property")
        if node.atom_text(1) == "Value"
    ][0]
    target = value_property.items[2]
    changed = apply_edits(
        document,
        (replace_node(target, make_string("GREEN")),),
    )

    assert changed[: target.start] == SOURCE[: target.start]
    assert changed[target.start : target.start + len(b'"GREEN"')] == b'"GREEN"'
    assert changed[target.start + len(b'"GREEN"') :] == SOURCE[target.end :]
    assert apply_edits(parse_cst(changed), ()) == changed


def test_node_offsets_are_original_byte_offsets() -> None:
    document = parse_cst(SOURCE)
    symbol = document.root.find_children("symbol")[0]
    value_property = [
        node
        for node in symbol.find_children("property")
        if node.atom_text(1) == "Value"
    ][0]
    value = value_property.items[2]

    assert value.start == SOURCE.index(b'"\xe7\x8a\xb6\xe6\x80\x81LED"')
    assert value.end == value.start + len(b'"\xe7\x8a\xb6\xe6\x80\x81LED"')
    assert SOURCE[value.start : value.end] == b'"\xe7\x8a\xb6\xe6\x80\x81LED"'


@given(st.text(st.characters(blacklist_categories=("Cs",)), max_size=128))
def test_arbitrary_unicode_string_node_roundtrips(value: str) -> None:
    document = parse_cst(b'(root "old")')
    changed = apply_edits(
        document,
        (replace_node(document.root.items[1], make_string(value)),),
    )
    reparsed = parse_cst(changed)
    assert reparsed.root.atom_text(1) == value
    assert apply_edits(reparsed, ()) == changed


def test_parser_enforces_size_depth_node_and_string_limits() -> None:
    with pytest.raises(CstLimitError, match="file_size"):
        parse_cst(b"(x)", CstLimits(max_file_bytes=2))
    with pytest.raises(CstLimitError, match="depth"):
        parse_cst(b"(((x)))", CstLimits(max_depth=2))
    with pytest.raises(CstLimitError, match="nodes"):
        parse_cst(b"(x y z)", CstLimits(max_nodes=2))
    with pytest.raises(CstLimitError, match="string"):
        parse_cst(b'(x "long")', CstLimits(max_string_bytes=3))


def test_tokenizer_retains_original_trivia_bytes() -> None:
    document = parse_cst(b"(root\tvalue  \r\n)")

    assert [token.kind for token in document.tokens] == [
        TokenKind.LEFT,
        TokenKind.ATOM,
        TokenKind.TRIVIA,
        TokenKind.ATOM,
        TokenKind.TRIVIA,
        TokenKind.RIGHT,
    ]
    assert b"".join(token.raw for token in document.tokens) == document.source


@pytest.mark.parametrize(
    ("source", "message"),
    [
        (b"(root \xff)", "UTF-8"),
        (b"(root \x00)", "NUL"),
        (b'(root "open)', "unterminated string"),
        (b'(root bare"quote")', "quote must begin a string token"),
        (b"(root))", "unmatched closing parenthesis"),
        (b"(root", "unmatched opening parenthesis"),
        (b"(root)(second)", "exactly one root"),
    ],
)
def test_parser_rejects_invalid_input(source: bytes, message: str) -> None:
    with pytest.raises(CstParseError, match=message):
        parse_cst(source)


def test_controlled_helpers_reject_unsafe_bare_atoms() -> None:
    for value in ("", "contains space", "has(paren", 'has"quote', "nul\x00"):
        with pytest.raises(ValueError, match="bare atom"):
            make_atom(value)

    node = make_list(make_atom("property"), make_string("safe \"value\""))
    document = parse_cst(b"(root old)")
    changed = apply_edits(document, (replace_node(document.root, node),))
    assert parse_cst(changed).root.atom_text(1) == 'safe "value"'


def test_insert_before_close_splices_controlled_subtree() -> None:
    document = parse_cst(b"(root\r\n  (known item)\r\n)")
    edit = insert_before_close(
        document.root,
        (make_list(make_atom("added"), make_string("value")),),
        indent=2,
    )

    assert apply_edits(document, (edit,)) == (
        b'(root\r\n  (known item)\r\n\n  (added "value"))'
    )


def test_committed_kicad_9_fixture_is_crlf_and_roundtrips() -> None:
    fixture = (
        Path(__file__).parents[1]
        / "fixtures"
        / "kicad"
        / "controlled-design"
        / "board.kicad_sch"
    )
    source = fixture.read_bytes()

    assert b"\r\n" in source
    assert b"\n" not in source.replace(b"\r\n", b"")
    document = parse_cst(source)

    assert apply_edits(document, ()) == source
    assert document.root.head == "kicad_sch"
    assert document.root.find_children("version")[0].atom_text(1) == "20250114"
    assert document.root.find_children("uuid")[0].atom_text(1) == (
        "00000000-0000-0000-0000-000000000001"
    )

    symbol = document.root.find_children("symbol")[0]
    assert symbol.find_children("lib_id")[0].atom_text(1) == "Device:LED"
    assert len(symbol.find_children("pin")) == 2
    custom_property = [
        node
        for node in symbol.find_children("property")
        if node.atom_text(1) == "PCBFlowCustom"
    ][0]
    assert any(ord(character) > 127 for character in custom_property.atom_text(2))

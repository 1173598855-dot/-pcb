from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum


class CstParseError(ValueError):
    pass


class CstLimitError(CstParseError):
    pass


@dataclass(frozen=True, slots=True)
class CstLimits:
    max_file_bytes: int = 20_000_000
    max_depth: int = 256
    max_nodes: int = 1_000_000
    max_string_bytes: int = 1_000_000


class TokenKind(StrEnum):
    LEFT = "left"
    RIGHT = "right"
    ATOM = "atom"
    STRING = "string"
    TRIVIA = "trivia"


@dataclass(frozen=True, slots=True)
class Token:
    kind: TokenKind
    start: int
    end: int
    raw: bytes


@dataclass(frozen=True, slots=True)
class CstAtom:
    value: str
    quoted: bool
    start: int = -1
    end: int = -1


@dataclass(frozen=True, slots=True)
class CstList:
    items: tuple[CstNode, ...]
    start: int = -1
    end: int = -1
    close_start: int = -1

    @property
    def head(self) -> str | None:
        if self.items and isinstance(self.items[0], CstAtom):
            return self.items[0].value
        return None

    def find_children(self, head: str) -> tuple[CstList, ...]:
        return tuple(
            item
            for item in self.items[1:]
            if isinstance(item, CstList) and item.head == head
        )

    def atom_text(self, index: int) -> str:
        item = self.items[index]
        if not isinstance(item, CstAtom):
            raise CstParseError(f"item {index} is not an atom")
        return item.value


CstNode = CstAtom | CstList


@dataclass(frozen=True, slots=True)
class CstDocument:
    source: bytes
    tokens: tuple[Token, ...]
    root: CstList


@dataclass(frozen=True, slots=True)
class CstEdit:
    start: int
    end: int
    replacement: bytes


_ASCII_WHITESPACE = frozenset(b" \t\r\n\v\f")


def parse_cst(data: bytes, limits: CstLimits = CstLimits()) -> CstDocument:
    if not isinstance(data, bytes):
        raise TypeError("CST source must be bytes")
    if len(data) > limits.max_file_bytes:
        raise CstLimitError("file_size limit exceeded")
    if b"\x00" in data:
        raise CstParseError("NUL byte is not permitted")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise CstParseError("source is not valid UTF-8") from error

    tokens = _tokenize(data, limits)
    return _parse_tokens(data, tokens, limits)


def _tokenize(data: bytes, limits: CstLimits) -> tuple[Token, ...]:
    tokens: list[Token] = []
    index = 0
    length = len(data)
    while index < length:
        start = index
        current = data[index]
        if current in _ASCII_WHITESPACE:
            index += 1
            while index < length and data[index] in _ASCII_WHITESPACE:
                index += 1
            tokens.append(Token(TokenKind.TRIVIA, start, index, data[start:index]))
            continue
        if current == ord("("):
            index += 1
            tokens.append(Token(TokenKind.LEFT, start, index, data[start:index]))
            continue
        if current == ord(")"):
            index += 1
            tokens.append(Token(TokenKind.RIGHT, start, index, data[start:index]))
            continue
        if current == ord('"'):
            index = _string_end(data, start, limits)
            raw = data[start:index]
            try:
                value = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError as error:
                raise CstParseError("invalid JSON-compatible string") from error
            if not isinstance(value, str):
                raise CstParseError("string token did not decode to text")
            tokens.append(Token(TokenKind.STRING, start, index, raw))
            continue

        index += 1
        while (
            index < length
            and data[index] not in _ASCII_WHITESPACE
            and data[index] not in (ord("("), ord(")"))
        ):
            index += 1
        raw = data[start:index]
        if b'"' in raw:
            raise CstParseError("quote must begin a string token")
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise CstParseError("atom is not valid UTF-8") from error
        tokens.append(Token(TokenKind.ATOM, start, index, raw))
    return tuple(tokens)


def _string_end(data: bytes, start: int, limits: CstLimits) -> int:
    index = start + 1
    while index < len(data):
        current = data[index]
        if current == ord('"'):
            string_bytes = index - start - 1
            if string_bytes > limits.max_string_bytes:
                raise CstLimitError("string limit exceeded")
            return index + 1
        if current == ord("\\"):
            index += 2
        else:
            index += 1
    raise CstParseError("unterminated string")


def _parse_tokens(
    source: bytes, tokens: tuple[Token, ...], limits: CstLimits
) -> CstDocument:
    stack: list[tuple[int, list[CstNode]]] = []
    root: CstList | None = None
    node_count = 0

    def add_node(node: CstNode) -> None:
        nonlocal root
        if stack:
            stack[-1][1].append(node)
        elif root is None and isinstance(node, CstList):
            root = node
        else:
            raise CstParseError("document must contain exactly one root list")

    def count_node() -> None:
        nonlocal node_count
        node_count += 1
        if node_count > limits.max_nodes:
            raise CstLimitError("nodes limit exceeded")

    for token in tokens:
        if token.kind is TokenKind.TRIVIA:
            continue
        if token.kind is TokenKind.LEFT:
            if len(stack) >= limits.max_depth:
                raise CstLimitError("depth limit exceeded")
            count_node()
            stack.append((token.start, []))
            continue
        if token.kind is TokenKind.RIGHT:
            if not stack:
                raise CstParseError("unmatched closing parenthesis")
            start, items = stack.pop()
            add_node(
                CstList(
                    items=tuple(items),
                    start=start,
                    end=token.end,
                    close_start=token.start,
                )
            )
            continue
        if not stack:
            raise CstParseError("document must contain exactly one root list")
        count_node()
        if token.kind is TokenKind.STRING:
            try:
                value = json.loads(token.raw.decode("utf-8"))
            except json.JSONDecodeError as error:
                raise CstParseError("invalid JSON-compatible string") from error
            if not isinstance(value, str):
                raise CstParseError("string token did not decode to text")
            add_node(CstAtom(value, True, token.start, token.end))
        else:
            add_node(CstAtom(token.raw.decode("utf-8"), False, token.start, token.end))

    if stack:
        raise CstParseError("unmatched opening parenthesis")
    if root is None:
        raise CstParseError("document must contain exactly one root list")
    return CstDocument(source=source, tokens=tokens, root=root)


def make_atom(value: str) -> CstAtom:
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or any(character.isspace() or character in '()"' for character in value)
    ):
        raise ValueError("bare atom contains reserved characters")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError("bare atom is not valid UTF-8") from error
    return CstAtom(value=value, quoted=False)


def make_string(value: str) -> CstAtom:
    if not isinstance(value, str):
        raise TypeError("string value must be text")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError("string value is not valid UTF-8") from error
    return CstAtom(value=value, quoted=True)


def make_list(*items: CstNode) -> CstList:
    if not all(isinstance(item, (CstAtom, CstList)) for item in items):
        raise TypeError("list items must be CST nodes")
    return CstList(items=tuple(items))


def render_node(node: CstNode) -> bytes:
    if isinstance(node, CstAtom):
        if node.quoted:
            return json.dumps(node.value, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        return node.value.encode("utf-8")
    if isinstance(node, CstList):
        return b"(" + b" ".join(render_node(item) for item in node.items) + b")"
    raise TypeError("node must be a CST atom or list")


def replace_node(target: CstNode, replacement: CstNode) -> CstEdit:
    if target.start < 0 or target.end < target.start:
        raise ValueError("target is not attached to a parsed document")
    return CstEdit(target.start, target.end, render_node(replacement))


def insert_before_close(
    parent: CstList, nodes: tuple[CstNode, ...], indent: int
) -> CstEdit:
    if parent.close_start < 0:
        raise ValueError("parent is not attached to a parsed document")
    if not isinstance(indent, int) or indent < 0:
        raise ValueError("indent must be a non-negative integer")
    if not all(isinstance(node, (CstAtom, CstList)) for node in nodes):
        raise TypeError("inserted nodes must be CST nodes")
    prefix = b"\n" + (b" " * indent)
    replacement = b"".join(prefix + render_node(node) for node in nodes)
    return CstEdit(parent.close_start, parent.close_start, replacement)


def apply_edits(document: CstDocument, edits: tuple[CstEdit, ...]) -> bytes:
    ordered = sorted(edits, key=lambda edit: (edit.start, edit.end))
    cursor = 0
    output = bytearray()
    for edit in ordered:
        if edit.start < cursor or edit.end < edit.start or edit.end > len(document.source):
            raise ValueError("CST edits overlap or escape the source")
        output.extend(document.source[cursor : edit.start])
        output.extend(edit.replacement)
        cursor = edit.end
    output.extend(document.source[cursor:])
    return bytes(output)

"""Tree-sitter code structure: classes, functions and the calls inside them."""

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import cache
from types import MappingProxyType
from typing import Literal

import tree_sitter_javascript
import tree_sitter_python
import tree_sitter_typescript
from tree_sitter import Language, Node, Parser

LANGUAGES: Mapping[str, str] = MappingProxyType(
    {
        ".py": "python",
        ".js": "javascript",
        ".jsx": "javascript",
        ".mjs": "javascript",
        ".cjs": "javascript",
        ".ts": "typescript",
        ".tsx": "tsx",
    }
)


@dataclass(frozen=True)
class Symbol:
    kind: Literal["Class", "Function"]
    name: str
    qualname: str
    start_line: int
    end_line: int
    calls: tuple[str, ...] = ()


@dataclass(frozen=True)
class FileStructure:
    path: str
    language: str
    hash: str
    symbols: tuple[Symbol, ...]


@dataclass(frozen=True)
class _Spec:
    class_kinds: frozenset[str]
    function_kinds: frozenset[str]
    call_kind: str
    member_kind: str
    member_field: str


_PYTHON = _Spec(
    frozenset({"class_definition"}),
    frozenset({"function_definition"}),
    "call",
    "attribute",
    "attribute",
)
_JS = _Spec(
    frozenset({"class_declaration", "abstract_class_declaration"}),
    frozenset(
        {"function_declaration", "generator_function_declaration", "method_definition"}
    ),
    "call_expression",
    "member_expression",
    "property",
)
_SPECS = {"python": _PYTHON, "javascript": _JS, "typescript": _JS, "tsx": _JS}
_FUNCTION_VALUES = frozenset({"arrow_function", "function_expression"})


@cache
def _parser(language: str) -> Parser:
    grammar = {
        "python": tree_sitter_python.language,
        "javascript": tree_sitter_javascript.language,
        "typescript": tree_sitter_typescript.language_typescript,
        "tsx": tree_sitter_typescript.language_tsx,
    }[language]
    return Parser(Language(grammar()))


def language_for(path: str) -> str | None:
    dot = path.rfind(".")
    return LANGUAGES.get(path[dot:]) if dot != -1 else None


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _text(node: Node | None) -> str | None:
    if node is None or node.text is None:
        return None
    return node.text.decode("utf-8", errors="replace")


def _callee(node: Node, spec: _Spec) -> str | None:
    function = node.child_by_field_name("function")
    if function is None:
        return None
    if function.type == "identifier":
        return _text(function)
    if function.type == spec.member_kind:
        return _text(function.child_by_field_name(spec.member_field))
    return None


def _symbol_of(
    node: Node, spec: _Spec
) -> tuple[Literal["Class", "Function"], str, Node] | None:
    """(kind, name, node whose span the symbol takes) if `node` defines a named symbol."""
    kind: Literal["Class", "Function"]
    if node.type in spec.class_kinds:
        kind = "Class"
    elif node.type in spec.function_kinds:
        kind = "Function"
    elif node.type == "variable_declarator" and spec is _JS:
        value = node.child_by_field_name("value")
        name = node.child_by_field_name("name")
        if value is None or value.type not in _FUNCTION_VALUES:
            return None
        if name is None or name.type != "identifier" or not (text := _text(name)):
            return None
        return "Function", text, node
    else:
        return None
    name = _text(node.child_by_field_name("name"))
    if not name:
        return None
    span = (
        node.parent
        if node.parent is not None and node.parent.type == "decorated_definition"
        else node
    )
    return kind, name, span


def parse_source(path: str, data: bytes) -> FileStructure | None:
    language = language_for(path)
    if language is None:
        return None
    spec = _SPECS[language]
    tree = _parser(language).parse(data)

    symbols: list[Symbol] = []
    calls_of: list[list[str]] = []
    seen: set[str] = set()
    # Iterative walk; each entry carries its enclosing qualname parts and the call
    # list of the nearest named function (None outside any function).
    stack: list[tuple[Node, tuple[str, ...], list[str] | None]] = [
        (tree.root_node, (), None)
    ]
    while stack:
        node, parts, calls = stack.pop()
        found = _symbol_of(node, spec)
        if found is not None:
            kind, name, span = found
            parts = (*parts, name)
            qualname = ".".join(parts)
            if kind == "Function":
                calls = []
            if qualname not in seen:
                seen.add(qualname)
                symbols.append(
                    Symbol(
                        kind,
                        name,
                        qualname,
                        span.start_point.row + 1,
                        span.end_point.row + 1,
                    )
                )
                calls_of.append(calls if kind == "Function" else [])
        elif node.type == spec.call_kind and calls is not None:
            callee = _callee(node, spec)
            if callee and callee not in calls:
                calls.append(callee)
        stack.extend((child, parts, calls) for child in reversed(node.children))

    structured = tuple(
        Symbol(s.kind, s.name, s.qualname, s.start_line, s.end_line, tuple(calls))
        for s, calls in zip(symbols, calls_of, strict=True)
    )
    return FileStructure(path, language, content_hash(data), structured)


def innermost(symbols: Iterable[Symbol], line: int) -> Symbol | None:
    covering = [s for s in symbols if s.start_line <= line <= s.end_line]
    if not covering:
        return None
    return max(covering, key=lambda s: (s.start_line, s.start_line - s.end_line))

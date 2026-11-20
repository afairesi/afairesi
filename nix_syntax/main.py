# Copyright (c) 2026- Paschalis Bizopoulos
"""Parse Nix expressions and read or quote their literals."""

# ruff: noqa: INP001 - mkPythonPackage installs this module as __init__.py.

import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

from tree_sitter_language_pack import get_parser

if TYPE_CHECKING:
    from collections.abc import Iterable

    from tree_sitter import Node, Tree
_PARSER = get_parser("nix")


class NixSyntaxError(ValueError):
    """A Nix source file could not be parsed."""


@dataclass(frozen=True)
class Document:
    """Parsed Nix source and its concrete syntax tree."""

    source: bytes
    tree: Tree

    @property
    def root(self) -> Node:
        """Return the source expression rather than the source-code wrapper."""
        expression = self.tree.root_node.child_by_field_name("expression")
        return expression if expression is not None else self.tree.root_node

    def text(self, node: Node) -> str:
        """Decode the exact source covered by a node."""
        return self.source[node.start_byte : node.end_byte].decode("utf-8")


def parse(source: str | bytes, path: str = "<expression>") -> Document:
    """Parse valid UTF-8 Nix source or raise a stable syntax error."""
    encoded = source.encode() if isinstance(source, str) else source
    try:
        encoded.decode("utf-8")
    except UnicodeDecodeError as error:
        msg = f"{path}: source is not UTF-8"
        raise NixSyntaxError(msg) from error
    tree = _PARSER.parse(encoded)
    if tree.root_node.has_error:
        parse_error = _first_error(tree.root_node)
        row, column = parse_error.start_point
        msg = f"{path}:{row + 1}:{column + 1}: parse error"
        raise NixSyntaxError(msg)
    return Document(encoded, tree)


def _first_error(node: Node) -> Node:
    if node.is_error or node.is_missing:
        return node
    for child in node.children:
        if child.has_error or child.is_error or child.is_missing:
            return _first_error(child)
    return node


def walk(node: Node) -> Iterable[Node]:
    """Walk a syntax tree depth first."""
    yield node
    for child in node.named_children:
        yield from walk(child)


def binding_set(node: Node) -> Node | None:
    """Find the direct binding set of an attribute set or let expression."""
    return next(
        (child for child in node.named_children if child.type == "binding_set"),
        None,
    )


def quote_string(value: str) -> str:
    """Encode a quoted Nix string without enabling interpolation."""
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', r"\"")
        .replace("\n", r"\n")
        .replace("\r", r"\r")
        .replace("\t", r"\t")
        .replace("${", r"\${")
    )
    return '"' + escaped + '"'


def string_value(document: Document, node: Node) -> str | None:
    """Decode a quoted Nix literal, returning None for other expressions."""
    if node.type != "string_expression" or any(
        child.type == "interpolation" for child in node.named_children
    ):
        return None
    completed = subprocess.run(
        [  # noqa: S607
            "nix",
            "--extra-experimental-features",
            "nix-command",
            "eval",
            "--store",
            "dummy://",
            "--raw",
            "--file",
            "-",
        ],
        input=document.source[node.start_byte : node.end_byte],
        capture_output=True,
        check=True,
    )
    return completed.stdout.decode("utf-8")


def attrpath_parts(document: Document, attrpath: Node) -> Iterable[str | None]:
    """Read the attribute components in order, yielding None for dynamic names."""
    for child in attrpath.children_by_field_name("attr"):
        yield (
            document.text(child)
            if child.type == "identifier"
            else string_value(document, child)
        )


def static_attrpath(document: Document, attrpath: Node) -> tuple[str, ...] | None:
    """Extract an attribute path when every component is statically named."""
    parts = []
    for value in attrpath_parts(document, attrpath):
        if value is None:
            return None
        parts.append(value)
    return tuple(parts) if parts else None

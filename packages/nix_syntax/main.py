#!/usr/bin/env python3
# Copyright (c) 2026- Paschalis Bizopoulos
"""Shared, lossless-enough Nix parsing and rewriting helpers."""

from __future__ import annotations

import argparse
import contextlib
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
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
    path: str = "<expression>"

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
    return Document(encoded, tree, path)


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


def static_attrpath(document: Document, attrpath: Node) -> tuple[str, ...] | None:
    """Extract an attribute path when every component is statically named."""
    parts: list[str] = []
    for child in attrpath.named_children:
        text = document.text(child)
        if child.type == "identifier":
            parts.append(text)
        elif child.type == "string_expression" and not any(
            node.type == "interpolation" for node in walk(child)
        ):
            characters = iter(text[1:-1])
            decoded = []
            for character in characters:
                if character == "\\":
                    escaped = next(characters)
                    decoded.append(
                        {"n": "\n", "r": "\r", "t": "\t"}.get(escaped, escaped),
                    )
                else:
                    decoded.append(character)
            parts.append("".join(decoded))
        else:
            return None
    return tuple(parts) if parts else None


def compact(text: str) -> str:
    """Normalize layout whitespace while preserving literals and comments."""
    document = parse(text)
    edits = []

    def normalize(node: Node) -> None:
        if node.type in {"string_expression", "indented_string_expression", "comment"}:
            return
        position = node.start_byte
        line_comment = False
        for child in node.children:
            normalize_gap(position, child.start_byte, line_comment=line_comment)
            normalize(child)
            position = child.end_byte
            line_comment = child.type == "comment" and document.text(child).startswith(
                "#",
            )
        if node.children:
            normalize_gap(position, node.end_byte, line_comment=line_comment)

    def normalize_gap(start: int, end: int, *, line_comment: bool) -> None:
        gap = document.source[start:end]
        if gap and gap.isspace():
            replacement = b"\n" if line_comment else b" "
            edits.append((start, end, replacement))

    normalize(document.tree.root_node)
    return apply_edits(document.source, edits).decode().strip()


def apply_edits(source: bytes, edits: Iterable[tuple[int, int, bytes]]) -> bytes:
    """Apply non-overlapping edits in source-coordinate order."""
    result = source
    previous_start = len(source) + 1
    for start, end, replacement in sorted(edits, reverse=True):
        if not (0 <= start <= end <= len(source)) or end > previous_start:
            msg = "overlapping or invalid source edits"
            raise ValueError(msg)
        result = result[:start] + replacement + result[end:]
        previous_start = start
    return result


def write_if_changed(path: Path, contents: str) -> None:
    """Atomically replace a regular file only when its contents changed."""
    original = path.read_text(encoding="utf-8")
    if original == contents:
        return
    if path.is_symlink() or not path.is_file():
        msg = f"{path}: must be a regular file"
        raise OSError(msg)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(contents)
        os.chmod(temporary_name, path.stat().st_mode)  # noqa: PTH101
        os.replace(temporary_name, path)  # noqa: PTH105
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary_name)  # noqa: PTH108


def main() -> None:
    """Parse selected Nix files and report syntax errors without changing them."""
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog="Example: nix_syntax flake.nix packages/*/default.nix",
    )
    parser.add_argument(
        "files",
        nargs="+",
        metavar="FILE",
        help="Nix files to validate",
    )
    failed = False
    for argument in parser.parse_args().files:
        try:
            parse(Path(argument).read_bytes(), argument)
        except (OSError, NixSyntaxError) as error:  # noqa: PERF203
            print(f"error: {error}", file=sys.stderr)  # noqa: T201
            failed = True
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

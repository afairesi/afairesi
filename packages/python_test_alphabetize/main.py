#!/usr/bin/env python3
# Copyright (c) 2026- Paschalis Bizopoulos
"""Alphabetize Python tests within modules and classes without executing source."""

from __future__ import annotations

import argparse
import ast
import io
import itertools
import sys
import tokenize
from pathlib import Path


def _statement_start(
    node: ast.stmt,
    lower: int,
    comments: dict[int, int],
    decorators: list[tuple[int, int]],
) -> int:
    start = node.lineno - 1
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and (
        node.decorator_list
    ):
        start = min(
            row
            for row, column in decorators
            if lower <= row < start and column == node.col_offset
        )
    while start > lower and comments.get(start - 1) == node.col_offset:
        start -= 1
    return start


def _statement_end(
    node: ast.stmt,
    limit: int,
    lines: list[bytes],
    comments: dict[int, int],
) -> int:
    end = node.end_lineno or node.lineno
    for row in range(end, limit):
        if not lines[row].strip():
            continue
        if comments.get(row, -1) <= node.col_offset:
            break
        end = row + 1
    return end


def _line_ending(source: bytes) -> bytes:
    for ending in (b"\r\n", b"\n", b"\r"):
        if source.endswith(ending):
            return ending
    return b""


def _test_edits(
    tests: list[ast.FunctionDef | ast.AsyncFunctionDef],
    starts: dict[ast.stmt, int],
    ends: dict[ast.stmt, int],
    limits: dict[ast.stmt, int],
    lines: list[bytes],
) -> list[tuple[int, int, bytes]]:
    ordered = sorted(tests, key=lambda node: node.name)
    if tests == ordered and all(
        limits[left] == starts[right] for left, right in itertools.pairwise(tests)
    ):
        return []
    edits = []
    blocks = []
    for original, replacement_node in zip(
        tests,
        ordered,
        strict=True,
    ):
        current = b"".join(lines[starts[original] : ends[original]])
        replacement = b"".join(
            lines[starts[replacement_node] : ends[replacement_node]],
        )
        replacement = replacement.removesuffix(
            _line_ending(replacement),
        ) + _line_ending(
            current,
        )
        if original is tests[-1]:
            blocks.append(replacement)
            continue
        gap = b"".join(lines[ends[original] : limits[original]])
        end = ends[original]
        if not gap.strip():
            replacement += gap
            end = limits[original]
        blocks.append(replacement)
        edits.append((starts[original], end, b""))
    if tests:
        last = tests[-1]
        edits.append((starts[last], ends[last], b"".join(blocks)))
    return edits


def _scope_edits(  # noqa: PLR0913
    body: list[ast.stmt],
    lines: list[bytes],
    comments: dict[int, int],
    decorators: list[tuple[int, int]],
    limit: int,
    *,
    lower: int = 0,
) -> list[tuple[int, int, bytes]]:
    starts = {}
    for node in body:
        starts[node] = _statement_start(node, lower, comments, decorators)
        lower = node.end_lineno or node.lineno
    limits = {
        node: starts[body[index + 1]] if index + 1 < len(body) else limit
        for index, node in enumerate(body)
    }
    ends = {node: _statement_end(node, limits[node], lines, comments) for node in body}
    edits = []
    tests: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
            node.name.startswith("test_")
        ):
            tests.append(node)
        elif isinstance(node, ast.ClassDef):
            edits.extend(
                _scope_edits(node.body, lines, comments, decorators, limits[node]),
            )
    edits.extend(_test_edits(tests, starts, ends, limits, lines))
    return edits


def format_source(source: bytes, filename: str = "<source>") -> bytes:
    """Group sorted tests after intervening support code, preserving its order."""
    module = ast.parse(source, filename=filename)
    lines = source.splitlines(keepends=True)
    comments = {}
    decorators = []
    normalized = source.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    for token in tokenize.tokenize(io.BytesIO(normalized).readline):
        row, column = token.start
        if token.type == tokenize.COMMENT and not token.line[:column].strip():
            comments[row - 1] = column
        elif token.type == tokenize.OP and token.string == "@":
            decorators.append((row - 1, column))
    header_end = 0
    while header_end < len(lines) and (
        not lines[header_end].strip() or header_end in comments
    ):
        header_end += 1
    edits = _scope_edits(
        module.body,
        lines,
        comments,
        decorators,
        len(lines),
        lower=header_end,
    )
    for start, end, replacement in sorted(edits, reverse=True):
        lines[start:end] = [replacement]
    formatted = b"".join(lines)
    ast.parse(formatted, filename=filename)
    return formatted


def format_file(path: Path) -> bool:
    """Rewrite one regular file only after the sorted source passes parsing."""
    try:
        if path.is_symlink() or not path.is_file():
            message = "must be a regular file"
            raise OSError(message)  # noqa: TRY301
        source = path.read_bytes()
        formatted = format_source(source, str(path))
        if formatted != source:
            path.write_bytes(formatted)
    except (OSError, SyntaxError, tokenize.TokenError) as error:
        print(f"error: {path}: {error}", file=sys.stderr)  # noqa: T201
        return False
    return True


def main() -> None:
    """Alphabetize tests in selected Python files in place."""
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=(
            "Example: python_test_alphabetize packages/*/test_main.py. "
            "All tests are grouped and sorted by name within each module or class, "
            "after the support code that separates them. Other statements keep "
            "their relative order, and following code stays after the tests. "
            "Module headers stay in place; decorators and attached comments move "
            "with their tests. Source is parsed, never executed."
        ),
    )
    parser.add_argument(
        "files",
        nargs="+",
        metavar="FILE",
        help="Python test files to rewrite in place",
    )
    success = True
    for argument in parser.parse_args().files:
        success = format_file(Path(argument)) and success
    if not success:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

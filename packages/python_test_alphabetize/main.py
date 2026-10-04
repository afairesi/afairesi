#!/usr/bin/env python3
# Copyright (c) 2026- Paschalis Bizopoulos
"""Alphabetize Python tests within modules and classes without executing source."""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path
from typing import TYPE_CHECKING, cast

import libcst as cst

if TYPE_CHECKING:
    from collections.abc import Sequence


def _sort_tests(body: Sequence[cst.BaseStatement]) -> tuple[cst.BaseStatement, ...]:
    """Move complete CST definitions, keeping blank lines at their original slots."""
    if not body:
        return ()
    statements = []
    prefixes = []
    for node in body:
        statement = cast("cst.SimpleStatementLine | cst.BaseCompoundStatement", node)
        if isinstance(statement, cst.ClassDef) and isinstance(
            statement.body,
            cst.IndentedBlock,
        ):
            statement = statement.with_changes(
                body=statement.body.with_changes(body=_sort_tests(statement.body.body)),
            )
        leading = statement.leading_lines
        split = len(leading)
        while split and leading[split - 1].comment is not None:
            split -= 1
        prefixes.append(leading[:split])
        statements.append(statement.with_changes(leading_lines=leading[split:]))
    tests = [
        index
        for index, statement in enumerate(statements)
        if isinstance(statement, cst.FunctionDef)
        and statement.name.value.startswith("test_")
    ]
    ordered = sorted(
        (cast("cst.FunctionDef", statements[index]) for index in tests),
        key=lambda statement: statement.name.value,
    )
    gaps = [*prefixes[1:], ()]
    rows: list[
        tuple[
            cst.SimpleStatementLine | cst.BaseCompoundStatement,
            Sequence[cst.EmptyLine],
        ]
    ] = []
    for index, statement in enumerate(statements):
        if tests and index == tests[-1]:
            rows.extend(zip(ordered, (gaps[index] for index in tests), strict=True))
        elif index not in tests:
            rows.append((statement, gaps[index]))
    result = []
    leading = prefixes[0]
    for statement, gap in rows:
        result.append(
            statement.with_changes(
                leading_lines=(*leading, *statement.leading_lines),
            ),
        )
        leading = gap
    return tuple(result)


def format_source(source: bytes, filename: str = "<source>") -> bytes:
    """Group sorted tests after intervening support code, preserving its order."""
    ast.parse(source, filename=filename)
    module = cst.parse_module(source)
    formatted = module.with_changes(
        body=_sort_tests(module.body),
        has_trailing_newline=source.endswith((b"\n", b"\r")),
    ).bytes
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
    except (OSError, SyntaxError, cst.ParserSyntaxError) as error:
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

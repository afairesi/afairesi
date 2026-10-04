# Copyright (c) 2026- Paschalis Bizopoulos
"""Check Nix formatting through files and the installed executable."""

from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            '{ z = [ 3 1 2 ]; strings = [ "c" "a" ]; f = { z, a }: z + a; }',
            '{ f = { a, z }: z + a; strings = [ "c" "a" ]; z = [ 1 2 3 ]; }',
        ),
        ("{ b.z = 1; b.x = 2; a = 1; }", "{ a = 1; b = { x = 2; z = 1; }; }"),
        ("{ passthru = { inherit python; }; }", "{ passthru.python = python; }"),
        ("{ a = 1; a.b = 2; }", "{ a = 1; a.b = 2; }"),
        ("{ a = rec { inherit python; }; }", "{ a = rec { inherit python; }; }"),
        (
            "{ a = { inherit (python) version; }; }",
            "{ a = { inherit (python) version; }; }",
        ),
        ('{ "é" = "keep  spaces"; a = 0; }', '{ a = 0; "é" = "keep  spaces"; }'),
        (
            '{ "line\\nnext\\t\\r" = 1; a = 0; }',
            '{ a = 0; "line\\nnext\\t\\r" = 1; }',
        ),
        (
            r'{ "literal \${name}" = 1; "back\\slash\"quote" = 2; }',
            r'{ "back\\slash\"quote" = 2; "literal \${name}" = 1; }',
        ),
    ],
)
def test_cli_formats_and_converges(tmp_path: Path, source: str, expected: str) -> None:
    """Canonicalize representative expressions without losing significant text."""
    path = tmp_path / "example with spaces.nix"
    path.write_text(source, encoding="utf-8")
    for _ in range(2):
        result = subprocess.run(  # noqa: S603
            [os.environ["PACKAGE_E2E_EXECUTABLE"], str(path)],
            capture_output=True,
            check=False,
            timeout=10,
        )
        if result.returncode or result.stdout or result.stderr:
            raise AssertionError(result)
        if path.read_text(encoding="utf-8") != expected:
            raise AssertionError(path.read_text(encoding="utf-8"))


def test_cli_rejects_invalid_nix_without_overwriting_it(tmp_path: Path) -> None:
    """A syntax error leaves the user's input intact and returns failure."""
    path = tmp_path / "broken.nix"
    source = "{ invalid = ; }"
    path.write_text(source, encoding="utf-8")
    result = subprocess.run(  # noqa: S603
        [os.environ["PACKAGE_E2E_EXECUTABLE"], str(path)],
        capture_output=True,
        check=False,
        timeout=10,
    )
    if not result.returncode or not result.stderr or path.read_text() != source:
        raise AssertionError(result)

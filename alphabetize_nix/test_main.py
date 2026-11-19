# Copyright (c) 2026- Paschalis Bizopoulos
"""Check Nix formatting through files and the installed executable."""

from __future__ import annotations

import json
import os
import subprocess
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import alphabetize_nix as alphabetize

if TYPE_CHECKING:
    from pathlib import Path


def test_formatting_nested_bindings_requires_work_proportional_to_source_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bound tree visits so small nested inputs cannot trigger exponential work."""
    depth = 10
    source = "{ a = " * depth + "1" + "; }" * depth
    renderer = Mock(wraps=alphabetize.render_node)
    monkeypatch.setattr(alphabetize, "render_node", renderer)
    formatted = alphabetize.format_text(source)
    if renderer.call_count > len(source) * 4:
        raise AssertionError(renderer.call_count)
    if formatted != "{ " + ".".join(["a"] * depth) + " = 1; }":
        raise AssertionError(formatted)


def test_formatting_reports_an_invalid_file_and_still_formats_the_next_file(
    tmp_path: Path,
) -> None:
    """Preserve invalid input while reporting it and formatting subsequent files."""
    invalid = tmp_path / "invalid.nix"
    source = "{ broken = ; }"
    invalid.write_text(source)
    valid = tmp_path / "valid.nix"
    valid.write_text("{ z = 1; a = 2; }")
    result = subprocess.run(  # noqa: S603 - Test harness selects the executable and fixture arguments.
        [os.environ["PACKAGE_E2E_EXECUTABLE"], str(invalid), str(valid)],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if result.returncode != 1 or str(invalid) not in result.stderr:
        raise AssertionError(result)
    if invalid.read_text() != source:
        raise AssertionError(invalid.read_text())
    if valid.read_text() != "{ a = 2; z = 1; }":
        raise AssertionError(valid.read_text())


@settings(deadline=None, max_examples=10)
@given(
    st.dictionaries(
        st.text(alphabet="abcxyz_", min_size=1, max_size=8),
        st.integers(min_value=-1000, max_value=1000),
        max_size=12,
    ),
)
def test_formatting_integer_bindings_preserves_their_values_and_stabilizes_in_one_pass(
    bindings: dict[str, int],
) -> None:
    """Generated bindings retain their Nix value and stabilize after one pass."""
    source = (
        "{ " + " ".join(f'"{key}" = {value};' for key, value in bindings.items()) + " }"
    )
    formatted = alphabetize.format_text(source)
    if alphabetize.format_text(formatted) != formatted:
        raise AssertionError(formatted)
    result = _evaluate(formatted)
    if result.returncode or json.loads(result.stdout) != bindings:
        raise AssertionError((source, formatted, result))


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            '{ z = [ 3 1 2 ]; strings = [ "c" "a" ]; f = { z, a }: z + a; }',
            '{ f = { a, z }: z + a; strings = [ "c" "a" ]; z = [ 3 1 2 ]; }',
        ),
        ("{ b.z = 1; b.x = 2; a = 1; }", "{ a = 1; b = { x = 2; z = 1; }; }"),
        ("{ passthru = { inherit python; }; }", "{ passthru.python = python; }"),
        (
            "{ passthru = { inherit z a; value = 1; }; }",
            "{ passthru = { inherit a z; value = 1; }; }",
        ),
        (
            "{ a.z = z; a.x = x; }",
            "{ a = { inherit x z; }; }",
        ),
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
def test_formatting_sorts_nix_bindings_and_reaches_a_stable_form_in_one_pass(
    tmp_path: Path, source: str, expected: str
) -> None:
    """Canonicalize representative expressions without losing significant text."""
    path = tmp_path / "example with spaces.nix"
    path.write_text(source, encoding="utf-8")
    for _ in range(2):
        result = subprocess.run(  # noqa: S603 - Test harness selects the executable and fixture arguments.
            [os.environ["PACKAGE_E2E_EXECUTABLE"], str(path)],
            capture_output=True,
            check=False,
            timeout=10,
        )
        if result.returncode or result.stdout or result.stderr:
            raise AssertionError(result)
        if path.read_text(encoding="utf-8") != expected:
            raise AssertionError(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "source",
    [
        "[ 3 1 2 1 ]",
        "let z = 3; a = 1; in [ z a z ]",
        'let key = "dynamic"; in { ${key} = {}; "" = 2; z = 1; }',
        "builtins.concatLists [ [ 3 1 ] [ 2 ] ]",
        "[ { z = 3; a = 1; } { b = 2; a = 0; } ]",
        "({ z ? 3, a ? z, ... }: [ z a ]) {}",
        "let python = 7; in { passthru = { inherit python; }; }",
        "let x = 7; z = 8; in { a.z = z; a.x = x; }",
        "let x = 7; in rec { x = 9; a = { inherit x; y = 1; }; }",
        "let x = 7; in { a = rec { inherit x; y = x; }; }",
        "rec { z = 3; a = { x = z; y = 1; }; }",
        "{ b.z = 1; b.x = 2; a = 1; }",
        "# leading comment\n{ z = 3; a = 1; }\n# trailing comment\n",
        "{ z = 3; # keep this comment\n a = 1; }",
        "({ z, # keep this formal\n a }: [ z a ]) { z = 3; a = 1; }",
        "[ 3 # keep this element\n 1 2 ]",
        "{ z /* key comment */ = 3; a = 1; }",
        "{ z = { # nested comment\n y = 2; x = 1; }; a = 0; }",
    ],
)
def test_formatting_preserves_nix_values_list_order_and_comments(source: str) -> None:
    """Rewriting preserves the value that Nix evaluates, including list order."""
    rendered = alphabetize.format_text(source)
    expected = _evaluate(source)
    actual = _evaluate(rendered)
    if expected.returncode or actual.returncode or actual.stdout != expected.stdout:
        raise AssertionError((expected, actual, rendered))
    if alphabetize.format_text(rendered) != rendered:
        raise AssertionError(rendered)
    for line in source.splitlines():
        if "#" in line and line.split("#", 1)[1] not in rendered:
            raise AssertionError(rendered)
    if "/* key comment */" in source and "/* key comment */" not in rendered:
        raise AssertionError(rendered)


def _evaluate(source: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [  # noqa: S607
            "nix",
            "--extra-experimental-features",
            "nix-command",
            "eval",
            "--store",
            "dummy://",
            "--json",
            "--file",
            "-",
        ],
        input=source,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )


@pytest.mark.parametrize(
    ("source", "diagnostic"),
    [
        ("{ z = 1; a.x = 2; a.x = 3; }", "attribute 'a.x' already defined"),
        ("{ z = 1; a = missing; }", "undefined variable 'missing'"),
        ("({ z, a }: z + a) { z = 1; }", "without required argument 'a'"),
    ],
)
def test_formatting_preserves_nix_evaluation_errors(
    source: str,
    diagnostic: str,
) -> None:
    """Nix continues to reject semantic errors after canonicalization."""
    expected = _evaluate(source)
    actual = _evaluate(alphabetize.format_text(source))
    if (
        not expected.returncode
        or actual.returncode != expected.returncode
        or diagnostic not in expected.stderr
        or diagnostic not in actual.stderr
    ):
        raise AssertionError((expected, actual))

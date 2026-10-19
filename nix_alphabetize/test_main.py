# Copyright (c) 2026- Paschalis Bizopoulos
"""Check Nix formatting through files and the installed executable."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from nix_alphabetize import main as alphabetize


@settings(deadline=None)
@given(
    st.dictionaries(
        st.text(alphabet="abcxyz_", min_size=1, max_size=8),
        st.integers(min_value=-1000, max_value=1000),
        max_size=12,
    ),
)
def test_fuzz_preserves_values_and_converges(bindings: dict[str, int]) -> None:
    """Generated bindings retain their Nix value and stabilize after one pass."""
    source = (
        "{ " + " ".join(f'"{key}" = {value};' for key, value in bindings.items()) + " }"
    )
    formatted = alphabetize.format_text(source)
    if alphabetize.format_text(formatted) != formatted:
        raise AssertionError(formatted)
    with TemporaryDirectory(prefix="alphabetize-fuzz-") as directory:
        root = Path(directory)
        original = root / "original.nix"
        rewritten = root / "rewritten.nix"
        original.write_text(source)
        rewritten.write_text(formatted)
        environment = dict(os.environ)
        environment["NIX_CONFIG"] = "build-users-group =\n"
        environment["NIX_REMOTE"] = f"local?root={root / 'nix'}"
        before = _evaluate(original, environment)
        after = _evaluate(rewritten, environment)
        if before.returncode or after.returncode or before.stdout != after.stdout:
            raise AssertionError((source, formatted, before, after))


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            '{ z = [ 3 1 2 ]; strings = [ "c" "a" ]; f = { z, a }: z + a; }',
            '{ f = { a, z }: z + a; strings = [ "c" "a" ]; z = [ 3 1 2 ]; }',
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


@pytest.mark.parametrize(
    "source",
    [
        "[ 3 1 2 1 ]",
        "let z = 3; a = 1; in [ z a z ]",
        "builtins.head [ false true ]",
        "builtins.concatLists [ [ 3 1 ] [ 2 ] ]",
        "[ { z = 3; a = 1; } { b = 2; a = 0; } ]",
        "({ z ? 3, a ? z, ... }: [ z a ]) {}",
        "let python = 7; in { passthru = { inherit python; }; }",
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
def test_cli_preserves_native_nix_value(tmp_path: Path, source: str) -> None:
    """Rewriting preserves the value that Nix evaluates, including list order."""
    original = tmp_path / "original.nix"
    formatted = tmp_path / "formatted.nix"
    original.write_text(source, encoding="utf-8")
    formatted.write_text(source, encoding="utf-8")
    environment = dict(os.environ)
    environment["NIX_CONFIG"] = "build-users-group =\n"
    environment["NIX_REMOTE"] = (
        f"local?store={tmp_path / 'store'}&state={tmp_path / 'state'}"
    )
    expected = _evaluate(original, environment)
    for _ in range(2):
        subprocess.run(  # noqa: S603
            [os.environ["PACKAGE_E2E_EXECUTABLE"], str(formatted)],
            capture_output=True,
            check=True,
            timeout=10,
        )
        actual = _evaluate(formatted, environment)
        if expected.returncode or actual.returncode or actual.stdout != expected.stdout:
            raise AssertionError((expected, actual, formatted.read_text()))
        for line in source.splitlines():
            if "#" in line and line.split("#", 1)[1] not in formatted.read_text():
                raise AssertionError(formatted.read_text())
        rendered = formatted.read_text()
        if "/* key comment */" in source and "/* key comment */" not in rendered:
            raise AssertionError(rendered)


def _evaluate(
    path: Path,
    environment: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["nix-instantiate", "--eval", "--strict", "--json", str(path)],  # noqa: S607
        env=environment,
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
def test_cli_preserves_native_nix_errors(
    tmp_path: Path,
    source: str,
    diagnostic: str,
) -> None:
    """Nix continues to reject semantic errors after canonicalization."""
    path = tmp_path / "error.nix"
    path.write_text(source, encoding="utf-8")
    environment = dict(os.environ)
    environment["NIX_CONFIG"] = "build-users-group =\n"
    environment["NIX_REMOTE"] = (
        f"local?store={tmp_path / 'store'}&state={tmp_path / 'state'}"
    )
    expected = _evaluate(path, environment)
    subprocess.run(  # noqa: S603
        [os.environ["PACKAGE_E2E_EXECUTABLE"], str(path)],
        capture_output=True,
        check=True,
        timeout=10,
    )
    actual = _evaluate(path, environment)
    if (
        not expected.returncode
        or actual.returncode != expected.returncode
        or diagnostic not in expected.stderr
        or diagnostic not in actual.stderr
    ):
        raise AssertionError((expected, actual))

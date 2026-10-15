# Copyright (c) 2026- Paschalis Bizopoulos
"""Validate complete Nix documents through the packaged command."""

from __future__ import annotations

import json
import os
import subprocess
from typing import TYPE_CHECKING

import pytest

from packages.nix_syntax import main as nix_syntax

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _isolated_nix_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run native commands against a private store without host configuration."""
    monkeypatch.setenv("NIX_REMOTE", f"local?root={tmp_path / 'store'}")
    monkeypatch.setenv("NIX_CONFIG", "build-users-group =\n")


def test_cli_validates_files_and_reports_each_failure(tmp_path: Path) -> None:
    """Accept valid Unicode Nix, report malformed and missing files, and never edit."""
    valid = tmp_path / "valid.nix"
    valid.write_text(
        '{ "é"."a b" = "${toString 1}"; f = { x, ... }: x; }',
        encoding="utf-8",
    )
    invalid = tmp_path / "invalid.nix"
    invalid.write_text("{ invalid = ; }", encoding="utf-8")
    missing = tmp_path / "missing.nix"
    originals = {path: path.read_bytes() for path in (valid, invalid)}
    for paths, code in [([valid], 0), ([invalid, valid, missing], 1)]:
        result = subprocess.run(  # noqa: S603
            [os.environ["PACKAGE_E2E_EXECUTABLE"], *map(str, paths)],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        if result.returncode != code or result.stdout:
            raise AssertionError(result)
        if code and not all(str(path) in result.stderr for path in (invalid, missing)):
            raise AssertionError(result.stderr)
        if not code and result.stderr:
            raise AssertionError(result.stderr)
    if any(path.read_bytes() != source for path, source in originals.items()):
        message = "validation modified an input"
        raise AssertionError(message)


@pytest.mark.parametrize(
    "source",
    [
        b"{ z = 1; a = 2; }",
        b"{ duplicate = 1; duplicate = 2; }",
        b"{ a = 1; a.b = 2; }",
        b"{ missing = undefined; }",
        b"({ a, a }: a)",
        b"{ broken = ; }",
        b"# only a comment\n",
        b"let x = 3; in rec { a = x; b = a; }",
        b'"a\r\nb"',
        b'"\xff"',
    ],
)
def test_cli_matches_native_parser(tmp_path: Path, source: bytes) -> None:
    """Native Nix determines acceptance, error text, and exit status."""
    path = tmp_path / "expression with spaces.nix"
    path.write_bytes(source)
    native = subprocess.run(  # noqa: S603
        ["nix-instantiate", "--parse", "--", str(path)],  # noqa: S607
        capture_output=True,
        check=False,
        timeout=10,
    )
    actual = subprocess.run(  # noqa: S603
        [os.environ["PACKAGE_E2E_EXECUTABLE"], str(path)],
        capture_output=True,
        check=False,
        timeout=10,
    )
    if (actual.returncode, actual.stderr, actual.stdout) != (
        native.returncode,
        native.stderr,
        b"",
    ) or path.read_bytes() != source:
        raise AssertionError((native, actual))


@pytest.mark.parametrize("kind", ["missing", "directory", "symlink"])
def test_cli_uses_native_file_resolution(tmp_path: Path, kind: str) -> None:
    """Delegate missing paths, directory defaults, and links to Nix."""
    path = tmp_path / kind
    if kind == "directory":
        path.mkdir()
        (path / "default.nix").write_text("{}")
    elif kind == "symlink":
        target = tmp_path / "target.nix"
        target.write_text("{}")
        path.symlink_to(target)
    native = subprocess.run(  # noqa: S603
        ["nix-instantiate", "--parse", "--", str(path)],  # noqa: S607
        capture_output=True,
        check=False,
        timeout=10,
    )
    actual = subprocess.run(  # noqa: S603
        [os.environ["PACKAGE_E2E_EXECUTABLE"], str(path)],
        capture_output=True,
        check=False,
        timeout=10,
    )
    if (actual.returncode, actual.stderr, actual.stdout) != (
        native.returncode,
        native.stderr,
        b"",
    ):
        raise AssertionError((native, actual))


@pytest.mark.parametrize(
    "source",
    [
        '"a\r\nb"',
        '"a\rb"',
        '"a\\\r\nb"',
        r'"\q"',
        r'"\${name}"',
        r'"back\\slash\"quote\n\t\r"',
        '"é"',
    ],
)
def test_string_value_matches_native_nix(source: str) -> None:
    """Decode literals with Nix's own escape and line-ending rules."""
    native = subprocess.run(  # noqa: S603
        ["nix-instantiate", "--eval", "--strict", "--json", "--expr", source],  # noqa: S607
        capture_output=True,
        check=True,
        timeout=10,
    )
    document = nix_syntax.parse(source)
    if nix_syntax.string_value(document, document.root) != json.loads(native.stdout):
        raise AssertionError(source)


@pytest.mark.parametrize("source", ['"${undefined}"', "42", "''indented''"])
def test_string_value_leaves_expressions_unevaluated(source: str) -> None:
    """Source inspection only evaluates quoted literals without interpolation."""
    document = nix_syntax.parse(source)
    if nix_syntax.string_value(document, document.root) is not None:
        raise AssertionError(source)


def test_string_value_does_not_initialize_home_or_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inspecting a literal leaves HOME and the configured store untouched."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    document = nix_syntax.parse('"metadata"')
    if nix_syntax.string_value(document, document.root) != "metadata":
        msg = "literal value changed"
        raise AssertionError(msg)
    if list(home.rglob("*")) or (tmp_path / "store").exists():
        msg = "literal inspection created Nix state"
        raise AssertionError(msg)

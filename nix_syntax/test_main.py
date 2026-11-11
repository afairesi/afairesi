# Copyright (c) 2026- Paschalis Bizopoulos
"""Check Nix parsing and literal decoding through the shared library."""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

from nix_syntax import main as nix_syntax

if TYPE_CHECKING:
    from pathlib import Path


@given(
    st.one_of(
        st.binary(max_size=4096),
        st.sampled_from([b"{}", b"[ 1 2 ]", b"let a = 1; in a", b'"${toString 1}"']),
    ),
)
def test_fuzz_parsing_preserves_valid_source_or_reports_a_syntax_error(
    source: bytes,
) -> None:
    """Arbitrary bytes produce a document or the documented syntax error."""
    try:
        document = nix_syntax.parse(source)
    except nix_syntax.NixSyntaxError:
        return
    if document.source != source or document.tree.root_node.has_error:
        raise AssertionError(document)


@pytest.fixture(autouse=True)
def _isolated_nix_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run native commands against a private store without host configuration."""
    monkeypatch.setenv("NIX_REMOTE", f"local?root={tmp_path / 'store'}")
    monkeypatch.setenv("NIX_CONFIG", "build-users-group =\n")


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
def test_string_literals_follow_native_nix_decoding_rules(source: str) -> None:
    """Decode literals with Nix's own escape and line-ending rules."""
    native = subprocess.run(  # noqa: S603 - Fixed Nix executable with fixture paths or expressions.
        ["nix-instantiate", "--eval", "--strict", "--json", "--expr", source],  # noqa: S607
        capture_output=True,
        check=True,
        timeout=10,
    )
    document = nix_syntax.parse(source)
    if nix_syntax.string_value(document, document.root) != json.loads(native.stdout):
        raise AssertionError(source)


@pytest.mark.parametrize("source", ['"${undefined}"', "42", "''indented''"])
def test_literal_inspection_leaves_expressions_unevaluated(source: str) -> None:
    """Source inspection only evaluates quoted literals without interpolation."""
    document = nix_syntax.parse(source)
    if nix_syntax.string_value(document, document.root) is not None:
        raise AssertionError(source)


def test_literal_inspection_preserves_home_and_nix_store(
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

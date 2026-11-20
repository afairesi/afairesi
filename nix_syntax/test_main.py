# Copyright (c) 2026- Paschalis Bizopoulos
"""Check Nix parsing and literal decoding through the shared library."""

from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

import nix_syntax

if TYPE_CHECKING:
    from pathlib import Path


@given(
    st.one_of(
        st.binary(max_size=4096),
        st.sampled_from([b"{}", b"[ 1 2 ]", b"let a = 1; in a", b'"${toString 1}"']),
    ),
)
def test_parsing_returns_the_original_bytes_or_raises_a_nix_syntax_error(
    source: bytes,
) -> None:
    """Arbitrary bytes produce a document or the documented syntax error."""
    try:
        document = nix_syntax.parse(source)
    except nix_syntax.NixSyntaxError:
        return
    if document.source != source or document.tree.root_node.has_error:
        raise AssertionError(document)


def test_string_value_decodes_only_the_selected_literal() -> None:
    """Read the literal at its UTF-8 byte offset and preserve its trailing newline."""
    document = nix_syntax.parse('["préfixe" "café\\r\\n\\${name}\r\n" undefined]')
    literal = document.root.named_children[1]
    value = nix_syntax.string_value(document, literal)
    if value != "café\r\n${name}\n":
        raise AssertionError(value)


@pytest.mark.parametrize("source", ['"${undefined}"', "42", "''indented''"])
def test_literal_inspection_reads_only_double_quoted_strings_without_interpolation(
    source: str,
) -> None:
    """Source inspection only evaluates quoted literals without interpolation."""
    document = nix_syntax.parse(source)
    if nix_syntax.string_value(document, document.root) is not None:
        raise AssertionError(source)


def test_reading_long_string_literals_leaves_the_home_directory_and_nix_store_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read a literal larger than a command argument and leave Nix state untouched."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("NIX_REMOTE", f"local?root={tmp_path / 'store'}")
    monkeypatch.setenv("NIX_CONFIG", "build-users-group =\n")
    value = "metadata" * 32768
    document = nix_syntax.parse(nix_syntax.quote_string(value))
    if nix_syntax.string_value(document, document.root) != value:
        msg = "literal value changed"
        raise AssertionError(msg)
    if list(home.rglob("*")) or (tmp_path / "store").exists():
        msg = "literal inspection created Nix state"
        raise AssertionError(msg)

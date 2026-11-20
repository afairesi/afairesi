# Copyright (c) 2026- Paschalis Bizopoulos
"""Check Nix parsing and literal decoding through the shared library."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

import nix_syntax


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


@pytest.mark.parametrize(
    ("literal", "expected"),
    [
        (r'"café\r\n\${name}"', "café\r\n${name}"),
        ('"a\rb\r\nc\\\r\n"', "a\nb\nc\r\n"),
        (r'"\q\\\t\"$$"', 'q\\\t"$$'),
        ('""', ""),
    ],
)
def test_string_value_decodes_only_the_selected_literal(
    literal: str, expected: str
) -> None:
    """Read the literal at its UTF-8 byte offset using Nix escape and newline rules."""
    document = nix_syntax.parse(f'["préfixe" {literal} undefined]')
    value = nix_syntax.string_value(document, document.root.named_children[1])
    if value != expected:
        raise AssertionError(value)


@pytest.mark.parametrize("source", ['"${undefined}"', "42", "''indented''"])
def test_literal_inspection_reads_only_double_quoted_strings_without_interpolation(
    source: str,
) -> None:
    """Leave interpolations and other expression types unresolved."""
    document = nix_syntax.parse(source)
    if nix_syntax.string_value(document, document.root) is not None:
        raise AssertionError(source)

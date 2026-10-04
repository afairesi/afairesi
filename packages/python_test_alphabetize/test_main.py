# Copyright (c) 2026- Paschalis Bizopoulos
"""Check alphabetical test ordering through the installed executable."""

from __future__ import annotations

import os
import stat
import subprocess
from typing import TYPE_CHECKING

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from packages.python_test_alphabetize.main import format_source

if TYPE_CHECKING:
    from pathlib import Path
FILE_MODE = 0o640
HEADER = b'# Copyright (c) 2026\n"""Test module."""\n\n'
ZEBRA = (
    b"# Zebra documentation\n"
    b"@(\n    marker\n)\n"
    b"async def test_zebra():\n"
    b'    value = """\ndef test_hidden():\n    unchanged\n"""\n'
    b"    return value\n"
    b"    # Zebra body comment\n"
)
ALPHA = (
    b"# Alpha documentation\n"
    b'@marker(\n    "alpha",\n)\n'
    b"def test_alpha():\n"
    b'    return "alpha"\n'
    b"    # Alpha body comment\n"
)
CLASS_HEADER = (
    b"import unittest as unit\n"
    b"class Reports(unit.TestCase):\n"
    b"    def setUp(self): pass\n\n"
)
CLASS_ZEBRA = b"    @marker\n    def test_zebra(self): return 'zebra'\n"
CLASS_ALPHA = b"    async def test_alpha(self): return 'alpha'\n"
BOUNDARY = (
    b"LIMIT = 1\n"
    b"def helper():\n"
    b"    def test_z_local(): pass\n"
    b"    def test_a_local(): pass\n"
    b"class TestBoundary:\n"
    b"    def test_z_before_helper(self): pass\n"
    b"    def helper(self): pass\n"
    b"    def test_a_after_helper(self): pass\n"
)
SORTED_BOUNDARY = (
    b"LIMIT = 1\n"
    b"def helper():\n"
    b"    def test_z_local(): pass\n"
    b"    def test_a_local(): pass\n"
    b"class TestBoundary:\n"
    b"    def test_a_after_helper(self): pass\n"
    b"    def helper(self): pass\n"
    b"    def test_z_before_helper(self): pass\n"
)


def _run(*paths: Path) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(  # noqa: S603
        [os.environ["PACKAGE_E2E_EXECUTABLE"], *map(str, paths)],
        capture_output=True,
        check=False,
        timeout=10,
    )


def test_cli_continues_after_errors_and_preserves_invalid_or_linked_files(
    tmp_path: Path,
) -> None:
    """Report invalid inputs without changing them and still sort valid inputs."""
    invalid = tmp_path / "broken.py"
    invalid.write_bytes(b"def invalid(\n")
    binary = tmp_path / "binary.py"
    binary.write_bytes(b"\0def test_z(): pass\n")
    target = tmp_path / "unselected.py"
    source = b"def test_z(): pass\ndef test_a(): pass\n"
    target.write_bytes(source)
    link = tmp_path / "linked.py"
    link.symlink_to(target)
    selected = tmp_path / "selected.py"
    selected.write_bytes(source)
    result = _run(invalid, binary, link, tmp_path / "missing.py", tmp_path, selected)
    if (
        result.returncode != 1
        or result.stdout
        or not all(
            str(path).encode() in result.stderr for path in (invalid, binary, link)
        )
        or invalid.read_bytes() != b"def invalid(\n"
        or binary.read_bytes() != b"\0def test_z(): pass\n"
        or target.read_bytes() != source
        or not link.is_symlink()
        or selected.read_bytes() != b"def test_a(): pass\ndef test_z(): pass\n"
    ):
        raise AssertionError(result)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            (
                b'def test_package_remove(): return "remove"\n\n'
                b'def test_converge(): return "converge"\n\n'
                b'def test_package_add(): return "add"\n'
            ),
            (
                b'def test_converge(): return "converge"\n\n'
                b'def test_package_add(): return "add"\n\n'
                b'def test_package_remove(): return "remove"\n'
            ),
        ),
        (HEADER + ZEBRA + b"\n" + ALPHA, HEADER + ALPHA + b"\n" + ZEBRA),
        (
            CLASS_HEADER + CLASS_ZEBRA + b"\n" + CLASS_ALPHA,
            CLASS_HEADER + CLASS_ALPHA + b"\n" + CLASS_ZEBRA,
        ),
        (
            b"def test_z(): pass\ndef test_a(): pass\n"
            + BOUNDARY
            + b"def test_z_after(): pass\ndef test_a_after(): pass\n",
            b"def test_a(): pass\ndef test_a_after(): pass\n"
            + SORTED_BOUNDARY
            + b"def test_z(): pass\ndef test_z_after(): pass\n",
        ),
        (
            (
                b"# Module header\ndef test_z(): pass\n"
                b"# Alpha documentation\ndef test_a(): pass\n"
            ),
            (
                b"# Module header\n# Alpha documentation\ndef test_a(): pass\n"
                b"def test_z(): pass\n"
            ),
        ),
        (
            b"# Module header\r\ndef test_z(): pass\r\n\r\ndef test_a(): pass",
            b"# Module header\r\ndef test_a(): pass\r\n\r\ndef test_z(): pass",
        ),
        (
            b"@marker\rdef test_z(): pass\rdef test_a(): pass\r",
            b"def test_a(): pass\r@marker\rdef test_z(): pass\r",
        ),
        (
            (
                b"class TestTabs:\n\t@marker\n\tdef test_z(self): pass\n"
                b"\tdef test_a(self): pass\n"
            ),
            (
                b"class TestTabs:\n\tdef test_a(self): pass\n"
                b"\t@marker\n\tdef test_z(self): pass\n"
            ),
        ),
        (
            (
                b'\xef\xbb\xbf"""Unicode module."""\n'
                b"def test_z(): return '\xce\x94'\ndef test_a(): pass\n"
            ),
            (
                b'\xef\xbb\xbf"""Unicode module."""\n'
                b"def test_a(): pass\ndef test_z(): return '\xce\x94'\n"
            ),
        ),
        (
            b"# coding: latin-1\ndef test_z(): return 'caf\xe9'\ndef test_a(): pass\n",
            b"# coding: latin-1\ndef test_a(): pass\ndef test_z(): return 'caf\xe9'\n",
        ),
        (
            (
                b'def test_z(): return "first"\ndef test_a(): pass\n'
                b'def test_z(): return "last"\n'
            ),
            (
                b'def test_a(): pass\ndef test_z(): return "first"\n'
                b'def test_z(): return "last"\n'
            ),
        ),
        (b"# No tests\nvalue = 1\n", b"# No tests\nvalue = 1\n"),
        (b"", b""),
    ],
)
def test_cli_sorts_tests_and_preserves_source_bytes_modes_and_fixed_points(
    tmp_path: Path,
    source: bytes,
    expected: bytes,
) -> None:
    """Move complete test definitions, keeping scopes, trivia, and other code intact."""
    path = tmp_path / "test module.py"
    path.write_bytes(source)
    path.chmod(FILE_MODE)
    result = _run(path)
    if (
        result.returncode
        or result.stdout
        or result.stderr
        or path.read_bytes() != expected
        or stat.S_IMODE(path.stat().st_mode) != FILE_MODE
    ):
        raise AssertionError((result, path.read_bytes(), expected))
    modified = path.stat().st_mtime_ns
    repeated = _run(path)
    if (
        repeated.returncode
        or repeated.stdout
        or repeated.stderr
        or path.read_bytes() != expected
        or path.stat().st_mtime_ns != modified
    ):
        raise AssertionError((repeated, path.read_bytes(), expected))


@settings(deadline=None)
@given(
    names=st.lists(
        st.text(alphabet="abcxyz_", min_size=1, max_size=12),
        max_size=12,
        unique=True,
    ),
    newline=st.sampled_from((b"\n", b"\r\n")),
    final_newline=st.booleans(),
    separated=st.booleans(),
    scope=st.sampled_from(("module", "class")),
)
@example(
    names=["zebra", "alpha", "middle"],
    newline=b"\n",
    final_newline=True,
    separated=True,
    scope="module",
)
@example(
    names=["z", "a"],
    newline=b"\r\n",
    final_newline=False,
    separated=True,
    scope="class",
)
@example(
    names=["z", "a"],
    newline=b"\n",
    final_newline=False,
    separated=False,
    scope="module",
)
@example(
    names=[],
    newline=b"\n",
    final_newline=False,
    separated=False,
    scope="class",
)
def test_sorting_preserves_generated_test_bodies_and_is_idempotent(
    names: list[str],
    newline: bytes,
    scope: str,
    *,
    final_newline: bool,
    separated: bool,
) -> None:
    """Sort every scope across helpers without losing a body or changing constants."""

    def render(order: list[str]) -> bytes:
        lines = [b'"""Generated tests."""']
        indent = b""
        if scope == "class":
            lines.append(b"class TestGenerated:")
            indent = b"    "
            if not order:
                lines.append(indent + b"pass")
        for index, name in enumerate(order):
            lines.append(indent + f"def test_{name}(): return {name!r}".encode())
            if separated:
                lines.extend(
                    [
                        indent + f"VALUE_{index} = {index}".encode(),
                        indent + f"def helper_{index}(): return {index}".encode(),
                    ],
                )
        contents = newline.join(lines)
        return contents + newline if final_newline else contents

    source = render(names)
    expected = render(sorted(names))
    formatted = format_source(source)
    if formatted != expected or format_source(formatted) != formatted:
        raise AssertionError((source, formatted, expected))

# Copyright (c) 2026- Paschalis Bizopoulos
"""End-to-end checks for explicit file cleanup."""

from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def _read_error(path: Path) -> str:
    """Capture the native error for an unreadable fixture."""
    try:
        path.read_bytes()
    except OSError as error:
        return str(error)
    message = f"expected an unreadable path: {path}"
    raise AssertionError(message)


def test_cli_cleans_selected_files_and_preserves_protected_inputs(
    tmp_path: Path,
) -> None:
    """Clean text, preserve binary and linked files, and converge on a second run."""
    selected = tmp_path / "selected file.txt"
    selected.write_bytes(b"first\r\n\xff\nlast")
    binary = tmp_path / "binary"
    binary.write_bytes(b"\0first\r\n\nlast")
    target = tmp_path / "unselected.txt"
    target.write_bytes(b"first\r\n\xff\nlast")
    link = tmp_path / "link"
    link.symlink_to(target)
    empty = tmp_path / "empty"
    empty.touch()
    for _ in range(2):
        completed = subprocess.run(  # noqa: S603
            [
                os.environ["PACKAGE_E2E_EXECUTABLE"],
                str(selected),
                str(binary),
                str(link),
                str(empty),
            ],
            capture_output=True,
            check=False,
            timeout=10,
        )
        if completed.returncode or completed.stdout or completed.stderr:
            raise AssertionError(completed)
        if selected.read_bytes() != b"first\xfflast":
            raise AssertionError(selected.read_bytes())
        if (
            binary.read_bytes() != b"\0first\r\n\nlast"
            or target.read_bytes() != b"first\r\n\xff\nlast"
            or not link.is_symlink()
            or empty.read_bytes()
        ):
            message = "cleanup changed a protected input"
            raise AssertionError(message)


def test_cli_reports_filesystem_errors_and_processes_other_files(
    tmp_path: Path,
) -> None:
    """Report each native filesystem error and still clean selected text files."""
    missing = tmp_path / "missing.txt"
    directory = tmp_path / "directory"
    directory.mkdir()
    selected = tmp_path / "selected.txt"
    selected.write_bytes(b"first\r\nlast\n")
    expected_errors = [_read_error(path) for path in (missing, directory)]
    result = subprocess.run(  # noqa: S603
        [
            os.environ["PACKAGE_E2E_EXECUTABLE"],
            str(missing),
            str(selected),
            str(directory),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if (
        result.returncode != 1
        or result.stdout
        or result.stderr.splitlines() != expected_errors
        or selected.read_bytes() != b"firstlast"
    ):
        raise AssertionError(result)

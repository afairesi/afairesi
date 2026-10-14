#!/usr/bin/env python3
# Copyright (c) 2026- Paschalis Bizopoulos
"""Remove empty lines from explicitly selected text files."""

from __future__ import annotations

import argparse
from pathlib import Path


def remove_empty_lines(contents: bytes) -> bytes:
    """Remove UTF-8 whitespace-only lines without changing other bytes."""
    lines = contents.splitlines(keepends=True)
    return b"".join(line for line in lines if not _is_empty_utf8_line(line))


def _is_empty_utf8_line(line: bytes) -> bool:
    """Identify empty UTF-8 lines while retaining invalid UTF-8 data."""
    try:
        return line.rstrip(b"\r\n").decode("utf-8").strip() == ""
    except UnicodeDecodeError:
        return False


def process_file(path: Path) -> None:
    """Rewrite one regular text file without following symbolic links."""
    if path.is_symlink() or not path.is_file():
        return
    contents = path.read_bytes()
    if b"\0" in contents:
        return
    updated_contents = remove_empty_lines(contents)
    if updated_contents != contents:
        path.write_bytes(updated_contents)


def main() -> None:
    """Remove blank and whitespace-only lines from selected text files in place."""
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog="Example: remove_empty_lines LICENSE flake.lock",
    )
    parser.add_argument(
        "files",
        nargs="+",
        metavar="FILE",
        help="text files to rewrite in place",
    )
    for path in map(Path, parser.parse_args().files):
        process_file(path)


if __name__ == "__main__":
    main()

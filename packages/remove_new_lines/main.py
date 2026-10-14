#!/usr/bin/env python3
# Copyright (c) 2026- Paschalis Bizopoulos
"""Remove new lines from explicitly selected text files."""

from __future__ import annotations

import argparse
from pathlib import Path


def remove_new_lines(contents: bytes) -> bytes:
    """Remove CR and LF bytes without changing other bytes."""
    return contents.replace(b"\n", b"").replace(b"\r", b"")


def process_file(path: Path) -> None:
    """Rewrite one regular text file without following symbolic links."""
    if path.is_symlink() or not path.is_file():
        return
    contents = path.read_bytes()
    if b"\0" in contents:
        return
    updated_contents = remove_new_lines(contents)
    if updated_contents != contents:
        path.write_bytes(updated_contents)


def main() -> None:
    """Remove CR and LF bytes from selected text files in place."""
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog="Example: remove_new_lines packages/site/index.html",
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

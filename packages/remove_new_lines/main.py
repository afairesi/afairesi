#!/usr/bin/env python3
# Copyright (c) 2026- Paschalis Bizopoulos
"""Remove new lines from explicitly selected text files."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def remove_new_lines(contents: bytes) -> bytes:
    """Remove CR and LF bytes without changing other bytes."""
    return contents.replace(b"\n", b"").replace(b"\r", b"")


def process_file(path: Path) -> None:
    """Rewrite one regular text file without following symbolic links."""
    if path.is_symlink():
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
    failed = False
    for path in map(Path, parser.parse_args().files):
        try:
            process_file(path)
        except OSError as error:  # noqa: PERF203
            print(error, file=sys.stderr)  # noqa: T201
            failed = True
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

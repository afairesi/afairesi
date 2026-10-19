#!/usr/bin/env python3
# Copyright (c) 2026- Paschalis Bizopoulos
"""Remove new lines from explicitly selected text files."""

from __future__ import annotations

import argparse
from pathlib import Path

from remove_empty_lines import process_files


def remove_new_lines(contents: bytes) -> bytes:
    """Remove CR and LF bytes without changing other bytes."""
    return contents.replace(b"\n", b"").replace(b"\r", b"")


def main() -> None:
    """Remove CR and LF bytes from selected text files in place."""
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog="Example: remove_new_lines site/index.html",
    )
    parser.add_argument(
        "files",
        nargs="+",
        metavar="FILE",
        help="text files to rewrite in place",
    )
    process_files(map(Path, parser.parse_args().files), remove_new_lines)


if __name__ == "__main__":
    main()

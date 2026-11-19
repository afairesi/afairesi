#!/usr/bin/env python3
# Copyright (c) 2026- Paschalis Bizopoulos
"""Expose the consumer's runtime environment and command arguments."""

import os
import sys


def main() -> None:
    """Report the runtime hook state and arguments."""
    hook = os.environ.get("AFAIRESI_CONSUMER_HOOK")
    if hook is None:
        hook = "unloaded"
    sys.stdout.write(hook + "\n")
    sys.stdout.write(repr(sys.argv[1:]) + "\n")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# Copyright (c) 2026- Paschalis Bizopoulos
"""Expose the consumer's runtime environment and command arguments."""

import os
import sys


def main() -> None:
    """Report the runtime hook state and arguments."""
    sys.stdout.write(os.environ.get("AFAIRESI_CONSUMER_HOOK", "unloaded") + "\n")
    sys.stdout.write(repr(sys.argv[1:]) + "\n")

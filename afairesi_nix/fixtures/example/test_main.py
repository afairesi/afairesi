# Copyright (c) 2026- Paschalis Bizopoulos
"""Check the installed consumer command without its application runtime hook."""

import os
import subprocess


def test_cli() -> None:
    """Keep runtime initialization separate from the installed executable."""
    result = subprocess.run(  # noqa: S603 - The test environment supplies the built executable.
        [os.environ["PACKAGE_E2E_EXECUTABLE"]],
        capture_output=True,
        text=True,
        check=True,
    )
    if result.stdout != "unloaded\n[]\n":
        message = "The installed executable changed its runtime hook or argument output"
        raise AssertionError(message)

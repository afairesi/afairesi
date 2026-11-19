# Copyright (c) 2026- Paschalis Bizopoulos
"""Check the installed consumer command without its application runtime hook."""

import os
import subprocess

import pytest


@pytest.mark.parametrize("hook", [None, "loaded"], ids=["unset", "set"])
def test_executable_reports_the_inherited_runtime_hook_setting(
    hook: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The installed executable sees the environment inherited by its subprocess."""
    if hook is None:
        monkeypatch.delenv("AFAIRESI_CONSUMER_HOOK", raising=False)
    else:
        monkeypatch.setenv("AFAIRESI_CONSUMER_HOOK", hook)
    result = subprocess.run(  # noqa: S603 - The test environment supplies the built executable.
        [os.environ["PACKAGE_E2E_EXECUTABLE"]],
        capture_output=True,
        text=True,
        check=True,
    )
    if result.stdout != f"{hook or 'unloaded'}\n[]\n":
        message = "The installed executable changed its runtime hook or argument output"
        raise AssertionError(message)

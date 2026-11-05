import os
import subprocess


def test_cli() -> None:
    result = subprocess.run(
        [os.environ["PACKAGE_E2E_EXECUTABLE"]],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout == "unloaded\n[]\n"

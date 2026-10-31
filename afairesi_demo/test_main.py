# Copyright (c) 2026- Paschalis Bizopoulos
"""Exercise terminal capture, failed recordings, and isolated demo execution."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

from afairesi_demo.main import Demo, Recorder

if TYPE_CHECKING:
    from pathlib import Path


def test_terminal_recording_and_nonzero_exit(tmp_path: Path) -> None:
    """Capture real terminal output, Unicode, and diagnostics before failing."""
    stream = io.StringIO()
    recorder = Recorder(stream, speed=2, idle_limit=0.1)
    recorder.command(
        [sys.executable, "-c", "import os; print(os.isatty(1)); print('日本語')"],
        tmp_path,
        dict(os.environ),
        timeout=5,
    )
    with pytest.raises(RuntimeError, match="exited 7"):
        recorder.command(
            [sys.executable, "-c", "import sys; print('diagnostic'); sys.exit(7)"],
            tmp_path,
            dict(os.environ),
            timeout=5,
        )
    header, *events = [json.loads(line) for line in stream.getvalue().splitlines()]
    if (header["version"], header["width"]) != (2, 100):
        raise AssertionError(header)
    if not all(event[1] == "o" for event in events):
        raise AssertionError(events)
    if [event[0] for event in events] != sorted(event[0] for event in events):
        raise AssertionError(events)
    output = "".join(event[2] for event in events)
    if "True\r\n日本語\r\n" not in output or "diagnostic\r\n" not in output:
        raise AssertionError(output)


def test_timeout_stops_descendants(tmp_path: Path) -> None:
    """A child holding the terminal cannot keep a failed recording alive."""
    marker = tmp_path / "escaped"
    child = (
        "import time; from pathlib import Path; time.sleep(1); "
        f"Path({str(marker)!r}).touch()"
    )
    parent = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(5)"
    )
    recorder = Recorder(io.StringIO(), speed=1, idle_limit=1)
    with pytest.raises(TimeoutError, match="exceeded"):
        recorder.command(
            [sys.executable, "-c", parent],
            tmp_path,
            dict(os.environ),
            timeout=0.3,
        )
    subprocess.run(
        [sys.executable, "-c", "import time; time.sleep(1.1)"],
        check=True,
    )
    if marker.exists():
        message = "A descendant survived the timeout"
        raise AssertionError(message)


def test_interactive_terminal_and_pipeline_failure(tmp_path: Path) -> None:
    """Deliver keys through a controlling terminal and propagate pipeline failures."""
    stream = io.StringIO()
    recorder = Recorder(stream, speed=1, idle_limit=1)
    recorder.command(
        [
            sys.executable,
            "-c",
            (
                "import os, tty; tty.setraw(0); "
                "terminal = os.open('/dev/tty', os.O_RDONLY); "
                "print('received=' + os.read(terminal, 1).decode())"
            ),
        ],
        tmp_path,
        dict(os.environ),
        timeout=5,
        terminal_input=((0.3, b"q"),),
    )
    output = "".join(json.loads(line)[2] for line in stream.getvalue().splitlines()[1:])
    if "received=q" not in output:
        raise AssertionError(output)
    demo = Demo(tmp_path, recorder, "x86_64-linux")
    with pytest.raises(RuntimeError, match="exited 7"):
        demo.shell("(exit 7) | cat")


def test_private_home_and_local_remote(tmp_path: Path) -> None:
    """Register a sample via a hosted URL without touching the user's Git config."""
    root = tmp_path / "workspace"
    root.mkdir()
    demo = Demo(
        root,
        Recorder(io.StringIO(), speed=10, idle_limit=0.01),
        "x86_64-linux",
    )
    demo.cwd = root / "repositories" / "sample"
    demo.cwd.mkdir(parents=True)
    demo.run("git", "init")
    demo.run("git", "commit", "--allow-empty", "-m", "Sample")
    demo.home_scene()
    if not (demo.home / ".git").is_dir():
        message = "No isolated Git home was created"
        raise AssertionError(message)
    if "insteadOf" not in (root / "gitconfig").read_text():
        message = "No private remote rewrite was configured"
        raise AssertionError(message)
    if (demo.home / "github.com" / "afairesi-demo" / "sample").exists():
        message = "home rm left the checkout behind"
        raise AssertionError(message)

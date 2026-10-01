#!/usr/bin/env python3
# Copyright (c) 2026- Paschalis Bizopoulos
# ruff: noqa: S603, S607
"""Generate a repeatable MP4 demonstration of canonical_browser."""

import argparse
import fcntl
import json
import os
import pty
import select
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
from pathlib import Path

DEMO_MAIN = (
    "import argparse\n\n"
    'message = "before"\n\n'
    "def main():\n"
    '    parser = argparse.ArgumentParser(description="Example CLI")\n'
    '    parser.add_argument("--message", help="Message to print")\n'
    "    parser.parse_args()\n\n"
    'if __name__ == "__main__":\n'
    "    main()\n"
)
WIDTH, HEIGHT, TIMEOUT = 100, 30, 180
PLAYBACK_SLOWDOWN = 2.0


def schedule_viewer(actions: list[tuple[float, bytes]]) -> None:
    """Demonstrate navigation, interface search, changes, and refresh."""
    moment = time.monotonic() + 1.5
    for keys, pause in (
        (b"l", 1.0),
        (b"/Arguments\n", 1.0),
        (b"l", 1.2),
        (b"/Tests\n", 1.0),
        (b"l", 1.2),
        (b"D", 1.2),
        (b"l", 1.0),
        (b"D", 1.0),
        (b"r", 1.2),
        (b"q", 0.0),
    ):
        actions.append((moment, keys))
        moment += pause


def slow_cast(path: Path) -> None:
    """Stretch event timestamps while keeping the terminal recording intact."""
    lines = path.read_text(encoding="utf-8").splitlines()
    with path.open("w", encoding="utf-8") as stream:
        stream.write(lines[0] + "\n")
        for line in lines[1:]:
            event = json.loads(line)
            event[0] *= PLAYBACK_SLOWDOWN
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")


def session() -> int:  # noqa: PLR0915
    """Drive canonical_browser in a fixed-size PTY and a temporary workspace."""
    with tempfile.TemporaryDirectory(prefix="canonical-browser-video-") as root:
        workspace = Path(root) / "workspace"
        workspace.mkdir()
        (workspace / "README.md").write_text(
            "# Demo repository\n\nA tiny Python package for the demo.\n",
            encoding="utf-8",
        )
        package = workspace / "packages/example"
        package.mkdir(parents=True)
        (workspace / "flake.nix").write_text("{}\n", encoding="utf-8")
        (package / "default.nix").write_text(
            '{ meta.description = "Demo package for canonical_browser"; }\n',
            encoding="utf-8",
        )
        (package / "main.py").write_text(
            DEMO_MAIN.replace('message = "before"', 'message = "initial"'),
            encoding="utf-8",
        )
        (package / "test_main.py").write_text(
            "from pathlib import Path\n\n"
            "def test_message_argument_is_available():\n"
            '    source = Path(__file__).with_name("main.py").read_text()\n'
            '    assert "--message" in source\n',
            encoding="utf-8",
        )
        subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
        subprocess.run(["git", "add", "."], cwd=workspace, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Demo",
                "-c",
                "user.email=demo@example.invalid",
                "commit",
                "-qm",
                "Baseline demo package",
            ],
            cwd=workspace,
            check=True,
        )
        (package / "main.py").write_text(
            DEMO_MAIN.replace('"--message"', '"--text"'),
            encoding="utf-8",
        )
        env = os.environ | {
            "HOME": root,
            "TERM": "xterm-256color",
            "COLUMNS": str(WIDTH),
            "LINES": str(HEIGHT),
        }
        print("$ canonical_browser --help", flush=True)  # noqa: T201
        subprocess.run(
            ["canonical_browser", "--help"],
            cwd=workspace,
            env=env,
            check=True,
            timeout=TIMEOUT,
        )
        time.sleep(1.2)
        print("\n$ canonical_browser", flush=True)  # noqa: T201
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", HEIGHT, WIDTH, 0, 0))
        try:
            process = subprocess.Popen(
                ["canonical_browser"],
                cwd=workspace,
                env=env,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                start_new_session=True,
            )
        finally:
            os.close(slave)
        actions: list[tuple[float, bytes]] = []
        schedule_viewer(actions)
        deadline = time.monotonic() + TIMEOUT
        try:
            while time.monotonic() < deadline:
                if actions and time.monotonic() >= actions[0][0]:
                    _, keys = actions.pop(0)
                    os.write(master, keys)
                ready, _, _ = select.select([master], [], [], 0.02)
                if ready:
                    try:
                        data = os.read(master, 65536)
                    except OSError:
                        break
                    if not data:
                        break
                    os.write(sys.stdout.fileno(), data)
                if process.poll() is not None:
                    break
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            status = process.wait(timeout=5)
            if status or actions:
                return status or 1
            return 0
        finally:
            os.close(master)
            if process.poll() is None:
                process.kill()
                process.wait()


def repository_root() -> Path:
    """Find this canonical checkout from any working directory."""
    candidates = list(Path.cwd().resolve().parents)
    candidates.insert(0, Path.cwd().resolve())
    home = Path.home()
    configured = subprocess.run(
        [
            "git",
            "-C",
            str(home),
            "config",
            "--file",
            ".gitmodules",
            "--get-regexp",
            r"^submodule\..*\.path$",
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    candidates.extend(
        home / line.split(maxsplit=1)[1]
        for line in configured.stdout.splitlines()
        if len(line.split(maxsplit=1)) == 2  # noqa: PLR2004
    )
    for candidate in candidates:
        root = candidate.resolve()
        if (root / "flake.nix").is_file() and (
            root / "packages/canonical_browser_video/default.nix"
        ).is_file():
            return root
    message = "Cannot locate the canonical checkout containing canonical_browser_video"
    raise RuntimeError(message)


def generate() -> Path:
    """Record a terminal session, render it, and save the MP4 in this package."""
    for executable in ("canonical_browser", "asciinema", "agg", "ffmpeg", "git"):
        if shutil.which(executable) is None:
            message = f"Required executable not found: {executable}"
            raise RuntimeError(message)
    output = (
        repository_root() / "packages/canonical_browser_video/tmp/canonical_browser.mp4"
    )
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="canonical-browser-video-") as directory:
        cast, gif = Path(directory) / "demo.cast", Path(directory) / "demo.gif"
        command = shlex.join(
            [sys.executable, str(Path(__file__).resolve()), "--session"],
        )
        subprocess.run(
            [
                "asciinema",
                "rec",
                "--overwrite",
                "--return",
                "--window-size",
                f"{WIDTH}x{HEIGHT}",
                "--command",
                command,
                str(cast),
            ],
            check=True,
            timeout=TIMEOUT + 10,
        )
        slow_cast(cast)
        subprocess.run(
            [
                "agg",
                "--theme",
                "solarized-light",
                "--font-size",
                "18",
                "--font-family",
                "DejaVu Sans Mono",
                "--font-dir",
                os.environ["CANONICAL_BROWSER_VIDEO_FONT_DIR"],
                str(cast),
                str(gif),
            ],
            check=True,
            timeout=TIMEOUT,
        )
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(gif),
                "-vf",
                "scale=trunc(iw/2)*2:trunc(ih/2)*2",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(output),
            ],
            check=True,
            timeout=TIMEOUT,
        )
    return output


def main(argv: list[str] | None = None) -> None:
    """Parse arguments and generate the demo video."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.session:
        raise SystemExit(session())
    try:
        output = generate()
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"canonical_browser_video: {exc}\n")
    print(f"Created {output}")  # noqa: T201


if __name__ == "__main__":
    main()

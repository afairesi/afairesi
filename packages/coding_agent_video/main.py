#!/usr/bin/env python3
# Copyright (c) 2026- Paschalis Bizopoulos
# ruff: noqa: S603, S607
"""Generate a repeatable MP4 demonstration of coding_agent."""

import argparse
import fcntl
import http.server
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
import threading
import time
from pathlib import Path
from typing import Any

PROMPT = "Demonstrate the available file, shell, Nix, and canonical tools."
FOLLOW_UP = "Show me the final contents of packages/example/main.py."
ONE_SHOT = "Summarize the demo repository in one sentence."
FINISH_TOOLS = "All six tools ran: read, write, edit, bash, nix, and git-canonical."
FINISH_READ = "The package's main.py now contains message = after."
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
FIRST_BATCH_TOOLS = 3
WAITING_FOR_PROMPT, RUNNING_TOOLS, RUNNING_FOLLOW_UP, BROWSING, DONE = range(5)


class Handler(http.server.BaseHTTPRequestHandler):
    """Serve fixed OpenAI-compatible responses for the demonstration."""

    completions = 0

    def do_GET(self) -> None:
        """Return the fixed mock model listing."""
        self.send_json({"data": [{"id": "coding-agent-demo"}]})

    def do_POST(self) -> None:
        """Return the next scripted assistant response."""
        request = json.loads(
            self.rfile.read(int(self.headers.get("Content-Length", "0"))),
        )
        messages = request["messages"]
        user_prompt = next(
            message["content"]
            for message in reversed(messages)
            if message["role"] == "user"
        )
        message: dict[str, Any]
        if user_prompt == ONE_SHOT:
            message = {
                "role": "assistant",
                "content": (
                    "This demo repository contains a Python package with a README."
                ),
            }
        elif user_prompt == PROMPT and messages[-1]["role"] == "user":
            message = {
                "role": "assistant",
                "content": "I will exercise each tool in this isolated workspace.",
                "tool_calls": [
                    self.call("demo-read", "read", path="README.md"),
                    self.call(
                        "demo-write",
                        "write",
                        path="packages/example/main.py",
                        content=DEMO_MAIN,
                    ),
                    self.call(
                        "demo-edit",
                        "edit",
                        path="packages/example/main.py",
                        old_text="before",
                        new_text="after",
                    ),
                ],
            }
        elif (
            user_prompt == PROMPT
            and len(
                [item for item in messages if item.get("role") == "tool"],
            )
            == FIRST_BATCH_TOOLS
        ):
            message = {
                "role": "assistant",
                "content": (
                    "The file is edited. I will check it and run the command tools."
                ),
                "tool_calls": [
                    self.call(
                        "demo-bash",
                        "bash",
                        command=(
                            "sed -i 's/Message to print/Updated message to print/' "
                            "packages/example/main.py && cat packages/example/main.py"
                        ),
                    ),
                    self.call("demo-nix", "nix", arguments="--version"),
                    self.call(
                        "demo-canonical",
                        "git-canonical",
                        arguments="args packages/example",
                    ),
                ],
            }
        elif user_prompt == FOLLOW_UP and messages[-1]["role"] != "tool":
            message = {
                "role": "assistant",
                "tool_calls": [
                    self.call(
                        "followup-read",
                        "read",
                        path="packages/example/main.py",
                    ),
                ],
            }
        elif user_prompt == PROMPT:
            results = [
                item["content"] for item in messages if item.get("role") == "tool"
            ]
            expected = (
                "A tiny Python package",
                "OK",
                "OK",
                'message = "after"',
                "exit status: 0",
                "--message",
            )
            if len(results) != len(expected) or any(
                text not in result
                for text, result in zip(expected, results, strict=True)
            ):
                self.send_error(500, "A scripted demo tool did not succeed")
                return
            message = {"role": "assistant", "content": FINISH_TOOLS}
        elif user_prompt == FOLLOW_UP:
            result = messages[-1]["content"]
            if 'message = "after"' not in result:
                self.send_error(500, "The follow-up read did not return edited content")
                return
            message = {"role": "assistant", "content": FINISH_READ}
        else:
            message = {
                "role": "assistant",
                "content": "This local model response is scripted for the demo.",
            }
        self.send_json({"choices": [{"message": message}]})

    @staticmethod
    def call(identifier: str, name: str, **arguments: str) -> dict[str, Any]:
        """Create a valid model tool call."""
        return {
            "id": identifier,
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }

    def send_json(self, body: object) -> None:
        """Send a JSON response to the mock model client."""
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, message_format: str, *args: object) -> None:
        """Keep HTTP request logs out of the recording."""
        del message_format, args


def schedule_prompt(actions: list[tuple[float, bytes]], prompt: str) -> None:
    """Queue keystrokes while leaving the PTY free to be recorded."""
    moment = time.monotonic() + 1.2
    for character in prompt:
        actions.append((moment, character.encode()))
        moment += 0.025
    actions.append((moment + 0.5, b"\n"))


def schedule_viewer(actions: list[tuple[float, bytes]]) -> None:
    """Show transcript search, expansion, package overview, and refresh."""
    moment = time.monotonic() + 1.5
    for keys, pause in (
        (b"\x1b", 0.9),
        (b"/main.py\n", 1.2),
        (b"n", 0.8),
        (b"N", 0.8),
        (b"h", 0.8),
        (b"l", 1.2),
        (b"L", 1.2),
        (b"l", 1.2),
        (b"j", 0.8),
        (b"l", 1.2),
        (b"/Arguments\n", 1.0),
        (b"l", 1.2),
        (b"h", 0.8),
        (b"/Tests\n", 1.0),
        (b"l", 1.2),
        (b"r", 1.2),
        (b"l", 0.8),
        (b"/Arguments\n", 0.8),
        (b"l", 1.6),
        (b"L", 1.0),
        (b"q", 1.0),
        (b"\x04", 0.0),
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


def session() -> int:  # noqa: C901, PLR0912, PLR0915
    """Drive coding_agent in a fixed-size PTY and a temporary workspace."""
    Handler.completions = 0
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="coding-agent-video-") as root:
            workspace, state = Path(root) / "workspace", Path(root) / "state"
            workspace.mkdir()
            (workspace / "README.md").write_text(
                "# Demo repository\n\nA tiny Python package for the demo.\n",
                encoding="utf-8",
            )
            package = workspace / "packages/example"
            package.mkdir(parents=True)
            (workspace / "flake.nix").write_text("{}\n", encoding="utf-8")
            (package / "default.nix").write_text(
                '{ meta.description = "Demo package for coding_agent"; }\n',
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
            env = os.environ | {
                "CODING_AGENT_BASE_URL": f"http://127.0.0.1:{server.server_port}",
                "XDG_STATE_HOME": str(state),
                "HOME": root,
                "TERM": "xterm-256color",
                "COLUMNS": str(WIDTH),
                "LINES": str(HEIGHT),
            }
            print("$ coding_agent --help", flush=True)  # noqa: T201
            subprocess.run(
                ["coding_agent", "--help"],
                cwd=workspace,
                env=env,
                check=True,
                timeout=TIMEOUT,
            )
            time.sleep(1.2)
            print("\n$ coding_agent --clear-history", flush=True)  # noqa: T201
            subprocess.run(
                ["coding_agent", "--clear-history"],
                cwd=workspace,
                env=env,
                check=True,
                timeout=TIMEOUT,
            )
            time.sleep(1.2)
            print(  # noqa: T201
                '$ coding_agent --prompt "Summarize the demo repository in '
                'one sentence."',
                flush=True,
            )
            subprocess.run(
                ["coding_agent", "--prompt", ONE_SHOT],
                cwd=workspace,
                env=env,
                check=True,
                timeout=TIMEOUT,
            )
            time.sleep(1.5)
            print("\n$ coding_agent", flush=True)  # noqa: T201
            master, slave = pty.openpty()
            fcntl.ioctl(
                slave,
                termios.TIOCSWINSZ,
                struct.pack("HHHH", HEIGHT, WIDTH, 0, 0),
            )
            process = subprocess.Popen(
                ["coding_agent"],
                cwd=workspace,
                env=env,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                start_new_session=True,
            )
            os.close(slave)
            output = bytearray()
            actions: list[tuple[float, bytes]] = []
            stage = WAITING_FOR_PROMPT
            sent_eof = False
            deadline = time.monotonic() + TIMEOUT
            try:
                while time.monotonic() < deadline:
                    if actions and time.monotonic() >= actions[0][0]:
                        _, keys = actions.pop(0)
                        os.write(master, keys)
                        if keys == b"\x04":
                            sent_eof = True
                            stage = DONE
                    ready, _, _ = select.select([master], [], [], 0.02)
                    if ready:
                        try:
                            data = os.read(master, 65536)
                        except OSError:
                            break
                        if not data:
                            break
                        output.extend(data)
                        os.write(sys.stdout.fileno(), data)
                        if stage == WAITING_FOR_PROMPT and b"> " in output:
                            schedule_prompt(actions, PROMPT)
                            stage = RUNNING_TOOLS
                        if stage == RUNNING_TOOLS and FINISH_TOOLS.encode() in output:
                            schedule_prompt(actions, FOLLOW_UP)
                            stage = RUNNING_FOLLOW_UP
                        if (
                            stage == RUNNING_FOLLOW_UP
                            and FINISH_READ.encode() in output
                        ):
                            schedule_viewer(actions)
                            stage = BROWSING
                    if process.poll() is not None:
                        break
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                status = process.wait(timeout=5)
                if status or stage != DONE or not sent_eof:
                    print(  # noqa: T201
                        "coding_agent demo did not complete successfully",
                        file=sys.stderr,
                    )
                    return status or 1
                return 0
            finally:
                os.close(master)
                if process.poll() is None:
                    process.kill()
                    process.wait()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


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
            root / "packages/coding_agent_video/default.nix"
        ).is_file():
            return root
    message = "Cannot locate the canonical checkout containing coding_agent_video"
    raise RuntimeError(message)


def generate() -> Path:
    """Record a terminal session, render it, and save the MP4 in this package."""
    for executable in ("coding_agent", "asciinema", "agg", "ffmpeg", "git"):
        if shutil.which(executable) is None:
            message = f"Required executable not found: {executable}"
            raise RuntimeError(message)
    output = repository_root() / "packages/coding_agent_video/tmp/coding_agent.mp4"
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="coding-agent-video-") as directory:
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
                os.environ["CODING_AGENT_VIDEO_FONT_DIR"],
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
        parser.exit(1, f"coding_agent_video: {exc}\n")
    print(f"Created {output}")  # noqa: T201


if __name__ == "__main__":
    main()

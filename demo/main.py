#!/usr/bin/env python3
# Copyright (c) 2026- Paschalis Bizopoulos
"""Record real afairesi commands as a portable asciinema v2 screencast."""

from __future__ import annotations

import codecs
import contextlib
import errno
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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import TextIO

RUNTIME_DIR = Path.home() / "github.com/afairesi/afairesi/demo/tmp"


def _attach_terminal() -> None:
    """Give an interactive child its PTY as the controlling terminal."""
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


class Recorder:
    """Capture a PTY stream; shorten idle time without inventing command output."""

    def __init__(self, stream: TextIO, *, speed: float, idle_limit: float) -> None:
        """Write the cast header and initialize the playback clock."""
        self.stream = stream
        self.speed = speed
        self.idle_limit = idle_limit
        self.previous = time.monotonic()
        self.elapsed = 0.0
        self.emit_json(
            {
                "version": 2,
                "width": 100,
                "height": 30,
                "timestamp": int(time.time()),
                "title": "afairesi: repositories, packages, tests, and home",
                "env": {"TERM": "xterm-256color"},
            },
        )

    def emit_json(self, value: object) -> None:
        """Flush each record so interrupted recordings remain playable."""
        self.stream.write(json.dumps(value, ensure_ascii=False) + "\n")
        self.stream.flush()

    def output(self, text: str, *, pause: float = 0.0) -> None:
        """Append UTF-8 terminal output and show it during recording."""
        now = time.monotonic()
        self.elapsed += min(now - self.previous, self.idle_limit) / self.speed + pause
        self.previous = now
        self.emit_json([round(self.elapsed, 6), "o", text])
        sys.stdout.write(text)
        sys.stdout.flush()

    def note(self, text: str) -> None:
        """Add an explicitly narrated chapter caption."""
        self.output(f"\r\n\x1b[1;33m# {text}\x1b[0m\r\n")
        self.hold(max(3.0, len(text.split()) / 2.5))

    def hold(self, seconds: float) -> None:
        """Leave the current screen visible before the next playback event."""
        self.elapsed += seconds

    def command(  # noqa: C901, PLR0912, PLR0913, PLR0915 - Keep PTY lifetime and cleanup together.
        self,
        arguments: Sequence[str],
        cwd: Path,
        environment: dict[str, str],
        *,
        timeout: float,
        terminal_input: Sequence[tuple[float, bytes]] = (),
        display: str | None = None,
    ) -> None:
        """Execute argv in a terminal, stopping its entire process group on failure."""
        command = display if display is not None else shlex.join(arguments)
        self.output(f"\r\n\x1b[1;32m$ {command}\x1b[0m\r\n")
        self.hold(max(1.5, len(command) / 35))
        master, slave = pty.openpty()
        process = None
        output_lines = 0
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
            process = subprocess.Popen(  # noqa: S603 - Explicit demo argv, never shell input.
                arguments,
                cwd=cwd,
                env=environment,
                stdin=slave if terminal_input else subprocess.DEVNULL,
                stdout=slave,
                stderr=slave,
                start_new_session=True,
                preexec_fn=_attach_terminal if terminal_input else None,  # noqa: PLW1509 - Single-threaded recorder.
            )
            os.close(slave)
            slave = -1
            deadline = time.monotonic() + timeout
            started = time.monotonic()
            pending_input = iter(terminal_input)
            next_input = next(pending_input, None)
            decoder = codecs.getincrementaldecoder("utf-8")("replace")
            while True:
                if time.monotonic() >= deadline:
                    message = f"command exceeded {timeout:g}s: {shlex.join(arguments)}"
                    raise TimeoutError(message)
                if (
                    next_input is not None
                    and time.monotonic() - started >= next_input[0]
                ):
                    os.write(master, next_input[1])
                    next_input = next(pending_input, None)
                ready, _, _ = select.select([master], [], [], 0.1)
                if not ready:
                    continue
                try:
                    chunk = os.read(master, 65536)
                except OSError as error:
                    if error.errno != errno.EIO:
                        raise
                    break
                if not chunk:
                    break
                text = decoder.decode(chunk)
                if text:
                    self.output(text)
                    output_lines += text.count("\n")
            tail = decoder.decode(b"", final=True)
            if tail:
                self.output(tail)
            code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
            if code:
                message = f"command exited {code}: {shlex.join(arguments)}"
                raise RuntimeError(message)
            self.hold(min(10.0, max(2.5, output_lines / 4)))
        finally:
            if process is not None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            os.close(master)
            if slave != -1:
                os.close(slave)


class Demo:
    """Create separate example flakes and a disposable Git-based home."""

    def __init__(
        self,
        root: Path,
        recorder: Recorder,
        system: str,
    ) -> None:
        """Prepare private Git configuration and identities."""
        self.root = root
        self.recorder = recorder
        self.system = system
        self.cwd = root
        self.repositories: list[Path] = []
        self.home = root
        config = root / ".gitconfig"
        config.write_text(
            "[user]\n name = Demo\n email = demo@example.invalid\n"
            "[init]\n defaultBranch = main\n"
            "[commit]\n gpgSign = false\n"
            "[core]\n hooksPath = /dev/null\n"
            '[protocol "file"]\n allow = always\n',
        )
        self.environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("GIT_", "XDG_"))
        }
        self.environment.update(
            HOME=str(self.home),
            GIT_CONFIG_GLOBAL=str(config),
            GIT_CONFIG_NOSYSTEM="1",
            GIT_TERMINAL_PROMPT="0",
            TERM="xterm-256color",
            PAGER="cat",
            BROWSER="true",
            NIX_CONFIG=os.environ.get("NIX_CONFIG", "")
            + "\nexperimental-features = nix-command flakes\n"
            "accept-flake-config = false\n",
        )

    def run(self, *arguments: str, explain: str | None = None) -> None:
        """Record one real command in the current repository."""
        if explain is not None:
            self.recorder.note(explain)
        self.recorder.command(
            arguments,
            self.cwd,
            self.environment,
            timeout=1800,
        )

    def write(self, relative: str, source: str) -> None:
        """Prepare a fixture without printing its contents."""
        path = self.cwd / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")

    def shell(
        self,
        source: str,
        *,
        browse: bool = False,
        explain: str | None = None,
        terminal_input: Sequence[tuple[float, bytes]] = (),
    ) -> None:
        """Record a fixed pipeline and briefly navigate an interactive JSON viewer."""
        if explain is not None:
            self.recorder.note(explain)
        self.recorder.command(
            ["bash", "-o", "pipefail", "-c", source],
            self.cwd,
            self.environment,
            timeout=120,
            terminal_input=(
                ((3, b"j"), (6, b"l"), (12, b"q")) if browse else terminal_input
            ),
            display=source,
        )

    def tree(self) -> None:
        """Browse the source layout without opening file contents or generated data."""
        self.shell(
            "tree -a --gitignore -I .git --noreport | less -R",
            explain=(
                "Browse the source tree in less; "
                "Git metadata and ignored files are hidden."
            ),
            terminal_input=((6, b" "), (12, b"q")),
        )

    def start_home(self) -> None:
        """Initialize the artificial home before creating repositories inside it."""
        self.cwd = self.home
        self.run(
            "afairesi",
            "home",
            "init",
            explain=f"Initialize the home directory: {self.home}",
        )

    def register_repository(self, name: str) -> None:
        """Register an empty local seed through the normal hosted-URL workflow."""
        seed = self.root / ".remotes" / name
        seed.mkdir(parents=True)
        remote = f"https://github.com/afairesi-demo/{name}.git"
        for arguments in (
            ["init", "--quiet"],
            ["commit", "--quiet", "--allow-empty", "-m", "Initial commit"],
            ["config", "--global", f"url.{seed.as_uri()}.insteadOf", remote],
        ):
            subprocess.run(  # noqa: S603 - Fixed setup commands in the private fixture.
                [shutil.which("git") or "git", *arguments],
                cwd=seed,
                env=self.environment,
                check=True,
                timeout=30,
            )
        self.cwd = self.home
        self.run(
            "afairesi",
            "home",
            "add",
            remote,
            explain=f"Register {name} under ~/github.com/afairesi-demo/.",
        )
        self.cwd = self.home / "github.com" / "afairesi-demo" / name
        self.repositories.append(self.cwd)

    def repository(self, name: str) -> None:
        """Create an afairesi flake inside a repository registered in the home."""
        self.register_repository(name)
        self.run(
            "afairesi",
            "repo",
            "init",
            explain=f"Initialize {name} with an afairesi flake.",
        )

    def python(self) -> None:
        """Show a useful CLI, tests, structural inspection, and package lifecycle."""
        self.repository("toolbox")
        self.run(
            "afairesi",
            "repo",
            "add",
            "hello",
            "python",
            explain="Create a Python package named hello from the standard template.",
        )
        self.recorder.note("Prepare a greeting CLI, tests, and supporting files.")
        self.write(
            "hello/main.py",
            "#!/usr/bin/env python3\n# Copyright (c) 2026- Demo authors\n"
            '"""Greet someone from an afairesi package."""\n'
            "import argparse\nimport sys\n\n\ndef greeting(name: str) -> str:\n"
            '    """Return a friendly greeting."""\n'
            '    return f"Hello, {name}!"\n\n\ndef main() -> None:\n'
            '    """Parse a name and print a greeting."""\n'
            "    parser = argparse.ArgumentParser(description=__doc__)\n"
            '    parser.add_argument("name", nargs="?", default="afairesi")\n'
            '    sys.stdout.write(greeting(parser.parse_args().name) + "\\n")\n\n\n'
            'if __name__ == "__main__":\n    main()\n',
        )
        self.write(
            "hello/test_main.py",
            "# Copyright (c) 2026- Demo authors\n"
            '"""Check the greeting contract."""\n'
            "from hypothesis import given, strategies as st\n"
            "from hello.main import greeting\n\n\ndef test_greeting() -> None:\n"
            '    """Keep the greeting stable."""\n'
            '    if greeting("Ada") != "Hello, Ada!":\n'
            '        message = "The greeting changed"\n'
            "        raise AssertionError(message)\n\n\n"
            "@given(st.text())\ndef test_fuzz_greeting(name: str) -> None:\n"
            '    """Preserve arbitrary names in the greeting."""\n'
            '    if greeting(name) != f"Hello, {name}!":\n'
            '        message = "The name was not preserved"\n'
            "        raise AssertionError(message)\n",
        )
        self.write("hello/prm/example.txt", "Supporting files are tracked.\n")
        self.write("hello/tmp/session.txt", "Temporary files are ignored.\n")
        self.run(
            "afairesi",
            "repo",
            "fix",
            explain=(
                "Stage the example sources and refresh the repository's managed files."
            ),
        )
        self.run("nix", "fmt")
        self.run("git", "add", ".")
        self.run("nix", "run", ".#hello", "--", "Ada")
        self.run("nix", "flake", "check")
        self.shell(
            "afairesi repo inspect | jless",
            browse=True,
            explain="Browse the package interface and test names in jless.",
        )
        self.run("git", "commit", "-m", "Add tested greeting CLI")
        self.recorder.note(
            "Change the package description to see a small interface diff.",
        )
        self.write(
            "hello/default.nix",
            (self.cwd / "hello/default.nix")
            .read_text()
            .replace(
                '"A Python package."',
                '"A greeting CLI."',
            ),
        )
        self.run("git", "add", "hello/default.nix")
        self.shell(
            "afairesi repo inspect diff --cached | less -R",
            explain="Review the staged interface changes, already rendered by jd.",
            terminal_input=((10, b"q"),),
        )
        self.run("git", "commit", "-m", "Describe the greeting CLI")

    def website(self) -> None:
        """Create a second repository with a different package type."""
        self.repository("website")
        self.run(
            "afairesi",
            "repo",
            "add",
            "site",
            "html",
            explain="Create a website package in the second repository.",
        )
        self.run("git", "commit", "-m", "Add website package")

    def home_scene(self) -> None:
        """Show the completed home with both repositories registered as submodules."""
        self.cwd = self.home
        self.run(
            "git",
            "add",
            "--",
            *(str(p.relative_to(self.home)) for p in self.repositories),
        )
        self.run("git", "commit", "-m", "Register demo repositories")
        self.tree()


def record(root: Path, output: Path, system: str) -> None:
    """Run every chapter, preserving a partial cast if a command fails."""
    with output.open("w", encoding="utf-8") as stream:
        recorder = Recorder(stream, speed=1, idle_limit=10)
        demo = Demo(root, recorder, system)
        try:
            demo.start_home()
            demo.python()
            demo.website()
            demo.home_scene()
            recorder.note("Demo complete. Changes were staged and committed with Git.")
            recorder.output("\r\n", pause=4.0)
        except (
            OSError,
            RuntimeError,
            subprocess.SubprocessError,
            KeyboardInterrupt,
        ) as error:
            recorder.note(f"Recording stopped: {error}")
            raise
    recording = json.dumps(output.read_text(encoding="utf-8"))
    output.with_suffix(".js").write_text(
        "// Generated from afairesi-demo.cast for playback from local HTML files.\n"
        f"globalThis.afairesiDemoRecording = {recording};\n",
        encoding="utf-8",
    )


def main() -> None:
    """Record the complete demo with no configuration or arguments."""
    output = RUNTIME_DIR / "afairesi-demo.cast"
    missing = [
        name
        for name in (
            "afairesi",
            "git",
            "nix",
            "bash",
            "jless",
            "less",
            "tree",
        )
        if shutil.which(name) is None
    ]
    if missing:
        sys.exit(f"missing executables: {', '.join(missing)}")
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        system = subprocess.check_output(  # noqa: S603 - Fixed Nix query.
            [
                shutil.which("nix") or "nix",
                "eval",
                "--impure",
                "--raw",
                "--expr",
                "builtins.currentSystem",
            ],
            text=True,
            timeout=30,
        ).strip()
        with tempfile.TemporaryDirectory(
            prefix="home-",
            dir="/tmp",
        ) as temporary:
            record(Path(temporary), output, system)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        sys.stderr.write(f"error: {error}\nPartial recording (if started): {output}\n")
        sys.exit(1)
    except KeyboardInterrupt:
        sys.stderr.write(f"Interrupted; partial recording: {output}\n")
        sys.exit(130)
    sys.stdout.write(
        f"Recording: {output}\nPlay: asciinema play {shlex.quote(str(output))}\n",
    )


if __name__ == "__main__":
    main()

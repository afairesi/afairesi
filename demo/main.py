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
import termios
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from typing import TextIO

RUNTIME_DIR = Path.home() / "github.com/afairesi/afairesi/demo/tmp"

POST = """A small notes site should be easy to maintain. I keep the articles and the
tools that help publish them in separate repositories, so each project has a
clear purpose and its own history. Both repositories live inside my Git-based
home directory, where I can find them again after setting up another machine.

Before publishing a new article, I estimate how long it takes to read. A simple
command counts the words and rounds the result up to whole minutes. Readers
have different reading speeds, so the command should let me choose the rate
instead of hiding that choice in its implementation.

The website shows the estimate beside the article. Tests check the rounding and
the command line option before I commit the change. Finally, I record the new
repository revisions in my home repository, keeping the complete workspace easy
to reproduce."""


@contextlib.contextmanager
def artificial_home(directory: str | Path) -> Iterator[Path]:
    """Create an owned home directory, preserving any pre-existing directory."""
    path = Path(directory)
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path)


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
                "title": "afairesi: build a reading-time tool and notes site",
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
        self.hold(max(3.0, len(text.split()) / 3))

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
            if output_lines:
                self.hold(min(4.0, max(1.5, output_lines / 6)))
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
    ) -> None:
        """Prepare private Git configuration and identities."""
        self.root = root
        self.recorder = recorder
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
                ((2, b"j"), (4, b"l"), (6, b"q")) if browse else terminal_input
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
            terminal_input=((3, b" "), (6, b"q")),
        )

    def start_home(self) -> None:
        """Initialize the artificial home before creating repositories inside it."""
        self.cwd = self.home
        self.run(
            "afairesi",
            "home",
            "init",
            explain=f"Set up the notes workspace in {self.home}.",
        )

    def cd(self, directory: Path) -> None:
        """Show and perform directory changes between project steps."""
        if directory != self.cwd:
            relative = directory.relative_to(self.home)
            destination = (
                "~" if relative == Path() else "~/" + shlex.quote(str(relative))
            )
            self.shell(f"cd {destination}")
            self.cwd = directory

    def register_repository(self, name: str) -> None:
        """Register an empty local seed through the normal hosted-URL workflow."""
        seed = self.root / ".remotes" / name
        seed.mkdir(parents=True)
        remote = f"https://github.com/user/{name}.git"
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
        self.cd(self.home)
        self.run(
            "afairesi",
            "home",
            "add",
            remote,
            explain=f"Clone and register {name} under ~/github.com/user/.",
        )
        self.cd(self.home / "github.com" / "user" / name)
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

    def write_tool(self, *, adjustable: bool) -> None:
        """Prepare the article tool and tests without printing source code."""
        option = (
            '    parser.add_argument("--words-per-minute", type=int, default=200)\n'
            if adjustable
            else ""
        )
        rate = "options.words_per_minute" if adjustable else "200"
        self.write(
            "reading_time/main.py",
            "#!/usr/bin/env python3\n# Copyright (c) 2026- Demo authors\n"
            '"""Estimate how many minutes an article takes to read."""\n'
            "import argparse\nimport sys\nfrom pathlib import Path\n\n\n"
            "def reading_minutes(words: int, words_per_minute: int = 200) -> int:\n"
            '    """Round the reading time up to whole minutes."""\n'
            "    if words < 0 or words_per_minute <= 0:\n"
            '        message = "Invalid word count or reading speed"\n'
            "        raise ValueError(message)\n"
            "    return (words + words_per_minute - 1) // words_per_minute\n\n\n"
            "def main() -> None:\n"
            '    """Read an article and print its estimated reading time."""\n'
            "    parser = argparse.ArgumentParser(description=__doc__)\n"
            '    parser.add_argument("path", type=Path)\n'
            + option
            + "    options = parser.parse_args()\n"
            "    try:\n"
            "        words = len(options.path.read_text().split())\n"
            f"        minutes = reading_minutes(words, {rate})\n"
            "    except (OSError, ValueError) as error:\n"
            "        parser.error(str(error))\n"
            '    sys.stdout.write(f"{minutes} min read\\n")\n\n\n'
            'if __name__ == "__main__":\n    main()\n',
        )
        tests = (
            "# Copyright (c) 2026- Demo authors\n"
            '"""Check reading-time estimates and command line behavior."""\n'
            "import pytest\nfrom reading_time.main import reading_minutes\n\n\n"
            "@pytest.mark.parametrize(\n"
            '    ("words", "expected"), [(0, 0), (1, 1), (200, 1), (201, 2)],\n'
            ")\n"
            "def test_reading_minutes(words: int, expected: int) -> None:\n"
            '    """Round partial minutes up, including at the boundary."""\n'
            "    if reading_minutes(words) != expected:\n"
            '        message = "Incorrect reading time"\n'
            "        raise AssertionError(message)\n"
        )
        if adjustable:
            tests = tests.replace(
                "import pytest\n",
                "import sys\nfrom pathlib import Path\nimport pytest\n",
            )
            tests = tests.replace(
                "import reading_minutes\n",
                "import main, reading_minutes\n",
            )
            tests += (
                "\n\ndef test_reading_speed(\n"
                "    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,\n"
                "    capsys: pytest.CaptureFixture[str],\n"
                ") -> None:\n"
                '    """Use the requested reading speed in the CLI output."""\n'
                '    article = tmp_path / "article.txt"\n'
                '    article.write_text("one two three four")\n'
                "    arguments = [\n"
                '        "reading_time", str(article), "--words-per-minute", "2",\n'
                "    ]\n"
                '    monkeypatch.setattr(sys, "argv", arguments)\n'
                "    main()\n"
                '    if capsys.readouterr().out != "2 min read\\n":\n'
                '        message = "The reading speed option was ignored"\n'
                "        raise AssertionError(message)\n"
            )
        self.write("reading_time/test_main.py", tests)

    def python(self) -> None:
        """Create and verify the complete reading-time tool in one pass."""
        self.repository("notes-tools")
        self.run(
            "afairesi",
            "repo",
            "add",
            "reading_time",
            "python",
            explain="Create the tool that estimates article reading time.",
        )
        self.recorder.note("Implement the tool and its reading-speed tests.")
        self.write_tool(adjustable=True)
        self.write(
            "reading_time/default.nix",
            (self.cwd / "reading_time/default.nix")
            .read_text()
            .replace(
                '"A Python package."',
                '"Estimate reading time for notes."',
            ),
        )
        self.run(
            "afairesi",
            "repo",
            "fix",
            explain="Stage the tool and tests under the repository policy.",
        )
        self.shell("nix fmt --quiet && nix flake check --quiet")
        self.run("git", "add", ".")
        self.shell(
            "afairesi repo inspect | jless",
            browse=True,
            explain="Review the tool's public interface.",
        )
        self.shell(
            "afairesi repo inspect diff --cached | less -R",
            explain="Check the new interface before committing.",
            terminal_input=((6, b"q"),),
        )
        self.run("git", "commit", "-m", "Add tested article reading-time tool")

    def website(self) -> None:
        """Use the tool from a second repository to publish the first article."""
        self.repository("notes-site")
        self.run(
            "afairesi",
            "repo",
            "add",
            "notes",
            "html",
            explain="Create the companion website for our notes.",
        )
        self.write("notes/prm/first-post.txt", POST)
        self.recorder.note("Estimate the first article at 100 words per minute.")
        self.run(
            "nix",
            "run",
            "../notes-tools#reading_time",
            "--",
            "notes/prm/first-post.txt",
            "--words-per-minute",
            "100",
        )
        self.recorder.note("Add the article and reading-time label to the site.")
        minutes = (len(POST.split()) + 99) // 100
        paragraphs = "".join(f"<p>{text}</p>" for text in POST.split("\n\n"))
        self.write(
            "notes/index.html",
            '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
            "<title>Field notes</title></head><body><main><article>"
            f"<h1>A reproducible notes workspace</h1><p>{minutes} min read</p>"
            + paragraphs
            + "</article></main></body></html>\n",
        )
        self.run(
            "afairesi",
            "repo",
            "fix",
            explain="Stage the article and website together.",
        )
        self.shell("nix fmt --quiet && nix build .#notes --no-link --quiet")
        self.run("git", "commit", "-am", "Publish the first note with its reading time")

    def home_scene(self) -> None:
        """Show the completed home with both repositories registered as submodules."""
        self.cd(self.home)
        self.run(
            "git",
            "add",
            "--",
            *(str(p.relative_to(self.home)) for p in self.repositories),
        )
        self.run("git", "commit", "-m", "Track the reading-time tool and notes site")
        self.tree()


def record(root: Path, output: Path) -> None:
    """Run every chapter, preserving a partial cast if a command fails."""
    with output.open("w", encoding="utf-8") as stream:
        recorder = Recorder(stream, speed=1, idle_limit=3)
        demo = Demo(root, recorder)
        try:
            demo.start_home()
            demo.python()
            demo.website()
            demo.home_scene()
            recorder.note("Workspace ready.")
            recorder.output("\r\n", pause=2.0)
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
        with artificial_home("/tmp/home/user") as home:  # noqa: S108 - Requested path; exclusive creation and owned cleanup.
            record(home, output)
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

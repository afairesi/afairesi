#!/usr/bin/env python3
# Copyright (c) 2026- Paschalis Bizopoulos
"""An interactive or single-prompt client for a local llama.cpp coding model."""

import argparse
import contextlib
import ctypes
import curses
import fcntl
import hashlib
import json
import os
import queue
import re
import readline
import select
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import termios
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, Self, cast

from git_canonical import CommandError as GitCanonicalError
from git_canonical import (
    detect_packages,
    git,
    home_repositories,
    package_overview,
    source_package_overview,
)

if TYPE_CHECKING:
    from collections.abc import Callable
BASE_URL = os.environ.get(
    "CANONICAL_AGENT_BASE_URL",
    os.environ.get("CODING_AGENT_BASE_URL", "http://127.0.0.1:8080"),
)
OUTPUT_LIMIT = 16_000
BASH_TIMEOUT = 60
NIX_TIMEOUT = 600
HTTP_TIMEOUT = 300
MAX_REQUESTS = 20
READLINE_GETTER = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p)
READLINE_HANDLER = ctypes.CFUNCTYPE(None, ctypes.c_void_p)
SYSTEM_PROMPT = (
    "Work in the startup directory and read its README for repository conventions "
    "when present. "
    "In Canonical repositories, inspect package descriptions, CLI arguments, and "
    "test names before source code when they answer the task. Use git-canonical "
    "for persistent changes to canonical repository structure. Put tracked "
    "resources outside the standard package layout in prm/ and runtime output "
    "in tmp/. In Canonical flakes, nix fmt runs git canonical converge first."
)


def tool(name: str, description: str, **properties: str) -> dict[str, Any]:  # noqa: D103
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {
                    key: {"type": "string", "description": value}
                    for key, value in properties.items()
                },
                "required": list(properties),
                "additionalProperties": False,
            },
        },
    }


TOOLS = [
    tool("read", "Read a UTF-8 file.", path="File path"),
    tool(
        "write",
        "Write a UTF-8 file, replacing it; create parent directories.",
        path="File path",
        content="New contents",
    ),
    tool(
        "edit",
        "Replace exactly one literal occurrence; reject empty or ambiguous matches.",
        path="File path",
        old_text="Text to replace",
        new_text="Replacement",
    ),
    tool(
        "bash",
        "Run Bash in the startup directory with a 60-second timeout.",
        command="Bash command",
    ),
    tool(
        "nix",
        "Run Nix with flakes enabled in the startup directory; 600-second timeout. "
        "Supports build, run, develop, fmt, and flake subcommands. No shell expansion. "
        "In a Canonical flake, fmt also applies repository convergence.",
        arguments="Arguments without nix, e.g. build .#package or flake check .",
    ),
    tool(
        "git-canonical",
        "Run git-canonical in the startup directory; 600-second timeout. "
        "No shell expansion. Converge can stage and remove files.",
        arguments=(
            "Arguments without git-canonical, e.g. converge --dry-run or "
            "add packages/name python"
        ),
    ),
]


def bounded(text: str) -> str:  # noqa: D103
    marker = "\n[output truncated]"
    return (
        text
        if len(text) <= OUTPUT_LIMIT
        else text[: OUTPUT_LIMIT - len(marker)] + marker
    )


class AgentError(Exception):  # noqa: D101
    pass


class HistoryError(Exception):
    """History could not be read or safely persisted."""


class History:
    """Own one directory's locked, atomically checkpointed history."""

    def __init__(self, cwd: Path) -> None:
        """Select state storage using the resolved startup directory."""
        self.cwd = str(cwd.resolve())
        root = os.environ.get("XDG_STATE_HOME", "")
        base = (
            Path(root)
            if root and Path(root).is_absolute()
            else Path.home() / ".local/state"
        )
        key = hashlib.sha256(os.fsencode(self.cwd)).hexdigest()
        current = base / "canonical_agent" / key
        previous = base / "coding_agent" / key
        self.directory = (
            previous if previous.is_dir() and not current.exists() else current
        )
        self.path = self.directory / "history.json"
        self.prompts: list[str] = []
        self.messages: list[dict[str, Any]] = []
        self.entries: list[Entry] = []
        self.lock: int | None = None

    def __enter__(self) -> Self:
        """Acquire exclusive ownership before loading or clearing state."""
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.lock = os.open(self.directory / "lock", os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.close()
            msg = (
                f"Cannot lock history at {self.directory} "
                f"(another instance may be running): {exc}"
            )
            raise HistoryError(msg) from exc
        return self

    def close(self) -> None:
        """Release the process lock, including after failed initialization."""
        if self.lock is not None:
            os.close(self.lock)
            self.lock = None

    def __exit__(self, *args: object) -> None:
        """Release ownership on normal exit or an exception."""
        self.close()

    def load(self) -> None:
        """Validate stored data before exposing any of it to the agent."""
        try:
            try:
                content = self.path.read_text(encoding="utf-8")
            except FileNotFoundError:
                return
            data = json.loads(content)
            if data["version"] != 1 or data["cwd"] != self.cwd:
                msg = "unsupported version or directory mismatch"
                raise ValueError(msg)  # noqa: TRY301
            prompts, messages, entries = (
                data["prompts"],
                data["messages"],
                data["entries"],
            )
            if not isinstance(prompts, list) or not all(
                isinstance(p, str) for p in prompts
            ):
                msg = "invalid prompts"
                raise ValueError(msg)  # noqa: TRY301
            self.validate_messages(messages)
            if not isinstance(entries, list):
                msg = "invalid transcript"
                raise TypeError(msg)  # noqa: TRY301
            restored = []
            for item in entries:
                entry = Entry(**item)
                if (
                    not isinstance(entry.title, str)
                    or not isinstance(entry.body, str)
                    or type(entry.tool) is not bool
                    or type(entry.expanded) is not bool
                    or (entry.success is not None and type(entry.success) is not bool)
                ):
                    msg = "invalid transcript entry"
                    raise ValueError(msg)  # noqa: TRY301
                entry.expanded = False
                if entry.tool and entry.success is None:
                    entry.success = False
                    entry.body += "\nInterrupted before completion was recorded."
                restored.append(entry)
            self.prompts, self.messages, self.entries = prompts, messages, restored
        except (OSError, ValueError, TypeError, KeyError) as exc:
            msg = (
                f"Cannot load history {self.path}: {exc}. "
                "Use --clear-history to reset it."
            )
            raise HistoryError(msg) from exc

    @staticmethod
    def validate_messages(messages: Any) -> None:  # noqa: ANN401, C901, PLR0912
        """Reject malformed conversations and unmatched tool responses."""
        if not isinstance(messages, list):
            msg = "invalid messages"
            raise TypeError(msg)
        pending: set[str] = set()
        expected = "user"
        for message in messages:
            if not isinstance(message, dict):
                msg = "invalid message"
                raise TypeError(msg)
            role = message["role"]
            content = message.get("content")
            if role != ("tool" if pending else expected):
                msg = "invalid message order"
                raise ValueError(msg)
            if role == "assistant":
                calls = message.get("tool_calls", [])
                if not isinstance(calls, list) or (
                    content is not None and not isinstance(content, str)
                ):
                    msg = "invalid assistant message"
                    raise ValueError(msg)
                for call in calls:
                    identifier = call["id"]
                    if (
                        call["type"] != "function"
                        or not isinstance(identifier, str)
                        or not identifier
                        or identifier in pending
                        or not isinstance(call["function"]["name"], str)
                        or not isinstance(call["function"]["arguments"], str)
                    ):
                        msg = "invalid tool call"
                        raise ValueError(msg)
                    pending.add(identifier)
                if not calls and not isinstance(content, str):
                    msg = "missing assistant content"
                    raise ValueError(msg)
                expected = "assistant" if calls else "user"
            else:
                if not isinstance(content, str):
                    msg = "invalid message content"
                    raise ValueError(msg)
                if role == "tool":
                    pending.remove(message["tool_call_id"])
                else:
                    expected = "assistant"
        if pending or expected != "user":
            msg = "incomplete conversation"
            raise ValueError(msg)

    def save(self) -> None:
        """Replace the checkpoint only after a complete write succeeds."""
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.directory,
                delete=False,
            ) as stream:
                temporary = stream.name
                json.dump(
                    {
                        "version": 1,
                        "cwd": self.cwd,
                        "prompts": self.prompts,
                        "messages": self.messages,
                        "entries": [asdict(e) for e in self.entries],
                    },
                    stream,
                )
                stream.flush()
                os.fsync(stream.fileno())
            Path(temporary).replace(self.path)
        except OSError as exc:
            msg = f"Cannot save history {self.path}: {exc}"
            raise HistoryError(msg) from exc
        finally:
            if temporary is not None:
                with contextlib.suppress(OSError):
                    Path(temporary).unlink(missing_ok=True)

    def clear(self) -> None:
        """Remove the checkpoint while retaining the locked inode."""
        try:
            self.path.unlink(missing_ok=True)
        except OSError as exc:
            msg = f"Cannot clear history {self.path}: {exc}"
            raise HistoryError(msg) from exc

    def event(self, kind: str, text: str, success: bool | None) -> None:  # noqa: FBT001
        """Persist transcript events separately from committed model context."""
        record_event(self.entries, kind, text, success)
        self.save()


class Agent:  # noqa: D101
    def __init__(  # noqa: D107
        self,
        cwd: str | Path | None = None,
        base_url: str = BASE_URL,
        history: History | None = None,
    ) -> None:
        self.cwd = Path(cwd or Path.cwd()).resolve()
        self.history = history
        self.base_url = base_url.rstrip("/")
        self.model: str | None = None
        self.output: Callable[[str], None] | None = None
        self.event: Callable[[str, str, bool | None], None] | None = None
        self.tool_success = True
        self.tool_output = False
        self.cancel: threading.Event | None = None
        self.messages = [
            {
                "role": "system",
                "content": SYSTEM_PROMPT,
            },
        ]
        if history is not None:
            self.messages.extend(history.messages)

    def emit(self, text: str, kind: str = "chat") -> None:  # noqa: D102
        if self.history is not None:
            self.history.event(kind, text, None)
        if kind == "output":
            self.tool_output = True
        if self.output is not None:
            self.output(text)
        if self.event is not None:
            self.event(kind, text, None)

    def request(self, endpoint: str, body: dict[str, Any] | None = None) -> Any:  # noqa: ANN401, D102
        request = urllib.request.Request(  # noqa: S310
            self.base_url + endpoint,
            data=None if body is None else json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        if self.cancel is None:
            return self.receive(request)
        result: queue.Queue[tuple[Any, BaseException | None]] = queue.Queue()

        def receive() -> None:
            try:
                result.put((self.receive(request), None))
            except BaseException as exc:  # noqa: BLE001
                result.put((None, exc))

        self.check_cancelled()
        threading.Thread(target=receive, daemon=True).start()
        while True:
            self.check_cancelled()
            try:
                value, error = result.get(timeout=0.1)
            except queue.Empty:
                continue
            self.check_cancelled()
            if error is not None:
                raise error
            return value

    def check_cancelled(self) -> None:
        """Stop a background turn at a boundary before further effects."""
        if self.cancel is not None and self.cancel.is_set():
            raise KeyboardInterrupt

    @staticmethod
    def receive(request: urllib.request.Request) -> Any:  # noqa: ANN401
        """Read a response without mutating conversation or terminal state."""
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:  # noqa: S310
                return json.load(response)
        except (urllib.error.URLError, OSError) as exc:
            msg = f"Connection/HTTP error: {exc}"
            raise AgentError(msg) from exc
        except (ValueError, UnicodeError) as exc:
            msg = f"Protocol error: invalid JSON: {exc}"
            raise AgentError(msg) from exc

    def discover(self) -> None:  # noqa: D102
        data = self.request("/v1/models")
        try:
            model = data["data"][0]["id"]
            if not isinstance(model, str) or not model:
                msg = "empty or invalid model ID"
                raise ValueError(msg)  # noqa: TRY301
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            msg = "Protocol error: /v1/models has no valid model ID"
            raise AgentError(msg) from exc
        self.model = model

    def bash(self, command: str) -> str:  # noqa: D102
        return self.run(["bash", "-c", command], BASH_TIMEOUT)

    def nix(self, arguments: str) -> str:
        """Run Nix with flakes enabled."""
        nix_args = shlex.split(arguments)
        return self.run(
            ["nix", "--extra-experimental-features", "nix-command flakes", *nix_args],
            NIX_TIMEOUT,
        )

    def run(self, command: list[str], timeout: int) -> str:  # noqa: D102
        self.check_cancelled()
        with tempfile.TemporaryFile() as output:
            process = subprocess.Popen(  # noqa: S603
                command,
                cwd=self.cwd,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            timed_out = False
            cancelled = False
            try:
                deadline = time.monotonic() + timeout
                while True:
                    self.check_cancelled()
                    remaining = deadline - time.monotonic()
                    try:
                        process.wait(
                            timeout=max(0, remaining)
                            if self.cancel is None
                            else min(0.1, max(0, remaining)),
                        )
                        self.check_cancelled()
                        break
                    except subprocess.TimeoutExpired:
                        if time.monotonic() >= deadline:
                            raise
            except subprocess.TimeoutExpired:
                timed_out = True
            except KeyboardInterrupt:
                cancelled = True
            finally:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            output.seek(0)
            captured = output.read(OUTPUT_LIMIT * 4 + 1).decode(
                "utf-8",
                errors="replace",
            )
            status = f"exit status: {process.returncode}\n"
            if timed_out:
                status = f"timed out after {timeout} seconds; " + status
            self.tool_success = (
                process.returncode == 0 and not timed_out and not cancelled
            )
            if (
                self.history is not None
                or self.output is not None
                or self.event is not None
            ):
                output.seek(0)
                self.emit(
                    status + output.read().decode("utf-8", errors="replace"),
                    "output",
                )
            if cancelled:
                raise KeyboardInterrupt
            return bounded(status + captured)

    def execute(self, name: str, arguments: str) -> str:  # noqa: D102
        self.check_cancelled()
        try:
            args = json.loads(arguments)
            definition = next(
                t["function"] for t in TOOLS if t["function"]["name"] == name
            )
            if not isinstance(args, dict) or set(args) != set(
                definition["parameters"]["required"],
            ):
                msg = "incorrect tool arguments"
                raise ValueError(msg)  # noqa: TRY301
            if not all(isinstance(value, str) for value in args.values()):
                msg = "tool arguments must be strings"
                raise ValueError(msg)  # noqa: TRY301
            if name == "bash":
                return self.bash(args["command"])
            if name == "nix":
                return self.nix(args["arguments"])
            if name == "git-canonical":
                return self.run(
                    ["git-canonical", *shlex.split(args["arguments"])],
                    NIX_TIMEOUT,
                )
            path = self.cwd / args["path"]
            if name == "read":
                content = path.read_text(encoding="utf-8")
                self.emit(content, "output")
                return bounded(content)
            if name == "write":
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(args["content"], encoding="utf-8")
            else:
                old = args["old_text"]
                content = path.read_text(encoding="utf-8")
                first = content.find(old)
                if not old or first < 0 or content.find(old, first + 1) >= 0:
                    msg = "old_text must match exactly once and must not be empty"
                    raise ValueError(msg)  # noqa: TRY301
                path.write_text(
                    content.replace(old, args["new_text"], 1),
                    encoding="utf-8",
                )
            return "OK"  # noqa: TRY300
        except (OSError, ValueError, TypeError, StopIteration) as exc:
            self.tool_success = False
            return bounded(f"Tool error ({name}): {exc}")

    def completion(self) -> dict[str, Any]:  # noqa: D102
        data = self.request(
            "/v1/chat/completions",
            {
                "model": self.model,
                "messages": self.messages,
                "tools": TOOLS,
                "stream": False,
            },
        )
        try:
            message = data["choices"][0]["message"]
            if message["role"] != "assistant":
                msg = "expected assistant role"
                raise ValueError(msg)  # noqa: TRY301
            content = message.get("content")
            calls = message.get("tool_calls", [])
            if content is not None and not isinstance(content, str):
                msg = "invalid content"
                raise ValueError(msg)  # noqa: TRY301
            if not isinstance(calls, list) or (not calls and content is None):
                msg = "missing content or tool calls"
                raise ValueError(msg)  # noqa: TRY301
            ids = set()
            for call in calls:
                if (
                    call["type"] != "function"
                    or not isinstance(call["id"], str)
                    or not call["id"]
                    or call["id"] in ids
                    or not isinstance(call["function"]["name"], str)
                    or not isinstance(call["function"]["arguments"], str)
                ):
                    msg = "invalid tool call"
                    raise ValueError(msg)  # noqa: TRY301
                ids.add(call["id"])
            return {  # noqa: TRY300
                "role": "assistant",
                "content": content,
                **({"tool_calls": calls} if calls else {}),
            }
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            msg = f"Protocol error: invalid assistant response: {exc}"
            raise AgentError(msg) from exc

    def turn(self, prompt: str) -> str:  # noqa: C901, D102, PLR0912
        self.check_cancelled()
        if self.history is not None and prompt.strip():
            self.history.prompts.append(prompt)
            self.history.event("chat", "user> " + prompt, None)
            if self.event is not None:
                self.event("chat", "user> " + prompt, None)
        if self.model is None:
            self.discover()
        start = len(self.messages)
        self.messages.append({"role": "user", "content": prompt})
        try:
            for _ in range(MAX_REQUESTS):
                message = self.completion()
                self.check_cancelled()
                self.messages.append(message)
                if message.get("content"):
                    self.emit("assistant> " + message["content"])
                if not message.get("tool_calls"):
                    self.check_cancelled()
                    if self.history is not None:
                        self.history.messages = self.messages[1:]
                        self.history.save()
                    return cast("str", message["content"])
                for call in message["tool_calls"]:
                    self.check_cancelled()
                    self.emit(
                        f"tool> {call['function']['name']} "
                        f"{call['function']['arguments']}",
                        "tool",
                    )
                    self.tool_success = True
                    self.tool_output = False
                    try:
                        result = self.execute(
                            call["function"]["name"],
                            call["function"]["arguments"],
                        )
                        self.check_cancelled()
                        if not self.tool_output:
                            self.emit(result, "output")
                    except KeyboardInterrupt:
                        self.tool_success = False
                        self.emit("Cancelled", "output")
                        raise
                    finally:
                        if self.history is not None:
                            self.history.event("finish", "", self.tool_success)
                        if self.event is not None:
                            self.event("finish", "", self.tool_success)
                    self.messages.append(
                        {"role": "tool", "tool_call_id": call["id"], "content": result},
                    )
            msg = f"Stopped after {MAX_REQUESTS} model requests"
            raise AgentError(msg)  # noqa: TRY301
        except (AgentError, KeyboardInterrupt):
            del self.messages[start:]
            raise


def configure_filename_completion() -> None:  # noqa: D103
    readline.set_completer(None)
    readline.set_completer_delims(" \t\n\"'`@$><=;|&{(")
    readline.parse_and_bind("tab: complete")


def readline_library() -> ctypes.CDLL:
    """Load GNU readline with pointer-returning functions typed correctly."""
    library = ctypes.CDLL(readline.__file__)
    library.rl_get_keymap.restype = ctypes.c_void_p
    library.rl_get_keymap_by_name.argtypes = [ctypes.c_char_p]
    library.rl_get_keymap_by_name.restype = ctypes.c_void_p
    library.rl_callback_handler_install.argtypes = [ctypes.c_char_p, READLINE_HANDLER]
    library.rl_callback_handler_install.restype = None
    library.rl_callback_handler_remove.restype = None
    library.rl_callback_read_char.restype = None
    library.rl_replace_line.argtypes = [ctypes.c_char_p, ctypes.c_int]
    library.rl_replace_line.restype = None
    return library


@dataclass
class Entry:  # noqa: D101
    title: str
    body: str
    tool: bool = False
    success: bool | None = None
    expanded: bool = False


@dataclass
class TreeNode:  # noqa: D101
    title: str
    children: list["TreeNode"] | None = None
    expanded: bool = False
    style: int | None = None
    warning: bool = False


def record_event(
    entries: list[Entry],
    kind: str,
    text: str,
    success: bool | None,  # noqa: FBT001
) -> None:
    """Apply an event to either a persistent or an in-memory transcript."""
    if kind == "finish":
        entries[-1].success = success
    elif kind == "output" and entries and entries[-1].tool:
        entry = entries[-1]
        entry.body += ("\n" if entry.body else "") + text
    elif kind == "tool":
        entries.append(Entry(text, "", tool=True))
    else:
        preview = next((line for line in text.splitlines() if line.strip()), "chat")
        entries.append(Entry(preview, text))


class Row(NamedTuple):  # noqa: D101
    owner: int
    line: int
    text: str
    start: int = 0


class Viewer:  # noqa: D101
    def __init__(self, agent: Agent) -> None:  # noqa: D107
        self.agent = agent
        self.lines: list[str] = []
        self.entries: list[Entry] = (
            [replace(entry) for entry in agent.history.entries]
            if agent.history is not None
            else []
        )
        self.selected = 0
        self.top = 0
        self.pattern = ""
        self.direction = 1
        self.match: tuple[int, int, int] | None = None
        self.status = ""
        self.width = 80
        self.height = 23
        self.chat_active = False
        self.worker: threading.Thread | None = None
        self.events: queue.Queue[
            tuple[str, str, bool | None] | BaseException | None
        ] = queue.Queue()
        self.waiting_notice = False
        self.mode = "chat"
        self.overview: list[TreeNode] = []
        self.overview_loaded = False
        self.overview_visible: list[TreeNode] = []
        self.overview_parents: list[int | None] = []

    def refresh_overview(self) -> None:
        """Rebuild the package overview from the current working tree."""
        self.status = ""
        self.overview = self.package_entries()
        self.overview_loaded = True
        self.selected = self.top = 0
        self.match = None

    def ensure_overview(self) -> None:
        """Build the package overview once, on startup or first use."""
        if not self.overview_loaded:
            self.refresh_overview()

    def package_entries(self, *, diff: bool = False) -> list[TreeNode]:
        """Build a collapsible package tree with high-level changes."""
        root = self.agent.cwd
        if (root / ".gitmodules").is_file() and not (root / "packages").is_dir():
            return self.home_package_entries(root, diff=diff)
        packages = root / "packages"
        try:
            current_names = {package.name for package in detect_packages(root)}
        except GitCanonicalError as exc:
            self.status = str(exc)
            return []
        if not packages.is_dir():
            return [TreeNode("packages/ (not found)")]
        names = current_names
        historic = git(
            root,
            ["ls-tree", "-d", "--name-only", "HEAD:packages"],
            check=False,
        )
        has_history = diff or historic.returncode == 0
        if historic.returncode == 0:
            names = current_names | set(historic.stdout.splitlines())
        return [
            entry
            for name in sorted(names)
            if (
                entry := self.package_entry(
                    root,
                    name,
                    diff=has_history,
                    only_changes=diff,
                )
            )
            is not None
        ]

    def package_entry(
        self,
        root: Path,
        name: str,
        *,
        diff: bool,
        only_changes: bool = False,
    ) -> TreeNode | None:
        """Build one package summary or its high-level changes."""
        directory = root / "packages" / name
        filenames = ("default.nix", "main.py", "test_main.py")
        current = package_overview(directory)
        if not diff:
            return TreeNode(
                f"packages/{name}",
                self.summary_tree(current) if current else None,
            )
        previous_files = {}
        for filename in filenames:
            completed = git(
                root,
                ["show", f"HEAD:packages/{name}/{filename}"],
                check=False,
            )
            if completed.returncode == 0:
                previous_files[filename] = completed.stdout
        previous = self.package_summary(name, previous_files)
        if only_changes and previous == current:
            return None
        summary_tree = self.merged_summary_tree(previous, current)
        return TreeNode(f"packages/{name}", summary_tree or None)

    def home_package_entries(self, root: Path, *, diff: bool) -> list[TreeNode]:
        """Build repository summaries beneath their home-repository paths."""
        try:
            repositories = home_repositories(root, require_url=False)
        except GitCanonicalError as exc:
            self.status = str(exc)
            return []
        tree: dict[str, Any] = {}
        for item in repositories:
            relative = item["path"]
            repository = root / relative
            if not (repository / "packages").is_dir():
                continue
            viewer = Viewer(Agent(repository))
            entries = viewer.package_entries(diff=diff)
            if viewer.status:
                self.status = viewer.status
            if not entries:
                continue
            branch = tree
            parts = Path(relative).parts
            for part in parts[:-1]:
                branch = branch.setdefault(part, {})
            branch[parts[-1]] = {"": entries}

        def nodes(branch: dict[str, Any]) -> list[TreeNode]:
            result = []
            for name, children in sorted(branch.items()):
                if name == "":
                    result.extend(children)
                else:
                    result.append(TreeNode(name, nodes(children)))
            return result

        return nodes(tree)

    @staticmethod
    def summary_tree(summary: str) -> list[TreeNode]:
        """Convert the displayed summary into field, argument, and test nodes."""
        lines = summary.splitlines()
        arguments_start = next(
            (index for index, line in enumerate(lines) if line == "Arguments:"),
            len(lines),
        )
        tests_start = next(
            (
                index
                for index, line in enumerate(lines)
                if line == "Tests:" and index > arguments_start
            ),
            len(lines),
        )
        fields = [TreeNode(line) for line in lines[:arguments_start]]
        arguments = [
            TreeNode(line.strip(), warning=line.strip().startswith("(unavailable:"))
            for line in lines[arguments_start + 1 : tests_start]
        ]
        fields.append(TreeNode("Arguments", arguments))
        tests = [
            TreeNode(line.strip(), warning=line.strip().startswith("(unavailable:"))
            for line in lines[tests_start + 1 :]
        ]
        fields.append(TreeNode("Tests", tests))
        return fields

    @classmethod
    def summary_changes(cls, previous: str, current: str) -> list[TreeNode]:
        """Build collapsible field, argument, and test changes."""
        old_lines, new_lines = previous.splitlines(), current.splitlines()
        changes: list[TreeNode] = []
        old_fields = {line.partition(":")[0]: line for line in old_lines if ":" in line}
        new_fields = {line.partition(":")[0]: line for line in new_lines if ":" in line}
        for field in ("Name", "Description", "Help"):
            before, after = old_fields.get(field), new_fields.get(field)
            if before != after:
                if before is not None:
                    changes.append(TreeNode(f"- {before}", style=31))
                if after is not None:
                    changes.append(TreeNode(f"+ {after}", style=32))
        old_arguments = cls.summary_group(previous, "Arguments")
        new_arguments = cls.summary_group(current, "Arguments")
        argument_changes = [
            TreeNode(f"- {argument}", style=31)
            for argument in old_arguments
            if argument not in new_arguments
        ]
        argument_changes.extend(
            TreeNode(f"+ {argument}", style=32)
            for argument in new_arguments
            if argument not in old_arguments
        )
        if argument_changes:
            changes.append(TreeNode("Arguments", argument_changes))
        old_tests = cls.summary_group(previous, "Tests")
        new_tests = cls.summary_group(current, "Tests")
        test_changes = [
            TreeNode(f"- {name}", style=31)
            for name in old_tests
            if name not in new_tests
        ]
        test_changes.extend(
            TreeNode(f"+ {name}", style=32)
            for name in new_tests
            if name not in old_tests
        )
        if test_changes:
            changes.append(TreeNode("Tests", test_changes))
        return changes

    @classmethod
    def merged_summary_tree(cls, previous: str, current: str) -> list[TreeNode]:
        """Show summary changes inline at their existing field and group positions."""
        old_lines, new_lines = previous.splitlines(), current.splitlines()
        old_fields = {line.partition(":")[0]: line for line in old_lines if ":" in line}
        new_fields = {line.partition(":")[0]: line for line in new_lines if ":" in line}
        result = []
        for field in ("Name", "Description", "Help"):
            before, after = old_fields.get(field), new_fields.get(field)
            if before == after:
                if after is not None:
                    result.append(TreeNode(after))
            else:
                if before is not None:
                    result.append(TreeNode(f"- {before}", style=31))
                if after is not None:
                    result.append(TreeNode(f"+ {after}", style=32))
        for group in ("Arguments", "Tests"):
            old_entries = cls.summary_group(previous, group)
            new_entries = cls.summary_group(current, group)
            children = [
                TreeNode(f"- {item}", style=31)
                for item in old_entries
                if item not in new_entries
            ]
            children.extend(
                TreeNode(
                    item if item in old_entries else f"+ {item}",
                    style=None if item in old_entries else 32,
                    warning=item.startswith("(unavailable:"),
                )
                for item in new_entries
            )
            if children or f"{group}:" in current:
                result.append(TreeNode(group, children))
        return result

    @staticmethod
    def summary_group(summary: str, name: str) -> list[str]:
        """Return indented entries in a named summary group."""
        lines = summary.splitlines()
        start = next(
            (index for index, line in enumerate(lines) if line == f"{name}:"),
            len(lines),
        )
        if start == len(lines):
            return []
        entries = []
        for line in lines[start + 1 :]:
            if line and not line.startswith("  "):
                break
            if line.startswith("  "):
                entries.append(line.strip())
        return entries

    @staticmethod
    def package_summary(name: str, files: dict[str, str]) -> str:
        """Render the user-facing package fields from source file contents."""
        return source_package_overview(name, files) if files else ""

    def event(self, kind: str, text: str, success: bool | None) -> None:  # noqa: D102, FBT001
        follow = (
            self.chat_active
            and self.selected == max(0, len(self.entries) - 1)
            and self.match is None
            and self.top >= max(0, len(self.rows(self.width)) - self.height)
        )
        record_event(self.entries, kind, text, success)
        if follow:
            self.selected = max(0, len(self.entries) - 1)
            self.top = max(0, len(self.rows(self.width)) - self.height)

    def enqueue(self, kind: str, text: str, success: bool | None) -> None:  # noqa: FBT001
        """Transfer worker events without touching terminal-owned state."""
        self.events.put((kind, text, success))

    def start_turn(self, prompt: str) -> None:
        """Start one turn; all agent and history mutations belong to its worker."""
        if self.worker is not None:
            msg = "A turn is already running"
            raise RuntimeError(msg)
        self.agent.cancel = threading.Event()
        self.lines.append("> " + prompt)
        if self.agent.history is None:
            self.event("chat", "user> " + prompt, None)
        self.selected = max(0, len(self.entries) - 1)
        self.match = None
        self.top = max(0, len(self.rows(self.width)) - self.height)

        def turn() -> None:
            try:
                try:
                    self.agent.turn(prompt)
                except (AgentError, KeyboardInterrupt) as exc:
                    self.agent.emit(
                        f"\n{str(exc) or 'Cancelled'}. Completed tool effects remain; "
                        "incomplete turn history discarded.",
                    )
            except BaseException as exc:  # noqa: BLE001
                self.events.put(exc)
            finally:
                self.events.put(None)

        self.worker = threading.Thread(target=turn)
        self.worker.start()

    def poll(self) -> bool:
        """Apply queued events on the UI thread and reap finished turns."""
        changed = False
        failure: BaseException | None = None
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            changed = True
            if event is None:
                if self.worker is not None:
                    self.worker.join()
                self.worker = None
                self.agent.cancel = None
                self.waiting_notice = False
            elif isinstance(event, BaseException):
                failure = event
            elif event[0] == "line":
                self.lines.append(event[1])
            else:
                self.event(*event)
        if failure is not None:
            raise failure
        return changed

    def stop_turn(self) -> None:
        """Cancel and join before restoring callbacks or releasing history."""
        if self.worker is not None:
            if self.agent.cancel is not None:
                self.agent.cancel.set()
            while self.worker.is_alive():
                with contextlib.suppress(KeyboardInterrupt):
                    self.worker.join(timeout=0.1)
        self.poll()

    def waiting(self) -> str:
        """Return an ephemeral answer placeholder, never a transcript entry."""
        if self.worker is None:
            return ""
        dots = "." * (1 + int(time.monotonic() * 5) % 3)
        notice = " Waiting for the current answer" if self.waiting_notice else ""
        return f"assistant> {dots}{notice}"

    @staticmethod
    def safe(text: str) -> str:  # noqa: D102
        return "".join(
            char if char.isprintable() else "?" for char in text.expandtabs(4)
        )

    @staticmethod
    def wrap(text: str, width: int) -> list[tuple[int, str]]:
        """Wrap by terminal cells, retaining character offsets for search."""
        parts = []
        start = 0
        used = 0
        content = ""
        for offset, char in enumerate(text):
            cells = (
                0
                if unicodedata.combining(char)
                else (2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1)
            )
            if used + cells > width and content:
                parts.append((start, content))
                start, used, content = offset, 0, ""
            content += "?" if cells > width else char
            used += min(cells, width)
        parts.append((start, content))
        return parts

    def rows(self, width: int) -> list[Row]:  # noqa: D102
        width = max(1, width)
        if self.mode != "chat":
            return self.overview_rows(width)
        rows = []
        indent = " " * min(2, width - 1)
        for index, entry in enumerate(self.entries):
            marker = "[-]" if entry.expanded else "[+]"
            status = ""
            if entry.tool:
                status = {True: " [OK]", False: " [FAILED]", None: " [pending]"}[
                    entry.success
                ]
            title = f"{marker}{status} {self.safe(entry.title)}"
            rows.append(Row(index, -1, self.wrap(title, width)[0][1]))
            if entry.expanded:
                for line, content in enumerate(entry.body.splitlines() or [""]):
                    rows.extend(
                        Row(index, line, indent + part, start)
                        for start, part in self.wrap(
                            self.safe(content),
                            width - len(indent),
                        )
                    )
        return rows

    def overview_rows(self, width: int) -> list[Row]:
        """Flatten the expanded package and test groups into visible tree rows."""
        rows: list[Row] = []
        self.overview_visible = []
        self.overview_parents = []

        def visit(nodes: list[TreeNode], depth: int, parent: int | None) -> None:
            for node in nodes:
                owner = len(self.overview_visible)
                self.overview_visible.append(node)
                self.overview_parents.append(parent)
                marker = "[-]" if node.expanded else "[+]" if node.children else "   "
                prefix = "  " * depth + marker + " "
                continuation = " " * len(prefix)
                title = node.title + (" [!]" if self.has_warning(node) else "")
                wrapped = self.wrap(self.safe(title), max(1, width - len(prefix)))
                rows.append(Row(owner, -1, prefix + wrapped[0][1]))
                rows.extend(
                    Row(owner, -1, continuation + part) for _start, part in wrapped[1:]
                )
                if node.expanded and node.children:
                    visit(node.children, depth + 1, owner)

        visit(self.overview, 0, None)
        return rows

    def matched(self, row: Row) -> bool:
        """Identify the wrapped row containing the current search hit."""
        if self.match is None or self.match[:2] != (row.owner, row.line):
            return False
        if row.line == -1:
            return True
        indent = min(2, self.width - 1)
        end = row.start + len(row.text) - indent
        content = self.entries[row.owner].body.splitlines()[row.line]
        return row.start <= self.match[2] < end or (
            self.match[2] == end and end == len(self.safe(content))
        )

    def styles(self, row: Row) -> list[int]:
        """Share terminal styling between the readline and curses displays."""
        if self.mode != "chat":
            node = self.overview_visible[row.owner]
            style = self.tree_style(node)
            if self.has_warning(node):
                style = 33
            styles = [style] if style is not None else []
            if row.owner == self.selected:
                styles.append(7)
            return styles
        styles = []
        entry = self.entries[row.owner]
        if row.line == -1:
            if entry.tool and entry.success is not None:
                styles.append(32 if entry.success else 31)
            if row.owner == self.selected:
                styles.append(7)
        if self.matched(row):
            styles.extend((1, 4))
        return styles

    @classmethod
    def has_warning(cls, node: TreeNode) -> bool:
        """Show diagnostics even when their parent groups are collapsed."""
        return node.warning or any(
            cls.has_warning(child) for child in node.children or []
        )

    @classmethod
    def tree_style(cls, node: TreeNode) -> int | None:
        """Return a node's own change color or the aggregate color of its children."""
        additions = node.style in (32, 33)
        removals = node.style in (31, 33)
        for child in node.children or []:
            style = cls.tree_style(child)
            additions |= style in (32, 33)
            removals |= style in (31, 33)
        if additions and removals:
            return 33
        if additions:
            return 32
        if removals:
            return 31
        return node.style

    def reveal(self, position: int) -> None:
        """Keep a target row visible without jumping or overscrolling."""
        if position < self.top:
            self.top = position
        elif position >= self.top + self.height:
            self.top = position - self.height + 1
        self.top = max(
            0,
            min(self.top, max(0, len(self.rows(self.width)) - self.height)),
        )

    def render_chat(self, *, follow: bool = False) -> None:
        """Paint the shared tree above native readline's input row."""
        if not sys.stdout.isatty():
            return
        follow = follow or (
            self.selected == max(0, len(self.entries) - 1)
            and self.match is None
            and self.top >= max(0, len(self.rows(self.width)) - self.height)
        )
        size = shutil.get_terminal_size()
        waiting = self.waiting()
        self.width, self.height = (
            max(1, size.columns - 1),
            max(
                1,
                size.lines - 1 - bool(waiting),
            ),
        )
        rows = self.rows(self.width)
        if follow:
            self.top = max(0, len(rows) - self.height)
        self.top = min(self.top, max(0, len(rows) - self.height))
        output = ["\x1b[0m\x1b[H\x1b[2J"]
        for y, row in enumerate(rows[self.top : self.top + self.height], 1):
            styles = ";".join(map(str, self.styles(row))) or "0"
            output.append(f"\x1b[{y};1H\x1b[{styles}m{row.text}\x1b[0m")
        if waiting and size.lines > 1:
            output.append(f"\x1b[{size.lines - 1};1H{waiting[: self.width]}")
        output.append(f"\x1b[{size.lines};1H")
        sys.stdout.write("".join(output))
        sys.stdout.flush()

    def search(self, direction: int, *, repeat: bool = False) -> None:  # noqa: D102
        try:
            pattern = re.compile(self.pattern)
        except re.error as exc:
            self.match = None
            self.status = f"Invalid pattern: {exc}"
            return
        if self.mode != "chat":
            tree_candidates = [
                index
                for index, node in enumerate(self.overview_visible)
                if pattern.search(self.safe(node.title))
            ]
            if not tree_candidates:
                self.match = None
                self.status = "Pattern not found"
                return
            ordered_tree = tree_candidates if direction > 0 else tree_candidates[::-1]
            selected_index = next(
                (
                    index
                    for index in ordered_tree
                    if (
                        index > self.selected
                        if direction > 0
                        else index < self.selected
                    )
                ),
                ordered_tree[0],
            )
            self.selected = selected_index
            self.status = ""
            self.reveal(
                next(
                    i
                    for i, row in enumerate(self.rows(self.width))
                    if row.owner == self.selected
                ),
            )
            return
        candidates = [
            (index, line, found.start())
            for index, entry in enumerate(self.entries)
            for line, content in [
                (-1, entry.title),
                *enumerate(entry.body.splitlines()),
            ]
            for found in pattern.finditer(self.safe(content))
        ]
        if not candidates:
            self.match = None
            self.status = "Pattern not found"
            return
        anchor = (
            self.match
            if repeat and self.match is not None
            else (
                self.selected,
                -2
                if direction > 0
                else len(self.entries[self.selected].body.splitlines()),
                -1,
            )
        )
        ordered = candidates if direction > 0 else candidates[::-1]
        match = next(
            (
                item
                for item in ordered
                if (item > anchor if direction > 0 else item < anchor)
            ),
            ordered[0],
        )
        self.match = match
        self.selected = match[0]
        self.entries[self.selected].expanded = True
        self.status = ""
        self.reveal(
            next(i for i, row in enumerate(self.rows(self.width)) if self.matched(row)),
        )

    def navigate(  # noqa: D102
        self,
        key: str | int,
        height: int,
        rows: list[Row],
    ) -> None:
        self.height = height
        if not rows:
            return
        if self.mode != "chat":
            self.navigate_overview(key, height, rows)
            return
        if key in ("j", "k"):
            self.selected = max(
                0,
                min(len(self.entries) - 1, self.selected + (1 if key == "j" else -1)),
            )
            self.reveal(
                next(i for i, row in enumerate(rows) if row.owner == self.selected),
            )
            self.match = None
            return
        if key in ("h", "l"):
            self.match = None
            self.entries[self.selected].expanded = key == "l"
            self.reveal(
                next(
                    i
                    for i, row in enumerate(self.rows(self.width))
                    if row.owner == self.selected
                ),
            )
            return
        self.navigate_page(key, height, rows)

    def navigate_overview(
        self,
        key: str | int,
        height: int,
        rows: list[Row],
    ) -> None:
        """Navigate and expand package and test groups in an overview tree."""
        if not self.overview_visible:
            return
        if key in ("j", "k"):
            self.selected = max(
                0,
                min(
                    len(self.overview_visible) - 1,
                    self.selected + (1 if key == "j" else -1),
                ),
            )
            self.match = None
            self.reveal(
                next(i for i, row in enumerate(rows) if row.owner == self.selected),
            )
            return
        if key in ("h", "l"):
            node = self.overview_visible[self.selected]
            if key == "l" and node.children:
                node.expanded = True
            elif key == "h":
                self.collapse_overview_node()
            self.match = None
            refreshed = self.overview_rows(self.width)
            self.reveal(
                next(
                    (
                        i
                        for i, row in enumerate(refreshed)
                        if row.owner == self.selected
                    ),
                    0,
                ),
            )
            return
        self.navigate_page(key, height, rows)

    def collapse_overview_node(self) -> None:
        """Collapse the selected node or its nearest expanded ancestor."""
        node = self.overview_visible[self.selected]
        if node.children and node.expanded:
            node.expanded = False
            return
        parent = self.overview_parents[self.selected]
        while parent is not None and not self.overview_visible[parent].expanded:
            parent = self.overview_parents[parent]
        if parent is not None:
            self.overview_visible[parent].expanded = False
            self.selected = parent

    def navigate_page(self, key: str | int, height: int, rows: list[Row]) -> None:
        """Handle page movement shared by chat and overview views."""
        offsets = {
            " ": height,
            "f": height,
            curses.KEY_NPAGE: height,
            "b": -height,
            curses.KEY_PPAGE: -height,
            "d": max(1, height // 2),
            "u": -max(1, height // 2),
            curses.KEY_DOWN: 1,
            "\n": 1,
            curses.KEY_UP: -1,
        }
        if key in ("g", "G"):
            self.top = 0 if key == "g" else max(0, len(rows) - height)
            cursor_row = (
                self.top if key == "g" else min(len(rows) - 1, self.top + height - 1)
            )
            self.selected = rows[cursor_row].owner
            self.match = None
        elif key in offsets:
            self.top = max(0, min(max(0, len(rows) - height), self.top + offsets[key]))
            self.selected = rows[self.top].owner
            self.match = None

    def view(self) -> None:  # noqa: D102
        try:
            curses.wrapper(self.screen)
        except curses.error as exc:
            print(f"Viewer error: {exc}", flush=True)  # noqa: T201

    def screen(self, screen: curses.window) -> None:  # noqa: C901, D102, PLR0912, PLR0915
        screen.timeout(100)
        with contextlib.suppress(curses.error):
            curses.curs_set(0)
        colors = curses.has_colors()
        if colors:
            curses.start_color()
            background = curses.COLOR_BLACK
            with contextlib.suppress(curses.error):
                curses.use_default_colors()
                background = -1
            curses.init_pair(1, curses.COLOR_GREEN, background)
            curses.init_pair(2, curses.COLOR_RED, background)
            curses.init_pair(3, curses.COLOR_YELLOW, background)
        editing: str | None = None
        query = ""
        prefix = ""
        while True:
            self.poll()
            waiting = self.waiting()
            height, width = screen.getmaxyx()
            self.width = max(1, width - 1)
            page = self.height = max(1, height - 1 - bool(waiting))
            rows = self.rows(self.width)
            self.top = max(0, min(self.top, max(0, len(rows) - page)))
            screen.erase()
            attributes = {
                1: curses.A_BOLD,
                4: curses.A_UNDERLINE,
                7: curses.A_REVERSE,
                31: curses.color_pair(2) if colors else 0,
                32: curses.color_pair(1) if colors else 0,
                33: curses.color_pair(3) if colors else 0,
            }
            for y, row in enumerate(rows[self.top : self.top + page]):
                attr = curses.A_NORMAL
                for style in self.styles(row):
                    attr |= attributes[style]
                with contextlib.suppress(curses.error):
                    screen.addstr(y, 0, row.text, attr)
            if waiting and height > 1:
                with contextlib.suppress(curses.error):
                    screen.addnstr(height - 2, 0, waiting, self.width)
            footer = (
                editing + query
                if editing
                else self.status
                or (
                    f"{'(END) ' if self.top + page >= len(rows) else ''}"
                    f"{self.mode} | L view  "
                    f"j/k {'parent' if self.mode == 'chat' else 'node'}  "
                    f"l/h open/close  {'r refresh  ' if self.mode != 'chat' else ''}"
                    "space/b page  "
                    "/? search  n/N next  q quit"
                )
            )
            with contextlib.suppress(curses.error):
                screen.addnstr(height - 1, 0, footer, self.width, curses.A_REVERSE)
            screen.refresh()
            try:
                key = screen.get_wch()
            except curses.error:
                continue
            if key == curses.KEY_RESIZE:
                continue
            if editing:
                if key == "\x1b":
                    editing = None
                elif key in ("\n", "\r", curses.KEY_ENTER):
                    self.pattern = query or self.pattern
                    self.direction = 1 if editing == "/" else -1
                    editing = None
                    if self.pattern and (
                        self.entries if self.mode == "chat" else self.overview
                    ):
                        self.search(self.direction)
                elif key in ("\x7f", "\b", curses.KEY_BACKSPACE):
                    query = query[:-1]
                elif isinstance(key, str) and key.isprintable():
                    query += key
                continue
            if key == "q" or (prefix == "Z" and key == "Z"):
                return
            if key == "L":
                modes = ("chat", "high-level")
                next_mode = modes[(modes.index(self.mode) + 1) % len(modes)]
                if self.mode == "chat":
                    self.chat_entries = self.entries
                if next_mode == "chat":
                    self.entries = self.chat_entries
                else:
                    self.ensure_overview()
                self.mode = next_mode
                self.selected = self.top = 0
                self.match = None
                continue
            if key == "r" and self.mode != "chat":
                self.refresh_overview()
                continue
            prefix = key if key in (":", "Z") else ""
            self.status = ""
            if key in ("/", "?"):
                editing, query = str(key), ""
            elif (
                key in ("n", "N")
                and self.pattern
                and (self.entries if self.mode == "chat" else self.overview)
            ):
                self.search(self.direction * (1 if key == "n" else -1), repeat=True)
            else:
                self.navigate(key, page, rows)

    def read_chat(self) -> str:  # noqa: C901, D102, PLR0915
        library = readline_library()
        slot = ctypes.c_void_p.in_dll(library, "rl_getc_function")
        previous = slot.value
        if previous is None:
            msg = "GNU readline has no character input function"
            raise RuntimeError(msg)
        original = READLINE_GETTER(previous)
        vi_insert = library.rl_get_keymap_by_name(b"vi-insert")
        catch_signals = ctypes.c_int.in_dll(library, "rl_catch_signals")
        catch_resize = ctypes.c_int.in_dll(library, "rl_catch_sigwinch")
        previous_signals, previous_catch_resize = (
            catch_signals.value,
            catch_resize.value,
        )
        failure: BaseException | None = None
        finished = False
        resize_pending = False
        prompt: str | None = None
        allocator = ctypes.CDLL(None)
        allocator.free.argtypes = [ctypes.c_void_p]
        allocator.free.restype = None
        previous_resize = signal.getsignal(signal.SIGWINCH)
        terminal = termios.tcgetattr(sys.stdin) if sys.stdin.isatty() else None

        def restore_terminal() -> None:
            if terminal is not None:
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, terminal)

        def accept_line(address: int | None) -> None:
            nonlocal finished, prompt, failure
            finished = True
            prompt = None
            if address is not None:
                try:
                    prompt = ctypes.string_at(address).decode(
                        sys.stdin.encoding or "utf-8",
                        sys.stdin.errors or "strict",
                    )
                except BaseException as exc:  # noqa: BLE001
                    failure = exc
                finally:
                    allocator.free(address)

        def redisplay() -> None:
            self.render_chat()
            library.rl_on_new_line()
            library.rl_forced_update_display()

        def resize_chat(_signum: int, _frame: object) -> None:
            nonlocal resize_pending
            resize_pending = True

        def get_character(stream: int) -> int:
            nonlocal failure
            try:
                select.select([sys.stdin], [], [])
                char = original(stream)
                if char in (ord("\n"), ord("\r")) and self.worker is not None:
                    self.waiting_notice = True
                    redisplay()
                    return 0
                if char != ord("\x1b") or library.rl_get_keymap() == vi_insert:
                    return int(char)
                if select.select([sys.stdin], [], [], 0.15)[0]:
                    return int(char)
                library.rl_deprep_terminal()
                signal.signal(signal.SIGWINCH, previous_resize)
                try:
                    self.view()
                finally:
                    signal.signal(signal.SIGWINCH, resize_chat)
                    restore_terminal()
                    library.rl_prep_terminal(1)
                    redisplay()
            except BaseException as exc:  # noqa: BLE001
                failure = exc
                ctypes.c_int.in_dll(library, "rl_done").value = 1
                return ord("\n")
            return 0

        callback = READLINE_GETTER(get_character)
        slot.value = ctypes.cast(callback, ctypes.c_void_p).value
        catch_signals.value = catch_resize.value = 0
        signal.signal(signal.SIGWINCH, resize_chat)
        handler = READLINE_HANDLER(accept_line)
        try:
            library.rl_callback_handler_install(b"> ", handler)
            while not finished:
                changed = self.poll()
                if resize_pending:
                    resize_pending = False
                    library.rl_resize_terminal()
                    changed = True
                if changed or self.worker is not None:
                    redisplay()
                if not select.select([sys.stdin], [], [], 0.1)[0]:
                    continue
                library.rl_callback_read_char()
                if failure is not None:
                    raise failure
                if finished and prompt is not None and self.worker is not None:
                    library.rl_callback_handler_remove()
                    restore_terminal()
                    library.rl_callback_handler_install(b"> ", handler)
                    library.rl_replace_line(prompt.encode(sys.stdin.encoding), 0)
                    ctypes.c_int.in_dll(library, "rl_point").value = len(
                        prompt.encode(sys.stdin.encoding),
                    )
                    finished = False
                    self.waiting_notice = True
                    redisplay()
            if prompt is None:
                raise EOFError
            if prompt and prompt != readline.get_history_item(
                readline.get_current_history_length(),
            ):
                readline.add_history(prompt)
            return prompt
        finally:
            library.rl_callback_handler_remove()
            restore_terminal()
            catch_signals.value, catch_resize.value = (
                previous_signals,
                previous_catch_resize,
            )
            signal.signal(signal.SIGWINCH, previous_resize)
            slot.value = previous

    def run(self) -> None:  # noqa: D102
        self.ensure_overview()
        configure_filename_completion()
        previous = self.agent.output
        previous_event = self.agent.event
        self.agent.output = lambda text: self.enqueue("line", text, None)
        self.chat_active = True
        self.agent.event = self.enqueue
        try:
            while True:
                try:
                    self.poll()
                    self.render_chat()
                    prompt = self.read_chat()
                    if prompt.strip():
                        self.start_turn(prompt)
                except EOFError:  # noqa: PERF203
                    print()  # noqa: T201
                    return
                except (AgentError, KeyboardInterrupt) as exc:
                    if self.worker is not None:
                        self.stop_turn()
                    else:
                        self.agent.emit(
                            f"\n{str(exc) or 'Cancelled'}. "
                            "Completed tool effects remain; "
                            "incomplete turn history discarded.",
                        )
        finally:
            try:
                self.stop_turn()
            finally:
                self.chat_active = False
                self.agent.output = previous
                self.agent.event = previous_event


def main(argv: list[str] | None = None) -> None:  # noqa: D103
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=(
            "Prompt and chat history resume automatically for the resolved startup "
            "directory, including --prompt runs. Stored under "
            "$XDG_STATE_HOME/canonical_agent (default: "
            "~/.local/state/canonical_agent); existing coding_agent history is reused."
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "-p",
        "--prompt",
        help="Run a single prompt non-interactively and exit",
    )
    mode.add_argument(
        "--clear-history",
        action="store_true",
        help="Clear this directory's prompt and chat history and exit",
    )
    args = parser.parse_args(argv)
    if args.prompt is not None and not args.prompt.strip():
        parser.error("--prompt must not be empty or whitespace")
    try:
        with History(Path.cwd()) as history:
            if args.clear_history:
                history.clear()
                return
            history.load()
            agent = Agent(history=history)
            readline.clear_history()
            for prompt in history.prompts:
                readline.add_history(prompt)
            run_cli(agent, args.prompt)
    except HistoryError as exc:
        print(f"Error: {exc}", file=sys.stderr)  # noqa: T201
        raise SystemExit(1) from exc


def run_cli(agent: Agent, prompt: str | None) -> None:  # noqa: C901, PLR0912
    """Run either CLI mode with the directory's history already loaded."""
    if prompt is not None:
        try:
            print(agent.turn(prompt))  # noqa: T201
        except AgentError as exc:
            if agent.history is not None:
                agent.history.event("chat", f"Error: {exc}", None)
            print(f"Error: {exc}", file=sys.stderr)  # noqa: T201
            raise SystemExit(1) from exc
        except KeyboardInterrupt as exc:
            if agent.history is not None:
                agent.history.event(
                    "chat",
                    "Cancelled. Incomplete turn history discarded.",
                    None,
                )
            print("\nCancelled. Completed tool effects remain.", file=sys.stderr)  # noqa: T201
            raise SystemExit(130) from exc
        return
    if sys.stdin.isatty() and sys.stdout.isatty():
        Viewer(agent).run()
        return
    configure_filename_completion()
    while True:
        try:
            prompt = input("> ")
            if prompt.strip():
                print(agent.turn(prompt))  # noqa: T201
        except EOFError:  # noqa: PERF203
            print()  # noqa: T201
            return
        except KeyboardInterrupt:
            if agent.history is not None:
                agent.history.event(
                    "chat",
                    "Cancelled. Incomplete turn history discarded.",
                    None,
                )
            print(  # noqa: T201
                "\nCancelled. Completed tool effects remain; "
                "incomplete turn history discarded.",
            )
        except AgentError as exc:
            if agent.history is not None:
                agent.history.event("chat", f"Error: {exc}", None)
            print(  # noqa: T201
                f"Error: {exc}. Completed tool effects remain; "
                "incomplete turn history discarded.",
            )


if __name__ == "__main__":
    main()

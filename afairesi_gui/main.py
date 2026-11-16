#!/usr/bin/env python3
# Copyright (c) 2026- Paschalis Bizopoulos
"""Serve the local Afairesi workspace and its repository-scoped operations."""

from __future__ import annotations

import argparse
import base64
import codecs
import contextlib
import fcntl
import hashlib
import json
import mimetypes
import os
import pty
import re
import select
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import termios
import threading
import time
import uuid
import webbrowser
from copy import deepcopy
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import parse_qs, urlsplit

import afairesi

if TYPE_CHECKING:
    import io
    from collections.abc import Callable

MAX_LOG = 100_000
MAX_BODY = 65_536
MAX_JOBS = 4
BUNDLED_CODEX = "codex"
SOURCE_REVISION = ""
TERMINAL_ASSETS = ""
MAX_TERMINAL_OUTPUT = 2_000_000
ASSETS = {
    "/": "index.html",
    "/app.js": "app.js",
    "/terminal.js": "terminal.js",
    "/arguments.js": "arguments.js",
    "/differences.js": "differences.js",
    "/style.css": "style.css",
}

TERMINAL_FILES = {"xterm.js", "xterm.css", "addon-fit.js", "addon-web-links.js"}


def command(arguments: list[str], root: Path, *, input_text: str | None = None) -> str:
    """Read a command result without a shell or optional Git index writes."""
    result = subprocess.run(  # noqa: S603 - Argument vectors are constructed here, never shell source.
        arguments,
        cwd=root,
        capture_output=True,
        text=True,
        input=input_text,
        check=False,
        timeout=30,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
    )
    if result.returncode:
        raise ValueError(result.stderr.strip() or "Command failed")
    return result.stdout


def git(root: Path, *arguments: str) -> str:
    """Read Git data in the selected working tree."""
    return command(["git", *arguments], root)


def interface_fingerprint(root: Path) -> str:
    """Track HEAD, index entries, and source metadata without inspecting sources."""
    entries = git(root, "ls-files", "--stage", "-z")
    digest = hashlib.sha256()
    digest.update(git(root, "rev-parse", "--revs-only", "HEAD").encode())
    digest.update(entries.encode())
    paths: set[Path] = set()
    for entry in entries.split("\0"):
        if not entry:
            continue
        metadata, relative = entry.split("\t", 1)
        if metadata.startswith("160000 "):
            continue
        path = Path(relative)
        paths.update((path, *path.parents))
    for tracked in sorted(paths):
        digest.update(str(tracked).encode())
        try:
            stat = (root / tracked).lstat()
            digest.update(
                repr(
                    (
                        stat.st_dev,
                        stat.st_ino,
                        stat.st_mode,
                        stat.st_size,
                        stat.st_mtime_ns,
                        stat.st_ctime_ns,
                    )
                ).encode()
            )
        except (FileNotFoundError, NotADirectoryError):
            digest.update(b"missing")
    return digest.hexdigest()


class Workspace:
    """Use only the root repository's direct, tracked submodule registrations."""

    def __init__(self, home: Path) -> None:
        """Set the home repository without scanning its filesystem."""
        self.home = home.resolve()
        self._diff_cache: dict[Path, tuple[str, dict[str, Any]]] = {}
        self._diff_locks: dict[Path, threading.Lock] = {}
        self._cache_lock = threading.Lock()

    def repositories(self) -> list[dict[str, Any]]:
        """Join .gitmodules paths with gitlinks in the current home index."""
        root = git(self.home, "rev-parse", "--show-toplevel").strip()
        if Path(root).resolve() != self.home:
            message = "The selected home must be a Git repository root."
            raise ValueError(message)
        modules = self.home / ".gitmodules"
        if not modules.is_file():
            return []
        configured = git(
            self.home, "config", "--null", "--file", str(modules), "--list"
        )
        paths = {
            entry.partition("\n")[2]
            for entry in configured.split("\0")
            if entry.partition("\n")[0].startswith("submodule.")
            and entry.partition("\n")[0].endswith(".path")
        }
        result = []
        for entry in git(self.home, "ls-files", "--stage", "-z").split("\0"):
            if not entry:
                continue
            metadata, path = entry.split("\t", 1)
            mode, revision, stage = metadata.split()
            if mode != "160000" or stage != "0" or path not in paths:
                continue
            location = (self.home / path).resolve()
            if not location.is_relative_to(self.home) or location == self.home:
                continue
            initialized = (location / ".git").exists()
            result.append(
                {
                    "id": path,
                    "name": Path(path).name,
                    "path": path,
                    "revision": revision,
                    "initialized": initialized,
                }
            )
        return sorted(result, key=lambda item: item["id"])

    def repository(self, identifier: str) -> tuple[Path, dict[str, Any]]:
        """Resolve a request only through a currently registered submodule."""
        for repository in self.repositories():
            if repository["id"] == identifier:
                root = (self.home / identifier).resolve()
                if not repository["initialized"]:
                    message = "This submodule is not initialized."
                    raise ValueError(message)
                actual = git(root, "rev-parse", "--show-toplevel").strip()
                if Path(actual).resolve() != root:
                    message = "The registered path is not a repository checkout."
                    raise ValueError(message)
                return root, repository
        message = "Repository is not a direct registered home submodule."
        raise ValueError(message)

    def inspect(self, identifier: str) -> dict[str, Any]:
        """Attach Git state to the existing Afairesi resource report."""
        root, repository = self.repository(identifier)
        revision = git(root, "rev-parse", "HEAD").strip()
        branch = git(root, "rev-parse", "--abbrev-ref", "HEAD").strip()
        status = git(root, "status", "--porcelain", "--untracked-files=normal")
        error = None
        try:
            resources = afairesi.inspect_repository(root)
        except (afairesi.CommandError, OSError, SyntaxError, ValueError) as failure:
            resources = {}
            error = str(failure)
        return {
            **repository,
            "branch": branch,
            "head": revision,
            "dirty": bool(status),
            "pin_changed": revision != repository["revision"],
            "resources": resources,
            "inspection_error": error,
        }

    def changes(self, identifier: str) -> dict[str, Any]:
        """Read staged and unstaged interface and file differences."""
        root, _repository = self.repository(identifier)
        options = ["--no-ext-diff", "--no-textconv", "--no-color"]
        return {
            "interfaces": self.interface_changes(identifier),
            "working": git(root, "diff", *options)[-MAX_LOG:],
            "staged": git(root, "diff", "--cached", *options)[-MAX_LOG:],
            "status": git(root, "status", "--short")[-MAX_LOG:],
        }

    def interface_changes(self, identifier: str) -> dict[str, Any]:
        """Reuse CLI diffs only while the tracked repository state is unchanged."""
        root, _repository = self.repository(identifier)
        with self._cache_lock:
            lock = self._diff_locks.setdefault(root, threading.Lock())
        with lock:
            fingerprint = interface_fingerprint(root)
            cached = self._diff_cache.get(root)
            if cached and cached[0] == fingerprint:
                return deepcopy(cached[1])
            interfaces = self._read_interface_changes(root)
            if (
                not any("error" in result for result in interfaces.values())
                and interface_fingerprint(root) == fingerprint
            ):
                self._diff_cache[root] = (fingerprint, deepcopy(interfaces))
            return interfaces

    @staticmethod
    def _read_interface_changes(root: Path) -> dict[str, Any]:
        """Run the packaged CLI for working-tree and staged interfaces."""
        interfaces = {}
        for name, arguments in (("working", []), ("staged", ["--cached"])):
            try:
                difference = command(["afairesi", "diff", *arguments], root)
                interfaces[name] = {
                    "diff": difference,
                    "patch": json.loads(
                        command(["jd", "-t", "jd2patch"], root, input_text=difference)
                    )
                    if difference
                    else [],
                }
                if name == "working":
                    interfaces[name]["resources"] = afairesi._inspection_git_report(  # noqa: SLF001 - Paired packages share the CLI's snapshot semantics.
                        root, working_tree=True
                    )
            except (
                afairesi.CommandError,
                OSError,
                SyntaxError,
                ValueError,
                subprocess.SubprocessError,
            ) as error:
                interfaces[name] = {"error": str(error)}
        return interfaces

    def actions(self, identifier: str, resource: str) -> dict[str, list[str]]:
        """Resolve available actions from evaluated Nix outputs on demand."""
        root, _repository = self.repository(identifier)
        report = afairesi.inspect_repository(root)
        if resource not in report:
            message = "Resource is not present in the repository inspection."
            raise ValueError(message)
        return self._actions(root, [resource])[resource]

    def action_map(self, identifier: str) -> dict[str, dict[str, list[str]]]:
        """Evaluate capabilities together for every inspected repository resource."""
        root, _repository = self.repository(identifier)
        resources = afairesi.inspect_repository(root)
        return self._actions(root, list(resources)) if resources else {}

    def _actions(
        self, root: Path, resources: list[str]
    ) -> dict[str, dict[str, list[str]]]:
        """Share one Nix evaluation across the visible resource boxes."""
        system = command(
            ["nix", "eval", "--raw", "--impure", "--expr", "builtins.currentSystem"],
            root,
        ).strip()
        names = " ".join(json.dumps(resource) for resource in resources)
        expression = (
            "flake: let "
            f"system = {json.dumps(system)}; names = [ {names} ]; "
            "packages = flake.packages.${system} or {}; "
            "checks = flake.checks.${system} or {}; "
            "apps = flake.apps.${system} or {}; "
            "hosts = flake.nixosConfigurations or {}; "
            "in builtins.listToAttrs (map (name: { inherit name; value = { "
            "package = builtins.hasAttr name packages; "
            "host = builtins.hasAttr name hosts; "
            "run = builtins.hasAttr name apps || "
            "(builtins.hasAttr name packages && "
            "(packages.${name}.meta or {}) ? mainProgram); "
            "test = builtins.hasAttr name checks; "
            'hostTest = builtins.hasAttr (name + "Vm") checks; }; }) names)'
        )
        available = json.loads(
            command(
                [
                    "nix",
                    "eval",
                    "--json",
                    "--no-write-lock-file",
                    "--impure",
                    "--expr",
                    f"({expression}) (builtins.getFlake {json.dumps(str(root))})",
                ],
                root,
            )
        )
        return {
            resource: self._action_commands(resource, system, capabilities)
            for resource, capabilities in available.items()
        }

    @staticmethod
    def _action_commands(
        resource: str, system: str, available: dict[str, bool]
    ) -> dict[str, list[str]]:
        """Construct argument vectors from the evaluated capability flags."""
        name = json.dumps(resource)
        actions = {}
        build = [
            "nix",
            "build",
            "--no-link",
            "--print-out-paths",
            "--no-write-lock-file",
        ]
        if available["package"]:
            actions["build"] = [*build, f".#packages.{system}.{name}"]
        elif available["host"]:
            actions["build"] = [
                *build,
                f".#nixosConfigurations.{name}.config.system.build.toplevel",
            ]
        if available["run"]:
            actions["run"] = ["nix", "run", "--no-write-lock-file", f".#{name}"]
        if available["test"]:
            actions["test"] = [*build, f".#checks.{system}.{name}"]
        elif available["hostTest"]:
            actions["test"] = [
                *build,
                f".#checks.{system}.{json.dumps(resource + 'Vm')}",
            ]
        return actions


class Jobs:
    """Track bounded process output and cancellation for resource actions."""

    def __init__(self) -> None:
        """Initialize this server session's job history."""
        self.lock = threading.RLock()
        self.entries: dict[str, dict[str, Any]] = {}
        self.processes: dict[str, subprocess.Popen[str]] = {}

    def start(
        self, root: Path, repository: str, resource: str, action: str, argv: list[str]
    ) -> dict[str, Any]:
        """Launch an explicit argument vector in the selected repository."""
        with self.lock:
            if len(self.processes) >= MAX_JOBS:
                message = "Four jobs are already running. Stop or finish a job first."
                raise ValueError(message)
            process = subprocess.Popen(  # noqa: S603 - Only server-resolved Nix action vectors reach this method.
                argv,
                cwd=root,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                start_new_session=True,
            )
            identifier = uuid.uuid4().hex
            entry = {
                "id": identifier,
                "repository": repository,
                "resource": resource,
                "action": action,
                "command": shlex.join(argv),
                "status": "running",
                "started": time.time(),
                "output": "",
                "exit_code": None,
            }
            self.entries[identifier] = entry
            self.processes[identifier] = process
            for old in list(self.entries)[:-32]:
                if old not in self.processes:
                    del self.entries[old]
            threading.Thread(
                target=self._read, args=(identifier, process), daemon=True
            ).start()
            return deepcopy(entry)

    def _read(self, identifier: str, process: subprocess.Popen[str]) -> None:
        """Stream output and publish the actual process exit status."""
        if process.stdout is not None:
            decoder = codecs.getincrementaldecoder("utf-8")("replace")
            output = cast(
                "io.BufferedReader", cast("io.TextIOWrapper", process.stdout).buffer
            )
            with process.stdout:
                while data := output.read1(4096):
                    chunk = decoder.decode(data)
                    with self.lock:
                        entry = self.entries[identifier]
                        entry["output"] = (entry["output"] + chunk)[-MAX_LOG:]
        code = process.wait()
        with self.lock:
            entry = self.entries[identifier]
            entry["exit_code"] = code
            if entry["status"] != "cancelled":
                entry["status"] = "passed" if code == 0 else "failed"
            self.processes.pop(identifier, None)

    def snapshot(self, repository: str) -> list[dict[str, Any]]:
        """Return a consistent copy of the selected repository's history."""
        with self.lock:
            return deepcopy(
                [
                    item
                    for item in reversed(list(self.entries.values()))
                    if item["repository"] == repository
                ]
            )

    def cancel(self, identifier: str) -> None:
        """Terminate the job's process group, including any running program."""
        with self.lock:
            process = self.processes.get(identifier)
            if process is None:
                return
            self.entries[identifier]["status"] = "cancelled"
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
        threading.Thread(target=self.reap, args=(process,), daemon=True).start()

    @staticmethod
    def reap(process: subprocess.Popen[Any]) -> None:
        """Escalate cancellation when a program ignores termination."""
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)

    def close(self) -> None:
        """Stop processes owned by this server."""
        with self.lock:
            identifiers = list(self.processes)
        for identifier in identifiers:
            self.cancel(identifier)


class Terminal:
    """Own one native Codex session behind a browser terminal."""

    def __init__(
        self, root: Path, executable: str = BUNDLED_CODEX, *, remote: bool = False
    ) -> None:
        """Retain terminal output and process ownership across browser reconnects."""
        self.root = root
        self.executable = executable
        self.remote = remote
        self.condition = threading.Condition(threading.RLock())
        self.process: subprocess.Popen[bytes] | None = None
        self.reader: threading.Thread | None = None
        self.master: int | None = None
        self.output = bytearray()
        self.offset = 0
        self.session = ""
        self.finished = True
        self.closed = False

    def start(self, columns: int, rows: int) -> dict[str, Any]:
        """Start once; attaching another browser preserves the existing process."""
        with self.condition:
            if self.closed:
                message = "The workspace is shutting down."
                raise ValueError(message)
            if not self.finished:
                return self.snapshot()
            self.dimensions(columns, rows)
            if shutil.which(self.executable) is None:
                message = "Codex executable is unavailable."
                raise ValueError(message)
            master, slave = pty.openpty()
            try:
                termios.tcsetwinsize(slave, (rows, columns))
                self.process = subprocess.Popen(  # noqa: S603 - Fixed child launcher; executable remains an argv value.
                    [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "--home",
                        str(self.root),
                        "--codex",
                        self.executable,
                    ],
                    stdin=slave,
                    stdout=slave,
                    stderr=slave,
                    cwd=self.root,
                    env={
                        **os.environ,
                        "PYTHONPATH": os.pathsep.join(sys.path),
                        "TERM": "xterm-256color",
                        "COLORTERM": "truecolor",
                        "AFAIRESI_GUI_TERMINAL_CHILD": "1",
                        "AFAIRESI_GUI_DEVICE_AUTH": "1" if self.remote else "0",
                    },
                    start_new_session=True,
                )
            except BaseException:
                os.close(master)
                raise
            finally:
                os.close(slave)
            self.master = master
            os.set_blocking(master, False)
            self.output.clear()
            self.offset = 0
            self.session = uuid.uuid4().hex
            self.finished = False
            self.reader = threading.Thread(
                target=self._read, args=(self.process, master), daemon=True
            )
            self.reader.start()
            return self.snapshot()

    def _read(self, process: subprocess.Popen[bytes], master: int) -> None:
        """Relay raw UTF-8 and terminal escapes without interpreting commands."""
        try:
            while True:
                select.select([master], [], [])
                chunk = os.read(master, 65_536)
                if not chunk:
                    break
                with self.condition:
                    self.output.extend(chunk)
                    self.offset += len(chunk)
                    del self.output[:-MAX_TERMINAL_OUTPUT]
                    self.condition.notify_all()
        except OSError:
            pass
        finally:
            process.wait()
            with self.condition:
                os.close(master)
                self.master = None
                self.finished = True
                self.condition.notify_all()

    def snapshot(self, after: int = 0, session: str = "") -> dict[str, Any]:
        """Return a bounded replay, with cursors measured in bytes."""
        with self.condition:
            base = self.offset - len(self.output)
            reset = session != self.session or not base <= after <= self.offset
            start = base if reset else after
            return {
                "session": self.session,
                "offset": self.offset,
                "reset": reset,
                "output": base64.b64encode(self.output[start - base :]).decode(),
                "running": not self.finished,
                "exit_code": self.process.poll() if self.process else None,
            }

    def read(self, after: int, session: str) -> dict[str, Any]:
        """Wait for output without keeping a browser in a busy polling loop."""
        with self.condition:
            self.condition.wait_for(
                lambda: (
                    self.finished or self.offset != after or self.session != session
                ),
                timeout=15,
            )
            return self.snapshot(after, session)

    def write(self, data: str, session: str) -> None:
        """Pass keyboard and bracketed-paste bytes to the current native session."""
        content = base64.b64decode(data, validate=True)
        deadline = time.monotonic() + 5
        with self.condition:
            while content:
                if session != self.session or self.master is None or self.finished:
                    message = "The Codex session ended. Start Codex again."
                    raise ValueError(message)
                try:
                    content = content[os.write(self.master, content) :]
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        message = "Codex is not accepting terminal input."
                        raise ValueError(message) from None
                    self.condition.wait(timeout=0.01)

    @staticmethod
    def dimensions(columns: int, rows: int) -> None:
        """Bound terminal allocations while allowing wide desktop screens."""
        if not 20 <= columns <= 500 or not 5 <= rows <= 200:  # noqa: PLR2004 - Terminal size bounds.
            message = "Terminal dimensions are out of range."
            raise ValueError(message)

    def resize(self, columns: int, rows: int, session: str) -> None:
        """Deliver a native SIGWINCH through the PTY's window size."""
        self.dimensions(columns, rows)
        with self.condition:
            if self.master is not None and session == self.session:
                termios.tcsetwinsize(self.master, (rows, columns))

    def close(self) -> None:
        """Terminate this workspace's native Codex process group and reader."""
        with self.condition:
            self.closed = True
            process = self.process
        if process is not None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            Jobs.reap(process)
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        if self.reader is not None:
            self.reader.join(timeout=3)


def terminal_child(root: Path, executable: str, *, remote: bool) -> None:
    """Acquire the controlling terminal before executing the official Codex CLI."""
    fcntl.ioctl(sys.stdin.fileno(), termios.TIOCSCTTY, 0)
    if remote:
        status = subprocess.run(  # noqa: S603 - Only query the selected Codex executable.
            [executable, "login", "status"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if status.returncode:
            login = subprocess.run(  # noqa: S603 - Codex owns device authentication and its credentials.
                [executable, "login", "--device-auth"], check=False
            )
            if login.returncode:
                raise SystemExit(login.returncode)
    os.execvp(  # noqa: S606 - Replace the PTY child with the selected native Codex.
        executable,
        [
            executable,
            "--no-alt-screen",
            "--no-daemon",
            "--cd",
            str(root),
            "--sandbox",
            "workspace-write",
            "--ask-for-approval",
            "on-request",
        ],
    )


class Application:
    """Route local API calls to workspace inspection, actions, and Codex."""

    def __init__(
        self, home: Path, codex: str = BUNDLED_CODEX, *, remote: bool = False
    ) -> None:
        """Own runtime state independently of HTTP request handlers."""
        self.workspace = Workspace(home)
        self.jobs = Jobs()
        self.terminal = Terminal(self.workspace.home, codex, remote=remote)

    def get(self, path: str, query: dict[str, list[str]]) -> object:
        """Handle read-only workspace requests."""
        repository = query.get("repository", [""])[0]
        if path == "/api/workspace":
            return {
                "home": str(self.workspace.home),
                "repositories": self.workspace.repositories(),
            }
        if path == "/api/terminal":
            return self.terminal.read(
                int(query.get("after", ["0"])[0]), query.get("session", [""])[0]
            )
        if path == "/api/repository":
            return self.workspace.inspect(repository)
        if path in {"/api/changes", "/api/diff"}:
            return (
                self.workspace.changes(repository)
                if path == "/api/changes"
                else self.workspace.interface_changes(repository)
            )
        if path == "/api/actions":
            return (
                self.workspace.actions(repository, query["resource"][0])
                if "resource" in query
                else self.workspace.action_map(repository)
            )
        if path == "/api/activity":
            self.workspace.repository(repository)
            return {
                "jobs": self.jobs.snapshot(repository),
            }
        raise FileNotFoundError(path)

    def post(self, path: str, body: dict[str, Any]) -> object:
        """Validate targets again before initiating user-requested operations."""
        if path.startswith("/api/terminal/"):
            return self._terminal_action(path, body)
        repository = str(body.get("repository", ""))
        root, _entry = self.workspace.repository(repository)
        if path == "/api/jobs":
            return self._start_job(root, repository, body)
        if path == "/api/jobs/cancel":
            identifier = str(body.get("id", ""))
            if identifier not in {
                item["id"] for item in self.jobs.snapshot(repository)
            }:
                message = "Job does not belong to this repository."
                raise ValueError(message)
            self.jobs.cancel(identifier)
            return {}
        raise FileNotFoundError(path)

    def _terminal_action(self, path: str, body: dict[str, Any]) -> object:
        """Keep raw terminal controls separate from repository action routing."""
        session = str(body.get("session", ""))
        if path == "/api/terminal/start":
            return self.terminal.start(
                int(body.get("columns", 0)), int(body.get("rows", 0))
            )
        if path == "/api/terminal/input":
            self.terminal.write(str(body.get("data", "")), session)
        elif path == "/api/terminal/resize":
            self.terminal.resize(
                int(body.get("columns", 0)), int(body.get("rows", 0)), session
            )
        else:
            raise FileNotFoundError(path)
        return {}

    def _start_job(
        self, root: Path, repository: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        """Resolve and execute one supported resource action."""
        resource = str(body.get("resource", ""))
        action = str(body.get("action", ""))
        actions = self.workspace.actions(repository, resource)
        if action not in actions:
            message = "This action is unavailable for the selected resource."
            raise ValueError(message)
        argv = actions[action]
        if action == "run":
            arguments = body.get("arguments", [])
            if isinstance(arguments, str):
                arguments = shlex.split(arguments)
            if not isinstance(arguments, list) or not all(
                isinstance(value, str) for value in arguments
            ):
                message = "Run arguments must be an array of strings."
                raise TypeError(message)
            argv = [*argv, "--", *arguments]
        return self.jobs.start(root, repository, resource, action, argv)

    def close(self) -> None:
        """Release the processes owned by this local server."""
        self.jobs.close()
        self.terminal.close()


def handler(application: Application) -> type[BaseHTTPRequestHandler]:  # noqa: C901 - Factory contains all HTTP methods.
    """Create an HTTP handler serving only explicit assets and local APIs."""

    class Handler(BaseHTTPRequestHandler):
        """Enforce loopback host and origin checks for the local control panel."""

        def log_message(self, _format: str, *args: object) -> None:
            """Keep request paths and chat content out of terminal logs."""

        def _local(self) -> None:
            host = self.headers.get("Host", "")
            port = cast("ThreadingHTTPServer", self.server).server_port
            if host not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
                message = "Only the local workspace origin is allowed."
                raise PermissionError(message)
            origin = self.headers.get("Origin")
            if origin is not None and origin != f"http://{host}":
                message = "Cross-origin requests are not allowed."
                raise PermissionError(message)
            if self.headers.get("Sec-Fetch-Site") == "cross-site":
                message = "Cross-site requests are not allowed."
                raise PermissionError(message)

        def _send(self, code: int, content: bytes, content_type: str) -> None:
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                    "img-src 'self' data:; "
                    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
                )
                self.end_headers()
                self.wfile.write(content)

        def _json(self, code: int, value: object) -> None:
            self._send(
                code, json.dumps(value).encode(), "application/json; charset=utf-8"
            )

        def _handle(self, operation: Callable[[], None]) -> None:
            try:
                self._local()
                operation()
            except PermissionError as error:
                self._json(HTTPStatus.FORBIDDEN, {"error": str(error)})
            except FileNotFoundError as error:
                self._json(HTTPStatus.NOT_FOUND, {"error": str(error)})
            except (
                ValueError,
                TypeError,
                OSError,
                afairesi.CommandError,
                subprocess.SubprocessError,
            ) as error:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})

        def do_GET(self) -> None:
            """Serve application assets and JSON snapshots."""

            def respond() -> None:
                url = urlsplit(self.path)
                vendor = url.path.removeprefix("/terminal-assets/")
                if url.path in ASSETS or (
                    url.path.startswith("/terminal-assets/")
                    and vendor in TERMINAL_FILES
                ):
                    asset = (
                        Path(__file__).parent / ASSETS[url.path]
                        if url.path in ASSETS
                        else Path(TERMINAL_ASSETS) / vendor
                    )
                    mime = mimetypes.guess_type(asset.name)[0] or "text/plain"
                    self._send(
                        HTTPStatus.OK, asset.read_bytes(), f"{mime}; charset=utf-8"
                    )
                else:
                    self._json(
                        HTTPStatus.OK, application.get(url.path, parse_qs(url.query))
                    )

            self._handle(respond)

        def do_POST(self) -> None:
            """Accept same-origin JSON actions with bounded request bodies."""

            def respond() -> None:
                if self.headers.get_content_type() != "application/json":
                    message = "Expected application/json."
                    raise TypeError(message)
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_BODY:
                    message = "Request body is empty or too large."
                    raise ValueError(message)
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    message = "Expected a JSON object."
                    raise TypeError(message)
                self._json(
                    HTTPStatus.OK, application.post(urlsplit(self.path).path, body)
                )

            self._handle(respond)

    return Handler


def ssh_arguments(options: argparse.Namespace) -> tuple[list[str], str]:
    """Quote one remote launcher command and forward its loopback port unchanged."""
    revision = options.remote_revision or SOURCE_REVISION
    if not re.fullmatch(r"[0-9a-fA-F]{40}", revision):
        message = (
            "SSH needs a full 40-character published commit. Use a clean release "
            "build or pass --remote-revision COMMIT for a development build."
        )
        raise ValueError(message)
    port = options.port
    if port == 0:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
    remote = [
        "env",
        "AFAIRESI_GUI_SSH_SESSION=1",
        "nix",
        "--extra-experimental-features",
        "nix-command flakes",
        "run",
        f"github:afairesi/afairesi/{revision.lower()}#afairesi_gui",
        "--",
        "--no-open",
        "--port",
        str(port),
    ]
    if options.codex is not None:
        remote.extend(["--codex", options.codex])
    if options.home is not None:
        remote.extend(["--home", str(options.home)])
    return [
        "ssh",
        "-T",
        "-S",
        "none",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "StdinNull=no",
        "-o",
        "RemoteCommand=none",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
        "-L",
        f"127.0.0.1:{port}:127.0.0.1:{port}",
        "--",
        options.ssh,
        shlex.join(remote),
    ], f"http://127.0.0.1:{port}"


def remote_workspace(options: argparse.Namespace) -> None:
    """Keep one SSH transport alive for the remote server and local browser."""
    arguments, url = ssh_arguments(options)
    with subprocess.Popen(  # noqa: S603 - SSH uses a fixed option vector and a shell-quoted remote command.
        arguments,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    ) as process:
        ready = False
        try:
            if process.stdout is None:
                message = "SSH did not provide a startup stream."
                raise RuntimeError(message)
            for line in process.stdout:
                if line.strip() == f"Afairesi workspace: {url}":
                    ready = True
                    sys.stdout.write(f"Afairesi workspace ({options.ssh}): {url}\n")
                    sys.stdout.flush()
                    if not options.no_open:
                        webbrowser.open(url)
                else:
                    sys.stderr.write(line)
            code = process.wait()
            if not ready or code:
                message = (
                    f"Remote workspace exited (SSH status {code}). "
                    "Ensure Nix is on the remote PATH, the package source is "
                    "reachable, and the selected port is free on both machines."
                )
                raise ValueError(message)
        except KeyboardInterrupt:
            pass
        finally:
            if process.stdin is not None:
                process.stdin.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def stop_on_ssh_disconnect(server: ThreadingHTTPServer) -> None:
    """Close the remote server when its owning SSH session closes stdin."""
    sys.stdin.buffer.read()
    server.shutdown()


def serve_workspace(options: argparse.Namespace) -> None:
    """Run the workspace and release its jobs and terminal when the launcher exits."""
    remote_session = os.environ.get("AFAIRESI_GUI_SSH_SESSION") == "1"
    if remote_session:
        os.environ["NIX_CONFIG"] = (
            os.environ.get("NIX_CONFIG", "")
            + "\nextra-experimental-features = nix-command flakes\n"
        )
    application = Application(
        (options.home or Path.home()).expanduser(),
        options.codex or BUNDLED_CODEX,
        remote=remote_session,
    )
    try:
        application.workspace.repositories()
        with ThreadingHTTPServer(
            ("127.0.0.1", options.port), handler(application)
        ) as server:
            url = f"http://127.0.0.1:{server.server_port}"
            sys.stdout.write(f"Afairesi workspace: {url}\n")
            sys.stdout.flush()
            if not options.no_open:
                webbrowser.open(url)
            if remote_session:
                threading.Thread(
                    target=stop_on_ssh_disconnect, args=(server,), daemon=True
                ).start()
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        application.close()


def main() -> None:
    """Launch the browser workspace on the loopback interface."""
    parser = argparse.ArgumentParser(
        description="Open home's direct submodules locally or over SSH."
    )
    parser.add_argument(
        "--ssh", metavar="DESTINATION", help="remote SSH host alias or user@host"
    )
    parser.add_argument(
        "--remote-revision",
        metavar="COMMIT",
        help="published remote GUI commit (default: this build's clean Git revision)",
    )
    parser.add_argument(
        "--home",
        type=Path,
        help="home Git repository (default: home on the machine running the workspace)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="HTTP port on each machine (default: 8765; 0 chooses a local free port)",
    )
    parser.add_argument(
        "--no-open", action="store_true", help="print the URL without opening a browser"
    )
    parser.add_argument(
        "--codex", help="override the bundled Codex executable on the workspace machine"
    )
    options = parser.parse_args()
    if os.environ.pop("AFAIRESI_GUI_TERMINAL_CHILD", "") == "1":
        terminal_child(
            (options.home or Path.home()).expanduser(),
            options.codex or BUNDLED_CODEX,
            remote=os.environ.get("AFAIRESI_GUI_DEVICE_AUTH") == "1",
        )
        return
    if not 0 <= options.port <= 65535:  # noqa: PLR2004 - TCP port range.
        parser.error("--port must be between 0 and 65535")
    if options.remote_revision is not None and options.ssh is None:
        parser.error("--remote-revision requires --ssh")
    if options.ssh is not None:
        if not options.ssh or options.ssh.startswith("-"):
            parser.error("--ssh requires an SSH host alias or user@host")
        try:
            remote_workspace(options)
        except (OSError, ValueError) as error:
            parser.error(str(error))
        return
    serve_workspace(options)


if __name__ == "__main__":
    main()

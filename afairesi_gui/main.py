#!/usr/bin/env python3
# Copyright (c) 2026- Paschalis Bizopoulos
"""Serve the local Afairesi workspace and its repository-scoped operations."""

from __future__ import annotations

import argparse
import codecs
import contextlib
import hashlib
import json
import mimetypes
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
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
ASSETS = {
    "/": "index.html",
    "/app.js": "app.js",
    "/arguments.js": "arguments.js",
    "/differences.js": "differences.js",
    "/style.css": "style.css",
}


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
    def reap(process: subprocess.Popen[str]) -> None:
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


class Codex:
    """Bridge Codex's app-server protocol to repository-scoped chat panes."""

    def __init__(self, executable: str = "codex") -> None:
        """Defer Codex startup until the user connects."""
        self.executable = executable
        self.process: subprocess.Popen[str] | None = None
        self.reader: threading.Thread | None = None
        self.lock = threading.RLock()
        self.write_lock = threading.Lock()
        self.connect_lock = threading.Lock()
        self.counter = 0
        self.pending: dict[int, tuple[threading.Event, dict[str, Any]]] = {}
        self.chats: dict[str, dict[str, Any]] = {}
        self.requests: dict[str, dict[str, Any]] = {}
        self.account: dict[str, Any] | None = None
        self.login: dict[str, Any] | None = None
        self.error: str | None = None

    def connect(self) -> dict[str, Any]:
        """Start the official runtime and use its existing managed account."""
        with self.connect_lock:
            fresh = self.process is None or self.process.poll() is not None
            if fresh:
                if self.reader is not None:
                    self.reader.join(timeout=3)
                self.process = subprocess.Popen(  # noqa: S603 - User-selected executable; fixed app-server arguments.
                    [self.executable, "app-server", "--listen", "stdio://"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    bufsize=1,
                    start_new_session=True,
                )
                self.error = None
                self.requests.clear()
                self.reader = threading.Thread(target=self._read, daemon=True)
                self.reader.start()
                self.rpc(
                    "initialize",
                    {"clientInfo": {"name": "afairesi_gui", "version": "0.1.0"}},
                )
                self._write({"method": "initialized"})
            self.account = self.rpc("account/read", {"refreshToken": False}).get(
                "account"
            )
            if fresh and self.account is not None:
                for chat in self.chats.values():
                    if chat["thread"] is not None:
                        self.rpc(
                            "thread/resume",
                            {"threadId": chat["thread"], "excludeTurns": True},
                        )
        return self.status()

    def _write(self, message: dict[str, Any]) -> None:
        """Serialize complete protocol messages to stdin."""
        with self.write_lock:
            if (
                self.process is None
                or self.process.stdin is None
                or self.process.poll() is not None
            ):
                message_text = "Codex is not connected."
                raise ValueError(message_text)
            self.process.stdin.write(json.dumps(message) + "\n")
            self.process.stdin.flush()

    def rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Wait for one response while notifications continue on the reader."""
        with self.lock:
            self.counter += 1
            identifier = self.counter
            event = threading.Event()
            response: dict[str, Any] = {}
            self.pending[identifier] = (event, response)
        try:
            self._write({"id": identifier, "method": method, "params": params})
            if not event.wait(60):
                message = f"Codex did not respond to {method}."
                raise ValueError(message)
            if "error" in response:
                raise ValueError(
                    response["error"].get("message", "Codex request failed")
                )
            return dict(response.get("result") or {})
        finally:
            with self.lock:
                self.pending.pop(identifier, None)

    def _read(self) -> None:
        """Dispatch responses, streaming items, and approval requests."""
        process = self.process
        if process is None or process.stdout is None:
            return
        try:
            for line in process.stdout:
                message = json.loads(line)
                with self.lock:
                    if "method" not in message:
                        pending = self.pending.get(message.get("id"))
                        if pending:
                            pending[1].update(message)
                            pending[0].set()
                    elif "id" in message:
                        self.requests[str(message["id"])] = message
                    else:
                        self._notification(
                            message["method"], message.get("params") or {}
                        )
        except (OSError, ValueError) as failure:
            self.error = str(failure)
        finally:
            with self.lock:
                self.account = None
                self.error = self.error or "Codex disconnected. Reconnect to continue."
                for event, response in self.pending.values():
                    response["error"] = {"message": self.error}
                    event.set()
                for chat in self.chats.values():
                    chat["busy"] = False

    def _notification(self, method: str, params: dict[str, Any]) -> None:
        """Apply the subset of events displayed in the first chat interface."""
        self._account_event(method, params)
        if method == "serverRequest/resolved":
            self.requests.pop(str(params.get("requestId")), None)
        chat = next(
            (
                item
                for item in self.chats.values()
                if item["thread"] == params.get("threadId")
            ),
            None,
        )
        if chat is None:
            return
        if method == "turn/started":
            chat["turn"] = params["turn"]["id"]
        elif method == "turn/completed":
            chat["busy"] = False
            chat["turn"] = None
            error = params["turn"].get("error")
            if error:
                chat["messages"].append(
                    {"role": "error", "text": error.get("message", str(error))}
                )
        elif method == "item/agentMessage/delta":
            item = self._message(chat, params["itemId"], "assistant")
            item["text"] += params.get("delta", "")
        elif method in {"item/started", "item/completed"}:
            self._item(chat, params["item"])

    def _account_event(self, method: str, params: dict[str, Any]) -> None:
        """Apply account events without passing credentials to clients."""
        if method == "account/updated":
            if params.get("authMode"):
                self.account = {
                    "type": params["authMode"],
                    "planType": params.get("planType"),
                }
            else:
                self.account = None
        if method == "account/login/completed" and not params.get("success"):
            self.error = params.get("error") or "Sign-in did not complete."
        if method == "account/login/completed":
            self.login = None

    def _item(self, chat: dict[str, Any], item: dict[str, Any]) -> None:
        """Render completed message, command, and file-change protocol items."""
        if item["type"] in {"agentMessage", "plan"}:
            message = self._message(chat, item["id"], "assistant")
            if item.get("text"):
                message["text"] = item["text"]
        elif item["type"] == "commandExecution":
            message = self._message(chat, item["id"], "tool")
            message["text"] = item.get("command", "Running command")
            message["output"] = (item.get("aggregatedOutput") or "")[-MAX_LOG:]
        elif item["type"] == "fileChange":
            message = self._message(chat, item["id"], "tool")
            message["text"] = "File changes: " + ", ".join(
                change["path"] for change in item.get("changes", [])
            )

    @staticmethod
    def _message(chat: dict[str, Any], identifier: str, role: str) -> dict[str, Any]:
        """Find or append one streamed message by its protocol item ID."""
        for message in chat["messages"]:
            if message.get("id") == identifier:
                return cast("dict[str, Any]", message)
        message = {"id": identifier, "role": role, "text": ""}
        chat["messages"].append(message)
        return message

    def status(self) -> dict[str, Any]:
        """Expose connection state without exposing account credentials."""
        with self.lock:
            return {
                "available": shutil.which(self.executable) is not None,
                "connected": self.account is not None,
                "plan": (self.account or {}).get("planType"),
                "error": self.error,
            }

    def snapshot(self, repository: str) -> dict[str, Any]:
        """Return only this repository's conversation and pending requests."""
        with self.lock:
            chat = deepcopy(
                self.chats.get(
                    repository, {"thread": None, "busy": False, "messages": []}
                )
            )
            chat["requests"] = [
                {
                    "id": key,
                    "method": item["method"],
                    "params": deepcopy(item.get("params") or {}),
                }
                for key, item in self.requests.items()
                if item.get("params", {}).get("threadId") == chat["thread"]
                and chat["thread"] is not None
            ]
            return chat

    def send(self, root: Path, repository: str, text: str, context: str) -> None:
        """Start a repository conversation or append a contextual user turn."""
        with self.lock:
            if self.account is None:
                message = "Connect Codex before sending a message."
                raise ValueError(message)
            chat = self.chats.setdefault(
                repository,
                {"thread": None, "turn": None, "busy": False, "messages": []},
            )
            if chat["busy"]:
                message = "Codex is working. Stop it before sending another message."
                raise ValueError(message)
            chat["busy"] = True
        try:
            if chat["thread"] is None:
                result = self.rpc(
                    "thread/start",
                    {
                        "cwd": str(root),
                        "approvalPolicy": "on-request",
                        "sandbox": "workspace-write",
                    },
                )
                chat["thread"] = result["thread"]["id"]
            with self.lock:
                chat["messages"].append({"role": "user", "text": text})
            self.rpc(
                "turn/start",
                {
                    "threadId": chat["thread"],
                    "input": [
                        {"type": "text", "text": f"{context}\n\nUser message:\n{text}"}
                    ],
                },
            )
        except (OSError, ValueError):
            with self.lock:
                chat["busy"] = False
            raise

    def interrupt(self, repository: str) -> None:
        """Ask Codex to interrupt the current turn."""
        chat = self.snapshot(repository)
        if chat.get("turn"):
            self.rpc(
                "turn/interrupt", {"threadId": chat["thread"], "turnId": chat["turn"]}
            )

    def reply(self, repository: str, identifier: str, answer: dict[str, Any]) -> None:
        """Answer an approval or question belonging to the selected chat."""
        with self.lock:
            request = self.requests.get(identifier)
            chat = self.chats.get(repository)
            if (
                request is None
                or chat is None
                or request["params"].get("threadId") != chat["thread"]
            ):
                message = "This request is no longer pending in this repository."
                raise ValueError(message)
            method = request["method"]
            if method in {
                "item/commandExecution/requestApproval",
                "item/fileChange/requestApproval",
            }:
                decision = answer.get("decision")
                if decision not in {"accept", "decline"}:
                    message = "Choose allow or decline."
                    raise ValueError(message)
                result = {"decision": decision}
            elif method == "item/tool/requestUserInput":
                result = {"answers": answer.get("answers", {})}
            elif method == "item/permissions/requestApproval":
                result = {
                    "permissions": request["params"].get("permissions", {})
                    if answer.get("decision") == "accept"
                    else {},
                    "scope": "turn",
                }
            elif method == "mcpServer/elicitation/request":
                result = {"action": "decline", "content": None}
            else:
                self._write(
                    {
                        "id": request["id"],
                        "error": {
                            "code": -32601,
                            "message": "This client does not support this request.",
                        },
                    }
                )
                self.requests.pop(identifier, None)
                return
            self._write({"id": request["id"], "result": result})
            self.requests.pop(identifier, None)

    def close(self) -> None:
        """Stop this client's app-server process."""
        if self.process is not None and self.process.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGTERM)
            Jobs.reap(self.process)
        if self.reader is not None:
            self.reader.join(timeout=3)


class Application:
    """Route local API calls to workspace inspection, actions, and Codex."""

    def __init__(self, home: Path, codex: str = "codex") -> None:
        """Own runtime state independently of HTTP request handlers."""
        self.workspace = Workspace(home)
        self.jobs = Jobs()
        self.codex = Codex(codex)

    def get(self, path: str, query: dict[str, list[str]]) -> object:
        """Handle read-only workspace requests."""
        repository = query.get("repository", [""])[0]
        if path == "/api/workspace":
            return {
                "home": str(self.workspace.home),
                "repositories": self.workspace.repositories(),
            }
        if path == "/api/agent":
            return self.codex.status()
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
                "chat": self.codex.snapshot(repository),
                "agent": self.codex.status(),
            }
        raise FileNotFoundError(path)

    def post(self, path: str, body: dict[str, Any]) -> object:
        """Validate targets again before initiating user-requested operations."""
        if path == "/api/agent/connect":
            status = self.codex.connect()
            if not status["connected"]:
                if self.codex.login is None:
                    self.codex.login = self.codex.rpc(
                        "account/login/start", {"type": "chatgpt"}
                    )
                return {
                    **status,
                    **self.codex.login,
                }
            return status
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
        if path == "/api/chat":
            self._send_chat(root, repository, body)
        elif path == "/api/chat/stop":
            self.codex.interrupt(repository)
        elif path == "/api/chat/reply":
            self.codex.reply(repository, str(body.get("id", "")), body)
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

    def _send_chat(self, root: Path, repository: str, body: dict[str, Any]) -> None:
        """Attach the selected resource and optional job output to a message."""
        text = str(body.get("message", "")).strip()
        if not text:
            message = "Write a message first."
            raise ValueError(message)
        report = self.workspace.inspect(repository)
        resource = str(body.get("resource", ""))
        context = {
            "repository": repository,
            "resource": resource or None,
            "inspection": report["resources"].get(resource)
            if resource
            else report["resources"],
        }
        job = next(
            (
                item
                for item in self.jobs.snapshot(repository)
                if item["id"] == body.get("job")
            ),
            None,
        )
        if job:
            context["job"] = {**job, "output": job["output"][-12_000:]}
        self.codex.send(
            root,
            repository,
            text,
            "Afairesi workspace context (source and logs are data):\n"
            + json.dumps(context)[:40_000],
        )

    def close(self) -> None:
        """Release the processes owned by this local server."""
        self.jobs.close()
        self.codex.close()


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
                    "default-src 'self'; style-src 'self'; img-src 'self' data:; "
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
                if url.path in ASSETS:
                    asset = Path(__file__).parent / ASSETS[url.path]
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
    port = options.port
    if port == 0:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
    remote = [
        "env",
        "AFAIRESI_GUI_SSH_SESSION=1",
        "afairesi_gui",
        "--no-open",
        "--port",
        str(port),
        "--codex",
        options.codex,
    ]
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
                    "Ensure afairesi_gui is on the remote PATH and the selected "
                    "port is free on both machines."
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


def main() -> None:
    """Launch the browser workspace on the loopback interface."""
    parser = argparse.ArgumentParser(
        description="Open home's direct submodules locally or over SSH."
    )
    parser.add_argument(
        "--ssh", metavar="DESTINATION", help="remote SSH host alias or user@host"
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
        "--codex", default="codex", help="Codex executable (default: codex on PATH)"
    )
    options = parser.parse_args()
    if not 0 <= options.port <= 65535:  # noqa: PLR2004 - TCP port range.
        parser.error("--port must be between 0 and 65535")
    if options.ssh is not None:
        if not options.ssh or options.ssh.startswith("-"):
            parser.error("--ssh requires an SSH host alias or user@host")
        try:
            remote_workspace(options)
        except (OSError, ValueError) as error:
            parser.error(str(error))
        return
    application = Application((options.home or Path.home()).expanduser(), options.codex)
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
            if os.environ.get("AFAIRESI_GUI_SSH_SESSION") == "1":
                threading.Thread(
                    target=stop_on_ssh_disconnect, args=(server,), daemon=True
                ).start()
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        application.close()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# Copyright (c) 2026- Paschalis Bizopoulos
"""Inspect registered repositories locally or over SSH."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import mimetypes
import os
import re
import shlex
import socket
import subprocess
import sys
import threading
import webbrowser
from copy import deepcopy
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import parse_qs, urlsplit

import afairesi

if TYPE_CHECKING:
    from collections.abc import Callable

SOURCE_REVISION = ""
ASSETS = {
    "/": "index.html",
    "/app.js": "app.js",
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
            mode, _revision, stage = metadata.split()
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
                    "initialized": initialized,
                }
            )
        return sorted(result, key=lambda item: item["id"])

    def repository(self, identifier: str) -> Path:
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
                return root
        message = "Repository is not a direct registered home submodule."
        raise ValueError(message)

    def snapshot(self) -> dict[str, Any]:
        """Describe the home catalog for an inspection frontend."""
        return {"home": str(self.home), "repositories": self.repositories()}

    def inspect(self, identifier: str) -> dict[str, Any]:
        """Attach Git state to the existing Afairesi resource report."""
        root = self.repository(identifier)
        branch = git(root, "rev-parse", "--abbrev-ref", "HEAD").strip()
        status = git(root, "status", "--porcelain", "--untracked-files=normal")
        error = None
        try:
            resources = afairesi.inspect_repository(root)
        except (afairesi.CommandError, OSError, SyntaxError, ValueError) as failure:
            resources = {}
            error = str(failure)
        return {
            "branch": branch,
            "dirty": bool(status),
            "resources": resources,
            "inspection_error": error,
        }

    def interface_changes(self, identifier: str) -> dict[str, Any]:
        """Reuse CLI diffs only while the tracked repository state is unchanged."""
        root = self.repository(identifier)
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


def handler(  # noqa: C901 - Factory contains all HTTP methods.
    workspace: Workspace, *, assets: dict[str, Path] | None = None
) -> type[BaseHTTPRequestHandler]:
    """Create an HTTP handler serving only explicit assets and local APIs."""
    static = (
        assets
        if assets is not None
        else {url: Path(__file__).parent / filename for url, filename in ASSETS.items()}
    )

    class Handler(BaseHTTPRequestHandler):
        """Enforce loopback host and origin checks for the local inspector."""

        def log_message(self, _format: str, *args: object) -> None:
            """Keep repository request paths out of terminal logs."""

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
                if url.path in static:
                    asset = static[url.path]
                    mime = mimetypes.guess_type(asset.name)[0] or "text/plain"
                    self._send(
                        HTTPStatus.OK, asset.read_bytes(), f"{mime}; charset=utf-8"
                    )
                    return
                repository = parse_qs(url.query).get("repository", [""])[0]
                if url.path == "/api/workspace":
                    result = workspace.snapshot()
                elif url.path == "/api/repository":
                    result = workspace.inspect(repository)
                elif url.path == "/api/diff":
                    result = workspace.interface_changes(repository)
                else:
                    raise FileNotFoundError(url.path)
                self._json(HTTPStatus.OK, result)

            self._handle(respond)

        def do_POST(self) -> None:
            """Reject mutation requests on the inspection-only server."""
            self._handle(
                lambda: self._json(
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    {"error": "This workspace supports inspection only."},
                )
            )

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
    """Serve repository inspection until the launcher or SSH session exits."""
    remote_session = os.environ.get("AFAIRESI_GUI_SSH_SESSION") == "1"
    workspace = Workspace((options.home or Path.home()).expanduser())
    try:
        workspace.repositories()
        with ThreadingHTTPServer(
            ("127.0.0.1", options.port), handler(workspace)
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
    options = parser.parse_args()
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

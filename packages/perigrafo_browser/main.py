#!/usr/bin/env python3
# Copyright (c) 2026- Paschalis Bizopoulos
"""Browse Perigrafo directories, packages, hosts, and changes in a web diagram."""

import argparse
import contextlib
import fcntl
import filecmp
import hashlib
import json
import mimetypes
import os
import platform
import re
import shlex
import shutil
import signal
import stat
import subprocess
import tempfile
import webbrowser
from collections.abc import AsyncIterator, Awaitable, Callable
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from difflib import unified_diff
from functools import cache, partial
from html import escape
from http import HTTPStatus
from pathlib import Path
from threading import Event, Lock
from time import monotonic
from typing import Any, Self
from urllib.parse import urlencode

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from perigrafo import (
    CliEntry,
    ResourceData,
    canonical_root,
    command_catalog,
    overview_data,
    repository_type,
    source_resource_data,
)
from perigrafo import CommandError as PerigrafoError
from pydantic import BaseModel, ConfigDict, StrictStr

MAX_PORT = 65535
MOUNT_FIELDS = 3
OUTPUT_DIFF_TIMEOUT = 120
OUTPUT_TEXT_LIMIT = 65536
OUTPUT_TREE_LIMIT = 1000
ACTION_BODY_LIMIT = 16384


@dataclass
class TreeNode:  # noqa: D101
    title: str
    children: list["TreeNode"] = field(default_factory=list)
    change: str | None = None
    warning: bool = False
    directory: Path | None = None
    source_file: bool = False
    output_diff: str | None = None
    text_diff: str | None = None
    expandable: bool = False
    field: str | None = None
    value: str | None = None
    lines: int | None = None


@dataclass
class OutputComparison:
    """Keep the output from a package's last two successful actions."""

    output: Path
    store: Path
    previous: Path | None = None
    current: Path | None = None
    future: Future[None] | None = None
    initialized: bool = False
    changed: bool = False
    error: str = ""
    entry: str = ""
    report: str = "report.html"
    tree: TreeNode | None = None


def copy_output_file(
    source: str,
    destination: str,
    *,
    cancelled: Event | None = None,
) -> str:
    """Copy file contents without reading devices or changing the live output."""
    if not stat.S_ISREG(Path(source).lstat().st_mode):
        msg = f"Cannot capture a special output file: {source}"
        raise ValueError(msg)
    with Path(source).open("rb") as incoming, Path(destination).open("wb") as outgoing:
        while True:
            if cancelled is not None and cancelled.is_set():
                msg = "Output capture cancelled"
                raise CancelledError(msg)
            chunk = incoming.read(1024 * 1024)
            if not chunk:
                break
            outgoing.write(chunk)
    return destination


def writable_capture_directories(capture: Path) -> None:
    """Allow removing private copies of read-only Nix output without following links."""
    if not capture.is_dir() or capture.is_symlink():
        return
    for directory, _, _ in capture.walk(follow_symlinks=False):
        directory.chmod(directory.stat().st_mode | stat.S_IRWXU)


def remove_output_capture(capture: Path) -> None:
    """Remove snapshot history, including copies saved with Nix directory modes."""
    writable_capture_directories(capture)
    shutil.rmtree(capture)


def copy_output(
    source: Path,
    destination: Path,
    *,
    cancelled: Event | None = None,
) -> None:
    """Capture a directory or its absence, preserving links without following them."""
    if source.is_symlink() or (source.exists() and not source.is_dir()):
        msg = f"Not a regular output directory: {source}"
        raise ValueError(msg)
    if source.is_dir():
        try:
            shutil.copytree(
                source,
                destination,
                symlinks=True,
                copy_function=partial(copy_output_file, cancelled=cancelled),
            )
        finally:
            writable_capture_directories(destination)
    else:
        destination.mkdir()


def compare_output(
    previous: Path,
    current: Path,
    *,
    entry: str = "",
    report: str = "report.html",
    cancelled: Event | None = None,
) -> bool:
    """Generate an offline HTML report and bound the entire comparison process."""
    executable = shutil.which("diffoscope")
    if executable is None:
        msg = "diffoscope is not installed"
        raise FileNotFoundError(msg)
    with (
        (current / "diffoscope.log").open("wb") as log,
        subprocess.Popen(  # noqa: S603 - fixed executable and argument list
            [
                executable,
                "--html",
                str(current / report),
                "--jquery",
                "disable",
                "--no-progress",
                "--new-file",
                "--exclude-directory-metadata",
                "yes",
                str(previous / "output" / entry),
                str(current / "output" / entry),
            ],
            stdout=subprocess.DEVNULL,
            stderr=log,
            start_new_session=True,
        ) as process,
    ):
        deadline = monotonic() + OUTPUT_DIFF_TIMEOUT
        try:
            while True:
                if cancelled is not None and cancelled.is_set():
                    msg = "Output comparison cancelled"
                    raise CancelledError(msg)
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(process.args, OUTPUT_DIFF_TIMEOUT)
                try:
                    result = process.wait(timeout=min(0.1, remaining))
                    break
                except subprocess.TimeoutExpired:
                    continue
        finally:
            if process.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait()
    if result not in {0, 1}:
        diagnostic = read_text(current / "diffoscope.log")[-2000:]
        msg = f"diffoscope failed (exit {result}): {diagnostic}"
        raise ValueError(msg)
    if result == 1 and not (current / report).is_file():
        msg = "diffoscope did not produce an HTML report"
        raise ValueError(msg)
    return result == 1


class OutputSnapshots:
    """Compare output from consecutive successful package actions."""

    def __init__(self) -> None:
        """Serialize capture jobs and hold package locks until the browser closes."""
        self.comparisons: dict[Path, OutputComparison] = {}
        self.reports: dict[tuple[Path, Path, str], OutputComparison] = {}
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.locks = contextlib.ExitStack()
        self.cancelled = Event()

    def __enter__(self) -> Self:
        """Keep snapshots available throughout the session."""
        return self

    def __exit__(self, *_: object) -> None:
        """Finish the active capture before releasing its history lock."""
        self.close()

    def close(self) -> None:
        """Stop queued jobs and release package histories."""
        self.cancelled.set()
        self.executor.shutdown(wait=True, cancel_futures=True)
        self.locks.close()

    def observe(self, data: dict[str, Any]) -> None:
        """Link reports without copying output just to browse a repository."""
        for record in data.get("nodes", []):
            if record["kind"] != "package" or record.get("change") == "removed":
                continue
            package = Path(record["directory"]) / record["path"]
            output = Path(record.get("output_directory", package / "tmp"))
            key = package / "tmp"
            store = package.parent.parent / "tmp" / "perigrafo_browser" / package.name
            if key not in self.comparisons:
                if not output.is_dir() and not (store / "latest").is_file():
                    continue
                comparison = OutputComparison(output, store)
                self.comparisons[key] = comparison
            else:
                comparison = self.comparisons[key]
                if comparison.future is None:
                    comparison.output = output
            record["output_diff"] = "/output-diff?" + urlencode({"path": str(key)})
            pending = comparison.future is not None and not comparison.future.done()
            data["output_pending"] = data.get("output_pending", False) or pending
            tree = self.comparison_tree(comparison, key)
            tree.output_diff = record["output_diff"]
            children = record["tree"]["children"]
            children[:] = [child for child in children if child["field"] != "tmp"]
            children.append(serialize_node(tree))

    @staticmethod
    def comparison_tree(
        comparison: OutputComparison,
        key: Path | None = None,
    ) -> TreeNode:
        """Reuse immutable capture contents while updating their status message."""
        pending = comparison.future is not None and not comparison.future.done()
        if comparison.future is None:
            message = "Build or Check to capture output."
        elif pending:
            message = "Capturing and comparing output…"
        elif comparison.error:
            message = comparison.error
        elif comparison.previous is None:
            message = "Initial output saved; compare after the next Build or Check."
        else:
            message = "Previous capture → Current capture"
        if comparison.current is not None and not pending:
            if comparison.tree is None:
                comparison.tree = output_tree(
                    comparison.output,
                    comparison.previous / "output" if comparison.previous else None,
                    comparison.current / "output",
                    diff_path=key,
                )
            tree = deepcopy(comparison.tree)
        else:
            tree = TreeNode(
                "tmp/",
                directory=comparison.output,
                expandable=True,
                field="tmp",
            )
        tree.children.insert(0, TreeNode(message, warning=bool(comparison.error)))
        return tree

    def request_capture(
        self,
        comparison: OutputComparison,
        *,
        refresh: bool = False,
        output: Path | None = None,
    ) -> None:
        """Queue each completed action, keeping report requests passive."""
        future = comparison.future
        if not self.cancelled.is_set() and (future is None or refresh):
            comparison.tree = None
            comparison.future = self.executor.submit(self.capture, comparison, output)

    def completed(self, package: Path, output: Path) -> None:
        """Capture a successful action using a stable package history."""
        key = package / "tmp"
        if key not in self.comparisons:
            store = package.parent.parent / "tmp" / "perigrafo_browser" / package.name
            self.comparisons[key] = OutputComparison(output, store)
        self.request_capture(self.comparisons[key], refresh=True, output=output)

    def entry_report(
        self,
        comparison: OutputComparison,
        entry: str,
    ) -> OutputComparison:
        """Compare an allowlisted captured entry in the background."""
        if not entry or comparison.current is None or comparison.previous is None:
            return comparison
        relative = Path(entry)
        if relative.is_absolute() or ".." in relative.parts:
            msg = "Invalid output entry"
            raise ValueError(msg)
        for capture in (comparison.previous, comparison.current):
            path = capture / "output" / relative
            if not path.parent.resolve().is_relative_to((capture / "output").resolve()):
                msg = "Output entry escapes capture"
                raise ValueError(msg)
        if any(
            (capture / "output" / relative).is_symlink()
            for capture in (comparison.previous, comparison.current)
        ):
            return self.entry_report(
                comparison,
                str(relative.parent) if relative.parent != Path() else "",
            )
        key = comparison.output, comparison.current, entry
        if key not in self.reports:
            report = replace(
                comparison,
                entry=entry,
                report=hashlib.sha256(entry.encode()).hexdigest() + ".html",
                future=None,
            )
            report.future = self.executor.submit(self.compare_entry, report)
            self.reports[key] = report
        return self.reports[key]

    def compare_entry(self, comparison: OutputComparison) -> None:
        """Generate a report for one entry without recapturing live output."""
        if comparison.previous is None or comparison.current is None:
            return
        try:
            comparison.changed = compare_output(
                comparison.previous,
                comparison.current,
                entry=comparison.entry,
                report=comparison.report,
                cancelled=self.cancelled,
            )
        except (CancelledError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
            comparison.error = str(exc)

    def initialize(self, comparison: OutputComparison) -> None:
        """Lock the history and retain the previous launch's last complete capture."""
        if comparison.initialized:
            return
        store = comparison.store
        for directory in (store.parent.parent, store.parent, store):
            if directory.is_symlink():
                msg = f"Output history must not be a symbolic link: {directory}"
                raise ValueError(msg)
            directory.mkdir(exist_ok=True)
        if any((store / name).is_symlink() for name in ("lock", "latest")):
            msg = "Output history metadata must not be a symbolic link"
            raise ValueError(msg)
        with contextlib.ExitStack() as acquired:
            lock = acquired.enter_context((store / "lock").open("a"))
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                msg = (
                    "Output history is in use by another browser. "
                    "Close it and Build or Check again."
                )
                raise ValueError(msg) from exc
            latest = store / "latest"
            if latest.exists():
                name = latest.read_text().strip()
                previous = store / name
                if (
                    re.fullmatch(r"capture-[a-zA-Z0-9_-]+", name) is None
                    or previous.is_symlink()
                    or (previous / "output").is_symlink()
                    or (previous / "timestamp").is_symlink()
                    or not (previous / "output").is_dir()
                    or not (previous / "timestamp").is_file()
                ):
                    msg = f"Invalid output capture: {latest}"
                    raise ValueError(msg)
                comparison.previous = previous
            self.locks.enter_context(acquired.pop_all())
        comparison.initialized = True

    def capture(self, comparison: OutputComparison, output: Path | None = None) -> None:
        """Publish complete copies, retain two captures, and compare their contents."""
        capture: Path | None = None
        comparison.error = ""
        comparison.changed = False
        try:
            self.initialize(comparison)
            capture = Path(tempfile.mkdtemp(prefix="capture-", dir=comparison.store))
            source = output if output is not None else comparison.output
            copy_output(source, capture / "output", cancelled=self.cancelled)
            if self.cancelled.is_set():
                return
            (capture / "timestamp").write_text(datetime.now(UTC).isoformat())
            latest = capture / "latest"
            latest.write_text(capture.name)
            latest.replace(comparison.store / "latest")
            if comparison.current is not None:
                comparison.previous = comparison.current
            comparison.output = source
            comparison.current = capture
            comparison.tree = None
            for old in comparison.store.glob("capture-*"):
                if old not in {comparison.previous, capture} and not old.is_symlink():
                    remove_output_capture(old)
            if comparison.previous is not None:
                comparison.changed = compare_output(
                    comparison.previous,
                    capture,
                    cancelled=self.cancelled,
                )
        except subprocess.TimeoutExpired:
            comparison.error = f"diffoscope exceeded {OUTPUT_DIFF_TIMEOUT} seconds"
        except (CancelledError, OSError, ValueError) as exc:
            comparison.error = str(exc)
        finally:
            if capture is not None and capture != comparison.current:
                with contextlib.suppress(OSError):
                    remove_output_capture(capture)


def output_diff_page(comparison: OutputComparison) -> bytes:
    """Serve the saved report with capture times or await its background job."""
    pending = comparison.future is not None and not comparison.future.done()
    title = escape(str(comparison.output / comparison.entry))
    heading = f"<section><h1>{title} changes</h1>"
    for label, capture in (
        ("Previous capture", comparison.previous),
        ("Current capture", comparison.current),
    ):
        if capture is not None:
            heading += f"<p>{label}: {escape(read_text(capture / 'timestamp'))}</p>"
    heading += "</section>"
    if pending:
        message = (
            "Capturing output and comparing changes… This page updates automatically."
        )
    elif comparison.error:
        message = f"Could not compare output: {comparison.error}"
    elif comparison.current is None:
        message = "Build or Check to capture output."
    elif comparison.previous is None:
        message = (
            "Initial capture saved. "
            "Changes will be available after the next Build or Check."
        )
    elif comparison.changed and comparison.current is not None:
        report = (comparison.current / comparison.report).read_bytes()
        return re.sub(
            rb"(<body[^>]*>)",
            lambda match: match[0] + heading.encode(),
            report,
            count=1,
        )
    else:
        message = "No output changes since the previous capture."
    return (
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        + ('<meta http-equiv="refresh" content="2">' if pending else "")
        + f"<title>{title} changes</title>"
        + "<style>body{font:14px system-ui;margin:32px;"
        + "background:#fdf6e3;color:#657b83;}"
        + "h1{font-size:18px;overflow-wrap:anywhere;}</style>"
        + heading
        + f"<p>{escape(message)}</p></html>"
    ).encode()


class RepositoryBrowser:
    """Cache source declarations and scope them to the requested directory."""

    def __init__(self, cwd: str | Path | None = None) -> None:
        """Start browsing at the given directory."""
        self.cwd = Path(cwd or Path.cwd()).resolve()
        self.snapshot: dict[str, Any] = {}
        self.source_snapshot = cache(browser_snapshot)

    def refresh(self) -> None:
        """Discard cached declarations and rebuild the current snapshot."""
        self.source_snapshot.cache_clear()
        self.load()

    def load(self) -> None:
        """Scope cached declarations without changing the source snapshot."""
        root = canonical_root(self.cwd)
        self.snapshot = deepcopy(scope_snapshot(self.source_snapshot(root), self.cwd))


def cli_tree(current: list[CliEntry], previous: list[CliEntry]) -> list[TreeNode]:
    """Nest interface changes by their declared command paths."""
    roots: list[TreeNode] = []
    commands: dict[tuple[str, ...], TreeNode] = {}
    old, new = set(previous), set(current)
    entries: list[tuple[CliEntry, str | None]] = [
        (entry, "removed") for entry in previous if entry not in new
    ]
    entries.extend((entry, None if entry in old else "added") for entry in current)
    for entry, change in entries:
        children = roots
        for depth, name in enumerate(entry.path, 1):
            path = entry.path[:depth]
            if path not in commands:
                commands[path] = TreeNode(name, [])
                children.append(commands[path])
            node = commands[path]
            children = node.children
        prefix = "- " if change == "removed" else "+ " if change == "added" else ""
        if entry.command:
            node.title = prefix + entry.path[-1]
            node.change = change
        else:
            children.append(TreeNode(prefix + entry.text, change=change))
    return roots


def resource_entry(record: dict[str, Any]) -> TreeNode:  # noqa: C901
    """Render source facts directly under their files, using structured identities."""
    current = record["details"]
    previous = record.get("previous_details", current)
    files = {
        source["path"]: TreeNode(
            source["path"],
            [TreeNode(f"(unavailable: {source['diagnostic']})", warning=True)]
            if source["diagnostic"]
            else [],
            source_file=True,
            lines=source["lines"],
        )
        for source in current["sources"]
    }

    def source(name: str) -> TreeNode:
        return files.setdefault(name, TreeNode(name, [], source_file=True))

    def fields(key: str, label: str | None = None) -> list[TreeNode]:
        before = previous[key] if previous["sources"] else None
        after = current[key] if current["sources"] else None
        values = (
            [(after, None)]
            if before == after
            else [(before, "removed"), (after, "added")]
        )
        rows = []
        for value, change in values:
            if value is None:
                continue
            title = f"{label}: {value}" if label else value
            prefix = "- " if change == "removed" else "+ " if change == "added" else ""
            rows.append(TreeNode(prefix + title, change=change, field=key, value=value))
        return rows

    metadata = fields("name", "Name") + fields("description", "Description")
    documentation = fields("help")
    if documentation:
        source("main.py").children[:0] = documentation

    def suppressions(data: ResourceData) -> dict[tuple[str, str, str], int]:
        return {
            (item["path"], suppression["kind"], suppression["scope"]): suppression[
                "count"
            ]
            for item in data["sources"]
            for suppression in item["suppressions"]
        }

    old, new = suppressions(previous), suppressions(current)
    for filename, kind, scope in sorted(old.keys() | new.keys()):
        before, after = (
            old.get((filename, kind, scope), 0),
            new.get((filename, kind, scope), 0),
        )
        change = (
            None
            if before == after
            else "added"
            if not before
            else "removed"
            if not after
            else "modified"
        )
        title = f"{kind} ({scope}): " + (
            str(after) if change is None else f"{before} → {after}"
        )
        parent = source(filename)
        parent.children.append(TreeNode(title, change=change, field="suppressions"))

    def cli(data: ResourceData) -> list[CliEntry]:
        return [
            CliEntry(tuple(row["path"]), row["text"], row["command"])
            for row in data["cli"]
        ]

    for key, title, filename in (
        ("cli", "Arguments", "main.py"),
        ("tests", "Tests", "test_main.py"),
        ("dependencies", "Dependencies", current["dependency_source"]),
    ):
        if error := current["diagnostics"].get(key):
            children = [TreeNode(f"(unavailable: {error})", warning=True)]
        elif key == "cli":
            children = cli_tree(cli(current), cli(previous))
        else:
            previous_values = (
                [f"{item['kind']}: {item['target']}" for item in previous[key]]
                if key == "dependencies"
                else previous[key]
            )
            current_values = (
                [f"{item['kind']}: {item['target']}" for item in current[key]]
                if key == "dependencies"
                else current[key]
            )
            children = [
                TreeNode(f"- {item}", change="removed")
                for item in previous_values
                if item not in current_values
            ]
            children.extend(
                TreeNode(
                    item if item in previous_values else f"+ {item}",
                    change=None if item in previous_values else "added",
                )
                for item in current_values
            )
        if children:
            parent = source(filename)
            parent.children.append(TreeNode(title, children, field=key))
    tree = TreeNode(
        record["path"],
        [*metadata, *(files[name] for name in sorted(files))],
        change=record.get("change"),
    )
    if record["kind"] == "host":
        tree.children.insert(0, TreeNode("OS: NixOS (configuration)"))
    return tree


def serialize_node(node: TreeNode) -> dict[str, Any]:
    """Serialize source details and semantic changes for the web browser."""
    children = [serialize_node(child) for child in node.children]
    return {
        "title": node.title,
        "change": node.change,
        "warning": node.warning or any(child["warning"] for child in children),
        "source_file": node.source_file,
        "output_diff": node.output_diff,
        "text_diff": node.text_diff,
        "expandable": node.expandable,
        "directory": str(node.directory) if node.directory is not None else None,
        "field": node.field,
        "value": node.value,
        "lines": node.lines,
        "children": children,
    }


def read_text(path: Path) -> str:
    """Read optional runtime evidence without failing the repository browser."""
    try:
        return path.read_text()
    except OSError:
        return ""


def machine_resource(
    system: Path | None = None,
    mounts_path: Path | None = None,
) -> dict[str, Any]:
    """Describe the running OS and observed persistence mechanisms."""
    try:
        release = platform.freedesktop_os_release()
    except OSError:
        release = {}
    mounts = [
        line.split()
        for line in read_text(mounts_path or Path("/proc/self/mounts")).splitlines()
    ]
    filesystems = {parts[1]: parts[2] for parts in mounts if len(parts) >= MOUNT_FIELDS}
    root_fs = filesystems.get("/", "unknown")
    system = system or Path("/run/current-system")
    units = system / "etc/systemd/system"
    preservation = (units / "preservation.target").exists() or (
        system / "etc/tmpfiles.d/preservation.conf"
    ).exists()
    impermanence = "impermanence" in read_text(system / "activate") or any(
        "impermanence" in read_text(unit) for unit in units.glob("persist-*.service")
    )
    name = platform.node()
    return {
        "id": ".:machine",
        "kind": "machine",
        "name": name,
        "home": str(Path.home().resolve()),
        "repository": ".",
        "path": "/",
        "icon": release.get("ID"),
        "description": release.get("PRETTY_NAME", platform.system()),
        "details": [
            f"OS: {release.get('PRETTY_NAME', platform.system())}",
            f"Hostname: {name}",
            "Root: "
            + (
                f"ephemeral ({root_fs})"
                if root_fs in {"tmpfs", "ramfs"}
                else f"{root_fs}; reset on reboot not established"
            ),
            "Preservation: "
            + ("detected in running system" if preservation else "not detected"),
            "Impermanence: "
            + ("detected in running system" if impermanence else "not detected"),
            *[
                f"Filesystem {mount}: {filesystems[mount]}"
                for mount in ("/home", "/nix", "/persistent", "/persist")
                if mount in filesystems
            ],
        ],
    }


def validate_overview(data: dict[str, Any]) -> None:
    """Reject incompatible source contracts before interpreting their records."""
    version = data.get("schema_version")
    if (
        data.get("schema") != "perigrafo.overview"
        or type(version) is not int
        or version != 1
    ):
        msg = (
            f"Unsupported Perigrafo overview schema: {data.get('schema')!r}, "
            f"version {version!r}"
        )
        raise ValueError(msg)


def merge_overviews(
    current: dict[str, Any],
    previous: dict[str, Any],
) -> dict[str, Any]:
    """Annotate resource and relationship changes using their structured identities."""
    validate_overview(current)
    validate_overview(previous)
    data = deepcopy(current)
    old = {node["id"]: node for node in previous["nodes"]}
    new = {node["id"]: node for node in data["nodes"]}
    historical = {
        node["repository"]
        for node in previous["nodes"]
        if node["kind"] == "repository" and node.get("revision_available", False)
    }
    for identifier in sorted(old.keys() | new.keys()):
        before, after = old.get(identifier), new.get(identifier)
        record = after if after is not None else deepcopy(before)
        if record is None or record["repository"] not in historical:
            continue
        if after is None:
            record["removed"] = True
            record["change"] = "removed"
            data["nodes"].append(record)
        elif before is None and record["kind"] in {"package", "host", "check"}:
            record["change"] = "added"
        if record["kind"] in {"package", "host", "check"}:
            empty = source_resource_data(record["name"], {}, path=record["path"])
            record["previous_details"] = (
                before["details"] if before is not None else empty
            )
            if after is None:
                record["details"] = empty

    def key(edge: dict[str, Any]) -> tuple[str, str, str]:
        return edge["source"], edge["target"], edge["kind"]

    old_edges = {key(edge) for edge in previous["edges"]}
    new_edges = {key(edge) for edge in data["edges"]}
    repositories = {node["id"]: node["repository"] for node in data["nodes"]}
    for edge in data["edges"]:
        if (
            repositories.get(edge["target"]) in historical
            and key(edge) not in old_edges
        ):
            edge["change"] = "added"
    data["edges"].extend(
        {**deepcopy(edge), "change": "removed"}
        for edge in previous["edges"]
        if key(edge) not in new_edges and repositories.get(edge["target"]) in historical
    )
    data["nodes"].sort(key=lambda record: record["id"])
    return data


def package_storage(directory: Path) -> list[TreeNode]:
    """Describe optional tracked storage and link to existing runtime output."""
    nodes: list[TreeNode] = []
    resources = directory / "prm"
    if resources.is_dir() and not resources.is_symlink():
        nodes.append(TreeNode("prm/"))
    output = directory / "tmp"
    if output.is_dir() and not output.is_symlink():
        nodes.append(TreeNode("tmp/", directory=output, expandable=True, field="tmp"))
    return nodes


def output_kind(path: Path | None) -> str:
    """Classify output without following symbolic links."""
    if path is None:
        return "missing"
    if path.is_symlink():
        return "link"
    if path.is_dir():
        return "directory"
    return "file" if path.is_file() else "missing"


def output_text(path: Path | None) -> str | None:
    """Read bounded text or a link target for an inline diff."""
    kind = output_kind(path)
    if kind == "missing":
        return ""
    if path is None:
        return None
    if kind == "link":
        return str(path.readlink()) + "\n"
    if kind != "file" or path.stat().st_size > OUTPUT_TEXT_LIMIT:
        return None
    try:
        value = path.read_text()
    except UnicodeError:
        return None
    return None if "\0" in value else value


def output_change(old: Path | None, new: Path | None) -> str | None:
    """Compare entry content while ignoring directory metadata."""
    before, after = output_kind(old), output_kind(new)
    if before == "missing":
        return "added" if after != "missing" else None
    if after == "missing":
        return "removed"
    same = before == after
    if same and before == "file" and old is not None and new is not None:
        same = filecmp.cmp(old, new, shallow=False)
    elif same and before == "link" and old is not None and new is not None:
        same = old.readlink() == new.readlink()
    return None if same else "modified"


def output_entry(root: Path | None, relative: Path) -> Path | None:
    """Treat descendants of missing directories or links as absent."""
    if root is None:
        return None
    path = root
    for part in relative.parts:
        if output_kind(path) != "directory":
            return None
        path /= part
    return path


def output_tree(  # noqa: C901 - merge entries with a shared recursive budget
    output: Path,
    previous: Path | None,
    current: Path,
    *,
    diff_path: Path | None = None,
) -> TreeNode:
    """Merge a bounded output tree, linking to full listings for large outputs."""
    remaining = OUTPUT_TREE_LIMIT

    def build(relative: Path) -> TreeNode:  # noqa: C901 - directory merge and entry diff
        nonlocal remaining
        remaining -= 1
        old = output_entry(previous, relative)
        new = output_entry(current, relative)
        before, after = output_kind(old), output_kind(new)
        directory = "directory" in {before, after}
        title = (
            "tmp/" if relative == Path() else relative.name + ("/" if directory else "")
        )
        node = TreeNode(title, expandable=directory, field="tmp")
        if after != "missing" and new is not None and not new.is_symlink():
            with contextlib.suppress(ValueError, OSError):
                _, node.directory = output_path(str(output / relative), roots={output})
        if directory:
            names: set[str] = set()
            for path in (old, new):
                if output_kind(path) == "directory" and path is not None:
                    names.update(child.name for child in path.iterdir())
            for name in sorted(names):
                if remaining <= 0:
                    node.children.append(
                        TreeNode(
                            "More output entries: open directory to browse",
                            directory=node.directory,
                            warning=True,
                        ),
                    )
                    break
                node.children.append(build(relative / name))
        if previous is not None:
            node.change = output_change(old, new)
            if node.change or any(
                child.change or child.output_diff for child in node.children
            ):
                node.output_diff = "/output-diff?" + urlencode(
                    {
                        "path": str(diff_path if diff_path is not None else output),
                        "entry": str(relative) if relative != Path() else "",
                    },
                )
                if not directory:
                    before_text, after_text = output_text(old), output_text(new)
                    if before_text is not None and after_text is not None:
                        node.text_diff = "".join(
                            unified_diff(
                                before_text.splitlines(keepends=True),
                                after_text.splitlines(keepends=True),
                                fromfile="Previous capture",
                                tofile="Current capture",
                            ),
                        )[:16384]
        return node

    return build(Path())


def browser_snapshot(root: Path) -> dict[str, Any]:
    """Build one source graph with a single detail tree per resource."""
    try:
        current = overview_data(root)
    except PerigrafoError:
        if repository_type(root, "directory") != "directory":
            raise
        return directory_snapshot(root)
    data = merge_overviews(current, overview_data(root, revision="HEAD"))
    machine = machine_resource()
    data["machine"] = machine
    data["nodes"].insert(0, machine)
    data["edges"].extend(
        {"source": machine["id"], "target": node["id"], "kind": "hostname-match"}
        for node in data["nodes"]
        if node["kind"] == "host" and node["name"] == machine["name"]
    )
    for record in data["nodes"]:
        if record["kind"] not in {"package", "host", "check"}:
            continue
        if record["kind"] == "host":
            record["icon"] = "nixos"
        tree = resource_entry(record)
        directory = root / record["repository"] / record["path"]
        tree.children.extend(package_storage(directory))
        links = [
            TreeNode(
                f"{edge['kind']}: {edge['source']} → {edge['target']}",
                change=edge.get("change"),
            )
            for edge in data["edges"]
            if edge["target"] == record["id"]
            and edge["kind"] not in {"contains", "submodule"}
        ]
        if links:
            tree.children.append(TreeNode("Connections", links, field="connections"))
        record["source_metrics"] = record["details"]["source_metrics"]
        record["tree"] = serialize_node(tree)
    data["root"] = str(root)
    return data


def gui_data(
    root: Path,
    *,
    browser: RepositoryBrowser | None = None,
    refresh: bool = True,
) -> dict[str, Any]:
    """Return a scoped snapshot, reusing the server's source cache when provided."""
    viewer = browser if browser is not None else RepositoryBrowser(root)
    viewer.cwd = root
    viewer.refresh() if refresh else viewer.load()
    return viewer.snapshot


def browser_parent(directory: Path) -> Path | None:
    """Stop upward navigation at home or the filesystem root."""
    if directory == Path.home().resolve() or directory.parent == directory:
        return None
    return directory.parent


def directory_snapshot(directory: Path) -> dict[str, Any]:
    """Show directory containers outside a Perigrafo repository."""
    return {
        "root": str(directory),
        "machine": machine_resource(),
        "parent": str(parent) if (parent := browser_parent(directory)) else None,
        "nodes": [
            {
                "id": f"{child.name}:directory",
                "kind": "directory",
                "repository": child.name,
                "path": ".",
                "directory": str(child.resolve()),
            }
            for child in sorted(directory.iterdir())
            if child.is_dir() and not child.name.startswith(".")
        ],
        "edges": [],
    }


def scope_snapshot(data: dict[str, Any], directory: Path) -> dict[str, Any]:
    """Restrict the graph to a directory without rebuilding its detail trees."""
    data = data.copy()
    root = Path(data["root"])
    if not data["nodes"] or all(
        record["kind"] == "directory" for record in data["nodes"]
    ):
        return data
    nodes = []
    for source_record in data["nodes"]:
        record = source_record.copy()
        repository = (root / record["repository"]).resolve()
        location = repository / record["path"]
        if record["kind"] == "machine":
            if directory == Path.home().resolve():
                nodes.append(record)
            continue
        if repository.is_relative_to(directory):
            record["repository"] = str(repository.relative_to(directory))
        elif directory.is_relative_to(repository):
            if record.get("profile") == "home":
                continue
            if record["kind"] != "repository" and not location.is_relative_to(
                directory,
            ):
                continue
            record["repository"] = "."
        else:
            continue
        record["directory"] = str(repository)
        nodes.append(record)
    known = {record["id"] for record in nodes}
    data["nodes"] = nodes
    data["edges"] = [
        edge
        for edge in data["edges"]
        if edge["source"] in known and edge["target"] in known
    ]
    data["root"] = str(directory)
    data["parent"] = str(parent) if (parent := browser_parent(directory)) else None
    return data


def output_path(
    requested: str,
    *,
    roots: set[Path] | None = None,
) -> tuple[Path, Path]:
    """Resolve a requested output entry within a conventional package tmp directory."""
    path = Path(requested)
    root = next(
        (
            parent
            for parent in (path, *path.parents)
            if parent in (roots or set())
            or (parent.name == "tmp" and parent.parent.parent.name == "packages")
        ),
        None,
    )
    if not path.is_absolute() or root is None or root.is_symlink():
        msg = "Not a package output directory"
        raise ValueError(msg)
    target = path.resolve(strict=True)
    root = root.resolve(strict=True)
    if not target.is_relative_to(root) or not root.is_dir():
        msg = "Output path escapes tmp"
        raise ValueError(msg)
    if not target.is_dir() and not target.is_file():
        msg = "Output is not a regular file or directory"
        raise ValueError(msg)
    return root, target


def output_index(root: Path, directory: Path, diff_url: str | None = None) -> bytes:
    """Build a browser directory listing without exposing paths outside tmp/."""
    entries = []
    if directory != root:
        entries.append(("../", directory.parent))
    for child in sorted(
        directory.iterdir(),
        key=lambda path: (not path.is_dir(), path.name),
    ):
        try:
            output_path(str(child), roots={root})
        except (ValueError, OSError):
            continue
        entries.append((child.name + ("/" if child.is_dir() else ""), child))
    links = "".join(
        '<li><a href="/output?'
        + escape(urlencode({"path": str(path)}), quote=True)
        + '">'
        + escape(name)
        + "</a></li>"
        for name, path in entries
    )
    title = escape(str(directory))
    return (
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{title}</title>"
        "<style>body{font:14px system-ui;margin:32px;background:#fdf6e3;color:#657b83;}"
        "h1{font-size:18px;overflow-wrap:anywhere;}li{margin:10px 0;}"
        "a{color:#268bd2;}</style>"
        f"<h1>{title}</h1>"
        + (
            f'<p><a href="{escape(diff_url, quote=True)}">'
            "View changes since previous capture</a></p>"
            if diff_url is not None
            else ""
        )
        + f"<ul>{links}</ul>"
        + ("" if entries else "<p>This directory is empty.</p>")
        + "</html>"
    ).encode()


@dataclass
class CommandJob:
    """Retain a command and its output while the browser is open."""

    action: str
    process: subprocess.Popen[bytes]
    log: Path
    stopped: bool = False
    observed: bool = False


class CommandActions:
    """Keep process groups and bounded command logs for registered directories."""

    def __init__(self) -> None:
        """Keep command logs outside the package output being compared."""
        self.storage = tempfile.TemporaryDirectory(prefix="perigrafo-browser-actions-")
        self.directories: set[str] = set()
        self.jobs: dict[str, CommandJob] = {}

    def launch(self, directory: str, action: str, command: list[str]) -> dict[str, Any]:
        """Launch a command without a shell and retain its output and process group."""
        if directory not in self.directories:
            msg = "Unknown directory"
            raise ValueError(msg)
        active = self.jobs.get(directory)
        if action == "stop":
            if active is not None and active.process.poll() is None:
                self.stop(active)
            return self.status(directory)
        if active is not None and active.process.poll() is None:
            msg = "A command is already running for this directory"
            raise ValueError(msg)
        log = Path(self.storage.name) / (
            hashlib.sha256(directory.encode()).hexdigest() + ".log"
        )
        with log.open("wb") as output:
            process = subprocess.Popen(  # noqa: S603 - allowlisted package and argument list
                command,
                cwd=directory,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        self.jobs[directory] = CommandJob(action, process, log)
        return self.status(directory)

    def status(self, directory: str) -> dict[str, Any]:
        """Return status and the tail of command output without blocking."""
        if directory not in self.directories:
            msg = "Unknown directory"
            raise ValueError(msg)
        job = self.jobs.get(directory)
        if job is None:
            return {"state": "idle", "output": ""}
        result = job.process.poll()
        with job.log.open("rb") as stream:
            stream.seek(max(0, job.log.stat().st_size - OUTPUT_TEXT_LIMIT))
            output = stream.read(OUTPUT_TEXT_LIMIT).decode(errors="replace")
        state = "running" if result is None else "passed" if result == 0 else "failed"
        return {
            "action": job.action,
            "state": "stopped" if job.stopped else state,
            "exit_code": result,
            "output": output,
        }

    @staticmethod
    def stop(job: CommandJob) -> None:
        """Terminate a command and its descendants, escalating after a short wait."""
        job.stopped = True
        with contextlib.suppress(ProcessLookupError):
            os.killpg(job.process.pid, signal.SIGTERM)
        try:
            job.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(job.process.pid, signal.SIGKILL)
            job.process.wait()

    def close(self) -> None:
        """Stop active commands before removing session logs."""
        for job in self.jobs.values():
            if job.process.poll() is None:
                self.stop(job)
        self.storage.cleanup()


class PackageActions(CommandActions):
    """Construct run and check commands for packages discovered by the browser."""

    def __init__(self) -> None:
        """Register packages separately from their command process state."""
        super().__init__()
        self.packages: dict[str, tuple[Path, str, bool, bool]] = {}
        self.results: dict[str, Path] = {}

    def package_output(self, package: str) -> Path | None:
        """Resolve the output of the latest successful build in this session."""
        result = self.results.get(package)
        if result is None or not result.is_symlink():
            return None
        output = result.resolve(strict=True)
        return output if output.is_dir() else None

    def output_roots(self) -> set[Path]:
        """Allow browsing only check outputs registered by this browser."""
        return {
            output
            for package in self.results
            if (output := self.package_output(package)) is not None
        }

    def observe(self, data: dict[str, Any]) -> None:
        """Advertise actions for existing packages and their declared checks."""
        if "root" in data:
            directory = str(Path(data["root"]))
            self.directories.add(directory)
            data["actions"] = {"directory": directory, "check": True, "package": False}
        for record in data.get("nodes", []):
            if (
                record["kind"] != "package"
                or record.get("change") == "removed"
                or not record.get("directory")
            ):
                continue
            repository = Path(record["directory"])
            package = repository / record["path"]
            if not package.is_dir():
                continue
            check = (repository / "checks" / record["name"] / "default.nix").is_file()
            kind = record.get("package_type", "nix")
            default = package / "default.nix"
            runnable = default.is_file() and (
                kind == "html"
                or bool(re.search(r"\bmainProgram\s*=", read_text(default)))
            )
            self.packages[str(package)] = repository, record["name"], check, runnable
            self.directories.add(str(package))
            record["actions"] = {
                "directory": str(package),
                "build": default.is_file(),
                "run": runnable,
                "check": check,
                "package": True,
            }
            output = self.package_output(str(package))
            if output is not None:
                record["output_directory"] = str(output)
                children = record["tree"]["children"]
                children[:] = [child for child in children if child["field"] != "tmp"]
                children.append(
                    serialize_node(
                        TreeNode(
                            "tmp/",
                            directory=output,
                            expandable=True,
                            field="tmp",
                        ),
                    ),
                )

    def start(self, package: str, action: str, arguments: str = "") -> dict[str, Any]:
        """Start an argument-list command, or stop the active process group."""
        tests = {
            entry["command"]
            for entry in command_catalog()
            if entry["command"].startswith("test ")
        }
        if package in self.directories and package not in self.packages:
            if action not in {"check", "stop"}:
                msg = "Unknown directory action"
                raise ValueError(msg)
            return self.launch(
                package,
                action,
                [] if action == "stop" else ["perigrafo", "check"],
            )
        if package not in self.packages or action not in {
            "check",
            "build",
            "run",
            "stop",
            *tests,
        }:
            msg = "Unknown package or action"
            raise ValueError(msg)
        repository, name, check, runnable = self.packages[package]
        if action == "run" and not runnable:
            msg = "This package has no declared executable"
            raise ValueError(msg)
        if action == "build" and not (Path(package) / "default.nix").is_file():
            msg = "This package has no build definition"
            raise ValueError(msg)
        if action == "stop":
            command = []
        elif action in tests:
            command = ["perigrafo", *action.split(), *shlex.split(arguments)]
        elif action in {"check", "build"}:
            if action == "check" and not check:
                msg = "This package has no declared check"
                raise ValueError(msg)
            machine = {"arm64": "aarch64"}.get(platform.machine(), platform.machine())
            system = f"{machine}-{platform.system().lower()}"
            result = Path(self.storage.name) / (
                hashlib.sha256(package.encode()).hexdigest() + "-check"
            )
            self.results[package] = result
            command = [
                "nix",
                "build",
                "--out-link",
                str(result),
                "--print-build-logs",
                f"{repository}#checks.{system}.{json.dumps(name)}"
                if action == "check"
                else f"{repository}#{name}",
            ]
        else:
            command = [
                "nix",
                "run",
                f"{repository}#{name}",
                "--",
                *shlex.split(arguments),
            ]
        return self.launch(package, action, command)


class PerigrafoActions(CommandActions):
    """Expose the Perigrafo CLI in directories visited during this session."""

    def observe(self, data: dict[str, Any]) -> None:
        """Register the selected directory and publish the shared command catalog."""
        directory = Path(data["root"])
        self.directories.add(str(directory))
        data["commands"] = command_catalog()

    def start(self, directory: str, action: str, arguments: str = "") -> dict[str, Any]:
        """Execute a catalog command with CLI arguments in the selected directory."""
        if directory not in self.directories:
            msg = "Unknown directory"
            raise ValueError(msg)
        if action != "stop" and action not in {
            entry["command"] for entry in command_catalog()
        }:
            msg = "Unknown Perigrafo command"
            raise ValueError(msg)
        return self.launch(
            directory,
            action,
            []
            if action == "stop"
            else ["perigrafo", *action.split(), *shlex.split(arguments)],
        )


class ActionRequest(BaseModel):
    """Validate the browser's command parameters without coercing their types."""

    model_config = ConfigDict(extra="forbid")
    directory: StrictStr = ""
    action: StrictStr = ""
    args: StrictStr = ""


def gui_app(root: Path) -> FastAPI:  # noqa: C901, PLR0915
    """Serve the browser with native ASGI routing, responses, and resource cleanup."""
    assets = Path(__file__).parent / "prm"
    outputs = OutputSnapshots()
    actions = PackageActions()
    commands = PerigrafoActions()
    browser = RepositoryBrowser(root)
    lock = Lock()

    def requested_data(
        directory: str | None = None,
        *,
        refresh: bool = False,
    ) -> dict[str, Any]:
        selected = Path(directory or root).resolve()
        if not selected.is_dir():
            msg = f"Directory not found: {selected}"
            raise ValueError(msg)
        with lock:
            data = gui_data(selected, browser=browser, refresh=refresh)
            actions.observe(data)
            outputs.observe(data)
            commands.observe(data)
            return data

    @contextlib.asynccontextmanager
    async def lifespan(_application: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            actions.close()
            commands.close()
            outputs.close()

    application = FastAPI(
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @application.middleware("http")
    async def local_requests(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if request.method == "POST" and request.url.path in {
            "/api/action",
            "/api/command",
        }:
            origin = request.headers.get("Origin")
            port = request.scope["server"][1]
            local_origins = {
                f"http://{host}:{port}" for host in ("127.0.0.1", "localhost")
            }
            if (
                origin is not None
                and (
                    origin not in local_origins
                    or origin != str(request.base_url).rstrip("/")
                )
            ) or request.headers.get("Sec-Fetch-Site") == "cross-site":
                return JSONResponse(
                    {"error": "Local requests only"},
                    status_code=HTTPStatus.FORBIDDEN,
                )
            try:
                length = int(request.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if (
                not 0 < length <= ACTION_BODY_LIMIT
                or request.headers.get("Content-Type") != "application/json"
            ):
                return JSONResponse(
                    {"error": "Expected a bounded JSON request"},
                    status_code=HTTPStatus.BAD_REQUEST,
                )
        response = await call_next(request)
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": (
                    "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                    "img-src 'self' data:; object-src 'none'; frame-ancestors 'none'"
                ),
            },
        )
        return response

    @application.exception_handler(RequestValidationError)
    async def invalid_parameters(
        _request: Request,
        _error: RequestValidationError,
    ) -> JSONResponse:
        return JSONResponse(
            {"error": "Invalid request parameters"},
            status_code=HTTPStatus.BAD_REQUEST,
        )

    @application.get("/api/overview")
    def overview(directory: str | None = None, refresh: str = "0") -> Response:
        try:
            return JSONResponse(requested_data(directory, refresh=refresh == "1"))
        except (PerigrafoError, ValueError, OSError) as exc:
            return JSONResponse(
                {"error": str(exc)},
                status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def action_response(
        runner: PackageActions | PerigrafoActions,
        directory: str,
        parameters: ActionRequest | None = None,
    ) -> Response:
        with lock:
            try:
                state = (
                    runner.start(directory, parameters.action, parameters.args)
                    if parameters is not None
                    else runner.status(directory)
                )
                job = runner.jobs.get(directory)
                if job is not None and state["state"] != "running" and not job.observed:
                    job.observed = True
                    if (
                        runner is actions
                        and directory in actions.packages
                        and state["state"] == "passed"
                        and job.action in {"build", "check"}
                    ):
                        output = actions.package_output(directory)
                        if output is not None:
                            outputs.completed(Path(directory), output)
                return JSONResponse(state)
            except (OSError, ValueError) as exc:
                return JSONResponse(
                    {"error": str(exc)},
                    status_code=HTTPStatus.BAD_REQUEST,
                )

    @application.get("/api/action")
    def package_status(directory: str = "") -> Response:
        return action_response(actions, directory)

    @application.post("/api/action")
    def package_action(parameters: ActionRequest) -> Response:
        return action_response(actions, parameters.directory, parameters)

    @application.get("/api/command")
    def command_status(directory: str = "") -> Response:
        return action_response(commands, directory)

    @application.post("/api/command")
    def command_action(parameters: ActionRequest) -> Response:
        return action_response(commands, parameters.directory, parameters)

    @application.get("/output-diff")
    def output_report(path: str = "", entry: str = "") -> Response:
        with lock:
            comparison = outputs.comparisons.get(Path(path))
            if comparison is None:
                return Response(
                    "Output capture not found",
                    status_code=HTTPStatus.NOT_FOUND,
                )
            try:
                return HTMLResponse(
                    output_diff_page(
                        outputs.entry_report(comparison, entry)
                        if comparison.future is not None and comparison.future.done()
                        else comparison,
                    ),
                )
            except (OSError, ValueError):
                return Response(
                    "Output report not found",
                    status_code=HTTPStatus.NOT_FOUND,
                )

    @application.api_route("/output", methods=["GET", "HEAD"])
    def output(path: str = "") -> Response:
        try:
            output_root, target = output_path(path, roots=actions.output_roots())
            if target.is_dir():
                key = next(
                    (
                        key
                        for key, comparison in outputs.comparisons.items()
                        if output_root in (key, comparison.output)
                    ),
                    None,
                )
                diff_url = (
                    "/output-diff?" + urlencode({"path": str(key)})
                    if key is not None
                    else None
                )
                return HTMLResponse(output_index(output_root, target, diff_url))
            return FileResponse(
                target,
                media_type=mimetypes.guess_type(target.name)[0]
                or "application/octet-stream",
            )
        except (OSError, ValueError):
            return Response("Output not found", status_code=HTTPStatus.NOT_FOUND)

    routes = {
        "/": ("index.html", "text/html"),
        "/script.js": ("script.js", "text/javascript"),
        "/g6.js": (os.environ.get("PERIGRAFO_BROWSER_G6", "g6.js"), "text/javascript"),
        "/icons.js": (
            os.environ.get("PERIGRAFO_BROWSER_ICONS", "icons.js"),
            "text/javascript",
        ),
        "/style.css": ("style.css", "text/css"),
    }

    def asset(request: Request) -> Response:
        filename, media_type = routes[request.url.path]
        return FileResponse(assets / filename, media_type=media_type)

    for route in routes:
        application.add_api_route(route, asset, methods=["GET", "HEAD"])
    return application


def open_gui(root: Path, *, port: int, open_browser: bool) -> None:
    """Run the loopback ASGI server and open its actual listening port."""
    config = uvicorn.Config(
        gui_app(root),
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
    )
    with config.bind_socket() as connection:
        connection.listen(config.backlog)
        url = f"http://127.0.0.1:{connection.getsockname()[1]}"
        print(f"Perigrafo browser: {url}\nPress Ctrl+C to stop.", flush=True)  # noqa: T201
        if open_browser:
            webbrowser.open(url)
        with contextlib.suppress(KeyboardInterrupt):
            uvicorn.Server(config).run(sockets=[connection])


def main(argv: list[str] | None = None) -> None:
    """Open the web diagram at the selected working directory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "directory",
        nargs="?",
        type=Path,
        default=Path(),
        help="initial directory (default: current directory)",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="print the URL without opening a web browser",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="loopback port (default: 8765; 0 chooses a free port)",
    )
    args = parser.parse_args(argv)
    if not 0 <= args.port <= MAX_PORT:
        parser.error("--port must be between 0 and 65535")
    if not args.directory.is_dir():
        parser.error(f"Directory not found: {args.directory}")
    try:
        open_gui(
            args.directory.resolve(),
            port=args.port,
            open_browser=not args.no_open,
        )
    except OSError as exc:
        parser.exit(1, f"Browser error: {exc}\n")


if __name__ == "__main__":
    main()

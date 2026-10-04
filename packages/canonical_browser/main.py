#!/usr/bin/env python3
# Copyright (c) 2026- Paschalis Bizopoulos
"""Browse Canonical directories, packages, hosts, and changes in a web diagram."""

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
from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from difflib import unified_diff
from functools import cache
from html import escape
from http import HTTPStatus
from pathlib import Path
from threading import Lock
from typing import Any, Self
from urllib.parse import urlencode

import uvicorn
from canonical import (
    CliEntry,
    ResourceData,
    canonical_root,
    command_catalog,
    overview_data,
    repository_type,
    source_resource_data,
)
from canonical import CommandError as CanonicalError
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, StrictStr

MAX_PORT = 65535
MOUNT_FIELDS = 3
OUTPUT_DIFF_TIMEOUT = 120
OUTPUT_TEXT_LIMIT = 65536
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
    """Keep one package's previous capture fixed for a browser session."""

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


def copy_output_file(source: str, destination: str) -> str:
    """Copy file contents without reading devices or changing the live output."""
    if not stat.S_ISREG(Path(source).lstat().st_mode):
        msg = f"Cannot capture a special output file: {source}"
        raise ValueError(msg)
    return shutil.copyfile(source, destination)


def copy_output(source: Path, destination: Path) -> None:
    """Capture a directory or its absence, preserving links without following them."""
    if source.is_symlink() or (source.exists() and not source.is_dir()):
        msg = f"Not a regular output directory: {source}"
        raise ValueError(msg)
    if source.is_dir():
        shutil.copytree(
            source,
            destination,
            symlinks=True,
            copy_function=copy_output_file,
        )
    else:
        destination.mkdir()


def compare_output(
    previous: Path,
    current: Path,
    *,
    entry: str = "",
    report: str = "report.html",
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
        try:
            result = process.wait(timeout=OUTPUT_DIFF_TIMEOUT)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise
    if result not in {0, 1}:
        diagnostic = read_text(current / "diffoscope.log")[-2000:]
        msg = f"diffoscope failed (exit {result}): {diagnostic}"
        raise ValueError(msg)
    if result == 1 and not (current / report).is_file():
        msg = "diffoscope did not produce an HTML report"
        raise ValueError(msg)
    return result == 1


class OutputSnapshots:
    """Capture package output in the background once per launch or explicit refresh."""

    def __init__(self) -> None:
        """Serialize capture jobs and hold package locks until the browser closes."""
        self.comparisons: dict[Path, OutputComparison] = {}
        self.reports: dict[tuple[Path, Path, str], OutputComparison] = {}
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.locks = contextlib.ExitStack()

    def __enter__(self) -> Self:
        """Keep snapshots available throughout the session."""
        return self

    def __exit__(self, *_: object) -> None:
        """Finish the active capture before releasing its history lock."""
        self.close()

    def close(self) -> None:
        """Stop queued jobs and release package histories."""
        self.executor.shutdown(wait=True, cancel_futures=True)
        self.locks.close()

    def observe(self, data: dict[str, Any], *, refresh: bool = False) -> None:
        """Link reports and capture newly visited or refreshed packages."""
        for record in data.get("nodes", []):
            if record["kind"] != "package":
                continue
            package = Path(record["directory"]) / record["path"]
            output = package / "tmp"
            store = package.parent.parent / "tmp" / "canonical_browser" / package.name
            if output not in self.comparisons:
                if not output.is_dir() and not (store / "latest").is_file():
                    continue
                comparison = OutputComparison(output, store)
                self.comparisons[output] = comparison
            else:
                comparison = self.comparisons[output]
            if comparison.future is None or refresh:
                comparison.future = self.executor.submit(self.capture, comparison)
            record["output_diff"] = "/output-diff?" + urlencode({"path": str(output)})
            pending = comparison.future is not None and not comparison.future.done()
            data["output_pending"] = data.get("output_pending", False) or pending
            tree = output_tree(
                output,
                comparison.previous / "output"
                if comparison.previous and not pending
                else None,
                comparison.current / "output"
                if comparison.current and not pending
                else output,
            )
            if pending:
                message = "Capturing and comparing output…"
            elif comparison.error:
                message = comparison.error
            elif comparison.previous is None:
                message = (
                    "Initial capture saved; compare after the next browser launch."
                )
            else:
                message = "Previous capture → Current capture"
            tree.children.insert(0, TreeNode(message, warning=bool(comparison.error)))
            children = record["tree"]["children"]
            children[:] = [child for child in children if child["field"] != "tmp"]
            children.append(serialize_node(tree))

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

    @staticmethod
    def compare_entry(comparison: OutputComparison) -> None:
        """Generate a report for one entry without recapturing live output."""
        if comparison.previous is None or comparison.current is None:
            return
        try:
            comparison.changed = compare_output(
                comparison.previous,
                comparison.current,
                entry=comparison.entry,
                report=comparison.report,
            )
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
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
                    "Output history is in use by another browser. Close it and Refresh."
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

    def capture(self, comparison: OutputComparison) -> None:
        """Publish complete copies, retain two captures, and compare their contents."""
        capture: Path | None = None
        comparison.error = ""
        comparison.changed = False
        try:
            self.initialize(comparison)
            capture = Path(tempfile.mkdtemp(prefix="capture-", dir=comparison.store))
            copy_output(comparison.output, capture / "output")
            (capture / "timestamp").write_text(datetime.now(UTC).isoformat())
            latest = capture / "latest"
            latest.write_text(capture.name)
            latest.replace(comparison.store / "latest")
            comparison.current = capture
            for old in comparison.store.glob("capture-*"):
                if old not in {comparison.previous, capture} and not old.is_symlink():
                    shutil.rmtree(old)
            if comparison.previous is not None:
                comparison.changed = compare_output(comparison.previous, capture)
        except subprocess.TimeoutExpired:
            comparison.error = f"diffoscope exceeded {OUTPUT_DIFF_TIMEOUT} seconds"
        except (OSError, ValueError) as exc:
            comparison.error = str(exc)
        finally:
            if capture is not None and capture != comparison.current:
                shutil.rmtree(capture, ignore_errors=True)


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
    elif comparison.previous is None:
        message = (
            "Initial capture saved. "
            "Changes will be available after the next browser launch."
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
        + "<style>body{font:14px system-ui;margin:32px;color:#202e29;}"
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
        self.snapshot = scope_snapshot(deepcopy(self.source_snapshot(root)), self.cwd)


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
        data.get("schema") != "canonical.overview"
        or type(version) is not int
        or version != 1
    ):
        msg = (
            f"Unsupported Canonical overview schema: {data.get('schema')!r}, "
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


def directory_disk_size(directory: Path) -> int:
    """Measure allocated bytes once per inode without following symbolic links."""
    seen = set()
    total = 0

    def failed(error: OSError) -> None:
        raise error

    for parent, folders, files in os.walk(directory, followlinks=False, onerror=failed):
        for path in [Path(parent), *(Path(parent) / name for name in folders + files)]:
            metadata = path.lstat()
            identity = metadata.st_dev, metadata.st_ino
            if identity not in seen:
                seen.add(identity)
                total += getattr(metadata, "st_blocks", 0) * 512
    return total


def format_bytes(count: int) -> str:
    """Format storage amounts with binary units."""
    size = float(count)
    unit_bytes = 1024
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < unit_bytes or unit == "TiB":
            return f"{size:g} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= unit_bytes
    return ""


def package_storage(directory: Path) -> list[TreeNode]:
    """Describe optional tracked storage and link to existing runtime output."""
    nodes: list[TreeNode] = []
    resources = directory / "prm"
    if resources.is_dir() and not resources.is_symlink():
        try:
            size = format_bytes(directory_disk_size(resources))
        except OSError:
            size = "unavailable"
        nodes.append(TreeNode(f"prm/: {size}"))
    output = directory / "tmp"
    if output.is_dir() and not output.is_symlink():
        nodes.append(output_tree(output, None, output))
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


def output_tree(output: Path, previous: Path | None, current: Path) -> TreeNode:
    """Merge output entries into a collapsible tree with captured changes."""

    def build(relative: Path) -> TreeNode:
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
                _, node.directory = output_path(str(output / relative))
        if directory:
            names: set[str] = set()
            for path in (old, new):
                if output_kind(path) == "directory" and path is not None:
                    names.update(child.name for child in path.iterdir())
            node.children = [build(relative / name) for name in sorted(names)]
        if previous is not None:
            node.change = output_change(old, new)
            if node.change or any(
                child.change or child.output_diff for child in node.children
            ):
                node.output_diff = "/output-diff?" + urlencode(
                    {
                        "path": str(output),
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
    except CanonicalError:
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
    """Show directory containers outside a Canonical repository."""
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
    root = Path(data["root"])
    if not data["nodes"] or all(
        record["kind"] == "directory" for record in data["nodes"]
    ):
        return data
    nodes = []
    for record in data["nodes"]:
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


def output_path(requested: str) -> tuple[Path, Path]:
    """Resolve a requested output entry within a conventional package tmp directory."""
    path = Path(requested)
    root = next(
        (
            parent
            for parent in (path, *path.parents)
            if parent.name == "tmp" and parent.parent.parent.name == "packages"
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
            output_path(str(child))
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
        "<style>body{font:14px system-ui;margin:32px;color:#202e29;}"
        "h1{font-size:18px;overflow-wrap:anywhere;}li{margin:10px 0;}"
        "a{color:#276850;}</style>"
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
class PackageAction:
    """Retain a command and its output while the browser is open."""

    action: str
    process: subprocess.Popen[bytes]
    log: Path
    stopped: bool = False
    observed: bool = False


class PackageActions:
    """Run only packages discovered by this browser, with bounded output reads."""

    def __init__(self) -> None:
        """Keep command logs outside the package output being compared."""
        self.storage = tempfile.TemporaryDirectory(prefix="canonical-browser-actions-")
        self.packages: dict[str, tuple[Path, str, bool]] = {}
        self.jobs: dict[str, PackageAction] = {}

    def observe(self, data: dict[str, Any]) -> None:
        """Advertise actions for existing packages and their declared checks."""
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
            self.packages[str(package)] = repository, record["name"], check
            record["actions"] = {"package": str(package), "check": check}

    def start(self, package: str, action: str, arguments: str = "") -> dict[str, Any]:
        """Start an argument-list command, or stop the active process group."""
        tests = {
            entry["command"]
            for entry in command_catalog()
            if entry["command"].startswith("test ")
        }
        if package not in self.packages or action not in {
            "check",
            "run",
            "stop",
            *tests,
        }:
            msg = "Unknown package or action"
            raise ValueError(msg)
        active = self.jobs.get(package)
        if action == "stop":
            if active is not None and active.process.poll() is None:
                self.stop(active)
            return self.status(package)
        if active is not None and active.process.poll() is None:
            msg = "A command is already running for this package"
            raise ValueError(msg)
        repository, name, check = self.packages[package]
        if action in tests:
            command = ["canonical", *action.split(), *shlex.split(arguments)]
        elif action == "check":
            if not check:
                msg = "This package has no declared check"
                raise ValueError(msg)
            machine = {"arm64": "aarch64"}.get(platform.machine(), platform.machine())
            system = f"{machine}-{platform.system().lower()}"
            command = [
                "nix",
                "build",
                "--no-link",
                "--print-build-logs",
                f"{repository}#checks.{system}.{json.dumps(name)}",
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

    def launch(self, package: str, action: str, command: list[str]) -> dict[str, Any]:
        """Launch a command without a shell and retain its output and process group."""
        log = Path(self.storage.name) / (
            hashlib.sha256(package.encode()).hexdigest() + ".log"
        )
        with log.open("wb") as output:
            process = subprocess.Popen(  # noqa: S603 - allowlisted package and argument list
                command,
                cwd=package,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        self.jobs[package] = PackageAction(action, process, log)
        return self.status(package)

    def status(self, package: str) -> dict[str, Any]:
        """Return status and the tail of command output without blocking."""
        if package not in self.packages:
            msg = "Unknown package"
            raise ValueError(msg)
        job = self.jobs.get(package)
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
    def stop(job: PackageAction) -> None:
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


class CanonicalActions(PackageActions):
    """Expose the Canonical CLI in directories visited during this session."""

    def observe(self, data: dict[str, Any]) -> None:
        """Register the selected directory and publish the shared command catalog."""
        directory = Path(data["root"])
        self.packages[str(directory)] = directory, "", False
        data["commands"] = command_catalog()

    def start(self, package: str, action: str, arguments: str = "") -> dict[str, Any]:
        """Execute a catalog command with CLI arguments in the selected directory."""
        if package not in self.packages:
            msg = "Unknown directory"
            raise ValueError(msg)
        if action == "stop":
            job = self.jobs.get(package)
            if job is not None and job.process.poll() is None:
                self.stop(job)
            return self.status(package)
        if action not in {entry["command"] for entry in command_catalog()}:
            msg = "Unknown Canonical command"
            raise ValueError(msg)
        active = self.jobs.get(package)
        if active is not None and active.process.poll() is None:
            msg = "A command is already running for this directory"
            raise ValueError(msg)
        return self.launch(
            package,
            action,
            ["canonical", *action.split(), *shlex.split(arguments)],
        )


class ActionRequest(BaseModel):
    """Validate the browser's command parameters without coercing their types."""

    model_config = ConfigDict(extra="forbid")
    package: StrictStr = ""
    action: StrictStr = ""
    args: StrictStr = ""


def gui_app(root: Path) -> FastAPI:  # noqa: C901, PLR0915
    """Serve the browser with native ASGI routing, responses, and resource cleanup."""
    assets = Path(__file__).parent / "prm"
    outputs = OutputSnapshots()
    actions = PackageActions()
    commands = CanonicalActions()
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
            outputs.observe(data, refresh=refresh)
            actions.observe(data)
            commands.observe(data)
            return data

    @contextlib.asynccontextmanager
    async def lifespan(_application: FastAPI) -> AsyncIterator[None]:
        try:
            requested_data()
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
            if (
                origin is not None and origin != f"http://127.0.0.1:{port}"
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
        except (CanonicalError, ValueError, OSError) as exc:
            return JSONResponse(
                {"error": str(exc)},
                status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def action_response(
        runner: PackageActions,
        package: str,
        parameters: ActionRequest | None = None,
    ) -> Response:
        with lock:
            try:
                state = (
                    runner.start(package, parameters.action, parameters.args)
                    if parameters is not None
                    else runner.status(package)
                )
                job = runner.jobs.get(package)
                if job is not None and state["state"] != "running" and not job.observed:
                    job.observed = True
                    comparison = outputs.comparisons.get(Path(package) / "tmp")
                    if comparison is not None:
                        comparison.future = outputs.executor.submit(
                            outputs.capture,
                            comparison,
                        )
                return JSONResponse(state)
            except (OSError, ValueError) as exc:
                return JSONResponse(
                    {"error": str(exc)},
                    status_code=HTTPStatus.BAD_REQUEST,
                )

    @application.get("/api/action")
    def package_status(package: str = "") -> Response:
        return action_response(actions, package)

    @application.post("/api/action")
    def package_action(parameters: ActionRequest) -> Response:
        return action_response(actions, parameters.package, parameters)

    @application.get("/api/command")
    def command_status(package: str = "") -> Response:
        return action_response(commands, package)

    @application.post("/api/command")
    def command_action(parameters: ActionRequest) -> Response:
        return action_response(commands, parameters.package, parameters)

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
                    output_diff_page(outputs.entry_report(comparison, entry)),
                )
            except (OSError, ValueError):
                return Response(
                    "Output report not found",
                    status_code=HTTPStatus.NOT_FOUND,
                )

    @application.api_route("/output", methods=["GET", "HEAD"])
    def output(path: str = "") -> Response:
        try:
            output_root, target = output_path(path)
            if target.is_dir():
                diff_url = (
                    "/output-diff?" + urlencode({"path": str(output_root)})
                    if output_root in outputs.comparisons
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
        "/g6.js": (os.environ.get("CANONICAL_BROWSER_G6", "g6.js"), "text/javascript"),
        "/icons.js": (
            os.environ.get("CANONICAL_BROWSER_ICONS", "icons.js"),
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
        print(f"Canonical browser: {url}\nPress Ctrl+C to stop.", flush=True)  # noqa: T201
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

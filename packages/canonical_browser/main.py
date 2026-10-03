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
from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from difflib import unified_diff
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Self
from urllib.parse import parse_qs, urlencode, urlsplit

from git_canonical import (
    CliEntry,
    ResourceData,
    canonical_root,
    overview_data,
    profile,
    resource_data,
    source_resource_data,
)
from git_canonical import CommandError as GitCanonicalError

MAX_PORT = 65535
MOUNT_FIELDS = 3
EMPTY_ENTRIES = {"(none)", "(not applicable)", "(not declared)"}
OUTPUT_DIFF_TIMEOUT = 120
OUTPUT_TEXT_LIMIT = 65536
ACTION_BODY_LIMIT = 16384


@dataclass
class TreeNode:  # noqa: D101
    title: str
    children: list["TreeNode"] | None = None
    change: str | None = None
    warning: bool = False
    resource_id: str | None = None
    directory: Path | None = None
    source_file: bool = False
    output_diff: str | None = None
    text_diff: str | None = None
    expandable: bool = False


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
            tree.children = [
                TreeNode(message, warning=bool(comparison.error)),
                *(tree.children or []),
            ]
            children = record["tree"]["children"]
            children[:] = [child for child in children if child["title"] != "tmp/"]
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
    """Read directory-scoped Canonical data with cached source snapshots."""

    def __init__(self, cwd: str | Path | None = None) -> None:
        """Start browsing at the given directory."""
        self.cwd = Path(cwd or Path.cwd()).resolve()
        self.snapshot: dict[str, Any] = {}
        self.status = ""
        self.source_snapshots: dict[Path, tuple[dict[str, Any], str]] = {}

    def refresh(self) -> None:
        """Discard cached declarations and rebuild the current snapshot."""
        self.source_snapshots.clear()
        self.load()

    def load(self) -> None:
        """Scope cached declarations to the current directory."""
        root = browser_root(self.cwd)
        if root not in self.source_snapshots:
            snapshot, _ = browser_snapshot(root)
            self.source_snapshots[root] = deepcopy(snapshot), ""
        snapshot, self.status = deepcopy(self.source_snapshots[root])
        self.snapshot, _ = scope_snapshot(snapshot, self.cwd)

    def package_entries(self, *, diff: bool = False) -> list[TreeNode]:
        """Build a collapsible package tree with high-level changes."""
        try:
            current = overview_data(self.cwd)
            previous = overview_data(self.cwd, revision="HEAD")
        except GitCanonicalError as exc:
            self.status = str(exc)
            return []
        validate_overview(current)
        validate_overview(previous)
        records = merge_overviews(current, previous)["nodes"]
        tree: dict[str, Any] = {}
        for record in records:
            if record["kind"] != "package":
                continue
            entry = self.resource_entry(record)
            if diff:
                entry.children = self.changed_nodes(entry.children or [])
                if not entry.children and entry.change is None:
                    continue
            branch = tree
            for part in Path(record["repository"]).parts:
                branch = branch.setdefault(part, {})
            branch.setdefault("", []).append(entry)

        def nodes(branch: dict[str, Any]) -> list[TreeNode]:
            result = []
            for name, children in sorted(branch.items()):
                if name == "":
                    result.extend(children)
                else:
                    result.append(TreeNode(name, nodes(children)))
            return result

        return nodes(tree)

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
        current = resource_data(directory)
        if not diff:
            return TreeNode(
                f"packages/{name}",
                self.details_tree(current) if current["sources"] else None,
            )
        snapshot = overview_data(root, revision="HEAD")
        validate_overview(snapshot)
        previous = next(
            (
                record["details"]
                for record in snapshot["nodes"]
                if record["id"] == f".:packages/{name}"
            ),
            source_resource_data(name, {}),
        )
        summary_tree = self.merged_details_tree(previous, current)
        if only_changes:
            summary_tree = self.changed_nodes(summary_tree)
            if not summary_tree:
                return None
        return TreeNode(f"packages/{name}", summary_tree or None)

    @classmethod
    def resource_entry(cls, record: dict[str, Any]) -> TreeNode:
        """Render a resource identity and its structured source comparison."""
        current = record.get(
            "details",
            source_resource_data(record["name"], {}, path=record["path"]),
        )
        previous = record.get("previous_details")
        children = (
            cls.merged_details_tree(previous, current)
            if previous is not None
            else cls.details_tree(current)
        )
        return TreeNode(record["path"], children, change=record.get("change"))

    @classmethod
    def changed_nodes(cls, nodes: list[TreeNode]) -> list[TreeNode]:
        """Keep changed fields and their ancestors in a diff-only tree."""
        result = []
        for node in nodes:
            children = (
                cls.changed_nodes(node.children) if node.children is not None else None
            )
            if node.change is not None or children:
                result.append(replace(node, children=children))
        return result

    @staticmethod
    def details_tree(
        data: ResourceData,
        *,
        cli: list[CliEntry] | None = None,
    ) -> list[TreeNode]:
        """Render source facts without parsing the terminal overview."""
        return RepositoryBrowser.merged_details_tree(data, data, current_cli=cli)

    @classmethod
    def merged_details_tree(
        cls,
        previous: ResourceData,
        current: ResourceData,
        *,
        previous_cli: list[CliEntry] | None = None,
        current_cli: list[CliEntry] | None = None,
    ) -> list[TreeNode]:
        """Show changes to structured facts at their existing field positions."""
        result = []
        for field, old, new, fallback in (
            ("Name", previous["name"], current["name"], ""),
            (
                "Description",
                previous["description"],
                current["description"],
                "(not declared)",
            ),
            (
                "Help",
                previous["help"],
                current["help"],
                "(module docstring not declared)",
            ),
        ):
            before = f"{field}: {old or fallback}" if previous["sources"] else None
            after = f"{field}: {new or fallback}" if current["sources"] else None
            if before == after:
                if after is not None:
                    result.append(TreeNode(after))
            else:
                if before is not None:
                    result.append(TreeNode(f"- {before}", change="removed"))
                if after is not None:
                    result.append(TreeNode(f"+ {after}", change="added"))
        for group in ("Arguments", "Dependencies", "Tests", "Suppressions"):
            old_entries = cls.detail_group(previous, group)
            new_entries = cls.detail_group(current, group)
            children = [
                TreeNode(f"- {item}", change="removed")
                for item in old_entries
                if item not in new_entries
            ]
            children.extend(
                TreeNode(
                    item if item in old_entries else f"+ {item}",
                    change=None if item in old_entries else "added",
                    warning=item.startswith("(unavailable:"),
                )
                for item in new_entries
            )
            if group == "Suppressions":
                children = cls.suppression_tree(previous, current)
            if group == "Arguments":
                children = cls.cli_tree(
                    cls.cli_entries(current) if current_cli is None else current_cli,
                    previous=cls.cli_entries(previous)
                    if previous_cli is None
                    else previous_cli,
                )
            children = cls.declared_children(children)
            if children:
                result.append(TreeNode(group, children))
        return result

    @staticmethod
    def declared_children(children: list[TreeNode]) -> list[TreeNode]:
        """Omit empty declarations while retaining diagnostics and actual changes."""
        return [
            child
            for child in children
            if child.title.removeprefix("- ").removeprefix("+ ") not in EMPTY_ENTRIES
        ]

    @classmethod
    def suppression_tree(
        cls,
        previous: ResourceData,
        current: ResourceData,
    ) -> list[TreeNode]:
        """Group suppression counts and their changes beneath each source filename."""

        def counts(data: ResourceData) -> dict[tuple[str, str, str], int]:
            return {
                (source["path"], item["kind"], item["scope"]): item["count"]
                for source in data["sources"]
                for item in source["suppressions"]
            }

        before, after = counts(previous), counts(current)
        files: dict[str, list[TreeNode]] = {}
        for key in sorted(before.keys() | after.keys()):
            old, new = before.get(key, 0), after.get(key, 0)
            filename, kind, scope = key
            kind = f"{kind} ({scope})"
            children = files.setdefault(filename, [])
            if old == new:
                children.append(TreeNode(f"{kind}: {new}"))
            else:
                change = "added" if old == 0 else "removed" if new == 0 else "modified"
                children.append(TreeNode(f"{kind}: {old} → {new}", change=change))
        return [
            TreeNode(filename, children) for filename, children in files.items()
        ] or [
            TreeNode("(none)"),
        ]

    @staticmethod
    def cli_tree(
        current: list[CliEntry],
        *,
        previous: list[CliEntry] | None = None,
    ) -> list[TreeNode]:
        """Nest CLI entries by their source-discovered command paths."""
        roots: list[TreeNode] = []
        commands: dict[tuple[str, ...], TreeNode] = {}
        old = set(previous or [])
        new = set(current)
        entries: list[tuple[CliEntry, str | None]] = [
            (entry, "removed") for entry in previous or [] if entry not in new
        ]
        entries.extend(
            (entry, "added" if previous is not None and entry not in old else None)
            for entry in current
        )
        for entry, change in entries:
            children = roots
            for depth, name in enumerate(entry.path, 1):
                path = entry.path[:depth]
                if path not in commands:
                    node = TreeNode(name, [])
                    commands[path] = node
                    children.append(node)
                node = commands[path]
                children = node.children if node.children is not None else []
            prefix = "" if change is None else {"removed": "- ", "added": "+ "}[change]
            if entry.command:
                node.title = prefix + entry.path[-1]
                node.change = change
            else:
                children.append(
                    TreeNode(
                        prefix + entry.text,
                        change=change,
                        warning=entry in new and entry.text.startswith("(unavailable:"),
                    ),
                )
        return roots

    @staticmethod
    def cli_entries(data: ResourceData) -> list[CliEntry]:
        """Adapt the shared command records to the browser's nested rendering."""
        if error := data["diagnostics"].get("cli"):
            return [CliEntry((), f"(unavailable: {error})")]
        return [
            CliEntry(tuple(row["path"]), row["text"], row["command"])
            for row in data["cli"]
        ]

    @staticmethod
    def detail_group(data: ResourceData, name: str) -> list[str]:
        """Format a structured group only when creating display nodes."""
        key = {
            "Arguments": "cli",
            "Dependencies": "dependencies",
            "Tests": "tests",
        }.get(name)
        if key and (error := data["diagnostics"].get(key)):
            return [f"(unavailable: {error})"]
        if name == "Arguments":
            return [entry.render() for entry in RepositoryBrowser.cli_entries(data)]
        if name == "Dependencies":
            return [
                f"{item['kind']}: {item['target']}" for item in data["dependencies"]
            ]
        return data["tests"] if name == "Tests" else []

    @staticmethod
    def package_data(name: str, files: dict[str, str]) -> ResourceData:
        """Use the backend's structured analysis for source snapshots."""
        return source_resource_data(name, files)

    @classmethod
    def has_warning(cls, node: TreeNode) -> bool:
        """Show diagnostics even when their parent groups are collapsed."""
        return node.warning or any(
            cls.has_warning(child) for child in node.children or []
        )


def serialize_node(node: TreeNode) -> dict[str, Any]:
    """Serialize source details and semantic changes for the web browser."""
    return {
        "title": node.title,
        "change": node.change,
        "warning": node.warning,
        "resource_id": node.resource_id,
        "source_file": node.source_file,
        "output_diff": node.output_diff,
        "text_diff": node.text_diff,
        "expandable": node.expandable,
        "directory": str(node.directory) if node.directory is not None else None,
        "children": [serialize_node(child) for child in node.children or []],
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


def resource_tree(record: dict[str, Any], tree: TreeNode) -> TreeNode:
    """Attach shared machine and OS details to a resource."""
    tree.resource_id = record["id"]
    if record["kind"] == "machine":
        tree.title = f"Machine: {record['name']}"
        tree.children = [TreeNode(detail) for detail in record["details"]]
    elif record["kind"] == "host":
        record["icon"] = "nixos"
        tree.children = [TreeNode("OS: NixOS (configuration)"), *(tree.children or [])]
    elif record["kind"] == "repository":
        tree.title = record["repository"]
        tree.children = [TreeNode(f"Profile: {record.get('profile', 'flake')}")]
    return tree


def package_sources(directory: Path) -> list[TreeNode]:
    """Render the backend's conventional source inventory."""
    return source_nodes(resource_data(directory))


def source_nodes(data: ResourceData) -> list[TreeNode]:
    """Convert physical source records into browser detail rows."""
    result = []
    for source in data["sources"]:
        if source["diagnostic"]:
            children = [
                TreeNode(f"(unavailable: {source['diagnostic']})", warning=True),
            ]
        else:
            children = [TreeNode(f"Lines: {source['lines']}")]
            children.extend(
                TreeNode(f"{item['kind']} ({item['scope']}): {item['count']}")
                for item in source["suppressions"]
            )
        result.append(TreeNode(source["path"], children, source_file=True))
    return result


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
        node = TreeNode(title, expandable=directory)
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
                child.change or child.output_diff for child in node.children or []
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


def ordered_source(source: TreeNode, documentation: list[TreeNode]) -> TreeNode:
    """Place source documentation and counters before interfaces and dependencies."""
    children = sorted(
        source.children or [],
        key=lambda child: (
            child not in documentation,
            not child.title.startswith("Lines:"),
            not re.search(r"\((?:local|global)\):", child.title),
        ),
    )
    return replace(source, children=children)


def package_file_tree(  # noqa: C901 - move each declaration to its source file
    directory: Path,
    tree: TreeNode,
    sources: list[TreeNode] | None = None,
    *,
    dependency_source: str = "default.nix",
) -> TreeNode:
    """Organize declarations beneath their source files, preserving semantic diffs."""
    files = {
        node.title: node
        for node in (package_sources(directory) if sources is None else sources)
    }
    fields = []
    documentation = []

    def source(name: str) -> TreeNode:
        return files.setdefault(name, TreeNode(name, [], source_file=True))

    def attach(name: str, node: TreeNode) -> None:
        parent = source(name)
        parent.children = [
            *(child for child in parent.children or [] if child.title != node.title),
            node,
        ]

    for node in tree.children or []:
        title = node.title.removeprefix("- ").removeprefix("+ ")
        if title.startswith("Language:"):
            continue
        if node.title == "Suppressions":
            for file in node.children or []:
                for suppression in file.children or []:
                    parent = source(file.title)
                    key = suppression.title.partition("):")[0]
                    children = parent.children or []
                    matching = next(
                        (
                            index
                            for index, child in enumerate(children)
                            if child.title.partition("):")[0] == key
                        ),
                        None,
                    )
                    if matching is None:
                        children.append(suppression)
                    else:
                        children[matching] = suppression
                    parent.children = children
        elif node.title in {"Arguments", "Tests", "Dependencies"}:
            name = {
                "Arguments": "main.py",
                "Tests": "test_main.py",
                "Dependencies": dependency_source,
            }[node.title]
            attach(name, node)
        elif title.startswith("Help:"):
            if title != "Help: (module docstring not declared)":
                node.title = node.title.replace("Help: ", "", 1)
                documentation.append(node)
        else:
            fields.append(node)
    if documentation:
        parent = source("main.py")
        parent.children = [*documentation, *(parent.children or [])]
    tree.children = [
        *fields,
        *(ordered_source(files[name], documentation) for name in sorted(files)),
    ]
    tree.children.extend(package_storage(directory))
    return tree


def browser_snapshot(
    root: Path,
) -> tuple[dict[str, Any], list[TreeNode]]:
    """Build the machine and repository model for the web browser."""
    try:
        current = overview_data(root)
    except GitCanonicalError:
        if profile(root, "directory") != "directory":
            raise
        return directory_snapshot(root)
    validate_overview(current)
    previous = overview_data(root, revision="HEAD")
    data = merge_overviews(current, previous)
    resources: dict[str, TreeNode] = {}
    machine = machine_resource()
    data["machine"] = machine
    data["nodes"].insert(0, machine)
    data["edges"].extend(
        {"source": machine["id"], "target": node["id"], "kind": "hostname-match"}
        for node in data["nodes"]
        if node["kind"] == "host" and node["name"] == machine["name"]
    )
    for record in data["nodes"]:
        identifier = record["id"]
        tree = resource_tree(
            record,
            RepositoryBrowser.resource_entry(record)
            if record["kind"] in {"package", "host", "check"}
            else TreeNode(record["path"], [], change=record.get("change")),
        )
        if record["kind"] in {"package", "host", "check"}:
            directory = root / record["repository"] / record["path"]
            details = record["details"]
            record["source_metrics"] = details["source_metrics"]
            tree = package_file_tree(
                directory,
                tree,
                source_nodes(details),
                dependency_source=details["dependency_source"],
            )
        links = [
            TreeNode(
                f"{edge['kind']}: {edge['source']} → {edge['target']}",
                change=edge.get("change"),
            )
            for edge in data["edges"]
            if edge["target"] == identifier
            and edge["kind"] not in {"contains", "submodule"}
        ]
        if links:
            tree.children = [*(tree.children or []), TreeNode("Connections", links)]
        resources[identifier] = tree
        record["tree"] = serialize_node(tree)
    result = [resources[machine["id"]]]
    for record in data["nodes"]:
        if record["kind"] != "repository":
            continue
        tree = resources[record["id"]]
        tree.children = [
            *(tree.children or []),
            *[
                resources[node["id"]]
                for node in data["nodes"]
                if node["repository"] == record["repository"]
                and node["kind"] not in {"machine", "repository"}
            ],
        ]
        result.append(tree)
    data["tree"] = [serialize_node(node) for node in result]
    data["root"] = str(root)
    return data, result


def gui_data(root: Path) -> dict[str, Any]:
    """Return a directory-scoped machine and repository snapshot."""
    viewer = RepositoryBrowser(root)
    viewer.refresh()
    data = viewer.snapshot
    data["warning"] = viewer.status
    return data


def browser_root(directory: Path) -> Path:
    """Find the nearest Canonical source root for a directory-scoped view."""
    return canonical_root(directory)


def browser_parent(directory: Path) -> Path | None:
    """Stop upward navigation at home or the filesystem root."""
    if directory == Path.home().resolve() or directory.parent == directory:
        return None
    return directory.parent


def directory_snapshot(directory: Path) -> tuple[dict[str, Any], list[TreeNode]]:
    """Show immediate directory containers outside a Canonical repository."""
    children = [
        child
        for child in sorted(directory.iterdir())
        if child.is_dir() and not child.name.startswith(".")
    ]
    nodes = [TreeNode(child.name, [], directory=child.resolve()) for child in children]
    return {
        "root": str(directory),
        "machine": machine_resource(),
        "parent": str(parent_directory)
        if (parent_directory := browser_parent(directory))
        else None,
        "nodes": [
            {
                "id": f"{child.name}:directory",
                "kind": "repository",
                "repository": child.name,
                "path": ".",
                "profile": "directory",
                "directory": str(child.resolve()),
            }
            for child in children
        ],
        "edges": [],
        "tree": [serialize_node(node) for node in nodes],
    }, nodes


def scope_snapshot(  # noqa: C901, PLR0912 - filter resources and reconstruct containment
    data: dict[str, Any],
    directory: Path,
) -> tuple[dict[str, Any], list[TreeNode]]:
    """Restrict resources to a directory and preserve navigable containment."""
    root = Path(data["root"])
    if not data["nodes"] or all(
        record.get("profile") == "directory" for record in data["nodes"]
    ):
        return data, [
            TreeNode(record["repository"], [], directory=Path(record["directory"]))
            for record in data["nodes"]
        ]
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
    data["parent"] = (
        str(parent_directory)
        if (parent_directory := browser_parent(directory))
        else None
    )
    result: list[TreeNode] = []
    containers: dict[str, TreeNode] = {}

    def container(relative: Path) -> TreeNode:
        key = str(relative)
        if key not in containers:
            node = TreeNode(
                directory.name if key == "." else relative.name,
                [],
                directory=directory / relative,
            )
            containers[key] = node
            siblings = result if key == "." else container(relative.parent).children
            if siblings is not None:
                siblings.append(node)
        return containers[key]

    for record in nodes:
        if record["kind"] == "machine":
            result.append(
                TreeNode(
                    record["tree"]["title"],
                    [TreeNode(detail) for detail in record["details"]],
                ),
            )
            continue
        parent = container(Path(record["repository"]))
        if record["kind"] == "repository":
            continue

        def deserialize(tree: dict[str, Any]) -> TreeNode:
            return TreeNode(
                tree["title"],
                [deserialize(child) for child in tree["children"]],
                change=tree["change"],
                warning=tree["warning"],
                resource_id=tree["resource_id"],
                source_file=tree.get("source_file", False),
                output_diff=tree.get("output_diff"),
                text_diff=tree.get("text_diff"),
                expandable=tree.get("expandable", False),
                directory=Path(tree["directory"]) if tree.get("directory") else None,
            )

        if parent.children is not None:
            parent.children.append(deserialize(record["tree"]))
    data["tree"] = [serialize_node(node) for node in result]
    return data, result


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
        if package not in self.packages or action not in {"check", "run", "stop"}:
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
        if action == "check":
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


def gui_server(  # noqa: C901 - serve graph assets and package runtime output
    root: Path,
    port: int = 0,
) -> HTTPServer:
    """Serve the graph and output reports, retaining captures outside package output."""
    assets = Path(__file__).parent / "prm"
    outputs = OutputSnapshots()
    actions = PackageActions()
    routes = {
        "/": ("index.html", "text/html; charset=utf-8"),
        "/script.js": ("script.js", "text/javascript; charset=utf-8"),
        "/g6.js": (
            os.environ.get("CANONICAL_BROWSER_G6", "g6.js"),
            "text/javascript; charset=utf-8",
        ),
        "/style.css": ("style.css", "text/css; charset=utf-8"),
    }

    def requested_data(query: str) -> dict[str, Any]:
        parameters = parse_qs(query)
        requested = parameters.get("directory", [str(root)])[0]
        directory = Path(requested).resolve()
        if not directory.is_dir():
            msg = f"Directory not found: {directory}"
            raise ValueError(msg)
        data = gui_data(directory)
        outputs.observe(data, refresh=parameters.get("refresh") == ["1"])
        actions.observe(data)
        return data

    class Handler(BaseHTTPRequestHandler):
        def action_parameters(self) -> dict[str, str]:
            """Read a small JSON object of string action parameters."""
            length = int(self.headers.get("Content-Length", "0"))
            if (
                not 0 < length <= ACTION_BODY_LIMIT
                or self.headers.get("Content-Type") != "application/json"
            ):
                msg = "Expected a bounded JSON request"
                raise ValueError(msg)
            parameters = json.loads(self.rfile.read(length))
            if not isinstance(parameters, dict) or not all(
                isinstance(value, str) for value in parameters.values()
            ):
                msg = "Action parameters must be strings"
                raise ValueError(msg)
            return parameters

        def action_response(self, *, start: bool) -> None:
            """Serve local package actions through JSON requests."""
            try:
                if start:
                    origin = self.headers.get("Origin")
                    if (
                        origin is not None
                        and origin != f"http://127.0.0.1:{server.server_port}"
                    ) or self.headers.get("Sec-Fetch-Site") == "cross-site":
                        self.send_error(HTTPStatus.FORBIDDEN, "Local requests only")
                        return
                    parameters = self.action_parameters()
                    package = parameters.get("package", "")
                    state = actions.start(
                        package,
                        parameters.get("action", ""),
                        parameters.get("args", ""),
                    )
                else:
                    package = parse_qs(urlsplit(self.path).query).get("package", [""])[
                        0
                    ]
                    state = actions.status(package)
                job = actions.jobs.get(package)
                if (
                    job is not None
                    and job.process.poll() is not None
                    and not job.observed
                ):
                    job.observed = True
                    comparison = outputs.comparisons.get(Path(package) / "tmp")
                    if comparison is not None:
                        comparison.future = outputs.executor.submit(
                            outputs.capture,
                            comparison,
                        )
                content = json.dumps(state).encode()
                status = HTTPStatus.OK
            except (OSError, ValueError) as exc:
                content = json.dumps({"error": str(exc)}).encode()
                status = HTTPStatus.BAD_REQUEST
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(content)

        def do_POST(self) -> None:
            """Start package commands only through the action endpoint."""
            if urlsplit(self.path).path == "/api/action":
                self.action_response(start=True)
            else:
                self.send_error(HTTPStatus.NOT_IMPLEMENTED)

        def do_GET(self) -> None:
            """Return an allowlisted asset or a fresh repository snapshot."""
            request = urlsplit(self.path)
            route = request.path
            status = HTTPStatus.OK
            if route == "/api/action":
                self.action_response(start=False)
                return
            if route == "/output":
                self.serve_output(parse_qs(request.query).get("path", [""])[0])
                return
            if route == "/output-diff":
                requested = parse_qs(request.query).get("path", [""])[0]
                comparison = outputs.comparisons.get(Path(requested))
                if comparison is None:
                    self.send_error(HTTPStatus.NOT_FOUND, "Output capture not found")
                    return
                content_type = "text/html; charset=utf-8"
                try:
                    comparison = outputs.entry_report(
                        comparison,
                        parse_qs(request.query).get("entry", [""])[0],
                    )
                    content = output_diff_page(comparison)
                except (OSError, ValueError):
                    self.send_error(HTTPStatus.NOT_FOUND, "Output report not found")
                    return
            elif route == "/api/overview":
                content_type = "application/json; charset=utf-8"
                try:
                    content = json.dumps(requested_data(request.query)).encode()
                except (GitCanonicalError, ValueError, OSError) as exc:
                    status = HTTPStatus.INTERNAL_SERVER_ERROR
                    content = json.dumps({"error": str(exc)}).encode()
            elif route in routes:
                filename, content_type = routes[route]
                content = (assets / filename).read_bytes()
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data:; object-src 'none'; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(content)

        def serve_output(self, requested: str) -> None:
            """Serve a package output listing or file for viewing in a browser tab."""
            try:
                root, target = output_path(requested)
                if target.is_dir():
                    diff_url = (
                        "/output-diff?" + urlencode({"path": str(root)})
                        if root in outputs.comparisons
                        else None
                    )
                    content = output_index(root, target, diff_url)
                    content_type = "text/html; charset=utf-8"
                    length = len(content)
                else:
                    content = None
                    content_type = (
                        mimetypes.guess_type(target.name)[0]
                        or "application/octet-stream"
                    )
                    length = target.stat().st_size
            except (OSError, ValueError):
                self.send_error(HTTPStatus.NOT_FOUND, "Output not found")
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                "object-src 'none'; frame-ancestors 'none'",
            )
            self.end_headers()
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                if content is not None:
                    self.wfile.write(content)
                else:
                    with target.open("rb") as stream:
                        shutil.copyfileobj(stream, self.wfile)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            """Keep the launcher output focused on its URL."""

    class Server(HTTPServer):
        def server_close(self) -> None:
            """Finish output capture and release locks when this launch ends."""
            super().server_close()
            actions.close()
            outputs.close()

    server = Server(("127.0.0.1", port), Handler)
    try:
        requested_data("")
    except (GitCanonicalError, ValueError, OSError):
        server.server_close()
        raise
    return server


def open_gui(root: Path, *, port: int, open_browser: bool) -> None:
    """Launch the expandable repository graph until interrupted."""
    with gui_server(root, port) as server:
        url = f"http://127.0.0.1:{server.server_port}"
        print(f"Canonical browser: {url}\nPress Ctrl+C to stop.", flush=True)  # noqa: T201
        if open_browser:
            webbrowser.open(url)
        with contextlib.suppress(KeyboardInterrupt):
            server.serve_forever()


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

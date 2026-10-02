#!/usr/bin/env python3
# Copyright (c) 2026- Paschalis Bizopoulos
"""Browse Canonical directories, packages, hosts, and changes in a web diagram."""

import argparse
import contextlib
import json
import mimetypes
import os
import platform
import re
import shutil
import webbrowser
from copy import deepcopy
from dataclasses import dataclass, replace
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from git_canonical import (
    CliEntry,
    detect_packages,
    git,
    home_repositories,
    overview_data,
    package_cli,
    package_overview,
    source_cli_overview,
    source_package_overview,
    source_suppressions,
)
from git_canonical import CommandError as GitCanonicalError

MAX_PORT = 65535
MOUNT_FIELDS = 3
EMPTY_ENTRIES = {"(none)", "(not applicable)", "(not declared)"}
SOURCE_SUFFIXES = {
    ".nix",
    ".py",
    ".html",
    ".js",
    ".mjs",
    ".css",
    ".tex",
    ".sh",
    ".ts",
    ".tsx",
    ".jsx",
}


@dataclass
class TreeNode:  # noqa: D101
    title: str
    children: list["TreeNode"] | None = None
    change: str | None = None
    warning: bool = False
    resource_id: str | None = None
    directory: Path | None = None
    source_file: bool = False


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
            source = RepositoryBrowser(root)
            snapshot, _ = browser_snapshot(root, source.package_entries())
            self.source_snapshots[root] = deepcopy(snapshot), source.status
        snapshot, self.status = deepcopy(self.source_snapshots[root])
        self.snapshot, _ = scope_snapshot(snapshot, self.cwd)

    def package_entries(self, *, diff: bool = False) -> list[TreeNode]:
        """Build a collapsible package tree with high-level changes."""
        root = self.cwd
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
        filenames = (
            "default.nix",
            "main.py",
            "test_main.py",
            "index.html",
            "script.js",
            "style.css",
        )
        current = package_overview(directory)
        current_cli = package_cli(directory) if current else []
        if not diff:
            return TreeNode(
                f"packages/{name}",
                self.summary_tree(current, cli=current_cli) if current else None,
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
        previous_cli = (
            source_cli_overview(previous_files.get("main.py")) if previous else []
        )
        summary_tree = self.merged_summary_tree(
            previous,
            current,
            previous_cli=previous_cli,
            current_cli=current_cli,
        )
        if only_changes:
            summary_tree = self.changed_nodes(summary_tree)
        return TreeNode(f"packages/{name}", summary_tree or None)

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
            viewer = RepositoryBrowser(repository)
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
    def summary_tree(
        summary: str,
        *,
        cli: list[CliEntry] | None = None,
    ) -> list[TreeNode]:
        """Convert the displayed summary into fields and grouped detail nodes."""
        lines = summary.splitlines()
        arguments_start = next(
            (index for index, line in enumerate(lines) if line == "Arguments:"),
            len(lines),
        )
        fields = [TreeNode(line) for line in lines[:arguments_start]]
        for group in ("Arguments", "Dependencies", "Tests", "Suppressions"):
            if f"{group}:" not in lines:
                continue
            start = lines.index(f"{group}:")
            end = next(
                (
                    index
                    for index in range(start + 1, len(lines))
                    if not lines[index].startswith("  ")
                ),
                len(lines),
            )
            children = [
                TreeNode(line.strip(), warning=line.strip().startswith("(unavailable:"))
                for line in lines[start + 1 : end]
            ]
            if group == "Suppressions":
                children = RepositoryBrowser.suppression_tree(summary, summary)
            if group == "Arguments" and cli is not None:
                children = RepositoryBrowser.cli_tree(cli)
            children = RepositoryBrowser.declared_children(children)
            if children:
                fields.append(TreeNode(group, children))
        return fields

    @classmethod
    def merged_summary_tree(
        cls,
        previous: str,
        current: str,
        *,
        previous_cli: list[CliEntry] | None = None,
        current_cli: list[CliEntry] | None = None,
    ) -> list[TreeNode]:
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
                    result.append(TreeNode(f"- {before}", change="removed"))
                if after is not None:
                    result.append(TreeNode(f"+ {after}", change="added"))
        for group in ("Arguments", "Dependencies", "Tests", "Suppressions"):
            old_entries = cls.summary_group(previous, group)
            new_entries = cls.summary_group(current, group)
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
            if group == "Arguments" and current_cli is not None:
                children = cls.cli_tree(current_cli, previous=previous_cli)
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
    def suppression_tree(cls, previous: str, current: str) -> list[TreeNode]:
        """Group suppression counts and their changes beneath each source filename."""

        def counts(summary: str) -> dict[str, int]:
            result = {}
            for entry in cls.summary_group(summary, "Suppressions"):
                label, separator, count = entry.rpartition(": ")
                if separator and count.isdecimal():
                    result[label] = int(count)
            return result

        before, after = counts(previous), counts(current)
        files: dict[str, list[TreeNode]] = {}
        for label in sorted(before.keys() | after.keys()):
            old, new = before.get(label, 0), after.get(label, 0)
            filename, _, kind = label.partition(": ")
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


def merge_dependency_changes(
    data: dict[str, Any],
    resources: dict[str, TreeNode],
) -> None:
    """Attach semantic dependency changes to the graph's relationships."""
    known = {node["id"] for node in data["nodes"]}
    for identifier, tree in resources.items():
        repository = identifier.split(":", 1)[0]
        dependencies = next(
            (child for child in tree.children or [] if child.title == "Dependencies"),
            None,
        )
        for child in dependencies.children or [] if dependencies else []:
            match = re.fullmatch(r"[-+] ([^:]+): (packages/\S+)", child.title)
            if not match or child.change not in ("removed", "added"):
                continue
            source = f"{repository}:{match[2]}"
            change = child.change
            edge = next(
                (
                    edge
                    for edge in data["edges"]
                    if edge["source"] == source
                    and edge["target"] == identifier
                    and edge["kind"] == match[1]
                ),
                None,
            )
            if edge is not None:
                edge["change"] = change
                continue
            if source not in known:
                data["nodes"].append(
                    {
                        "id": source,
                        "kind": "package-reference",
                        "repository": repository,
                        "path": match[2],
                        "name": match[2].removeprefix("packages/"),
                        "removed": change == "removed",
                    },
                )
                known.add(source)
            data["edges"].append(
                {
                    "source": source,
                    "target": identifier,
                    "kind": match[1],
                    "change": change,
                },
            )


def resource_tree(record: dict[str, Any], tree: TreeNode) -> TreeNode:
    """Attach shared machine and OS details to a resource."""
    tree.resource_id = record["id"]
    if record["kind"] == "machine":
        tree.title = f"Machine: {record['name']}"
        tree.children = [TreeNode(detail) for detail in record["details"]]
    elif record["kind"] == "host":
        record["icon"] = "nixos"
        tree.children = [TreeNode("OS: NixOS (configuration)")]
    elif record["kind"] == "repository":
        tree.title = record["repository"]
        tree.children = [TreeNode(f"Profile: {record.get('profile', 'flake')}")]
    return tree


def package_sources(directory: Path) -> list[TreeNode]:
    """Count physical lines in package sources and source assets under prm/."""
    if not directory.is_dir() or directory.is_symlink():
        return []
    candidates = list(directory.iterdir())
    resources = directory / "prm"
    if resources.is_dir() and not resources.is_symlink():
        for parent, folders, files in os.walk(resources, followlinks=False):
            folders[:] = [
                child
                for child in folders
                if not child.startswith(".") and not (Path(parent) / child).is_symlink()
            ]
            candidates.extend(Path(parent) / filename for filename in files)
    result = []
    for path in sorted(candidates):
        if (
            path.suffix not in SOURCE_SUFFIXES
            or path.is_symlink()
            or not path.is_file()
        ):
            continue
        name = str(path.relative_to(directory))
        try:
            content = path.read_bytes()
        except OSError as error:
            children = [TreeNode(f"(unavailable: {error.strerror})", warning=True)]
        else:
            children = [TreeNode(f"Lines: {len(content.splitlines())}")]
            counts = source_suppressions(name, content.decode(errors="replace"))
            children.extend(
                TreeNode(f"{kind} ({scope}): {count}")
                for (kind, scope), count in sorted(counts.items())
            )
        result.append(TreeNode(name, children, source_file=True))
    return result


def source_metrics(sources: list[TreeNode]) -> dict[str, dict[str, int]]:
    """Keep current physical source totals independent of semantic diff rows."""
    lines = {}
    suppressions: dict[str, int] = {}
    for source in sources:
        if Path(source.title).parts[0] == "prm":
            continue
        for child in source.children or []:
            label, separator, count = child.title.rpartition(": ")
            if not separator or not count.isdecimal():
                continue
            if label == "Lines":
                lines[source.title] = int(count)
            else:
                suppressions[label] = suppressions.get(label, 0) + int(count)
    return {"lines": lines, "suppressions": suppressions}


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
        nodes.append(TreeNode("tmp/", directory=output.resolve()))
    return nodes


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
                "Dependencies": "default.nix",
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


def browser_snapshot(  # noqa: C901 - assemble resources and semantic relationships
    root: Path,
    semantic_tree: list[TreeNode],
) -> tuple[dict[str, Any], list[TreeNode]]:
    """Build the machine and repository model for the web browser."""
    try:
        data = overview_data(root)
    except GitCanonicalError:
        if any(
            (root / name).exists()
            for name in ("flake.nix", ".gitmodules", "packages", "hosts", "checks")
        ):
            raise
        return directory_snapshot(root)
    resources: dict[str, TreeNode] = {}

    def index(nodes: list[TreeNode], path: tuple[str, ...] = ()) -> None:
        for node in nodes:
            if node.title.startswith("packages/"):
                resources[f"{'/'.join(path) or '.'}:{node.title}"] = node
            else:
                index(node.children or [], (*path, node.title))

    index(semantic_tree)
    known = {node["id"] for node in data["nodes"]}
    for identifier in resources:
        if identifier not in known:
            repository, path = identifier.split(":", 1)
            data["nodes"].append(
                {
                    "id": identifier,
                    "kind": "package",
                    "name": path.removeprefix("packages/"),
                    "path": path,
                    "repository": repository,
                    "removed": True,
                },
            )
    merge_dependency_changes(data, resources)
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
            resources.get(identifier, TreeNode(record["path"], [])),
        )
        if record["kind"] in {"package", "host"}:
            directory = root / record["repository"] / record["path"]
            sources = package_sources(directory)
            record["source_metrics"] = source_metrics(sources)
            if record["kind"] == "package":
                tree = package_file_tree(directory, tree, sources)
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
    return next(
        (
            parent
            for parent in (directory, *directory.parents)
            if any((parent / name).exists() for name in ("flake.nix", ".gitmodules"))
        ),
        directory,
    )


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


def output_index(root: Path, directory: Path) -> bytes:
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
        f"<h1>{title}</h1><ul>{links}</ul>"
        + ("" if entries else "<p>This directory is empty.</p>")
        + "</html>"
    ).encode()


def gui_server(  # noqa: C901 - serve graph assets and package runtime output
    root: Path,
    port: int = 0,
) -> HTTPServer:
    """Serve only GUI assets and read-only repository data on loopback."""
    assets = Path(__file__).parent / "prm"
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
        requested = parse_qs(query).get("directory", [str(root)])[0]
        directory = Path(requested).resolve()
        if not directory.is_dir():
            msg = f"Directory not found: {directory}"
            raise ValueError(msg)
        return gui_data(directory)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            """Return an allowlisted asset or a fresh repository snapshot."""
            request = urlsplit(self.path)
            route = request.path
            status = HTTPStatus.OK
            if route == "/output":
                self.serve_output(parse_qs(request.query).get("path", [""])[0])
                return
            if route == "/api/overview":
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
                    content = output_index(root, target)
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

    return HTTPServer(("127.0.0.1", port), Handler)


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
        default=Path.cwd(),
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

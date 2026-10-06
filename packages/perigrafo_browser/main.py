#!/usr/bin/env python3
# Copyright (c) 2026- Paschalis Bizopoulos
"""Browse Perigrafo packages, interfaces, tests, and changes in a terminal tree."""

import argparse
import contextlib
import curses
import os
import platform
import re
import sys
import unicodedata
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, NamedTuple

from perigrafo import (
    CliEntry,
    detect_packages,
    git,
    home_submodules,
    overview_data,
    package_overview,
    render_resource_overview,
    resource_data,
    source_package_cli,
    source_resource_data,
    source_suppressions,
)
from perigrafo import CommandError as PerigrafoError

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


def source_cli_overview(source: str | None) -> list[CliEntry]:
    """Summarize a static CLI with absent-interface and discovery diagnostics."""
    if not source:
        return [CliEntry((), "(not applicable)")]
    try:
        return source_package_cli(source.encode(), "main.py") or [
            CliEntry((), "(none)"),
        ]
    except (SyntaxError, ValueError) as error:
        return [CliEntry((), f"(unavailable: {error})")]


def package_cli(package: Path) -> list[CliEntry]:
    """Read the current Perigrafo CLI data for a package."""
    data = resource_data(package)
    if error := data["diagnostics"].get("cli"):
        return [CliEntry((), f"(unavailable: {error})")]
    return [
        CliEntry(tuple(row["path"]), row["text"], row["command"]) for row in data["cli"]
    ]


def source_package_overview(name: str, files: dict[str, str]) -> str:
    """Render historical package files through the current Perigrafo analyzer."""
    return render_resource_overview(source_resource_data(name, files))


@dataclass
class TreeNode:  # noqa: D101
    title: str
    children: list["TreeNode"] | None = None
    expanded: bool = False
    style: int | None = None
    warning: bool = False
    resource_id: str | None = None
    directory: Path | None = None
    source_file: bool = False


class Row(NamedTuple):  # noqa: D101
    owner: int
    text: str


class Viewer:  # noqa: D101
    def __init__(self, cwd: str | Path | None = None) -> None:  # noqa: D107
        self.cwd = Path(cwd or Path.cwd()).resolve()
        self.snapshot: dict[str, Any] = {}
        self.selected = 0
        self.top = 0
        self.pattern = ""
        self.direction = 1
        self.status = ""
        self.width = 80
        self.height = 23
        self.mode = "high-level"
        self.overview: list[TreeNode] = []
        self.full_overview: list[TreeNode] = []
        self.diff_overview: list[TreeNode] = []
        self.overview_loaded = False
        self.overview_visible: list[TreeNode] = []
        self.overview_parents: list[int | None] = []
        self.source_snapshots: dict[Path, tuple[dict[str, Any], str]] = {}

    def refresh_overview(self) -> None:
        """Rebuild the package overview from the current working tree."""
        self.source_snapshots.clear()
        self.load_overview()

    def load_overview(self) -> None:
        """Navigate within cached source snapshots without rereading declarations."""
        self.status = ""
        root = browser_root(self.cwd)
        if root not in self.source_snapshots:
            source = Viewer(root) if root != self.cwd else self
            self.full_overview = source.package_entries(diff=False)
            self.snapshot, self.full_overview = browser_snapshot(
                root,
                self.full_overview,
            )
            self.source_snapshots[root] = deepcopy(self.snapshot), source.status
        self.snapshot, self.status = deepcopy(self.source_snapshots[root])
        self.snapshot, self.full_overview = scope_snapshot(self.snapshot, self.cwd)
        self.diff_overview = self.changed_nodes(self.full_overview)
        self.select_overview()
        self.overview_loaded = True
        self.selected = self.top = 0

    def ensure_overview(self) -> None:
        """Build the package overview once, on startup or first use."""
        if not self.overview_loaded:
            self.refresh_overview()

    def select_overview(self) -> None:
        """Select a cached tree without inspecting the working directory."""
        self.overview = (
            self.diff_overview if self.mode == "high-level diff" else self.full_overview
        )
        self.selected = self.top = 0

    def package_entries(self, *, diff: bool = False) -> list[TreeNode]:
        """Build a collapsible package tree with high-level changes."""
        root = self.cwd
        if (root / ".gitmodules").is_file() and not (root / "packages").is_dir():
            return self.home_package_entries(root, diff=diff)
        packages = root / "packages"
        try:
            current_names = {package.name for package in detect_packages(root)}
        except PerigrafoError as exc:
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
            if node.style is not None or children:
                result.append(replace(node, children=children))
        return result

    def home_package_entries(self, root: Path, *, diff: bool) -> list[TreeNode]:
        """Build repository summaries beneath their home-repository paths."""
        try:
            repositories = home_submodules(root, require_url=False)
        except PerigrafoError as exc:
            self.status = str(exc)
            return []
        tree: dict[str, Any] = {}
        for item in repositories:
            relative = item["path"]
            repository = root / relative
            if not (repository / "packages").is_dir():
                continue
            viewer = Viewer(repository)
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
                children = Viewer.suppression_tree(summary, summary)
            if group == "Arguments" and cli is not None:
                children = Viewer.cli_tree(cli)
            children = Viewer.declared_children(children)
            if children:
                fields.append(TreeNode(group, children))
        return fields

    @classmethod
    def summary_changes(cls, previous: str, current: str) -> list[TreeNode]:
        """Build collapsible changes for fields and grouped details."""
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
        old_dependencies = cls.summary_group(previous, "Dependencies")
        new_dependencies = cls.summary_group(current, "Dependencies")
        dependency_changes = [
            TreeNode(f"- {item}", style=31)
            for item in old_dependencies
            if item not in new_dependencies
        ]
        dependency_changes.extend(
            TreeNode(f"+ {item}", style=32)
            for item in new_dependencies
            if item not in old_dependencies
        )
        if dependency_changes:
            changes.append(TreeNode("Dependencies", dependency_changes))
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
        suppression_changes = cls.changed_nodes(cls.suppression_tree(previous, current))
        if suppression_changes:
            changes.append(TreeNode("Suppressions", suppression_changes))
        return changes

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
                    result.append(TreeNode(f"- {before}", style=31))
                if after is not None:
                    result.append(TreeNode(f"+ {after}", style=32))
        for group in ("Arguments", "Dependencies", "Tests", "Suppressions"):
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
                style = 32 if old == 0 else 31 if new == 0 else 33
                children.append(TreeNode(f"{kind}: {old} → {new}", style=style))
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
        entries: list[tuple[CliEntry, int | None]] = [
            (entry, 31) for entry in previous or [] if entry not in new
        ]
        entries.extend(
            (entry, 32 if previous is not None and entry not in old else None)
            for entry in current
        )
        for entry, style in entries:
            children = roots
            for depth, name in enumerate(entry.path, 1):
                path = entry.path[:depth]
                if path not in commands:
                    node = TreeNode(name, [])
                    commands[path] = node
                    children.append(node)
                node = commands[path]
                children = node.children if node.children is not None else []
            prefix = "" if style is None else {31: "- ", 32: "+ "}[style]
            if entry.command:
                node.title = prefix + entry.path[-1]
                node.style = style
            else:
                children.append(
                    TreeNode(
                        prefix + entry.text,
                        style=style,
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
        return self.overview_rows(max(1, width))

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
                rows.append(Row(owner, prefix + wrapped[0][1]))
                rows.extend(
                    Row(owner, continuation + part) for _start, part in wrapped[1:]
                )
                if node.expanded and node.children:
                    visit(node.children, depth + 1, owner)

        visit(self.overview, 0, None)
        return rows

    def styles(self, row: Row) -> list[int]:
        """Style the selected node, changes, and diagnostics."""
        node = self.overview_visible[row.owner]
        style = self.tree_style(node)
        if self.has_warning(node):
            style = 33
        styles = [style] if style is not None else []
        if row.owner == self.selected:
            styles.append(7)
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

    def search(self, direction: int) -> None:  # noqa: D102
        try:
            pattern = re.compile(self.pattern)
        except re.error as exc:
            self.status = f"Invalid pattern: {exc}"
            return
        self.search_overview(pattern, direction)

    def search_overview(self, pattern: re.Pattern[str], direction: int) -> None:
        """Search every tree node and reveal a match through collapsed ancestors."""
        nodes: list[tuple[TreeNode, tuple[TreeNode, ...]]] = []

        def visit(children: list[TreeNode], ancestors: tuple[TreeNode, ...]) -> None:
            for node in children:
                nodes.append((node, ancestors))
                visit(node.children or [], (*ancestors, node))

        visit(self.overview, ())
        self.overview_rows(self.width)
        selected = (
            self.overview_visible[self.selected]
            if self.overview_visible and self.selected < len(self.overview_visible)
            else None
        )
        anchor = next((i for i, (node, _) in enumerate(nodes) if node is selected), -1)
        candidates = [
            i
            for i, (node, _) in enumerate(nodes)
            if pattern.search(self.safe(node.title))
        ]
        if not candidates:
            self.status = "Pattern not found"
            return
        ordered = candidates if direction > 0 else candidates[::-1]
        index = next(
            (i for i in ordered if (i > anchor if direction > 0 else i < anchor)),
            ordered[0],
        )
        node, ancestors = nodes[index]
        for ancestor in ancestors:
            ancestor.expanded = True
        rows = self.overview_rows(self.width)
        self.selected = next(
            i for i, item in enumerate(self.overview_visible) if item is node
        )
        self.status = ""
        self.reveal(next(i for i, row in enumerate(rows) if row.owner == self.selected))

    def navigate(  # noqa: D102
        self,
        key: str | int,
        height: int,
        rows: list[Row],
    ) -> None:
        self.height = height
        if key in ("\x08", curses.KEY_BACKSPACE, "\x7f"):
            parent = browser_parent(self.cwd)
            if parent is not None:
                self.cwd = parent
                self.load_overview()
            return
        if key in ("\n", "\r", curses.KEY_ENTER) and self.overview_visible:
            directory = self.overview_visible[self.selected].directory
            if directory is not None:
                self.cwd = directory
                self.load_overview()
                return
        if not rows:
            return
        self.navigate_overview(key, height, rows)

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
        """Handle page movement in the overview tree."""
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
        elif key in offsets:
            self.top = max(0, min(max(0, len(rows) - height), self.top + offsets[key]))
            self.selected = rows[self.top].owner

    def view(self) -> None:
        """Open the browser at the current repository snapshot."""
        self.ensure_overview()
        curses.wrapper(self.screen)

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
            height, width = screen.getmaxyx()
            self.width = max(1, width - 1)
            page = self.height = max(1, height - 1)
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
            footer = (
                editing + query
                if editing
                else self.status
                or (
                    f"{'(END) ' if self.top + page >= len(rows) else ''}"
                    f"D diff {'on' if self.mode == 'high-level diff' else 'off'}  "
                    "j/k node  l/h open/close  Enter directory  "
                    "Backspace parent  r refresh  "
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
                    if self.pattern and self.overview:
                        self.search(self.direction)
                elif key in ("\x7f", "\b", curses.KEY_BACKSPACE):
                    query = query[:-1]
                elif isinstance(key, str) and key.isprintable():
                    query += key
                continue
            if key == "q" or (prefix == "Z" and key == "Z"):
                return
            if key == "D":
                self.mode = (
                    "high-level diff" if self.mode == "high-level" else "high-level"
                )
                self.ensure_overview()
                self.select_overview()
                self.status = ""
                continue
            if key == "r":
                self.refresh_overview()
                continue
            prefix = key if key in (":", "Z") else ""
            self.status = ""
            if key in ("/", "?"):
                editing, query = str(key), ""
            elif key in ("n", "N") and self.pattern and self.overview:
                self.search(self.direction * (1 if key == "n" else -1))
            else:
                self.navigate(key, page, rows)


def serialize_node(node: TreeNode) -> dict[str, Any]:
    """Serialize the same resource details used by the terminal tree."""
    return {
        "title": node.title,
        "change": (
            {31: "removed", 32: "added", 33: "modified"}.get(node.style)
            if node.style is not None
            else None
        ),
        "warning": node.warning,
        "resource_id": node.resource_id,
        "source_file": node.source_file,
        "directory": str(node.directory) if node.directory is not None else None,
        "children": [serialize_node(child) for child in node.children or []],
    }


def deserialize_node(tree: dict[str, Any]) -> TreeNode:
    """Restore directories and resource details from the shared snapshot."""
    return TreeNode(
        tree["title"],
        [deserialize_node(child) for child in tree["children"]],
        style={"removed": 31, "added": 32, "modified": 33}.get(tree["change"]),
        warning=tree["warning"],
        resource_id=tree["resource_id"],
        source_file=tree.get("source_file", False),
        directory=Path(tree["directory"]) if tree.get("directory") else None,
    )


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
    """Share added and removed dependency relationships with both renderers."""
    known = {node["id"] for node in data["nodes"]}
    for identifier, tree in resources.items():
        repository = identifier.split(":", 1)[0]
        dependencies = next(
            (child for child in tree.children or [] if child.title == "Dependencies"),
            None,
        )
        for child in dependencies.children or [] if dependencies else []:
            match = re.fullmatch(r"[-+] ([^:]+): (packages/\S+)", child.title)
            if not match or child.style not in (31, 32):
                continue
            source = f"{repository}:{match[2]}"
            change = {31: "removed", 32: "added"}[child.style]
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
        if title.startswith(("Language:", "Name:")) or node.title == "Dependencies":
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
        elif node.title in {"Arguments", "Tests"}:
            name = {
                "Arguments": "main.py",
                "Tests": "test_main.py",
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


def relationship_groups(
    record: dict[str, Any],
    edges: list[dict[str, Any]],
) -> list[TreeNode]:
    """Group declared local providers without repeating paths or file references."""
    groups: dict[str, list[TreeNode]] = {}
    for edge in edges:
        group = {
            "runtime": "Runtime dependencies",
            "test": "Test dependencies",
            "source": "Shared files",
        }.get(edge["kind"])
        if edge["target"] != record["id"] or group is None:
            continue
        repository, _, provider = edge["source"].partition(":")
        title = provider.removeprefix("packages/")
        if repository != record["repository"]:
            title = f"{repository}/{provider}"
        link = TreeNode(
            title,
            style={"added": 32, "removed": 31}.get(edge.get("change") or ""),
        )
        children = groups.setdefault(group, [])
        if link not in children:
            children.append(link)
    return [TreeNode(group, groups[group]) for group in sorted(groups)]


def browser_snapshot(  # noqa: C901 - assemble resources and semantic relationships
    root: Path,
    semantic_tree: list[TreeNode],
) -> tuple[dict[str, Any], list[TreeNode]]:
    """Build one resource model for the terminal tree and graphical components."""
    try:
        data = overview_data(root)
    except PerigrafoError:
        if any(
            (root / name).exists()
            for name in ("flake.nix", ".gitmodules", "packages", "hosts", "checks")
        ):
            raise
        return directory_snapshot(root)
    data["nodes"] = [node for node in data["nodes"] if node["kind"] != "check"]
    visible = {node["id"] for node in data["nodes"]}
    data["edges"] = [
        edge
        for edge in data["edges"]
        if edge["source"] in visible and edge["target"] in visible
    ]
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
        tree.children = [
            *(tree.children or []),
            *relationship_groups(record, data["edges"]),
        ]
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


def browser_data(root: Path) -> dict[str, Any]:
    """Return the shared machine and repository snapshot used by the terminal."""
    viewer = Viewer(root)
    viewer.refresh_overview()
    data = viewer.snapshot
    data["warning"] = viewer.status
    return data


def browser_root(directory: Path) -> Path:
    """Find the nearest Perigrafo source root for a directory-scoped view."""
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
    """Show immediate directory containers outside a Perigrafo repository."""
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


def scoped_tree(
    data: dict[str, Any],
    directory: Path,
    nodes: list[TreeNode],
) -> tuple[dict[str, Any], list[TreeNode]]:
    """Nest the home user under their machine and serialize the scoped tree."""
    if directory == Path.home().resolve():
        user = next((node for node in nodes if node.directory == directory), None)
        if user is None:
            user = TreeNode(directory.name, nodes, directory=directory)
        user.title = f"User: {directory.name} ({directory})"
        machine = data["machine"]
        nodes = [
            TreeNode(
                f"Machine: {machine['name']}",
                [
                    user,
                    TreeNode(
                        "Details",
                        [TreeNode(text) for text in machine["details"]],
                    ),
                ],
                expanded=True,
            ),
        ]
    data["tree"] = [serialize_node(node) for node in nodes]
    return data, nodes


def scope_snapshot(  # noqa: C901, PLR0912 - filter resources and reconstruct containment
    data: dict[str, Any],
    directory: Path,
) -> tuple[dict[str, Any], list[TreeNode]]:
    """Restrict both views to a directory and preserve navigable containment."""
    root = Path(data["root"])
    if not data["nodes"] or all(
        record.get("profile") == "directory" for record in data["nodes"]
    ):
        return scoped_tree(
            data,
            directory,
            [deserialize_node(tree) for tree in data["tree"]],
        )
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
            continue
        if record["kind"] == "repository":
            container(Path(record["repository"]))
            continue
        location = Path(record["directory"]) / record["path"]
        relative = location.relative_to(directory)
        parent = container(relative.parent)
        node = deserialize_node(record["tree"])
        node.title = location.name
        record["tree"] = serialize_node(node)
        if parent.children is not None:
            parent.children.append(node)
    return scoped_tree(data, directory, result)


def main(argv: list[str] | None = None) -> None:
    """Open the restored terminal tree at the selected working directory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "directory",
        nargs="?",
        type=Path,
        default=Path(),
        help="initial directory (default: current directory)",
    )
    args = parser.parse_args(argv)
    if not args.directory.is_dir():
        parser.error(f"Directory not found: {args.directory}")
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        parser.exit(
            1,
            "A terminal is required; use perigrafo overview for text or JSON output.\n",
        )
    try:
        Viewer(args.directory).view()
    except (PerigrafoError, OSError, curses.error) as exc:
        parser.exit(1, f"Viewer error: {exc}\n")
    except KeyboardInterrupt:
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()

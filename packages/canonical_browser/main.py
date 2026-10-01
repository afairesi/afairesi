#!/usr/bin/env python3
# Copyright (c) 2026- Paschalis Bizopoulos
"""Browse Canonical packages, interfaces, tests, and changes in the terminal."""

import argparse
import contextlib
import curses
import re
import sys
import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, NamedTuple

from git_canonical import (
    CliEntry,
    detect_packages,
    git,
    home_repositories,
    package_cli,
    package_overview,
    source_cli_overview,
    source_package_overview,
)
from git_canonical import CommandError as GitCanonicalError


@dataclass
class TreeNode:  # noqa: D101
    title: str
    children: list["TreeNode"] | None = None
    expanded: bool = False
    style: int | None = None
    warning: bool = False


class Row(NamedTuple):  # noqa: D101
    owner: int
    text: str


class Viewer:  # noqa: D101
    def __init__(self, cwd: str | Path | None = None) -> None:  # noqa: D107
        self.cwd = Path(cwd or Path.cwd()).resolve()
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

    def refresh_overview(self) -> None:
        """Rebuild the package overview from the current working tree."""
        self.status = ""
        self.full_overview = self.package_entries(diff=False)
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
            if node.style is not None or children:
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
            if children or f"{group}:" in current:
                result.append(TreeNode(group, children))
        return result

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
                        warning=entry.text.startswith("(unavailable:"),
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
                    "j/k node  l/h open/close  r refresh  "
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


def main(argv: list[str] | None = None) -> None:
    """Open the terminal browser in the startup directory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        parser.exit(
            1,
            "A terminal is required; use git canonical overview "
            "for text or JSON output.\n",
        )
    try:
        Viewer().view()
    except curses.error as exc:
        parser.exit(1, f"Viewer error: {exc}\n")
    except KeyboardInterrupt:
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()

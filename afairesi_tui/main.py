#!/usr/bin/env python3
# Copyright (c) 2026- Paschalis Bizopoulos
"""Browse home's direct submodules in an expandable text tree."""

from __future__ import annotations

import argparse
import contextlib
import curses
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

CACHE_VERSION = 1
Payload = dict[str, Any] | list[dict[str, Any]]

READ_ERRORS = (
    curses.error,
    OSError,
    SyntaxError,
    ValueError,
    subprocess.SubprocessError,
)


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


def repositories(home: Path) -> list[dict[str, Any]]:
    """Join .gitmodules paths with gitlinks in the current home index."""
    root = git(home, "rev-parse", "--show-toplevel").strip()
    if Path(root).resolve() != home:
        message = "The selected home must be a Git repository root."
        raise ValueError(message)
    modules = home / ".gitmodules"
    if not modules.is_file():
        return []
    configured = git(home, "config", "--null", "--file", str(modules), "--list")
    paths = {
        entry.partition("\n")[2]
        for entry in configured.split("\0")
        if entry.partition("\n")[0].startswith("submodule.")
        and entry.partition("\n")[0].endswith(".path")
    }
    result = []
    for entry in git(home, "ls-files", "--stage", "-z").split("\0"):
        if not entry:
            continue
        metadata, path = entry.split("\t", 1)
        mode, _revision, stage = metadata.split()
        if mode != "160000" or stage != "0" or path not in paths:
            continue
        location = (home / path).resolve()
        if not location.is_relative_to(home) or location == home:
            continue
        initialized = (location / ".git").exists()
        result.append(
            {
                "id": path,
                "initialized": initialized,
            }
        )
    return sorted(result, key=lambda item: item["id"])


def fingerprint(root: Path) -> str:
    """Track HEAD, index, tracked files, and the targets of tracked symlinks."""
    digest = hashlib.sha256(interface_fingerprint(root).encode())
    for relative in git(root, "ls-files", "-z").split("\0"):
        path = root / relative
        if relative and path.is_symlink():
            try:
                stat = path.stat()
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
            except OSError as error:
                digest.update(f"unreadable target: {error.errno}".encode())
    return digest.hexdigest()


def read_output(root: Path, comparison: str | None) -> Payload:
    """Read only the requested CLI result; errors are never cached."""
    if comparison is None:
        resources: dict[str, Any] = json.loads(command(["afairesi"], root))
        return resources
    arguments = ["afairesi", "diff"]
    if comparison == "staged":
        arguments.append("--cached")
    difference = command(arguments, root)
    patch: list[dict[str, Any]] = (
        json.loads(command(["jd", "-t", "jd2patch"], root, input_text=difference))
        if difference
        else []
    )
    return patch


def cached_output(
    root: Path, comparison: str | None, cache: Path | None, *, refresh: bool
) -> tuple[Payload, bool]:
    """Reuse a stable CLI result, with atomic writes and disposable cache failures."""
    if cache is None:
        return read_output(root, comparison), False
    tools = [
        str(Path(shutil.which(name) or name).resolve()) for name in ("afairesi", "jd")
    ]
    key = hashlib.sha256(
        json.dumps([CACHE_VERSION, str(root), comparison, tools]).encode()
    ).hexdigest()
    path = cache / f"{key}.json"
    before = fingerprint(root)
    if not refresh:
        with contextlib.suppress(OSError, ValueError, TypeError):
            saved = json.loads(path.read_text(encoding="utf-8"))
            if (
                isinstance(saved, dict)
                and saved.get("fingerprint") == before
                and isinstance(saved.get("value"), dict if comparison is None else list)
            ):
                return saved["value"], True
    value = read_output(root, comparison)
    if fingerprint(root) == before:
        with contextlib.suppress(OSError):
            cache.mkdir(mode=0o700, parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=cache, delete=False
            ) as temporary:
                pending = Path(temporary.name)
                try:
                    json.dump({"fingerprint": before, "value": value}, temporary)
                    temporary.close()
                    pending.replace(path)
                finally:
                    pending.unlink(missing_ok=True)
    return value, False


def repository_report(
    home: Path,
    entry: dict[str, Any],
    *,
    comparison: str | None,
    cache: Path | None = None,
    refresh: bool = False,
) -> tuple[str, dict[str, Any], bool]:
    """Run just the requested CLI operation in one direct home submodule."""
    identifier = entry["id"]
    hit = False
    report: dict[str, Any] = {"initialized": entry["initialized"]}
    if not entry["initialized"]:
        report["error"] = "This submodule is not initialized."
        return identifier, report, hit
    try:
        root = (home / identifier).resolve()
        actual = Path(git(root, "rev-parse", "--show-toplevel").strip()).resolve()
        if actual != root or not root.is_relative_to(home) or root == home:
            message = "The registered path is not a repository checkout within home."
            raise ValueError(message)
        if comparison is None:
            report["branch"] = git(root, "rev-parse", "--abbrev-ref", "HEAD").strip()
            report["dirty"] = bool(
                git(
                    root,
                    "status",
                    "--porcelain",
                    "--untracked-files=no",
                    "--ignore-submodules=all",
                )
            )
        value, hit = cached_output(root, comparison, cache, refresh=refresh)
        report["resources" if comparison is None else "patch"] = value
    except READ_ERRORS as error:
        report["error"] = str(error)
    return identifier, report, hit


def snapshot(
    home: Path,
    *,
    comparison: str | None = None,
    progress: bool = False,
    cache: Path | None = None,
    refresh: bool = False,
) -> dict[str, Any]:
    """Nest direct registered submodules beneath their home directories."""
    home = home.expanduser().resolve()
    entries = repositories(home)
    compact_progress = progress and sys.stderr.isatty()
    if progress:
        sys.stderr.write(
            f"Reading 0/{len(entries)} repositories…"
            if compact_progress
            else f"Reading {len(entries)} direct submodules of {home}…\n"
        )
        sys.stderr.flush()
    reports = {}
    tree: dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [
            pool.submit(
                repository_report,
                home,
                entry,
                comparison=comparison,
                cache=cache,
                refresh=refresh,
            )
            for entry in entries
        ]
        for completed, future in enumerate(as_completed(futures), 1):
            identifier, report, hit = future.result()
            reports[identifier] = report
            if progress:
                status = "error" if "error" in report else "cached" if hit else "done"
                sys.stderr.write(
                    f"\rReading {completed}/{len(entries)} repositories…"
                    if compact_progress
                    else f"[{completed}/{len(entries)}] {identifier}: {status}\n"
                )
                sys.stderr.flush()
    if compact_progress:
        sys.stderr.write("\r\033[K")
        sys.stderr.flush()
    for identifier, report in sorted(reports.items()):
        if "patch" in report and not any(op["op"] != "test" for op in report["patch"]):
            continue
        parts = Path(identifier).parts
        parent = tree
        for directory in parts[:-1]:
            parent = parent.setdefault(directory, {})
        parent[parts[-1]] = report
    return {str(home): tree}


@dataclass(eq=False)
class Node:
    """One plain label with optional folded children."""

    label: str
    children: list[Node] = field(default_factory=list)
    expanded: bool = False
    change: str = ""

    def add(self, label: str) -> Node:
        """Append a folded child and return it for further details."""
        child = Node(label, change=self.change)
        self.children.append(child)
        return child

    def visible(self, depth: int = 0) -> Iterator[tuple[Node, int]]:
        """Visit only the rows exposed by expanded branches."""
        yield self, depth
        if self.expanded:
            for child in self.children:
                yield from child.visible(depth + 1)


def add_value(parent: Node, label: str, value: object) -> None:
    """Render metadata as named text nodes, without JSON braces or array indexes."""
    if not isinstance(value, (dict, list)) or not value:
        text = "none" if value is None or value in ([], {}) else str(value)
        parent.add(f"{label}: {text}" if label else text)
        return
    node = parent.add(label)
    if isinstance(value, dict):
        for key, item in value.items():
            add_value(node, str(key), item)
    else:
        for item in value:
            if isinstance(item, dict) and "names" in item:
                name = ", ".join(item["names"])
                add_value(
                    node,
                    name,
                    {key: data for key, data in item.items() if key != "names"},
                )
            elif isinstance(item, dict) and "resource" in item:
                node.add(f"{item['resource']} ({item.get('kind', 'dependency')})")
            else:
                add_value(node, "item" if isinstance(item, (dict, list)) else "", item)


def add_repository(parent: Node, name: str, report: dict[str, Any]) -> None:
    """Keep repository and resource summaries short, with details folded beneath."""
    node = parent.add(name)
    if "branch" in report:
        node.add(f"branch: {report['branch']}")
    if report.get("dirty"):
        node.add("modified")
    if "error" in report:
        node.add(f"error: {report['error']}")
    for resource, fields in report.get("resources", {}).items():
        child = node.add(resource)
        for key, value in fields.items():
            add_value(child, key, value)
    add_changes(node, report.get("patch", []))
    if not node.children:
        node.add("No changes" if "patch" in report else "No resources")


def add_changes(node: Node, patch: list[dict[str, Any]]) -> None:
    """Show added and removed interface values, omitting redundant patch tests."""
    branches: dict[tuple[Node, str], Node] = {}
    for operation in patch:
        if operation["op"] == "test":
            continue
        parts = [
            part.replace("~1", "/").replace("~0", "~")
            for part in operation["path"].split("/")[1:]
        ] or ["repository"]
        parent = node
        for part in parts[:-1]:
            key = (parent, part)
            if key not in branches:
                branches[key] = parent.add(part)
            parent = branches[key]
        marker = "+" if operation["op"] == "add" else "-"
        value = operation.get("value")
        structured = isinstance(value, (dict, list))
        label = f"{marker} {parts[-1]}"
        change = parent.add(label if structured else f"{label}: {value}")
        change.change = marker
        if structured:
            add_value(change, "value", value)


def populate_tree(parent: Node, directories: dict[str, Any]) -> None:
    """Build the home hierarchy with all children initially folded."""
    for name, value in directories.items():
        if isinstance(value.get("initialized"), bool):
            add_repository(parent, name, value)
        else:
            populate_tree(parent.add(name), value)


def build_tree(report: dict[str, Any], *, diff: bool = False) -> Node:
    """Show just home and its first directory level until the user expands it."""
    home, directories = next(iter(report.items()))
    root = Node(home, expanded=True)
    populate_tree(root, directories)
    if not root.children:
        root.add("No changes" if diff else "No direct registered submodules.")
    return root


class Viewer:
    """Keep folding, selection, and scrolling independent of terminal drawing."""

    def __init__(self, root: Node) -> None:
        """Start on the home row with no scroll offset."""
        self.root = root
        self.selected = 0
        self.top = 0
        self.horizontal = 0

    def search(self, query: str) -> bool:
        """Select the next matching label, expanding its ancestors and wrapping once."""
        current = list(self.root.visible())[self.selected][0]
        nodes = []
        parents = {}
        stack = [self.root]
        while stack:
            node = stack.pop()
            for child in reversed(node.children):
                parents[child] = node
                stack.append(child)
            nodes.append(node)
        start = nodes.index(current) + 1
        needle = query.casefold()
        for match in nodes[start:] + nodes[:start]:
            if needle in match.label.casefold():
                node = match
                while node in parents:
                    node = parents[node]
                    node.expanded = True
                self.selected = next(
                    i for i, (row, _) in enumerate(self.root.visible()) if row is match
                )
                self.horizontal = 0
                return True
        return False

    def handle_search(self, key: str | int, query: str) -> tuple[str | None, str]:
        """Edit or finish the search query and return its status message."""
        if key in ("\n", "\r", curses.KEY_ENTER):
            message = (
                f"No match for {query}" if query and not self.search(query) else ""
            )
            return None, message
        if key == "\x1b":
            return None, ""
        if key in ("\b", "\x7f", curses.KEY_BACKSPACE):
            return query[:-1], ""
        if isinstance(key, str) and key.isprintable():
            query += key
        return query, ""

    def handle(self, key: str | int, height: int) -> bool:
        """Apply vim or arrow navigation; return false when the user quits."""
        rows = list(self.root.visible())
        node, depth = rows[self.selected]
        steps = {
            "j": 1,
            curses.KEY_DOWN: 1,
            "k": -1,
            curses.KEY_UP: -1,
            curses.KEY_NPAGE: height,
            "\x06": height,
            curses.KEY_PPAGE: -height,
            "\x02": -height,
        }
        if key in steps:
            self.selected = max(0, min(len(rows) - 1, self.selected + steps[key]))
        elif key in ("g", curses.KEY_HOME):
            self.selected = 0
        elif key in ("G", curses.KEY_END):
            self.selected = len(rows) - 1
        elif key in ("h", curses.KEY_LEFT):
            if node.children and node.expanded:
                node.expanded = False
            else:
                self.selected = next(
                    (
                        index
                        for index in range(self.selected - 1, -1, -1)
                        if rows[index][1] < depth
                    ),
                    self.selected,
                )
        elif key in ("l", curses.KEY_RIGHT) and node.children:
            if node.expanded:
                self.selected += 1
            else:
                node.expanded = True
        elif key in (" ", "\n", "\r", curses.KEY_ENTER) and node.children:
            node.expanded = not node.expanded
        elif key in ("H", "L", "0"):
            self.horizontal = (
                0 if key == "0" else max(0, self.horizontal + (4 if key == "L" else -4))
            )
        return key not in ("q", "\x1b")

    def frame(self, height: int) -> list[tuple[str, bool, str]]:
        """Produce indented text, keeping selection visible after movement or resize."""
        rows = list(self.root.visible())
        self.selected = min(self.selected, len(rows) - 1)
        self.top = max(0, min(self.top, self.selected, len(rows) - height))
        self.top = max(self.top, self.selected - height + 1)
        result = []
        for index, (node, depth) in enumerate(
            rows[self.top : self.top + height], self.top
        ):
            marker = ("▾" if node.expanded else "▸") if node.children else " "
            label = "".join(
                char if char.isprintable() else repr(char)[1:-1] for char in node.label
            )
            result.append(
                (
                    f"{'  ' * depth}{marker} {label}"[self.horizontal :],
                    index == self.selected,
                    node.change,
                )
            )
        return result


def browse(
    screen: curses.window,
    root: Node,
    comparison: str | None,
    load: Callable[[str | None], Node],
) -> None:
    """Draw the outline with colored changes and a highlighted selection."""
    with contextlib.suppress(curses.error):
        curses.curs_set(0)
    colors = {}
    if curses.has_colors():
        background = curses.COLOR_BLACK
        with contextlib.suppress(curses.error):
            curses.use_default_colors()
            background = -1
        with contextlib.suppress(curses.error):
            curses.init_pair(1, curses.COLOR_GREEN, background)
            curses.init_pair(2, curses.COLOR_RED, background)
            colors = {"+": curses.color_pair(1), "-": curses.color_pair(2)}
    viewer = Viewer(root)
    views = {comparison: viewer}
    diff = comparison or "working"
    query = None
    message = ""
    while True:
        height, width = screen.getmaxyx()
        screen.erase()
        for row, (text, selected, change) in enumerate(
            viewer.frame(max(1, height - 1))
        ):
            if width > 1:
                with contextlib.suppress(curses.error):
                    screen.addnstr(
                        row,
                        0,
                        text,
                        width - 1,
                        colors.get(change, 0) | (curses.A_REVERSE if selected else 0),
                    )
        mode = f"{comparison} diff" if comparison else "resources"
        footer = (
            "/" + query
            if query is not None
            else (
                message
                or f"{mode}   / search   d {'resources' if comparison else 'diff'}"
            )
        )
        with contextlib.suppress(curses.error):
            screen.addnstr(height - 1, 0, footer, max(0, width - 1))
        screen.refresh()
        key = screen.get_wch()
        message = ""
        if query is not None:
            query, message = viewer.handle_search(key, query)
        elif key == "/":
            query = ""
        elif key == "d":
            comparison = None if comparison else diff
            if comparison not in views:
                views[comparison] = Viewer(load(comparison))
            viewer = views[comparison]
        elif not viewer.handle(key, max(1, height - 1)):
            return


def main() -> None:
    """Reuse unchanged inspections, then open a simple expandable text tree."""
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=(
            "j/k move, h/l collapse or expand, Space toggles, g/G jump, "
            "H/L pan, 0 resets pan, / searches, d toggles diff, q quits."
        ),
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=["diff"],
        help="show only interface diffs across all registered home repositories",
    )
    parser.add_argument(
        "--home",
        type=Path,
        default=Path.home(),
        help="home Git repository (default: your home directory)",
    )
    parser.add_argument(
        "--cached",
        "--staged",
        action="store_true",
        help="with diff, show staged changes instead of unstaged changes",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="reinspect repositories and replace cached results",
    )
    options = parser.parse_args()
    if options.cached and options.command != "diff":
        parser.error("--cached requires diff")
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        parser.error("an interactive terminal is required")
    comparison = (
        ("staged" if options.cached else "working") if options.command else None
    )
    try:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
        if not base.is_absolute():
            base = Path.home() / ".cache"

        def load(mode: str | None, *, progress: bool = False) -> Node:
            return build_tree(
                snapshot(
                    options.home,
                    comparison=mode,
                    progress=progress,
                    cache=base / "afairesi_tui",
                    refresh=options.refresh,
                ),
                diff=mode is not None,
            )

        curses.wrapper(browse, load(comparison, progress=True), comparison, load)
    except READ_ERRORS as error:
        parser.exit(1, f"{parser.prog}: {error}\n")
    except KeyboardInterrupt:
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()

# Copyright (c) 2026- Paschalis Bizopoulos
"""Browse package test names and their changes across home's direct submodules."""

# ruff: noqa: INP001 - mkPythonPackage installs this module as __init__.py.

import argparse
import contextlib
import curses
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypedDict, TypeGuard

from afairesi import CommandError, diff_inputs

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

CACHE_VERSION = 5
XTERM_ORANGE = 208
RGB_COLORS = 1 << 24
RGB_ORANGE = 0xFF8700
type View = Literal["tests", "diff"]

READ_ERRORS = (CommandError, OSError, ValueError, subprocess.SubprocessError)


class PatchChange(TypedDict):
    """A package or test name added or removed by jd."""

    op: Literal["add", "remove"]
    path: str
    value: str | list[str]


type Payload = dict[str, list[str]] | list[PatchChange]


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


def _path_stamp(path: Path) -> bytes:
    """Include changes to a file or link target and track directory identity."""
    try:
        info = path.lstat()
    except FileNotFoundError, NotADirectoryError:
        return b"missing"
    if stat.S_ISDIR(info.st_mode):
        return repr((info.st_dev, info.st_ino, info.st_mode)).encode()
    stats = [info]
    if stat.S_ISLNK(info.st_mode):
        with contextlib.suppress(OSError):
            stats.append(path.stat())
    return repr(
        [
            (
                item.st_dev,
                item.st_ino,
                item.st_mode,
                item.st_size,
                item.st_mtime_ns,
                item.st_ctime_ns,
            )
            for item in stats
        ]
    ).encode()


def fingerprint(root: Path, view: View) -> str | None:
    """Track the CLI's specification inputs in the index and working tree."""
    entries = git(root, "ls-files", "--stage", "-z")
    try:
        files = diff_inputs(root, entries)
    except CommandError:
        return None
    digest = hashlib.sha256()
    paths = {path for _, _, path in files}
    if view == "tests":
        paths.add(Path("flake.nix"))
    else:
        for mode, oid, path in files:
            if mode == "120000":
                return None
            digest.update(f"{mode} {oid}\t{path}\0".encode())
    paths.update(parent for path in list(paths) for parent in path.parents)
    for tracked in sorted(paths):
        digest.update(str(tracked).encode())
        digest.update(_path_stamp(root / tracked))
    return digest.hexdigest()


def repositories(home: Path) -> list[str]:
    """Join .gitmodules paths with gitlinks in the current home index."""
    root = git(home, "rev-parse", "--show-toplevel").strip()
    if Path(root).resolve() != home:
        message = "The selected home must be a Git repository root."
        raise ValueError(message)
    modules = home / ".gitmodules"
    if not modules.is_file():
        return []
    configured = git(home, "config", "list", "--null", "--file", str(modules))
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
        result.append(path)
    return sorted(result)


def _is_test_mapping(value: object) -> TypeGuard[dict[str, list[str]]]:
    """Recognize the CLI's package-to-test-names JSON format."""
    return isinstance(value, dict) and all(
        isinstance(package, str)
        and isinstance(names, list)
        and all(isinstance(name, str) for name in names)
        for package, names in value.items()
    )


def read_output(root: Path, view: View) -> Payload:
    """Read only the requested CLI result; errors are never cached."""
    commands = {"tests": ["afairesi"], "diff": ["afairesi", "diff"]}
    output = command(commands[view], root)
    if view == "tests":
        tests = json.loads(output)
        if not _is_test_mapping(tests):
            msg = "Expected afairesi JSON mapping each package to a list of test names."
            raise ValueError(msg)
        return tests
    if not output:
        return []
    patch = json.loads(command(["jd", "-t", "jd2patch"], root, input_text=output))
    return [operation for operation in patch if operation["op"] in {"add", "remove"}]


def _cache_home() -> Path:
    """Locate the user's XDG cache directory."""
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base if base.is_absolute() else Path.home() / ".cache"


def _write_cache_entry(path: Path, value: object) -> None:
    """Publish a complete JSON cache entry, tolerating unavailable storage."""
    with contextlib.suppress(OSError):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete_on_close=False
        ) as pending:
            json.dump(value, pending)
            pending.close()
            Path(pending.name).replace(path)


def cached_output(
    root: Path, view: View, cache: Path, *, refresh: bool
) -> tuple[Payload, bool]:
    """Reuse a stable CLI result, with atomic writes and disposable cache failures."""
    executables = ("afairesi", "jd") if view == "diff" else ("afairesi",)
    tools = [str(Path(shutil.which(name) or name).resolve()) for name in executables]
    key = hashlib.sha256(
        json.dumps([CACHE_VERSION, str(root), view, tools]).encode()
    ).hexdigest()
    path = cache / f"{key}.json"
    before = fingerprint(root, view)
    if before is not None and not refresh:
        with contextlib.suppress(OSError, ValueError):
            saved = json.loads(path.read_text(encoding="utf-8"))
            if (
                isinstance(saved, dict)
                and saved.get("fingerprint") == before
                and (
                    _is_test_mapping(saved.get("value"))
                    if view == "tests"
                    else isinstance(saved.get("value"), list)
                )
            ):
                return saved["value"], True
    value = read_output(root, view)
    if before is not None and fingerprint(root, view) == before:
        _write_cache_entry(path, {"fingerprint": before, "value": value})
    return value, False


def _repository_node(
    home: Path,
    path: str,
    *,
    view: View,
    cache: Path,
    refresh: bool,
) -> tuple[str, Node, str]:
    """Build a repository row and return its progress status."""
    node = Node(Path(path).name)
    packages = None
    try:
        root = (home / path).resolve()
        if not root.is_relative_to(home) or root == home:
            message = "The registered path must be inside the home repository."
            raise ValueError(message)
        if not (root / ".git").exists():
            message = "This submodule is not initialized."
            raise ValueError(message)
        if Path(git(root, "rev-parse", "--show-toplevel").strip()).resolve() != root:
            message = "The registered path is not a repository checkout within home."
            raise ValueError(message)
        value, hit = cached_output(root, view, cache, refresh=refresh)
        if isinstance(value, dict):
            packages = value
            status = git(
                root,
                "status",
                "--porcelain",
                "--untracked-files=no",
                "--ignore-submodules=all",
            )
            if status:
                node.change = "~"
                if any(line[1] != " " for line in status.rstrip("\n").split("\n")):
                    value, diff_hit = cached_output(
                        root, "diff", cache, refresh=refresh
                    )
                    hit = hit and diff_hit
        add_tests(node, packages, value if isinstance(value, list) else ())
    except READ_ERRORS as error:
        if packages is not None:
            add_tests(node, packages)
        node.add(f"error: {error}")
        return path, node, "error"
    return path, node, "cached" if hit else "done"


def build_tree(
    home: Path,
    *,
    view: View = "tests",
    progress: bool | Callable[[int, int], None] = False,
    cache: Path,
    refresh: bool = False,
) -> Node:
    """Group repository rows beneath their home directories in path order."""
    home = home.expanduser().resolve()
    entries = repositories(home)
    compact_progress = progress is True and sys.stderr.isatty()

    def report(completed: int, detail: str) -> None:
        if callable(progress):
            progress(completed, len(entries))
        elif progress:
            line = f"\rReading {completed}/{len(entries)} repositories…"
            sys.stderr.write(line if compact_progress else detail + "\n")
            sys.stderr.flush()

    report(0, f"Reading {len(entries)} direct submodules of {home}…")
    repositories_by_path = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [
            pool.submit(
                _repository_node,
                home,
                path,
                view=view,
                cache=cache,
                refresh=refresh,
            )
            for path in entries
        ]
        for completed, future in enumerate(as_completed(futures), 1):
            identifier, node, status = future.result()
            if node.children:
                repositories_by_path[identifier] = node
            report(completed, f"[{completed}/{len(entries)}] {identifier}: {status}")
    if compact_progress:
        sys.stderr.write("\r\033[K")
        sys.stderr.flush()
    tree = _directory_tree(home, repositories_by_path)
    if not tree.children:
        tree.add(
            "No test specification changes"
            if view == "diff"
            else "No direct registered submodules."
        )
    return tree


@dataclass(eq=False)
class Node:
    """One plain label with optional folded children."""

    label: str
    children: list[Node] = field(default_factory=list)
    expanded: bool = False
    change: str = ""
    detail: str = ""
    test_label: str = ""

    def add(self, label: str, *, change: str = "") -> Node:
        """Append a folded child and return it for further details."""
        child = Node(label, change=change)
        self.children.append(child)
        return child

    def add_test(self, name: str, *, change: str = "") -> None:
        """Show a readable test label while keeping the source name searchable."""
        scope, separator, leaf = name.rpartition("::" if "::" in name else ".")
        title = re.sub(r"^test(?:_|(?=[A-Z]))", "", leaf) or leaf
        label = (scope + separator + title).replace("_", " ")
        marker = f"{change} " if change else ""
        child = self.add(marker + name, change=change)
        child.test_label = marker + (label if label.strip() else name)

    def visible(self, depth: int = 0) -> Iterator[tuple[Node, int]]:
        """Visit only the rows exposed by expanded branches."""
        yield self, depth
        if self.expanded:
            for child in self.children:
                yield from child.visible(depth + 1)


def _directory_tree(home: Path, repositories_by_path: dict[str, Node]) -> Node:
    """Group repositories that share a home directory under the same node."""
    tree = Node(str(home), expanded=True)
    directories: dict[tuple[Node, str], Node] = {}
    for path, node in sorted(repositories_by_path.items()):
        parent = tree
        if node.change:
            tree.change = "~"
        for directory in Path(path).parts[:-1]:
            key = (parent, directory)
            if key not in directories:
                directories[key] = parent.add(directory)
            parent = directories[key]
            if node.change:
                parent.change = "~"
        parent.children.append(node)
    return tree


def add_tests(
    node: Node, packages: dict[str, list[str]] | None, patch: Sequence[PatchChange] = ()
) -> None:
    """Mark changed tests and include current tests and counts in the tests view."""
    names = {
        package: dict.fromkeys(tests, "") for package, tests in (packages or {}).items()
    }
    package_changes = {}
    for operation in patch:
        marker = {"add": "+", "remove": "-"}[operation["op"]]
        path = [
            part.replace("~1", "/").replace("~0", "~")
            for part in operation["path"].split("/")
        ]
        match path, operation["value"]:
            case ["", package], list(tests):
                package_changes[package] = marker
                names[package] = dict.fromkeys(tests, marker)
            case ["", package, index], str(name) if index.isdecimal() or index == "-":
                package_changes[package] = "~"
                names.setdefault(package, {})[name] = marker
            case _:
                message = f"Unexpected package test change at {operation['path']}"
                raise ValueError(message)
    if packages is not None and not names:
        node.add("No packages")
    for package, test_changes in sorted(names.items()):
        change = package_changes.get(package, "")
        label = f"{change} {package}" if change in {"+", "-"} else package
        child = node.add(label, change=change)
        if packages is not None:
            child.detail = str(len(packages.get(package, ())))
        for name, marker in sorted(
            test_changes.items(), key=lambda item: (item[1] != "-", item[0])
        ):
            child.add_test(name, change=marker)
    if patch:
        node.change = "~"


class Viewer:
    """Keep folding, selection, and scrolling independent of terminal drawing."""

    def __init__(self, root: Node) -> None:
        """Start on the home row with no scroll offset."""
        self.root = root
        self.selected = 0
        self.top = 0
        self.horizontal = 0

    def refresh(self, root: Node) -> None:
        """Keep surviving folds and the selected row when reloading a view."""
        selected = list(self.root.visible())[self.selected][0]
        replacement = None
        pairs = [(self.root, root)]
        while pairs:
            old, new = pairs.pop()
            new.expanded = old.expanded
            if old is selected:
                replacement = new
            siblings: defaultdict[str, list[Node]] = defaultdict(list)
            for child in reversed(old.children):
                siblings[child.label].append(child)
            pairs.extend(
                (siblings[child.label].pop(), child)
                for child in new.children
                if siblings[child.label]
            )
        self.root = root
        rows = list(root.visible())
        self.selected = next(
            (index for index, (row, _) in enumerate(rows) if row is replacement),
            min(self.selected, len(rows) - 1),
        )

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
            if (
                needle in match.label.casefold()
                or needle in match.test_label.casefold()
            ):
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
                char if char.isprintable() else repr(char)[1:-1]
                for char in node.test_label or node.label
            )
            if node.detail:
                label += f" ({node.detail})"
            result.append(
                (
                    f"{'  ' * depth}{marker} {label}"[self.horizontal :],
                    index == self.selected,
                    node.change,
                )
            )
        return result


def _change_colors() -> dict[str, int]:
    """Use orange for modifications on terminals with an xterm-256 palette."""
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
        with contextlib.suppress(curses.error):
            orange = (
                XTERM_ORANGE if curses.COLORS > XTERM_ORANGE else curses.COLOR_YELLOW
            )
            if curses.COLORS == RGB_COLORS:
                orange = RGB_ORANGE
            curses.init_pair(3, orange, background)
            colors["~"] = curses.color_pair(3)
    return colors


def _draw(
    screen: curses.window, viewer: Viewer, colors: dict[str, int], footer: str
) -> int:
    """Draw the visible tree and status line, returning the page height."""
    height, width = screen.getmaxyx()
    page = max(1, height - 1)
    screen.erase()
    for row, (text, selected, change) in enumerate(viewer.frame(page)):
        style = colors.get(change, 0)
        if selected:
            style |= curses.A_UNDERLINE | curses.A_BOLD if style else curses.A_REVERSE
        with contextlib.suppress(curses.error):
            screen.addnstr(
                row,
                0,
                text,
                max(0, width - 1),
                style,
            )
    with contextlib.suppress(curses.error):
        screen.addnstr(height - 1, 0, footer, max(0, width - 1))
    screen.refresh()
    return page


def _show_loading(
    screen: curses.window, view: View, completed: int = 0, total: int | None = None
) -> None:
    """Show the requested view and its repository count in the center of the screen."""
    height, width = screen.getmaxyx()
    label = f"Loading {view}…"
    if total is not None:
        label += f" {completed}/{total}"
    screen.erase()
    with contextlib.suppress(curses.error):
        screen.addnstr(
            height // 2, max(0, (width - len(label)) // 2), label, width, curses.A_BOLD
        )
    screen.refresh()


def browse(
    screen: curses.window,
    root: Node,
    view: View,
    load: Callable[[View, Callable[[int, int], None]], Node],
) -> None:
    """Draw the outline with colored changes and a highlighted selection."""
    with contextlib.suppress(curses.error):
        curses.curs_set(0)
    colors = _change_colors()
    viewer = Viewer(root)
    views = {view: viewer}
    query = None
    message = ""
    while True:
        other: View = "tests" if view == "diff" else "diff"
        help_line = f"{view}   / search   d {other}   r refresh"
        footer = "/" + query if query is not None else message or help_line
        page = _draw(screen, viewer, colors, footer)
        key = screen.get_wch()
        message = ""
        if query is not None:
            query, message = viewer.handle_search(key, query)
        elif key == "/":
            query = ""
        elif key in ("d", "r"):
            target = other if key == "d" else view
            if key == "r" or target not in views:
                show_loading = partial(_show_loading, screen, target)
                show_loading()
                try:
                    updated = load(target, show_loading)
                except READ_ERRORS as error:
                    message = f"error: {error}"
                    continue
                if key == "r":
                    viewer.refresh(updated)
                    views = {target: viewer}
                else:
                    views[target] = Viewer(updated)
            view, viewer = target, views[target]
        elif not viewer.handle(key, page):
            return


def main() -> None:
    """Reuse unchanged inspections, then open a simple expandable text tree."""
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=(
            "j/k move, h/l collapse or expand, Space toggles, g/G jump, "
            "H/L pan, 0 resets pan, / searches, d toggles diff, r refreshes, q quits."
        ),
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=["diff"],
        help="show test name changes across registered home repositories",
    )
    parser.add_argument(
        "--home",
        type=Path,
        default=Path.home(),
        help="home Git repository (default: your home directory)",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="reinspect repositories and replace cached results",
    )
    options = parser.parse_args()
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        parser.error("an interactive terminal is required")
    try:
        cache = _cache_home() / "afairesi_tui"

        load = partial(build_tree, options.home, cache=cache, refresh=options.refresh)
        view: View = options.command or "tests"
        curses.wrapper(
            browse,
            load(view=view, progress=True),
            view,
            lambda target, report: load(view=target, progress=report),
        )
    except (*READ_ERRORS, curses.error) as error:
        parser.exit(1, f"{parser.prog}: {error}\n")
    except KeyboardInterrupt:
        raise SystemExit(130) from None

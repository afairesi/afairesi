# Copyright (c) 2026- Paschalis Bizopoulos
"""Exercise home discovery, directory grouping, and CLI diffs with real repositories."""

import curses
import json
import os
import subprocess
from typing import TYPE_CHECKING
from unittest.mock import Mock, call, patch

import pytest

import afairesi_tui as subject

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def expect(condition: bool, detail: object) -> None:  # noqa: FBT001
    """Report an unexpected result with its observed value."""
    if not condition:
        raise AssertionError(detail)


def labels(node: subject.Node, *path: str) -> list[str]:
    """Read the child labels at a displayed tree path."""
    for name in path:
        node = next(child for child in node.children if child.label == name)
    return [child.test_label or child.label for child in node.children]


def git(root: Path, *arguments: str) -> str:
    """Run Git only against isolated test repositories."""
    return subprocess.run(  # noqa: S603 - Test-owned argument vectors.
        ["git", "-C", str(root), *arguments],  # noqa: S607
        text=True,
        capture_output=True,
        check=True,
        timeout=10,
    ).stdout.strip()


def commit(root: Path, message: str) -> None:
    """Commit the staged fixture files with a test identity."""
    git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "--quiet",
        "-m",
        message,
    )


def initialize(root: Path) -> None:
    """Create a committed resource without running its Nix builder."""
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "--quiet")
    (root / "flake.nix").write_text("{}")
    package = root / "example"
    package.mkdir()
    (package / "default.nix").write_text(
        "{ inputs, pkgs, ... }: inputs.self.lib.mkPythonPackage { "
        "inherit pkgs; executable = false; src = ./.; }"
    )
    (package / "test_main.py").write_text("def test_original(): ...\n")
    git(root, "add", "--force", ".")
    commit(root, "baseline")


@pytest.fixture(name="checkout")
def checkout_fixture(tmp_path: Path) -> Path:
    """Create one tracked package for cache tests."""
    root = tmp_path / "project"
    initialize(root)
    return root


def register(home: Path, child: Path) -> None:
    """Register a direct gitlink even when its path has several directory levels."""
    path = child.relative_to(home).as_posix()
    git(home, "config", "set", "--file", ".gitmodules", f"submodule.{path}.path", path)
    git(
        home,
        "config",
        "set",
        "--file",
        ".gitmodules",
        f"submodule.{path}.url",
        "https://example.invalid/repo",
    )
    git(home, "add", "--force", ".gitmodules")
    git(
        home,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{git(child, 'rev-parse', 'HEAD')},{path}",
    )


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """Include duplicate basenames, both diff stages, and unavailable repositories."""
    root = tmp_path / "home"
    root.mkdir()
    git(root, "init", "--quiet")
    first = root / "github.com/owner/project"
    for name in (
        "github.com/owner/project",
        "gitlab.com/owner/project",
        "missing",
        "broken",
    ):
        child = root / name
        initialize(child)
        register(root, child)
    (root / "missing").rename(root / "unregistered")
    (root / "broken/flake.nix").unlink()
    source = first / "example/test_main.py"
    source.write_text(source.read_text().replace("test_original", "test_staged"))
    git(first, "add", "example/test_main.py")
    source.write_text(source.read_text().replace("test_staged", "test_working"))
    return root


def test_home_tree_groups_direct_submodules_by_path_without_modifying_them(
    home: Path,
) -> None:
    """Discover direct checkouts and read diffs only for unstaged changes."""
    first = home / "github.com/owner/project"
    second = home / "gitlab.com/owner/project"
    nested = second / "nested"
    initialize(nested)
    register(second, nested)
    commit(second, "Register nested checkout")
    (nested / "example/test_main.py").write_text("def test_nested_edit(): ...\n")
    (second / "untracked.txt").touch()
    paths = [home / ".git/index", first / ".git/index", first / "example/test_main.py"]
    before = [path.read_bytes() for path in paths]
    with patch.object(subject, "command", wraps=subject.command) as command:
        tree = subject.build_tree(home, cache=home.parent / "cache")
    calls = [invocation.args for invocation in command.call_args_list]
    expect(labels(tree) == ["broken", "github.com", "gitlab.com", "missing"], tree)
    project = ("github.com", "owner", "project")
    expect(labels(tree, *project, "example") == ["- staged", "+ working"], tree)
    expect(labels(tree, *project) == ["example"], tree)
    viewer = subject.Viewer(tree)
    expect(viewer.search("test_working"), tree)
    colors = {text.strip(): color for text, _, color in viewer.frame(30)}
    expect(
        all(
            colors["▾ " + label] == "~"
            for label in (str(home), *project, "example (1)")
        ),
        colors,
    )
    expect(colors["+ working"] == "+" and colors["- staged"] == "-", colors)
    expect(colors["▸ gitlab.com"] == "", colors)
    other = ("gitlab.com", "owner", "project")
    expect(labels(tree, *other, "example") == ["original"], tree)
    expect(labels(tree, *other) == ["example"], tree)
    expect(
        labels(tree, "missing") == ["error: This submodule is not initialized."], tree
    )
    expect(any("flake.nix" in label for label in labels(tree, "broken")), tree)
    roots = {first, second, home / "broken"}
    inspections = sorted((root, args) for args, root in calls if args[0] == "afairesi")
    expected = [(root, ["afairesi"]) for root in roots]
    expected.append((first, ["afairesi", "diff"]))
    expect(inspections == sorted(expected), inspections)
    expect(all(root in roots | {home} for _, root in calls), calls)
    catalogs = [
        args for args, root in calls if root == home and args[:2] == ["git", "ls-files"]
    ]
    expect(len(catalogs) == 1, catalogs)
    expect([path.read_bytes() for path in paths] == before, "inspection modified files")


def test_home_diff_shows_working_changes_across_registered_repositories(
    home: Path,
) -> None:
    """Run afairesi diff once for each initialized direct checkout."""
    with patch.object(subject, "command", wraps=subject.command) as command:
        tree = subject.build_tree(home, view="diff", cache=home.parent / "cache")
    project = ("github.com", "owner", "project")
    expect(labels(tree, *project) == ["example"], tree)
    expect(
        labels(tree, *project, "example") == ["- staged", "+ working"],
        tree,
    )
    expect("gitlab.com" not in labels(tree), tree)
    viewer = subject.Viewer(tree)
    expect(viewer.search("test_staged"), tree)
    colors = {text.strip(): color for text, _, color in viewer.frame(30)}
    expect(colors["▾ example"] == "~", colors)
    expect(colors["- staged"] == "-", colors)
    expect(colors["+ working"] == "+", colors)
    expect(
        labels(tree, "missing") == ["error: This submodule is not initialized."], tree
    )
    inspections = [
        invocation.args[0]
        for invocation in command.call_args_list
        if invocation.args[0][0] == "afairesi"
    ]
    expect(inspections == [["afairesi", "diff"]] * 3, inspections)


def test_multilingual_test_names_are_searchable_and_diffed_under_their_packages(
    home: Path,
) -> None:
    """Follow names from the real CLI through the tree, search and diff views."""
    checkout = home / "github.com/owner/project"
    suites = {
        "shell": (
            "mkBashPackage",
            "test_main.bats",
            '@test "preserves spaces" { true; }',
        ),
        "nix": (
            "",
            "test_main.nix",
            "{ testKeepsName = { expr = 1; expected = 1; }; }",
        ),
        "proof": (
            "mkLean4Package",
            "test_main.lean",
            "namespace Output\ntheorem keepsName : True := trivial\nend Output",
        ),
        "crate": (
            "",
            "tests/output.rs",
            "mod output { #[test] fn test_keeps_name() {} }",
        ),
        "untested": ("", "test_main.nix", "{}"),
    }
    for package, (builder, filename, source) in suites.items():
        directory = checkout / package
        path = directory / filename
        path.parent.mkdir(parents=True)
        (directory / "default.nix").write_text(
            f"{{ inputs, ... }}: inputs.self.lib.{builder} {{}}" if builder else "{}"
        )
        path.write_text(source + "\n")
    (checkout / "crate/Cargo.toml").write_text(
        '[package]\nname = "crate"\nversion = "0.1.0"\n'
    )
    git(checkout, "add", "--", *suites)
    cache = home.parent / "cache"
    tree = subject.build_tree(home, cache=cache)
    viewer = subject.Viewer(tree)
    project = ("github.com", "owner", "project")
    for package, name in {
        "shell": "preserves spaces",
        "nix": "KeepsName",
        "proof": "Output.keepsName",
        "crate": "output::keeps name",
    }.items():
        expect(labels(tree, *project, package) == [name], package)
        expect(viewer.search(name), name)
        frame = viewer.frame(40)
        expect(
            any(selected and row.strip() == name for row, selected, _ in frame), frame
        )
        expect(
            any(row.strip() == f"▾ {package} (1)" for row, _, _ in frame),
            frame,
        )
    expect(viewer.search("untested"), tree)
    frame = viewer.frame(40)
    expect(
        any(selected and row.strip() == "untested (0)" for row, selected, _ in frame),
        frame,
    )

    subject.cached_output(checkout, "diff", cache, refresh=False)
    (checkout / "shell/test_main.bats").write_text(
        '@test "preserves tabs" { true; }\n@test "accepts blanks" { true; }\n'
    )
    for package in ("crate", "proof", "untested"):
        (checkout / package / "default.nix").unlink()
    changes = subject.build_tree(home, view="diff", cache=cache)
    expect(labels(changes, *project, "- crate") == ["- output::keeps name"], changes)
    expect(
        labels(changes, *project, "shell")
        == ["- preserves spaces", "+ accepts blanks", "+ preserves tabs"],
        changes,
    )
    expect(labels(changes, *project, "- proof") == ["- Output.keepsName"], changes)
    expect("- untested" in labels(changes, *project), changes)
    tests = subject.Viewer(subject.build_tree(home, cache=cache))
    expect(tests.search("+ preserves tabs"), tests.root)
    rows = {row.strip(): color for row, _, color in tests.frame(40)}
    expect(rows["▾ shell (2)"] == "~" and rows["▸ - crate (0)"] == "-", rows)
    expect(rows["- preserves spaces"] == "-" and rows["+ preserves tabs"] == "+", rows)


def test_home_tree_shows_a_timeout_alongside_results_from_other_repositories(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Show progress and retain successful results when one CLI inspection times out."""
    original: Callable[..., str] = subject.command

    def timeout(
        arguments: list[str], root: Path, *, input_text: str | None = None
    ) -> str:
        if arguments == ["afairesi"] and root == home / "github.com/owner/project":
            raise subprocess.TimeoutExpired(arguments, 30)
        return original(arguments, root, input_text=input_text)

    monkeypatch.setattr(subject, "command", timeout)
    tree = subject.build_tree(home, progress=True, cache=home.parent / "cache")
    expect(
        any(
            "timed out" in label
            for label in labels(tree, "github.com", "owner", "project")
        ),
        tree,
    )
    expect("example" in labels(tree, "gitlab.com", "owner", "project"), tree)
    progress = capsys.readouterr().err
    expect("4 direct submodules" in progress and "[4/4]" in progress, progress)


def test_home_tree_requires_a_git_root_and_accepts_one_without_submodules(
    tmp_path: Path,
) -> None:
    """Show an empty valid home, but reject a nested directory or a non-repository."""
    cache = tmp_path / "cache"
    with pytest.raises(ValueError, match="not a git repository"):
        subject.build_tree(tmp_path, cache=cache)
    git(tmp_path, "init", "--quiet")
    tree = subject.build_tree(tmp_path, cache=cache)
    expect(
        tree.label == str(tmp_path)
        and labels(tree) == ["No direct registered submodules."],
        tree,
    )
    differences = subject.build_tree(tmp_path, cache=cache, view="diff")
    expect(labels(differences) == ["No test specification changes"], differences)
    child = tmp_path / "directory"
    child.mkdir()
    with pytest.raises(ValueError, match="repository root"):
        subject.build_tree(child, cache=cache)


@pytest.mark.parametrize("view", ["tests", "diff"])
def test_both_views_reuse_cached_results_until_their_test_inputs_change(
    checkout: Path,
    view: subject.View,
) -> None:
    """Use working names for the tests view and staged names as the diff baseline."""
    cache = checkout.parent / "cache"
    nested = checkout / "nested"
    initialize(nested)
    register(checkout, nested)
    source = checkout / "example/test_main.py"
    source.write_text("def test_working(): ...\n")
    first, hit = subject.cached_output(checkout, view, cache, refresh=False)
    expect(bool(first) and not hit, first)
    (checkout / "example/main.py").write_text("irrelevant")
    git(checkout, "add", "example/main.py")
    (nested / "example/test_main.py").write_text("def test_nested(): ...\n")
    again, hit = subject.cached_output(checkout, view, cache, refresh=False)
    expect(hit and first == again, again)
    metadata = source.stat()
    source.write_text(source.read_text().replace("test_working", "test_updated"))
    os.utime(source, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
    changed, hit = subject.cached_output(checkout, view, cache, refresh=False)
    expect(not hit and changed != first, changed)
    git(checkout, "add", "example/test_main.py")
    staged, hit = subject.cached_output(checkout, view, cache, refresh=False)
    if view == "tests":
        expect(hit and staged == changed == {"example": ["test_updated"]}, staged)
    else:
        expect(not hit and staged == [], staged)
    commit(checkout, "updated")
    committed, hit = subject.cached_output(checkout, view, cache, refresh=False)
    expect(hit and committed == staged, committed)
    _, hit = subject.cached_output(checkout, view, cache, refresh=True)
    expect(not hit, "explicit refresh used cached data")
    git(checkout, "rm", "--cached", "example/test_main.py")
    untracked, _ = subject.cached_output(checkout, view, cache, refresh=False)
    expect(untracked == ({"example": []} if view == "tests" else []), untracked)


def test_home_tree_shows_modified_files_with_cached_test_names(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Read repository status again when test names come from the cache."""
    root = home / "gitlab.com/owner/project"
    cache = home / "cache"
    subject.cached_output(root, "tests", cache, refresh=False)
    (root / "notes.txt").touch()
    git(root, "add", "notes.txt")
    tree = subject.build_tree(home, cache=cache, progress=True)
    expect(labels(tree, "gitlab.com", "owner", "project") == ["example"], tree)
    viewer = subject.Viewer(tree)
    expect(viewer.search("gitlab.com"), tree)
    expect(
        any(selected and color == "~" for _, selected, color in viewer.frame(8)), tree
    )
    progress = capsys.readouterr().err
    expect("gitlab.com/owner/project: cached" in progress, progress)


def test_test_names_are_reloaded_when_the_cache_is_damaged_or_unavailable(
    checkout: Path,
) -> None:
    """Read fresh test names when cached JSON is malformed or storage is unavailable."""
    cache = checkout.parent / "cache"
    expected, _ = subject.cached_output(checkout, "tests", cache, refresh=False)
    entry = next(cache.glob("*.json"))
    saved = json.loads(entry.read_text())
    entry.write_text(json.dumps({**saved, "value": {"example": [None]}}))
    actual, hit = subject.cached_output(checkout, "tests", cache, refresh=False)
    expect(not hit and actual == expected, actual)
    entry.write_text("partial {")
    actual, hit = subject.cached_output(checkout, "tests", cache, refresh=False)
    expect(not hit and actual == expected, actual)
    blocked = checkout.parent / "blocked-cache"
    blocked.write_text("not a directory")
    actual, hit = subject.cached_output(checkout, "tests", blocked, refresh=False)
    expect(not hit and actual == expected, actual)


def test_test_name_cache_only_stores_successful_reads_of_unchanged_sources(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retry errors and avoid persisting a result read while the source was changing."""
    cache = checkout.parent / "cache"
    flake = checkout / "flake.nix"
    flake.unlink()
    with pytest.raises(ValueError, match=r"flake\.nix"):
        subject.cached_output(checkout, "tests", cache, refresh=False)
    expect(not cache.exists(), "failed inspection was cached")
    flake.write_text("{}")
    original = subject.read_output

    def changing(path: Path, view: subject.View) -> object:
        value = original(path, view)
        (path / "example/test_main.py").write_text("def test_changed(): ...\n")
        return value

    monkeypatch.setattr(subject, "read_output", changing)
    subject.cached_output(checkout, "tests", cache, refresh=False)
    expect(not cache.exists(), "changing source was cached")


def test_test_name_cache_refreshes_when_a_symlink_target_changes(
    checkout: Path,
) -> None:
    """Read new test names through an unchanged suite symlink."""
    cache = checkout.parent / "cache"
    target = checkout.parent / "external.py"
    tests = checkout / "example/test_main.py"
    tests.rename(target)
    tests.symlink_to(target)
    git(checkout, "add", "example/test_main.py")
    subject.cached_output(checkout, "tests", cache, refresh=False)
    target.write_text("def test_other(): ...\n")
    value, hit = subject.cached_output(checkout, "tests", cache, refresh=False)
    expect(not hit and value == {"example": ["test_other"]}, value)


def test_viewer_starts_with_folded_directories_and_supports_vim_navigation() -> None:
    """Show meaningful labels and let h/l fold and unfold details without JSON noise."""
    root = subject.Node("/home/test", expanded=True)
    project = root.add("github.com").add("owner").add("project")
    subject.add_tests(project, {"hello": ["test_greets_the_reader"]})
    viewer = subject.Viewer(root)
    expect(
        viewer.frame(30) == [("▾ /home/test", True, ""), ("  ▸ github.com", False, "")],
        viewer.frame(30),
    )
    for key in "lllll":
        viewer.handle(key, 30)
    repository = list(viewer.root.visible())[viewer.selected][0]
    expect(repository.label == "project" and not repository.expanded, repository)
    expect(
        [node.label for node in repository.children] == ["hello"],
        repository.children,
    )
    for key in "ljl":
        viewer.handle(key, 30)
    package = list(viewer.root.visible())[viewer.selected][0]
    expect(package.label == "hello" and package.expanded, package)
    expect(labels(package) == ["greets the reader"], package.children)
    for key in "hhh":
        viewer.handle(key, 30)
    expect(
        list(viewer.root.visible())[viewer.selected][0] is repository
        and not repository.expanded,
        viewer.frame(30),
    )
    viewer.handle(" ", 30)
    expect(repository.expanded, "Space did not expand repository")
    viewer.handle("\n", 30)
    expect(not repository.expanded, "Enter did not fold repository")
    expect(not viewer.handle("q", 30), "q did not quit")


def test_viewer_keeps_the_selection_visible_when_paging_or_resizing() -> None:
    """Keep navigation visible across viewport changes, paging, and collapsed roots."""
    root = subject.Node("home", expanded=True)
    for number in range(12):
        root.add(f"repository {number}")
    viewer = subject.Viewer(root)
    viewer.handle("G", 4)
    expect(viewer.frame(4)[-1] == ("    repository 11", True, ""), viewer.frame(4))
    expect(
        viewer.frame(2)
        == [("    repository 10", False, ""), ("    repository 11", True, "")],
        viewer.frame(2),
    )
    viewer.handle(curses.KEY_PPAGE, 2)
    expect(viewer.frame(2)[0] == ("    repository 9", True, ""), viewer.frame(2))
    viewer.handle(curses.KEY_DOWN, 2)
    expect(viewer.frame(2)[-1][1], viewer.frame(2))
    viewer.handle(curses.KEY_UP, 2)
    expect(viewer.frame(2)[0] == ("    repository 9", True, ""), viewer.frame(2))
    viewer.handle("\x06", 2)
    viewer.handle("j", 2)
    expect(viewer.frame(2)[-1] == ("    repository 11", True, ""), viewer.frame(2))
    viewer.handle("h", 2)
    expect(viewer.selected == 0, viewer.selected)
    viewer.handle(curses.KEY_LEFT, 2)
    expect(viewer.frame(2) == [("▸ home", True, "")], viewer.frame(2))
    viewer.handle(curses.KEY_RIGHT, 2)
    viewer.handle("g", 2)
    viewer.handle("k", 2)
    expect(viewer.selected == 0, viewer.selected)


def test_viewer_pans_and_displays_control_characters_as_text() -> None:
    """Long labels remain accessible and source text cannot inject terminal controls."""
    viewer = subject.Viewer(subject.Node("hello\nworld\x1b[31m"))
    label = "  hello\\nworld\\x1b[31m"
    expect(viewer.frame(1) == [(label, True, "")], viewer.frame(1))
    viewer.handle("L", 1)
    viewer.handle("L", 1)
    expect(viewer.frame(1) == [(label[8:], True, "")], viewer.frame(1))
    viewer.handle("H", 1)
    expect(viewer.frame(1) == [(label[4:], True, "")], viewer.frame(1))
    viewer.handle("0", 1)
    viewer.handle("H", 1)
    expect(viewer.frame(1) == [(label, True, "")], viewer.frame(1))


@pytest.mark.parametrize(
    ("palette", "modified"), [(1 << 24, 0xFF8700), (256, 208), (8, curses.COLOR_YELLOW)]
)
def test_browse_refreshes_both_views_and_preserves_navigation(
    monkeypatch: pytest.MonkeyPatch, palette: int, modified: int
) -> None:
    """Reload each mode and allow r in search queries."""
    screen = Mock(spec=curses.window)
    screen.getmaxyx.return_value = (24, 80)
    screen.get_wch.side_effect = ["d", "j", "j", "/", "r", "\n", "r", "d", "r", "q"]
    monkeypatch.setattr(curses, "has_colors", lambda: True)
    monkeypatch.setattr(curses, "use_default_colors", lambda: None)
    monkeypatch.setattr(curses, "COLORS", palette, raising=False)
    pairs = Mock()
    monkeypatch.setattr(curses, "init_pair", pairs)
    monkeypatch.setattr(curses, "color_pair", lambda pair: pair << 8)
    calls = []

    def load(view: subject.View, _report: Callable[[int, int], None]) -> subject.Node:
        calls.append(view)
        tree = subject.Node("home", expanded=True)
        if view == "diff":
            subject.add_tests(
                tree,
                None,
                [
                    {"op": "add", "path": "/new", "value": []},
                    {"op": "remove", "path": "/old", "value": []},
                ],
            )
        group = tree.add("group")
        group.add(f"generation {len(calls)}")
        return tree

    subject.browse(screen, subject.Node("home"), "tests", load)
    expect(calls == ["diff", "diff", "tests", "tests"], calls)
    pairs.assert_has_calls(
        [
            call(1, curses.COLOR_GREEN, -1),
            call(2, curses.COLOR_RED, -1),
            call(3, modified, -1),
        ]
    )
    lines = [args.args[2] for args in screen.addnstr.call_args_list]
    expect("/r" in lines and any("r refresh" in line for line in lines), lines)
    expect("  ▸ group" in lines, lines)
    for label, pair in (("+ new", 1), ("- old", 2)):
        styles = {
            args.args[-1]
            for args in screen.addnstr.call_args_list
            if args.args[2].strip() == label
        }
        color = curses.color_pair(pair)
        expect(styles == {color, color | curses.A_BOLD | curses.A_UNDERLINE}, styles)


def test_switching_to_diff_shows_repository_progress_until_the_view_is_ready(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Show progress before discovery and after each repository, then reuse the view."""
    screen = Mock(spec=curses.window)
    screen.getmaxyx.return_value = (24, 80)
    screen.get_wch.side_effect = ["d", "d", "d", "q"]
    monkeypatch.setattr(curses, "has_colors", lambda: False)
    frames: list[str] = []
    screen.refresh.side_effect = lambda: frames.append(screen.addnstr.call_args.args[2])

    def load(view: subject.View, report: Callable[[int, int], None]) -> subject.Node:
        expect(frames[-1] == "Loading diff…", frames)
        return subject.build_tree(
            home, view=view, cache=home / "cache", progress=report
        )

    subject.browse(screen, subject.Node("home"), "tests", load)
    expect(
        frames[1:-3] == ["Loading diff…", *(f"Loading diff… {n}/4" for n in range(5))],
        frames,
    )
    expect(
        [line.split()[0] for line in frames[-3:]] == ["diff", "tests", "diff"],
        frames,
    )


def test_viewer_refresh_keeps_an_expanded_selection_when_rows_move() -> None:
    """Match selected rows by their tree location and clamp after removals."""
    root = subject.Node("home", expanded=True)
    root.add("repository").add("field")
    viewer = subject.Viewer(root)
    viewer.search("field")
    updated = subject.Node("home", expanded=True)
    updated.add("another")
    updated.add("repository").add("field")
    viewer.refresh(updated)
    expect(viewer.frame(10)[-1] == ("      field", True, ""), viewer.frame(10))
    viewer.refresh(subject.Node("home"))
    expect(viewer.selected == 0, viewer.selected)

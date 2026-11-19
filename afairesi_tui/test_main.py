# Copyright (c) 2026- Paschalis Bizopoulos
"""Exercise home discovery, directory grouping, and CLI diffs with real repositories."""

from __future__ import annotations

import curses
import os
import subprocess
from typing import TYPE_CHECKING

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
    return [child.label for child in node.children]


def git(root: Path, *arguments: str) -> str:
    """Run Git only against isolated test repositories."""
    return subprocess.run(  # noqa: S603 - Test-owned argument vectors.
        ["git", "-C", str(root), *arguments],  # noqa: S607
        text=True,
        capture_output=True,
        check=True,
        timeout=10,
    ).stdout.strip()


def initialize(root: Path) -> None:
    """Create a committed resource without running its Nix builder."""
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "--quiet")
    (root / "flake.nix").write_text("{}")
    package = root / "example"
    package.mkdir()
    (package / "default.nix").write_text(
        '{ pkgs, ... }: pkgs.runCommand "example" '
        '{ meta.description = "original"; } "exit 99"'
    )
    git(root, "add", "--force", ".")
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
        "baseline",
    )


def register(home: Path, child: Path) -> None:
    """Register a direct gitlink even when its path has several directory levels."""
    path = child.relative_to(home).as_posix()
    git(home, "config", "--file", ".gitmodules", f"submodule.{path}.path", path)
    git(
        home,
        "config",
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
    (root / "missing").rename(root / "parked")
    (root / "broken/flake.nix").unlink()
    initialize(root / "unregistered")
    nested = first / "nested"
    initialize(nested)
    register(first, nested)
    source = first / "example/default.nix"
    source.write_text(source.read_text().replace('"original"', '"staged"'))
    git(first, "add", "example/default.nix")
    source.write_text(source.read_text().replace('"staged"', '"working"'))
    return root


def test_home_tree_groups_direct_submodules_by_path_without_modifying_them(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Discover once and inspect each direct checkout once, without eager diffs."""
    calls: list[tuple[list[str], Path]] = []
    original: Callable[..., str] = subject.command

    def counted(
        arguments: list[str], root: Path, *, input_text: str | None = None
    ) -> str:
        calls.append((arguments, root))
        return original(arguments, root, input_text=input_text)

    monkeypatch.setattr(subject, "command", counted)
    first = home / "github.com/owner/project"
    paths = [home / ".git/index", first / ".git/index", first / "example/default.nix"]
    before = [path.read_bytes() for path in paths]
    tree = subject.build_tree(home, cache=home.parent / "cache")
    expect(labels(tree) == ["broken", "github.com", "gitlab.com", "missing"], tree)
    project = ("github.com", "owner", "project")
    expect("description: working" in labels(tree, *project, "example"), tree)
    expect("modified" in labels(tree, *project), tree)
    expect(
        "description: original"
        in labels(tree, "gitlab.com", "owner", "project", "example"),
        tree,
    )
    expect(
        labels(tree, "missing") == ["error: This submodule is not initialized."], tree
    )
    expect(any("flake.nix" in label for label in labels(tree, "broken")), tree)
    roots = {first, home / "gitlab.com/owner/project", home / "broken"}
    inspections = [(args, root) for args, root in calls if args[0] == "afairesi"]
    expect(
        len(inspections) == len(roots) and {root for _, root in inspections} == roots,
        inspections,
    )
    expect(all(args == ["afairesi"] for args, _ in inspections), inspections)
    expect(all(root in roots | {home} for _, root in calls), calls)
    catalogs = [
        args for args, root in calls if root == home and args[:2] == ["git", "ls-files"]
    ]
    expect(len(catalogs) == 1, catalogs)
    for args, _ in calls:
        if args[:2] == ["git", "status"]:
            expect(
                "--ignore-submodules=all" in args and "--untracked-files=no" in args,
                args,
            )
    expect([path.read_bytes() for path in paths] == before, "inspection modified files")


@pytest.mark.parametrize("comparison", ["working", "staged"])
def test_home_diff_shows_staged_or_working_changes_across_registered_repositories(
    home: Path,
    comparison: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run only the requested diff stage once for each initialized direct checkout."""
    calls = []
    original: Callable[..., str] = subject.command

    def counted(
        arguments: list[str], root: Path, *, input_text: str | None = None
    ) -> str:
        if arguments[0] == "afairesi":
            calls.append(arguments)
        return original(arguments, root, input_text=input_text)

    monkeypatch.setattr(subject, "command", counted)
    tree = subject.build_tree(home, comparison=comparison, cache=home.parent / "cache")
    project = ("github.com", "owner", "project")
    expect(labels(tree, *project) == ["example"], tree)
    baseline = "staged" if comparison == "working" else "original"
    expect(
        labels(tree, *project, "example")
        == [f"- description: {baseline}", f"+ description: {comparison}"],
        tree,
    )
    expect("gitlab.com" not in labels(tree), tree)
    expect(
        labels(tree, "missing") == ["error: This submodule is not initialized."], tree
    )
    expected = ["afairesi", "diff", *(["--cached"] if comparison == "staged" else [])]
    expect(calls == [expected] * 3, calls)


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
    differences = subject.build_tree(tmp_path, cache=cache, comparison="working")
    expect(labels(differences) == ["No changes"], differences)
    child = tmp_path / "directory"
    child.mkdir()
    with pytest.raises(ValueError, match="repository root"):
        subject.build_tree(child, cache=cache)


def test_cached_results_are_reused_until_a_tracked_change_or_explicit_refresh(
    home: Path,
) -> None:
    """Refresh for tracked edits, staging, commits, and explicit refresh requests."""
    root = home / "github.com/owner/project"
    cache = home / "cache"
    source = root / "example/default.nix"
    first, hit = subject.cached_output(root, None, cache, refresh=False)
    expect(not hit, "first inspection was cached")
    second, hit = subject.cached_output(root, None, cache, refresh=False)
    expect(hit and first == second, "unchanged inspection was repeated")
    nested = root / "nested/example/default.nix"
    nested.write_text(nested.read_text() + "\n")
    _, hit = subject.cached_output(root, None, cache, refresh=False)
    expect(hit, "nested checkout invalidated the direct repository cache")
    metadata = source.stat()
    source.write_text(source.read_text().replace('"working"', '"updated"'))
    os.utime(source, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
    changed, hit = subject.cached_output(root, None, cache, refresh=False)
    expect(not hit and changed["example"]["description"] == "updated", changed)
    git(root, "add", "example/default.nix")
    _, hit = subject.cached_output(root, None, cache, refresh=False)
    expect(not hit, "index change was missed")
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
        "updated",
    )
    _, hit = subject.cached_output(root, None, cache, refresh=False)
    expect(not hit, "HEAD change was missed")
    _, hit = subject.cached_output(root, None, cache, refresh=True)
    expect(not hit, "explicit refresh used cached data")


def test_home_tree_shows_the_current_branch_even_with_a_cached_inspection(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Cache each operation independently while reading Git state fresh."""
    root = home / "github.com/owner/project"
    cache = home / "cache"
    for comparison in (None, "working", "staged"):
        _, hit = subject.cached_output(root, comparison, cache, refresh=False)
        expect(not hit, comparison)
        _, hit = subject.cached_output(root, comparison, cache, refresh=False)
        expect(hit, comparison)
    git(root, "checkout", "--quiet", "-b", "cached-branch")
    tree = subject.build_tree(home, cache=cache, progress=True)
    expect(
        "branch: cached-branch" in labels(tree, "github.com", "owner", "project"), tree
    )
    progress = capsys.readouterr().err
    expect("github.com/owner/project: cached" in progress, progress)


def test_inspection_returns_resources_even_if_the_cache_is_corrupt_or_unavailable(
    home: Path,
) -> None:
    """Treat cache data as disposable; cache I/O failures must not hide resources."""
    root = home / "github.com/owner/project"
    cache = home / "cache"
    expected, _ = subject.cached_output(root, None, cache, refresh=False)
    next(cache.glob("*.json")).write_text("partial {")
    actual, hit = subject.cached_output(root, None, cache, refresh=False)
    expect(not hit and actual == expected, actual)
    blocked = home / "blocked-cache"
    blocked.write_text("not a directory")
    actual, hit = subject.cached_output(root, None, blocked, refresh=False)
    expect(not hit and actual == expected, actual)


def test_inspection_caches_only_successful_results_from_unchanged_sources(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retry errors and avoid persisting a result read while the source was changing."""
    root = home / "github.com/owner/project"
    cache = home / "cache"
    flake = root / "flake.nix"
    flake.unlink()
    with pytest.raises(ValueError, match=r"flake\.nix"):
        subject.cached_output(root, None, cache, refresh=False)
    expect(not cache.exists(), "failed inspection was cached")
    flake.write_text("{}")
    original = subject.read_output

    def changing(path: Path, comparison: str | None) -> object:
        value = original(path, comparison)
        source = root / "example/default.nix"
        source.write_text(source.read_text().replace('"working"', '"changed"'))
        return value

    monkeypatch.setattr(subject, "read_output", changing)
    subject.cached_output(root, None, cache, refresh=False)
    expect(not cache.exists(), "changing source was cached")


def test_inspection_refreshes_when_a_symlinked_entrypoint_changes(home: Path) -> None:
    """A tracked Python entrypoint can change through an unchanged symlink."""
    root = home / "github.com/owner/project"
    cache = home / "cache"
    (root / "example/default.nix").write_text(
        "{ inputs, pkgs, ... }: inputs.self.lib.mkPythonPackage { "
        "inherit pkgs; executable = true; src = ./.; }"
    )
    target = home / "external.py"
    target.write_text(
        "import argparse\ndef main():\n    p = argparse.ArgumentParser()\n"
        '    p.add_argument("--name", default="first")\n    p.parse_args()\n'
    )
    (root / "example/main.py").symlink_to(target)
    git(root, "add", "example/default.nix", "example/main.py")
    subject.cached_output(root, None, cache, refresh=False)
    target.write_text(target.read_text().replace('"first"', '"other"'))
    value, hit = subject.cached_output(root, None, cache, refresh=False)
    expect(not hit and value["example"]["arguments"][0]["default"] == "other", value)


def test_viewer_starts_with_folded_directories_and_supports_vim_navigation() -> None:
    """Show meaningful labels and let h/l fold and unfold details without JSON noise."""
    root = subject.Node("/home/test", expanded=True)
    project = root.add("github.com").add("owner").add("project")
    project.add("branch: main")
    project.add("modified")
    subject.add_resources(
        project,
        {
            "hello": {
                "type": "python",
                "description": "Hello [red]literal[/red]",
                "arguments": [{"names": ["--name"], "default": "world"}],
                "tests": ["test_hello"],
                "dependencies": {"library": ["runtime", "test"]},
            }
        },
    )
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
        [node.label for node in repository.children]
        == ["branch: main", "modified", "hello"],
        repository.children,
    )
    for key in "ljjjl":
        viewer.handle(key, 30)
    resource = list(viewer.root.visible())[viewer.selected][0]
    expect(resource.label == "hello" and resource.expanded, resource)
    expect(
        resource.children[1].label == "description: Hello [red]literal[/red]",
        resource.children,
    )
    arguments = resource.children[2]
    expect(arguments.label == "arguments", arguments)
    expect(arguments.children[0].label == "--name", arguments.children)
    dependencies = resource.children[4]
    expect(dependencies.children[0].label == "library (runtime, test)", dependencies)
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

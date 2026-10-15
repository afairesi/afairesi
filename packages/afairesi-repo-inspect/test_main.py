# Copyright (c) 2026- Paschalis Bizopoulos
"""Verify read-only inventories and static argparse discovery."""

from __future__ import annotations

import ast
import json
import os
import re
import stat
import subprocess
from importlib import import_module
from pathlib import Path
from typing import Any, cast

import pytest
from afairesi import (
    CommandError,
    git,
    initialize_home,
    initialize_submodule,
)

subject = import_module("packages.afairesi-repo-inspect.main")


def _expect(condition: bool, message: str) -> None:  # noqa: FBT001
    if not condition:
        raise AssertionError(message)


def _discovery(source: str) -> dict[str, Any]:
    return cast("dict[str, Any]", subject.ArgumentDiscovery(ast.parse(source)).report())


def test_argparse_helpers_factories_and_subcommands() -> None:
    """Resolve aliases, returned parsers, groups, and helper contexts."""
    report = _discovery("""
import argparse as ap
def configure(cli):
    group = cli.add_mutually_exclusive_group()
    group.add_argument("--quiet", action="store_true")
def parser():
    cli = ap.ArgumentParser()
    cli.add_argument("input", type=str)
    subs = cli.add_subparsers(dest="command")
    build = subs.add_parser("build")
    build.add_argument("-j", "--jobs", type=int, default=2, choices=[1, 2])
    configure(build)
    return cli
def main():
    parser().parse_args()
""")
    _expect(list(report["commands"]) == ["build"], "lost subcommand")
    _expect(
        report["arguments"] == [{"names": ["input"], "options": {"type": "str"}}],
        "wrong positional scope",
    )
    rows = {
        tuple(row["names"]): row for row in report["commands"]["build"]["arguments"]
    }
    _expect(
        rows[("-j", "--jobs")]["options"]
        == {"type": "int", "default": 2, "choices": [1, 2]},
        "lost metadata",
    )
    _expect(
        rows[("--quiet",)]
        == {"names": ["--quiet"], "options": {"action": "store_true"}},
        "lost helper context",
    )
    _expect(not report["warnings"], "resolved declarations produced warnings")


def test_nested_commands_and_commands_without_arguments() -> None:
    """Keep empty commands and arguments with the same name in distinct scopes."""
    report = _discovery("""
import argparse
cli = argparse.ArgumentParser()
cli.add_argument("--verbose", action="store_true")
commands = cli.add_subparsers()
commands.add_parser("version")
remote = commands.add_parser("remote")
remote.add_argument("--verbose", action="count")
subcommands = remote.add_subparsers()
subcommands.add_parser("list")
add = subcommands.add_parser("add")
add.add_argument("url")
""")
    _expect(
        report
        == {
            "status": "argparse",
            "arguments": [
                {"names": ["--verbose"], "options": {"action": "store_true"}},
            ],
            "commands": {
                "version": {"arguments": [], "commands": {}},
                "remote": {
                    "arguments": [
                        {"names": ["--verbose"], "options": {"action": "count"}},
                    ],
                    "commands": {
                        "list": {"arguments": [], "commands": {}},
                        "add": {
                            "arguments": [{"names": ["url"], "options": {}}],
                            "commands": {},
                        },
                    },
                },
            },
            "warnings": [],
        },
        "command tree lost scope or empty commands",
    )


def test_unknown_declarations_are_explicit() -> None:
    """Keep unresolved metadata and parser bindings visible."""
    report = _discovery("""
from argparse import ArgumentParser as Parser
cli = Parser()
cli.add_argument("--output", default=compute_default(), **options)
other.add_argument("--external")
""")
    options = report["arguments"][0]["options"]
    _expect(
        options["default"] == {"unknown": "compute_default()"},
        "default was guessed",
    )
    _expect(
        options["**kwargs"] == {"unknown": "options"},
        "unresolved kwargs were hidden",
    )
    _expect(bool(report["warnings"]), "unresolved parser was hidden")


def test_factory_return_and_keyword_helper() -> None:
    """Follow returned constructors and keyword helper arguments."""
    report = _discovery("""
from argparse import ArgumentParser
def configure(target):
    target.add_argument("--required", required=True)
def parser():
    return ArgumentParser()
def main():
    cli = parser()
    configure(target=cli)
""")
    _expect(
        report["arguments"]
        == [{"names": ["--required"], "options": {"required": True}}],
        "factory or keyword binding was lost",
    )


def test_declared_test_identifiers() -> None:
    """Retain sorted test identifiers and class context."""
    module = ast.parse("""
from unittest import TestCase as Case
def test_default_output(): pass
def helper():
    def test_nested(): pass
class TestArguments:
    async def test_missing_input(self): pass
class Arguments(Case):
    def test_valid_input(self): pass
class Derived(Arguments):
    def test_other_input(self): pass
class Helper:
    def test_not_collected(self): pass
""")
    rows = subject.declared_tests(module)
    _expect(
        rows
        == [
            "Arguments::test_valid_input",
            "Derived::test_other_input",
            "Derived::test_valid_input",
            "TestArguments::test_missing_input",
            "test_default_output",
        ],
        "wrong test identifiers or collection scope",
    )


def _repository(root: Path) -> None:
    (root / "flake.nix").touch()
    package = root / "packages" / "example"
    package.mkdir(parents=True)
    (package / "default.nix").write_text('{ meta.description = "Example"; }')
    (package / "main.py").write_text("""
import argparse
raise RuntimeError("inspection must not execute this")
def parser():
    cli = argparse.ArgumentParser()
    cli.add_argument("--output", default="result")
    return cli
""")
    (package / "test_main.py").write_text("""
raise RuntimeError("inspection must not collect or run tests")
def test_default_output(): pass
""")
    host = root / "hosts" / "laptop"
    host.mkdir(parents=True)
    (host / "configuration.nix").touch()
    library = root / "packages" / "library"
    library.mkdir()
    (library / "default.nix").write_text("{}")


def test_inspection_is_read_only(tmp_path: Path) -> None:
    """Inspect source containing failing statements without executing or writing."""
    _repository(tmp_path)
    before = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    report = subject.inspect_repository(tmp_path)
    after = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    _expect(before == after, "inspection wrote to the repository")
    _expect(
        report["hosts"] == ["laptop"],
        "wrong hosts",
    )
    _expect(
        report["packages"]["library"]["cli"]["status"] == "not applicable",
        "non-Python package has CLI metadata",
    )
    _expect(
        report["packages"]["example"]["tests"] == ["test_default_output"],
        "wrong test identifiers",
    )
    _expect(report["packages"]["library"]["tests"] == [], "wrong empty tests")


@pytest.mark.parametrize("location", ["outside", "root", "nested"])
def test_cli_inventory_resolves_repository_paths(
    tmp_path: Path,
    location: str,
) -> None:
    """Return the same JSON inventory through supported paths inside and outside Git."""
    _repository(tmp_path)
    expected = subject.inspect_repository(tmp_path)
    _expect(
        "test_default_output" in expected["packages"]["example"]["tests"],
        "inventory lost test identifier",
    )
    if location != "outside":
        subprocess.run(  # noqa: S603
            ["git", "init", "--quiet", str(tmp_path)],  # noqa: S607
            check=True,
            capture_output=True,
            timeout=10,
        )
    cwd = tmp_path / "packages/example" if location == "nested" else tmp_path
    paths = [["."], [str(tmp_path)]]
    if location != "outside":
        paths.append([])
    for arguments in paths:
        completed = subprocess.run(  # noqa: S603
            [os.environ["PACKAGE_E2E_EXECUTABLE"], *arguments],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        _expect(
            json.loads(completed.stdout) == expected,
            "path resolution changed the CLI inventory",
        )


def test_invalid_repository_and_source(tmp_path: Path) -> None:
    """Report invalid repositories and malformed source with useful errors."""
    with pytest.raises(CommandError, match=r"flake\.nix"):
        subject.inspect_repository(tmp_path)
    _repository(tmp_path)
    (tmp_path / "packages" / "example" / "main.py").write_text("def invalid(")
    with pytest.raises(CommandError, match="could not parse Python source"):
        subject.inspect_repository(tmp_path)


def test_empty_repository(tmp_path: Path) -> None:
    """Report missing hosts as a list and missing packages as an object."""
    (tmp_path / "flake.nix").touch()
    _expect(
        subject.inspect_repository(tmp_path) == {"hosts": [], "packages": {}},
        "empty collections were omitted",
    )


def _remote_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path, str]:
    """Serve a hosted URL from a local repository without network access."""
    home, source = tmp_path / "home", tmp_path / "source"
    home.mkdir()
    source.mkdir()
    _repository(source)
    git(source, ["init", "--quiet"])
    git(source, ["config", "user.name", "Test"])
    git(source, ["config", "user.email", "test@example.test"])
    git(source, ["add", "."])
    git(source, ["commit", "--quiet", "-m", "initial"])
    remote = "https://example.test/team/repository.git"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{source.as_uri()}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", remote)
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")
    return home, source, remote


def _inspect_target(target: str) -> dict[str, Any]:
    """Exercise the installed CLI with an unchanged path or Git URL."""
    completed = subprocess.run(  # noqa: S603
        [os.environ["PACKAGE_E2E_EXECUTABLE"], target],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return cast("dict[str, Any]", json.loads(completed.stdout))


def _tree_snapshot(root: Path) -> dict[Path, tuple[int, bytes]]:
    """Observe contents and modes, including Git metadata and symbolic links."""
    return {
        path.relative_to(root): (
            path.lstat().st_mode,
            os.fsencode(path.readlink())
            if path.is_symlink()
            else path.read_bytes()
            if stat.S_ISREG(path.lstat().st_mode)
            else b"",
        )
        for path in root.rglob("*")
    }


def _advance_remote(source: Path) -> None:
    """Publish a fixture change that existing checkouts and cached refs lack."""
    (source / "hosts/new").mkdir(parents=True)
    (source / "hosts/new/configuration.nix").touch()
    git(source, ["add", "."])
    git(source, ["commit", "--quiet", "-m", "add host"])


@pytest.mark.parametrize("initialized", [False, True])
def test_remote_inspection_preserves_home_and_does_not_block_submodule_add(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    initialized: bool,
) -> None:
    """Inspect fresh remote contents without creating persistent home state."""
    home, source, remote = _remote_fixture(tmp_path, monkeypatch)
    if initialized:
        initialize_home()
    before = _tree_snapshot(home)
    _expect(
        _inspect_target(remote) == subject.inspect_repository(source),
        "wrong remote inventory",
    )
    _advance_remote(source)
    _expect(
        _inspect_target(remote) == subject.inspect_repository(source),
        "remote inspection was stale",
    )
    _expect(_tree_snapshot(home) == before, "remote inspection changed HOME")
    if initialized:
        initialize_submodule(remote)
        checkout = home / "example.test/team/repository"
        _expect(
            git(checkout, ["rev-parse", "HEAD"]).stdout
            == git(source, ["rev-parse", "HEAD"]).stdout,
            "inspection interfered with native submodule add",
        )


@pytest.mark.parametrize("malformed", [False, True])
def test_remote_inspection_cleans_temporary_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    malformed: bool,
) -> None:
    """Remove temporary clones after successful inspection and parser errors."""
    home, source, remote = _remote_fixture(tmp_path, monkeypatch)
    if malformed:
        (source / "packages/example/main.py").write_text("def invalid(")
        git(source, ["add", "."])
        git(source, ["commit", "--quiet", "-m", "malformed source"])
        with (
            pytest.raises(CommandError, match="could not parse Python source"),
            subject.inspection_remote_repository(remote) as checkout,
        ):
            subject.inspect_repository(checkout)
    else:
        with subject.inspection_remote_repository(remote) as checkout:
            _expect(
                subject.inspect_repository(checkout)
                == subject.inspect_repository(source),
                "wrong temporary checkout contents",
            )
    _expect(not checkout.exists(), "temporary checkout survived inspection")
    _expect(not list(home.iterdir()), "temporary inspection changed HOME")


def test_local_path_and_remote_url_inspect_the_requested_contents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep local dirty content visible only when explicitly inspecting its path."""
    home, source, remote = _remote_fixture(tmp_path, monkeypatch)
    initialize_home()
    initialize_submodule(remote)
    checkout = home / "example.test/team/repository"
    (checkout / "hosts/local").mkdir(parents=True)
    (checkout / "hosts/local/configuration.nix").touch()
    git(checkout, ["remote", "set-url", "origin", "https://fork.test/team/repository"])
    before = _tree_snapshot(home)
    _expect(
        _inspect_target(str(checkout)) == subject.inspect_repository(checkout),
        "explicit local inspection ignored local content",
    )
    _expect(
        _inspect_target(remote) == subject.inspect_repository(source),
        "remote URL inspected a local fork or untracked files",
    )
    _expect(_tree_snapshot(home) == before, "inspection changed local Git state")


@pytest.mark.parametrize("state", ["checkout", "deinitialized", "cache", "placeholder"])
def test_remote_inspection_ignores_existing_home_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    """Leave existing worktrees, retained Git data, and unrelated paths untouched."""
    home, source, remote = _remote_fixture(tmp_path, monkeypatch)
    initialize_home()
    relative = Path("example.test/team/repository")
    checkout = home / relative
    if state == "cache":
        modules = Path(
            git(
                home,
                ["rev-parse", "--path-format=absolute", "--git-path", "modules"],
            ).stdout.strip(),
        )
        storage = modules / relative
        storage.parent.mkdir(parents=True)
        git(home, ["clone", "--bare", "--", str(source), str(storage)])
    elif state == "placeholder":
        checkout.mkdir(parents=True)
        (checkout / "personal-data").write_bytes(b"preserve data\\x00\\xff")
    else:
        initialize_submodule(remote)
        if state == "deinitialized":
            git(home, ["submodule", "deinit", "--force", "--", str(relative)])
        else:
            git(checkout, ["remote", "remove", "origin"])
            (checkout / "LICENSE").write_text("local changes")
    _advance_remote(source)
    before = _tree_snapshot(home)
    _expect(
        _inspect_target(remote) == subject.inspect_repository(source),
        "local state affected remote inspection",
    )
    _expect(_tree_snapshot(home) == before, "inspection changed existing Git storage")


def test_remote_inspection_accepts_native_file_urls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Let Git handle transports without imposing hosted repository conventions."""
    home, source, _remote = _remote_fixture(tmp_path, monkeypatch)
    _expect(
        _inspect_target(source.as_uri()) == subject.inspect_repository(source),
        "native file URL was rejected",
    )
    _expect(not list(home.iterdir()), "file URL inspection changed HOME")


@pytest.mark.parametrize("remote_kind", ["missing", "unsupported", "invalid"])
def test_remote_inspection_preserves_native_clone_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    remote_kind: str,
) -> None:
    """Compare failed remote inspection with Git's exit status and diagnostics."""
    home, _source, _remote = _remote_fixture(tmp_path, monkeypatch)
    remote = {
        "missing": (tmp_path / "missing.git").as_uri(),
        "unsupported": "unsupported://example.test/team/repository",
        "invalid": "https://",
    }[remote_kind]
    scratch = tmp_path / "temporary"
    scratch.mkdir()
    monkeypatch.setenv("TMPDIR", str(scratch))
    native_checkout = tmp_path / "native-checkout"
    expected = subprocess.run(  # noqa: S603
        ["git", "clone", "--", remote, str(native_checkout)],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    actual = subprocess.run(  # noqa: S603
        [os.environ["PACKAGE_E2E_EXECUTABLE"], remote],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    actual_error = re.sub(
        re.escape(str(scratch)) + r"/afairesi-repo-inspect-[^/]+/repository",
        "<CHECKOUT>",
        actual.stderr,
    )
    _expect(
        (actual.returncode, actual.stdout, actual_error)
        == (
            expected.returncode,
            expected.stdout,
            expected.stderr.replace(str(native_checkout), "<CHECKOUT>"),
        ),
        f"native clone error changed: {expected.stderr!r}, {actual.stderr!r}",
    )
    _expect(not list(scratch.iterdir()), "failed clone left temporary files")
    _expect(not list(home.iterdir()), "failed clone changed HOME")

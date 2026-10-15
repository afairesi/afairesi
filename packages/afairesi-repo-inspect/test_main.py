# Copyright (c) 2026- Paschalis Bizopoulos
"""Verify read-only inventories and static argparse discovery."""

from __future__ import annotations

import ast
import json
import os
import subprocess
from importlib import import_module
from pathlib import Path
from typing import Any

import pytest
from afairesi import (
    CommandError,
    git,
    home_module_directory,
    initialize_home,
    initialize_submodule,
    inspection_remote_repository,
)

subject = import_module("packages.afairesi-repo-inspect.main")


def _expect(condition: bool, message: str) -> None:  # noqa: FBT001
    if not condition:
        raise AssertionError(message)


def _discovery(source: str) -> dict[str, Any]:
    return subject.ArgumentDiscovery(ast.parse(source)).report()  # type: ignore[no-any-return]


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


def _inspect_remote(remote: str) -> dict[str, Any]:
    """Exercise the installed CLI with an unchanged remote URL argument."""
    completed = subprocess.run(  # noqa: S603
        [os.environ["PACKAGE_E2E_EXECUTABLE"], remote],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return json.loads(completed.stdout)  # type: ignore[no-any-return]


@pytest.mark.parametrize("initialized", [False, True])
def test_remote_inspection_caches_without_registration_and_refreshes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    initialized: bool,
) -> None:
    """Inspect remote changes without staging home files, then adopt cached Git data."""
    home, source, remote = _remote_fixture(tmp_path, monkeypatch)
    relative = Path("example.test/team/repository")
    if initialized:
        initialize_home()
        before = git(home, ["ls-files", "--stage"]).stdout
    _expect(
        _inspect_remote(remote) == subject.inspect_repository(source),
        "wrong remote inventory",
    )
    _expect(not (home / relative).exists(), "inspection created a home checkout")
    _expect(not (home / ".gitmodules").exists(), "inspection registered a submodule")
    if not initialized:
        _expect(not list(home.iterdir()), "standalone inspection changed HOME")
        return
    _expect(
        git(home, ["ls-files", "--stage"]).stdout == before,
        "inspection staged files",
    )
    directory = home_module_directory(home, relative)
    _expect(directory.is_dir(), "inspection did not cache Git data")
    (source / "hosts/new").mkdir(parents=True)
    (source / "hosts/new/configuration.nix").touch()
    git(source, ["add", "."])
    git(source, ["commit", "--quiet", "-m", "add host"])
    _expect(
        _inspect_remote(remote) == subject.inspect_repository(source),
        "cache was stale",
    )
    expected_revision = git(source, ["rev-parse", "HEAD"]).stdout
    source.rename(tmp_path / "offline-source")
    initialize_submodule(remote)
    checkout = home / relative
    _expect(
        git(checkout, ["rev-parse", "HEAD"]).stdout == expected_revision,
        "clone lost cached revision",
    )
    _expect(
        git(checkout, ["rev-parse", "--absolute-git-dir"]).stdout.strip()
        == str(directory),
        "clone did not adopt cache",
    )
    _expect(
        git(checkout, ["rev-parse", "@{upstream}"]).stdout == expected_revision,
        "adopted checkout has no upstream tracking branch",
    )
    (checkout / "hosts/local").mkdir(parents=True)
    (checkout / "hosts/local/configuration.nix").touch()
    before_status = git(checkout, ["status", "--porcelain"]).stdout
    _expect(
        _inspect_remote(remote) == subject.inspect_repository(checkout),
        "local changes were ignored",
    )
    _expect(
        _inspect_remote("git@example.test:team/repository")
        == subject.inspect_repository(checkout),
        "SSH spelling did not reuse the local HTTPS checkout",
    )
    _expect(
        git(checkout, ["status", "--porcelain"]).stdout == before_status,
        "inspection changed local files",
    )


def test_remote_inspection_preserves_deinitialized_submodule_branches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refresh inspection data without advancing retained user branch refs."""
    home, source, remote = _remote_fixture(tmp_path, monkeypatch)
    initialize_home()
    initialize_submodule(remote)
    relative = Path("example.test/team/repository")
    checkout = home / relative
    before = git(checkout, ["for-each-ref", "refs/heads"]).stdout
    git(home, ["submodule", "deinit", "--force", "--", str(relative)])
    checkout.rmdir()
    (source / "hosts/new").mkdir(parents=True)
    (source / "hosts/new/configuration.nix").touch()
    git(source, ["add", "."])
    git(source, ["commit", "--quiet", "-m", "add host"])
    _expect(
        _inspect_remote(remote) == subject.inspect_repository(source),
        "retained submodule inspection was stale",
    )
    directory = home_module_directory(home, relative)
    completed = subprocess.run(  # noqa: S603
        ["git", "--git-dir", str(directory), "for-each-ref", "refs/heads"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    _expect(completed.stdout == before, "inspection advanced retained local branches")
    _expect(not checkout.exists(), "inspection recreated a home checkout")


def test_remote_inspection_rejects_checkout_and_cache_collisions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not inspect or adopt a repository with a different canonical origin."""
    home, source, remote = _remote_fixture(tmp_path, monkeypatch)
    initialize_home()
    with inspection_remote_repository(remote):
        pass
    relative = Path("example.test/team/repository")
    directory = home_module_directory(home, relative)
    git(
        directory,
        ["config", "remote.origin.url", "https://other.test/team/repository"],
    )
    with (
        pytest.raises(CommandError, match="origin does not match"),
        inspection_remote_repository(remote),
    ):
        pass
    with pytest.raises(CommandError, match="origin does not match"):
        initialize_submodule(remote)
    _expect(not (home / ".gitmodules").exists(), "collision registered a submodule")
    checkout = home / relative
    checkout.parent.mkdir(parents=True)
    source.rename(checkout)
    git(checkout, ["remote", "add", "origin", "https://other.test/team/repository"])
    with (
        pytest.raises(CommandError, match="origin does not match"),
        inspection_remote_repository(remote),
    ):
        pass

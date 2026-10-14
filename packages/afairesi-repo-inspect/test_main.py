# Copyright (c) 2026- Paschalis Bizopoulos
"""Verify read-only inventories and static argparse discovery."""

from __future__ import annotations

import ast
import json
import os
import subprocess
from importlib import import_module
from typing import TYPE_CHECKING, Any

import pytest
from afairesi import CommandError

if TYPE_CHECKING:
    from pathlib import Path
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
    _expect(report["commands"] == [["build"]], "lost subcommand")
    rows = {tuple(row["names"]): row for row in report["arguments"]}
    _expect(rows[("input",)]["command"] == [], "wrong positional scope")
    _expect(
        rows[("-j", "--jobs")]["options"]
        == {"type": "int", "default": 2, "choices": [1, 2]},
        "lost metadata",
    )
    _expect(rows[("--quiet",)]["command"] == ["build"], "lost helper context")
    _expect(not report["warnings"], "resolved declarations produced warnings")


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
        == [{"command": [], "names": ["--required"], "options": {"required": True}}],
        "factory or keyword binding was lost",
    )


def test_declared_test_names() -> None:
    """Humanize declared tests while retaining class context and identifiers."""
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
            {"id": "Arguments::test_valid_input", "name": "Arguments: valid input"},
            {"id": "Derived::test_other_input", "name": "Derived: other input"},
            {
                "id": "TestArguments::test_missing_input",
                "name": "TestArguments: missing input",
            },
            {"id": "test_default_output", "name": "default output"},
        ],
        "wrong test names or collection scope",
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
        report["hosts"]
        == [{"name": "laptop", "configuration": "hosts/laptop/configuration.nix"}],
        "wrong hosts",
    )
    _expect(
        report["packages"][1]["cli"]["status"] == "not applicable",
        "non-Python package has CLI metadata",
    )
    text = subject.render(report)
    _expect(
        "default output" in text and "test_default_output" not in text,
        "test name was not humanized",
    )


def test_cli_json_and_readable_output(tmp_path: Path) -> None:
    """Exercise the installed executable in both output modes."""
    _repository(tmp_path)
    for arguments in ([], ["--json"]):
        completed = subprocess.run(  # noqa: S603
            [os.environ["PACKAGE_E2E_EXECUTABLE"], str(tmp_path), *arguments],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        if arguments:
            report = json.loads(completed.stdout)
            _expect(
                report["packages"][0]["tests"][0]["id"] == "test_default_output",
                "JSON lost test identifier",
            )
        else:
            _expect(
                "laptop" in completed.stdout and "--output" in completed.stdout,
                "incomplete CLI inventory",
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
    """Render missing collections explicitly."""
    (tmp_path / "flake.nix").touch()
    _expect(
        subject.render(subject.inspect_repository(tmp_path))
        == "hosts/\n  (none)\npackages/\n  (none)",
        "empty collections were omitted",
    )

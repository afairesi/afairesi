#!/usr/bin/env python3
# Copyright (c) 2026- Paschalis Bizopoulos
"""Inspect repository hosts, packages, argparse arguments, and test names."""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path
from typing import Any

from afairesi import (
    CommandError,
    detect_packages,
    package_description,
    python_test_definitions,
    repository_root,
    resolve_repository_path,
)


def _source(path: Path) -> ast.Module:
    """Parse source without importing it or executing its declarations."""
    try:
        return ast.parse(path.read_bytes(), filename=str(path))
    except (OSError, SyntaxError, UnicodeError) as error:
        message = f"{path}: could not parse Python source: {error}"
        raise CommandError(message) from error


def _imports(module: ast.Module, name: str) -> dict[str, str]:
    """Map imported aliases back to their standard library names."""
    result = {}
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            result.update(
                {
                    alias.asname or alias.name: name
                    for alias in node.names
                    if alias.name == name
                },
            )
        elif isinstance(node, ast.ImportFrom) and node.module == name:
            result.update(
                {
                    alias.asname or alias.name: f"{name}.{alias.name}"
                    for alias in node.names
                },
            )
    return result


def _value(node: ast.expr) -> object:
    """Describe literals exactly and unresolved expressions explicitly."""
    try:
        value = ast.literal_eval(node)
        json.dumps(value)
    except (ValueError, TypeError):
        if isinstance(node, ast.Name) and node.id in {"str", "int", "float", "bool"}:
            return node.id
        return {"unknown": ast.unparse(node)}
    else:
        return value


class ArgumentDiscovery:
    """Follow static parser bindings, factory returns, and configuration helpers."""

    def __init__(self, module: ast.Module) -> None:
        """Index parser assignments and helper call arguments."""
        self.imports = _imports(module, "argparse")
        self.bindings: dict[tuple[str, str], list[tuple[str, ast.expr]]] = {}
        self.returns: dict[str, list[ast.expr]] = {}
        self.functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
        self.calls: list[tuple[str, ast.Call]] = []
        self._collect(module, "")
        for scope, call in self.calls:
            if isinstance(call.func, ast.Name) and call.func.id in self.functions:
                function = self.functions[call.func.id]
                parameters = function.args.posonlyargs + function.args.args
                for parameter, argument in zip(parameters, call.args, strict=False):
                    self._bind(function.name, parameter.arg, scope, argument)
                for keyword in call.keywords:
                    if keyword.arg is not None:
                        self._bind(function.name, keyword.arg, scope, keyword.value)

    def _bind(self, scope: str, name: str, source_scope: str, value: ast.expr) -> None:
        self.bindings.setdefault((scope, name), []).append((source_scope, value))

    def _collect(self, node: ast.AST, scope: str) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scope = node.name
            self.functions[scope] = node
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self._bind(scope, target.id, scope, node.value)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.value
        ):
            self._bind(scope, node.target.id, scope, node.value)
        elif isinstance(node, ast.Return) and node.value:
            self.returns.setdefault(scope, []).append(node.value)
        elif isinstance(node, ast.Call):
            self.calls.append((scope, node))
        for child in ast.iter_child_nodes(node):
            self._collect(child, scope)

    def _qualified(self, node: ast.expr) -> str:
        if isinstance(node, ast.Name):
            return self.imports.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            return f"{self._qualified(node.value)}.{node.attr}"
        return ""

    def _resolve(  # noqa: PLR0911
        self,
        scope: str,
        node: ast.expr,
        seen: frozenset[tuple[str, int]] = frozenset(),
    ) -> set[tuple[str, ...]]:
        key = (scope, id(node))
        if key in seen:
            return set()
        seen = seen | {key}
        if isinstance(node, ast.Name):
            bindings = self.bindings.get(
                (scope, node.id),
                self.bindings.get(("", node.id), []),
            )
            return set().union(
                *(self._resolve(origin, value, seen) for origin, value in bindings),
            )
        if not isinstance(node, ast.Call):
            return set()
        if self._qualified(node.func) == "argparse.ArgumentParser":
            return {()}
        if isinstance(node.func, ast.Name):
            return set().union(
                *(
                    self._resolve(node.func.id, value, seen)
                    for value in self.returns.get(node.func.id, [])
                ),
            )
        if isinstance(node.func, ast.Attribute):
            parents = self._resolve(scope, node.func.value, seen)
            if node.func.attr in {
                "add_subparsers",
                "add_argument_group",
                "add_mutually_exclusive_group",
            }:
                return parents
            if node.func.attr == "add_parser":
                name = (
                    _value(node.args[0]) if node.args else {"unknown": "command name"}
                )
                label = name if isinstance(name, str) else "unknown command"
                return {(*parent, label) for parent in parents}
        return set()

    def report(self) -> dict[str, Any]:
        """Return declared arguments and command paths with unresolved warnings."""
        arguments: list[dict[str, Any]] = []
        commands: set[tuple[str, ...]] = set()
        warnings = []
        for scope, call in self.calls:
            if not isinstance(call.func, ast.Attribute):
                continue
            if call.func.attr == "add_parser":
                commands.update(self._resolve(scope, call))
            if call.func.attr != "add_argument":
                continue
            contexts = self._resolve(scope, call.func.value)
            if not contexts:
                if self.imports:
                    warnings.append(
                        f"line {call.lineno}: unknown parser for {ast.unparse(call)}",
                    )
                continue
            names = [_value(argument) for argument in call.args]
            options = {
                keyword.arg or "**kwargs": _value(keyword.value)
                for keyword in call.keywords
            }
            arguments.extend(
                {"command": list(context), "names": names, "options": options}
                for context in sorted(contexts)
            )
        status = "argparse" if self.imports else "unknown"
        return {
            "status": status,
            "arguments": arguments,
            "commands": [list(command) for command in sorted(commands)],
            "warnings": warnings,
        }


def declared_tests(module: ast.Module) -> list[dict[str, str]]:
    """List declared test functions and methods, retaining their identifiers."""
    result = []
    for owner, definition in python_test_definitions(module):
        label = definition.name.removeprefix("test_").replace("_", " ")
        result.append(
            {
                "id": f"{owner}::{definition.name}" if owner else definition.name,
                "name": f"{owner}: {label}" if owner else label,
            },
        )
    return sorted(result, key=lambda item: item["id"])


def inspect_repository(root: Path) -> dict[str, Any]:
    """Build a deterministic inventory of an Afairesi flake repository."""
    if not (root / "flake.nix").is_file():
        message = f"{root}: expected an Afairesi repository with flake.nix"
        raise CommandError(message)
    hosts = root / "hosts"
    host_rows = (
        [
            {"name": host.name, "configuration": f"hosts/{host.name}/configuration.nix"}
            for host in sorted(hosts.iterdir())
            if host.is_dir() and not host.is_symlink()
        ]
        if hosts.is_dir()
        else []
    )
    packages = []
    for package in detect_packages(root):
        relative = f"packages/{package.name}"
        cli = (
            ArgumentDiscovery(_source(package.root / "main.py")).report()
            if package.kind == "python"
            else {
                "status": "not applicable",
                "arguments": [],
                "commands": [],
                "warnings": [],
            }
        )
        tests = package.root / "test_main.py"
        packages.append(
            {
                "name": package.name,
                "kind": package.kind,
                "path": relative,
                "description": package_description(package),
                "cli": cli,
                "tests": declared_tests(_source(tests)) if tests.is_file() else [],
                "check": f"checks/{package.name}/default.nix"
                if (root / "checks" / package.name / "default.nix").is_file()
                else None,
            },
        )
    return {"hosts": host_rows, "packages": packages}


def parser() -> argparse.ArgumentParser:
    """Declare the inspection command-line interface."""
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "repository",
        nargs="?",
        type=Path,
        metavar="PATH",
        help=(
            "repository path relative to the Git root, or cwd outside a repository "
            "(default: current Git repository); absolute paths are accepted"
        ),
    )
    return result


def main() -> None:
    """Inspect a repository without running package code or tests."""
    cli = parser()
    options = cli.parse_args()
    try:
        root = (
            resolve_repository_path(options.repository)
            if options.repository is not None
            else repository_root()
        )
        report = inspect_repository(root)
    except CommandError as error:
        cli.exit(1, f"{cli.prog}: {error}\n")
    sys.stdout.write(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()

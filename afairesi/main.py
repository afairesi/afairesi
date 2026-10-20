#!/usr/bin/env python3
# Copyright (c) 2026- Paschalis Bizopoulos
"""Afairesi minimizes the persistent state needed to reconstruct a software system."""

from __future__ import annotations

import argparse
import ast
import contextlib
import inspect
import json
import keyword
import math
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from xml.etree import ElementTree as ET

import nix_syntax

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator

    import libcst as cst
    from tree_sitter import Node
PACKAGE_KINDS = ("html", "latex", "nix", "python")
NIX_FMT_HELP = (
    "nix fmt also cleans up structure, enforces all lint rules, and applies unsafe "
    "fixes that can change behavior. Review the diff and run tests."
)
KIND_MARKERS = {
    "html": "index.html",
    "latex": "ms.tex",
    "python": "main.py",
}
ROOT_FILES = {
    ".forgejo/workflows/workflow.yml",
    ".github/workflows/workflow.yml",
    ".gitignore",
    "LICENSE",
    "flake.lock",
    "flake.nix",
}
PRM_NAME = "prm"
TMP_NAME = "tmp"
MINIMUM_REMOTE_COMPONENTS = 3
RESOURCE_PATH_COMPONENTS = 1
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


class CommandError(RuntimeError):
    """A user-facing command failure."""


@dataclass(frozen=True)
class Package:
    """A repository package with its kind and source directory."""

    name: str
    kind: str
    root: Path


def run(
    arguments: list[str],
    cwd: Path | None = None,
    *,
    check: bool = True,
    input_source: str | None = None,
    capture_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a process with captured output or inherited terminal streams."""
    completed = subprocess.run(  # noqa: S603
        arguments,
        cwd=cwd,
        capture_output=capture_output,
        check=False,
        input=input_source,
        text=True,
    )
    if check and completed.returncode != 0:
        if completed.stdout:
            sys.stdout.write(completed.stdout)
        if completed.stderr:
            sys.stderr.write(completed.stderr)
        raise SystemExit(completed.returncode)
    return completed


def git(
    root: Path,
    arguments: list[str],
    *,
    check: bool = True,
    capture_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run Git in a selected repository."""
    return run(
        ["git", "-C", str(root), *arguments],
        check=check,
        capture_output=capture_output,
    )


def repository_root(path: Path = Path()) -> Path:
    """Discover the current Git worktree root."""
    completed = git(
        path,
        ["rev-parse", "--path-format=absolute", "--show-toplevel"],
        check=False,
    )
    if completed.returncode != 0:
        msg = "not inside a Git repository"
        raise CommandError(msg)
    return Path(completed.stdout.strip())


def resolve_repository_path(path: Path) -> Path:
    """Resolve explicit paths from the Git root, or cwd outside a repository."""
    if path.is_absolute():
        return path.resolve()
    try:
        root = repository_root()
    except CommandError:
        root = Path.cwd()
    return (root / path).resolve()


def repository_type(root: Path, default: str | None = None) -> str:
    """Detect home/submodule and flake repository layouts."""
    flake, home = _repository_type_markers(root)
    if home and not flake:
        return "home"
    if flake and not home:
        return "flake"
    if not home and not flake and default is not None:
        return default
    if home and flake:
        msg = "repository contains markers for both home and flake layouts"
        raise CommandError(msg)
    msg = (
        "cannot determine the repository type; run "
        "'afairesi home init' or "
        "'afairesi repo init'"
    )
    raise CommandError(msg)


def _repository_type_markers(root: Path) -> tuple[bool, bool]:
    """Recognize home and flake layouts for lifecycle commands."""
    flake = any((root / marker).exists() for marker in ("flake.nix", "flake.lock"))
    gitignore = read_regular(root / ".gitignore") or ""
    home = (root / ".gitmodules").exists() or "!/.gitmodules" in gitignore.splitlines()
    return flake, home


def read_regular(path: Path) -> str | None:
    """Read an optional regular file, rejecting other filesystem entries."""
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(mode):
        msg = f"{path}: must be a regular file"
        raise CommandError(msg)
    return path.read_text(encoding="utf-8")


def report_change(message: str, *, dry_run: bool) -> None:
    """Report one deterministic convergence action."""
    sys.stdout.write((("would " if dry_run else "") + message) + "\n")


def write_managed(
    root: Path,
    relative: Path,
    source: str,
    *,
    dry_run: bool,
    executable: bool = False,
) -> bool:
    """Write and stage one managed file when its contents or mode differ."""
    path = root / relative
    current = read_regular(path)
    current_mode = path.lstat().st_mode if path.exists() else 0
    mode_matches = bool(current_mode & 0o111) == executable
    if current == source and mode_matches:
        return False
    report_change(f"write '{relative}'", dry_run=dry_run)
    if dry_run:
        return True
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755 if executable else 0o644)
    git(root, ["add", "--", str(relative)])
    return True


def _tracked_paths(root: Path) -> set[Path]:
    """Return paths represented in the index."""
    completed = git(root, ["ls-files", "-z"])
    return {Path(item) for item in completed.stdout.split("\0") if item}


def _flake_clean_arguments(*, dry_run: bool) -> list[str]:
    """Build the flake cleanup command."""
    arguments = ["clean", "-ndx" if dry_run else "-fdx"]
    for exclusion in (f"/{TMP_NAME}/", f"/*/{TMP_NAME}/"):
        arguments.extend(("-e", exclusion))
    return arguments


def hosted_remote(remote: str) -> tuple[str, str]:
    """Read hosted remote components with Git's native URL parser."""
    components = [
        run(["git", "url-parse", "--component", component, "--", remote], check=False)
        for component in ("scheme", "host", "path")
    ]
    scheme, host, path = [result.stdout.strip() for result in components]
    if (
        all(result.returncode == 0 for result in components)
        and scheme in {"http", "https", "ssh", "git+ssh", "git"}
        and host
        and path.strip("/")
    ):
        return host.lower(), path.strip("/")
    msg = f"remote URL has no canonical host and repository path: {remote}"
    raise CommandError(
        msg,
    )


def canonical_remote_path(remote: str) -> Path:
    """Map a hosted remote to its canonical home-relative path."""
    host, remote_path = hosted_remote(remote)
    remote_path = remote_path.removesuffix(".git")
    components = [host, *remote_path.split("/")]
    if len(components) < MINIMUM_REMOTE_COMPONENTS or any(
        not re.fullmatch(r"[A-Za-z0-9._-]+", component) or component in {".", ".."}
        for component in components
    ):
        msg = (
            "repository path components must contain only ASCII letters, "
            "digits, '.', '-', or '_'"
        )
        raise CommandError(
            msg,
        )
    return Path(*components)


def _repository_remote_path(root: Path) -> Path | None:
    """Return a hosted origin's canonical path when one is available."""
    origin = git(root, ["remote", "get-url", "origin"], check=False)
    if origin.returncode != 0:
        return None
    try:
        return canonical_remote_path(origin.stdout.strip())
    except CommandError:
        return None


def _package_kind(name: str, markers: set[str]) -> str:
    """Classify regular package markers consistently across source snapshots."""
    matches = [kind for kind, marker in KIND_MARKERS.items() if marker in markers]
    if len(matches) > 1:
        msg = f"{name}: has ambiguous project markers: {', '.join(matches)}"
        raise CommandError(msg)
    return matches[0] if matches else "nix"


def detect_packages(root: Path) -> list[Package]:
    """Detect supported packages from unambiguous marker files."""
    packages_root = root
    if not packages_root.is_dir():
        return []
    result: list[Package] = []
    for package_root in sorted(
        path
        for path in packages_root.iterdir()
        if path.is_dir()
        and not path.is_symlink()
        and path.name not in {PRM_NAME, TMP_NAME}
        and (
            (path / "default.nix").is_file()
            or any((path / marker).is_file() for marker in KIND_MARKERS.values())
        )
    ):
        markers = {
            marker
            for marker in KIND_MARKERS.values()
            if (package_root / marker).is_file()
        }
        result.append(
            Package(
                package_root.name,
                _package_kind(package_root.name, markers),
                package_root,
            ),
        )
    validate_python_package_names(result)
    return result


def validate_name(name: str) -> None:
    """Enforce package naming conventions."""
    if not re.fullmatch(r"[a-z0-9]+(?:_[a-z0-9]+)*|[a-z0-9]+(?:-[a-z0-9]+)*", name):
        msg = f"package name must use snake_case or dash-case: {name}"
        raise CommandError(msg)


def validate_python_package_names(packages: list[Package]) -> None:
    """Reject invalid or conflicting installed Python module names."""
    modules: dict[str, str] = {}
    for package in packages:
        if package.kind != "python":
            continue
        module = package.name.replace("-", "_")
        if not module.isidentifier() or keyword.iskeyword(module):
            msg = (
                f"{package.name}: installed Python module must be "
                "a valid non-keyword identifier"
            )
            raise CommandError(msg)
        if module in modules:
            msg = (
                f"{package.name} and {modules[module]}: "
                f"installed Python module names collide: {module}"
            )
            raise CommandError(msg)
        modules[module] = package.name


def validate_host_name(name: str) -> None:
    """Enforce lower camelCase host names."""
    if not re.fullmatch(r"[a-z][A-Za-z0-9]*", name):
        msg = f"host name must use camelCase: {name}"
        raise CommandError(msg)


def package_files(package: Package) -> set[Path]:
    """Return permitted regular files for a package kind."""
    relative = Path() / package.name
    kind_files = {
        "python": {"main.py", "test_main.py"},
        "html": {"index.html", "script.js", "style.css"},
        "latex": {"ms.tex", "ms.bib"},
        "nix": set(),
    }[package.kind]
    return {relative / "default.nix", *(relative / item for item in kind_files)}


def required_package_files(package: Package) -> set[Path]:
    """Return regular files required for a package kind."""
    optional = {
        "html": {"script.js", "style.css"},
        "latex": set(),
        "nix": set(),
        "python": {"test_main.py"},
    }[package.kind]
    return {path for path in package_files(package) if path.name not in optional}


def allowed_paths(root: Path, packages: list[Package]) -> set[Path]:
    """Compute the repository whitelist represented by .gitignore."""
    allowed = {Path(item) for item in ROOT_FILES if (root / item).exists()}
    hosts = root
    if hosts.is_dir():
        for host in hosts.iterdir():
            if host.is_dir() and (host / "configuration.nix").exists():
                validate_host_name(host.name)
                allowed.add(Path() / host.name / "configuration.nix")
                hardware = Path() / host.name / "hardware-configuration.nix"
                if (root / hardware).exists():
                    allowed.add(hardware)
    for package in packages:
        allowed.update(
            path for path in package_files(package) if (root / path).exists()
        )
    return allowed


def prm_directories(root: Path) -> set[Path]:
    """Return existing prm directories whose contents are unrestricted."""
    candidates = {Path(PRM_NAME)}
    candidates.update(
        Path(child.name) / PRM_NAME
        for child in root.iterdir()
        if child.is_dir() and child.name not in {".git", PRM_NAME, TMP_NAME}
    )
    return {path for path in candidates if (root / path).is_dir()}


def tmp_directories(root: Path) -> set[Path]:
    """Return permitted untracked tmp trees."""
    candidates = {Path(TMP_NAME)}
    candidates.update(
        Path(child.name) / TMP_NAME
        for child in root.iterdir()
        if child.is_dir() and child.name not in {".git", PRM_NAME, TMP_NAME}
    )
    return {
        path
        for path in candidates
        if (root / path).is_dir() and not (root / path).is_symlink()
    }


def beneath(path: Path, trees: set[Path]) -> bool:
    """Return whether path is a tree or lies beneath one."""
    return any(path == tree or tree in path.parents for tree in trees)


def render_gitignore(paths: set[Path], trees: set[Path] | None = None) -> str:
    """Render a minimal whitelist Git ignore file."""
    trees = trees or set()
    directories: set[Path] = set()
    for path in paths | trees:
        directories.update(path.parents)
    directories.discard(Path())
    patterns = {f"!/{directory.as_posix()}/" for directory in directories}
    patterns.update(f"!/{path.as_posix()}" for path in paths)
    for tree in trees:
        patterns.update((f"!/{tree.as_posix()}/", f"!/{tree.as_posix()}/**"))
    return "\n".join(["*", *sorted(patterns)]) + "\n"


def _refresh_gitignore(root: Path) -> None:
    """Refresh the whitelist after changing repository resources."""
    packages = detect_packages(root)
    nix_syntax.write_if_changed(
        root / ".gitignore",
        render_gitignore(allowed_paths(root, packages), prm_directories(root)),
    )


def _structure_paths(root: Path, excluded: set[Path]) -> list[Path]:
    """List managed paths without traversing ignored repository trees."""
    paths: list[Path] = []
    for directory, subdirectories, filenames in root.walk():
        relative = directory.relative_to(root)
        subdirectories[:] = [
            name for name in subdirectories if relative / name not in excluded
        ]
        paths.extend(
            directory / name
            for name in subdirectories + filenames
            if relative / name not in excluded
        )
    return sorted(paths)


def validate_flake_layout(root: Path, *, removing: Path | None = None) -> None:
    """Establish the managed layout and readable test metadata before a command."""
    excluded = prm_directories(root) | tmp_directories(root) | {Path(".git")}
    for path in _structure_paths(root, excluded):
        relative = path.relative_to(root)
        if path.is_symlink():
            msg = f"{relative}: managed layout requires regular files and directories"
            raise CommandError(msg)
    for package in detect_packages(root):
        tests = package.root / "test_main.py"
        if (
            package.kind == "python"
            and package.root.relative_to(root) != removing
            and tests.is_file()
        ):
            has_python_tests(tests)


def inspect_structure(root: Path) -> tuple[list[Package], list[str]]:
    """Validate the declared repository subset."""
    packages = detect_packages(root)
    allowed = allowed_paths(root, packages)
    issues: list[str] = []
    for package in packages:
        validate_name(package.name)
        issues.extend(_python_source_issues(package))
        for relative in sorted(required_package_files(package)):
            if not (root / relative).is_file():
                issues.extend([f"{relative}: missing required regular file"])
    prm = prm_directories(root)
    tmp = tmp_directories(root)
    for path in _structure_paths(root, prm | tmp | {Path(".git")}):
        relative = path.relative_to(root)
        if path.is_symlink():
            issues.append(
                f"{relative}: expected regular file or directory, found symbolic link",
            )
        elif path.is_file() and relative not in allowed:
            issues.append(
                f"{relative}: unsupported by the canonical flake layout; "
                "move unrestricted project files under prm/ "
                f"(for example, prm/{relative.name})",
            )
    return packages, issues


def has_python_tests(path: Path) -> bool:
    """Detect static Python test definitions without executing the source."""
    try:
        module = ast.parse(path.read_bytes(), filename=str(path))
        return bool(python_test_definitions(module))
    except (OSError, SyntaxError, UnicodeError) as error:
        msg = f"{path}: Python source could not be parsed: {error}"
        raise CommandError(
            msg,
        ) from error


def _python_class_mro(
    classes: dict[str, ast.ClassDef],
    name: str,
    seen: set[str],
) -> list[str]:
    """Compute Python's class precedence without executing base declarations."""
    if name in seen:
        msg = f"cyclic class inheritance: {name}"
        raise CommandError(msg)
    if name not in classes:
        return [name]
    bases = [ast.unparse(base) for base in classes[name].bases]
    sequences = [_python_class_mro(classes, base, seen | {name}) for base in bases] + [
        bases.copy(),
    ]
    result = [name]
    while sequences := [sequence for sequence in sequences if sequence]:
        candidate = next(
            (
                sequence[0]
                for sequence in sequences
                if all(sequence[0] not in other[1:] for other in sequences)
            ),
            None,
        )
        if candidate is None:
            msg = f"inconsistent class inheritance: {name}"
            raise CommandError(msg)
        result.append(candidate)
        for sequence in sequences:
            if sequence[0] == candidate:
                sequence.pop(0)
    return result


def python_test_definitions(
    module: ast.Module,
) -> list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    """Find static test definitions, including methods inherited from local bases."""
    classes = {
        node.name: node for node in module.body if isinstance(node, ast.ClassDef)
    }
    case_classes = _unittest_classes(module)

    def methods(name: str) -> dict[str, ast.stmt]:
        result: dict[str, ast.stmt] = {}
        for ancestor in reversed(_python_class_mro(classes, name, set())):
            if ancestor not in classes:
                continue
            for statement in classes[ancestor].body:
                if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    result[statement.name] = statement
                elif isinstance(statement, (ast.Assign, ast.AnnAssign)):
                    targets = (
                        statement.targets
                        if isinstance(statement, ast.Assign)
                        else [statement.target]
                    )
                    for target in targets:
                        if isinstance(target, ast.Name):
                            result[target.id] = statement
        return result

    result: list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]] = []
    for node in module.body:
        owner = node.name if isinstance(node, ast.ClassDef) else ""
        definitions = (
            methods(owner).values()
            if isinstance(node, ast.ClassDef)
            and (owner.startswith("Test") or owner in case_classes)
            else [node]
        )
        result.extend(
            (owner, definition)
            for definition in definitions
            if isinstance(definition, (ast.FunctionDef, ast.AsyncFunctionDef))
            and definition.name.startswith("test_")
        )
    return result


def _runtime_path(expression: ast.expr) -> Path | None:
    """Recognize home-relative literal paths without executing source code."""
    parts: list[str] = []
    while isinstance(expression, ast.BinOp) and isinstance(expression.op, ast.Div):
        if not isinstance(expression.right, ast.Constant) or not isinstance(
            expression.right.value,
            str,
        ):
            return None
        parts.insert(0, expression.right.value)
        expression = expression.left
    if ast.unparse(expression) != "Path.home()" or not parts:
        return None
    if any(Path(part).is_absolute() or ".." in Path(part).parts for part in parts):
        return None
    return Path(*parts)


def _implicit_runtime_paths(
    module: ast.Module,
    bindings: list[ast.Assign | ast.AnnAssign],
) -> list[ast.expr]:
    """Find cwd, home, and installed-source tmp paths, including aliases."""
    paths: list[ast.expr] = []
    declared_nodes = {id(node) for binding in bindings for node in ast.walk(binding)}
    assignments = {
        target.id: statement.value
        for statement in module.body
        if isinstance(statement, (ast.Assign, ast.AnnAssign))
        for target in (
            statement.targets
            if isinstance(statement, ast.Assign)
            else [statement.target]
        )
        if isinstance(target, ast.Name) and statement.value is not None
    }

    def implicit_root(expression: ast.AST, seen: frozenset[str] = frozenset()) -> bool:
        for node in ast.walk(expression):
            if isinstance(node, ast.Name) and node.id not in seen:
                if node.id in {"RUNTIME_DIR", "INPUT_DIRS"}:
                    continue
                if node.id in assignments and implicit_root(
                    assignments[node.id],
                    seen | {node.id},
                ):
                    return True
            if isinstance(node, ast.Call) and (
                (
                    isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "Path"
                    and node.func.attr in {"home", "cwd"}
                )
                or (
                    isinstance(node.func, ast.Name)
                    and node.func.id == "Path"
                    and any(
                        isinstance(argument, ast.Constant)
                        or (
                            isinstance(argument, ast.Name) and argument.id == "__file__"
                        )
                        for argument in node.args
                    )
                )
            ):
                return True
        return False

    for node in ast.walk(module):
        if not isinstance(node, (ast.BinOp, ast.Call)) or id(node) in declared_nodes:
            continue
        literal = (
            node.right
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)
            else node.args[0]
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "Path"
            and node.args
            else None
        )
        if (
            isinstance(literal, ast.Constant)
            and isinstance(literal.value, str)
            and "tmp" in Path(literal.value).parts
            and implicit_root(node)
        ):
            paths.append(node)
    return paths


def _runtime_directory_issues(
    package: Package,
    module: ast.Module,
) -> list[str]:
    """Validate declared output ownership and explicit input directories."""
    issues: list[str] = []
    bindings = [
        statement
        for statement in module.body
        if isinstance(statement, (ast.Assign, ast.AnnAssign))
        and any(
            isinstance(target, ast.Name) and target.id in {"RUNTIME_DIR", "INPUT_DIRS"}
            for target in (
                statement.targets
                if isinstance(statement, ast.Assign)
                else [statement.target]
            )
        )
    ]
    issues.extend(
        f"{package.name}/main.py:{node.lineno}: runtime paths "
        "must derive from declared RUNTIME_DIR or INPUT_DIRS"
        for node in _implicit_runtime_paths(module, bindings)
    )
    repository = package.root.parent
    expected = None
    if bindings and (repository / ".git").exists():
        expected = _repository_remote_path(repository)
    for statement in bindings:
        targets = (
            statement.targets
            if isinstance(statement, ast.Assign)
            else [statement.target]
        )
        names = {target.id for target in targets if isinstance(target, ast.Name)}
        for name in names & {"RUNTIME_DIR", "INPUT_DIRS"}:
            value = statement.value
            expressions = (
                value.elts
                if name == "INPUT_DIRS" and isinstance(value, ast.Tuple)
                else [value]
            )
            for expression in expressions:
                path = _runtime_path(expression) if expression is not None else None
                valid = (
                    path is not None
                    and len(path.parts[:-2]) >= len(("host", "repository"))
                    and path.parts[-1] == "tmp"
                    and (name != "INPUT_DIRS" or isinstance(value, ast.Tuple))
                    and (name == "INPUT_DIRS" or path.parts[-2] == package.name)
                    and (expected is None or Path(*path.parts[:-2]) == expected)
                )
                if not valid:
                    issues.append(
                        f"{package.name}/main.py:{statement.lineno}: {name} "
                        "must use Path.home() / "
                        "'<host>/<repository>/NAME/tmp'; "
                        "RUNTIME_DIR must belong to this package "
                        "and INPUT_DIRS must be a tuple",
                    )
    return issues


def _python_source_issues(package: Package) -> list[str]:
    """Check source contracts without importing or executing package code."""
    if package.kind != "python":
        return []
    issues = []
    source = read_regular(package.root / "main.py")
    if source is not None:
        module = ast.parse(source, filename=str(package.root / "main.py"))
        if python_test_definitions(module):
            issues.append(
                f"{package.name}/main.py: move test definitions to test_main.py",
            )
        issues.extend(_runtime_directory_issues(package, module))
        binding = _module_main_binding(module)
        entrypoint = (
            binding.value
            if isinstance(binding, (ast.Assign, ast.AnnAssign))
            else binding
        )
        prefix = f"{package.name}/main.py: "
        if isinstance(entrypoint, ast.AsyncFunctionDef):
            issues.append(
                prefix + "main must be synchronous; the executable calls main()",
            )
        elif isinstance(entrypoint, (ast.FunctionDef, ast.Lambda)) and (
            len(entrypoint.args.posonlyargs) + len(entrypoint.args.args)
            > len(entrypoint.args.defaults)
            or any(default is None for default in entrypoint.args.kw_defaults)
        ):
            issues.append(prefix + "main must accept a call without arguments")
        elif isinstance(binding, (ast.Assign, ast.AnnAssign)) and isinstance(
            binding.value,
            (ast.Constant, ast.List, ast.Tuple, ast.Set, ast.Dict),
        ):
            issues.append(prefix + "main must be callable; the executable calls main()")
    tests = package.root / "test_main.py"
    if tests.is_file():
        has_python_tests(tests)
    return issues


def package_description(package: Package) -> str | None:
    """Extract declared package metadata where supported."""
    default = read_regular(package.root / "default.nix")
    return source_package_description(default) if default is not None else None


def source_package_description(source: str) -> str | None:
    """Extract a literal Nix package description without evaluation."""
    with contextlib.suppress(nix_syntax.NixSyntaxError):
        return _meta_description(source)
    return None


def _meta_description(source: str) -> str | None:
    """Return a literal meta.description through the Nix syntax tree."""
    document, expression = _metadata_expression(source, "meta", ("description",))
    return (
        None
        if expression is None
        else cast(
            "str | None",
            nix_syntax.string_value(document, expression),
        )
    )


def _attrset_expression(
    document: nix_syntax.Document,
    expression: Node,
    path: tuple[str, ...],
) -> list[Node]:
    """Find direct static bindings beneath an attribute-set expression."""
    if expression.type not in {"attrset_expression", "rec_attrset_expression"}:
        return []
    binding_set = next(
        (child for child in expression.named_children if child.type == "binding_set"),
        None,
    )
    return [
        value
        for binding in ([] if binding_set is None else binding_set.named_children)
        if binding.type == "binding"
        and (attrpath := binding.child_by_field_name("attrpath")) is not None
        and nix_syntax.static_attrpath(document, attrpath) == path
        and (value := binding.child_by_field_name("expression")) is not None
    ]


def _metadata_expression(
    source: str,
    namespace: str,
    requested_path: tuple[str, ...],
) -> tuple[nix_syntax.Document, Node | None]:
    """Find an unambiguous metadata expression in dotted or nested form."""
    document = nix_syntax.parse(source)
    matches = []
    for binding in (
        node for node in nix_syntax.walk(document.root) if node.type == "binding"
    ):
        attrpath = binding.child_by_field_name("attrpath")
        expression = binding.child_by_field_name("expression")
        if attrpath is None or expression is None:
            continue
        binding_path = nix_syntax.static_attrpath(document, attrpath)
        if binding_path == (namespace, *requested_path):
            matches.append(expression)
        elif binding_path == (namespace,):
            matches.extend(
                _attrset_expression(document, expression, requested_path),
            )
    unique = {node.start_byte: node for node in matches}
    return document, next(iter(unique.values())) if len(unique) == 1 else None


def _template_binding(document: nix_syntax.Document, name: str) -> Node | None:
    """Find an outer template binding without interpreting comments or inner scopes."""
    container = document.root
    while container.type in {"function_expression", "parenthesized_expression"}:
        child = container.child_by_field_name(
            "body" if container.type == "function_expression" else "expression",
        )
        if child is None:
            return None
        container = child
    if container.type != "let_expression":
        return None
    bindings = next(
        (child for child in container.named_children if child.type == "binding_set"),
        None,
    )
    return next(
        (
            binding.child_by_field_name("expression")
            for binding in ([] if bindings is None else bindings.named_children)
            if (attrpath := binding.child_by_field_name("attrpath")) is not None
            and nix_syntax.static_attrpath(document, attrpath) == (name,)
        ),
        None,
    )


def _nix_binding_edits(
    document: nix_syntax.Document,
    container: Node,
    path: tuple[str, ...],
    value: str,
    *,
    overwrite: bool = True,
) -> list[tuple[int, int, bytes]]:
    """Set a scoped binding, retaining unrelated fields and inherited names."""
    while container.type == "parenthesized_expression" or (
        container.type == "binary_expression"
        and document.text(container.child_by_field_name("operator")) == "//"
    ):
        container = cast(
            "Node",
            container.child_by_field_name(
                "expression"
                if container.type == "parenthesized_expression"
                else "right",
            ),
        )
    assignment = f"{'.'.join(path)} = {value};"
    if container.type not in {
        "attrset_expression",
        "rec_attrset_expression",
        "let_expression",
    }:
        replacement = f"({document.text(container)}) // {{ {assignment} }}"
        return [(container.start_byte, container.end_byte, replacement.encode())]
    bindings = next(
        (node for node in container.named_children if node.type == "binding_set"),
        None,
    )
    children = [] if bindings is None else bindings.named_children
    for binding in children:
        attrpath = binding.child_by_field_name("attrpath")
        expression = binding.child_by_field_name("expression")
        if attrpath is not None and expression is not None:
            names = nix_syntax.static_attrpath(document, attrpath)
            if names == path:
                if not overwrite or nix_syntax.compact(
                    document.text(expression),
                ) == nix_syntax.compact(
                    value,
                ):
                    return []
                return [(expression.start_byte, expression.end_byte, value.encode())]
            if names and path[: len(names)] == names:
                return _nix_binding_edits(
                    document,
                    expression,
                    path[len(names) :],
                    value,
                    overwrite=overwrite,
                )
    inherited = [
        (binding, attr)
        for binding in children
        if (attrs := binding.child_by_field_name("attrs")) is not None
        for attr in attrs.named_children
        if document.text(attr) == path[0]
    ]
    if (inherited and not overwrite) or (
        len(path) == 1
        and value == path[0]
        and any(binding.type == "inherit" for binding, _ in inherited)
    ):
        return []
    edits = [(attr.start_byte, attr.end_byte, b"") for _, attr in inherited]
    offset = (
        container.start_byte + 3
        if container.type == "let_expression"
        else container.end_byte - 1
    )
    edits.append((offset, offset, f"\n  {assignment}\n".encode()))
    return edits


def source_python_has_main(source: str | None) -> bool:
    """Recognize the module-level main binding used by canonical wrappers."""
    if source is None:
        return True
    return _module_main_binding(ast.parse(source, filename="main.py")) is not None


def _module_main_binding(module: ast.Module) -> ast.stmt | None:
    """Find the last explicit module-level binding used by the executable."""
    for node in reversed(module.body):
        if (
            (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "main"
            )
            or (
                isinstance(node, (ast.Import, ast.ImportFrom))
                and any((alias.asname or alias.name) == "main" for alias in node.names)
            )
            or (
                isinstance(node, (ast.Assign, ast.AnnAssign))
                and node.value is not None
                and any(
                    isinstance(target, ast.Name) and target.id == "main"
                    for target in (
                        node.targets if isinstance(node, ast.Assign) else [node.target]
                    )
                )
            )
        ):
            return node
    return None


def _package_body(document: nix_syntax.Document) -> tuple[Node | None, Node]:
    """Find the outer package expression and its enclosing let bindings."""
    body = document.root
    scope = None
    while body.type in {
        "function_expression",
        "let_expression",
        "parenthesized_expression",
    }:
        if body.type == "let_expression":
            scope = body
        child = body.child_by_field_name(
            "expression" if body.type == "parenthesized_expression" else "body",
        )
        if child is None:
            msg = "Package definition has no body"
            raise CommandError(msg)
        body = child
    return scope, body


def _python_required_edits(
    package: Package,
    source: str,
) -> dict[str, list[tuple[int, int, bytes]]]:
    """Enforce the shared Python constructor's source interface."""
    document = nix_syntax.parse(source)
    _scope, body = _package_body(document)
    argument = body.child_by_field_name("argument")
    function = body.child_by_field_name("function")
    constructor = "(inputs.afairesi or inputs.self).lib.mkPythonPackage"
    if (
        body.type != "apply_expression"
        or function is None
        or nix_syntax.compact(document.text(function))
        != nix_syntax.compact(constructor)
        or argument is None
        or argument.type not in {"attrset_expression", "rec_attrset_expression"}
    ):
        msg = "Python package must call the shared mkPythonPackage constructor"
        raise CommandError(msg)
    edits = {
        "pkgs": _nix_binding_edits(document, argument, ("pkgs",), "pkgs"),
        "src": _nix_binding_edits(document, argument, ("src",), "./."),
        "executable": _nix_binding_edits(
            document,
            argument,
            ("executable",),
            "true"
            if source_python_has_main(read_regular(package.root / "main.py"))
            else "false",
        ),
    }
    outer = document.root
    while outer.type == "parenthesized_expression":
        outer = cast("Node", outer.child_by_field_name("expression"))
    formals = outer.child_by_field_name("formals")
    if formals is None:
        msg = "Python package must accept inputs and pkgs arguments"
        raise CommandError(msg)
    names = {
        document.text(name)
        for child in formals.named_children
        if child.type == "formal"
        and (name := child.child_by_field_name("name")) is not None
    }
    missing = [name for name in ("inputs", "pkgs") if name not in names]
    if missing:
        edits["function arguments"] = [
            (
                formals.start_byte + 1,
                formals.start_byte + 1,
                (" " + ", ".join(missing) + ",").encode(),
            ),
        ]
    return edits


def _canonical_python_default(package: Package, source: str) -> str:
    """Repair required Python bindings without removing custom attributes."""
    edits = _python_required_edits(package, source)
    return cast(
        "bytes",
        nix_syntax.apply_edits(
            source.encode(),
            [edit for changes in edits.values() for edit in changes],
        ),
    ).decode()


def canonical_package_default(package: Package) -> str | None:
    """Render a package definition while retaining package-specific fields."""
    if package.kind == "nix":
        return None
    source = read_regular(package.root / "default.nix")
    if source is None:
        rendered = scaffold(package.kind, package.name, None)[
            Path() / package.name / "default.nix"
        ]
        return (
            _canonical_python_default(package, rendered)
            if package.kind == "python"
            else rendered
        )
    if package.kind == "python":
        return _canonical_python_default(package, source)
    document = nix_syntax.parse(source, str(package.root / "default.nix"))
    scope, body = _package_body(document)
    function = body.child_by_field_name("function")
    argument = body.child_by_field_name("argument")
    constructor = {
        "html": "pkgs.writeShellApplication",
        "latex": "pkgs.stdenv.mkDerivation",
    }[package.kind]
    if (
        body.type != "apply_expression"
        or function is None
        or nix_syntax.compact(document.text(function)) != constructor
        or argument is None
        or argument.type not in {"attrset_expression", "rec_attrset_expression"}
    ):
        msg = f"{package.kind} package must call {constructor} with an attribute set"
        raise CommandError(msg)
    rendered = scaffold(package.kind, package.name, package_description(package))[
        Path() / package.name / "default.nix"
    ]
    template = nix_syntax.parse(rendered)
    _template_scope, template_body = _package_body(template)
    template_argument = template_body.child_by_field_name("argument")
    if template_argument is None:
        msg = "Package scaffold omitted its attributes"
        raise AssertionError(msg)
    required = (
        ("name", "text")
        if package.kind == "html"
        else ("pname", "buildPhase", "installPhase", "src", "strictDeps")
    )
    defaults = ("runtimeInputs",) if package.kind == "html" else ("nativeBuildInputs",)
    edits = []
    for name in (*required, *defaults):
        value = (
            "pname"
            if name == "pname"
            else template.text(
                _attrset_expression(template, template_argument, (name,))[0],
            )
        )
        edits.extend(
            _nix_binding_edits(
                document,
                argument,
                (name,),
                value,
                overwrite=name not in defaults,
            ),
        )
    bindings = _canonical_template_bindings(package, document, template)
    if scope is None:
        declarations = " ".join(f"{name} = {value};" for name, value in bindings)
        edits.append(
            (body.start_byte, body.start_byte, f"let {declarations} in ".encode()),
        )
    else:
        for name, value in bindings:
            edits.extend(_nix_binding_edits(document, scope, (name,), value))
    return cast("bytes", nix_syntax.apply_edits(source.encode(), edits)).decode()


def _canonical_template_bindings(
    package: Package,
    document: nix_syntax.Document,
    template: nix_syntax.Document,
) -> list[tuple[str, str]]:
    """Collect required local names while preserving existing site bindings."""
    pname = _template_binding(template, "pname")
    if pname is None:
        msg = "Package scaffold omitted its name"
        raise AssertionError(msg)
    bindings = [("pname", template.text(pname))]
    if package.kind == "html":
        site = _template_binding(template, "site")
        if site is None:
            msg = "HTML scaffold omitted its site"
            raise AssertionError(msg)
        if _template_binding(document, "site") is None:
            bindings.append(("site", template.text(site)))
    return bindings


def _write_managed_nix(
    root: Path,
    relative: Path,
    source: str,
    *,
    dry_run: bool,
) -> bool:
    """Write a Nix template only when its formatted structure differs."""
    current = read_regular(root / relative)
    if current is not None and nix_syntax.compact(current) == nix_syntax.compact(
        source,
    ):
        source = current
    return write_managed(root, relative, source, dry_run=dry_run)


def _converge_packages(root: Path, packages: list[Package], *, dry_run: bool) -> bool:
    """Converge package templates and required source files."""
    changed = False
    for package in packages:
        expected_default = canonical_package_default(package)
        if expected_default is not None:
            relative = Path() / package.name / "default.nix"
            changed |= _write_managed_nix(
                root,
                relative,
                expected_default,
                dry_run=dry_run,
            )
        expected_files = scaffold(
            package.kind,
            package.name,
            package_description(package),
        )
        for relative, source in expected_files.items():
            if (
                relative.name == "default.nix"
                or relative not in required_package_files(package)
                or (root / relative).exists()
            ):
                continue
            changed |= write_managed(
                root,
                relative,
                source,
                dry_run=dry_run,
                executable=relative.name == "main.py",
            )
    return changed


def _converge_allowed_files(
    root: Path,
    allowed: set[Path],
    tracked: set[Path],
    python_entrypoints: set[Path],
    *,
    dry_run: bool,
) -> bool:
    """Stage declared files and normalize their executable bits."""
    changed = False
    for relative in sorted(allowed - tracked):
        path = root / relative
        if not path.is_file() or path.is_symlink():
            continue
        report_change(f"stage '{relative}'", dry_run=dry_run)
        changed = True
        if not dry_run:
            git(root, ["add", "--", str(relative)])
            tracked.add(relative)
    for relative in sorted(allowed):
        path = root / relative
        if not path.is_file() or path.is_symlink():
            continue
        executable = relative in python_entrypoints
        if bool(path.stat().st_mode & 0o111) != executable:
            report_change(f"set mode on '{relative}'", dry_run=dry_run)
            changed = True
            if not dry_run:
                path.chmod(0o755 if executable else 0o644)
                git(root, ["add", "--", str(relative)])
    return changed


def _converge_prm_files(
    root: Path,
    prm: set[Path],
    tracked: set[Path],
    *,
    dry_run: bool,
) -> tuple[bool, set[Path]]:
    """Stage unmanaged files below prm trees without changing their modes."""
    files = {
        path.relative_to(root)
        for tree in prm
        for path in (root / tree).rglob("*")
        if path.is_file() or path.is_symlink()
    }
    changed = False
    for relative in sorted(files - tracked):
        report_change(f"stage '{relative}'", dry_run=dry_run)
        changed = True
        if not dry_run:
            git(root, ["add", "--", str(relative)])
            tracked.add(relative)
    return changed, files


def _remove_unsupported_tracked(
    root: Path,
    tracked: set[Path],
    tmp: set[Path],
    protected: set[Path],
    *,
    dry_run: bool,
) -> bool:
    """Untrack tmp content and delete unsupported tracked paths."""
    changed = False
    for relative in sorted(path for path in tracked if beneath(path, tmp)):
        report_change(f"untrack '{relative}'", dry_run=dry_run)
        changed = True
        if not dry_run:
            git(root, ["rm", "--cached", "-r", "--", str(relative)])
    for relative in sorted(tracked):
        if beneath(relative, protected):
            continue
        report_change(f"remove '{relative}'", dry_run=dry_run)
        changed = True
        if not dry_run:
            git(root, ["rm", "-rf", "--", str(relative)])
    return changed


def _cleanup_flake(root: Path, packages: list[Package], *, dry_run: bool) -> bool:
    """Remove undeclared files while preserving permitted tmp trees."""
    allowed = allowed_paths(root, packages)
    prm = prm_directories(root)
    tmp = tmp_directories(root)
    tracked = _tracked_paths(root)
    python_entrypoints = {
        Path() / package.name / "main.py"
        for package in packages
        if package.kind == "python"
    }
    changed = _converge_allowed_files(
        root,
        allowed,
        tracked,
        python_entrypoints,
        dry_run=dry_run,
    )
    prm_changed, prm_files = _converge_prm_files(
        root,
        prm,
        tracked,
        dry_run=dry_run,
    )
    changed |= prm_changed
    changed |= _remove_unsupported_tracked(
        root,
        tracked,
        tmp,
        prm | tmp | allowed,
        dry_run=dry_run,
    )
    clean_arguments = _flake_clean_arguments(dry_run=dry_run)
    if dry_run:
        for relative in sorted(allowed | prm_files):
            if (root / relative).exists():
                clean_arguments.extend(("-e", f"/{relative.as_posix()}"))
    clean = git(root, clean_arguments)
    if clean.stdout:
        sys.stdout.write(clean.stdout)
        changed = True
    return changed


def _resource_constructor(
    document: nix_syntax.Document,
    expression: Node,
    visited: frozenset[int] = frozenset(),
) -> str | None:
    """Resolve static constructor references and lexical let aliases."""
    if expression.start_byte in visited:
        return None
    visited = visited | {expression.start_byte}
    if expression.type == "parenthesized_expression":
        inner = expression.child_by_field_name("expression")
        return (
            None if inner is None else _resource_constructor(document, inner, visited)
        )
    if expression.type == "select_expression":
        attrpath = expression.child_by_field_name("attrpath")
        path = (
            None if attrpath is None else nix_syntax.static_attrpath(document, attrpath)
        )
        return None if path is None else path[-1]
    if expression.type != "variable_expression":
        return None
    return _resource_variable_constructor(document, expression, visited)


def _resource_variable_constructor(
    document: nix_syntax.Document,
    expression: Node,
    visited: frozenset[int],
) -> str | None:
    """Resolve aliases without crossing a binding that shadows their names."""
    name = document.text(expression)
    scope = expression.parent
    while scope is not None:
        if scope.type == "function_expression":
            parameters = [
                child
                for field in ("universal", "formals")
                if (child := scope.child_by_field_name(field)) is not None
            ]
            if any(
                node.type == "identifier" and document.text(node) == name
                for parameter in parameters
                for node in nix_syntax.walk(parameter)
            ):
                return None
        if scope.type in {"let_expression", "rec_attrset_expression"}:
            bindings = next(
                (node for node in scope.named_children if node.type == "binding_set"),
                None,
            )
            for binding in [] if bindings is None else bindings.named_children:
                path = binding.child_by_field_name("attrpath")
                value = binding.child_by_field_name("expression")
                if (
                    path is not None
                    and nix_syntax.static_attrpath(document, path) == (name,)
                    and value is not None
                ):
                    return _resource_constructor(document, value, visited)
        scope = scope.parent
    return name if name == "derivation" else None


def _flake_resource_issues(root: Path) -> list[str]:
    """Keep resource definitions in their owners and composition in the flake."""
    source = read_regular(root / "flake.nix")
    if source is None or not source.strip():
        return []
    document = nix_syntax.parse(source, "flake.nix")
    outputs = _attrset_expression(document, document.root, ("outputs",))
    constructors = {
        "buildPythonPackage": (1, "NAME/default.nix"),
        "buildPythonApplication": (1, "NAME/default.nix"),
        "derivation": (1, "NAME/default.nix"),
        "mkDerivation": (1, "NAME/default.nix"),
        "mkPythonPackage": (1, "NAME/default.nix"),
        "nixosSystem": (1, "NAME/configuration.nix"),
        "runNixOSTest": (1, "the host testing structure"),
        "runCommand": (3, "the package or host structure"),
        "runCommandLocal": (3, "the package or host structure"),
    }
    issues = []
    reported = set()
    for output in outputs:
        for node in nix_syntax.walk(output):
            if node.type != "apply_expression":
                continue
            function = node
            arguments = 0
            while function.type == "apply_expression":
                child = function.child_by_field_name("function")
                if child is None:
                    break
                arguments += 1
                function = child
            constructor = _resource_constructor(document, function)
            if constructor not in constructors or function.start_byte in reported:
                continue
            arity, destination = constructors[constructor]
            if arguments < arity:
                continue
            reported.add(function.start_byte)
            issues.append(
                f"flake.nix:{node.start_point.row + 1}: {constructor} defines a "
                f"resource outside its owner; move the definition to {destination}. "
                "flake.nix should compose existing resources and apply repository "
                "policy; aliases and wrappers of generated outputs are supported",
            )
    return issues


def converge_flake(
    root: Path,
    *,
    dry_run: bool,
    fail_on_changes: bool = True,
) -> list[Package]:
    """Converge required files, structure, templates, and root whitelist."""
    missing = [
        name
        for name in (".gitignore", "flake.nix", "flake.lock")
        if not (root / name).is_file()
    ]
    if missing:
        raise CommandError("missing required file: " + missing[0])
    packages = detect_packages(root)
    issues = _flake_resource_issues(root)
    for package in packages:
        validate_name(package.name)
        issues.extend(_python_source_issues(package))
        canonical_package_default(package)
    if issues:
        raise CommandError("\n".join(issues))
    changed = _converge_packages(root, packages, dry_run=dry_run)
    expected = render_gitignore(allowed_paths(root, packages), prm_directories(root))
    actual = read_regular(root / ".gitignore")
    if actual != expected:
        changed |= write_managed(
            root,
            Path(".gitignore"),
            expected,
            dry_run=dry_run,
        )
    changed |= _cleanup_flake(root, packages, dry_run=dry_run)
    if dry_run and changed:
        if fail_on_changes:
            msg = "flake repository would change"
            raise CommandError(msg)
        return packages
    return validate_flake_source(root)


def validate_flake_source(root: Path) -> list[Package]:
    """Validate a Git-filtered flake source without requiring Git metadata."""
    packages, issues = inspect_structure(root)
    issues.extend(_flake_resource_issues(root))
    for required in (".gitignore", "flake.lock", "flake.nix"):
        if not (root / required).is_file():
            issues.append(f"{required}: missing required regular file")
    expected_ignore = render_gitignore(
        allowed_paths(root, packages),
        prm_directories(root),
    )
    if read_regular(root / ".gitignore") != expected_ignore:
        issues.append(".gitignore: does not match the canonical source whitelist")
    for package in packages:
        issues.extend(_source_package_issues(root, package))
    if issues:
        formatted_issues = "\n".join(f"  - {issue}" for issue in issues)
        msg = f"repository layout validation failed:\n{formatted_issues}"
        raise CommandError(msg)
    _validate_nix_sources(packages)
    return packages


def _validate_nix_sources(packages: list[Package]) -> None:
    """Parse package definitions after validating repository structure."""
    for package in packages:
        default = package.root / "default.nix"
        if default.is_file():
            nix_syntax.parse(default.read_bytes(), str(default))


def _source_package_issues(root: Path, package: Package) -> list[str]:
    """Return source-only package-template issues."""
    issues: list[str] = []
    relative = Path() / package.name / "default.nix"
    actual = read_regular(root / relative)
    expected = canonical_package_default(package)
    if (
        expected is not None
        and actual is not None
        and nix_syntax.compact(actual) != nix_syntax.compact(expected)
    ):
        issues.append(f"{relative}: differs from its canonical package template")
    return issues


def scaffold(
    kind: str,
    name: str,
    description: str | None,
) -> dict[Path, str]:
    """Render one supported package."""
    description = (
        description
        or {
            "python": "A Python package.",
            "html": "An HTML package.",
            "latex": "A LaTeX package.",
            "nix": "A Nix package.",
        }[kind]
    )
    description_literal = nix_syntax.quote_string(description)
    root = Path() / name
    defaults = {
        "python": """{ inputs, pkgs, ... }:
(inputs.afairesi or inputs.self).lib.mkPythonPackage {
  inherit pkgs;
  executable = true;
  meta.description = __DESCRIPTION__;
  propagatedBuildInputs = [ ];
  src = ./.;
}
""",
        "html": """{ pkgs, ... }:
let
  pname = baseNameOf ./.;
  site = pkgs.runCommand "${pname}-site" { } ''
    mkdir -p "$out"
    cp ${./index.html} "$out/index.html"
    for asset in script.js style.css; do
      if [ -f ${./.}/"$asset" ]; then
        cp ${./.}/"$asset" "$out/$asset"
      fi
    done
    if [ -d ${./.}/prm ]; then
      cp -R ${./.}/prm "$out/prm"
      chmod -R u+w "$out/prm"
    fi
    runHook postInstall
  '';
in
pkgs.writeShellApplication {
  meta.description = __DESCRIPTION__;
  name = pname;
  runtimeInputs = [ pkgs.http-server ]
    ++ pkgs.lib.optionals pkgs.stdenv.hostPlatform.isLinux [ pkgs.xdg-utils ];
  text = ''
    open_args=()
    if [[ -n "''${DISPLAY:-}" || -n "''${WAYLAND_DISPLAY:-}" ]]; then
      open_args=(-o /)
    fi
    server_args=()
    for argument in "$@"; do
      case "$argument" in
        --no-open)
          open_args=()
          ;;
        -o|--o|-o=*|--o=*|--no-o)
          open_args=()
          server_args+=("$argument")
          ;;
        -h|--help)
          printf '%s\\n' 'Desktop runs open the browser; --no-open disables this.'
          server_args+=("$argument")
          ;;
        *)
          server_args+=("$argument")
          ;;
      esac
    done
    exec http-server ${site} "''${open_args[@]}" "''${server_args[@]}"
  '';
}
""",
        "latex": """{ pkgs, ... }:
let
  pname = baseNameOf ./.;
in
pkgs.stdenv.mkDerivation {
  inherit pname;
  buildPhase = ''
    latexmk -pdf ms.tex
  '';
  installPhase = ''
    install -Dm644 ms.pdf "$out/ms.pdf"
  '';
  meta.description = __DESCRIPTION__;
  nativeBuildInputs = [ pkgs.texliveFull ];
  src = ./.;
  strictDeps = true;
  version = "0.0.0";
}
""",
        "nix": """{ pkgs, ... }:
pkgs.writeTextFile {
  name = baseNameOf ./.;
  text = "";
  meta.description = __DESCRIPTION__;
}
""",
    }
    default = defaults[kind].replace("__DESCRIPTION__", description_literal)
    if "-" in name:
        default = default.replace(
            "baseNameOf ./.",
            'builtins.replaceStrings [ "-" ] [ "_" ] (baseNameOf ./.)',
        )
    files: dict[Path, str] = {root / "default.nix": default}
    if kind == "python":
        files[root / "main.py"] = (
            "#!/usr/bin/env python3\n"
            "# ruff: noqa: CPY001 - Set copyright ownership when publishing.\n"
            f"{description!r}\n\nimport argparse\n\n\n"
            "def parser() -> argparse.ArgumentParser:\n"
            '    """Declare the command-line interface."""\n'
            f"    return argparse.ArgumentParser(description={description!r})\n\n\n"
            "def main(argv: list[str] | None = None) -> None:\n"
            f'    """Run {name}."""\n'
            "    parser().parse_args(argv)\n\n\n"
            'if __name__ == "__main__":\n    main()\n'
        )
    elif kind == "html":
        files.update(
            {
                root / "index.html": "<!doctype html><html><body></body></html>\n",
                root / "script.js": (
                    'document.documentElement.dataset.javascript = "enabled";\n'
                ),
                root / "style.css": "",
            },
        )
    elif kind == "latex":
        files.update(
            {
                root / "ms.tex": (
                    "\\documentclass{article}\n\\begin{document}\n\\end{document}\n"
                ),
                root / "ms.bib": "",
            },
        )
    return files


def validate_managed_path(root: Path, relative: Path) -> None:
    """Reject symlinks and non-directory ancestors before modifying managed paths."""
    for component in (*reversed(relative.parents), relative):
        path = root / component
        if path.is_symlink() or (
            component != relative and path.exists() and not path.is_dir()
        ):
            msg = (
                f"{component}: managed paths cannot contain symlinks or file ancestors"
            )
            raise CommandError(msg)


def missing_directories(root: Path, paths: list[Path]) -> list[Path]:
    """Remember missing directories for cleanup after a failed operation."""
    directories = {parent for path in paths for parent in (path, *path.parents)}
    return sorted(
        (
            root / path
            for path in directories
            if path != Path() and not (root / path).exists()
        ),
        key=lambda path: len(path.parts),
        reverse=True,
    )


def remove_empty_directories(directories: list[Path]) -> None:
    """Remove only directories created by an operation that remain empty."""
    for directory in directories:
        with contextlib.suppress(OSError):
            directory.rmdir()


@contextlib.contextmanager
def preserve_whitelist_on_error(root: Path) -> Iterator[None]:
    """Restore the whitelist and its staged entry if a lifecycle operation fails."""
    path = root / ".gitignore"
    read_regular(path)
    snapshot = (
        (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
        if path.exists()
        else None
    )
    arguments = ["ls-files", "--stage", "-z", "--", ".gitignore"]
    indexed = git(root, arguments).stdout
    try:
        yield
    except BaseException:
        if snapshot is None:
            path.unlink(missing_ok=True)
        else:
            contents, mode = snapshot
            path.write_bytes(contents)
            path.chmod(mode)
        if git(root, arguments).stdout != indexed:
            if indexed:
                run(
                    ["git", "update-index", "-z", "--index-info"],
                    cwd=root,
                    input_source=indexed,
                )
            else:
                git(root, ["update-index", "--force-remove", "--", ".gitignore"])
        raise


def add_package(
    root: Path,
    kind: str,
    name: str,
    description: str | None = None,
) -> None:
    """Create a package transactionally and stage its managed files."""
    if kind not in PACKAGE_KINDS:
        msg = (
            f"unsupported package type: {kind}\n"
            f"hint: supported package types: {', '.join(PACKAGE_KINDS)}"
        )
        raise CommandError(
            msg,
        )
    validate_name(name)
    files = scaffold(kind, name, description)
    relative = Path() / name
    if (root / relative).exists():
        msg = f"package already exists: {name}"
        raise CommandError(msg)
    validate_python_package_names(
        [*detect_packages(root), Package(name, kind, root / relative)],
    )
    directories = missing_directories(root, [relative])
    created: list[Path] = []
    try:
        with preserve_whitelist_on_error(root):
            for relative, source in files.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                created.append(path)
                path.write_text(source, encoding="utf-8")
                path.chmod(0o755 if path.name == "main.py" else 0o644)
            _refresh_gitignore(root)
            generated = [str(path.relative_to(root)) for path in created] + [
                ".gitignore",
            ]
            git(root, ["add", "--", *generated])
    except BaseException:
        for path in reversed(created):
            path.unlink(missing_ok=True)
        remove_empty_directories(directories)
        raise


def add_host(root: Path, name: str) -> None:
    """Create a host and stage its canonical configuration."""
    validate_host_name(name)
    relative = Path() / name / "configuration.nix"
    path = root / relative
    if path.parent.exists():
        msg = f"host already exists: {name}"
        raise CommandError(msg)
    directories = missing_directories(root, [relative.parent])
    try:
        with preserve_whitelist_on_error(root):
            path.parent.mkdir(parents=True)
            path.write_text("{ ... }: { }\n", encoding="utf-8")
            _refresh_gitignore(root)
            git(
                root,
                [
                    "add",
                    "--",
                    str(relative),
                    ".gitignore",
                ],
            )
    except BaseException:
        path.unlink(missing_ok=True)
        remove_empty_directories(directories)
        raise


def remove_resource(
    root: Path,
    value: str,
    *,
    force: bool = False,
) -> None:
    """Remove a canonical package or host and stage its related metadata."""
    kind, name, relative = _parse_resource_path(value, root)
    resource_root = root / relative
    marker = "default.nix" if kind == "package" else "configuration.nix"
    if not resource_root.is_dir() or not (resource_root / marker).is_file():
        msg = f"{kind} does not exist: {name}"
        raise CommandError(msg)
    arguments = ["rm", "-r"]
    if force:
        arguments.append("--force")
    git(root, [*arguments, "--", str(relative)], capture_output=False)
    report_change("update '.gitignore'", dry_run=False)
    _refresh_gitignore(root)
    git(root, ["add", "--", ".gitignore"])


def _parse_resource_path(
    value: str,
    root: Path | None = None,
    *,
    kind: str | None = None,
) -> tuple[str, str, Path]:
    """Parse one root resource name and infer existing resources from markers."""
    path = Path(value)
    if (
        path.is_absolute()
        or len(path.parts) != RESOURCE_PATH_COMPONENTS
        or value != path.name
        or path.name in {PRM_NAME, TMP_NAME}
    ):
        msg = f"resource path must be a root directory name: {value}"
        raise CommandError(msg)
    name = path.name
    if kind is None and root is not None:
        kind = "package" if (root / path / "default.nix").is_file() else "host"
    if kind == "host":
        validate_host_name(name)
    elif kind == "package":
        validate_name(name)
    elif not (
        re.fullmatch(r"[a-z0-9]+(?:_[a-z0-9]+)*|[a-z0-9]+(?:-[a-z0-9]+)*", name)
        or re.fullmatch(r"[a-z][A-Za-z0-9]*", name)
    ):
        msg = f"invalid resource name: {name}"
        raise CommandError(msg)
    return kind or "package", name, path


def _renamed_runtime_expression(
    expression: cst.BaseExpression,
    old: Path,
    new: Path,
    repository: Path | None,
) -> cst.BaseExpression:
    """Change only the package component of a declared literal runtime path."""
    import libcst as cst  # noqa: PLC0415 - Load the rewriter only for renames.

    code = cst.Module([]).code_for_node(expression)
    path = _runtime_path(ast.parse(code, mode="eval").body)
    if path is None or path.parts[-2:] != (*old.parts, "tmp"):
        return expression
    if repository is not None and Path(*path.parts[:-2]) != repository:
        return expression
    literals: list[cst.SimpleString | cst.ConcatenatedString] = []
    current = expression
    while isinstance(current, cst.BinaryOperation) and isinstance(
        current.operator,
        cst.Divide,
    ):
        if not isinstance(current.right, (cst.SimpleString, cst.ConcatenatedString)):
            return expression
        literals.insert(0, current.right)
        current = current.left
    position = len(path.parts) - 2
    for literal in literals:
        components = list(Path(cast("str", literal.evaluated_value)).parts)
        if position < len(components):
            components[position] = new.name
            if isinstance(literal, cst.ConcatenatedString):
                return expression.deep_replace(
                    literal,
                    cst.parse_expression(repr("/".join(components))),
                )
            value = (
                literal.prefix + literal.quote + "/".join(components) + literal.quote
            )
            return expression.with_deep_changes(literal, value=value)
        position -= len(components)
    return expression


def _renamed_runtime_source(
    source: str,
    old: Path,
    new: Path,
    *,
    own_package: bool,
    repository: Path | None,
) -> str:
    """Use LibCST to edit module declarations without rewriting their formatting."""
    import libcst as cst  # noqa: PLC0415 - Load the rewriter only for renames.

    try:
        module = cst.parse_module(source)
    except cst.ParserSyntaxError as error:
        raise CommandError(str(error)) from error
    body: list[cst.BaseStatement] = []
    for line in module.body:
        if not isinstance(line, cst.SimpleStatementLine):
            body.append(line)
            continue
        statements = []
        for statement in line.body:
            if isinstance(statement, cst.Assign):
                targets = [target.target for target in statement.targets]
            elif isinstance(statement, cst.AnnAssign) and statement.value is not None:
                targets = [statement.target]
            else:
                statements.append(statement)
                continue
            names = {target.value for target in targets if isinstance(target, cst.Name)}
            value = cast("cst.BaseExpression", statement.value)
            if "RUNTIME_DIR" in names and own_package:
                value = _renamed_runtime_expression(value, old, new, repository)
            if (
                "INPUT_DIRS" in names
                and isinstance(value, cst.Tuple)
                and (own_package or repository is not None)
            ):
                value = value.with_changes(
                    elements=[
                        element.with_changes(
                            value=_renamed_runtime_expression(
                                element.value,
                                old,
                                new,
                                repository,
                            ),
                        )
                        if isinstance(element, cst.Element)
                        else element
                        for element in value.elements
                    ],
                )
            statements.append(statement.with_changes(value=value))
        body.append(line.with_changes(body=statements))
    return module.with_changes(body=body).code


def _runtime_rename_updates(
    root: Path,
    old: Path,
    new: Path,
) -> list[tuple[Path, str, str | None, str | None]]:
    """Prepare working and indexed runtime edits independently before moving files."""
    repository = _repository_remote_path(root)
    updates = []
    for package in detect_packages(root):
        if package.kind != "python":
            continue
        relative = Path() / package.name / "main.py"
        source = read_regular(root / relative)
        if source is None:
            continue
        own_package = relative.parent == old
        working = _renamed_runtime_source(
            source,
            old,
            new,
            own_package=own_package,
            repository=repository,
        )
        indexed = git(root, ["ls-files", "--stage", "--", str(relative)]).stdout
        staged = None
        mode = None
        if indexed:
            if len(indexed.splitlines()) != 1 or indexed.split()[2] != "0":
                msg = f"{relative}: resolve index conflicts before renaming"
                raise CommandError(msg)
            mode = indexed.split()[0]
            staged_source = git(root, ["show", f":{relative}"]).stdout
            staged = _renamed_runtime_source(
                staged_source,
                old,
                new,
                own_package=own_package,
                repository=repository,
            )
        if working != source or (staged is not None and staged != staged_source):
            destination = new / relative.name if own_package else relative
            updates.append((destination, working, staged, mode))
    return updates


def _apply_runtime_updates(
    root: Path,
    updates: list[tuple[Path, str, str | None, str | None]],
) -> None:
    """Apply prepared edits without staging unrelated working changes."""
    for relative, working, staged, mode in updates:
        report_change(f"update runtime paths in '{relative}'", dry_run=False)
        (root / relative).write_text(working, encoding="utf-8")
        if staged is not None and mode is not None:
            blob = run(
                ["git", "hash-object", "-w", "--stdin"],
                cwd=root,
                input_source=staged,
            ).stdout.strip()
            git(root, ["update-index", "--cacheinfo", mode, blob, str(relative)])


def rename_resource(
    root: Path,
    source: str,
    destination: str,
) -> None:
    """Rename one canonical package or host and stage its related metadata."""
    source_kind, source_name, source_relative = _parse_resource_path(source, root)
    _, _, destination_relative = _parse_resource_path(
        destination,
        kind=source_kind,
    )
    source_path = root / source_relative
    destination_path = root / destination_relative
    marker = "default.nix" if source_kind == "package" else "configuration.nix"
    if not source_path.is_dir() or not (source_path / marker).is_file():
        msg = f"{source_kind} does not exist: {source_name}"
        raise CommandError(msg)
    if destination_path.exists():
        msg = f"destination already exists: {destination_relative}"
        raise CommandError(msg)
    packages = detect_packages(root)
    validate_python_package_names(
        [
            Package(destination_relative.name, package.kind, destination_path)
            if package.root == source_path
            else package
            for package in packages
        ],
    )
    updates = (
        _runtime_rename_updates(root, source_relative, destination_relative)
        if source_kind == "package"
        else []
    )
    report_change(
        f"move '{source_relative}' to '{destination_relative}'",
        dry_run=False,
    )
    report_change("update '.gitignore'", dry_run=False)
    git(root, ["mv", "--", str(source_relative), str(destination_relative)])
    _apply_runtime_updates(root, updates)
    _refresh_gitignore(root)
    git(root, ["add", "--", ".gitignore"])


def require_nix_flakes() -> str:
    """Check the effective Nix features before initialization changes files."""
    executable = "nix"
    try:
        completed = run(
            [executable, "config", "show", "experimental-features"],
            check=False,
        )
    except FileNotFoundError as error:
        msg = f"Nix executable not found: {executable}; install Nix to use repo init"
        raise CommandError(msg) from error
    if completed.returncode != 0:
        msg = f"could not check Nix features: {completed.stderr.strip()}"
        raise CommandError(
            msg + "\nrepo init requires Nix with "
            "'experimental-features = nix-command flakes'",
        )
    missing = {"nix-command", "flakes"} - set(completed.stdout.split())
    if missing:
        msg = (
            f"repo init requires Nix features: {', '.join(sorted(missing))}; "
            "enable them with 'experimental-features = nix-command flakes' "
            "in your Nix configuration"
        )
        raise CommandError(msg)
    return executable


def initialize_flake(root: Path, *, nix_executable: str | None = None) -> None:
    """Scaffold and format a new flake in an existing repository directory."""
    executable = nix_executable or require_nix_flakes()
    (root / "flake.nix").write_text(
        "{\n"
        '  inputs.afairesi.url = "github:afairesi/afairesi";\n'
        "  outputs =\n"
        "    inputs:\n"
        "    inputs.afairesi.lib.mkFlake {\n"
        "      inherit inputs;\n"
        "    };\n"
        "}\n",
        encoding="utf-8",
    )
    git(root, ["add", "--", "flake.nix"])
    run([executable, "flake", "lock"], cwd=root, capture_output=False)
    packages = detect_packages(root)
    (root / ".gitignore").write_text(
        render_gitignore(
            allowed_paths(root, packages) | {Path(".gitignore")},
            prm_directories(root),
        ),
        encoding="utf-8",
    )
    git(root, ["add", "--", ".gitignore", "flake.lock"])
    run([executable, "fmt"], cwd=root, capture_output=False)
    git(root, ["add", "--", ".gitignore", "flake.lock", "flake.nix"])


def initialize_repository() -> None:
    """Initialize an empty Git worktree transactionally and stage its files."""
    root = repository_root()
    if (
        any(path.name != ".git" for path in root.iterdir())
        or git(
            root,
            ["ls-files", "--cached"],
        ).stdout
    ):
        msg = "repo init requires an empty Git worktree; existing files are preserved"
        raise CommandError(msg)
    executable = require_nix_flakes()
    index = Path(
        git(
            root,
            ["rev-parse", "--path-format=absolute", "--git-path", "index"],
        ).stdout.strip(),
    )
    original_index = (
        (index.read_bytes(), stat.S_IMODE(index.stat().st_mode))
        if index.exists()
        else None
    )
    try:
        initialize_flake(root, nix_executable=executable)
    except BaseException:
        for path in root.iterdir():
            if path.name == ".git":
                continue
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
        if original_index is None:
            index.unlink(missing_ok=True)
        else:
            contents, mode = original_index
            index.write_bytes(contents)
            index.chmod(mode)
        raise


def _unittest_classes(module: ast.Module) -> set[str]:
    """Recognize unittest subclasses regardless of their class names."""
    bases: set[str] = set()
    case_types = {"TestCase", "IsolatedAsyncioTestCase"}
    for node in module.body:
        if isinstance(node, ast.Import):
            bases.update(
                (alias.asname or alias.name) + "." + case
                for alias in node.names
                if alias.name == "unittest"
                for case in case_types
            )
        elif isinstance(node, ast.ImportFrom) and node.module == "unittest":
            bases.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name in case_types
            )
    classes = [node for node in module.body if isinstance(node, ast.ClassDef)]
    found: set[str] = set()
    while additions := {
        node.name
        for node in classes
        if node.name not in found
        and any(ast.unparse(base) in bases | found for base in node.bases)
    }:
        found.update(additions)
    return found


def _command_target(target: Path | None) -> Path:
    """Honor an explicit path or select the current Afairesi repository."""
    if target is not None:
        return resolve_repository_path(target)
    root = repository_root()
    if repository_type(root) != "flake":
        msg = (
            "test campaigns require a flake; use git submodule foreach "
            "'afairesi repo test' for home"
        )
        raise CommandError(msg)
    return root


def _test_target_root(package: Path) -> Path:
    """Validate the canonical target and return its flake root."""
    validate_name(package.name)
    root = package.parent
    if not (root / "flake.nix").is_file() or not all(
        (package / name).is_file()
        for name in ("default.nix", "main.py", "test_main.py")
    ):
        message = (
            "expected a canonical NAME with default.nix, main.py "
            "and test_main.py inside a flake"
        )
        raise CommandError(message)
    return root


def _copy_test_sources(root: Path, workspace: Path) -> None:
    """Copy package sources and supporting assets without tmp or metadata."""
    ignored = shutil.ignore_patterns(
        "tmp",
        ".git",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "*.pyc",
    )
    for package in detect_packages(root):
        shutil.copytree(package.root, workspace / package.name, ignore=ignored)
    if (root / "prm").is_dir():
        shutil.copytree(root / "prm", workspace / "prm", ignore=ignored)


def _stop_test_group(pid: int) -> None:
    """Terminate a subprocess session, including its descendants."""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pid, signal.SIGKILL)


def _run_test_command(
    command: list[str],
    workspace: Path,
    log: Path,
    *,
    timeout: float | None = None,
) -> None:
    """Capture command output and clean up subprocess groups on interruption."""
    with (
        log.open("w", encoding="utf-8") as output,
        subprocess.Popen(  # noqa: S603
            command,
            cwd=workspace,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        ) as process,
    ):
        try:
            code = process.wait(timeout=timeout)
        except (KeyboardInterrupt, subprocess.TimeoutExpired):
            active = workspace / "active-test-pgid"
            if active.exists():
                _stop_test_group(int(active.read_text(encoding="utf-8")))
            _stop_test_group(process.pid)
            process.wait()
            raise
    if code:
        error = subprocess.CalledProcessError(
            code,
            command,
            output=log.read_text(encoding="utf-8"),
        )
        error.add_note(f"Log: {log}")
        raise error


def _report_process_failure(error: subprocess.CalledProcessError) -> None:
    """Replay captured tool diagnostics and identify retained campaign logs."""
    for output in (error.stdout, error.stderr):
        if output:
            sys.stderr.write(output)
    for note in getattr(error, "__notes__", ()):
        sys.stderr.write(note + "\n")


@dataclass(frozen=True)
class TestEnvironment:
    """The interpreter and external tools resolved from a target package."""

    python: str
    path: str


def _build_test_environment(root: Path, name: str, workspace: Path) -> TestEnvironment:
    """Build the target's declared test environment through its flake output."""
    log = workspace / "environment.log"
    _run_test_command(
        [
            "nix",
            "build",
            "--no-update-lock-file",
            "--no-link",
            "--print-out-paths",
            _flake_installable(root, f"{name}-test-environment"),
        ],
        workspace,
        log,
    )
    paths = [
        line
        for line in log.read_text(encoding="utf-8").splitlines()
        if Path(line).is_absolute() and Path(line).is_file()
    ]
    if len(paths) != 1:
        message = f"could not resolve target environment; see {log}"
        raise CommandError(message)
    environment = json.loads(Path(paths[0]).read_text(encoding="utf-8"))
    return TestEnvironment(str(environment["python"]), str(environment["path"]))


@dataclass(frozen=True)
class TestSelection:
    """Select pytest cases and a subset of source mutations."""

    keywords: str = ""
    markers: str = ""
    lines: tuple[str, ...] = ()
    exclude_operators: tuple[str, ...] = ()
    max_mutations: int | None = None
    max_examples: int | None = None

    def pytest_arguments(self) -> list[str]:
        """Render selectors without passing them through a shell."""
        arguments = []
        for option, value in (("-k", self.keywords), ("-m", self.markers)):
            if value:
                arguments.extend([option, value])
        return arguments


COVERAGE_RUNTIME_START = None


def start_subprocess_coverage() -> None:
    """Start coverage in child interpreters and inherit the active test context."""
    import os  # noqa: PLC0415

    import coverage  # noqa: PLC0415

    tracer = coverage.Coverage.current() or coverage.process_startup()
    if tracer is not None:
        tracer.switch_context(os.environ.get("AFAIRESI_TEST_CONTEXT", ""))


def remap_coverage_paths(installed_path: str) -> None:
    """Combine installed-module coverage with the corresponding source file."""
    import os  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    import coverage  # noqa: PLC0415

    data = coverage.CoverageData()
    data.read()
    mapped = coverage.CoverageData(basename=".coverage-mapped")
    mapped.update(
        data,
        map_path=lambda path: (
            os.environ["src"] + "/main.py"  # noqa: SIM112 - Nix build environment.
            if path == installed_path
            else path
        ),
    )
    mapped.write()
    Path(mapped.data_filename()).replace(data.data_filename())


def _coverage_context_plugin() -> object:
    """Create a pytest hook that carries the current test into subprocesses."""
    import os  # noqa: PLC0415

    import coverage  # noqa: PLC0415
    import pytest  # noqa: PLC0415

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_protocol(item: pytest.Item) -> Generator[None, object, object]:
        """Attribute test setup, execution, teardown, and children to their test."""
        previous = os.environ.get("AFAIRESI_TEST_CONTEXT", "")
        os.environ["AFAIRESI_TEST_CONTEXT"] = item.nodeid
        tracer = coverage.Coverage.current()
        if tracer is not None:
            tracer.switch_context(item.nodeid)
        try:
            return (yield)
        finally:
            os.environ["AFAIRESI_TEST_CONTEXT"] = previous
            if tracer is not None:
                tracer.switch_context(previous)

    return pytest_runtest_protocol


COVERAGE_RUNTIME_END = None


def _fuzz_plugin(max_examples: int) -> object:
    """Create the pytest fuzz plugin inside the target's test environment."""
    import pytest  # noqa: PLC0415

    class FuzzTests:
        """Apply a small default budget and collect test_fuzz_* cases only."""

        def __init__(self, max_examples: int) -> None:
            """Set the example budget used when tests do not override settings."""
            self.max_examples = max_examples

        @pytest.hookimpl(tryfirst=True)
        def pytest_load_initial_conftests(self) -> None:
            """Load defaults before package plugins and test modules are imported."""
            from hypothesis import settings  # noqa: PLC0415

            settings.register_profile(
                "afairesi-fuzz",
                max_examples=self.max_examples,
                print_blob=True,
            )
            settings.load_profile("afairesi-fuzz")

        @pytest.hookimpl(trylast=True)
        def pytest_collection_modifyitems(
            self,
            config: pytest.Config,
            items: list[pytest.Item],
        ) -> None:
            """Keep selected fuzz targets and reject accidental ordinary tests."""
            from hypothesis import is_hypothesis_test  # noqa: PLC0415

            selected = []
            deselected = []
            for item in items:
                if not getattr(item, "originalname", item.name).startswith(
                    "test_fuzz_",
                ):
                    deselected.append(item)
                    continue
                if not is_hypothesis_test(getattr(item, "obj", None)):
                    message = f"{item.nodeid}: fuzz tests require @given"
                    raise pytest.UsageError(message)
                selected.append(item)
            items[:] = selected
            config.hook.pytest_deselected(items=deselected)

    return FuzzTests(max_examples)


def _prepare_package_tests(
    workspace: Path,
    name: str,
    environment: TestEnvironment,
    *,
    selection: TestSelection | None = None,
    performance: bool = False,
) -> list[str]:
    """Run pytest and package executables against the same isolated source copy."""
    selection = selection or TestSelection()
    launcher = workspace / "bin" / name
    launcher.parent.mkdir()
    launcher.write_text(
        "#!/bin/sh\nexec "
        + shlex.join([environment.python, str(workspace / "package-entry.py")])
        + ' "$@"\n',
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    (workspace / "package-entry.py").write_text(
        "import importlib, sys\n"
        f"sys.argv[0] = {str(launcher)!r}\n"
        f"importlib.import_module({name + '.main'!r}).main()\n",
        encoding="utf-8",
    )
    pytest_arguments = selection.pytest_arguments()
    if performance:
        pytest_arguments.extend(["--durations=20", "--durations-min=0"])
    plugins = ""
    if selection.max_examples is not None:
        (workspace / "fuzz_support.py").write_text(
            "from __future__ import annotations\n" + inspect.getsource(_fuzz_plugin),
            encoding="utf-8",
        )
        plugins = (
            "from fuzz_support import _fuzz_plugin\n"
            f"fuzz = _fuzz_plugin({selection.max_examples})\n"
        )
    pytest_arguments.extend(
        ["--import-mode=importlib", "-q", f"{name}/test_main.py"],
    )
    bootstrap = workspace / "run-tests.py"
    bootstrap.write_text(
        "import os, sys\n"
        "from pathlib import Path\n"
        "from tempfile import TemporaryDirectory\n"
        f"os.environ['PACKAGE_E2E_EXECUTABLE'] = {str(launcher)!r}\n"
        f"tools = {str(launcher.parent) + os.pathsep + environment.path!r}\n"
        "os.environ['PATH'] = tools + os.pathsep + os.environ.get('PATH', '')\n"
        "os.environ['PYTHONDONTWRITEBYTECODE'] = '1'\n"
        f"os.environ['GIT_CEILING_DIRECTORIES'] = {str(workspace.parent)!r}\n"
        f"os.environ['PYTHONPATH'] = {str(workspace)!r}\n"
        "os.environ['HYPOTHESIS_STORAGE_DIRECTORY'] = "
        f"{str(workspace / 'hypothesis')!r}\n"
        "runtime = TemporaryDirectory(prefix='test-runtime-', "
        f"dir={str(workspace)!r})\n"
        "directories = {'HOME': 'home', 'XDG_CACHE_HOME': 'cache',\n"
        "    'XDG_CONFIG_HOME': 'config', 'XDG_DATA_HOME': 'data',\n"
        "    'XDG_STATE_HOME': 'state', 'XDG_RUNTIME_DIR': 'run', 'TMPDIR': 'tmp'}\n"
        "for variable, name in directories.items():\n"
        "    directory = Path(runtime.name) / name\n"
        "    directory.mkdir(mode=0o700)\n"
        "    os.environ[variable] = str(directory)\n"
        "pid = Path('active-test-pgid')\n"
        "pid.write_text(str(os.getpgrp()))\n"
        "try:\n    import pytest\n"
        + "".join("    " + line + "\n" for line in plugins.splitlines())
        + f"    code = pytest.main({pytest_arguments!r} + sys.argv[1:]"
        + (", plugins=[fuzz]" if plugins else "")
        + ")\n"
        + (
            "    if code == pytest.ExitCode.NO_TESTS_COLLECTED:\n"
            "        print('No fuzz tests selected; skipping.')\n"
            "        code = 0\n"
            if plugins
            else ""
        )
        + "    sys.exit(code)\n"
        + "finally:\n"
        "    pid.unlink(missing_ok=True)\n"
        "    runtime.cleanup()\n",
        encoding="utf-8",
    )
    if performance:
        return _prepare_test_profiling(workspace, environment, launcher, bootstrap)
    return [environment.python, "-B", str(bootstrap)]


def _prepare_test_profiling(
    workspace: Path,
    environment: TestEnvironment,
    launcher: Path,
    bootstrap: Path,
) -> list[str]:
    """Profile pytest and the isolated package launcher in separate processes."""
    reports = workspace / "profiles"
    reports.mkdir()
    wrapper = workspace / "profile-script.py"
    wrapper.write_text(
        "import cProfile, os, runpy, sys\n"
        "from pathlib import Path\n"
        "sys.argv.pop(0)\n"
        f"reports = Path({str(reports)!r})\n"
        "report = reports / (Path(sys.argv[0]).stem + '-' + str(os.getpid()))\n"
        "profiler = cProfile.Profile()\n"
        "try:\n"
        "    profiler.runcall(runpy.run_path, sys.argv[0], run_name='__main__')\n"
        "finally:\n"
        "    profiler.dump_stats(str(report.with_suffix('.prof')))\n",
        encoding="utf-8",
    )
    launcher.write_text(
        "#!/bin/sh\nexec "
        + shlex.join(
            [
                environment.python,
                "-B",
                str(wrapper),
                str(workspace / "package-entry.py"),
            ],
        )
        + ' "$@"\n',
        encoding="utf-8",
    )
    sys.stdout.write(f"Function profiles (.prof): {reports}\n")
    sys.stdout.flush()
    return [environment.python, "-B", str(wrapper), str(bootstrap)]


def _limit_mutations(workspace: Path, limit: int) -> None:
    """Apply the requested run budget after native filtering."""
    from cosmic_ray.work_db import WorkDB, use_db  # noqa: PLC0415
    from cosmic_ray.work_item import WorkerOutcome, WorkResult  # noqa: PLC0415

    with use_db(workspace / "session.sqlite", WorkDB.Mode.open) as database:
        pending = database.pending_work_items
        database.set_multiple_results(
            [item.job_id for item in pending[limit:]],
            WorkResult(worker_outcome=WorkerOutcome.SKIPPED, output="run budget"),
        )
        sys.stdout.write(
            f"Selected {min(limit, len(pending))} of {len(pending)} mutations.\n",
        )


def _mutation_run_succeeded(workspace: Path) -> bool:
    """Preserve engine failures without imposing a mutation score threshold."""
    from cosmic_ray.work_db import WorkDB, use_db  # noqa: PLC0415
    from cosmic_ray.work_item import TestOutcome, WorkerOutcome  # noqa: PLC0415

    with use_db(workspace / "session.sqlite", WorkDB.Mode.open) as database:
        if database.num_results != database.num_work_items:
            return False
        return all(
            result.worker_outcome == WorkerOutcome.SKIPPED
            or (
                result.worker_outcome == WorkerOutcome.NORMAL
                and result.test_outcome != TestOutcome.INCOMPETENT
            )
            for _, result in database.results
        )


def _run_mutation_campaign(
    workspace: Path,
    name: str,
    environment: TestEnvironment,
    timeout: float,
    selection: TestSelection,
) -> bool:
    """Baseline, mutate, and report one copied package."""
    command = _prepare_package_tests(
        workspace,
        name,
        environment,
        selection=selection,
    )
    sys.stdout.write("Running baseline tests...\n")
    sys.stdout.flush()
    _run_test_command(
        [*command, "--junitxml=baseline.xml"],
        workspace,
        workspace / "baseline.log",
        timeout=timeout,
    )
    baseline = ET.parse(workspace / "baseline.xml").getroot()  # noqa: S314 - Locally generated pytest output.
    if not any(
        not any(child.tag in {"failure", "error", "skipped"} for child in case)
        for case in baseline.iter("testcase")
    ):
        message = "selected baseline has no passing tests; see baseline.xml"
        raise CommandError(message)
    module_path = f"{name}/main.py"
    filters = ""
    if selection.lines:
        filters += (
            "\n[cosmic-ray.filters.line-filter.lines]\n"
            f"{json.dumps(module_path)} = {json.dumps(selection.lines)}\n"
        )
    if selection.exclude_operators:
        filters += (
            "\n[cosmic-ray.filters.operators-filter]\n"
            f"exclude-operators = {json.dumps(selection.exclude_operators)}\n"
        )
    config = workspace / "cosmic-ray.toml"
    config.write_text(
        "[cosmic-ray]\n"
        f"module-path = {json.dumps(module_path)}\n"
        f"timeout = {timeout}\n"
        "excluded-modules = []\n"
        f"test-command = {json.dumps(shlex.join(command))}\n"
        '[cosmic-ray.distributor]\nname = "local"\n' + filters,
        encoding="utf-8",
    )
    engine = ["cosmic-ray"]
    session = str(workspace / "session.sqlite")
    _run_test_command(
        [*engine, "init", str(config), session],
        workspace,
        workspace / "init.log",
    )
    if selection.lines:
        _run_test_command(
            ["cr-filter-lines", "--config", str(config), session],
            workspace,
            workspace / "line-filter.log",
        )
    if selection.exclude_operators:
        _run_test_command(
            ["cr-filter-operators", session, str(config)],
            workspace,
            workspace / "operator-filter.log",
        )
    if selection.max_mutations is not None:
        _limit_mutations(workspace, selection.max_mutations)
    sys.stdout.write("Running mutations...\n")
    sys.stdout.flush()
    _run_test_command(
        [*engine, "exec", str(config), session],
        workspace,
        workspace / "engine.log",
    )
    _run_test_command(
        ["cr-html", session],
        workspace,
        workspace / "report.html",
    )
    _run_test_command(
        ["cr-report", session],
        workspace,
        workspace / "report.log",
    )
    sys.stdout.write((workspace / "report.log").read_text(encoding="utf-8"))
    return _mutation_run_succeeded(workspace)


def _run_test_package(
    package: Path,
    command: str,
    timeout: float | None,
    selection: TestSelection,
) -> bool:
    """Run one isolated package and retain its logs and reports."""
    root = _test_target_root(package)
    tmp = root / "tmp"
    tmp.mkdir(exist_ok=True)
    workspace = Path(
        tempfile.mkdtemp(prefix=f"python-{command}-{package.name}-", dir=tmp),
    )
    label = {
        "mutation": "Mutation workspace and reports",
        "property": "Property test workspace and logs",
        "fuzz": "Fuzz test workspace and logs",
        "performance": "Performance workspace and reports",
    }[command]
    sys.stdout.write(f"{label}: {workspace}\n")
    sys.stdout.flush()
    _copy_test_sources(root, workspace)
    environment = _build_test_environment(root, package.name, workspace)
    if command == "mutation":
        if timeout is None:
            message = "mutation tests require a timeout"
            raise CommandError(message)
        return _run_mutation_campaign(
            workspace,
            package.name,
            environment,
            timeout,
            selection,
        )
    arguments = _prepare_package_tests(
        workspace,
        package.name,
        environment,
        selection=selection,
        performance=command == "performance",
    )
    log = workspace / "tests.log"
    try:
        _run_test_command(arguments, workspace, log, timeout=timeout)
    except (KeyboardInterrupt, subprocess.TimeoutExpired):
        if log.exists():
            sys.stdout.write(log.read_text(encoding="utf-8"))
        raise
    sys.stdout.write(log.read_text(encoding="utf-8"))
    return True


def _python_test_packages(root: Path) -> list[Path]:
    """Find Python campaign targets or explain why none are available."""
    packages = [
        package.root for package in detect_packages(root) if package.kind == "python"
    ]
    if not packages:
        message = f"no Python packages found under {root}"
        raise CommandError(message)
    return packages


def _report_test_outcomes(outcomes: dict[str, str]) -> None:
    """Print consistent package outcomes and campaign totals."""
    sys.stdout.write("\nRepository summary:\n")
    for package_name, status in outcomes.items():
        sys.stdout.write(f"  {package_name}: {status}\n")
    sys.stdout.write(
        ", ".join(
            f"{sum(value == status for value in outcomes.values())} {status}"
            for status in ("passed", "failed", "skipped")
        )
        + "\n",
    )


def _run_test_repository(
    root: Path,
    command: str,
    timeout: float | None,
    selection: TestSelection,
) -> bool:
    """Run each Python package, continuing after failures and summarizing results."""
    packages = _python_test_packages(root)
    outcomes: dict[str, str] = {}
    for package in packages:
        if not (package / "test_main.py").is_file():
            outcomes[package.name] = "skipped"
            sys.stdout.write(f"Skipping {package.name}: no test_main.py\n")
            continue
        sys.stdout.write(f"Running {package.name}...\n")
        sys.stdout.flush()
        try:
            outcomes[package.name] = (
                "passed"
                if _run_test_package(package, command, timeout, selection)
                else "failed"
            )
        except (
            CommandError,
            OSError,
            subprocess.TimeoutExpired,
            subprocess.CalledProcessError,
        ) as error:
            outcomes[package.name] = "failed"
            sys.stderr.write(
                f"afairesi repo test {command}: {package.name}: {error}\n",
            )
            if isinstance(error, subprocess.CalledProcessError):
                _report_process_failure(error)
    _report_test_outcomes(outcomes)
    return "failed" not in outcomes.values()


def _dispatch_test_runner(
    options: argparse.Namespace,
    cli: argparse.ArgumentParser,
    target: Path,
) -> bool:
    """Validate budgets and run an explicitly requested test campaign."""
    if options.timeout is not None and (
        not math.isfinite(options.timeout) or options.timeout <= 0
    ):
        cli.error("--timeout must be positive and finite")
    mutation_limit = getattr(options, "max_mutations", None)
    if mutation_limit is not None and mutation_limit <= 0:
        cli.error("--max-mutations must be positive")
    example_limit = getattr(options, "max_examples", None)
    if example_limit is not None and example_limit <= 0:
        cli.error("--max-examples must be positive")
    selection = TestSelection(
        keywords=options.keywords,
        markers=options.markers,
        lines=tuple(getattr(options, "lines", ())),
        exclude_operators=tuple(getattr(options, "exclude_operators", ())),
        max_mutations=mutation_limit,
        max_examples=example_limit,
    )
    runner = (
        _run_test_repository if (target / "flake.nix").is_file() else _run_test_package
    )
    return runner(
        target,
        options.test_command,
        options.timeout,
        selection,
    )


def _flake_installable(root: Path, attribute: str) -> str:
    """Select a named output while retaining Git's tracked-source filtering."""
    return f"git+{root.as_uri()}#{attribute}"


def _build_package_coverage(package: Path) -> None:
    """Build an instrumented variant of the test check and print its report path."""
    root = _test_target_root(package)
    sys.stdout.write(f"Building coverage for {package.name}...\n")
    sys.stdout.flush()
    completed = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "nix",
            "build",
            "--no-link",
            "--no-update-lock-file",
            "--print-out-paths",
            _flake_installable(root, f"{package.name}-coverage"),
        ],
        stdout=subprocess.PIPE,
        text=True,
        check=True,
    )
    outputs = completed.stdout.splitlines()
    if len(outputs) != 1:
        message = f"expected one coverage output for {package.name}"
        raise CommandError(message)
    report = Path(outputs[0]) / "html" / "index.html"
    if not report.is_file():
        message = (
            f"coverage check produced no HTML report for {package.name}; "
            "check the package test suite and coverage output"
        )
        raise CommandError(message)
    sys.stdout.write(f"{package.name}: {report}\n")
    data = report.parents[1] / "coverage.json"
    if data.is_file():
        coverage = json.loads(data.read_text(encoding="utf-8"))
        totals = coverage["totals"]
        sys.stdout.write(
            f"  {totals['percent_covered']:.1f}% coverage; "
            f"{totals['missing_lines']} missing lines, "
            f"{totals['missing_branches']} missing branches\n",
        )
        for filename, measured in sorted(coverage["files"].items()):
            if measured["missing_branches"]:
                branches = ", ".join(
                    f"{start} -> {end if end > 0 else 'exit'}"
                    for start, end in measured["missing_branches"]
                )
                sys.stdout.write(f"  {filename}: missing branches {branches}\n")


def _run_coverage(target: Path) -> bool:
    """Build package checks independently, preserving caching and reporting failures."""
    repository = (target / "flake.nix").is_file()
    if repository:
        packages = _python_test_packages(target)
    else:
        _test_target_root(target)
        packages = [target]
    outcomes: dict[str, str] = {}
    for package in packages:
        try:
            if repository and (
                not (package / "test_main.py").is_file()
                or not has_python_tests(package / "test_main.py")
            ):
                outcomes[package.name] = "skipped"
                sys.stdout.write(f"Skipping {package.name}: no Python tests\n")
                continue
            _build_package_coverage(package)
            outcomes[package.name] = "passed"
        except (CommandError, OSError, subprocess.CalledProcessError) as error:
            if not repository and isinstance(error, subprocess.CalledProcessError):
                raise
            outcomes[package.name] = "failed"
            sys.stderr.write(f"afairesi repo test coverage: {package.name}: {error}\n")
            if isinstance(error, subprocess.CalledProcessError):
                _report_process_failure(error)
    if repository:
        _report_test_outcomes(outcomes)
    return "failed" not in outcomes.values()


def _configure_test_target(result: argparse.ArgumentParser) -> None:
    """Declare the shared package-or-repository campaign target."""
    result.add_argument(
        "target",
        type=Path,
        nargs="?",
        default=None,
        metavar="PATH",
        help=(
            "package or flake (default: current flake); "
            "relative to Git root, or cwd outside Git"
        ),
    )


def _configure_test_selection(result: argparse.ArgumentParser) -> None:
    """Declare pytest selectors shared by source test campaigns."""
    result.add_argument(
        "-k",
        dest="keywords",
        default="",
        help="pytest keyword expression",
    )
    result.add_argument(
        "-m",
        dest="markers",
        default="",
        help="pytest marker expression",
    )


def _configure_test_timeout(result: argparse.ArgumentParser) -> None:
    """Declare the optional time budget for a source test campaign."""
    result.add_argument(
        "--timeout",
        type=float,
        help="seconds per test run, excluding build (default: unlimited)",
    )


def _configure_test_parser(result: argparse.ArgumentParser) -> None:
    """Construct the test campaign command-line parser."""
    result.set_defaults(test_command=None, target=None, test_parser=result)
    commands = result.add_subparsers(
        dest="test_command",
        title="commands",
        metavar="COMMAND",
    )
    coverage = commands.add_parser(
        "coverage",
        help="measure coverage and print report paths",
        description=(
            "Measure coverage from the package test suite; skip packages without "
            "tests. Print HTML report paths in the Nix store."
        ),
    )
    _configure_test_target(coverage)
    fuzz = commands.add_parser(
        "fuzz",
        help="run a short generation-based fuzz campaign",
        description=(
            "Run Hypothesis tests named test_fuzz_* in isolated package copies. "
            "Skip packages without test_main.py or selected fuzz tests. "
            "Logs and Hypothesis examples: flake tmp/."
        ),
    )
    _configure_test_target(fuzz)
    _configure_test_selection(fuzz)
    _configure_test_timeout(fuzz)
    fuzz.add_argument(
        "--max-examples",
        type=int,
        default=10,
        metavar="N",
        help=(
            "generated examples per fuzz test (default: 10); "
            "explicit test settings and Hypothesis profiles can override this"
        ),
    )

    mutation = commands.add_parser(
        "mutation",
        help="run mutation tests",
        description=(
            "Run mutation tests in isolated package copies. "
            "Skip packages without test_main.py. Logs and reports: flake tmp/."
        ),
    )
    _configure_test_target(mutation)
    _configure_test_selection(mutation)
    mutation.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        help=("seconds per test run, excluding build (default: 300)"),
    )

    mutation.add_argument(
        "--lines",
        action="append",
        default=[],
        metavar="N_OR_START-END",
        help="native line ranges; include overlapping mutations (repeatable)",
    )
    mutation.add_argument(
        "--exclude-operator",
        dest="exclude_operators",
        action="append",
        default=[],
        metavar="REGEX",
        help="native operator exclusion regex, matched from the start (repeatable)",
    )
    mutation.add_argument(
        "--max-mutations",
        type=int,
        metavar="N",
        help="maximum mutations to test",
    )
    performance = commands.add_parser(
        "performance",
        help="profile existing tests and package CLI calls",
        description=(
            "Profile tests with cProfile and show slow test phases; skip packages "
            "without tests. Save separate pytest and package CLI profiles in flake "
            "tmp/. Timings include profiler overhead. Profiles cover "
            "each process's main Python thread; external executables and arbitrary "
            "Python subprocesses are not profiled."
        ),
    )
    _configure_test_target(performance)
    _configure_test_selection(performance)
    _configure_test_timeout(performance)
    property_tests = commands.add_parser(
        "property",
        help="run property-based tests",
        description=(
            "Run property-based tests in isolated package copies. "
            "Skip packages without test_main.py. Logs and reports: flake tmp/. "
            'Select profiles with PYTEST_ADDOPTS="--hypothesis-profile=NAME".'
        ),
    )
    _configure_test_target(property_tests)
    _configure_test_selection(property_tests)
    _configure_test_timeout(property_tests)


def _dispatch_test_command(
    options: argparse.Namespace,
    cli: argparse.ArgumentParser,
) -> None:
    """Show campaign help or run one explicitly selected campaign."""
    if options.test_command is None:
        options.test_parser.print_help()
        return
    target = _command_target(options.target)
    try:
        success = (
            _run_coverage(target)
            if options.test_command == "coverage"
            else _dispatch_test_runner(options, cli, target)
        )
    except (CommandError, OSError, subprocess.TimeoutExpired) as error:
        sys.stderr.write(
            f"afairesi repo test {options.test_command}: {target}: {error}\n",
        )
        sys.exit(1)
    sys.exit(0 if success else 1)


@contextlib.contextmanager
def inspection_remote_repository(remote: str) -> Iterator[Path]:
    """Inspect a native Git clone without changing existing repositories."""
    with tempfile.TemporaryDirectory(prefix="afairesi-inspect-") as temporary:
        checkout = Path(temporary) / "repository"
        run(["git", "clone", "--", remote, str(checkout)], capture_output=False)
        yield checkout


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


def _command_node(tree: dict[str, Any], path: tuple[str, ...]) -> dict[str, Any]:
    """Find or create the command node at a resolved path."""
    node = tree
    for name in path:
        node = node["commands"].setdefault(name, {"arguments": [], "commands": {}})
    return node


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
        if isinstance(node, ast.ClassDef):
            return
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

    def _resolve(
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
        return self._resolve_call(scope, node, seen)

    def _resolve_call(
        self,
        scope: str,
        node: ast.Call,
        seen: frozenset[tuple[str, int]],
    ) -> set[tuple[str, ...]]:
        if self._qualified(node.func) == "argparse.ArgumentParser":
            return {(f"{node.lineno}:{node.col_offset}",)}
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

    def _public_roots(self) -> set[str]:
        """Select parsers consumed by the public entry point, when identifiable."""
        scopes = {"", "main"}
        while True:
            reachable = scopes | {
                call.func.id
                for scope, call in self.calls
                if scope in scopes
                and isinstance(call.func, ast.Name)
                and call.func.id in self.functions
            }
            if reachable == scopes:
                break
            scopes = reachable
        return {
            path[0]
            for scope, call in self.calls
            if scope in scopes
            and isinstance(call.func, ast.Attribute)
            and call.func.attr in {"parse_args", "parse_known_args"}
            for path in self._resolve(scope, call.func.value)
        }

    def report(self) -> dict[str, Any]:
        """Return a command tree with declared arguments and unresolved warnings."""
        result: dict[str, Any] = {
            "status": "argparse" if self.imports else "unknown",
            "arguments": [],
            "commands": {},
            "warnings": [],
        }

        warnings = []
        roots = self._public_roots()
        for scope, call in self.calls:
            if not isinstance(call.func, ast.Attribute):
                continue
            if call.func.attr == "add_parser":
                paths = {
                    path[1:]
                    for path in self._resolve(scope, call)
                    if not roots or path[0] in roots
                }
                for path in sorted(paths):
                    _command_node(result, path)
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
            public_contexts = {
                context[1:] for context in contexts if not roots or context[0] in roots
            }
            for context in sorted(public_contexts):
                _command_node(result, context)["arguments"].append(
                    {"names": names, "options": options},
                )
        result["warnings"] = warnings
        return result


def inspect_repository(root: Path) -> dict[str, Any]:
    """Describe root resources by name, with their interfaces and test identifiers."""
    if not (root / "flake.nix").is_file():
        message = f"{root}: expected an Afairesi repository with flake.nix"
        raise CommandError(message)
    host_names = {
        host.name
        for host in root.iterdir()
        if host.is_dir()
        and not host.is_symlink()
        and (host / "configuration.nix").is_file()
    }
    resources: dict[str, Any] = {name: {"type": "host"} for name in host_names}
    for package in detect_packages(root):
        module = _source(package.root / "main.py") if package.kind == "python" else None
        cli = (
            ArgumentDiscovery(module).report()
            if module is not None
            else {
                "status": "not applicable",
                "arguments": [],
                "commands": {},
                "warnings": [],
            }
        )
        tests = package.root / "test_main.py"
        test_module = _source(tests) if tests.is_file() else None
        description = package_description(package)
        resources[package.name] = {
            "type": ["package", "host"] if package.name in resources else "package",
            "kind": package.kind,
            "description": description,
            "cli": cli,
            "tests": (
                sorted(
                    f"{owner}::{definition.name}" if owner else definition.name
                    for owner, definition in python_test_definitions(test_module)
                )
                if test_module is not None
                else []
            ),
        }
    return dict(sorted(resources.items()))


def _dispatch_inspect(
    options: argparse.Namespace,
    cli: argparse.ArgumentParser,
) -> None:
    """Inspect local or remote sources without executing package code."""
    try:
        value = options.repository
        remote = value is not None and (
            "://" in value
            or (":" in value.split("/", 1)[0] and not Path(value).exists())
        )
        repository = (
            inspection_remote_repository(value)
            if remote
            else contextlib.nullcontext(
                resolve_repository_path(Path(value))
                if value is not None
                else repository_root(),
            )
        )
        with repository as root:
            report = inspect_repository(root)
    except CommandError as error:
        cli.exit(1, f"{cli.prog}: {error}\n")
    sys.stdout.write(json.dumps(report, indent=2) + "\n")


def _configure_repo_parser(result: argparse.ArgumentParser) -> None:
    """Construct the flake resource command-line parser."""
    commands = result.add_subparsers(
        dest="command",
        title="commands",
        metavar="COMMAND",
    )
    create = commands.add_parser(
        "add",
        usage="%(prog)s [-h] {NAME TYPE | HOST}",
        help="create and stage a package or host",
        description=(
            "Create and stage a canonical package or host. "
            "Python checks are generated from test_main.py. "
            "Host checks are generated from host configurations. "
            "Keep only necessary supporting files under prm/."
        ),
    )
    create.add_argument(
        "resource",
        metavar="RESOURCE",
        help="new root directory name",
    )
    create.add_argument(
        "type",
        nargs="?",
        metavar="TYPE",
        choices=PACKAGE_KINDS,
        help=f"required for packages, omitted for hosts ({', '.join(PACKAGE_KINDS)})",
    )
    fix = commands.add_parser(
        "fix",
        help="fix and stage flake structure; remove unsupported files",
        description=(
            "Fix and stage flake structure; delete paths outside the source whitelist. "
            "Keep only necessary supporting files under prm/. "
            f"{NIX_FMT_HELP}"
        ),
    )
    fix.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="preview structural changes without modifying files or staging",
    )
    commands.add_parser(
        "init",
        help="initialize and stage a flake in an empty Git worktree",
        description=(
            "Initialize and stage a flake in an empty Git worktree. "
            "Use Git to configure remotes, commit, and push."
        ),
    )
    inspection = commands.add_parser(
        "inspect",
        help="inspect interfaces and test identifiers",
        description=(
            "Describe hosts, packages, CLI arguments, and declared test identifiers. "
            "No package code or tests are executed."
        ),
    )
    inspection.add_argument(
        "repository",
        nargs="?",
        metavar="PATH_OR_REMOTE",
        help=(
            "repository path or Git URL (default: current repository); "
            "relative paths use the Git root, or cwd outside Git"
        ),
    )
    move = commands.add_parser(
        "mv",
        help="rename and stage a package or host",
        description=(
            "Rename and stage a canonical package or host. "
            "Update runtime paths and package references, move runtime data, "
            "and preserve unstaged edits."
        ),
    )
    move.add_argument(
        "source",
        metavar="SOURCE",
        help="existing root directory name",
    )
    move.add_argument(
        "destination",
        metavar="DESTINATION",
        help="new root directory name",
    )
    remove = commands.add_parser(
        "rm",
        help="remove and stage a package or host",
        description=(
            "Remove a tracked package or host and related check files with git rm. "
            "Untracked and ignored files remain."
        ),
    )
    remove.add_argument(
        "resource",
        metavar="RESOURCE",
        help="root directory name",
    )
    remove.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="pass --force to git rm",
    )
    testing = commands.add_parser(
        "test",
        help="run test campaigns and profile existing tests",
        description="Run a test campaign or profile tests. Choose a command below.",
    )
    _configure_test_parser(testing)


def home_submodules(
    root: Path,
    *,
    require_url: bool = True,
) -> list[dict[str, str]]:
    """Read submodule records using Git's configuration parser."""
    modules = root / ".gitmodules"
    if not modules.exists():
        return []
    read_regular(modules)
    completed = git(
        root,
        [
            "config",
            "get",
            "--file",
            str(modules),
            "--null",
            "--show-names",
            "--all",
            "--regexp",
            r"^submodule\..*\.(path|url)$",
        ],
        check=False,
    )
    if completed.returncode == 1 and not completed.stdout and not completed.stderr:
        return []
    if completed.returncode != 0:
        msg = f"could not read {modules}: {completed.stderr.strip()}"
        raise CommandError(msg)
    grouped: dict[str, dict[str, str]] = {}
    for record in completed.stdout.split("\0"):
        if not record:
            continue
        key, separator, value = record.partition("\n")
        match = re.fullmatch(r"submodule\.(.+)\.(path|url)", key)
        if not separator or match is None:
            msg = "malformed .gitmodules field"
            raise CommandError(msg)
        fields = grouped.setdefault(match.group(1), {})
        if match.group(2) in fields:
            msg = f'submodule "{match.group(1)}": duplicate {match.group(2)} field'
            raise CommandError(msg)
        fields[match.group(2)] = value
    repositories = []
    for name, fields in sorted(grouped.items()):
        required = {"path", "url"} if require_url else {"path"}
        if not required.issubset(fields):
            suffix = "path and one URL" if require_url else "path"
            msg = f'submodule "{name}": must have exactly one {suffix}'
            raise CommandError(
                msg,
            )
        repositories.append({"name": name, **fields})
    return repositories


def _home_ignore_source(root: Path) -> str:
    """Validate the home whitelist and include its required entries."""
    path = root / ".gitignore"
    required = ["!/.gitignore", "!/.gitmodules"]
    source = read_regular(path)
    if source is None:
        return "*\n" + "\n".join(required) + "\n"
    lines = source.splitlines()
    if (
        not lines
        or lines[0] != "*"
        or any(not line.startswith("!/") for line in lines[1:])
    ):
        msg = f"{path}: must start with * and subsequent lines must start with !/"
        raise CommandError(msg)
    missing = [line for line in required if line not in lines]
    return source.rstrip("\n") + "\n" + "\n".join(missing) + "\n" if missing else source


def _converge_home_ignore(root: Path, *, dry_run: bool) -> bool:
    """Converge the canonical home whitelist."""
    return write_managed(
        root,
        Path(".gitignore"),
        _home_ignore_source(root),
        dry_run=dry_run,
    )


def _allow_home_submodule(root: Path, relative: Path, *, dry_run: bool = False) -> bool:
    """Allow a submodule and its parents through the home whitelist."""
    changed = _converge_home_ignore(root, dry_run=dry_run)
    path = root / ".gitignore"
    source = read_regular(path) or "*\n!/.gitignore\n!/.gitmodules\n"
    patterns = [
        *(
            f"!/{parent.as_posix()}/"
            for parent in reversed(relative.parents)
            if parent != Path()
        ),
        f"!/{relative.as_posix()}",
    ]
    existing = set(source.splitlines())
    missing = [pattern for pattern in patterns if pattern not in existing]
    if missing:
        changed |= write_managed(
            root,
            Path(".gitignore"),
            source.rstrip("\n") + "\n" + "\n".join(missing) + "\n",
            dry_run=dry_run,
        )
    return changed


def _converge_home_submodule(
    root: Path,
    repository: dict[str, str],
    expected: Path,
    *,
    dry_run: bool,
) -> bool:
    """Converge one home submodule record and checkout."""
    actual = Path(repository["path"])
    if actual == expected or not (root / actual / ".git").exists():
        return False
    report_change(f"move '{actual}' to '{expected}'", dry_run=dry_run)
    arguments = ["mv"]
    if dry_run:
        arguments.append("--dry-run")
    else:
        (root / expected).parent.mkdir(parents=True, exist_ok=True)
    git(root, [*arguments, "--", str(actual), str(expected)])
    _move_home_whitelist(root, actual, expected, dry_run=dry_run)
    return True


def _move_home_whitelist(
    root: Path,
    actual: Path,
    expected: Path,
    *,
    dry_run: bool,
) -> bool:
    """Move only whitelist entries owned by a relocated submodule."""
    source = read_regular(root / ".gitignore") or ""
    old = f"!/{actual.as_posix()}"
    new = f"!/{expected.as_posix()}"
    lines = source.splitlines()
    retained = [
        line for line in lines if line != old and not line.startswith(old + "/")
    ]
    if retained == lines:
        return False
    for line in lines:
        if line == old or line.startswith(old + "/"):
            replacement = new + line[len(old) :]
            if replacement not in retained:
                retained.append(replacement)
    updated = "\n".join(retained) + "\n"
    if updated == source:
        return False
    return write_managed(root, Path(".gitignore"), updated, dry_run=dry_run)


def converge_home(root: Path, *, dry_run: bool) -> list[dict[str, str]]:
    """Converge a canonical home repository."""
    repositories = home_submodules(root)
    actual_paths = [Path(repository["path"]) for repository in repositories]
    expected_paths = [
        canonical_remote_path(repository["url"]) for repository in repositories
    ]
    if len(set(expected_paths)) != len(expected_paths):
        msg = "duplicate canonical repository path"
        raise CommandError(msg)
    if len(set(actual_paths)) != len(actual_paths):
        msg = "duplicate configured repository path"
        raise CommandError(msg)
    repair_paths = [
        expected if (root / actual / ".git").exists() else actual
        for actual, expected in zip(actual_paths, expected_paths, strict=True)
    ]
    if len(set(repair_paths)) != len(repair_paths):
        msg = "canonical repository path conflicts with a missing checkout registration"
        raise CommandError(msg)
    for actual, expected in zip(actual_paths, repair_paths, strict=True):
        if actual != expected and (root / expected).exists():
            msg = f"target already exists: {expected}"
            raise CommandError(msg)
    _home_ignore_source(root)
    for repository, expected in zip(repositories, repair_paths, strict=True):
        _converge_home_submodule(
            root,
            repository,
            expected,
            dry_run=dry_run,
        )
    _converge_home_ignore(root, dry_run=dry_run)
    for expected in repair_paths:
        _allow_home_submodule(root, expected, dry_run=dry_run)
    return repositories


def remove_home_submodule(
    root: Path,
    value: str,
    *,
    force: bool = False,
) -> None:
    """Remove a registered home submodule and its owned whitelist entries."""
    relative = Path(value)
    repositories = home_submodules(root, require_url=False)
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or relative.as_posix()
        not in {repository["path"] for repository in repositories}
    ):
        msg = f"not a registered home submodule: {value}"
        raise CommandError(msg)
    source = read_regular(root / ".gitignore") or ""
    entry = f"!/{relative.as_posix()}"
    retained = [
        line
        for line in source.splitlines()
        if line != entry and not line.startswith(entry + "/")
    ]
    updated = "\n".join(retained) + "\n" if retained else ""
    arguments = ["rm", "-r"]
    if force:
        arguments.append("--force")
    run(
        ["git", *arguments, "--", f":(literal){relative.as_posix()}"],
        cwd=root,
        capture_output=False,
    )
    write_managed(root, Path(".gitignore"), updated, dry_run=False)


def initialize_home() -> None:
    """Initialize and stage the canonical home policy without cleaning."""
    root = Path.home()
    if repository_type(root, "home") != "home":
        msg = "cannot initialize a flake repository as a home repository"
        raise CommandError(msg)
    _home_ignore_source(root)
    run(["git", "init", "--", str(root)], capture_output=False)
    _converge_home_ignore(root, dry_run=False)


def _require_home_repository() -> Path:
    """Validate home setup and explain how to initialize it."""
    home = Path.home()
    completed = git(
        home,
        ["rev-parse", "--path-format=absolute", "--show-toplevel"],
        check=False,
    )
    if completed.returncode != 0:
        msg = "$HOME must be a Git repository; run 'afairesi home init' first"
        raise CommandError(msg)
    if Path(completed.stdout.strip()) != home or repository_type(home, "git") != "home":
        msg = (
            "$HOME must be an initialized canonical home repository; "
            "run 'afairesi home init' first"
        )
        raise CommandError(msg)
    return home


def initialize_submodule(
    remote: str,
    *,
    force: bool = False,
) -> None:
    """Add a hosted repository at its canonical home-relative path."""
    relative = canonical_remote_path(remote)
    home = _require_home_repository()
    validate_managed_path(home, relative)
    directories = missing_directories(home, [relative.parent])
    arguments = ["git", "submodule", "add"]
    if force:
        arguments.append("--force")
    try:
        with preserve_whitelist_on_error(home):
            _allow_home_submodule(home, relative)
            run(
                [*arguments, "--", remote, relative.as_posix()],
                cwd=home,
                capture_output=False,
            )
    except BaseException:
        remove_empty_directories(directories)
        raise


def _configure_home_parser(result: argparse.ArgumentParser) -> None:
    """Construct the home repository command-line parser."""
    commands = result.add_subparsers(
        dest="command",
        title="commands",
        metavar="COMMAND",
    )
    add = commands.add_parser(
        "add",
        help="add a Git submodule at its canonical home path",
        description=(
            "Add a Git submodule at $HOME/<remote-hostname>/<repository-path> "
            "and stage its submodule registration."
        ),
    )
    add.add_argument("remote", metavar="REMOTE", help="existing hosted Git remote")
    add.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="pass --force to git submodule add, including reuse of retained Git data",
    )
    fix_home = commands.add_parser(
        "fix",
        help="fix and stage home structure",
        description=(
            "Fix and stage the $HOME whitelist and submodule layout. "
            "Move submodules to <remote-hostname>/<repository-path>."
        ),
    )
    fix_home.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="preview structural changes without modifying files or staging",
    )
    commands.add_parser(
        "init",
        help="initialize the home repository and stage its whitelist",
        description="Initialize the repository at $HOME and stage its whitelist.",
    )
    remove = commands.add_parser(
        "rm",
        help="remove and stage a home submodule unregistration",
        description=(
            "Remove a home submodule with git rm, update the home whitelist, "
            "and stage its unregistration."
        ),
    )
    remove.add_argument(
        "resource",
        metavar="RESOURCE",
        help="home-relative registered submodule path",
    )
    remove.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="pass --force to git rm",
    )


def _dispatch_home(options: argparse.Namespace) -> None:
    """Dispatch lifecycle commands against the home repository."""
    if options.command == "init":
        initialize_home()
    elif options.command == "add":
        initialize_submodule(options.remote, force=options.force)
    elif options.command == "fix":
        converge_home(_require_home_repository(), dry_run=options.dry_run)
    elif options.command == "rm":
        root = _require_home_repository()
        remove_home_submodule(
            root,
            options.resource,
            force=options.force,
        )


def parser() -> argparse.ArgumentParser:
    """Construct the workspace command-line parser."""
    result = argparse.ArgumentParser(
        prog="afairesi",
        description=(
            "Afairesi minimizes the persistent state needed to reconstruct "
            "a software system."
        ),
    )
    scopes = result.add_subparsers(dest="scope", title="commands", metavar="SCOPE")
    home = scopes.add_parser(
        "home",
        help="manage the home repository and its submodules",
        description=(
            "Manage $HOME submodules at <remote-hostname>/<repository-path>. "
            "Changes are staged; use Git to commit and push."
        ),
    )
    repo = scopes.add_parser(
        "repo",
        help="manage the current repository",
        description=(
            "Manage Nix flake package, host, and check conventions. "
            "Structural changes are staged for review."
        ),
    )
    _configure_repo_parser(repo)
    _configure_home_parser(home)
    result.set_defaults(command=None, scope_parser=result)
    repo.set_defaults(scope_parser=repo)
    home.set_defaults(scope_parser=home)
    return result


def _dispatch_create(root: Path, options: argparse.Namespace) -> None:
    """Create the selected package or host resource."""
    kind, name, _relative = _parse_resource_path(
        options.resource,
        kind="host" if options.type is None else "package",
    )
    if kind == "host":
        add_host(root, name)
        return
    add_package(root, options.type, name)


def _validate_resource_arguments(
    cli: argparse.ArgumentParser,
    options: argparse.Namespace,
) -> None:
    """Reject invalid resource arguments before discovering the repository."""
    if options.scope != "repo" or options.command not in {"add", "mv", "rm"}:
        return
    try:
        if options.command == "mv":
            _parse_resource_path(options.source)
            _parse_resource_path(options.destination)
        else:
            _parse_resource_path(
                options.resource,
                kind=("host" if options.type is None else "package")
                if options.command == "add"
                else None,
            )
    except CommandError as error:
        cli.error(str(error))


def _require_flake_repository(message: str, *, removing: Path | None = None) -> Path:
    """Select the current repository and require flake lifecycle commands."""
    root = repository_root()
    if repository_type(root) != "flake":
        raise CommandError(message)
    validate_flake_layout(root, removing=removing)
    return root


def formatter_main() -> None:
    """Repair a flake repository before formatting; called internally by nix fmt."""
    cli = argparse.ArgumentParser(description="Repair the flake formatting inputs.")
    cli.add_argument("-n", "--dry-run", action="store_true")
    cli.add_argument(
        "--source",
        type=Path,
        help="source path relative to the Git root, or cwd outside a repository",
    )
    options = cli.parse_args()
    try:
        if options.source is not None:
            validate_flake_source(resolve_repository_path(options.source))
            return
        root = _require_flake_repository(
            "nix fmt requires a flake repository; run afairesi home fix for home",
        )
        converge_flake(root, dry_run=options.dry_run)
    except (
        CommandError,
        OSError,
        SyntaxError,
        UnicodeError,
        ValueError,
        nix_syntax.NixSyntaxError,
    ) as error:
        sys.stderr.write((f"error: {error}") + "\n")
        raise SystemExit(1) from error


def _dispatch_workspace_command(
    options: argparse.Namespace,
    cli: argparse.ArgumentParser,
) -> None:
    """Run the selected workspace operation with command-specific validation."""
    if options.command is None:
        options.scope_parser.print_help()
        return
    if options.scope == "home":
        _dispatch_home(options)
        return
    if options.command == "init":
        initialize_repository()
        return
    if options.command == "inspect":
        _dispatch_inspect(options, cli)
        return
    if options.command == "test":
        _dispatch_test_command(options, cli)
        return
    root = _require_flake_repository(
        "afairesi repo commands require a flake repository; use afairesi home for home",
        removing=Path(options.resource) if options.command == "rm" else None,
    )
    if options.command == "add":
        _dispatch_create(root, options)
    elif options.command == "rm":
        remove_resource(
            root,
            options.resource,
            force=options.force,
        )
    elif options.command == "mv":
        rename_resource(
            root,
            options.source,
            options.destination,
        )
    elif options.command == "fix":
        converge_flake(root, dry_run=options.dry_run, fail_on_changes=False)


def main() -> None:
    """Dispatch repository and home commands."""
    cli = parser()
    options = cli.parse_args()
    _validate_resource_arguments(cli, options)
    try:
        _dispatch_workspace_command(options, cli)
    except subprocess.CalledProcessError as error:
        _report_process_failure(error)
        raise SystemExit(
            error.returncode if error.returncode >= 0 else 128 - error.returncode,
        ) from error
    except KeyboardInterrupt:
        sys.exit(130)
    except (
        CommandError,
        subprocess.TimeoutExpired,
        OSError,
        SyntaxError,
        UnicodeError,
        ValueError,
        nix_syntax.NixSyntaxError,
    ) as error:
        sys.stderr.write((f"error: {error}") + "\n")
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()

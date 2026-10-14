#!/usr/bin/env python3
# Copyright (c) 2026- Paschalis Bizopoulos
"""Afairesi manages repository structure for Nix flakes through enforced conventions."""

from __future__ import annotations

import argparse
import ast
import contextlib
import os
import re
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

import nix_syntax

if TYPE_CHECKING:
    from collections.abc import Iterator

    import libcst as cst
    from tree_sitter import Node
PACKAGE_KINDS = ("html", "latex", "nix", "python")
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
    "README",
    "flake.lock",
    "flake.nix",
    "formatter.nix",
}
PRM_NAME = "prm"
TMP_NAME = "tmp"
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
class Package:  # noqa: D101
    name: str
    kind: str
    root: Path


def run(
    arguments: list[str],
    cwd: Path | None = None,
    *,
    check: bool = True,
    input_source: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a process and preserve failures and captured output."""
    completed = subprocess.run(  # noqa: S603
        arguments,
        cwd=cwd,
        capture_output=True,
        check=False,
        input=input_source,
        text=True,
    )
    if check and completed.returncode != 0:
        if completed.stdout:
            print(completed.stdout, end="")  # noqa: T201
        if completed.stderr:
            print(completed.stderr, end="", file=sys.stderr)  # noqa: T201
        raise SystemExit(completed.returncode)
    return completed


def git(
    root: Path,
    arguments: list[str],
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run Git in a selected repository."""
    return run(
        ["git", "-C", str(root), *arguments],
        check=check,
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
        "'afairesi home init-repo REMOTE'"
    )
    raise CommandError(msg)


def _repository_type_markers(root: Path) -> tuple[bool, bool]:
    """Recognize home and flake layouts for lifecycle commands."""
    flake = any(
        (root / marker).exists()
        for marker in ("flake.nix", "flake.lock", "packages", "checks", "hosts")
    )
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
    print(("would " if dry_run else "") + message)  # noqa: T201


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
    current = read_regular(path) if path.exists() and not path.is_symlink() else None
    current_mode = path.lstat().st_mode if path.exists() or path.is_symlink() else 0
    mode_matches = bool(current_mode & 0o111) == executable
    if current == source and mode_matches:
        return False
    report_change(f"write '{relative}'", dry_run=dry_run)
    if dry_run:
        return True
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        path.unlink()
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755 if executable else 0o644)
    git(root, ["add", "--", str(relative)])
    return True


def _tracked_paths(root: Path) -> set[Path]:
    """Return paths represented in the index."""
    completed = git(root, ["ls-files", "-z"])
    return {Path(item) for item in completed.stdout.split("\0") if item}


def _clean_arguments(*, dry_run: bool, exclusions: tuple[str, ...]) -> list[str]:
    """Build a native Git clean command with repository-specific exclusions."""
    arguments = ["clean", "-ndx" if dry_run else "-fdx"]
    for exclusion in exclusions:
        arguments.extend(("-e", exclusion))
    return arguments


def _flake_clean_arguments(*, dry_run: bool) -> list[str]:
    """Build the flake cleanup command."""
    return _clean_arguments(
        dry_run=dry_run,
        exclusions=(f"/{TMP_NAME}/", f"/packages/*/{TMP_NAME}/"),
    )


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
    if len(components) < 3 or any(  # noqa: PLR2004
        not re.fullmatch(r"[A-Za-z0-9._-]+", component) or component in {".", ".."}
        for component in components
    ):
        msg = "repository path components must contain only ASCII letters, digits, '.', '-', or '_'"  # noqa: E501
        raise CommandError(
            msg,
        )
    return Path(*components)


def _package_kind(name: str, markers: set[str]) -> str:
    """Classify regular package markers consistently across source snapshots."""
    matches = [kind for kind, marker in KIND_MARKERS.items() if marker in markers]
    if len(matches) > 1:
        msg = f"packages/{name}: has ambiguous project markers: {', '.join(matches)}"
        raise CommandError(msg)
    return matches[0] if matches else "nix"


def detect_packages(root: Path) -> list[Package]:
    """Detect supported packages from unambiguous marker files."""
    packages_root = root / "packages"
    if not packages_root.is_dir():
        return []
    result: list[Package] = []
    for package_root in sorted(
        path
        for path in packages_root.iterdir()
        if path.is_dir() and not path.is_symlink()
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
    return result


def validate_name(name: str) -> None:
    """Enforce package naming conventions."""
    if not re.fullmatch(r"[a-z0-9]+(?:_[a-z0-9]+)*|[a-z0-9]+(?:-[a-z0-9]+)*", name):
        msg = f"package name must use snake_case or dash-case: {name}"
        raise CommandError(msg)


def validate_host_name(name: str) -> None:
    """Enforce lower camelCase host names."""
    if not re.fullmatch(r"[a-z][A-Za-z0-9]*", name):
        msg = f"host name must use camelCase: {name}"
        raise CommandError(msg)


def package_files(package: Package) -> set[Path]:
    """Return permitted regular files for a package kind."""
    relative = Path("packages") / package.name
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


def canonical_checks(root: Path, packages: list[Package]) -> dict[Path, str]:
    """Return generated checks derived from the repository's resources."""
    checks = {
        Path("checks") / package.name / "default.nix": (_current_python_test_source())
        for package in packages
        if package.kind == "python"
        and (package.root / "test_main.py").is_file()
        and has_python_tests(package.root / "test_main.py")
    }
    hosts = root / "hosts"
    if hosts.is_dir():
        for host in sorted(hosts.iterdir()):
            check = Path("checks") / f"{host.name}VmWithDisko" / "default.nix"
            if (
                host.is_dir()
                and not host.is_symlink()
                and (host / "configuration.nix").is_file()
            ):
                validate_host_name(host.name)
                checks[check] = _current_host_check_source()
    return checks


def allowed_paths(root: Path, packages: list[Package]) -> set[Path]:
    """Compute the repository whitelist represented by .gitignore."""
    allowed = {Path(item) for item in ROOT_FILES if (root / item).exists()}
    allowed.update(canonical_checks(root, packages))
    hosts = root / "hosts"
    if hosts.is_dir():
        for host in hosts.iterdir():
            if host.is_dir() and (host / "configuration.nix").exists():
                allowed.add(Path("hosts") / host.name / "configuration.nix")
                hardware = Path("hosts") / host.name / "hardware-configuration.nix"
                if (root / hardware).exists():
                    allowed.add(hardware)
    for package in packages:
        allowed.update(
            path for path in package_files(package) if (root / path).exists()
        )
    return allowed


def prm_directories(root: Path) -> set[Path]:
    """Return existing prm directories whose contents are unrestricted."""
    candidates = {Path("prm")}
    for parent in ("hosts", "packages"):
        base = root / parent
        if base.is_dir():
            for child in base.iterdir():
                if child.is_dir():
                    candidates.add(Path(parent) / child.name / PRM_NAME)
    return {path for path in candidates if (root / path).is_dir()}


def tmp_directories(root: Path) -> set[Path]:
    """Return permitted untracked tmp trees."""
    candidates = {Path(TMP_NAME)}
    packages = root / "packages"
    if packages.is_dir():
        candidates.update(
            Path("packages") / child.name / TMP_NAME
            for child in packages.iterdir()
            if child.is_dir()
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


def _python_test_placement_issue(package: Package) -> str | None:
    """Reject embedded tests before convergence can remove their old checks."""
    source = package.root / "main.py"
    if package.kind == "python" and source.is_file() and has_python_tests(source):
        return f"packages/{package.name}/main.py: move test definitions to test_main.py"
    return None


def has_python_tests(path: Path) -> bool:
    """Detect static Python test definitions without executing the source."""
    try:
        module = ast.parse(path.read_bytes(), filename=str(path))
        case_classes = _unittest_classes(module)
        return any(
            isinstance(definition, (ast.FunctionDef, ast.AsyncFunctionDef))
            and definition.name.startswith("test_")
            for node in module.body
            for definition in (
                node.body
                if isinstance(node, ast.ClassDef)
                and (node.name.startswith("Test") or node.name in case_classes)
                else [node]
            )
        )
    except (OSError, SyntaxError, UnicodeError) as error:
        msg = f"{path}: Python source could not be parsed: {error}"
        raise CommandError(
            msg,
        ) from error


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
                ast.unparse(node.func) in {"Path.home", "Path.cwd"}
                or (
                    ast.unparse(node.func) == "Path"
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
            and ast.unparse(node.func) == "Path"
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
    *,
    check_repository: bool = True,
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
        f"packages/{package.name}/main.py:{node.lineno}: runtime paths "
        "must derive from declared RUNTIME_DIR or INPUT_DIRS"
        for node in _implicit_runtime_paths(module, bindings)
    )
    repository = package.root.parent.parent
    expected = None
    if check_repository and (repository / ".git").exists():
        origin = git(repository, ["remote", "get-url", "origin"], check=False)
        if origin.returncode == 0:
            expected = canonical_remote_path(origin.stdout.strip())
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
                    and len(path.parts[:-3]) >= len(("host", "repository"))
                    and path.parts[-3] == "packages"
                    and path.parts[-1] == "tmp"
                    and (name != "INPUT_DIRS" or isinstance(value, ast.Tuple))
                    and (name == "INPUT_DIRS" or path.parts[-2] == package.name)
                    and (expected is None or Path(*path.parts[:-3]) == expected)
                )
                if not valid:
                    issues.append(
                        f"packages/{package.name}/main.py:{statement.lineno}: {name} "
                        "must use Path.home() / "
                        "'<host>/<repository>/packages/NAME/tmp'; "
                        "RUNTIME_DIR must belong to this package "
                        "and INPUT_DIRS must be a tuple",
                    )
    return issues


def _python_source_issues(package: Package) -> list[str]:
    """Check source contracts without importing or executing package code."""
    if package.kind != "python":
        return []
    issues = []
    if issue := _python_test_placement_issue(package):
        issues.append(issue)
    source = read_regular(package.root / "main.py")
    if source is not None:
        module = ast.parse(source, filename=str(package.root / "main.py"))
        issues.extend(_runtime_directory_issues(package, module))
        binding = _module_main_binding(module)
        entrypoint = (
            binding.value
            if isinstance(binding, (ast.Assign, ast.AnnAssign))
            else binding
        )
        prefix = f"packages/{package.name}/main.py: "
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


def _check_test_default(root: Path, package: Package) -> None:
    """Ensure generated Python test checks retain their static definition."""
    check = root / "checks" / package.name / "default.nix"
    if not check.is_file():
        return
    actual = read_regular(check)
    if not (actual is not None):
        raise AssertionError
    expected = _current_python_test_source()
    if nix_syntax.compact(actual) != nix_syntax.compact(expected):
        msg = (
            f"{check.relative_to(root)}: differs from the canonical test check template"
        )
        raise CommandError(msg)


def _current_python_test_source() -> str:
    """Render a check using the repository's pinned shared builder."""
    return """{ inputs, pkgs, ... }:
(inputs.afairesi or inputs.self).lib.mkPythonCheck {
  inherit pkgs;
  packageDrv = inputs.self.packages.${pkgs.stdenv.system}.${baseNameOf ./.};
  packageName = baseNameOf ./.;
}
"""


def _current_host_check_source() -> str:
    """Render a host check using the repository's pinned shared builder."""
    return """{ inputs, pkgs, ... }:
(inputs.afairesi or inputs.self).lib.mkHostCheck {
  inherit inputs pkgs;
  host = pkgs.lib.removeSuffix "VmWithDisko" (baseNameOf ./.);
}
"""


def _python_static_template_issues(package: Package, source: str) -> list[str]:
    """Check only the stable interface required by Python package templates."""
    return [
        f"missing required {name} definition"
        for name, edits in _python_required_edits(package, source).items()
        if edits
    ]


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
    return _module_has_main(ast.parse(source, filename="main.py"))


def _module_has_main(module: ast.Module) -> bool:
    """Recognize function, import, and assignment bindings for a module's main."""
    return _module_main_binding(module) is not None


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


def canonical_package_default(package: Package) -> str | None:  # noqa: C901, PLR0912
    """Render a package definition while retaining package-specific fields."""
    if package.kind == "nix":
        return None
    source = read_regular(package.root / "default.nix")
    if source is None:
        rendered = scaffold(package.kind, package.name, None)[
            Path("packages") / package.name / "default.nix"
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
        Path("packages") / package.name / "default.nix"
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
    if scope is None:
        declarations = " ".join(f"{name} = {value};" for name, value in bindings)
        edits.append(
            (body.start_byte, body.start_byte, f"let {declarations} in ".encode()),
        )
    else:
        for name, value in bindings:
            edits.extend(_nix_binding_edits(document, scope, (name,), value))
    return cast("bytes", nix_syntax.apply_edits(source.encode(), edits)).decode()


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


def _converge_packages(root: Path, packages: list[Package], dry_run: bool) -> bool:  # noqa: FBT001
    """Converge package templates and required source files."""
    changed = False
    for package in packages:
        expected_default = canonical_package_default(package)
        if expected_default is not None:
            relative = Path("packages") / package.name / "default.nix"
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


def _converge_checks(root: Path, packages: list[Package], dry_run: bool) -> bool:  # noqa: FBT001
    """Generate every check derived from a canonical package or host."""
    changed = False
    for relative, source in canonical_checks(root, packages).items():
        changed |= _write_managed_nix(root, relative, source, dry_run=dry_run)
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


def _cleanup_flake(root: Path, packages: list[Package], dry_run: bool) -> bool:  # noqa: FBT001
    """Remove undeclared files while preserving permitted tmp trees."""
    allowed = allowed_paths(root, packages)
    prm = prm_directories(root)
    tmp = tmp_directories(root)
    tracked = _tracked_paths(root)
    python_entrypoints = {
        Path("packages") / package.name / "main.py"
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
    clean = git(root, clean_arguments, check=False)
    if clean.returncode != 0:
        raise CommandError(clean.stderr.strip() or "git clean failed")
    if clean.stdout:
        print(clean.stdout, end="")  # noqa: T201
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
        "buildPythonPackage": (1, "packages/NAME/default.nix"),
        "buildPythonApplication": (1, "packages/NAME/default.nix"),
        "derivation": (1, "packages/NAME/default.nix"),
        "mkDerivation": (1, "packages/NAME/default.nix"),
        "mkPythonPackage": (1, "packages/NAME/default.nix"),
        "mkPythonCheck": (1, "the package testing structure"),
        "mkHostCheck": (1, "the host testing structure"),
        "nixosSystem": (1, "hosts/NAME/configuration.nix"),
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
    dry_run: bool,  # noqa: FBT001
    *,
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
    changed = _converge_packages(root, packages, dry_run)
    expected = render_gitignore(allowed_paths(root, packages), prm_directories(root))
    actual = read_regular(root / ".gitignore")
    if actual != expected:
        changed |= write_managed(
            root,
            Path(".gitignore"),
            expected,
            dry_run=dry_run,
        )
    changed |= _converge_checks(root, packages, dry_run)
    changed |= _cleanup_flake(root, packages, dry_run)
    if dry_run and changed:
        if fail_on_changes:
            msg = "flake repository would change"
            raise CommandError(msg)
        return packages
    return validate_flake_source(root)


def validate_flake_source(root: Path) -> list[Package]:  # noqa: C901
    """Validate a Git-filtered flake source without requiring Git metadata."""
    packages, issues = inspect_structure(root)
    issues.extend(_flake_resource_issues(root))
    for required in (".gitignore", "README", "flake.lock", "flake.nix"):
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
    issues.extend(_generated_check_issues(root, packages))
    if issues:
        formatted_issues = "\n".join(f"  - {issue}" for issue in issues)
        msg = f"repository layout validation failed:\n{formatted_issues}"
        raise CommandError(msg)
    for package in packages:
        default = package.root / "default.nix"
        if default.is_file():
            nix_syntax.parse(default.read_bytes(), str(default))
        if package.kind == "python":
            _check_test_default(root, package)
    checks_root = root / "checks"
    if checks_root.is_dir():
        for check in checks_root.iterdir():
            default = check / "default.nix"
            if default.is_file():
                nix_syntax.parse(default.read_bytes(), str(default))
    return packages


def _generated_check_issues(root: Path, packages: list[Package]) -> list[str]:
    """Return missing or noncanonical generated-check issues."""
    issues = []
    for check, expected in canonical_checks(root, packages).items():
        actual = read_regular(root / check)
        if actual is None:
            issues.append(f"{check}: missing generated check")
        elif nix_syntax.compact(actual) != nix_syntax.compact(expected):
            issues.append(f"{check}: differs from its canonical generated template")
    return issues


def _source_package_issues(root: Path, package: Package) -> list[str]:
    """Return source-only package-template and generated-check issues."""
    issues: list[str] = []
    relative = Path("packages") / package.name / "default.nix"
    actual = read_regular(root / relative)
    expected = canonical_package_default(package)
    if (
        expected is not None
        and actual is not None
        and nix_syntax.compact(actual) != nix_syntax.compact(expected)
    ):
        issues.append(f"{relative}: differs from its canonical package template")
    if package.kind == "python" and actual is not None:
        issues.extend(
            f"{relative}: {issue}"
            for issue in _python_static_template_issues(package, actual)
        )
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
    root = Path("packages") / name
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
            f'''#!/usr/bin/env python3\n{description!r}\n\nimport argparse\n\n\ndef parser() -> argparse.ArgumentParser:\n    """Declare the command-line interface."""\n    return argparse.ArgumentParser(description={description!r})\n\n\ndef main(argv: list[str] | None = None) -> None:\n    """Run {name}."""\n    parser().parse_args(argv)\n\n\nif __name__ == "__main__":\n    main()\n'''  # noqa: E501
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
                root
                / "ms.tex": "\\documentclass{article}\n\\begin{document}\n\\end{document}\n",  # noqa: E501
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
    *,
    dry_run: bool = False,
) -> None:
    """Create a package transactionally and stage its managed files."""
    if kind not in PACKAGE_KINDS:
        msg = f"unsupported package type: {kind}\nhint: supported package types: {', '.join(PACKAGE_KINDS)}"  # noqa: E501
        raise CommandError(
            msg,
        )
    validate_name(name)
    files = scaffold(kind, name, description)
    relative = Path("packages") / name
    validate_managed_path(root, relative)
    validate_managed_path(root, Path("checks") / name)
    if (root / relative).exists() or (root / "checks" / name).exists():
        msg = f"package or generated check already exists: {name}"
        raise CommandError(msg)
    if dry_run:
        read_regular(root / ".gitignore")
        for path in files:
            report_change(f"write '{path}'", dry_run=True)
        report_change("update and stage '.gitignore' and package files", dry_run=True)
        return
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
            completed = git(root, ["add", "--", *generated], check=False)
            if completed.returncode != 0:
                raise CommandError(completed.stderr.strip() or "git add failed")  # noqa: TRY301
    except BaseException:
        for path in reversed(created):
            path.unlink(missing_ok=True)
        remove_empty_directories(directories)
        raise


def add_host(root: Path, name: str, *, dry_run: bool = False) -> None:
    """Create a host and stage its canonical configuration."""
    validate_host_name(name)
    relative = Path("hosts") / name / "configuration.nix"
    path = root / relative
    check_relative = Path("checks") / f"{name}VmWithDisko" / "default.nix"
    check = root / check_relative
    validate_managed_path(root, relative)
    validate_managed_path(root, check_relative)
    if (
        path.parent.exists()
        or path.parent.is_symlink()
        or check.parent.exists()
        or check.parent.is_symlink()
    ):
        msg = f"host or generated check already exists: {name}"
        raise CommandError(msg)
    if dry_run:
        read_regular(root / ".gitignore")
        report_change(f"write '{relative}'", dry_run=True)
        report_change(f"write '{check_relative}'", dry_run=True)
        report_change("update and stage '.gitignore' and host files", dry_run=True)
        return
    directories = missing_directories(root, [relative.parent, check_relative.parent])
    try:
        with preserve_whitelist_on_error(root):
            path.parent.mkdir(parents=True)
            path.write_text("{ ... }: { }\n", encoding="utf-8")
            check.parent.mkdir(parents=True)
            check.write_text(_current_host_check_source(), encoding="utf-8")
            _refresh_gitignore(root)
            completed = git(
                root,
                [
                    "add",
                    "--",
                    str(relative),
                    str(check_relative),
                    ".gitignore",
                ],
                check=False,
            )
            if completed.returncode != 0:
                raise CommandError(completed.stderr.strip() or "git add failed")  # noqa: TRY301
    except BaseException:
        path.unlink(missing_ok=True)
        check.unlink(missing_ok=True)
        remove_empty_directories(directories)
        raise


def remove_resource(
    root: Path,
    value: str,
    *,
    dry_run: bool,
    force: bool = False,
) -> None:
    """Remove a canonical package or host and stage its related metadata."""
    kind, name, relative = _parse_resource_path(value)
    resource_root = root / relative
    marker = "default.nix" if kind == "package" else "configuration.nix"
    if (
        not resource_root.is_dir()
        or resource_root.is_symlink()
        or not (resource_root / marker).is_file()
    ):
        msg = f"{kind} does not exist: {name}"
        raise CommandError(msg)
    check_root = root / "checks" / (name if kind == "package" else f"{name}VmWithDisko")
    targets = [
        resource_root,
        *([check_root] if check_root.exists() else []),
    ]
    for target in targets:
        validate_managed_path(root, target.relative_to(root))
        if not target.is_dir():
            msg = f"{target.relative_to(root)}: removal target must be a directory"
            raise CommandError(msg)
    read_regular(root / ".gitignore")
    target_relatives = [str(target.relative_to(root)) for target in targets]
    if not force:
        status = git(
            root,
            [
                "status",
                "--porcelain",
                "--untracked-files=all",
                "--ignored",
                "--",
                *target_relatives,
            ],
        )
        if status.stdout:
            msg = (
                "resource or related checks contain local changes or untracked data; "
                "commit or preserve them, or use --force to remove them"
            )
            raise CommandError(msg)
    arguments = ["rm", "-r", "--ignore-unmatch"]
    if force:
        arguments.append("--force")
    if dry_run:
        arguments.append("--dry-run")
    removal = git(root, [*arguments, "--", *target_relatives])
    print(removal.stdout, end="")  # noqa: T201
    report_change("update '.gitignore'", dry_run=dry_run)
    clean_arguments = ["clean", "-dx", "--dry-run" if dry_run else "--force"]
    if force:
        clean_arguments.extend(("--force", "--force"))
    clean = git(root, [*clean_arguments, "--", *target_relatives])
    print(clean.stdout, end="")  # noqa: T201
    if not dry_run:
        _refresh_gitignore(root)
        git(root, ["add", "--", ".gitignore"])


def _parse_resource_path(value: str) -> tuple[str, str, Path]:
    """Parse a canonical package or host resource path."""
    path = Path(value)
    if path.is_absolute() or len(path.parts) != 2:  # noqa: PLR2004
        msg = f"resource path must be packages/NAME or hosts/NAME: {value}"
        raise CommandError(msg)
    parent, name = path.parts
    if parent == "packages":
        validate_name(name)
        return "package", name, path
    if parent == "hosts":
        validate_host_name(name)
        return "host", name, path
    msg = f"resource path must be packages/NAME or hosts/NAME: {value}"
    raise CommandError(msg)


def _renamed_runtime_expression(
    expression: cst.BaseExpression,
    old: Path,
    new: Path,
    repository: Path | None,
) -> cst.BaseExpression:
    """Change only the package component of a declared literal runtime path."""
    import libcst as cst  # noqa: PLC0415

    code = cst.Module([]).code_for_node(expression)
    path = _runtime_path(ast.parse(code, mode="eval").body)
    if path is None or path.parts[-3:] != (*old.parts, "tmp"):
        return expression
    if repository is not None and Path(*path.parts[:-3]) != repository:
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
    import libcst as cst  # noqa: PLC0415

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
    origin = git(root, ["remote", "get-url", "origin"], check=False)
    repository = (
        canonical_remote_path(origin.stdout.strip()) if origin.returncode == 0 else None
    )
    updates = []
    for package in detect_packages(root):
        if package.kind != "python":
            continue
        relative = Path("packages") / package.name / "main.py"
        validate_managed_path(root, relative)
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
    *,
    dry_run: bool,
) -> None:
    """Apply prepared edits without staging unrelated working changes."""
    for relative, working, staged, mode in updates:
        report_change(f"update runtime paths in '{relative}'", dry_run=dry_run)
        if dry_run:
            continue
        (root / relative).write_text(working, encoding="utf-8")
        if staged is not None and mode is not None:
            blob = run(
                ["git", "hash-object", "-w", "--stdin"],
                cwd=root,
                input_source=staged,
            ).stdout.strip()
            git(root, ["update-index", "--cacheinfo", mode, blob, str(relative)])


def rename_resource(root: Path, source: str, destination: str, dry_run: bool) -> None:  # noqa: FBT001
    """Rename one canonical package or host and stage its related metadata."""
    source_kind, source_name, source_relative = _parse_resource_path(source)
    destination_kind, _, destination_relative = _parse_resource_path(destination)
    if source_kind != destination_kind:
        msg = "cannot rename a package to a host or a host to a package"
        raise CommandError(msg)
    validate_managed_path(root, source_relative)
    validate_managed_path(root, destination_relative)
    source_path = root / source_relative
    destination_path = root / destination_relative
    marker = "default.nix" if source_kind == "package" else "configuration.nix"
    if (
        not source_path.is_dir()
        or source_path.is_symlink()
        or not (source_path / marker).is_file()
    ):
        msg = f"{source_kind} does not exist: {source_name}"
        raise CommandError(msg)
    if destination_path.exists() or destination_path.is_symlink():
        msg = f"destination already exists: {destination_relative}"
        raise CommandError(msg)
    moves = [(source_relative, destination_relative)]
    check_suffix = "" if source_kind == "package" else "VmWithDisko"
    source_check = Path("checks") / f"{source_name}{check_suffix}"
    destination_name = destination_relative.name
    destination_check = Path("checks") / f"{destination_name}{check_suffix}"
    if (root / source_check).exists():
        if (root / destination_check).exists():
            msg = f"destination already exists: {destination_check}"
            raise CommandError(msg)
        moves.append((source_check, destination_check))
    updates = (
        _runtime_rename_updates(root, source_relative, destination_relative)
        if source_kind == "package"
        else []
    )
    for old, new in moves:
        validate_managed_path(root, old)
        validate_managed_path(root, new)
        report_change(f"move '{old}' to '{new}'", dry_run=dry_run)
    report_change("update '.gitignore'", dry_run=dry_run)
    if dry_run:
        _apply_runtime_updates(root, updates, dry_run=True)
        return
    tracked = _tracked_paths(root)
    for old, new in moves:
        if any(beneath(path, {old}) for path in tracked):
            git(root, ["mv", "--", str(old), str(new)])
        else:
            shutil.move(root / old, root / new)
    _apply_runtime_updates(root, updates, dry_run=False)
    _refresh_gitignore(root)
    git(root, ["add", "--", ".gitignore"])


def require_nix_flakes() -> str:
    """Check the effective Nix features before initialization changes files."""
    executable = os.environ.get("AFAIRESI_NIX", "nix")
    try:
        completed = run(
            [executable, "config", "show", "experimental-features"],
            check=False,
        )
    except FileNotFoundError as error:
        msg = f"Nix executable not found: {executable}; install Nix to use init-repo"
        raise CommandError(msg) from error
    if completed.returncode != 0:
        msg = f"could not check Nix features: {completed.stderr.strip()}"
        raise CommandError(
            msg + "\ninit-repo requires Nix with "
            "'experimental-features = nix-command flakes'",
        )
    missing = {"nix-command", "flakes"} - set(completed.stdout.split())
    if missing:
        msg = (
            f"init-repo requires Nix features: {', '.join(sorted(missing))}; "
            "enable them with 'experimental-features = nix-command flakes' "
            "in your Nix configuration"
        )
        raise CommandError(msg)
    return executable


def initialize_flake(root: Path, *, nix_executable: str | None = None) -> None:
    """Scaffold and format a new flake in an existing repository directory."""
    executable = nix_executable or require_nix_flakes()
    (root / "flake.nix").write_text(
        '{ inputs.afairesi.url = "github:afairesi/afairesi"; outputs = inputs: inputs.afairesi.blueprint { inherit inputs; }; }\n',  # noqa: E501
        encoding="utf-8",
    )
    (root / "README").write_text(f"# {root.name}\n", encoding="utf-8")
    run([executable, "flake", "lock"], cwd=root)
    packages = detect_packages(root)
    _converge_checks(root, packages, False)  # noqa: FBT003
    (root / ".gitignore").write_text(
        render_gitignore(
            allowed_paths(root, packages) | {Path(".gitignore")},
            prm_directories(root),
        ),
        encoding="utf-8",
    )
    run([executable, "fmt"], cwd=root)


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


def _configure_repo_parser(result: argparse.ArgumentParser) -> None:
    """Construct the flake resource command-line parser."""
    commands = result.add_subparsers(
        dest="command",
        title="commands",
        metavar="COMMAND",
    )
    create = commands.add_parser(
        "add",
        usage="%(prog)s [-h] [-n] {packages/NAME TYPE | hosts/NAME}",
        help="create a package or host",
        description="Create and stage a canonical package or host.",
        epilog=(
            "Update and stage the repository whitelist. Host creation also "
            "generates and stages its check. New Python packages have no tests "
            "or checks; add tests, then run nix fmt to generate their checks."
        ),
    )
    create.add_argument(
        "resource",
        metavar="RESOURCE",
        help="new repository-relative packages/NAME or hosts/NAME path",
    )
    create.add_argument(
        "type",
        nargs="?",
        metavar="TYPE",
        choices=PACKAGE_KINDS,
        help=f"required for packages, omitted for hosts ({', '.join(PACKAGE_KINDS)})",
    )
    create.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="print additions without changing the repository",
    )
    move = commands.add_parser(
        "mv",
        help="rename a package or host",
        description="Rename a canonical package or host and stage the result.",
        epilog=(
            "Update and stage the repository whitelist and related checks. Package "
            "renames update declared runtime paths and references from other "
            "packages in the repository, preserving unstaged edits. Runtime data "
            "moves with the package directory."
        ),
    )
    move.add_argument(
        "source",
        metavar="SOURCE",
        help="existing repository-relative packages/NAME or hosts/NAME path",
    )
    move.add_argument(
        "destination",
        metavar="DESTINATION",
        help="new repository-relative path in the same resource collection",
    )
    move.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="print moves without changing the repository",
    )
    repair = commands.add_parser(
        "repair",
        help="repair and stage the current repository structure",
        description=(
            "Repair and stage canonical flake structure from any working directory."
        ),
        epilog="Use nix fmt to repair structure and also format code.",
    )
    repair.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="report required repairs without changes",
    )
    remove = commands.add_parser(
        "rm",
        help="remove a package or host",
        description="Remove and stage a canonical package or host.",
        epilog=(
            "Reject local changes and untracked data unless --force is given. "
            "Update the whitelist and remove related checks, including runtime data "
            "inside the removed resource."
        ),
    )
    remove.add_argument(
        "resource",
        metavar="RESOURCE",
        help="repository-relative packages/NAME or hosts/NAME",
    )
    remove.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="print removals without changing the repository",
    )
    remove.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="allow deletion of local changes and untracked data",
    )


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


def _local_submodule_names(root: Path) -> set[str]:
    """Read submodule section names from the repository's own configuration."""
    completed = git(
        root,
        [
            "config",
            "--local",
            "--null",
            "--name-only",
            "--get-regexp",
            r"^submodule\..*\..*$",
        ],
        check=False,
    )
    if completed.returncode == 1 and not completed.stdout and not completed.stderr:
        return set()
    if completed.returncode != 0:
        msg = (
            f"could not read local submodule configuration: {completed.stderr.strip()}"
        )
        raise CommandError(msg)
    return {
        key.removeprefix("submodule.").rsplit(".", 1)[0]
        for key in completed.stdout.split("\0")
        if key
    }


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
    if not (root / actual / ".git").exists():
        return False
    changed = False
    if actual != expected:
        report_change(f"move '{actual}' to '{expected}'", dry_run=dry_run)
        changed = True
        if not dry_run:
            (root / expected).parent.mkdir(parents=True, exist_ok=True)
            git(root, ["mv", "--", str(actual), str(expected)])
            git(
                root,
                [
                    "config",
                    "--file",
                    ".gitmodules",
                    f"submodule.{repository['name']}.path",
                    expected.as_posix(),
                ],
            )
        changed |= _move_home_whitelist(root, actual, expected, dry_run=dry_run)
    if repository["name"] != expected.as_posix():
        report_change(
            f"rename submodule '{repository['name']}' to '{expected.as_posix()}'",
            dry_run=dry_run,
        )
        changed = True
        if not dry_run:
            git(
                root,
                [
                    "config",
                    "--file",
                    ".gitmodules",
                    "--rename-section",
                    f"submodule.{repository['name']}",
                    f"submodule.{expected.as_posix()}",
                ],
            )
            if repository["name"] in _local_submodule_names(root):
                git(
                    root,
                    [
                        "config",
                        "--local",
                        "--rename-section",
                        f"submodule.{repository['name']}",
                        f"submodule.{expected.as_posix()}",
                    ],
                )
    if changed and not dry_run:
        git(root, ["add", "--", ".gitmodules"])
    return changed


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
    for line in lines:
        if line == old or line.startswith(old + "/"):
            replacement = new + line[len(old) :]
            if replacement not in retained:
                retained.append(replacement)
    updated = "\n".join(retained) + "\n"
    if updated == source:
        return False
    return write_managed(root, Path(".gitignore"), updated, dry_run=dry_run)


def converge_home(root: Path, dry_run: bool) -> list[dict[str, str]]:  # noqa: FBT001
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
    if (
        actual_paths != repair_paths
        and git(
            root,
            ["diff", "--quiet", "--", ".gitmodules"],
            check=False,
        ).returncode
    ):
        msg = "stage .gitmodules with git add before moving submodules"
        raise CommandError(msg)
    _converge_home_ignore(root, dry_run=dry_run)
    for expected in repair_paths:
        _allow_home_submodule(root, expected, dry_run=dry_run)
    for repository, expected in zip(repositories, repair_paths, strict=True):
        _converge_home_submodule(
            root,
            repository,
            expected,
            dry_run=dry_run,
        )
    return repositories


def remove_home_submodule(
    root: Path,
    value: str,
    *,
    dry_run: bool,
    force: bool = False,
) -> None:
    """Remove a registered home submodule and its owned whitelist entries."""
    relative = Path(value)
    repositories = home_submodules(root)
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or relative.as_posix()
        not in {repository["path"] for repository in repositories}
    ):
        msg = f"not a registered home submodule: {value}"
        raise CommandError(msg)
    checkout = root / relative
    if not force and (checkout / ".git").exists():
        status = git(
            checkout,
            ["status", "--porcelain", "--untracked-files=all", "--ignored"],
        )
        if status.stdout:
            msg = (
                "submodule contains local changes or untracked data; "
                "commit or preserve them, or use --force to remove them"
            )
            raise CommandError(msg)
    source = read_regular(root / ".gitignore") or ""
    entry = f"!/{relative.as_posix()}"
    retained = [
        line
        for line in source.splitlines()
        if line != entry and not line.startswith(entry + "/")
    ]
    updated = "\n".join(retained) + "\n" if retained else ""
    arguments = ["rm", "-r", "--", f":(literal){relative.as_posix()}"]
    if force:
        arguments.insert(2, "--force")
    arguments.insert(2, "--dry-run")
    git(root, arguments)
    report_change(f"deinitialize submodule '{relative}' with Git", dry_run=dry_run)
    if not dry_run:
        git(root, ["submodule", "absorbgitdirs", "--", f":(literal){relative}"])
        deinit = ["submodule", "deinit", "--", f":(literal){relative}"]
        if force:
            deinit.insert(2, "--force")
        git(root, deinit)
        arguments.remove("--dry-run")
        git(root, arguments)
    report_change(
        f"remove submodule '{relative}' and update '.gitmodules'",
        dry_run=dry_run,
    )
    write_managed(root, Path(".gitignore"), updated, dry_run=dry_run)


def initialize_home() -> None:
    """Initialize and stage the canonical home policy without cleaning."""
    root = Path.home()
    if not (root / ".git").exists():
        run(["git", "init", str(root)])
    if repository_type(root, "home") != "home":
        msg = "cannot initialize a flake repository as a home repository"
        raise CommandError(msg)
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


def initialize_submodule(remote: str) -> None:
    """Add a hosted repository at its canonical home-relative path."""
    relative = canonical_remote_path(remote)
    home = _require_home_repository()
    validate_managed_path(home, relative)
    directories = missing_directories(home, [relative.parent])
    try:
        with preserve_whitelist_on_error(home):
            _allow_home_submodule(home, relative)
            registered = any(
                Path(repository["path"]) == relative and repository["url"] == remote
                for repository in home_submodules(home)
            )
            indexed = git(home, ["ls-files", "--stage", "--", str(relative)]).stdout
            if (
                registered
                and indexed.startswith("160000 ")
                and (home / relative / ".git").exists()
            ):
                return
            completed = run(
                ["git", "submodule", "add", "--", remote, relative.as_posix()],
                cwd=home,
            )
            if completed.stdout:
                print(completed.stdout, end="")  # noqa: T201
            if completed.stderr:
                print(completed.stderr, end="", file=sys.stderr)  # noqa: T201
    except BaseException:
        remove_empty_directories(directories)
        raise


def _remote_is_empty(remote: str) -> bool:
    """Return whether a hosted remote advertises no heads."""
    completed = run(
        ["git", "ls-remote", remote],
        check=False,
    )
    if completed.returncode != 0:
        raise CommandError(completed.stderr.strip() or "could not read remote")
    return not completed.stdout.strip()


def initialize_home_repo(remote: str) -> None:
    """Create a canonical flake at its remote-derived home path."""
    relative = canonical_remote_path(remote)
    home = _require_home_repository()
    _home_ignore_source(home)
    home_submodules(home)
    validate_managed_path(home, relative)
    nix_executable = require_nix_flakes()
    if not _remote_is_empty(remote):
        msg = "init-repo requires an empty remote"
        raise CommandError(msg)
    directory = home / relative
    if directory.exists():
        msg = f"target already exists: {directory}"
        raise CommandError(msg)
    directory.parent.mkdir(parents=True, exist_ok=True)
    run(["git", "clone", remote, str(directory)])
    try:
        initialize_flake(directory, nix_executable=nix_executable)
        git(directory, ["add", "--all"])
        git(directory, ["branch", "-M", "main"])
        git(directory, ["commit", "-m", "Initialize repository"])
    except BaseException:
        shutil.rmtree(directory)
        with contextlib.suppress(OSError):
            directory.parent.rmdir()
        raise
    _allow_home_submodule(home, relative)
    git(
        home,
        [
            "submodule",
            "add",
            "--name",
            relative.as_posix(),
            remote,
            str(relative),
        ],
    )
    git(directory, ["push", "--set-upstream", "origin", "main"])


def _configure_home_parser(result: argparse.ArgumentParser) -> None:
    """Construct the home repository command-line parser."""
    commands = result.add_subparsers(
        dest="command",
        title="commands",
        metavar="COMMAND",
    )
    clone = commands.add_parser(
        "clone",
        help="clone and register an existing remote as a home submodule",
        description=(
            "Clone a hosted remote at $HOME/<remote-hostname>/<repository-path> "
            "and register it as a submodule."
        ),
        epilog=(
            "Stage the home whitelist and submodule registration without "
            "creating commits or pushing."
        ),
    )
    clone.add_argument("remote", metavar="REMOTE", help="existing hosted Git remote")
    commands.add_parser(
        "init",
        help="initialize the home repository and stage its whitelist",
        description="Initialize the repository at $HOME and stage its whitelist.",
    )
    init_repo = commands.add_parser(
        "init-repo",
        help="create, commit, and push a flake under HOME",
        description=(
            "Create a flake from an empty hosted remote at "
            "$HOME/<remote-hostname>/<repository-path>."
        ),
        epilog=(
            "Clone the remote, scaffold "
            "and format the flake, commit its initial main branch, register and "
            "stage it as a home submodule, then push."
        ),
    )
    init_repo.add_argument("remote", metavar="REMOTE", help="empty hosted Git remote")
    repair_home = commands.add_parser(
        "repair",
        help="repair the home repository whitelist and submodule layout",
        description=(
            "Repair the repository at $HOME: its whitelist and registered "
            "submodule paths and names, from any working directory."
        ),
        epilog=(
            "Move submodules to <remote-hostname>/<repository-path>, migrate "
            "whitelist entries, and stage repairs for existing checkouts. "
        ),
    )
    repair_home.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="report required repairs without changes",
    )
    remove = commands.add_parser(
        "rm",
        help="remove a registered home submodule",
        description=(
            "Remove and stage a registered submodule of $HOME "
            "from any working directory."
        ),
        epilog=(
            "Remove the submodule's registration and whitelist entries. "
            "Reject local changes and untracked data unless --force is given."
        ),
    )
    remove.add_argument(
        "resource",
        metavar="RESOURCE",
        help="home-relative registered submodule path",
    )
    remove.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="print removals without changing the repository",
    )
    remove.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="allow deletion of local changes and untracked data",
    )


def _dispatch_home(options: argparse.Namespace) -> None:
    """Dispatch lifecycle commands against the home repository."""
    if options.command == "init":
        initialize_home()
    elif options.command == "clone":
        initialize_submodule(options.remote)
    elif options.command == "init-repo":
        initialize_home_repo(options.remote)
    elif options.command == "repair":
        converge_home(_require_home_repository(), options.dry_run)
    elif options.command == "rm":
        root = _require_home_repository()
        remove_home_submodule(
            root,
            options.resource,
            dry_run=options.dry_run,
            force=options.force,
        )


def parser() -> argparse.ArgumentParser:
    """Construct the workspace command-line parser."""
    result = argparse.ArgumentParser(
        prog="afairesi",
        description=(
            "Afairesi manages repository structure for Nix flakes "
            "through enforced conventions."
        ),
    )
    scopes = result.add_subparsers(dest="scope", title="commands", metavar="SCOPE")
    repo = scopes.add_parser(
        "repo",
        help="manage the current repository",
        description=(
            "Manage Nix flake package, host, and check conventions. "
            "Structural changes are staged for review."
        ),
        epilog=(
            "Use repair to repair flake structure, or nix fmt to also format code. "
            "Use afairesi-repo-test to run test campaigns."
        ),
    )
    home = scopes.add_parser(
        "home",
        help="manage the home repository and its submodules",
        description=(
            "Manage the Git repository at $HOME and its submodules. "
            "Hosted repositories live at $HOME/<remote-hostname>/<repository-path>. "
            "Repairs stage structural changes without committing them."
        ),
        epilog=(
            "Use Git for diffs, commits, branches, and remote synchronization. "
            "Only init-repo creates a commit and pushes."
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
    kind, name, _relative = _parse_resource_path(options.resource)
    if kind == "host":
        add_host(root, name, dry_run=options.dry_run)
        return
    add_package(root, options.type, name, dry_run=options.dry_run)


def _validate_resource_arguments(
    cli: argparse.ArgumentParser,
    options: argparse.Namespace,
) -> None:
    """Reject invalid resource arguments before discovering the repository."""
    if options.scope != "repo" or options.command not in {"add", "mv", "rm"}:
        return
    try:
        if options.command == "mv":
            source_kind, _name, _relative = _parse_resource_path(options.source)
            destination_kind, _name, _relative = _parse_resource_path(
                options.destination,
            )
        else:
            kind, _name, _relative = _parse_resource_path(options.resource)
    except CommandError as error:
        cli.error(str(error))
    if options.command == "mv":
        if source_kind != destination_kind:
            cli.error("source and destination must be in the same resource collection")
    elif options.command == "add":
        if kind == "host" and options.type is not None:
            cli.error("host creation does not accept TYPE")
        if kind == "package" and options.type is None:
            cli.error("package creation requires TYPE")


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
        root = repository_root()
        if repository_type(root) != "flake":
            msg = (
                "nix fmt requires a flake repository; run afairesi home repair for home"
            )
            raise CommandError(msg)  # noqa: TRY301
        converge_flake(root, options.dry_run)
    except (
        CommandError,
        OSError,
        SyntaxError,
        UnicodeError,
        ValueError,
        nix_syntax.NixSyntaxError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)  # noqa: T201
        raise SystemExit(1) from error


def main() -> None:
    """Dispatch the flake resource commands."""
    cli = parser()
    options = cli.parse_args()
    _validate_resource_arguments(cli, options)
    try:
        if options.command is None:
            options.scope_parser.print_help()
            return
        if options.scope == "home":
            _dispatch_home(options)
            return
        root = repository_root()
        if repository_type(root) != "flake":
            msg = (
                "afairesi repo commands require a flake repository; use "
                "afairesi home for home"
            )
            raise CommandError(msg)  # noqa: TRY301
        if options.command == "add":
            _dispatch_create(root, options)
        elif options.command == "rm":
            remove_resource(
                root,
                options.resource,
                dry_run=options.dry_run,
                force=options.force,
            )
        elif options.command == "mv":
            rename_resource(root, options.source, options.destination, options.dry_run)
        elif options.command == "repair":
            converge_flake(root, options.dry_run, fail_on_changes=False)
    except (
        CommandError,
        OSError,
        SyntaxError,
        UnicodeError,
        ValueError,
        nix_syntax.NixSyntaxError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)  # noqa: T201
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()

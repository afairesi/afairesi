#!/usr/bin/env python3
# Copyright (c) 2026- Paschalis Bizopoulos
"""Create, inspect, and converge Git and Nix repositories describing machines."""

from __future__ import annotations

import argparse
import ast
import contextlib
import getpass
import hashlib
import io
import json
import math
import os
import platform
import posixpath
import re
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict, cast

import jsonpatch
import nix_syntax
from jsonpointer import resolve_pointer

if TYPE_CHECKING:
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


def _run(
    arguments: list[str],
    cwd: Path | None = None,
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a process and preserve failures and captured output."""
    completed = subprocess.run(  # noqa: S603
        arguments,
        cwd=cwd,
        capture_output=True,
        check=False,
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
    return _run(
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


def _command_target(target: Path | None) -> Path:
    """Honor an explicit path or select the current Afairesi repository."""
    if target is not None:
        return target.resolve()
    root = repository_root()
    repository_type(root)
    return root


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
        "'afairesi init home' or "
        "'afairesi init flake REMOTE'"
    )
    raise CommandError(msg)


def _repository_type_markers(root: Path) -> tuple[bool, bool]:
    """Share layout recognition between inspection and lifecycle commands."""
    flake = any(
        (root / marker).exists()
        for marker in ("flake.nix", "flake.lock", "packages", "checks", "hosts")
    )
    gitignore = _read_regular(root / ".gitignore") or ""
    home = (root / ".gitmodules").exists() or "!/.gitmodules" in gitignore.splitlines()
    return flake, home


def _read_regular(path: Path) -> str | None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(mode):
        msg = f"{path}: must be a regular file"
        raise CommandError(msg)
    return path.read_text(encoding="utf-8")


def _change(message: str, *, dry_run: bool) -> None:
    """Report one deterministic convergence action."""
    print(("would " if dry_run else "") + message)  # noqa: T201


def _write_managed(
    root: Path,
    relative: Path,
    source: str,
    *,
    dry_run: bool,
    executable: bool = False,
) -> bool:
    """Write and stage one managed file when its contents or mode differ."""
    path = root / relative
    current = _read_regular(path) if path.exists() and not path.is_symlink() else None
    current_mode = path.lstat().st_mode if path.exists() or path.is_symlink() else 0
    mode_matches = bool(current_mode & 0o111) == executable
    if current == source and mode_matches:
        return False
    _change(f"write '{relative}'", dry_run=dry_run)
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
        _run(["git", "url-parse", "--component", component, "--", remote], check=False)
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


def home_submodules(
    root: Path,
    *,
    require_url: bool = True,
) -> list[dict[str, str]]:
    """Read submodule records using Git's configuration parser."""
    modules = root / ".gitmodules"
    if not modules.exists():
        return []
    _read_regular(modules)
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


def _converge_home_ignore(root: Path, *, dry_run: bool) -> bool:
    """Converge the canonical home whitelist."""
    changed = False
    gitignore_path = root / ".gitignore"
    source = _read_regular(gitignore_path)
    required = ["!/.gitignore", "!/.gitmodules"]
    if source is None:
        source = "*\n" + "\n".join(required) + "\n"
        changed |= _write_managed(root, Path(".gitignore"), source, dry_run=dry_run)
    lines = source.splitlines()
    if (
        not lines
        or lines[0] != "*"
        or any(not line.startswith("!/") for line in lines[1:])
    ):
        msg = f"{gitignore_path}: must start with * and subsequent lines must start with !/"  # noqa: E501
        raise CommandError(
            msg,
        )
    missing = [line for line in required if line not in lines]
    if missing:
        source = source.rstrip("\n") + "\n" + "\n".join(missing) + "\n"
        changed |= _write_managed(
            root,
            Path(".gitignore"),
            source,
            dry_run=dry_run,
        )
    return changed


def _allow_home_submodule(root: Path, relative: Path, *, dry_run: bool = False) -> bool:
    """Allow a submodule and its parents through the home whitelist."""
    changed = _converge_home_ignore(root, dry_run=dry_run)
    path = root / ".gitignore"
    source = _read_regular(path) or "*\n!/.gitignore\n!/.gitmodules\n"
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
        changed |= _write_managed(
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
    changed = False
    if actual != expected:
        _change(f"move '{actual}' to '{expected}'", dry_run=dry_run)
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
        _change(
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
            configured = git(
                root,
                ["config", "--get", f"submodule.{repository['name']}.url"],
                check=False,
            )
            if configured.returncode == 0:
                git(
                    root,
                    [
                        "config",
                        "--rename-section",
                        f"submodule.{repository['name']}",
                        f"submodule.{expected.as_posix()}",
                    ],
                )
    if changed and not dry_run:
        git(root, ["add", "--", ".gitmodules"])
    checkout = root / (actual if dry_run and actual != expected else expected)
    if not (checkout / ".git").exists():
        _change(f"initialize submodule '{expected}'", dry_run=dry_run)
        changed = True
        if not dry_run:
            git(root, ["submodule", "update", "--init", "--", str(expected)])
    if (checkout / ".git").exists():
        changed |= _sync_submodule_url(
            root,
            checkout,
            expected,
            repository["url"],
            dry_run=dry_run,
        )
    return changed


def _move_home_whitelist(
    root: Path,
    actual: Path,
    expected: Path,
    *,
    dry_run: bool,
) -> bool:
    """Move only whitelist entries owned by a relocated submodule."""
    source = _read_regular(root / ".gitignore") or ""
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
    return _write_managed(root, Path(".gitignore"), updated, dry_run=dry_run)


def _sync_submodule_url(
    root: Path,
    checkout: Path,
    expected: Path,
    configured_url: str,
    *,
    dry_run: bool,
) -> bool:
    """Synchronize a present submodule URL without changing its recorded commit."""
    changed = False
    origin = git(checkout, ["remote", "get-url", "origin"], check=False)
    if origin.returncode != 0:
        msg = f"{expected}: checkout has no origin remote"
        raise CommandError(msg)
    if origin.stdout.strip() != configured_url:
        _change(
            f"synchronize submodule URL for '{expected}'",
            dry_run=dry_run,
        )
        changed = True
        if not dry_run:
            git(root, ["submodule", "sync", "--recursive", "--", str(expected)])
            synchronized = git(checkout, ["remote", "get-url", "origin"], check=False)
            if (
                synchronized.returncode != 0
                or synchronized.stdout.strip() != configured_url
            ):
                msg = f"{expected}: origin does not match .gitmodules URL after sync"
                raise CommandError(msg)
    return changed


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
    for actual, expected in zip(actual_paths, expected_paths, strict=True):
        if actual != expected and (root / expected).exists():
            msg = f"target already exists: {expected}"
            raise CommandError(msg)
    if (
        actual_paths != expected_paths
        and git(
            root,
            ["diff", "--quiet", "--", ".gitmodules"],
            check=False,
        ).returncode
    ):
        msg = "stage .gitmodules with git add before moving submodules"
        raise CommandError(msg)
    changed = _converge_home_ignore(root, dry_run=dry_run)
    for expected in expected_paths:
        changed |= _allow_home_submodule(root, expected, dry_run=dry_run)
    for repository, expected in zip(repositories, expected_paths, strict=True):
        changed |= _converge_home_submodule(
            root,
            repository,
            expected,
            dry_run=dry_run,
        )
    if dry_run and changed:
        msg_0 = "home repository would change"
        raise CommandError(msg_0)
    return repositories


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
    """Detect the same static tests reported by overview."""
    try:
        return bool(source_test_names(path.read_bytes(), str(path)))
    except (OSError, SyntaxError, UnicodeError) as error:
        msg = f"{path}: Python source could not be parsed: {error}"
        raise CommandError(
            msg,
        ) from error


def _python_source_issues(package: Package) -> list[str]:
    """Check source contracts without importing or executing package code."""
    if package.kind != "python":
        return []
    issues = []
    if issue := _python_test_placement_issue(package):
        issues.append(issue)
    source = _read_regular(package.root / "main.py")
    if source is not None:
        module = ast.parse(source, filename=str(package.root / "main.py"))
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
    default = _read_regular(package.root / "default.nix")
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
    actual = _read_regular(check)
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
            if source_python_has_main(_read_regular(package.root / "main.py"))
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
    source = _read_regular(package.root / "default.nix")
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
    current = _read_regular(root / relative)
    if current is not None and nix_syntax.compact(current) == nix_syntax.compact(
        source,
    ):
        source = current
    return _write_managed(root, relative, source, dry_run=dry_run)


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
            changed |= _write_managed(
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
        _change(f"stage '{relative}'", dry_run=dry_run)
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
            _change(f"set mode on '{relative}'", dry_run=dry_run)
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
        _change(f"stage '{relative}'", dry_run=dry_run)
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
        _change(f"untrack '{relative}'", dry_run=dry_run)
        changed = True
        if not dry_run:
            git(root, ["rm", "--cached", "-r", "--", str(relative)])
    for relative in sorted(tracked):
        if beneath(relative, protected):
            continue
        _change(f"remove '{relative}'", dry_run=dry_run)
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


def converge_flake(root: Path, dry_run: bool) -> list[Package]:  # noqa: FBT001
    """Converge required files, structure, templates, and root whitelist."""
    missing = [
        name
        for name in (".gitignore", "flake.nix", "flake.lock")
        if not (root / name).is_file()
    ]
    if missing:
        raise CommandError("missing required file: " + missing[0])
    packages = detect_packages(root)
    issues = []
    for package in packages:
        validate_name(package.name)
        issues.extend(_python_source_issues(package))
        canonical_package_default(package)
    if issues:
        raise CommandError("\n".join(issues))
    changed = _converge_packages(root, packages, dry_run)
    expected = render_gitignore(allowed_paths(root, packages), prm_directories(root))
    actual = _read_regular(root / ".gitignore")
    if actual != expected:
        changed |= _write_managed(
            root,
            Path(".gitignore"),
            expected,
            dry_run=dry_run,
        )
    changed |= _converge_checks(root, packages, dry_run)
    changed |= _cleanup_flake(root, packages, dry_run)
    if dry_run and changed:
        msg = "flake repository would change"
        raise CommandError(msg)
    return validate_flake_source(root)


def validate_flake_source(root: Path) -> list[Package]:  # noqa: C901
    """Validate a Git-filtered flake source without requiring Git metadata."""
    packages, issues = inspect_structure(root)
    for required in (".gitignore", "README", "flake.lock", "flake.nix"):
        if not (root / required).is_file():
            issues.append(f"{required}: missing required regular file")
    expected_ignore = render_gitignore(
        allowed_paths(root, packages),
        prm_directories(root),
    )
    if _read_regular(root / ".gitignore") != expected_ignore:
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
        actual = _read_regular(root / check)
        if actual is None:
            issues.append(f"{check}: missing generated check")
        elif nix_syntax.compact(actual) != nix_syntax.compact(expected):
            issues.append(f"{check}: differs from its canonical generated template")
    return issues


def _source_package_issues(root: Path, package: Package) -> list[str]:
    """Return source-only package-template and generated-check issues."""
    issues: list[str] = []
    relative = Path("packages") / package.name / "default.nix"
    actual = _read_regular(root / relative)
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


def add_package(root: Path, kind: str, name: str, description: str | None) -> None:
    """Create a package transactionally and stage its managed files."""
    if kind not in PACKAGE_KINDS:
        msg = f"unsupported package type: {kind}\nhint: supported package types: {', '.join(PACKAGE_KINDS)}"  # noqa: E501
        raise CommandError(
            msg,
        )
    validate_name(name)
    files = scaffold(kind, name, description)
    if any((root / path).exists() for path in files):
        msg = f"package or generated check already exists: {name}"
        raise CommandError(msg)
    created: list[Path] = []
    try:
        for relative, source in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source, encoding="utf-8")
            path.chmod(0o755 if path.name == "main.py" else 0o644)
            created.append(path)
        _refresh_gitignore(root)
        generated = [str(path.relative_to(root)) for path in created] + [".gitignore"]
        completed = git(root, ["add", "--", *generated], check=False)
        if completed.returncode != 0:
            raise CommandError(completed.stderr.strip() or "git add failed")  # noqa: TRY301
    except BaseException:
        for path in reversed(created):
            path.unlink(missing_ok=True)
            with contextlib.suppress(OSError):
                path.parent.rmdir()
        raise


def add_host(root: Path, name: str) -> None:
    """Create a host and stage its canonical configuration."""
    validate_host_name(name)
    relative = Path("hosts") / name / "configuration.nix"
    path = root / relative
    check_relative = Path("checks") / f"{name}VmWithDisko" / "default.nix"
    check = root / check_relative
    if (
        path.parent.exists()
        or path.parent.is_symlink()
        or check.parent.exists()
        or check.parent.is_symlink()
    ):
        msg = f"host or generated check already exists: {name}"
        raise CommandError(msg)
    try:
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
        with contextlib.suppress(OSError):
            path.parent.rmdir()
        with contextlib.suppress(OSError):
            check.parent.rmdir()
        raise


def remove_home_submodule(root: Path, value: str, *, dry_run: bool) -> None:
    """Remove a registered home submodule and its owned whitelist entries."""
    relative = Path(value)
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or relative.as_posix()
        not in {repository["path"] for repository in home_submodules(root)}
    ):
        msg = f"not a registered home submodule: {value}"
        raise CommandError(msg)
    source = _read_regular(root / ".gitignore") or ""
    entry = f"!/{relative.as_posix()}"
    retained = [
        line
        for line in source.splitlines()
        if line != entry and not line.startswith(entry + "/")
    ]
    updated = "\n".join(retained) + "\n" if retained else ""
    arguments = ["rm", "-r", "--", f":(literal){relative.as_posix()}"]
    if dry_run:
        arguments.insert(2, "--dry-run")
    git(root, arguments)
    _change(f"remove submodule '{relative}' and update '.gitmodules'", dry_run=dry_run)
    _write_managed(root, Path(".gitignore"), updated, dry_run=dry_run)


def remove_resource(root: Path, value: str, dry_run: bool) -> None:  # noqa: FBT001
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
    target_relatives = [str(target.relative_to(root)) for target in targets]
    if dry_run:
        for target in targets:
            print(f"rm '{target.relative_to(root)}'")  # noqa: T201
        print("update '.gitignore'")  # noqa: T201
        return
    for target in targets:
        shutil.rmtree(target)
    _refresh_gitignore(root)
    git(
        root,
        [
            "add",
            "--all",
            "--force",
            "--",
            *target_relatives,
            ".gitignore",
        ],
    )


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


def rename_resource(root: Path, source: str, destination: str, dry_run: bool) -> None:  # noqa: FBT001
    """Rename one canonical package or host and stage its related metadata."""
    source_kind, source_name, source_relative = _parse_resource_path(source)
    destination_kind, _, destination_relative = _parse_resource_path(destination)
    if source_kind != destination_kind:
        msg = "cannot rename a package to a host or a host to a package"
        raise CommandError(msg)
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
    for old, new in moves:
        _change(f"move '{old}' to '{new}'", dry_run=dry_run)
    _change("update '.gitignore'", dry_run=dry_run)
    if dry_run:
        return
    tracked = _tracked_paths(root)
    for old, new in moves:
        if any(beneath(path, {old}) for path in tracked):
            git(root, ["mv", "--", str(old), str(new)])
        else:
            shutil.move(root / old, root / new)
    _refresh_gitignore(root)
    git(root, ["add", "--", ".gitignore"])


def initialize_home() -> None:
    """Initialize and stage the canonical home policy without cleaning."""
    root = Path.home()
    if not (root / ".git").exists():
        _run(["git", "init", str(root)])
    if repository_type(root, "home") != "home":
        msg = "cannot initialize a flake repository as a home repository"
        raise CommandError(msg)
    _converge_home_ignore(root, dry_run=False)


def initialize_submodule(remote: str) -> None:
    """Add a hosted repository at its canonical home-relative path."""
    relative = canonical_remote_path(remote)
    home = Path.home()
    if repository_root(home) != home or repository_type(home) != "home":
        message = "$HOME must be an initialized canonical home repository"
        raise CommandError(message)
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
    completed = subprocess.run(  # noqa: S603
        ["git", "submodule", "add", "--", remote, relative.as_posix()],  # noqa: S607
        cwd=home,
        check=False,
    )
    raise SystemExit(completed.returncode)


def _remote_is_empty(remote: str) -> bool:
    """Return whether a hosted remote advertises no heads."""
    completed = _run(
        ["git", "ls-remote", remote],
        check=False,
    )
    if completed.returncode != 0:
        raise CommandError(completed.stderr.strip() or "could not read remote")
    return not completed.stdout.strip()


def initialize_flake(remote: str) -> None:
    """Create a canonical flake at its remote-derived home path."""
    relative = canonical_remote_path(remote)
    home = Path.home()
    if repository_root(home) != home or repository_type(home) != "home":
        msg = "$HOME must be an initialized canonical home repository"
        raise CommandError(msg)
    if not _remote_is_empty(remote):
        msg = "init flake requires an empty remote"
        raise CommandError(msg)
    readme = f"# {relative.name}\n"
    directory = home / relative
    if directory.exists():
        msg = f"target already exists: {directory}"
        raise CommandError(msg)
    directory.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "clone", remote, str(directory)])
    try:
        flake = directory / "flake.nix"
        flake.write_text(
            '{ inputs.afairesi.url = "github:afairesi/afairesi"; outputs = inputs: inputs.afairesi.blueprint { inherit inputs; }; }\n',  # noqa: E501
            encoding="utf-8",
        )
        (directory / "README").write_text(readme, encoding="utf-8")
        _run(
            [os.environ.get("AFAIRESI_NIX", "nix"), "flake", "lock"],
            cwd=directory,
        )
        detected_packages = detect_packages(directory)
        _converge_checks(directory, detected_packages, False)  # noqa: FBT003
        (directory / ".gitignore").write_text(
            render_gitignore(
                allowed_paths(directory, detected_packages),
                prm_directories(directory),
            ),
            encoding="utf-8",
        )
        _run(
            [os.environ.get("AFAIRESI_NIX", "nix"), "fmt"],
            cwd=directory,
        )
        git(directory, ["add", "--all"])
        git(directory, ["branch", "-M", "main"])
        git(directory, ["commit", "-m", "Initialize repository"])
        git(directory, ["push", "--set-upstream", "origin", "main"])
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


def source_test_names(source: bytes, filename: str) -> list[str]:
    """Convert Python source into sentences in definition order without executing it."""
    module = ast.parse(source, filename=filename)
    case_classes = _unittest_classes(module)
    definitions = []
    for node in module.body:
        if isinstance(node, ast.ClassDef) and (
            node.name.startswith("Test") or node.name in case_classes
        ):
            definitions.extend(node.body)
        else:
            definitions.append(node)
    return [
        node.name.removeprefix("test_").replace("_", " ")
        for node in definitions
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    ]


@dataclass(frozen=True)
class CliEntry:
    """One statically discovered command or parameter at a command path."""

    path: tuple[str, ...]
    text: str
    command: bool = False


def _argparse_cli(module: ast.Module, filename: str) -> list[CliEntry]:  # noqa: C901, PLR0915
    """Describe the supported static argparse declarations."""
    lines: list[CliEntry] = []
    found = False
    constructors: set[str] = set()
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            constructors.update(
                f"{alias.asname or alias.name}.ArgumentParser"
                for alias in node.names
                if alias.name == "argparse"
            )
        elif isinstance(node, ast.ImportFrom) and node.module == "argparse":
            constructors.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name == "ArgumentParser"
            )

    def unsupported(node: ast.AST) -> None:
        message = (
            f"unsupported CLI interface at {filename}:{getattr(node, 'lineno', 0)}: "
            "expected static argparse declarations"
        )
        raise ValueError(message)

    def literal(node: ast.AST) -> object:
        try:
            return ast.literal_eval(node)
        except (ValueError, TypeError):
            unsupported(node)
        return None

    def argument_row(call: ast.Call) -> str:
        if not call.args or any(isinstance(arg, ast.Starred) for arg in call.args):
            unsupported(call)
        if any(
            keyword.arg == "help"
            and isinstance(keyword.value, ast.Attribute)
            and keyword.value.attr == "SUPPRESS"
            for keyword in call.keywords
        ):
            return ""
        names = [literal(arg) for arg in call.args]
        if any(not isinstance(arg, str) for arg in names):
            unsupported(call)
        values = {}
        for keyword in call.keywords:
            if keyword.arg is None:
                unsupported(call)
            if keyword.arg in {
                "action",
                "required",
                "default",
                "choices",
                "nargs",
                "metavar",
                "const",
                "type",
                "help",
            }:
                values[keyword.arg] = (
                    (
                        repr(keyword.value.id)
                        if keyword.arg == "action"
                        and isinstance(keyword.value, ast.Name)
                        else ast.unparse(keyword.value)
                    )
                    if keyword.arg in {"type", "action"}
                    or (
                        keyword.arg == "default"
                        and isinstance(keyword.value, ast.Call)
                        and ast.unparse(keyword.value.func) == "Path"
                        and not keyword.value.args
                        and not keyword.value.keywords
                    )
                    else repr(literal(keyword.value))
                )
        positional = not str(names[0]).startswith("-")
        required = values.get("required") == "True" or (
            positional and values.get("nargs") not in {"'?'", "'*'"}
        )
        details = ["required" if required else "optional"]
        details.extend(
            f"{key}={value}"
            for key, value in sorted(values.items())
            if key not in {"required", "help"}
        )
        help_text = values.get("help", "")
        suffix = f"; help={help_text}" if help_text else ""
        return ", ".join(str(arg) for arg in names) + "  " + "; ".join(details) + suffix

    def visit(  # noqa: C901, PLR0912
        statements: list[ast.stmt],
        inherited: dict[str, tuple[str, ...]],
    ) -> None:
        nonlocal found
        owners = dict(inherited)
        for statement in statements:
            if isinstance(
                statement,
                (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
            ):
                visit(statement.body, owners)
                continue
            if isinstance(statement, ast.If):
                visit(statement.body, owners)
                visit(statement.orelse, owners)
                continue
            call = (
                statement.value
                if isinstance(statement, (ast.Assign, ast.Expr, ast.Return))
                else None
            )
            if not isinstance(call, ast.Call):
                if any(
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr
                    in {"add_argument", "add_parser", "ArgumentParser"}
                    for node in ast.walk(statement)
                ):
                    unsupported(statement)
                continue
            method = call.func.attr if isinstance(call.func, ast.Attribute) else ""
            name = ast.unparse(call.func)
            parent = (
                ast.unparse(call.func.value)
                if isinstance(call.func, ast.Attribute)
                else ""
            )
            path = owners.get(parent, ())
            if name in constructors:
                found = True
                if any(
                    keyword.arg in {"parents", "argument_default", "prefix_chars", None}
                    for keyword in call.keywords
                ):
                    unsupported(call)
            elif method in {
                "add_subparsers",
                "add_argument_group",
                "add_mutually_exclusive_group",
                "add_parser",
                "add_argument",
            }:
                if parent not in owners:
                    unsupported(call)
                if method == "add_parser":
                    if not call.args or not isinstance(literal(call.args[0]), str):
                        unsupported(call)
                    path = (*path, str(literal(call.args[0])))
                    lines.append(CliEntry(path, "command", command=True))
                elif method == "add_argument":
                    row = argument_row(call)
                    if row:
                        lines.append(CliEntry(path, row))
                    continue
            else:
                continue
            if isinstance(statement, ast.Assign):
                for target in statement.targets:
                    if not isinstance(target, ast.Name):
                        unsupported(target)
                    owners[ast.unparse(target)] = path

    public_parser = next(
        (
            node
            for node in module.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "parser"
            and any(
                isinstance(child, ast.Call) and ast.unparse(child.func) in constructors
                for child in ast.walk(node)
            )
        ),
        None,
    )
    visit(public_parser.body if public_parser else module.body, {})
    if not found:
        message = (
            f"unsupported CLI interface in {filename}: no static argparse parser found"
        )
        raise ValueError(message)
    return lines


def _module_cli(module: ast.Module, filename: str) -> list[CliEntry]:
    """Read static argparse declarations without executing package code."""
    imports: set[str] = set()
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".", 1)[0])
    if "argparse" in imports:
        return _argparse_cli(module, filename)
    executable = _module_has_main(module) or bool(imports & {"click", "typer", "fire"})
    if not executable and not any(
        (isinstance(node, ast.Attribute) and node.attr == "argv")
        or (isinstance(node, ast.Name) and node.id == "argv")
        or (isinstance(node, ast.Constant) and node.value == "__main__")
        for node in ast.walk(module)
    ):
        return []
    msg = (
        f"unsupported CLI interface in {filename}: no static argparse parser found; "
        "declare an argparse parser() for executable packages"
    )
    raise ValueError(msg)


class DeclaredDependency(TypedDict):
    """One source declaration, without claiming an evaluated dependency closure."""

    kind: str
    target: str


_DEPENDENCY_FIELDS = {
    "propagatedBuildInputs": "runtime",
    "propagatedNativeBuildInputs": "build",
    "buildInputs": "build",
    "nativeBuildInputs": "build",
    "runtimeInputs": "runtime",
    "dependencies": "runtime",
    "build-system": "build",
    "checkInputs": "test",
    "nativeCheckInputs": "test",
    "systemPackages": "runtime",
}


def _dependency_alias(
    document: nix_syntax.Document,
    node: Node,
    name: str,
) -> Node | None:
    """Find an alias in its lexical let/recursive-set scope, respecting formals."""
    parent = node.parent
    while parent is not None:
        if parent.type == "function_expression":
            formals = parent.child_by_field_name("formals")
            if formals is not None and any(
                child.type == "formal"
                and (formal := child.child_by_field_name("name")) is not None
                and document.text(formal) == name
                for child in formals.named_children
            ):
                return None
        if parent.type in {"let_expression", "rec_attrset_expression"}:
            bindings = next(
                (
                    child
                    for child in parent.named_children
                    if child.type == "binding_set"
                ),
                None,
            )
            for binding in [] if bindings is None else bindings.named_children:
                attrpath = binding.child_by_field_name("attrpath")
                if attrpath is not None and nix_syntax.static_attrpath(
                    document,
                    attrpath,
                ) == (name,):
                    return binding.child_by_field_name("expression")
        parent = parent.parent
    return None


def _dependency_parts(
    document: nix_syntax.Document,
    node: Node,
    seen: frozenset[int] = frozenset(),
) -> list[str | None] | None:
    """Expand static attribute prefixes and aliases, retaining dynamic components."""
    if node.id in seen:
        return None
    seen = seen | {node.id}
    if node.type in {"variable_expression", "identifier"}:
        name = document.text(node)
        alias = _dependency_alias(document, node, name)
        if alias is not None:
            return _dependency_parts(document, alias, seen)
        return [name]
    if node.type == "select_expression":
        base = node.child_by_field_name("expression")
        attrs = node.child_by_field_name("attrpath")
        parts = None if base is None else _dependency_parts(document, base, seen)
        if parts is None or attrs is None:
            return None
        for child in attrs.named_children:
            if child.type == "identifier":
                parts.append(document.text(child))
            else:
                parts.append(nix_syntax.string_value(document, child))
        return parts
    return None


def _dependency_target(parts: list[str | None] | None, expression: str) -> str:
    if parts is None:
        return expression
    for prefix in (["inputs", "self", "packages"], ["self", "packages"]):
        if parts[: len(prefix)] == prefix and len(parts) >= len(prefix) + 2:
            name = parts[len(prefix) + 1]
            if name is not None:
                return "packages/" + name
    return (
        ".".join(cast("list[str]", parts))
        if all(part is not None for part in parts)
        else expression
    )


def _dependency_values(  # noqa: C901, PLR0911 - one case per supported syntax form
    document: nix_syntax.Document,
    node: Node,
    seen: frozenset[int] = frozenset(),
) -> list[tuple[Node, list[str | None] | None]]:
    """Expand lists and aliases while retaining unresolved expressions."""
    if node.id in seen:
        return [(node, None)]
    seen = seen | {node.id}
    if node.type == "comment":
        return []
    if node.type == "list_expression":
        return [
            value
            for child in node.named_children
            for value in _dependency_values(document, child, seen)
        ]
    if node.type in {"variable_expression", "identifier"}:
        alias = _dependency_alias(document, node, document.text(node))
        if alias is not None:
            if alias.type in {
                "apply_expression",
                "attrset_expression",
                "rec_attrset_expression",
            }:
                return [(node, [document.text(node)])]
            return _dependency_values(document, alias, seen)
    if node.type == "with_expression":
        body = node.child_by_field_name("body")
        environment = node.child_by_field_name("environment")
        prefix = (
            None if environment is None else _dependency_parts(document, environment)
        )
        values = [] if body is None else _dependency_values(document, body, seen)
        return [
            (item, prefix + parts if prefix and parts and len(parts) == 1 else parts)
            for item, parts in values
        ]
    if node.type == "parenthesized_expression":
        return _dependency_values(document, node.named_children[0], seen)
    if node.type == "binary_expression":
        left, right = (
            node.child_by_field_name("left"),
            node.child_by_field_name("right"),
        )
        if (
            left is not None
            and right is not None
            and document.source[left.end_byte : right.start_byte].strip() == b"++"
        ):
            return _dependency_values(document, left, seen) + _dependency_values(
                document,
                right,
                seen,
            )
    return [(node, _dependency_parts(document, node))]


def source_package_dependencies(
    source: str,
    name: str,
    *,
    path: str | None = None,
) -> list[DeclaredDependency]:
    """Read dependencies on this repository's packages without evaluation."""
    path = path or f"packages/{name}/default.nix"
    directory = posixpath.dirname(path)
    document = nix_syntax.parse(source, path)
    records: list[DeclaredDependency] = []
    for binding in nix_syntax.walk(document.root):
        attrpath = (
            binding.child_by_field_name("attrpath")
            if binding.type == "binding"
            else None
        )
        parts = (
            None if attrpath is None else nix_syntax.static_attrpath(document, attrpath)
        )
        expression = binding.child_by_field_name("expression")
        if not parts or parts[-1] not in _DEPENDENCY_FIELDS or expression is None:
            continue
        for node, reference in _dependency_values(document, expression):
            text = document.text(node)
            target = _dependency_target(reference, text)
            resolved = reference is not None and (
                target.startswith("packages/")
                or all(part is not None for part in reference)
            )
            records.append(
                {
                    "kind": _DEPENDENCY_FIELDS[parts[-1]] if resolved else "unresolved",
                    "target": target,
                },
            )
    known = {record["target"] for record in records}
    for node in nix_syntax.walk(document.root):
        text = document.text(node)
        if node.type == "select_expression":
            target = _dependency_target(_dependency_parts(document, node), text)
            if target.startswith("packages/") and target not in known:
                records.append(
                    {
                        "kind": "reference",
                        "target": target,
                    },
                )
                known.add(target)
        elif (
            node.type == "path_expression"
            and text.startswith(("./", "../"))
            and "${" not in text
        ):
            target = posixpath.normpath(f"{directory}/{text}")
            collection, separator, rest = target.partition("/")
            if collection == "packages" and separator and rest:
                package = "packages/" + rest.split("/")[0]
                if package != directory:
                    records.append(
                        {
                            "kind": "source",
                            "target": package,
                        },
                    )
    unique = {
        (record["kind"], record["target"]): record for record in reversed(records)
    }
    return [
        unique[key]
        for key in sorted(unique)
        if unique[key]["target"].startswith("packages/")
    ]


def _dependency_description(dependency: DeclaredDependency) -> str:
    target = " ".join(dependency["target"].split())
    return f"{dependency['kind']}: {target}"


class CliRecord(TypedDict):
    """A JSON-compatible command or parameter declaration."""

    path: list[str]
    text: str
    command: bool


class SourceRecord(TypedDict):
    """Physical source observations, excluding runtime output and symbolic links."""

    path: str
    lines: int | None
    diagnostic: str | None


class ResourceData(TypedDict):
    """Source facts used to build repository overviews."""

    description: str | None
    cli: list[CliRecord]
    tests: list[str]
    dependencies: list[DeclaredDependency]
    sources: list[SourceRecord]
    diagnostics: dict[str, str]


def _source_observations(
    files: dict[str, str],
    errors: dict[str, str],
) -> list[SourceRecord]:
    """Keep per-file observations for the source inventory."""
    sources: list[SourceRecord] = [
        {
            "path": filename,
            "lines": len(source.encode().splitlines()),
            "diagnostic": None,
        }
        for filename, source in sorted(files.items())
    ]
    sources.extend(
        {"path": filename, "lines": None, "diagnostic": message}
        for filename, message in sorted(errors.items())
    )
    sources.sort(key=lambda source: source["path"])
    return sources


def source_resource_data(
    name: str,
    files: dict[str, str],
    *,
    path: str | None = None,
    errors: dict[str, str] | None = None,
) -> ResourceData:
    """Analyze a source snapshot without evaluating Nix or importing Python."""
    path = path or f"packages/{name}"
    nix_filename = "configuration.nix" if path.startswith("hosts/") else "default.nix"
    diagnostics = {}
    dependencies: list[DeclaredDependency] = []
    if nix_source := files.get(nix_filename):
        try:
            dependencies = source_package_dependencies(
                nix_source,
                name,
                path=f"{path}/{nix_filename}",
            )
        except nix_syntax.NixSyntaxError as error:
            diagnostics["dependencies"] = str(error)
    cli: list[CliRecord] = []
    if main_source := files.get("main.py"):
        try:
            module = ast.parse(main_source, filename="main.py")
            cli = [
                {"path": list(entry.path), "text": entry.text, "command": entry.command}
                for entry in _module_cli(module, "main.py")
            ]
        except (SyntaxError, ValueError) as error:
            diagnostics["cli"] = str(error)
    tests: list[str] = []
    if test_source := files.get("test_main.py"):
        try:
            tests = source_test_names(test_source.encode(), "test_main.py")
        except SyntaxError as error:
            diagnostics["tests"] = str(error)
    sources = _source_observations(files, errors or {})
    return {
        "description": source_package_description(files.get(nix_filename, "")),
        "cli": cli,
        "tests": tests,
        "dependencies": dependencies,
        "sources": sources,
        "diagnostics": diagnostics,
    }


def _resource_source_path(filename: str) -> bool:
    """Recognize root sources and source assets under the tracked resource tree."""
    path = Path(filename)
    return (
        path.suffix in SOURCE_SUFFIXES
        and (len(path.parts) == 1 or path.parts[0] == PRM_NAME)
        and not any(part.startswith(".") for part in path.parts)
    )


def resource_data(directory: Path, *, path: str | None = None) -> ResourceData:
    """Read the conventional source inventory, including source assets in prm/."""
    files: dict[str, str] = {}
    errors = {}
    if directory.is_dir() and not directory.is_symlink():
        candidates = list(directory.iterdir())
        resources = directory / PRM_NAME
        if resources.is_dir() and not resources.is_symlink():
            for parent, folders, names in os.walk(resources, followlinks=False):
                folders[:] = [
                    name
                    for name in folders
                    if not name.startswith(".")
                    and not (Path(parent) / name).is_symlink()
                ]
                candidates.extend(Path(parent) / name for name in names)
        for source in sorted(candidates):
            filename = str(source.relative_to(directory))
            if (
                not _resource_source_path(filename)
                or source.is_symlink()
                or not source.is_file()
            ):
                continue
            try:
                files[filename] = source.read_bytes().decode(errors="replace")
            except OSError as error:
                errors[filename] = str(error)
    return source_resource_data(directory.name, files, path=path, errors=errors)


def cli_summary(entries: list[CliRecord]) -> dict[str, Any]:
    """Preserve command nesting and attach arguments to their owning command."""
    tree: dict[str, Any] = {}
    for entry in entries:
        branch = tree
        for name in entry["path"]:
            branch = branch.setdefault("commands", {}).setdefault(name, {})
        if not entry["command"]:
            branch.setdefault("arguments", []).append(entry["text"])
    return tree


def resource_summary(data: ResourceData) -> dict[str, Any]:
    """Keep populated interface and behavior facts for repository overviews."""
    fields = {
        "description": data["description"],
        **cli_summary(data["cli"]),
        "dependencies": [
            _dependency_description(item) for item in data["dependencies"]
        ],
        "tests": data["tests"],
        "diagnostics": data["diagnostics"],
    }
    return {name: value for name, value in fields.items() if value}


def _repository_identity(root: Path) -> tuple[str, ...]:
    """Use a hosted origin or canonical checkout path, with a local fallback."""
    origin = git(root, ["remote", "get-url", "origin"], check=False)
    if origin.returncode == 0:
        with contextlib.suppress(CommandError):
            return canonical_remote_path(origin.stdout.strip()).parts
    domain = root.parent.parent.name
    if "." in domain and not domain.startswith("."):
        return domain, root.parent.name, root.name
    return "local", getpass.getuser(), root.name


def _repository_summary(target: Path) -> dict[str, dict[str, dict[str, Any]]]:
    """Read the selected package and host collections within one repository."""
    if (target / "flake.nix").is_file():
        groups = {
            "packages": {
                package.name: resource_summary(resource_data(package.root))
                for package in detect_packages(target)
            },
            "hosts": {
                source.parent.name: resource_summary(
                    resource_data(source.parent, path=f"hosts/{source.parent.name}"),
                )
                for source in sorted((target / "hosts").glob("*/configuration.nix"))
                if source.is_file()
                and not source.is_symlink()
                and not source.parent.is_symlink()
            },
        }
        if not any(groups.values()):
            msg = f"no packages or hosts found under {target}"
            raise ValueError(msg)
        return {
            collection: resources
            for collection, resources in groups.items()
            if resources
        }
    collection = target.parent.name
    validate = validate_host_name if collection == "hosts" else validate_name
    validate(target.name)
    marker = "configuration.nix" if collection == "hosts" else "default.nix"
    if (
        collection not in {"packages", "hosts"}
        or not (target.parent.parent / "flake.nix").is_file()
        or not (target / marker).is_file()
    ):
        msg = "expected a canonical packages/NAME or hosts/NAME inside a flake"
        raise ValueError(msg)
    return {
        collection: {
            target.name: resource_summary(
                resource_data(target, path=f"{collection}/{target.name}"),
            ),
        },
    }


def home_preservation(root: Path) -> dict[str, Any]:
    """Show Git-governed home paths and discrepancies without reading their contents."""
    source = _read_regular(root / ".gitignore") or ""
    tree = _home_whitelist(source, root / ".gitignore")
    if (root / ".git").exists():
        diagnostics = _home_policy_diagnostics(root, source)
        if diagnostics:
            tree["diagnostics"] = diagnostics
    return tree


def _home_policy_diagnostics(root: Path, source: str) -> dict[str, list[str]]:
    """Report policy drift while leaving Git tracking choices to the user."""
    missing = set()
    for line in source.splitlines():
        if not line.startswith("!/") or any(char in line for char in "*?[\\"):
            continue
        relative = line[2:].rstrip("/")
        path = root / relative
        if not path.exists() and not path.is_symlink():
            missing.add(relative)
    ignored = git(
        root,
        ["ls-files", "--cached", "--ignored", "--exclude-standard", "-z"],
    ).stdout
    diagnostics = {
        "missing_whitelist_paths": sorted(missing),
        "tracked_outside_whitelist": sorted(set(ignored.split("\0")) - {""}),
    }
    return {name: paths for name, paths in diagnostics.items() if paths}


def _compact_filesystem(tree: dict[str, Any]) -> dict[str, Any]:
    """List file names in populated directories containing only file leaves."""
    for name, child in tree.items():
        if name.endswith("/") and isinstance(child, dict):
            _compact_filesystem(child)
            if child and all(value is None for value in child.values()):
                tree[name] = sorted(child)
    return tree


def _filesystem_branch(tree: dict[str, Any], name: str) -> dict[str, Any]:
    """Expand a file list when adding a directory or replacing home contents."""
    child = tree.setdefault(name, {})
    if isinstance(child, list):
        child = dict.fromkeys(child)
        tree[name] = child
    return cast("dict[str, Any]", child)


def _home_whitelist(source: str, path: Path) -> dict[str, Any]:
    """Represent whitelist directories as branches or lists of file leaves."""
    tree: dict[str, Any] = {}
    for line in source.splitlines():
        if not line.startswith("!/"):
            continue
        components = line[2:].rstrip("/").split("/")
        if any(component in {"", ".", ".."} for component in components):
            msg = f"{path}: invalid whitelist path: {line}"
            raise CommandError(msg)
        branch = tree
        for component in components[:-1]:
            branch.pop(component, None)
            name = component + "/"
            branch = branch.setdefault(name, {})
        name = components[-1]
        if line.endswith("/"):
            branch.pop(name, None)
            branch.setdefault(name + "/", {})
        elif name + "/" not in branch:
            branch.setdefault(name, None)
    return _compact_filesystem(tree)


def system_summary() -> dict[str, Any]:
    """Identify the OS before checking the NixOS preservation convention."""
    try:
        release = platform.freedesktop_os_release()
        operating_system = {
            "status": "detected",
            "id": release.get("ID", "unknown"),
            "name": release.get("PRETTY_NAME", release.get("NAME", "unknown")),
        }
    except OSError as error:
        operating_system = {
            "status": "unavailable",
            "id": "unknown",
            "name": platform.system(),
            "message": str(error),
        }
    preservation = {"status": "not_applicable"}
    if operating_system["status"] == "unavailable":
        preservation["message"] = (
            "OS detection failed; system preservation was not inspected."
        )
    elif operating_system["id"] != "nixos":
        preservation["message"] = "System preservation inspection requires NixOS."
    else:
        preservation = _preservation_status(Path("/persistent"))
    return {"os": operating_system, "preservation": preservation}


def _preservation_status(root: Path) -> dict[str, str]:
    """Distinguish absent, inaccessible, and non-directory preservation storage."""
    result = {"path": str(root), "status": "available"}
    try:
        mode = root.stat().st_mode
    except FileNotFoundError:
        result.update(
            status="missing",
            message="System preservation directory is missing.",
        )
    except OSError as error:
        result.update(status="unavailable", message=str(error))
    else:
        if not stat.S_ISDIR(mode):
            result.update(
                status="not_directory",
                message="System preservation path is not a directory.",
            )
    return result


def persistent_summary(root: Path) -> dict[str, Any]:
    """Map stored paths to a logical filesystem tree without following links."""
    tree: dict[str, Any] = {}

    def visit(directory: Path, branch: dict[str, Any], depth: int) -> None:
        try:
            entries = sorted(directory.iterdir())
        except OSError as error:
            branch.setdefault("diagnostics", []).append(error.strerror)
            return
        for entry in entries:
            name = "/" + entry.name if depth == 1 else entry.name
            child: dict[str, Any] = {}
            branch[name] = child
            try:
                mode = entry.lstat().st_mode
            except OSError as error:
                child["diagnostics"] = [error.strerror]
                continue
            if stat.S_ISDIR(mode):
                branch.pop(name)
                branch[name + "/"] = child
                if depth < 3:  # noqa: PLR2004 - compact storage inventory
                    visit(entry, child, depth + 1)
            else:
                branch[name] = None

    visit(root, tree, 1)
    return _compact_filesystem(tree)


def machine_summary() -> dict[str, Any]:
    """Inspect preservation conventions independently of the working directory."""
    home = Path.home()
    system = system_summary()
    filesystem: dict[str, Any] = {}
    if system["preservation"]["status"] == "available":
        filesystem = persistent_summary(Path("/persistent"))
        if "diagnostics" in filesystem:
            system["preservation"].update(
                status="unavailable",
                message="; ".join(filesystem.pop("diagnostics")),
            )
    branch = filesystem
    for index, component in enumerate(home.parts[1:]):
        name = "/" + component if index == 0 else component
        branch = _filesystem_branch(branch, name + "/")
    branch.clear()
    if (home / ".gitmodules").is_file() and not (home / "flake.nix").exists():
        branch.update(overview_summary(home)[socket.gethostname()][str(home.resolve())])
    else:
        branch.update(home_preservation(home))
    return {
        socket.gethostname(): {
            "system": system,
            "filesystem": _compact_filesystem(filesystem),
        },
    }


def _overview_repositories(target: Path) -> list[tuple[Path, Path]]:
    """Select a resource, a flake, or checked-out home flake submodules."""
    repositories = [(target, target)]
    is_home = (target / ".gitmodules").is_file() and not (target / "flake.nix").exists()
    if (
        target.parent.name in {"packages", "hosts"}
        and not (target / "flake.nix").is_file()
    ):
        repositories = [(target.parent.parent, target)]
    elif is_home:
        repositories = []
        for repository in home_submodules(target, require_url=False):
            relative = repository["path"]
            checkout = (target / relative).resolve()
            if not checkout.is_relative_to(target):
                msg = f"submodule path escapes the home repository: {relative}"
                raise CommandError(msg)
            if (checkout / "flake.nix").is_file():
                repositories.append((checkout, checkout))
    return repositories


def overview_summary(target: Path) -> dict[str, Any]:
    """Build the machine, user, domain, owner, repository, and resource tree."""
    target = target.resolve()
    is_home = (target / ".gitmodules").is_file() and not (target / "flake.nix").exists()
    tree: dict[str, Any] = {}
    machine = tree.setdefault(socket.gethostname(), {})
    if is_home:
        user = machine.setdefault(str(target), home_preservation(target))
    else:
        user = machine.setdefault(getpass.getuser(), {})
    for root, selected in _overview_repositories(target):
        branch = user
        components = (
            root.relative_to(target).parts if is_home else _repository_identity(root)
        )
        for component in components:
            if is_home:
                branch.pop(component, None)
            name = component + "/" if is_home else component
            branch = (
                _filesystem_branch(branch, name)
                if is_home
                else branch.setdefault(name, {})
            )
        try:
            groups = _repository_summary(selected)
        except ValueError as error:
            if not is_home:
                raise
            branch["diagnostics"] = [str(error)]
            continue
        for collection, resources in groups.items():
            branch.setdefault(collection, {}).update(resources)
    return tree


def _diff_entries(root: Path, version: str) -> dict[str, tuple[str, str]]:
    """Read stage-zero index entries or HEAD, treating unborn HEAD as empty."""
    if version == "HEAD":
        if git(
            root,
            ["rev-parse", "--verify", "--quiet", "HEAD"],
            check=False,
        ).returncode:
            return {}
        output = git(root, ["ls-tree", "-r", "-z", "HEAD"]).stdout
    else:
        output = git(root, ["ls-files", "--stage", "-z"]).stdout
    entries = {}
    for record in output.split("\0"):
        if not record:
            continue
        metadata, name = record.split("\t", 1)
        mode, middle, last = metadata.split()
        if version == "HEAD":
            oid = last
        else:
            oid = middle
            if last != "0":
                message = f"{root}: unresolved index conflict at {name}"
                raise CommandError(message)
        entries[name] = (mode, oid)
    return entries


def _diff_source_path(name: str) -> bool:
    """Select only files contributing to the semantic overview."""
    parts = Path(name).parts
    return (
        name in {".gitignore", ".gitmodules"}
        or (
            len(parts) >= 3  # noqa: PLR2004 - collection/resource/file
            and parts[0] in {"packages", "hosts"}
            and _resource_source_path(Path(*parts[2:]).as_posix())
        )
    )


def _diff_blobs(root: Path, oids: list[str]) -> dict[str, str]:
    """Read source blobs in one binary-safe Git request without checking them out."""
    if not oids:
        return {}
    completed = subprocess.run(  # noqa: S603
        ["git", "-C", str(root), "cat-file", "--batch"],  # noqa: S607
        input=("\n".join(oids) + "\n").encode(),
        capture_output=True,
        check=False,
    )
    if completed.returncode:
        message = f"{root}: could not read Git source blobs"
        raise CommandError(message)
    stream = io.BytesIO(completed.stdout)
    result = {}
    for oid in oids:
        header = stream.readline().split()
        if len(header) != 3 or header[1] != b"blob":  # noqa: PLR2004 - Git batch header
            message = f"{root}: unavailable Git blob {oid}"
            raise CommandError(message)
        result[oid] = stream.read(int(header[2])).decode(errors="replace")
        stream.read(1)
    return result


def _diff_sources(
    root: Path,
    version: str,
    entries: dict[str, tuple[str, str]],
) -> dict[str, str]:
    """Read tracked working sources or regular files from a Git snapshot."""
    modes = {"100644", "100755"}
    if version == "working_tree":
        modes.add("120000")
    selected = {
        name: oid
        for name, (mode, oid) in entries.items()
        if mode in modes and _diff_source_path(name)
    }
    if version != "working_tree":
        blobs = _diff_blobs(root, sorted(set(selected.values())))
        return {name: blobs[oid] for name, oid in selected.items()}
    sources = {}
    for name in selected:
        path = root / name
        if path.is_symlink() or any(
            parent.is_symlink() for parent in path.parents if parent != root
        ):
            continue
        try:
            if stat.S_ISREG(path.lstat().st_mode):
                sources[name] = path.read_bytes().decode(errors="replace")
        except FileNotFoundError:
            continue
    return sources


def _diff_submodules(
    root: Path,
    version: str,
    entries: dict[str, tuple[str, str]],
    sources: dict[str, str],
) -> list[str]:
    """Read configured checkout paths from the matching working or Git policy."""
    if ".gitmodules" not in sources:
        return []
    selector = (
        ["--file", str(root / ".gitmodules")]
        if version == "working_tree"
        else ["--blob", entries[".gitmodules"][1]]
    )
    completed = git(
        root,
        ["config", *selector, "--null", "--get-regexp", r"^submodule\..*\.path$"],
        check=False,
    )
    if completed.returncode not in {0, 1}:
        message = f"{root}: could not read {version} submodule paths"
        raise CommandError(message)
    paths = sorted(
        {record.split("\n", 1)[1] for record in completed.stdout.split("\0") if record},
    )
    for relative in paths:
        if not (root / relative).resolve().is_relative_to(root):
            message = f"submodule path escapes the home repository: {relative}"
            raise CommandError(message)
    return paths


def _diff_snapshot(root: Path, version: str, scope: Path | None) -> dict[str, Any]:
    """Apply the overview's source analysis to a working, index, or HEAD snapshot."""
    entries = _diff_entries(root, version)
    sources = _diff_sources(root, version, entries)
    if scope is None:
        return {
            "paths": _home_whitelist(
                sources.get(".gitignore", ""),
                root / ".gitignore",
            ),
            "repositories": _diff_submodules(root, version, entries, sources),
        }
    resources: dict[tuple[str, str], dict[str, str]] = {}
    for name, source in sources.items():
        path = Path(name)
        if len(path.parts) < 3 or path.parts[0] not in {"packages", "hosts"}:  # noqa: PLR2004
            continue
        if scope != Path() and not path.is_relative_to(scope):
            continue
        collection, resource = path.parts[:2]
        resources.setdefault((collection, resource), {})[
            Path(*path.parts[2:]).as_posix()
        ] = source
    result: dict[str, Any] = {"packages": {}, "hosts": {}}
    for (collection, name), files in sorted(resources.items()):
        if collection == "hosts" and "configuration.nix" not in files:
            continue
        result.setdefault(collection, {})[name] = resource_summary(
            source_resource_data(name, files, path=f"{collection}/{name}"),
        )
    return result


def _diff_facts(facts: dict[str, Any]) -> dict[str, Any]:
    """Canonicalize unordered declarations while retaining positional meaning."""
    result: dict[str, Any] = {}
    for field, value in facts.items():
        if isinstance(value, dict):
            result[field] = _diff_facts(value)
        elif isinstance(value, list) and field in {
            "tests",
            "dependencies",
            "repositories",
        }:
            result[field] = sorted(value)
        elif isinstance(value, list) and field == "arguments":
            result[field] = [
                item for item in value if not item.startswith("-")
            ] + sorted(item for item in value if item.startswith("-"))
        else:
            result[field] = value
    return result


def _json_diff_patch(
    before: dict[str, Any],
    after: dict[str, Any],
) -> list[dict[str, Any]]:
    """Emit JSON Patch's test/remove/add subset understood by jd's renderer."""
    document = before
    result: list[dict[str, Any]] = []
    for operation in jsonpatch.make_patch(before, after).patch:
        operations: list[dict[str, Any]] = []
        kind, path = operation["op"], operation["path"]
        if kind in {"remove", "replace", "move"}:
            source = operation.get("from", path)
            old = resolve_pointer(document, source)
            operations.extend(
                {"op": action, "path": source, "value": old}
                for action in ("test", "remove")
            )
            if kind in {"replace", "move"}:
                value = old if kind == "move" else operation["value"]
                operations.append({"op": "add", "path": path, "value": value})
        elif kind == "add":
            operations.append(operation)
        document = jsonpatch.apply_patch(document, operations)
        result.extend(operations)
    return result


def diff_summary(target: Path | None, *, cached: bool = False) -> dict[str, Any]:
    """Compare repository facts and report JSON Patch plus inspection errors."""
    before, after = ("HEAD", "index") if cached else ("index", "working_tree")
    selected = Path.home().resolve() if target is None else target.resolve()
    root = repository_root(selected)
    is_home = (
        target is None
        or (root / ".gitmodules").is_file()
        or "!/.gitmodules" in (_read_regular(root / ".gitignore") or "").splitlines()
    )
    scope = None if is_home else selected.relative_to(root)
    targets = [(root, scope)]
    home_snapshots = (
        {version: _diff_snapshot(root, version, None) for version in (before, after)}
        if is_home
        else {}
    )
    if is_home:
        paths = {
            relative
            for snapshot in home_snapshots.values()
            for relative in snapshot["repositories"]
        }
        targets.extend((root / relative, Path()) for relative in sorted(paths))
    old: dict[str, Any] = {}
    new: dict[str, Any] = {}
    diagnostics = []
    for checkout, resource_scope in targets:
        try:
            if repository_root(checkout) != checkout:
                message = f"{checkout}: repository is not checked out"
                raise CommandError(message)  # noqa: TRY301 - report per-checkout failures
            snapshots = (
                home_snapshots
                if checkout == root and is_home
                else {
                    version: _diff_snapshot(checkout, version, resource_scope)
                    for version in (before, after)
                }
            )
            left, right = (
                _diff_facts(snapshots[version]) for version in (before, after)
            )
            if left != right:
                old[str(checkout)], new[str(checkout)] = left, right
        except (CommandError, OSError) as error:  # noqa: PERF203 - inspect remaining repositories
            diagnostics.append(str(error))
    return {
        "patch": _json_diff_patch(old, new),
        "diagnostics": diagnostics,
    }


def _test_target_root(package: Path) -> Path:
    """Validate the canonical target and return its flake root."""
    validate_name(package.name)
    root = package.parent.parent
    if (
        package.parent.name != "packages"
        or not (root / "flake.nix").is_file()
        or not all(
            (package / name).is_file()
            for name in ("default.nix", "main.py", "test_main.py")
        )
    ):
        message = (
            "expected a canonical packages/NAME with default.nix, main.py "
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
    for package in sorted((root / "packages").iterdir()):
        if package.is_dir() and not package.is_symlink():
            shutil.copytree(
                package,
                workspace / "packages" / package.name,
                ignore=ignored,
            )
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
        message = f"command failed ({code}); see {log}"
        raise CommandError(message)


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
    """Select pytest cases and a reproducible subset of source mutations."""

    keywords: str = ""
    markers: str = ""
    lines: tuple[tuple[int, int], ...] = ()
    operators: tuple[str, ...] = ()
    max_mutations: int | None = None
    mutation_plan: Path | None = None

    def pytest_arguments(self) -> list[str]:
        """Render selectors without passing them through a shell."""
        arguments = []
        for option, value in (("-k", self.keywords), ("-m", self.markers)):
            if value:
                arguments.extend([option, value])
        return arguments


def _test_report_source() -> str:
    """Read the pytest plugin shared by coverage and on-demand campaigns."""
    return (Path(__file__).with_name("prm") / "test_report.py").read_text(
        encoding="utf-8",
    )


def _prepare_package_tests(
    workspace: Path,
    name: str,
    environment: TestEnvironment,
    max_examples: int | None = None,
    *,
    selection: TestSelection | None = None,
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
        f"importlib.import_module({'packages.' + name + '.main'!r}).main()\n",
        encoding="utf-8",
    )
    profile = (
        "    from hypothesis import Phase, settings\n"
        '    settings.register_profile("coverage", phases=[Phase.explicit])\n'
        '    settings.load_profile("coverage")\n'
        if max_examples is None
        else "    from hypothesis import settings\n"
        f'    settings.register_profile("ondemand", max_examples={max_examples},'
        " deadline=None)\n"
        '    settings.load_profile("ondemand")\n'
    )
    (workspace / "_afairesi_test_report.py").write_text(
        _test_report_source(),
        encoding="utf-8",
    )
    pytest_arguments = [
        "-p",
        "no:cacheprovider",
        "-p",
        "_afairesi_test_report",
        "-p",
        "_hypothesis_pytestplugin",
        *selection.pytest_arguments(),
    ]
    if max_examples is not None:
        pytest_arguments.extend(
            ["--hypothesis-show-statistics"],
        )
    pytest_arguments.extend(
        ["--import-mode=importlib", "-q", f"packages/{name}/test_main.py"],
    )
    bootstrap = workspace / "run-tests.py"
    bootstrap.write_text(
        "import os, sys\n"
        "from pathlib import Path\n"
        "from tempfile import TemporaryDirectory\n"
        "os.dup2(1, 2)\n"
        f"os.environ['PACKAGE_E2E_EXECUTABLE'] = {str(launcher)!r}\n"
        f"tools = {str(launcher.parent) + os.pathsep + environment.path!r}\n"
        "os.environ['PATH'] = tools + os.pathsep + os.environ.get('PATH', '')\n"
        "os.environ['PYTHONDONTWRITEBYTECODE'] = '1'\n"
        f"os.environ['GIT_CEILING_DIRECTORIES'] = {str(workspace.parent)!r}\n"
        "os.environ.pop('PYTHONPATH', None)\n"
        "os.environ.pop('PYTEST_ADDOPTS', None)\n"
        "os.environ['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'\n"
        "os.environ.pop('PYTEST_PLUGINS', None)\n"
        "os.environ.pop('AFAIRESI_TEST_REPORT_OWNER', None)\n"
        "os.environ.pop('AFAIRESI_TEST_CONTEXT', None)\n"
        f"os.environ['AFAIRESI_TEST_PACKAGE'] = {name!r}\n"
        f"os.environ['AFAIRESI_TEST_REPORT'] = {str(workspace / 'tests.json')!r}\n"
        "os.environ['AFAIRESI_MUTATION_REPORT'] = "
        f"{str(int(max_examples is None))!r}\n"
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
        "try:\n" + profile + "    import pytest\n"
        f"    sys.exit(pytest.main({pytest_arguments!r}))\n"
        "finally:\n"
        "    pid.unlink(missing_ok=True)\n"
        "    runtime.cleanup()\n",
        encoding="utf-8",
    )
    return [environment.python, "-B", str(bootstrap)]


def _mutation_id(mutations: list[dict[str, Any]]) -> str:
    """Identify a source mutation independently of Cosmic Ray's random job ID."""
    encoded = json.dumps(mutations, sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _select_mutations(workspace: Path, name: str, selection: TestSelection) -> None:
    """Filter the initialized session and retain a plan reusable with another suite."""
    from attrs import asdict  # noqa: PLC0415 - only mutation campaigns need the engine
    from cosmic_ray.work_db import WorkDB, use_db  # noqa: PLC0415
    from cosmic_ray.work_item import WorkerOutcome, WorkResult  # noqa: PLC0415

    source_hash = hashlib.sha256(
        (workspace / "packages" / name / "main.py").read_bytes(),
    ).hexdigest()
    replay_ids: set[str] | None = None
    if selection.mutation_plan is not None:
        try:
            plan = json.loads(selection.mutation_plan.read_text(encoding="utf-8"))
            if (
                plan["schema"] != "afairesi.mutation-plan"
                or plan["schema_version"] != 1
                or plan["package"] != name
                or plan["source_sha256"] != source_hash
            ):
                message = "mutation plan does not match the package source"
                raise CommandError(message)
            replay_ids = {entry["id"] for entry in plan["mutations"]}
        except (OSError, ValueError, KeyError, TypeError) as error:
            message = f"invalid mutation plan: {selection.mutation_plan}: {error}"
            raise CommandError(message) from error
    operators = [re.compile(pattern) for pattern in selection.operators]
    with use_db(workspace / "session.sqlite", WorkDB.Mode.open) as database:
        candidates = []
        for item in database.work_items:
            mutations = [asdict(mutation) for mutation in item.mutations]
            for mutation in mutations:
                mutation["module_path"] = str(mutation["module_path"])
            identity = _mutation_id(mutations)
            matches = all(
                (
                    not selection.lines
                    or any(
                        start <= mutation.start_pos[0] <= end
                        for start, end in selection.lines
                    )
                )
                and (
                    not operators
                    or any(
                        pattern.search(mutation.operator_name) for pattern in operators
                    )
                )
                for mutation in item.mutations
            )
            if matches and (replay_ids is None or identity in replay_ids):
                candidates.append((identity, item, mutations))
        candidates.sort(key=lambda candidate: candidate[0])
        if replay_ids is not None and replay_ids != {row[0] for row in candidates}:
            message = (
                "mutation plan contains mutations unavailable "
                "with these filters or engine"
            )
            raise CommandError(message)
        if selection.max_mutations is not None:
            candidates = candidates[: selection.max_mutations]
        selected_jobs = {item.job_id for _, item, _ in candidates}
        if database.num_work_items and not selected_jobs:
            message = "no mutations matched the selection"
            raise CommandError(message)
        database.set_multiple_results(
            [
                item.job_id
                for item in database.work_items
                if item.job_id not in selected_jobs
            ],
            WorkResult(worker_outcome=WorkerOutcome.SKIPPED, output="not selected"),
        )
        manifest = {
            "schema": "afairesi.mutation-plan",
            "schema_version": 1,
            "package": name,
            "source_sha256": source_hash,
            "mutations": [
                {"id": identity, "job_id": item.job_id, "mutations": mutations}
                for identity, item, mutations in candidates
            ],
        }
        (workspace / "mutation-plan.json").write_text(
            json.dumps(manifest, indent=2) + "\n",
            encoding="utf-8",
        )
        sys.stdout.write(
            f"Selected {len(selected_jobs)} of {database.num_work_items} mutations.\n",
        )


def _mutation_status(result: dict[str, Any] | None) -> str:
    """Keep filtered jobs, timeouts, and engine errors out of assertion kills."""
    if result is None:
        return "pending"
    if result["worker_outcome"] == "skipped":
        return "skipped"
    if result["worker_outcome"] != "normal" or result["test_outcome"] == "incompetent":
        return "error"
    if result["output"] == "timeout":
        return "timeout"
    return str(result["test_outcome"])


def _mutation_attribution(result: dict[str, Any] | None) -> dict[str, Any]:
    """Read the pytest report captured in an individual worker's output."""
    if result is not None:
        prefix = "AFAIRESI_TEST_REPORT "
        for line in reversed((result["output"] or "").splitlines()):
            if line.startswith(prefix):
                return cast("dict[str, Any]", json.loads(line.removeprefix(prefix)))
    return {}


def _summarize_mutations(workspace: Path) -> bool:
    """Report engine outcomes without treating survivors as command failures."""
    counts: Counter[str] = Counter()
    survivors: list[str] = []
    mutations = []
    kills: dict[str, list[str]] = {}
    for line in (workspace / "results.jsonl").read_text(encoding="utf-8").splitlines():
        item, result = json.loads(line)
        status = _mutation_status(result)
        counts[status] += 1
        attribution = _mutation_attribution(result)
        identity = _mutation_id(item["mutations"])
        failed_tests = attribution.get("failed_tests", [])
        if status == "killed":
            for nodeid in failed_tests:
                kills.setdefault(nodeid, []).append(identity)
        if status != "skipped":
            mutations.append(
                {
                    "id": identity,
                    "status": status,
                    "mutations": item["mutations"],
                    "failed_tests": failed_tests,
                    "collection_errors": attribution.get("collection_errors", []),
                    "diff": result["diff"] if result is not None else None,
                },
            )
        if status == "survived":
            survivors.append(f"Survived {item['job_id']}:\n{result['diff']}")
    summary = dict(counts)
    (workspace / "summary.json").write_text(
        json.dumps(summary, indent=2) + "\n",
        encoding="utf-8",
    )
    (workspace / "mutation-results.json").write_text(
        json.dumps(
            {
                "schema": "afairesi.mutations",
                "schema_version": 1,
                "summary": summary,
                "mutations": sorted(mutations, key=lambda mutation: mutation["id"]),
                "kills_by_test": {
                    nodeid: sorted(ids) for nodeid, ids in sorted(kills.items())
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    if not counts:
        sys.stdout.write("No mutations generated.\n")
    else:
        statuses = ("killed", "survived", "timeout", "error", "pending", "skipped")
        sys.stdout.write(", ".join(f"{key}: {counts[key]}" for key in statuses) + "\n")
    if survivors:
        sys.stdout.write("\n".join(survivors) + "\n")
    return not (counts["error"] or counts["pending"])


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
    _run_test_command(command, workspace, workspace / "baseline.log", timeout=timeout)
    shutil.copyfile(workspace / "tests.json", workspace / "baseline-tests.json")
    baseline = json.loads((workspace / "baseline-tests.json").read_text())
    if not baseline["summary"].get("passed", 0):
        message = "selected baseline has no passing tests; see baseline-tests.json"
        raise CommandError(message)
    config = workspace / "cosmic-ray.toml"
    config.write_text(
        "[cosmic-ray]\n"
        f"module-path = {json.dumps('packages/' + name + '/main.py')}\n"
        f"timeout = {timeout}\n"
        "excluded-modules = []\n"
        f"test-command = {json.dumps(shlex.join(command))}\n"
        '[cosmic-ray.distributor]\nname = "local"\n',
        encoding="utf-8",
    )
    engine = ["cosmic-ray"]
    session = str(workspace / "session.sqlite")
    _run_test_command(
        [*engine, "init", str(config), session],
        workspace,
        workspace / "init.log",
    )
    _select_mutations(workspace, name, selection)
    sys.stdout.write("Running mutations...\n")
    sys.stdout.flush()
    _run_test_command(
        [*engine, "exec", str(config), session],
        workspace,
        workspace / "engine.log",
    )
    _run_test_command(
        [*engine, "dump", session],
        workspace,
        workspace / "results.jsonl",
    )
    _run_test_command(
        ["cr-html", session],
        workspace,
        workspace / "report.html",
    )
    return _summarize_mutations(workspace)


def _run_test_package(
    package: Path,
    command: str,
    timeout: float | None,
    max_examples: int | None,
    selection: TestSelection,
) -> bool:
    """Run one isolated package and retain its logs and reports."""
    root = _test_target_root(package)
    tmp = root / "tmp"
    tmp.mkdir(exist_ok=True)
    workspace = Path(
        tempfile.mkdtemp(prefix=f"python-{command}-{package.name}-", dir=tmp),
    )
    label = (
        "Mutation workspace and reports"
        if command == "mutation"
        else "Hypothesis workspace and logs"
    )
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
        max_examples,
        selection=selection,
    )
    log = workspace / "tests.log"
    try:
        _run_test_command(arguments, workspace, log, timeout=timeout)
    finally:
        if log.exists():
            sys.stdout.write(log.read_text(encoding="utf-8"))
    return True


def _run_test_repository(
    root: Path,
    command: str,
    timeout: float | None,
    max_examples: int | None,
    selection: TestSelection,
) -> bool:
    """Run each Python package, continuing after failures and summarizing results."""
    directory = root / "packages"
    packages = (
        sorted(
            path
            for path in directory.iterdir()
            if path.is_dir() and not path.is_symlink() and (path / "main.py").is_file()
        )
        if directory.is_dir()
        else []
    )
    if not packages:
        message = f"no Python packages found under {directory}"
        raise CommandError(message)
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
                if _run_test_package(package, command, timeout, max_examples, selection)
                else "failed"
            )
        except (CommandError, OSError, subprocess.TimeoutExpired) as error:
            outcomes[package.name] = "failed"
            sys.stderr.write(f"afairesi test {command}: {package.name}: {error}\n")
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
    return "failed" not in outcomes.values()


def _dispatch_test_runner(
    options: argparse.Namespace,
    cli: argparse.ArgumentParser,
) -> bool:
    """Validate budgets and run an explicitly requested test campaign."""
    max_examples = getattr(options, "max_examples", None)
    if max_examples is not None and max_examples <= 0:
        cli.error("--max-examples must be positive")
    if options.timeout is not None and (
        not math.isfinite(options.timeout) or options.timeout <= 0
    ):
        cli.error("--timeout must be positive and finite")
    mutation_limit = getattr(options, "max_mutations", None)
    if mutation_limit is not None and mutation_limit <= 0:
        cli.error("--max-mutations must be positive")
    operators = getattr(options, "operators", ())
    try:
        for pattern in operators:
            re.compile(pattern)
    except re.error as error:
        cli.error(f"invalid --operator expression: {error}")
    selection = TestSelection(
        keywords=options.keywords,
        markers=options.markers,
        lines=tuple(getattr(options, "lines", ())),
        operators=tuple(operators),
        max_mutations=mutation_limit,
        mutation_plan=getattr(options, "mutation_plan", None),
    )
    target = _command_target(options.target)
    runner = (
        _run_test_repository if (target / "flake.nix").is_file() else _run_test_package
    )
    return runner(
        target,
        options.test_command,
        options.timeout,
        max_examples,
        selection,
    )


def _flake_installable(root: Path, attribute: str) -> str:
    """Select a named output while retaining Git's tracked-source filtering."""
    return f"git+{root.as_uri()}#{attribute}"


def _build_package_coverage(package: Path) -> None:
    """Build an instrumented variant of the test check and print its report path."""
    root = _test_target_root(package)
    check = root / "checks" / package.name / "default.nix"
    if not check.is_file():
        message = f"missing {check}; run afairesi converge to generate checks"
        raise CommandError(message)
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
        check=False,
    )
    if completed.returncode:
        message = f"coverage check failed for {package.name}"
        raise CommandError(message)
    outputs = completed.stdout.splitlines()
    if len(outputs) != 1:
        message = f"expected one coverage output for {package.name}"
        raise CommandError(message)
    report = Path(outputs[0]) / "html" / "index.html"
    if not report.is_file():
        message = (
            f"coverage check produced no HTML report for {package.name}; "
            "run afairesi converge to update the test checks"
        )
        raise CommandError(message)
    sys.stdout.write(f"{package.name}: {report}\n")
    test_report = Path(outputs[0]) / "tests.json"
    if not test_report.is_file():
        message = f"coverage check produced no test report for {package.name}"
        raise CommandError(message)
    summary = json.loads(test_report.read_text(encoding="utf-8"))["summary"]
    sys.stdout.write(
        f"{package.name}: {summary['selected_cases']} cases, "
        f"{summary.get('skipped', 0)} skipped; {test_report}\n",
    )
    if summary["unexecuted_properties"]:
        sys.stdout.write(
            f"{package.name}: properties with no body executions: "
            + ", ".join(summary["unexecuted_properties"])
            + "\n",
        )


def _run_coverage(target: Path) -> bool:
    """Build package checks independently, preserving caching and reporting failures."""
    repository = (target / "flake.nix").is_file()
    if repository:
        directory = target / "packages"
        packages = (
            sorted(
                path
                for path in directory.iterdir()
                if path.is_dir()
                and not path.is_symlink()
                and (path / "main.py").is_file()
            )
            if directory.is_dir()
            else []
        )
        if not packages:
            message = f"no Python packages found under {directory}"
            raise CommandError(message)
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
        except (CommandError, OSError) as error:
            outcomes[package.name] = "failed"
            sys.stderr.write(f"afairesi test coverage: {package.name}: {error}\n")
    if repository:
        sys.stdout.write("\nRepository summary:\n")
        for name, status in outcomes.items():
            sys.stdout.write(f"  {name}: {status}\n")
        sys.stdout.write(
            ", ".join(
                f"{sum(value == status for value in outcomes.values())} {status}"
                for status in ("passed", "failed", "skipped")
            )
            + "\n",
        )
    return "failed" not in outcomes.values()


def _mutation_lines(value: str) -> tuple[int, int]:
    """Validate inclusive source line ranges before a campaign creates state."""
    if not re.fullmatch(r"[0-9]+(?::[0-9]+)?", value):
        message = "expected a positive line number or START:END"
        raise argparse.ArgumentTypeError(message)
    first, _, last = value.partition(":")
    start, end = int(first), int(last or first)
    if not 0 < start <= end:
        message = "expected 0 < START <= END"
        raise argparse.ArgumentTypeError(message)
    return start, end


def _inspection_path(value: str) -> Path:
    """Require an explicit inspection target to be an existing directory."""
    path = Path(value)
    if not path.is_dir():
        message = f"not a directory: {value}"
        raise argparse.ArgumentTypeError(message)
    return path


def parser(*, include_target: bool = False) -> argparse.ArgumentParser:
    """Construct the public command-line parser."""
    result = argparse.ArgumentParser(
        prog="afairesi",
        description=(
            "Create, inspect, and converge Git and Nix repositories "
            "describing machines. Without a command, show "
            "OS and preservation status, preserved system paths, the Git-governed "
            "home whitelist, and repository descriptions, CLI arguments, "
            "dependencies, tests, and diagnostics as formatted JSON."
        ),
        usage="%(prog)s [-h] [PATH]\n       %(prog)s COMMAND ...",
        epilog=(
            "PATH selects a home, flake root, package, or host; default: whole "
            "machine. Use afairesi . to inspect a repository or afairesi "
            "packages/NAME to inspect a package. Home diagnostics report missing "
            "literal whitelist paths and tracked files excluded by the whitelist; "
            "patterns are not expanded and Git tracking choices remain explicit. "
            "Read tests or source code when more detail is needed."
        ),
    )
    result.set_defaults(target=None)
    if include_target:
        result.add_argument(
            "target",
            nargs="?",
            type=_inspection_path,
            metavar="PATH",
            help=(
                "home, flake root, packages/NAME, or hosts/NAME "
                "(default: whole machine)"
            ),
        )
    if include_target:
        result.set_defaults(command=None)
        return result
    commands = result.add_subparsers(
        dest="command",
        title="commands",
        metavar="COMMAND",
    )
    init = commands.add_parser(
        "init",
        help="initialize HOME, create a flake, or add a remote submodule",
        description="Initialize HOME, create a flake, or add a remote under HOME.",
    )
    init.add_argument(
        "repository_type",
        metavar="home|flake|REMOTE",
        help="home or flake repository type, or a hosted Git remote for a submodule",
    )
    init.add_argument(
        "remote",
        nargs="?",
        metavar="REMOTE",
        help="empty hosted Git remote required by the flake repository type",
    )
    add = commands.add_parser(
        "add",
        help="add a package or host",
        description="Create and stage a canonical package or host.",
        epilog="Update and stage the repository whitelist and related checks.",
    )
    add.add_argument(
        "resource",
        metavar="RESOURCE",
        help="new packages/NAME or hosts/NAME path",
    )
    add.add_argument(
        "type",
        nargs="?",
        metavar="TYPE",
        help="package type (html, latex, nix, python)",
    )
    add.add_argument(
        "description",
        nargs="*",
        metavar="DESCRIPTION",
        help="optional package description",
    )
    move = commands.add_parser(
        "mv",
        help="rename a package or host",
        description="Rename a canonical package or host and stage the result.",
        epilog="Update and stage the repository whitelist and related checks.",
    )
    move.add_argument(
        "source",
        metavar="SOURCE",
        help="existing packages/NAME or hosts/NAME path",
    )
    move.add_argument(
        "destination",
        metavar="DESTINATION",
        help="new path in the same resource collection",
    )
    move.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="print moves without changing the repository",
    )
    remove = commands.add_parser(
        "rm",
        help="remove a home submodule, package, or host",
        description="Remove and stage a registered home submodule or flake resource.",
        epilog=(
            "Home removal uses git rm and removes the submodule's whitelist "
            "entries while preserving shared parents. Git rejects local changes "
            "that would be lost. Flake removal updates the whitelist and removes "
            "related checks."
        ),
    )
    remove.add_argument(
        "resource",
        metavar="RESOURCE",
        help="registered home submodule path, packages/NAME, or hosts/NAME",
    )
    remove.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="print removals without changing the repository",
    )
    converge = commands.add_parser(
        "converge",
        help="converge the home or flake repository to its target layout",
        description=(
            "Converge the current home or flake repository to its target layout."
        ),
        epilog=(
            "Home convergence synchronizes submodule paths, URLs, and whitelist "
            "entries, migrating entries when submodules move. Flake convergence "
            "repairs managed files, checks, and the whitelist, and removes "
            "undeclared files while preserving permitted tmp/ output. "
            "Git governs home state; preservation governs system state outside "
            "HOME. Reboot to restore impermanent NixOS system state."
        ),
    )
    converge.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="report required actions without changing the repository",
    )
    converge.add_argument("--source", type=Path, help=argparse.SUPPRESS)
    diff = commands.add_parser(
        "diff",
        help="compare overview facts with the Git index or HEAD",
        description="Emit JSON Patch for tracked overview facts.",
        epilog=(
            "Report descriptions, CLI arguments, dependencies, tests, and "
            "diagnostics, with packages and hosts reported individually. Lists "
            "show added and removed entries. Test, dependency, and named-option "
            "declaration order is ignored; positional argument order is preserved. "
            "System state has no Git baseline and is excluded from comparison. "
            "Pipe the patch to jd -t patch2jd for a visual diff. "
            "Patch paths start with the absolute repository path; unordered "
            "declaration lists are sorted in the comparison document. "
            "Diagnostics and exclusions are written to stderr."
        ),
    )
    diff.add_argument(
        "target",
        nargs="?",
        type=_inspection_path,
        metavar="PATH",
        help="home, repository, package, or host (default: whole machine)",
    )
    diff.add_argument(
        "--cached",
        "--staged",
        action="store_true",
        help="compare the index with HEAD instead of working files with the index",
    )
    test = commands.add_parser(
        "test",
        help="run coverage, property tests, and mutation tests",
        description=(
            "Run coverage, Hypothesis, and mutation campaigns for the current "
            "flake repository or all registered submodules of the current home "
            "repository. Select a subcommand to run one campaign."
        ),
    )
    test.set_defaults(test_command=None)
    test_commands = test.add_subparsers(dest="test_command", metavar="COMMAND")
    coverage = test_commands.add_parser(
        "coverage",
        help="measure test coverage and print HTML report paths",
        description="Measure coverage from explicit test examples.",
        epilog=(
            "Repository targets skip packages without tests and summarize results. "
            "HTML reports are stored in the Nix store. "
            "Use converge to create or update checks."
        ),
    )
    coverage.add_argument(
        "target",
        type=Path,
        nargs="?",
        default=None,
        metavar="PATH",
        help="package, flake, or home (default: current repository and its submodules)",
    )
    hypothesis = test_commands.add_parser(
        "hypothesis",
        help="run generated property tests in isolated package copies",
        description="Run generated property tests in isolated package copies.",
        epilog=(
            "Repository targets run Python packages sequentially, skip packages "
            "without test_main.py, and summarize results. Logs and reports "
            "are retained under the flake's tmp/ directory."
        ),
    )
    hypothesis.add_argument(
        "target",
        type=Path,
        nargs="?",
        default=None,
        metavar="PATH",
        help="package, flake, or home (default: current repository and its submodules)",
    )
    hypothesis.add_argument(
        "--timeout",
        type=float,
        help=(
            "seconds per test-suite invocation, excluding environment build "
            "(default: unlimited)"
        ),
    )
    hypothesis.add_argument(
        "--max-examples",
        type=int,
        default=100,
        help="successful generated examples per property (default: 100)",
    )
    mutation = test_commands.add_parser(
        "mutation",
        help="run mutation tests in isolated package copies",
        description="Run mutation tests in isolated package copies.",
        epilog=(
            "Repository targets run Python packages sequentially, skip packages "
            "without test_main.py, and summarize results. Logs and reports "
            "are retained under the flake's tmp/ directory."
        ),
    )
    mutation.add_argument(
        "target",
        type=Path,
        nargs="?",
        default=None,
        metavar="PATH",
        help="package, flake, or home (default: current repository and its submodules)",
    )
    mutation.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        help=(
            "seconds per test-suite invocation, excluding environment build "
            "(default: 300)"
        ),
    )
    hypothesis.add_argument(
        "-k",
        dest="keywords",
        default="",
        help="run pytest cases matching this keyword expression",
    )
    hypothesis.add_argument(
        "-m",
        dest="markers",
        default="",
        help="run pytest cases matching this marker expression",
    )
    mutation.add_argument(
        "-k",
        dest="keywords",
        default="",
        help="run pytest cases matching this keyword expression",
    )
    mutation.add_argument(
        "-m",
        dest="markers",
        default="",
        help="run pytest cases matching this marker expression",
    )
    mutation.add_argument(
        "--lines",
        type=_mutation_lines,
        action="append",
        default=[],
        metavar="START:END",
        help=(
            "select mutations starting in this inclusive "
            "main.py line range (repeatable)"
        ),
    )
    mutation.add_argument(
        "--operator",
        dest="operators",
        action="append",
        default=[],
        metavar="REGEX",
        help="select mutation operators matching a regular expression (repeatable)",
    )
    mutation.add_argument(
        "--max-mutations",
        type=int,
        metavar="N",
        help="maximum mutations to test",
    )
    mutation.add_argument(
        "--mutation-plan",
        type=Path,
        metavar="PATH",
        help=(
            "reuse a mutation-plan.json against the same "
            "package source and different tests"
        ),
    )
    return result


def _run_test_targets(
    campaign: argparse.Namespace,
    cli: argparse.ArgumentParser,
    target: Path,
    targets: list[Path],
) -> bool:
    """Run one campaign across selected repositories and collect every failure."""
    command = campaign.test_command
    results: dict[Path, bool] = {}
    for selected in targets:
        selected_options = argparse.Namespace(**vars(campaign))
        selected_options.target = selected
        if selected != target:
            sys.stdout.write(f"\nRunning {command} for {selected}...\n")
            sys.stdout.flush()
        try:
            results[selected] = (
                _run_coverage(selected)
                if command == "coverage"
                else _dispatch_test_runner(selected_options, cli)
            )
        except (CommandError, OSError, subprocess.TimeoutExpired) as error:
            results[selected] = False
            sys.stderr.write(f"afairesi test {command}: {selected}: {error}\n")
        except KeyboardInterrupt:
            sys.stderr.write(f"afairesi test {command}: interrupted\n")
            sys.exit(130)
    if targets != [target]:
        sys.stdout.write(f"\nSubmodule summary ({command}):\n")
        for selected, success in results.items():
            sys.stdout.write(
                f"  {selected.relative_to(target)}: "
                f"{'passed' if success else 'failed'}\n",
            )
    return all(results.values())


def _dispatch_test_command(
    options: argparse.Namespace,
    cli: argparse.ArgumentParser,
) -> bool:
    """Run selected campaigns, continuing after failures and returning one status."""
    combined = options.test_command is None
    target = _command_target(options.target)
    targets = _test_targets(target)
    if combined:
        campaigns = [
            cli.parse_args(["test", command, str(target)])
            for command in ("coverage", "hypothesis", "mutation")
        ]
    else:
        campaigns = [options]
    outcomes: dict[str, bool] = {}
    for campaign in campaigns:
        command = campaign.test_command
        if combined:
            sys.stdout.write(f"\nRunning {command} campaign...\n")
            sys.stdout.flush()
        outcomes[command] = _run_test_targets(campaign, cli, target, targets)
    if combined:
        sys.stdout.write("\nCampaign summary:\n")
        for command, success in outcomes.items():
            sys.stdout.write(f"  {command}: {'passed' if success else 'failed'}\n")
    sys.exit(0 if all(outcomes.values()) else 1)


def _test_targets(target: Path) -> list[Path]:
    """Expand a home target into its registered submodules without changing them."""
    flake, home = _repository_type_markers(target)
    if not home or flake:
        return [target]
    targets = []
    for repository in home_submodules(target, require_url=False):
        checkout = (target / repository["path"]).resolve()
        if not checkout.is_relative_to(target) or checkout == target:
            message = (
                f"submodule path escapes the home repository: {repository['path']}"
            )
            raise CommandError(message)
        targets.append(checkout)
    if not targets:
        message = f"no submodules found under {target}"
        raise CommandError(message)
    return targets


def _dispatch_overview(options: argparse.Namespace) -> None:
    """Emit the selected machine or repository overview as formatted JSON."""
    tree = (
        machine_summary()
        if options.target is None
        else overview_summary(options.target)
    )
    sys.stdout.write(json.dumps(tree, indent=2, sort_keys=True) + "\n")


def _dispatch_diff(options: argparse.Namespace) -> None:
    """Emit JSON Patch and report incomplete comparisons separately on stderr."""
    data = diff_summary(options.target, cached=options.cached)
    sys.stdout.write(json.dumps(data["patch"], indent=2, sort_keys=True) + "\n")
    if options.target is None:
        sys.stderr.write(
            "Not compared: OS metadata and stored system paths have no Git baseline.\n",
        )
    for message in data["diagnostics"]:
        sys.stderr.write(f"error: {message}\n")
    if data["diagnostics"]:
        raise SystemExit(1)


def _dispatch_standalone_command(
    options: argparse.Namespace,
    cli: argparse.ArgumentParser,
) -> bool:
    """Dispatch inspection, testing, and initialization commands."""
    if options.command == "test":
        return _dispatch_test_command(options, cli)
    if options.command == "diff":
        _dispatch_diff(options)
        return True
    if options.command is None:
        _dispatch_overview(options)
        return True
    if options.command != "init":
        return False
    if options.repository_type == "home":
        if options.remote is not None:
            msg = "init home does not accept a remote"
            raise CommandError(msg)
        initialize_home()
    elif options.repository_type == "flake":
        if options.remote is None:
            msg = "init flake requires REMOTE"
            raise CommandError(msg)
        initialize_flake(options.remote)
    else:
        if options.remote is not None:
            cli.error("init REMOTE accepts exactly one remote")
        initialize_submodule(options.repository_type)
    return True


def _dispatch_add(root: Path, options: argparse.Namespace) -> None:
    """Create the selected package or host resource."""
    kind, name, _relative = _parse_resource_path(options.resource)
    description = " ".join(options.description) or None
    if kind == "host":
        if options.type is not None or description is not None:
            msg = "host creation does not accept a type or description"
            raise CommandError(msg)
        add_host(root, name)
        return
    if options.type is None:
        msg = "package creation requires TYPE"
        raise CommandError(msg)
    add_package(root, options.type, name, description)


def _dispatch_remove(root: Path, options: argparse.Namespace, kind: str) -> None:
    """Remove a resource through its repository's lifecycle."""
    if kind == "home":
        remove_home_submodule(root, options.resource, dry_run=options.dry_run)
    else:
        remove_resource(root, options.resource, options.dry_run)


def main() -> None:
    """Dispatch the Afairesi CLI."""
    arguments = sys.argv[1:]
    try:
        cli = parser()
        commands = next(
            action.choices
            for action in cli._actions  # noqa: SLF001 - inspect argparse's command names
            if isinstance(action, argparse._SubParsersAction)  # noqa: SLF001
        )
        if not arguments or arguments[0] not in {*commands, "-h", "--help"}:
            cli = parser(include_target=True)
        options = cli.parse_args(arguments)
        if _dispatch_standalone_command(options, cli):
            return
        if options.command == "converge" and options.source is not None:
            validate_flake_source(options.source.resolve())
            return
        root = repository_root()
        current_type = repository_type(root)
        if options.command in {"add", "mv"} and current_type != "flake":
            msg = f"{current_type} repositories do not support flake resources"
            raise CommandError(  # noqa: TRY301
                msg,
            )
        if options.command == "converge":
            converge_home(
                root,
                options.dry_run,
            ) if current_type == "home" else converge_flake(
                root,
                options.dry_run,
            )
        elif options.command == "add":
            _dispatch_add(root, options)
        elif options.command == "rm":
            _dispatch_remove(root, options, current_type)
        elif options.command == "mv":
            rename_resource(
                root,
                options.source,
                options.destination,
                options.dry_run,
            )
    except (
        CommandError,
        OSError,
        SyntaxError,
        UnicodeError,
        ValueError,
        json.JSONDecodeError,
        nix_syntax.NixSyntaxError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)  # noqa: T201
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()

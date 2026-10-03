#!/usr/bin/env python3
# Copyright (c) 2026- Paschalis Bizopoulos
"""Canonicalize home repositories and manage canonical flake repositories."""

from __future__ import annotations

import argparse
import ast
import contextlib
import io
import json
import math
import os
import posixpath
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import tokenize
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Any, TypedDict, cast
from urllib.parse import urlparse

import nix_syntax

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
OPAQUE_NAME = "prm"
SCRATCH_NAME = "tmp"
RESOURCE_SOURCE_DEPTH = 3
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


def profile(root: Path, default: str | None = None) -> str:
    """Detect home/submodule and flake repository layouts."""
    flake, home = _profile_markers(root)
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
        "'git canonical init home' or "
        "'git canonical init flake REMOTE'"
    )
    raise CommandError(msg)


def _profile_markers(root: Path) -> tuple[bool, bool]:
    """Share layout recognition between inspection and lifecycle commands."""
    flake = any(
        (root / marker).exists()
        for marker in ("flake.nix", "flake.lock", "packages", "checks", "hosts")
    )
    gitignore = _read_regular(root / ".gitignore") or ""
    home = (root / ".gitmodules").exists() or "!/.gitmodules" in gitignore.splitlines()
    return flake, home


def canonical_root(directory: Path) -> Path:
    """Find the nearest Canonical layout, or retain an ordinary directory."""
    directory = directory.resolve()
    for candidate in (directory, *directory.parents):
        if any(_profile_markers(candidate)):
            profile(candidate)
            return candidate
    return directory


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
    """Build a native Git clean command with profile-selected exclusions."""
    arguments = ["clean", "-ndx" if dry_run else "-fdx"]
    for exclusion in exclusions:
        arguments.extend(("-e", exclusion))
    return arguments


def _flake_clean_arguments(*, dry_run: bool) -> list[str]:
    """Build the flake cleanup command."""
    return _clean_arguments(
        dry_run=dry_run,
        exclusions=(f"/{SCRATCH_NAME}/", f"/packages/*/{SCRATCH_NAME}/"),
    )


def hosted_remote(remote: str) -> tuple[str, str]:
    """Parse URL- and SCP-style hosted Git remotes."""
    parsed = urlparse(remote)
    if (
        parsed.scheme in {"http", "https", "ssh", "git+ssh", "git"}
        and parsed.hostname
        and parsed.path.strip("/")
    ):
        return parsed.hostname.lower(), parsed.path.strip("/")
    match = re.fullmatch(r"(?:[^/@:]+@)?([^/:]+):(.+)", remote)
    if match:
        return match.group(1).lower(), match.group(2).rstrip("/")
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


def home_repositories(
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
            r"^submodule\..*",
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
        if not required.issubset(fields) or set(fields) - {"path", "url"}:
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


def _converge_home_repository(
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
        changed |= _converge_home_checkout(
            root,
            checkout,
            expected,
            repository["url"],
            dry_run=dry_run,
        )
    return changed


def _converge_home_checkout(
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


def check_home(root: Path, dry_run: bool) -> list[dict[str, str]]:  # noqa: FBT001
    """Converge a canonical home repository."""
    repositories = home_repositories(root)
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
        changed |= _converge_home_repository(
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
    if "main.py" in markers:
        matches = [kind for kind in matches if kind != "latex"]
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


def opaque_trees(root: Path) -> set[Path]:
    """Return existing repository trees whose contents are unrestricted."""
    candidates = {Path("prm")}
    for parent in ("hosts", "packages"):
        base = root / parent
        if base.is_dir():
            for child in base.iterdir():
                if child.is_dir():
                    candidates.add(Path(parent) / child.name / OPAQUE_NAME)
    return {path for path in candidates if (root / path).is_dir()}


def scratch_trees(root: Path) -> set[Path]:
    """Return permitted untracked scratch trees."""
    candidates = {Path(SCRATCH_NAME)}
    packages = root / "packages"
    if packages.is_dir():
        candidates.update(
            Path("packages") / child.name / SCRATCH_NAME
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
        render_gitignore(allowed_paths(root, packages), opaque_trees(root)),
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
        if issue := _python_test_placement_issue(package):
            issues.append(issue)
        for relative in sorted(required_package_files(package)):
            if not (root / relative).is_file():
                issues.extend([f"{relative}: missing required regular file"])
    opaque = opaque_trees(root)
    scratch = scratch_trees(root)
    for path in _structure_paths(root, opaque | scratch | {Path(".git")}):
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
    """Detect the same static tests reported by the test names command."""
    try:
        return bool(source_test_names(path.read_bytes(), str(path)))
    except (OSError, SyntaxError, UnicodeError) as error:
        msg = f"{path}: Python source could not be parsed: {error}"
        raise CommandError(
            msg,
        ) from error


def package_description(package: Package) -> str | None:
    """Extract declared package metadata where supported."""
    default = _read_regular(package.root / "default.nix")
    return source_package_description(default) if default is not None else None


def source_package_description(source: str) -> str | None:
    """Extract a literal Nix package description without evaluation."""
    with contextlib.suppress(json.JSONDecodeError, nix_syntax.NixSyntaxError):
        return _meta_description(source)
    return None


def _meta_description(source: str) -> str | None:
    """Return a literal meta.description through the Nix syntax tree."""
    document, expression = _metadata_expression(source, "meta", ("description",))
    if expression is None or expression.type != "string_expression":
        return None
    if any(node.type == "interpolation" for node in nix_syntax.walk(expression)):
        return None
    decoded = json.loads(document.text(expression).replace(r"\${", "${"))
    return cast("str", decoded)


def _nix_string(value: str) -> str:
    """Encode a non-interpolating Nix string literal."""
    return json.dumps(value).replace("${", r"\${")


def _attrset_expression(
    document: nix_syntax.Document,
    expression: Node,
    path: tuple[str, ...],
) -> list[Node]:
    """Find direct static bindings beneath an attribute-set expression."""
    if expression.type != "attrset_expression":
        return []
    binding_set = next(
        (child for child in expression.named_children if child.type == "binding_set"),
        None,
    )
    return [
        value
        for binding in ([] if binding_set is None else binding_set.named_children)
        if binding.type == "binding"
        and (attrpath := nix_syntax.field(binding, "attrpath")) is not None
        and nix_syntax.static_attrpath(document, attrpath) == path
        and (value := nix_syntax.field(binding, "expression")) is not None
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
        attrpath = nix_syntax.field(binding, "attrpath")
        expression = nix_syntax.field(binding, "expression")
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
    """Render the ordinary check that runs explicit examples."""
    return """{ inputs, pkgs, ... }:
let
  packageDrv = inputs.self.packages.${pkgs.stdenv.system}.${packageName};
  packageName = baseNameOf ./.;
  pythonEnv = packageDrv.python.withPackages (
    ps:
    packageDrv.propagatedBuildInputs
    ++ (packageDrv.buildInputs or [ ])
    ++ [
      ps.hypothesis
      ps.pytest
    ]
  );
in
pkgs.runCommand packageName
  (
    {
      inherit (packageDrv) src;
      nativeBuildInputs =
        (packageDrv.nativeBuildInputs or [ ]) ++ packageDrv.propagatedBuildInputs ++ [ pythonEnv ];
    }
    // pkgs.lib.optionalAttrs (packageDrv.meta ? mainProgram) {
      PACKAGE_E2E_EXECUTABLE = pkgs.lib.getExe packageDrv;
    }
  )
  ''
    export src PACKAGE_E2E_EXECUTABLE
    export HOME="$(mktemp -d)"
    mkdir -p "$out" packages
    ln -s "$src" "packages/${packageName}"
    export PYTHONPATH="$PWD:$PYTHONPATH"
    cd "$out"
    "${pythonEnv}/bin/python" - <<'PYTHON'
    import os
    import sys
    from hypothesis import Phase, settings
    settings.register_profile("explicit", phases=[Phase.explicit])
    settings.load_profile("explicit")
    import pytest
    sys.exit(pytest.main([
        "-p", "no:cacheprovider",
        "--import-mode=importlib",
        os.environ["src"] + "/test_main.py",
    ]))
    PYTHON
  ''
"""  # noqa: E501


def _current_host_check_source() -> str:
    """Render the canonical host boot, persistence, and bootstrap check."""
    return r"""{
  inputs,
  pkgs,
  ...
}:
let
  inherit (pkgs) lib;
  configuration = inputs.self.nixosConfigurations.${host};
  hostConfig = configuration.config;
  hasPreservation = hostConfig.preservation.enable or false;
  hasBootstrap = hasPreservation && hostConfig.services.openssh.enable;
  hasAge = hasBootstrap && builtins.attrNames (hostConfig.age.secrets or { }) != [ ];
  storage = lib.mapAttrsToList (path: state: {
    inherit path;
    files = state.files;
    directories = state.directories;
  }) (hostConfig.preservation.preserveAt or { });
  preservedFiles = lib.concatMap (state: map (file: {
    inherit (file) file how;
    persistent = state.path + file.file;
  }) state.files) storage;
  preservedDirectories = lib.concatMap (state: map (directory: {
    inherit (directory) directory how;
    persistent = state.path + directory.directory;
  }) state.directories) storage;
  keys = lib.imap0 (index: key:
    let
      files = builtins.filter (file:
        key.path == file.file || key.path == file.persistent
      ) preservedFiles;
      directories = lib.sort (a: b:
        builtins.stringLength a.directory > builtins.stringLength b.directory
      ) (builtins.filter (directory:
        lib.hasPrefix (directory.directory + "/") key.path
        || lib.hasPrefix (directory.persistent + "/") key.path
      ) preservedDirectories);
      preserved =
        if files != [ ] then (builtins.head files).persistent
        else if directories != [ ] then
          let directory = builtins.head directories;
          in if lib.hasPrefix (directory.persistent + "/") key.path then key.path
          else directory.persistent + lib.removePrefix directory.directory key.path
        else throw "Canonical bootstrap check: SSH host key ${key.path} is not preserved";
    in key // { inherit index preserved; }
  ) hostConfig.services.openssh.hostKeys;
  identityKeys = builtins.filter (key:
    builtins.elem key.type [ "rsa" "ed25519" ]
    && (builtins.elem key.path hostConfig.age.identityPaths
      || builtins.elem key.preserved hostConfig.age.identityPaths)
  ) keys;
  fixture = pkgs.runCommand "${host}-disposable-bootstrap-identities" {
    nativeBuildInputs = [ pkgs.openssh ] ++ lib.optional hasAge pkgs.age;
  } ''
    mkdir -p "$out"
    ${lib.concatMapStrings (key: ''
      ssh-keygen -q -t ${lib.escapeShellArg key.type} \
        ${lib.optionalString (key ? bits) "-b ${toString key.bits}"} \
        -N "" -C disposable-canonical-test -f "$out/key-${toString key.index}"
    '') keys}
    ${lib.optionalString hasAge ''
      ${assert lib.assertMsg (identityKeys != [ ])
        "Canonical bootstrap check: agenix needs a preserved SSH host identity"; ""}
      printf 'canonical-bootstrap-ok\n' | age \
        ${lib.concatMapStringsSep " " (key: "-R \"$out/key-${toString key.index}.pub\"") identityKeys} \
        -o "$out/probe.age"
    ''}
  '';
  bootstrapNode = seeded: { ... }: {
    imports = [ inputs.preservation.nixosModules.default ]
      ++ lib.optional hasAge inputs.agenix.nixosModules.default;
    boot.initrd.systemd = {
      inherit (hostConfig.boot.initrd.systemd) enable;
      storePaths = [ fixture ];
    };
    preservation = {
      enable = true;
      preserveAt = lib.mapAttrs (path: state: {
        files = map (file: {
          inherit (file) configureParent createLinkTarget file group how
            inInitrd mode mountOptions parent user;
        }) (builtins.filter (file: lib.any (key:
          path + file.file == key.preserved
          || path + file.file == key.preserved + ".pub"
        ) keys) state.files);
        directories = map (directory: {
          inherit (directory) configureParent createLinkTarget directory group how
            inInitrd mode mountOptions parent user;
        }) (builtins.filter (directory: lib.any (key:
          lib.hasPrefix (path + directory.directory + "/") key.preserved
        ) keys) state.directories);
      }) hostConfig.preservation.preserveAt;
    };
    services.openssh = {
      inherit (hostConfig.services.openssh) enable hostKeys;
    };
    systemd.services.sshd.preStart = lib.optionalString (hasAge && seeded) ''
      ${pkgs.gnugrep}/bin/grep -qx canonical-bootstrap-ok /run/agenix/canonical-probe
    '';
    system.stateVersion = hostConfig.system.stateVersion;
    testing.initrdBackdoor = true;
    virtualisation = {
      diskImage = null;
      emptyDiskImages = lib.imap0 (index: _: {
        size = 64;
        driveConfig.deviceExtraOpts.serial = "preserved-${toString index}";
      }) storage;
      fileSystems = builtins.listToAttrs (lib.imap0 (index: state: {
        name = state.path;
        value = {
          neededForBoot = hostConfig.fileSystems.${state.path}.neededForBoot or false;
          autoFormat = true;
          device = "/dev/disk/by-id/virtio-preserved-${toString index}";
          fsType = "ext4";
        };
      }) storage);
      memorySize = 1024;
    };
  } // lib.optionalAttrs hasAge {
    age = {
      inherit (hostConfig.age) identityPaths;
      secrets = lib.optionalAttrs seeded {
        canonical-probe.file = "${fixture}/probe.age";
      };
    };
  };
  hasDisko = builtins.attrNames (configuration.config.disko.devices or { }) != [ ];
  host = lib.removeSuffix "VmWithDisko" (baseNameOf ./.);
  instrumented = configuration.extendModules {
    modules = [
      (
        if hasDisko then
          {
            virtualisation.vmVariantWithDisko.virtualisation.graphics = lib.mkForce false;
          }
        else
          {
            virtualisation.vmVariant.virtualisation.graphics = lib.mkForce false;
          }
      )
      ({ modulesPath, ... }: {
        imports = [ (modulesPath + "/testing/test-instrumentation.nix") ];
        users.users.root.initialHashedPassword = lib.mkForce null;
      })
    ];
  };
  name = "${host}VmWithDisko";
  startScript = pkgs.writeShellScript "start-${name}" (
    if hasDisko then
      ''
        set -e
        ${pkgs.util-linux}/bin/setsid ${vm}/bin/disko-vm "$@" &
        vm_pid=$!
        trap 'kill -- -"$vm_pid" 2>/dev/null || true; wait "$vm_pid" 2>/dev/null || true' EXIT
        trap 'exit 130' INT
        trap 'exit 143' TERM
        wait "$vm_pid"
      ''
    else
      ''
        exec ${vm}/bin/run-*-vm "$@"
      ''
  );
  vm = if hasDisko then vmConfig.system.build.vmWithDisko else vmConfig.system.build.vm;
  vmConfig =
    if hasDisko then
      instrumented.config.virtualisation.vmVariantWithDisko
    else
      instrumented.config.virtualisation.vmVariant;
  preservedPaths = lib.concatLists (lib.mapAttrsToList (path: state:
    let
      users = builtins.attrValues state.users;
      files = state.files ++ lib.concatMap (user: user.files) users;
      directories = state.directories ++ lib.concatMap (user: user.directories) users;
    in map (file: {
      inherit (file) how;
      path = file.file;
      persistent = path + file.file;
      directory = false;
    }) files ++ map (directory: {
      inherit (directory) how;
      path = directory.directory;
      persistent = path + directory.directory;
      directory = true;
    }) (builtins.filter (directory: directory.how != "_intermediate") directories)
  ) (vmConfig.preservation.preserveAt or { }));
in
pkgs.testers.runNixOSTest {
  inherit name;
  globalTimeout = 600;
  requiredFeatures.kvm = pkgs.stdenv.hostPlatform.isLinux;
  nodes = lib.optionalAttrs hasBootstrap {
    fresh = bootstrapNode false;
    seeded = bootstrapNode true;
  };
  testScript = ''
    import json
    import shlex
    ${lib.optionalString hasBootstrap ''
      keys = json.loads(${builtins.toJSON (builtins.toJSON keys)})
      fixture = ${builtins.toJSON (toString fixture)}
      def check_host_keys(node):
          node.wait_for_unit("sshd.service")
          checksums = []
          for key in keys:
              path = shlex.quote(key["path"])
              preserved = shlex.quote(key["preserved"])
              node.succeed(f"test -s {preserved}")
              assert node.succeed(f"stat -Lc '%a %U %G' {path}").strip() == "600 root root"
              node.succeed(f"cmp {path} {preserved}")
              public = node.succeed(f"ssh-keygen -y -f {path}").split()
              saved = node.succeed("cat " + shlex.quote(key["path"] + ".pub")).split()
              assert public[:2] == saved[:2]
              checksums.append(node.succeed(f"sha256sum {preserved}"))
          return checksums
      for node in [seeded, fresh]:
          node.start(allow_reboot=True)
          node.wait_for_unit("default.target")
      with subtest("Bootstrap with provisioned SSH identities"):
          for key in keys:
              source = shlex.quote(fixture + "/key-" + str(key["index"]))
              destination = shlex.quote("/sysroot" + key["preserved"])
              seeded.succeed(f"install -D -m 0600 {source} {destination}")
              source_public = shlex.quote(fixture + "/key-" + str(key["index"]) + ".pub")
              destination_public = shlex.quote("/sysroot" + key["preserved"] + ".pub")
              seeded.succeed(f"install -D -m 0644 {source_public} {destination_public}")
          seeded.switch_root()
          check_host_keys(seeded)
          for key in keys:
              source = shlex.quote(fixture + "/key-" + str(key["index"]))
              destination = shlex.quote(key["preserved"])
              seeded.succeed(f"cmp {source} {destination}")
          ${lib.optionalString hasAge ''seeded.succeed("grep -qx canonical-bootstrap-ok /run/agenix/canonical-probe")''}
      with subtest("Generate SSH identities on empty persistent storage"):
          for key in keys:
              fresh.fail("test -e " + shlex.quote("/sysroot" + key["preserved"]))
          fresh.switch_root()
          check_host_keys(fresh)
      for node in [seeded, fresh]:
          with subtest(f"{node.name}: SSH identities survive a clean-root reboot"):
              original = check_host_keys(node)
              node.succeed("touch /canonical-unpreserved-marker")
              node.reboot()
              node.wait_for_unit("default.target")
              node.fail("test -e /sysroot/canonical-unpreserved-marker")
              node.switch_root()
              assert check_host_keys(node) == original
      ${lib.optionalString hasAge ''
        with subtest("Decrypt again after reboot without reprovisioning"):
            seeded.succeed("grep -qx canonical-bootstrap-ok /run/agenix/canonical-probe")
      ''}
      for node in [seeded, fresh]:
          node.shutdown()
    ''}
    machine = create_machine(start_command="${startScript}", name="machine")
    driver.machines_qemu.append(machine)
    machine.start(allow_reboot=True)
    for phase in ["boot", "reboot"]:
        with subtest(phase):
            machine.wait_for_unit("local-fs.target")
            machine.wait_for_unit("multi-user.target")
            ${lib.optionalString vmConfig.services.openssh.enable ''
              machine.wait_for_unit("sshd.service")
            ''}
            for entry in json.loads(${builtins.toJSON (builtins.toJSON preservedPaths)}):
                path = shlex.quote(entry["path"])
                persistent = shlex.quote(entry["persistent"])
                kind = "d" if entry["directory"] else "f"
                machine.succeed(f"test -{kind} {path}; test -{kind} {persistent}")
                if entry["how"] == "symlink":
                    assert machine.succeed(f"readlink -f {path}").strip() == machine.succeed(f"readlink -f {persistent}").strip()
                else:
                    assert machine.succeed(f"stat -Lc '%d:%i' {path}").strip() == machine.succeed(f"stat -Lc '%d:%i' {persistent}").strip()
                if entry["directory"]:
                    marker = shlex.quote(entry["path"] + "/.canonical-preservation-probe")
                    if phase == "boot":
                        machine.succeed(f"printf canonical-preserved > {marker}")
                    else:
                        assert machine.succeed(f"cat {marker}") == "canonical-preserved"
        if phase == "boot":
            machine.reboot()
    machine.shutdown()
  '';
}
"""  # noqa: E501


def _python_static_template_issues(package: Package, source: str) -> list[str]:
    """Check only the stable interface required by Python package templates."""
    return [
        f"missing required {name} definition"
        for name, edits in _python_required_edits(package, source).items()
        if edits
    ]


def _binding_value(source: str, name: str, kind: str) -> str | None:
    """Extract one permitted template binding expression."""
    escaped = re.escape(name)
    patterns = {
        "list": rf"(?s)(?<![\w.]){escaped}\s*=\s*(\[.*?\])\s*;",
        "string": (
            rf"(?s)(?<![\w.]){escaped}\s*=\s*"
            r"""((?:"(?:\\.|[^"\\])*"|''.*?''))\s*;"""
        ),
    }
    match = re.search(patterns[kind], source)
    return match.group(1) if match else None


def _replace_binding(source: str, name: str, value: str) -> str:
    """Replace one binding expression in a generated template."""
    escaped = re.escape(name)
    return re.sub(
        rf"(?s)((?<![\w.]){escaped}\s*=\s*)(?:\[.*?\]|\"(?:\\.|[^\"\\])*\"|''.*?'')(\s*;)",
        lambda match: match.group(1) + value + match.group(2),
        source,
        count=1,
    )


def _nix_binding_edits(
    document: nix_syntax.Document,
    container: Node,
    path: tuple[str, ...],
    value: str,
) -> list[tuple[int, int, bytes]]:
    """Set a scoped binding, retaining unrelated fields and inherited names."""
    while container.type == "parenthesized_expression" or (
        container.type == "binary_expression"
        and document.text(nix_syntax.field(container, "operator")) == "//"
    ):
        container = nix_syntax.field(
            container,
            "expression" if container.type == "parenthesized_expression" else "right",
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
        attrpath = nix_syntax.field(binding, "attrpath")
        expression = nix_syntax.field(binding, "expression")
        if attrpath is not None and expression is not None:
            names = nix_syntax.static_attrpath(document, attrpath)
            if names == path:
                if nix_syntax.compact(document.text(expression)) == nix_syntax.compact(
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
                )
    inherited = [
        (binding, attr)
        for binding in children
        if (attrs := nix_syntax.field(binding, "attrs")) is not None
        for attr in attrs.named_children
        if document.text(attr) == path[0]
    ]
    if (
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


def _nix_remove_binding_edits(
    document: nix_syntax.Document,
    container: Node,
    path: tuple[str, ...],
) -> list[tuple[int, int, bytes]]:
    """Remove a scoped metadata binding while retaining neighboring fields."""
    if container.type == "parenthesized_expression":
        return _nix_remove_binding_edits(
            document,
            nix_syntax.field(container, "expression"),
            path,
        )
    bindings = next(
        (node for node in container.named_children if node.type == "binding_set"),
        None,
    )
    edits = []
    for binding in [] if bindings is None else bindings.named_children:
        attrpath = nix_syntax.field(binding, "attrpath")
        expression = nix_syntax.field(binding, "expression")
        if attrpath is not None and expression is not None:
            names = nix_syntax.static_attrpath(document, attrpath)
            if names == path:
                edits.append((binding.start_byte, binding.end_byte, b""))
            elif names and path[: len(names)] == names:
                edits.extend(
                    _nix_remove_binding_edits(
                        document,
                        expression,
                        path[len(names) :],
                    ),
                )
        elif len(path) == 1 and (attrs := nix_syntax.field(binding, "attrs")):
            edits.extend(
                (attr.start_byte, attr.end_byte, b"")
                for attr in attrs.named_children
                if document.text(attr) == path[0]
            )
    return edits


def source_python_has_main(source: str | None) -> bool:
    """Recognize the module-level main binding used by canonical wrappers."""
    if not source:
        return True
    module = ast.parse(source, filename="main.py")
    return any(
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
            and any(
                isinstance(target, ast.Name) and target.id == "main"
                for target in (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
            )
        )
        for node in module.body
    )


def _python_required_edits(
    package: Package,
    source: str,
) -> dict[str, list[tuple[int, int, bytes]]]:
    """Derive validation and repair from the same scoped Python requirements."""
    document = nix_syntax.parse(source)
    body = document.root
    scope = None
    while body.type in {
        "function_expression",
        "let_expression",
        "parenthesized_expression",
    }:
        if body.type == "let_expression":
            scope = body
        body = nix_syntax.field(
            body,
            "expression" if body.type == "parenthesized_expression" else "body",
        )
    argument = nix_syntax.field(body, "argument")
    function = nix_syntax.field(body, "function")
    if (
        body.type != "apply_expression"
        or function is None
        or nix_syntax.compact(document.text(function))
        != "python.pkgs.buildPythonPackage"
        or argument is None
        or argument.type not in {"attrset_expression", "rec_attrset_expression"}
    ):
        msg = (
            "Python package must call python.pkgs.buildPythonPackage "
            "with an attribute set"
        )
        raise CommandError(msg)
    template = scaffold("python", package.name, None)[
        Path("packages") / package.name / "default.nix"
    ]
    install_phase = _binding_value(template, "installPhase", "string")
    if install_phase is None:
        msg = "Python scaffold omitted its install phase"
        raise AssertionError(msg)
    executable = source_python_has_main(_read_regular(package.root / "main.py"))
    if not executable:
        install_phase = "\n".join(
            line
            for line in install_phase.split("\n")
            if not any(
                marker in line
                for marker in ('mkdir -p "$out/bin"', "printf '%s", "chmod 755")
            )
        )
    required = {
        "pname": "pname",
        "installPhase": install_phase,
        "passthru.python": "python",
        "pyproject": "false",
        "src": "./.",
        "strictDeps": "true",
    }
    if executable:
        required["meta.mainProgram"] = (
            "baseNameOf ./." if "-" in package.name else "pname"
        )
    edits = {
        name: _nix_binding_edits(document, argument, tuple(name.split(".")), value)
        for name, value in required.items()
    }
    if not executable:
        edits["library metadata"] = _nix_remove_binding_edits(
            document,
            argument,
            ("meta", "mainProgram"),
        )
    pname = (
        'builtins.replaceStrings [ "-" ] [ "_" ] (baseNameOf ./.)'
        if "-" in package.name
        else "baseNameOf ./."
    )
    if scope is None:
        edits["Python let bindings"] = [
            (
                body.start_byte,
                body.start_byte,
                f"let pname = {pname}; python = pkgs.python3; in ".encode(),
            ),
        ]
    else:
        edits["local pname"] = _nix_binding_edits(
            document,
            scope,
            ("pname",),
            pname,
        )
        bindings = next(
            (node for node in scope.named_children if node.type == "binding_set"),
            None,
        )
        has_python = any(
            (
                (path := nix_syntax.field(binding, "attrpath")) is not None
                and nix_syntax.static_attrpath(document, path) == ("python",)
            )
            or (
                (attrs := nix_syntax.field(binding, "attrs")) is not None
                and any(
                    document.text(attr) == "python" for attr in attrs.named_children
                )
            )
            for binding in ([] if bindings is None else bindings.named_children)
        )
        if not has_python:
            edits["local python"] = _nix_binding_edits(
                document,
                scope,
                ("python",),
                "pkgs.python3",
            )
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


def canonical_typed_default(package: Package) -> str | None:
    """Render a typed definition while retaining package-specific fields."""
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
    nix_syntax.parse(source, str(package.root / "default.nix"))
    if package.kind == "python":
        return _canonical_python_default(package, source)
    description = package_description(package)
    rendered = scaffold(package.kind, package.name, description)[
        Path("packages") / package.name / "default.nix"
    ]
    fields = {
        "html": (("runtimeDeps", "list"), ("prmInstall", "string")),
        "latex": (("nativeDeps", "list"),),
    }[package.kind]
    for name, kind in fields:
        value = _binding_value(source, name, kind)
        if value is not None:
            rendered = _replace_binding(rendered, name, value)
    return rendered


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
        expected_default = canonical_typed_default(package)
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


def _converge_opaque_files(
    root: Path,
    opaque: set[Path],
    tracked: set[Path],
    *,
    dry_run: bool,
) -> tuple[bool, set[Path]]:
    """Stage unmanaged files below opaque trees without changing their modes."""
    files = {
        path.relative_to(root)
        for tree in opaque
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
    scratch: set[Path],
    protected: set[Path],
    *,
    dry_run: bool,
) -> bool:
    """Untrack scratch content and delete unsupported tracked paths."""
    changed = False
    for relative in sorted(path for path in tracked if beneath(path, scratch)):
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
    """Remove undeclared files while preserving permitted scratch trees."""
    allowed = allowed_paths(root, packages)
    opaque = opaque_trees(root)
    scratch = scratch_trees(root)
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
    opaque_changed, opaque_files = _converge_opaque_files(
        root,
        opaque,
        tracked,
        dry_run=dry_run,
    )
    changed |= opaque_changed
    changed |= _remove_unsupported_tracked(
        root,
        tracked,
        scratch,
        opaque | scratch | allowed,
        dry_run=dry_run,
    )
    clean_arguments = _flake_clean_arguments(dry_run=dry_run)
    if dry_run:
        for relative in sorted(allowed | opaque_files):
            if (root / relative).exists():
                clean_arguments.extend(("-e", f"/{relative.as_posix()}"))
    clean = git(root, clean_arguments, check=False)
    if clean.returncode != 0:
        raise CommandError(clean.stderr.strip() or "git clean failed")
    if clean.stdout:
        print(clean.stdout, end="")  # noqa: T201
        changed = True
    return changed


def check_flake(root: Path, dry_run: bool) -> list[Package]:  # noqa: FBT001
    """Converge required files, structure, templates, and root whitelist."""
    missing = [
        name
        for name in (".gitignore", "flake.nix", "flake.lock")
        if not (root / name).is_file()
    ]
    if missing:
        raise CommandError("missing required file: " + missing[0])
    packages = detect_packages(root)
    for package in packages:
        if issue := _python_test_placement_issue(package):
            raise CommandError(issue)
    changed = _converge_packages(root, packages, dry_run)
    expected = render_gitignore(allowed_paths(root, packages), opaque_trees(root))
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
        opaque_trees(root),
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
    """Return source-only typed-template and generated-check issues."""
    issues: list[str] = []
    relative = Path("packages") / package.name / "default.nix"
    actual = _read_regular(root / relative)
    expected = canonical_typed_default(package)
    if (
        expected is not None
        and actual is not None
        and nix_syntax.compact(actual) != nix_syntax.compact(expected)
    ):
        issues.append(f"{relative}: differs from its canonical typed template")
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
    description_literal = _nix_string(description)
    root = Path("packages") / name
    defaults = {
        "python": """{ pkgs, ... }:
let
  pname = baseNameOf ./.;
  python = pkgs.python3;
in
python.pkgs.buildPythonPackage {
  inherit pname;
  installPhase = ''
    install -Dm644 main.py "$out/${python.sitePackages}/$pname/__init__.py"
    mkdir -p "$out/bin"
    printf '%s\\n' '#!${python.interpreter}' "from $pname import main" 'main()' > "$out/bin/$pname"
    chmod 755 "$out/bin/$pname"
    if [ -d prm ]; then
      cp -R prm/ "$out/${python.sitePackages}/$pname/"
    fi
  '';
  meta = {
    description = __DESCRIPTION__;
    mainProgram = pname;
  };
  passthru.python = python;
  propagatedBuildInputs = [ ];
  pyproject = false;
  src = ./.;
  strictDeps = true;
  version = "0.0.0";
}
""",  # noqa: E501
        "html": """{ pkgs, ... }:
let
  pname = baseNameOf ./.;
  prmInstall = "";
  runtimeDeps = [ ];
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
    ${prmInstall}
  '';
in
pkgs.writeShellApplication {
  meta.description = __DESCRIPTION__;
  name = pname;
  runtimeInputs = runtimeDeps ++ [ pkgs.http-server ];
  text = ''
    exec http-server ${site} "$@"
  '';
}
""",
        "latex": """{ pkgs, ... }:
let
  nativeDeps = [ ];
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
  nativeBuildInputs = nativeDeps ++ [ pkgs.texliveFull ];
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
        if kind == "python":
            default = default.replace("$out/bin/$pname", "$out/bin/${baseNameOf ./.}")
            default = default.replace(
                "mainProgram = pname;",
                "mainProgram = baseNameOf ./.;",
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
    tracked_sources = [
        old for old, _new in moves if any(beneath(path, {old}) for path in tracked)
    ]
    for old, new in moves:
        shutil.move(root / old, root / new)
    _refresh_gitignore(root)
    git(
        root,
        [
            "add",
            "--all",
            "--",
            *(str(path) for path in tracked_sources),
            *(str(new) for _old, new in moves),
            ".gitignore",
        ],
    )


def initialize_home() -> None:
    """Initialize and stage the canonical home policy without cleaning."""
    root = Path.home()
    if not (root / ".git").exists():
        _run(["git", "init", str(root)])
    if profile(root, "home") != "home":
        msg = "cannot initialize a flake repository as a home repository"
        raise CommandError(msg)
    _converge_home_ignore(root, dry_run=False)


def initialize_submodule(remote: str) -> None:
    """Add a hosted repository at its canonical home-relative path."""
    relative = canonical_remote_path(remote)
    home = Path.home()
    if repository_root(home) != home or profile(home) != "home":
        message = "$HOME must be an initialized canonical home repository"
        raise CommandError(message)
    _allow_home_submodule(home, relative)
    registered = any(
        Path(repository["path"]) == relative and repository["url"] == remote
        for repository in home_repositories(home)
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
    if repository_root(home) != home or profile(home) != "home":
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
            '{ inputs.canonical.url = "github:pbizopoulos/canonical"; outputs = inputs: inputs.canonical.blueprint { inherit inputs; }; }\n',  # noqa: E501
            encoding="utf-8",
        )
        (directory / "README").write_text(readme, encoding="utf-8")
        _run(
            [os.environ.get("GIT_CANONICAL_NIX", "nix"), "flake", "lock"],
            cwd=directory,
        )
        detected_packages = detect_packages(directory)
        _converge_checks(directory, detected_packages, False)  # noqa: FBT003
        (directory / ".gitignore").write_text(
            render_gitignore(
                allowed_paths(directory, detected_packages),
                opaque_trees(directory),
            ),
            encoding="utf-8",
        )
        _run(
            [os.environ.get("GIT_CANONICAL_NIX", "nix"), "fmt"],
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


def _test_names_qualified_name(node: ast.expr) -> str:
    """Read a dotted Python name without evaluating it."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return _test_names_qualified_name(node.value) + "." + node.attr
    return ""


def _test_names_unittest_classes(module: ast.Module) -> set[str]:
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
        and any(
            _test_names_qualified_name(base) in bases | found for base in node.bases
        )
    }:
        found.update(additions)
    return found


def read_test_names(path: Path, *, source_order: bool = False) -> list[str]:
    """Read top-level test functions and methods in recognized test classes."""
    if path.is_symlink():
        message = f"linked test file: {path}"
        raise ValueError(message)
    names = source_test_names(path.read_bytes(), str(path))
    return names if source_order else sorted(names)


def source_test_names(source: bytes, filename: str) -> list[str]:
    """Convert Python source into sentences in definition order without executing it."""
    module = ast.parse(source, filename=filename)
    case_classes = _test_names_unittest_classes(module)
    definitions = []
    for node in module.body:
        if isinstance(node, ast.ClassDef) and (
            node.name.startswith("Test") or node.name in case_classes
        ):
            definitions.extend(node.body)
        else:
            definitions.append(node)
    return [
        node.name.replace("_", " ")
        for node in definitions
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    ]


def _print_package_test_names(package: Path) -> None:
    """Validate a canonical Python package and print its test sentences."""
    validate_name(package.name)
    if (
        package.parent.name != "packages"
        or not (package.parent.parent / "flake.nix").is_file()
        or not all(
            (package / name).is_file()
            for name in ("default.nix", "main.py", "test_main.py")
        )
    ):
        message = (
            "expected a canonical packages/NAME with default.nix, main.py "
            "and test_main.py inside a flake"
        )
        raise ValueError(message)
    for name in read_test_names(package / "test_main.py"):
        sys.stdout.write(name + "\n")


def _print_repository_test_names(root: Path) -> bool:
    """List packages sequentially and continue after individual parse failures."""
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
        raise ValueError(message)
    success = True
    for package in packages:
        if not (package / "test_main.py").exists():
            sys.stderr.write(f"Skipping {package.name}: no test_main.py\n")
            continue
        sys.stdout.write(f"packages/{package.name}:\n")
        try:
            _print_package_test_names(package)
        except (CommandError, OSError, SyntaxError, UnicodeError, ValueError) as error:
            success = False
            sys.stderr.write(f"git canonical test names: {package.name}: {error}\n")
    return success


def _test_names_git_output(arguments: list[str], *, data: bytes | None = None) -> bytes:
    """Read Git output while preserving its diagnostics and failures."""
    return subprocess.run(  # noqa: S603
        ["git", *arguments],  # noqa: S607
        input=data,
        stdout=subprocess.PIPE,
        check=True,
    ).stdout


def _test_names_git_arguments(arguments: list[str]) -> tuple[list[str], list[str]]:
    """Separate Git options/revisions from explicit path filters."""
    separator = arguments.index("--") if "--" in arguments else len(arguments)
    options = arguments[:separator]
    paths = arguments[separator + 1 :]
    for option in options:
        if option in {
            "--no-index",
            "--no-textconv",
            "--ext-diff",
            "--check",
        } or option.startswith(
            ("--output", "--textconv=", "-L"),
        ):
            message = f"unsupported test-name diff option: {option}"
            raise ValueError(message)
    return options, paths


def _check_test_names_diff_attributes(
    configuration: list[str],
    paths: bytes,
    driver_name: str = "python-test-names",
) -> None:
    """Refuse attribute overrides that would expose unconverted source."""
    if not paths:
        return
    attributes = _test_names_git_output(
        [*configuration, "check-attr", "-z", "--stdin", "diff"],
        data=paths,
    ).split(b"\0")
    for index in range(0, len(attributes) - 1, 3):
        path, _, driver = attributes[index : index + 3]
        if driver != driver_name.encode():
            message = f"conflicting diff attribute for {path.decode(errors='replace')}"
            raise ValueError(message)


def _test_names_diff_paths(raw: bytes) -> bytes:
    """Collect raw diff paths and reject modes that bypass Git's textconv."""
    paths = []
    for field in raw.split(b"\0"):
        if not field:
            continue
        header = field.lstrip(b"\n")
        if header.startswith(b":"):
            parents = len(header) - len(header.lstrip(b":"))
            modes = header.lstrip(b":").split()[: parents + 1]
            if any(mode not in {b"000000", b"100644", b"100755"} for mode in modes):
                message = (
                    "test-name diffs require regular files, not symlinks or submodules"
                )
                raise ValueError(message)
        else:
            paths.append(field)
    return b"\0".join(paths) + (b"\0" if paths else b"")


def _print_test_names_git(
    command: str,
    arguments: list[str],
    *,
    package_args: bool = False,
) -> int:
    """Let Git compare test sentences using an invocation-local textconv driver."""
    options, paths = _test_names_git_arguments(arguments)
    if command == "show":
        revisions = _test_names_git_output(
            ["rev-parse", "--revs-only", "--no-flags", *options],
        )
        for revision in revisions.decode().splitlines():
            _test_names_git_output(
                ["rev-parse", "--verify", revision.lstrip("^") + "^{commit}"],
            )
    root = (
        _test_names_git_output(["rev-parse", "--show-toplevel"]).decode().rstrip("\n")
    )
    view = ["args"] if package_args else ["test", "names"]
    driver = "python-package-args" if package_args else "python-test-names"
    filename = "main.py" if package_args else "test_main.py"
    converter = shlex.join([str(Path(sys.argv[0]).resolve()), *view, "_textconv"])
    with TemporaryDirectory(prefix="python-test-names-") as directory:
        attributes = Path(directory) / "attributes"
        attributes.write_text(
            f"/packages/*/{filename} diff={driver} {driver}\n",
        )
        configuration = [
            "-c",
            f"core.attributesFile={attributes}",
            "-c",
            f"diff.{driver}.textconv={converter}",
            "-c",
            f"diff.{driver}.cachetextconv=false",
        ]
        filters = [*paths, f":(top,exclude,attr:!{driver})**"]
        discovery = _test_names_git_output(
            [
                *configuration,
                command,
                *options,
                "--no-patch",
                "--raw",
                "-z",
                "--no-relative",
                "--no-renames",
                "--no-ext-diff",
                "--textconv",
                "--no-quiet",
                "--no-exit-code",
                *(["--format="] if command == "show" else []),
                "--",
                *filters,
            ],
        )
        _check_test_names_diff_attributes(
            ["-C", root, *configuration],
            _test_names_diff_paths(discovery),
            driver,
        )
        return subprocess.run(  # noqa: S603
            [  # noqa: S607
                "git",
                *configuration,
                command,
                *options,
                "--no-ext-diff",
                "--textconv",
                "--",
                *filters,
            ],
            check=False,
        ).returncode


def _run_test_names(arguments: list[str]) -> int:
    """Dispatch Git views separately from the original listing interface."""
    if arguments and arguments[0] in {"diff", "show"}:
        return _print_test_names_git(arguments[0], arguments[1:])
    if arguments[:1] == ["_textconv"]:
        converter_parser = argparse.ArgumentParser(
            prog="git canonical test names _textconv",
        )
        converter_parser.add_argument("file", type=Path)
        for name in read_test_names(
            converter_parser.parse_args(arguments[1:]).file,
            source_order=True,
        ):
            sys.stdout.write(name + "\n")
        return 0
    parser = argparse.ArgumentParser(
        prog="git canonical test names",
        description="List Python test names as sentences or inspect their Git changes.",
        epilog=(
            "Repository targets list Python packages sequentially and skip packages "
            "without test_main.py. Test source is parsed, never executed. "
            "Git views: diff [Git options/revisions] [-- paths...] or "
            "show [Git options/revisions] [-- paths...]. Examples: diff; "
            "diff --staged; diff HEAD; diff HEAD~1 HEAD; show HEAD. "
            "Only packages/*/test_main.py sentences are compared. Git supplies "
            "formatting, commit metadata and exit codes. Body-only edits have "
            "no sentence hunks. These review diffs cannot be applied as source "
            "patches. Show requires commits; --no-index, --no-textconv, "
            "--ext-diff, --check, --output and -L are unsupported."
        ),
    )
    parser.add_argument(
        "target",
        type=Path,
        nargs="?",
        default=Path(),
        help=(
            "canonical packages/NAME directory or flake repository root "
            "(default: current directory)"
        ),
    )
    args = parser.parse_args(arguments)
    target = args.target.resolve()
    if (target / "flake.nix").is_file():
        return 0 if _print_repository_test_names(target) else 1
    _print_package_test_names(target)
    return 0


@dataclass(frozen=True)
class CliEntry:
    """One statically discovered command or parameter at a command path."""

    path: tuple[str, ...]
    text: str
    command: bool = False

    def render(self) -> str:
        """Keep the established flat argument-review format."""
        prefix = " ".join(self.path)
        return (prefix + ": " if prefix else "") + self.text


def source_package_args(source: bytes, filename: str) -> list[str]:
    """Render the static CLI contract in the established text format."""
    return [entry.render() for entry in source_package_cli(source, filename)]


def package_cli(package: Path) -> list[CliEntry]:
    """Read a package's static CLI contract, retaining discovery diagnostics."""
    source = _read_regular(package / "main.py")
    return source_cli_overview(source)


def source_cli_overview(source: str | None) -> list[CliEntry]:
    """Summarize a CLI, including absent or unsupported interfaces."""
    if not source:
        return [CliEntry((), "(not applicable)")]
    try:
        return source_package_cli(source.encode(), "main.py") or [
            CliEntry((), "(none)"),
        ]
    except (SyntaxError, ValueError) as error:
        return [CliEntry((), f"(unavailable: {error})")]


def _source_argparse_args(source: bytes, filename: str) -> list[CliEntry]:  # noqa: C901, PLR0915
    """Describe the supported static argparse declarations."""
    module = ast.parse(source, filename=filename)
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
                        and _test_names_qualified_name(keyword.value.func) == "Path"
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
            name = _test_names_qualified_name(call.func)
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
                isinstance(child, ast.Call)
                and _test_names_qualified_name(child.func) in constructors
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


def source_package_cli(  # noqa: C901, PLR0912, PLR0915
    source: bytes,
    filename: str,
) -> list[CliEntry]:
    """Describe conventional CLI declarations without importing package code."""
    module = ast.parse(source, filename=filename)
    imports: set[str] = set()
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".", 1)[0])
    if "argparse" in imports:
        return _source_argparse_args(source, filename)

    def unsupported(node: ast.AST, library: str) -> ValueError:
        return ValueError(
            f"unsupported CLI interface at {filename}:{getattr(node, 'lineno', 0)}: "
            f"expected conventional static {library} declarations",
        )

    def labels(call: ast.Call) -> list[str]:
        values = [*call.args]
        values.extend(
            keyword.value
            for keyword in call.keywords
            if keyword.arg in {"param_decls", "name", "help", "default", "required"}
        )
        result = []
        for value in values:
            try:
                rendered = ast.literal_eval(value)
            except (ValueError, TypeError):
                continue
            if isinstance(rendered, str) and rendered.startswith(("-", "<")):
                result.append(rendered)
        return result

    lines: list[CliEntry] = []
    found = False
    if "click" in imports:
        command_paths: dict[str, tuple[str, ...]] = {}
        command_functions = {
            node.name: node
            for node in ast.walk(module)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

        def click_path(
            name: str,
            visiting: frozenset[str] = frozenset(),
        ) -> tuple[str, ...]:
            if name in command_paths:
                return command_paths[name]
            if name in visiting:
                raise unsupported(command_functions[name], "Click")
            function = command_functions[name]
            for decorator in function.decorator_list:
                if not isinstance(decorator, ast.Call) or not isinstance(
                    decorator.func,
                    ast.Attribute,
                ):
                    continue
                if decorator.func.attr not in {"command", "group"}:
                    continue
                label = next(
                    (
                        str(ast.literal_eval(value))
                        for value in [
                            *decorator.args[:1],
                            *(
                                keyword.value
                                for keyword in decorator.keywords
                                if keyword.arg == "name"
                            ),
                        ]
                    ),
                    name,
                )
                owner = _test_names_qualified_name(decorator.func.value)
                parent = (
                    click_path(owner, visiting | {name})
                    if owner in command_functions
                    else ()
                )
                command_paths[name] = (*parent, label)
                return command_paths[name]
            return (name,)

        for node in ast.walk(module):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            command = any(
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr in {"command", "group"}
                for decorator in node.decorator_list
            )
            if command:
                found = True
                lines.append(CliEntry(click_path(node.name), "command", command=True))
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call) or not isinstance(
                    decorator.func,
                    ast.Attribute,
                ):
                    continue
                if decorator.func.attr not in {"option", "argument"}:
                    continue
                names = labels(decorator)
                if not names:
                    raise unsupported(decorator, "Click")
                found = True
                help_text = next(
                    (
                        ast.literal_eval(item.value)
                        for item in decorator.keywords
                        if item.arg == "help"
                        and isinstance(item.value, ast.Constant)
                        and isinstance(item.value.value, str)
                    ),
                    "",
                )
                suffix = f"  help={help_text}" if help_text else ""
                lines.append(
                    CliEntry(click_path(node.name), f"{', '.join(names)}{suffix}"),
                )
        if found:
            return lines
    if "typer" in imports:
        applications: dict[str, tuple[str, str]] = {}
        for call in ast.walk(module):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "add_typer"
                and call.args
                and isinstance(call.args[0], ast.Name)
            ):
                label = next(
                    (item.value for item in call.keywords if item.arg == "name"),
                    None,
                )
                if not isinstance(label, ast.Constant) or not isinstance(
                    label.value,
                    str,
                ):
                    raise unsupported(call, "Typer")
                applications[call.args[0].id] = (
                    _test_names_qualified_name(call.func.value),
                    label.value,
                )

        def typer_path(
            owner: str,
            visiting: frozenset[str] = frozenset(),
        ) -> tuple[str, ...]:
            if owner not in applications:
                return ()
            if owner in visiting:
                raise unsupported(module, "Typer")
            parent, name = applications[owner]
            return (*typer_path(parent, visiting | {owner}), name)

        lines.extend(
            CliEntry(typer_path(owner), "command", command=True)
            for owner in applications
        )
        functions = {
            node.name: node
            for node in module.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

        def typer_function(  # noqa: C901, PLR0912
            node: ast.FunctionDef | ast.AsyncFunctionDef,
            path: tuple[str, ...],
        ) -> None:
            arguments = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
            defaults: list[ast.expr | None] = [None] * (
                len(arguments) - len(node.args.defaults)
            )
            defaults.extend(node.args.defaults)
            defaults.extend(node.args.kw_defaults)
            for argument, parameter_default in zip(
                arguments,
                defaults,
                strict=True,
            ):
                if argument.arg in {"self", "cls"}:
                    continue
                annotation = argument.annotation
                if isinstance(annotation, ast.Subscript) and ast.unparse(
                    annotation.value,
                ).endswith("Annotated"):
                    annotation_parts = (
                        annotation.slice.elts
                        if isinstance(annotation.slice, ast.Tuple)
                        else [annotation.slice]
                    )
                    annotation = annotation_parts[0]
                    for metadata_part in annotation_parts[1:]:
                        if isinstance(metadata_part, ast.Call) and isinstance(
                            metadata_part.func,
                            ast.Attribute,
                        ):
                            parameter_default = metadata_part  # noqa: PLW2901
                annotation_text = ast.unparse(annotation) if annotation else "str"
                parameter_kind = (
                    "Option" if parameter_default is not None else "Argument"
                )
                help_text = ""
                option_names: list[str] = []
                if isinstance(parameter_default, ast.Call):
                    parameter_kind = (
                        parameter_default.func.attr
                        if isinstance(parameter_default.func, ast.Attribute)
                        else ast.unparse(parameter_default.func)
                    )
                    if parameter_kind not in {"Option", "Argument"}:
                        raise unsupported(parameter_default, "Typer")
                    option_names.extend(
                        item.value
                        for item in parameter_default.args
                        if isinstance(item, ast.Constant)
                        and isinstance(item.value, str)
                        and item.value.startswith("-")
                    )
                    for keyword in parameter_default.keywords:
                        if keyword.arg == "help" and isinstance(
                            keyword.value,
                            ast.Constant,
                        ):
                            help_text = str(keyword.value.value)
                optional = parameter_default is not None
                name = ", ".join(option_names) if option_names else argument.arg
                if parameter_kind == "Option" and not option_names:
                    name = f"--{argument.arg.replace('_', '-')}"
                if parameter_kind == "Argument" and optional:
                    name = f"[{argument.arg}]"
                if parameter_default is not None and not isinstance(
                    parameter_default,
                    ast.Call,
                ):
                    try:
                        default_text = repr(ast.literal_eval(parameter_default))
                    except (ValueError, TypeError):
                        raise unsupported(parameter_default, "Typer") from None
                else:
                    default_text = "required" if not optional else "optional"
                parameter_detail = (
                    default_text
                    if parameter_default is None
                    else f"default={default_text}"
                )
                suffix = f"; {parameter_detail}; type={annotation_text}"
                if help_text:
                    suffix += f"; help={help_text}"
                lines.append(CliEntry(path, f"{name}  {suffix.lstrip('; ')}"))

        run_targets = {
            call.args[0].id
            for call in ast.walk(module)
            if isinstance(call, ast.Call)
            and _test_names_qualified_name(call.func).endswith("run")
            and call.args
            and isinstance(call.args[0], ast.Name)
        }
        for name in sorted(run_targets):
            if name not in functions:
                continue
            found = True
            typer_function(functions[name], ())
        for node in ast.walk(module):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            decorators = [
                item
                for item in node.decorator_list
                if isinstance(item, ast.Call)
                and isinstance(item.func, ast.Attribute)
                and item.func.attr in {"command", "callback"}
            ]
            if not decorators:
                continue
            found = True
            command_name = node.name
            for decorator in decorators:
                if decorator.args and isinstance(decorator.args[0], ast.Constant):
                    command_name = str(decorator.args[0].value)
                for keyword in decorator.keywords:
                    if keyword.arg == "name" and isinstance(
                        keyword.value,
                        ast.Constant,
                    ):
                        command_name = str(keyword.value.value)
            decorator = decorators[0]
            typer_method = cast("ast.Attribute", decorator.func)
            owner = _test_names_qualified_name(typer_method.value)
            path = typer_path(owner)
            if typer_method.attr == "command":
                path = (*path, command_name)
                lines.append(CliEntry(path, "command", command=True))
            typer_function(node, path)
        if found:
            return lines
    if "fire" in imports:
        fire_calls = [
            node
            for node in ast.walk(module)
            if isinstance(node, ast.Call)
            and _test_names_qualified_name(node.func).endswith("Fire")
        ]
        if fire_calls:
            found = True
            targets = {
                node.name: node
                for node in module.body
                if isinstance(
                    node,
                    (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
                )
            }
            target = fire_calls[-1].args[0] if fire_calls[-1].args else None
            target_name = target.id if isinstance(target, ast.Name) else None
            for name, node in targets.items():
                if target_name is not None and name != target_name:
                    continue
                if isinstance(node, ast.ClassDef):
                    for method in node.body:
                        if isinstance(
                            method,
                            (ast.FunctionDef, ast.AsyncFunctionDef),
                        ) and not method.name.startswith("_"):
                            callable_node = method
                            method_name = method.name
                            lines.append(
                                CliEntry((method_name,), "command", command=True),
                            )
                            parameters = [
                                *callable_node.args.posonlyargs,
                                *callable_node.args.args,
                            ]
                            method_defaults: list[ast.expr | None] = [None] * (
                                len(parameters) - len(callable_node.args.defaults)
                            )
                            method_defaults.extend(callable_node.args.defaults)
                            for parameter, default in zip(
                                parameters,
                                method_defaults,
                                strict=True,
                            ):
                                if parameter.arg in {"self", "cls"}:
                                    continue
                                value = (
                                    "required"
                                    if default is None
                                    else repr(ast.literal_eval(default))
                                )
                                lines.append(
                                    CliEntry(
                                        (method_name,),
                                        f"{parameter.arg}  default={value}",
                                    ),
                                )
                else:
                    lines.append(CliEntry((name,), "command", command=True))
                    parameters = [*node.args.posonlyargs, *node.args.args]
                    function_defaults: list[ast.expr | None] = [None] * (
                        len(parameters) - len(node.args.defaults)
                    )
                    function_defaults.extend(node.args.defaults)
                    for parameter, default in zip(
                        parameters,
                        function_defaults,
                        strict=True,
                    ):
                        value = (
                            "required"
                            if default is None
                            else repr(ast.literal_eval(default))
                        )
                        lines.append(
                            CliEntry((name,), f"{parameter.arg}  default={value}"),
                        )
            return lines
    library = next(
        (name for name in ("click", "typer", "fire") if name in imports),
        None,
    )
    if library:
        raise unsupported(module, library)
    if not source_python_has_main(source.decode()) and not any(
        (isinstance(node, ast.Attribute) and node.attr == "argv")
        or (isinstance(node, ast.Name) and node.id == "argv")
        or (isinstance(node, ast.Constant) and node.value == "__main__")
        for node in ast.walk(module)
    ):
        return [CliEntry((), "(not applicable)")]
    msg = (
        f"unsupported CLI interface in {filename}: no supported static parser found; "
        "declare parser() for executable packages"
    )
    raise ValueError(msg)


def _python_suppressions(source: str) -> Counter[tuple[str, str]]:
    """Count explicit suppressions in Python comments."""
    counts: Counter[tuple[str, str]] = Counter()
    try:
        comments = (
            (token.string[1:].strip(), token.start[1] == 0)
            for token in tokenize.generate_tokens(io.StringIO(source).readline)
            if token.type == tokenize.COMMENT
        )
        for comment, standalone in comments:
            if re.search(r"^(?:ruff|flake8):\s*noqa\b", comment, re.IGNORECASE):
                counts["noqa", "global"] += 1
            elif re.search(r"^noqa\b", comment, re.IGNORECASE):
                counts["noqa", "local"] += 1
            for kind, pattern in (
                ("type: ignore", r"^type:\s*ignore\b"),
                ("pyright: ignore", r"^pyright:\s*ignore\b"),
                ("nosec", r"^nosec\b"),
                ("pragma: no cover", r"^pragma:\s*no cover\b"),
            ):
                if re.search(pattern, comment, re.IGNORECASE):
                    counts[kind, "local"] += 1
            if re.search(
                r"^mypy:\s*(?:ignore-errors|disable-error-code)\b",
                comment,
                re.IGNORECASE,
            ):
                counts["mypy", "global"] += 1
            if re.search(r"^pylint:\s*disable(?:-next)?=", comment, re.IGNORECASE):
                scope = (
                    "global"
                    if standalone and "disable-next=" not in comment
                    else "local"
                )
                counts["pylint: disable", scope] += 1
    except tokenize.TokenError:
        pass
    return counts


def source_suppressions(filename: str, source: str) -> Counter[tuple[str, str]]:
    """Count explicit lint and type-check suppressions in source comments."""
    if filename.endswith(".py"):
        return _python_suppressions(source)
    counts: Counter[tuple[str, str]] = Counter()
    if filename.endswith(".html"):
        web_comments = re.findall(r"<!--(.*?)-->", source, re.DOTALL)
    elif filename.endswith((".js", ".css")):
        pattern = (
            r"""("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`)"""
            r"|/\*(.*?)\*/" + (r"|(?<!:)//([^\n]*)" if filename.endswith(".js") else "")
        )
        web_comments = [
            match.group(2)
            or ((match.group(3) or "") if filename.endswith(".js") else "")
            for match in re.finditer(pattern, source, re.DOTALL)
            if not match.group(1)
        ]
    else:
        return counts
    for comment in web_comments:
        directive = re.match(
            r"\s*(html-validate|htmlhint|eslint|stylelint)-disable"
            r"(-next-line|-next|-current|-line)?\b",
            comment,
            re.IGNORECASE,
        )
        if directive:
            kind = f"{directive[1].lower()}-disable"
            counts[kind, "local" if directive[2] else "global"] += 1
        elif re.match(r"\s*prettier-ignore\b", comment, re.IGNORECASE):
            counts["prettier-ignore", "local"] += 1
    return counts


class DeclaredDependency(TypedDict):
    """One source declaration, without claiming an evaluated dependency closure."""

    kind: str
    target: str
    expression: str
    line: int
    resolved: bool


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
            formals = nix_syntax.field(parent, "formals")
            if formals is not None and any(
                child.type == "formal"
                and (formal := nix_syntax.field(child, "name")) is not None
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
                attrpath = nix_syntax.field(binding, "attrpath")
                if attrpath is not None and nix_syntax.static_attrpath(
                    document,
                    attrpath,
                ) == (name,):
                    return cast("Node | None", nix_syntax.field(binding, "expression"))
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
        base = nix_syntax.field(node, "expression")
        attrs = nix_syntax.field(node, "attrpath")
        parts = None if base is None else _dependency_parts(document, base, seen)
        if parts is None or attrs is None:
            return None
        for child in attrs.named_children:
            if child.type == "identifier":
                parts.append(document.text(child))
            elif child.type == "string_expression" and "${" not in document.text(child):
                parts.append(json.loads(document.text(child)))
            else:
                parts.append(None)
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
    """Expand literal lists and simple aliases; leave computed expressions opaque."""
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
        body = nix_syntax.field(node, "body")
        environment = nix_syntax.field(node, "environment")
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
        left, right = nix_syntax.field(node, "left"), nix_syntax.field(node, "right")
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
            nix_syntax.field(binding, "attrpath") if binding.type == "binding" else None
        )
        parts = (
            None if attrpath is None else nix_syntax.static_attrpath(document, attrpath)
        )
        expression = nix_syntax.field(binding, "expression")
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
                    "expression": text,
                    "line": node.start_point.row + 1,
                    "resolved": resolved,
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
                        "expression": text,
                        "line": node.start_point.row + 1,
                        "resolved": True,
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
                            "expression": text,
                            "line": node.start_point.row + 1,
                            "resolved": True,
                        },
                    )
    unique = {
        (record["kind"], record["target"], record["expression"]): record
        for record in reversed(records)
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


class SuppressionRecord(TypedDict):
    """One explicit suppression count within a source file."""

    kind: str
    scope: str
    count: int


class SourceRecord(TypedDict):
    """Physical source observations, excluding runtime output and symbolic links."""

    path: str
    lines: int | None
    suppressions: list[SuppressionRecord]
    diagnostic: str | None


class ResourceData(TypedDict):
    """Source facts shared by terminal summaries and visualization clients."""

    name: str
    description: str | None
    help: str | None
    cli: list[CliRecord]
    tests: list[str]
    dependencies: list[DeclaredDependency]
    dependency_source: str
    sources: list[SourceRecord]
    diagnostics: dict[str, str]
    source_metrics: dict[str, dict[str, int]]


def _source_observations(
    files: dict[str, str],
    errors: dict[str, str],
) -> tuple[list[SourceRecord], dict[str, dict[str, int]]]:
    """Keep per-file observations and aggregate source totals together."""
    sources: list[SourceRecord] = [
        {
            "path": filename,
            "lines": len(source.encode().splitlines()),
            "suppressions": [
                {"kind": kind, "scope": scope, "count": count}
                for (kind, scope), count in sorted(
                    source_suppressions(filename, source).items(),
                )
            ],
            "diagnostic": None,
        }
        for filename, source in sorted(files.items())
    ]
    sources.extend(
        {"path": filename, "lines": None, "suppressions": [], "diagnostic": message}
        for filename, message in sorted(errors.items())
    )
    sources.sort(key=lambda source: source["path"])
    lines = {}
    suppressions: Counter[str] = Counter()
    for source in sources:
        if source["path"].startswith("prm/"):
            continue
        if source["lines"] is not None:
            lines[source["path"]] = source["lines"]
        for item in source["suppressions"]:
            suppressions[f"{item['kind']} ({item['scope']})"] += item["count"]
    return sources, {"lines": lines, "suppressions": dict(suppressions)}


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
    help_text = None
    cli: list[CliRecord] = []
    if main_source := files.get("main.py"):
        try:
            help_text = ast.get_docstring(ast.parse(main_source, filename="main.py"))
            cli = [
                {"path": list(entry.path), "text": entry.text, "command": entry.command}
                for entry in source_package_cli(main_source.encode(), "main.py")
            ]
        except (SyntaxError, ValueError) as error:
            diagnostics["cli"] = str(error)
    tests: list[str] = []
    if test_source := files.get("test_main.py"):
        try:
            tests = source_test_names(test_source.encode(), "test_main.py")
        except SyntaxError as error:
            diagnostics["tests"] = str(error)
    sources, metrics = _source_observations(files, errors or {})
    return {
        "name": name,
        "description": source_package_description(files.get(nix_filename, "")),
        "help": help_text,
        "cli": cli,
        "tests": tests,
        "dependencies": dependencies,
        "dependency_source": nix_filename,
        "sources": sources,
        "diagnostics": diagnostics,
        "source_metrics": metrics,
    }


def _resource_source_path(filename: str) -> bool:
    """Recognize root sources and source assets under the tracked resource tree."""
    path = Path(filename)
    return (
        path.suffix in SOURCE_SUFFIXES
        and (len(path.parts) == 1 or path.parts[0] == OPAQUE_NAME)
        and not any(part.startswith(".") for part in path.parts)
    )


def resource_data(directory: Path, *, path: str | None = None) -> ResourceData:
    """Read the conventional source inventory, including source assets in prm/."""
    files: dict[str, str] = {}
    errors = {}
    if directory.is_dir() and not directory.is_symlink():
        candidates = list(directory.iterdir())
        resources = directory / OPAQUE_NAME
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


def render_resource_overview(data: ResourceData) -> str:
    """Render the terminal overview from the same facts used by other clients."""
    filenames = {source["path"] for source in data["sources"]}

    def group(name: str, entries: list[str], absent: str) -> list[str]:
        if error := data["diagnostics"].get(name):
            return [f"(unavailable: {error})"]
        return entries or [absent]

    arguments = group(
        "cli",
        [
            CliEntry(tuple(row["path"]), row["text"], row["command"]).render()
            for row in data["cli"]
        ],
        "(none)" if "main.py" in filenames else "(not applicable)",
    )
    dependencies = group(
        "dependencies",
        [_dependency_description(item) for item in data["dependencies"]],
        "(none)" if "default.nix" in filenames else "(not declared)",
    )
    tests = group(
        "tests",
        data["tests"],
        "(none)" if "test_main.py" in filenames else "(not declared)",
    )
    suppressions = [
        f"{source['path']}: {item['kind']} ({item['scope']}): {item['count']}"
        for source in data["sources"]
        for item in source["suppressions"]
    ]
    return "\n".join(
        [
            f"Name: {data['name']}",
            f"Description: {data['description'] or '(not declared)'}",
            f"Help: {data['help'] or '(module docstring not declared)'}",
            "Arguments:",
            *(f"  {item}" for item in arguments),
            "Dependencies:",
            *(f"  {item}" for item in dependencies),
            "Tests:",
            *(f"  {item}" for item in tests),
            "Suppressions:",
            *(f"  {item}" for item in (suppressions or ["(none)"])),
        ],
    )


def source_package_overview(name: str, files: dict[str, str]) -> str:
    """Render a package's source facts in the established terminal format."""
    return render_resource_overview(source_resource_data(name, files))


def package_overview(package: Path) -> str:
    """Read and summarize one package's conventional source inventory."""
    data = resource_data(package)
    return render_resource_overview(data) if data["sources"] else ""


def _overview_graph_package(
    scope: str,
    package: Package,
    nodes: dict[str, dict[str, Any]],
    edges: list[dict[str, Any]],
    *,
    details: ResourceData | None = None,
) -> None:
    path = f"packages/{package.name}"
    identifier = f"{scope}:{path}"
    details = details if details is not None else resource_data(package.root)
    dependencies = details["dependencies"]
    nodes[identifier] = {
        "id": identifier,
        "kind": "package",
        "name": package.name,
        "repository": scope,
        "path": path,
        "package_type": package.kind,
        "description": details["description"],
        "overview": render_resource_overview(details),
        "dependencies": dependencies,
        "details": details,
    }
    edges.append(
        {"source": f"{scope}:repository", "target": identifier, "kind": "contains"},
    )
    _overview_graph_dependencies(
        scope,
        path + "/default.nix",
        dependencies,
        nodes,
        edges,
    )


def _overview_graph_dependencies(
    scope: str,
    path: str,
    dependencies: list[DeclaredDependency],
    nodes: dict[str, dict[str, Any]],
    edges: list[dict[str, Any]],
) -> None:
    """Connect declared local packages to their package or host consumer."""
    identifier = f"{scope}:{posixpath.dirname(path)}"
    for dependency in dependencies:
        target = dependency["target"]
        target_id = f"{scope}:{target}"
        nodes.setdefault(
            target_id,
            {
                "id": target_id,
                "kind": "package-reference",
                "name": target,
                "repository": scope,
                "path": target,
                "expression": dependency["expression"],
                "resolved": dependency["resolved"],
            },
        )
        edges.append(
            {
                "source": target_id,
                "target": identifier,
                "kind": dependency["kind"],
                "declaration": {
                    "repository": scope,
                    "path": path,
                    "line": dependency["line"],
                    "expression": dependency["expression"],
                },
            },
        )


def _revision_tree(root: Path, revision: str) -> str | None:
    """Resolve revisions within this worktree without treating names as options."""
    location = git(root, ["rev-parse", "--show-toplevel"], check=False)
    if (
        location.returncode != 0
        or Path(location.stdout.strip()).resolve() != root.resolve()
    ):
        if revision == "HEAD":
            return None
        msg = f"could not read revision {revision}: expected a Git worktree at {root}"
        raise CommandError(msg)
    resolved = git(
        root,
        ["rev-parse", "--verify", "--end-of-options", f"{revision}^{{tree}}"],
        check=False,
    )
    if resolved.returncode != 0:
        if (
            revision == "HEAD"
            and git(root, ["rev-parse", "--verify", "HEAD"], check=False).returncode
            != 0
        ):
            return None
        msg = f"could not read revision {revision}: {resolved.stderr.strip()}"
        raise CommandError(msg)
    return resolved.stdout.strip()


def _revision_sources(root: Path, revision: str) -> dict[str, str] | None:
    """Read regular source blobs without checking out a revision or following links."""
    identifier = _revision_tree(root, revision)
    if identifier is None:
        return None
    tree = git(root, ["ls-tree", "-rz", "--full-tree", identifier, "--"], check=False)
    if tree.returncode != 0:
        msg = f"could not read revision {revision}: {tree.stderr.strip()}"
        raise CommandError(msg)
    blobs = []
    for entry in tree.stdout.split("\0"):
        if not entry:
            continue
        metadata, _, filename = entry.partition("\t")
        mode, kind, identifier = metadata.split()
        parts = Path(filename).parts
        if mode not in {"100644", "100755"} or kind != "blob":
            continue
        if filename == "flake.nix" or (
            len(parts) >= RESOURCE_SOURCE_DEPTH
            and parts[0] in {"packages", "hosts", "checks"}
            and _resource_source_path(str(Path(*parts[2:])))
        ):
            blobs.append((filename, identifier))
    if not blobs:
        return {}
    completed = subprocess.run(  # noqa: S603
        ["git", "-C", str(root), "cat-file", "--batch"],  # noqa: S607
        input="".join(f"{identifier}\n" for _, identifier in blobs).encode(),
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        diagnostic = completed.stderr.decode(errors="replace").strip()
        msg = f"could not read revision {revision}: {diagnostic}"
        raise CommandError(msg)
    result = {}
    position = 0
    for filename, _ in blobs:
        end = completed.stdout.index(b"\n", position)
        size = int(completed.stdout[position:end].rsplit(b" ", 1)[1])
        position = end + 1
        result[filename] = completed.stdout[position : position + size].decode(
            errors="replace",
        )
        position += size + 1
    return result


def _revision_resources(
    files: dict[str, str],
    collection: str,
) -> dict[str, dict[str, str]]:
    """Group the same conventional source inventory used by working-tree reads."""
    result: dict[str, dict[str, str]] = {}
    for filename, source in files.items():
        parts = Path(filename).parts
        if len(parts) >= RESOURCE_SOURCE_DEPTH and parts[0] == collection:
            result.setdefault(parts[1], {})[str(Path(*parts[2:]))] = source
    return result


def _revision_package(root: Path, name: str, files: dict[str, str]) -> Package:
    """Recognize historical package markers with the current detection rules."""
    return Package(name, _package_kind(name, set(files)), root / "packages" / name)


def _collection_data(
    root: Path,
    collection: str,
    filename: str,
    sources: dict[str, str] | None,
) -> dict[str, ResourceData]:
    """Read host and check facts from a working tree or historical source map."""
    if sources is not None:
        return {
            name: source_resource_data(name, files, path=f"{collection}/{name}")
            for name, files in _revision_resources(sources, collection).items()
            if filename in files
        }
    return {
        source.parent.name: resource_data(
            source.parent,
            path=f"{collection}/{source.parent.name}",
        )
        for source in (root / collection).glob(f"*/{filename}")
        if not source.parent.is_symlink() and not source.is_symlink()
    }


def overview_data(target: Path, *, revision: str | None = None) -> dict[str, Any]:  # noqa: C901, PLR0912 - traverse canonical collections
    """Return a versioned, source-based graph for other Canonical clients."""
    target = target.resolve()
    focus = None
    if target.parent.name == "packages" and (
        (target / "default.nix").is_file() or revision is not None
    ):
        focus = f".:packages/{target.name}"
        target = target.parent.parent
    current_profile = profile(target)
    repositories = [(".", target)]
    if current_profile == "home":
        repositories = []
        for repository in home_repositories(target, require_url=False):
            scope = repository["path"]
            checkout = (target / scope).resolve()
            if not checkout.is_relative_to(target):
                msg = f"submodule path escapes the home repository: {scope}"
                raise CommandError(msg)
            repositories.append((scope, checkout))
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []
    if current_profile == "home":
        nodes[".:repository"] = {
            "id": ".:repository",
            "kind": "repository",
            "name": ".",
            "repository": ".",
            "path": ".",
            "profile": "home",
        }
    for scope, root in sorted(repositories):
        historical = _revision_sources(root, revision) if revision is not None else None
        available = (
            "flake.nix" in (historical or {})
            if revision is not None
            else (root / "flake.nix").is_file()
        )
        nodes[f"{scope}:repository"] = {
            "id": f"{scope}:repository",
            "kind": "repository",
            "name": scope,
            "repository": scope,
            "path": ".",
            "profile": "flake",
            "available": available,
        }
        if revision is not None:
            nodes[f"{scope}:repository"]["revision_available"] = historical is not None
        if current_profile == "home":
            edges.append(
                {
                    "source": ".:repository",
                    "target": f"{scope}:repository",
                    "kind": "submodule",
                },
            )
        if not available:
            continue
        previous_packages = _revision_resources(historical or {}, "packages")
        packages = (
            [
                _revision_package(root, name, files)
                for name, files in sorted(previous_packages.items())
            ]
            if revision is not None
            else detect_packages(root)
        )
        for package in packages:
            _overview_graph_package(
                scope,
                package,
                nodes,
                edges,
                details=source_resource_data(
                    package.name,
                    previous_packages[package.name],
                )
                if revision is not None
                else None,
            )
        for collection, filename, kind in (
            ("hosts", "configuration.nix", "host"),
            ("checks", "default.nix", "check"),
        ):
            resources = _collection_data(root, collection, filename, historical)
            for name, details in sorted(resources.items()):
                path = f"{collection}/{name}"
                identifier = f"{scope}:{path}"
                nodes[identifier] = {
                    "id": identifier,
                    "kind": kind,
                    "name": name,
                    "repository": scope,
                    "path": path,
                    "details": details,
                }
                edges.append(
                    {
                        "source": f"{scope}:repository",
                        "target": identifier,
                        "kind": "contains",
                    },
                )
                if kind == "host":
                    source_path = f"{path}/{filename}"
                    dependencies = details["dependencies"]
                    nodes[identifier]["dependencies"] = dependencies
                    _overview_graph_dependencies(
                        scope,
                        source_path,
                        dependencies,
                        nodes,
                        edges,
                    )
                if kind == "check":
                    package_id = f"{scope}:packages/{name}"
                    host_id = f"{scope}:hosts/{name.removesuffix('VmWithDisko')}"
                    if package_id in nodes:
                        edges.append(
                            {
                                "source": package_id,
                                "target": identifier,
                                "kind": "checked-by",
                            },
                        )
                    elif host_id in nodes:
                        edges.append(
                            {
                                "source": host_id,
                                "target": identifier,
                                "kind": "checked-by",
                            },
                        )
    return {
        "schema": "canonical.overview",
        "schema_version": 1,
        "analysis": "source-declarations",
        "profile": current_profile,
        "focus": focus,
        "revision": revision,
        "nodes": [nodes[key] for key in sorted(nodes)],
        "edges": sorted(
            edges,
            key=lambda edge: (
                edge["source"],
                edge["target"],
                edge["kind"],
                json.dumps(edge, sort_keys=True),
            ),
        ),
    }


def _run_overview(target: Path, *, full: bool) -> None:
    """Show a package catalog or the complete summary of one package."""
    if (target / ".gitmodules").is_file() and not (target / "flake.nix").exists():
        found = False
        for repository in home_repositories(target, require_url=False):
            relative = repository["path"]
            checkout = target / relative
            if not (checkout / "flake.nix").is_file():
                continue
            found = True
            sys.stdout.write(f"{relative}:\n")
            _run_overview(checkout, full=full)
        if not found:
            msg = f"no checked-out flake submodules found under {target}"
            raise ValueError(msg)
        return
    if (target / "flake.nix").is_file():
        packages = detect_packages(target)
        if not packages:
            msg = f"no packages found under {target / 'packages'}"
            raise ValueError(msg)
        for package in packages:
            if full:
                sys.stdout.write(
                    f"packages/{package.name}:\n{package_overview(package.root)}\n\n",
                )
            else:
                sys.stdout.write(
                    f"packages/{package.name}: "
                    f"{package_description(package) or '(not declared)'}\n",
                )
        return
    validate_name(target.name)
    if (
        target.parent.name != "packages"
        or not (target.parent.parent / "flake.nix").is_file()
        or not (target / "default.nix").is_file()
    ):
        msg = "expected a canonical packages/NAME inside a flake"
        raise ValueError(msg)
    sys.stdout.write(package_overview(target) + "\n")


def _print_package_args(package: Path) -> None:
    """Validate a package and print its declared CLI interface."""
    validate_name(package.name)
    if (
        package.parent.name != "packages"
        or not (package.parent.parent / "flake.nix").is_file()
        or not (package / "default.nix").is_file()
    ):
        message = (
            "expected a canonical packages/NAME with default.nix "
            "and main.py inside a flake"
        )
        raise ValueError(message)
    source = package / "main.py"
    if source.is_symlink():
        message = f"linked source file: {source}"
        raise ValueError(message)
    for line in source_package_args(source.read_bytes(), str(source)):
        sys.stdout.write(line + "\n")


def _run_package_args(arguments: list[str]) -> int:
    """List argument declarations or compare them through Git."""
    if arguments and arguments[0] in {"diff", "show"}:
        return _print_test_names_git(arguments[0], arguments[1:], package_args=True)
    cli = argparse.ArgumentParser(
        prog="git canonical args",
        description=(
            "List statically declared argparse, Click, Fire, or Typer interfaces "
            "without executing source."
        ),
        epilog=(
            "Git views: diff [Git options/revisions] [-- paths...] or "
            "show [Git options/revisions] [-- paths...]. "
            "Only packages/*/main.py declarations are compared, in source order. "
            "Help text is included when declared statically. Dynamic declarations "
            "and non-conventional library patterns are unsupported. "
            "Review diffs cannot be applied as source patches. "
            "Show requires commits; --no-index, --no-textconv, --ext-diff, "
            "--check, --output and -L are unsupported."
        ),
    )
    if arguments[:1] == ["_textconv"]:
        cli.add_argument("file", type=Path)
        source = cli.parse_args(arguments[1:]).file
        for line in source_package_args(source.read_bytes(), str(source)):
            sys.stdout.write(line + "\n")
        return 0
    cli.add_argument(
        "target",
        type=Path,
        nargs="?",
        default=Path(),
        help="packages/NAME or flake root (default: current directory)",
    )
    target = cli.parse_args(arguments).target.resolve()
    if not (target / "flake.nix").is_file():
        _print_package_args(target)
        return 0
    directory = target / "packages"
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
        raise ValueError(message)
    status = 0
    for package in packages:
        sys.stdout.write(f"packages/{package.name}:\n")
        try:
            _print_package_args(package)
        except (CommandError, OSError, SyntaxError, UnicodeError, ValueError) as error:
            status = 1
            sys.stderr.write(f"git canonical args: {package.name}: {error}\n")
    return status


def _dispatch_test_names(arguments: list[str], *, package_args: bool = False) -> None:
    """Report errors consistently for listing, conversion and Git commands."""
    label = "args" if package_args else "test names"
    try:
        status = (
            _run_package_args(arguments) if package_args else _run_test_names(arguments)
        )
    except subprocess.CalledProcessError as error:
        sys.exit(error.returncode)
    except (CommandError, OSError, SyntaxError, UnicodeError, ValueError) as error:
        sys.stderr.write(f"git canonical {label}: {error}\n")
        sys.exit(1)
    except KeyboardInterrupt:
        sys.stderr.write(f"git canonical {label}: interrupted\n")
        sys.exit(130)
    sys.exit(status)


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
    """Copy package sources and supporting assets without scratch or metadata."""
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


def _build_test_environment(root: Path, name: str, workspace: Path) -> tuple[str, str]:
    """Build a target-specific interpreter and resolve its external tools."""
    expression = workspace / "environment.nix"
    expression.write_text(
        "let\n"
        f"  flake = builtins.getFlake {_nix_string('git+' + root.as_uri())};\n"
        "  system = builtins.currentSystem;\n"
        "  pkgs = import flake.inputs.nixpkgs { inherit system; };\n"
        f"  package = flake.packages.${{system}}.${{{_nix_string(name)}}};\n"
        "  dependencies = pkgs.lib.concatMap (name: package.${name} or []) [\n"
        '    "buildInputs" "checkInputs" "nativeBuildInputs" "nativeCheckInputs"\n'
        '    "propagatedBuildInputs" "propagatedNativeBuildInputs"\n'
        "  ];\n"
        "  python = package.python.withPackages (ps:\n"
        "    (package.propagatedBuildInputs or []) ++ [ps.hypothesis ps.pytest]);\n"
        'in pkgs.writeText "test-environment.json" (builtins.toJSON {\n'
        '  python = "${python}/bin/python";\n'
        "  path = pkgs.lib.makeBinPath dependencies;\n"
        "})\n",
        encoding="utf-8",
    )
    log = workspace / "environment.log"
    _run_test_command(
        [
            "nix",
            "build",
            "--impure",
            "--no-link",
            "--print-out-paths",
            "--file",
            str(expression),
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
    return str(environment["python"]), str(environment["path"])


def _prepare_package_tests(
    workspace: Path,
    name: str,
    python: str,
    tool_path: str,
    max_examples: int | None = None,
) -> list[str]:
    """Run pytest and package executables against the same isolated source copy."""
    launcher = workspace / "bin" / name
    launcher.parent.mkdir()
    launcher.write_text(
        "#!/bin/sh\nexec "
        + shlex.join([python, str(workspace / "package-entry.py")])
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
    pytest_arguments = ["-p", "no:cacheprovider"]
    if max_examples is not None:
        pytest_arguments.extend(
            ["-p", "_hypothesis_pytestplugin", "--hypothesis-show-statistics"],
        )
    pytest_arguments.extend(
        ["--import-mode=importlib", "-q", f"packages/{name}/test_main.py"],
    )
    bootstrap = workspace / "run-tests.py"
    bootstrap.write_text(
        "import os, sys\n"
        "from pathlib import Path\n"
        "os.dup2(1, 2)\n"
        f"os.environ['PACKAGE_E2E_EXECUTABLE'] = {str(launcher)!r}\n"
        f"tools = {str(launcher.parent) + os.pathsep + tool_path!r}\n"
        "os.environ['PATH'] = tools + os.pathsep + os.environ.get('PATH', '')\n"
        "os.environ['PYTHONDONTWRITEBYTECODE'] = '1'\n"
        "os.environ.pop('PYTHONPATH', None)\n"
        "os.environ.pop('PYTEST_ADDOPTS', None)\n"
        "os.environ['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'\n"
        "pid = Path('active-test-pgid')\n"
        "pid.write_text(str(os.getpgrp()))\n"
        "try:\n" + profile + "    import pytest\n"
        f"    sys.exit(pytest.main({pytest_arguments!r}))\n"
        "finally:\n"
        "    pid.unlink(missing_ok=True)\n",
        encoding="utf-8",
    )
    return [python, "-B", str(bootstrap)]


def _summarize_mutations(workspace: Path) -> bool:
    """Report engine outcomes without treating survivors as command failures."""
    counts: Counter[str] = Counter()
    survivors: list[str] = []
    for line in (workspace / "results.jsonl").read_text(encoding="utf-8").splitlines():
        item, result = json.loads(line)
        if result is None:
            status = "pending"
        elif (
            result["worker_outcome"] != "normal"
            or result["test_outcome"] == "incompetent"
        ):
            status = "error"
        elif result["output"] == "timeout":
            status = "timeout"
        else:
            status = result["test_outcome"]
        counts[status] += 1
        if status == "survived":
            survivors.append(f"Survived {item['job_id']}:\n{result['diff']}")
    summary = dict(counts)
    (workspace / "summary.json").write_text(
        json.dumps(summary, indent=2) + "\n",
        encoding="utf-8",
    )
    if not counts:
        sys.stdout.write("No mutations generated.\n")
    else:
        statuses = ("killed", "survived", "timeout", "error", "pending")
        sys.stdout.write(", ".join(f"{key}: {counts[key]}" for key in statuses) + "\n")
    if survivors:
        sys.stdout.write("\n".join(survivors) + "\n")
    return not (counts["error"] or counts["pending"])


def _run_mutation_campaign(
    workspace: Path,
    name: str,
    python: str,
    tool_path: str,
    timeout: float,
) -> bool:
    """Baseline, mutate, and report one copied package."""
    command = _prepare_package_tests(workspace, name, python, tool_path)
    sys.stdout.write("Running baseline tests...\n")
    sys.stdout.flush()
    _run_test_command(command, workspace, workspace / "baseline.log", timeout=timeout)
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
    timeout: float,
    max_examples: int | None,
) -> bool:
    """Run one isolated package and retain its logs and reports."""
    root = _test_target_root(package)
    scratch = root / "tmp"
    scratch.mkdir(exist_ok=True)
    workspace = Path(
        tempfile.mkdtemp(prefix=f"python-{command}-{package.name}-", dir=scratch),
    )
    label = (
        "Mutation workspace and reports"
        if command == "mutation"
        else "Hypothesis workspace and logs"
    )
    sys.stdout.write(f"{label}: {workspace}\n")
    sys.stdout.flush()
    _copy_test_sources(root, workspace)
    python, tool_path = _build_test_environment(root, package.name, workspace)
    if command == "mutation":
        return _run_mutation_campaign(
            workspace,
            package.name,
            python,
            tool_path,
            timeout,
        )
    arguments = _prepare_package_tests(
        workspace,
        package.name,
        python,
        tool_path,
        max_examples,
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
    timeout: float,
    max_examples: int | None,
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
                if _run_test_package(package, command, timeout, max_examples)
                else "failed"
            )
        except (CommandError, OSError, subprocess.TimeoutExpired) as error:
            outcomes[package.name] = "failed"
            sys.stderr.write(f"git canonical test {command}: {package.name}: {error}\n")
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
) -> None:
    """Validate budgets and run an explicitly requested test campaign."""
    max_examples = getattr(options, "max_examples", None)
    if max_examples is not None and max_examples <= 0:
        cli.error("--max-examples must be positive")
    if not math.isfinite(options.timeout) or options.timeout <= 0:
        cli.error("--timeout must be positive and finite")
    try:
        target = options.target.resolve()
        runner = (
            _run_test_repository
            if (target / "flake.nix").is_file()
            else _run_test_package
        )
        success = runner(target, options.test_command, options.timeout, max_examples)
    except (CommandError, OSError, subprocess.TimeoutExpired) as error:
        sys.stderr.write(f"git canonical test {options.test_command}: {error}\n")
        sys.exit(1)
    except KeyboardInterrupt:
        sys.stderr.write(
            f"git canonical test {options.test_command}: "
            "interrupted; diagnostics retained\n",
        )
        sys.exit(130)
    sys.exit(0 if success else 1)


def _coverage_expression(root: Path, name: str, system: str) -> str:
    """Instrument the ordinary check while reusing its environment and test command."""
    expression = """let
  flake = builtins.getFlake FLAKE;
  system = SYSTEM;
  packageName = PACKAGE;
  packageDrv = flake.packages.${system}.${packageName};
  check = flake.checks.${system}.${packageName};
in
check.overrideAttrs (previous: {
  name = "${previous.name}-coverage";
  buildCommand = ''
    mkdir -p "$out/html" "$TMPDIR/coverage-startup"
    export COVERAGE_FILE="$out/.coverage"
    export COVERAGE_PROCESS_START="$TMPDIR/coverage.ini"
    cat > "$COVERAGE_PROCESS_START" <<EOF
    [run]
    parallel = true
    data_file = $out/.coverage
    source =
        $src
        ${packageDrv}/${packageDrv.python.sitePackages}/${packageDrv.pname}
    omit =
        */test_main.py
        */prm/*
    EOF
    printf '%s\\n' 'import coverage; coverage.process_startup()' > "$TMPDIR/coverage-startup/sitecustomize.py"
    export PYTHONPATH="$TMPDIR/coverage-startup:$PWD:${packageDrv.python.pkgs.coverage}/${packageDrv.python.sitePackages}:$PYTHONPATH"
  '' + previous.buildCommand + ''
    unset COVERAGE_PROCESS_START
    python -m coverage combine --rcfile="$TMPDIR/coverage.ini"
    python - <<'PYTHON'
    import os
    import coverage
    data = coverage.CoverageData()
    data.read()
    mapped = coverage.CoverageData(basename=".coverage-mapped")
    installed = "${packageDrv}/${packageDrv.python.sitePackages}/${packageDrv.pname}/__init__.py"
    mapped.update(data, map_path=lambda path: os.environ["src"] + "/main.py" if path == installed else path)
    mapped.write()
    os.replace(mapped.data_filename(), data.data_filename())
    PYTHON
    python -m coverage html --rcfile="$TMPDIR/coverage.ini" -d "$out/html"
    python -m coverage json --rcfile="$TMPDIR/coverage.ini" -o "$out/coverage.json"
  '';
})
"""  # noqa: E501
    substitutions = {
        "FLAKE": _nix_string("git+" + root.as_uri()),
        "SYSTEM": _nix_string(system),
        "PACKAGE": _nix_string(name),
    }
    return re.sub(
        r"\b(?:FLAKE|SYSTEM|PACKAGE)\b",
        lambda match: substitutions[match[0]],
        expression,
    )


def _build_package_coverage(package: Path, system: str) -> None:
    """Build an instrumented variant of the test check and print its report path."""
    root = _test_target_root(package)
    check = root / "checks" / package.name / "default.nix"
    if not check.is_file():
        message = f"missing {check}; run git canonical converge to generate checks"
        raise CommandError(message)
    sys.stdout.write(f"Building coverage for {package.name}...\n")
    sys.stdout.flush()
    completed = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "nix",
            "build",
            "--no-link",
            "--no-write-lock-file",
            "--print-out-paths",
            "--impure",
            "--expr",
            _coverage_expression(root, package.name, system),
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
            "run git canonical converge to update the test checks"
        )
        raise CommandError(message)
    sys.stdout.write(f"{package.name}: {report}\n")


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
    system = _run(
        ["nix", "eval", "--impure", "--raw", "--expr", "builtins.currentSystem"],
    ).stdout.strip()
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
            _build_package_coverage(package, system)
            outcomes[package.name] = "passed"
        except (CommandError, OSError) as error:
            outcomes[package.name] = "failed"
            sys.stderr.write(f"git canonical test coverage: {package.name}: {error}\n")
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


def parser() -> argparse.ArgumentParser:
    """Construct the public command-line parser."""
    result = argparse.ArgumentParser(
        prog="git canonical",
        description="Manage canonical persistent state in HOME and flake repositories.",
    )
    commands = result.add_subparsers(
        dest="command",
        required=True,
        title="commands",
        metavar="COMMAND",
    )
    init = commands.add_parser(
        "init",
        help="initialize HOME, create a flake, or add a remote submodule",
        description="Initialize HOME, create a flake, or add a remote under HOME.",
    )
    init.add_argument(
        "profile",
        metavar="home|flake|REMOTE",
        help="home or flake profile, or a hosted Git remote to add as a submodule",
    )
    init.add_argument(
        "remote",
        nargs="?",
        metavar="REMOTE",
        help="empty hosted Git remote required by the flake profile",
    )
    add = commands.add_parser(
        "add",
        help="add a package or host",
        description="Create and stage a canonical package or host.",
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
    remove = commands.add_parser(
        "rm",
        help="remove a package or host",
        description="Remove and stage a canonical package or host.",
    )
    remove.add_argument(
        "resource",
        metavar="RESOURCE",
        help="existing packages/NAME or hosts/NAME path",
    )
    remove.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="print removals without changing the repository",
    )
    move = commands.add_parser(
        "mv",
        help="rename a package or host",
        description="Rename a canonical package or host and stage the result.",
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
    converge = commands.add_parser(
        "converge",
        help="converge the repository to its canonical layout",
        description="Converge the repository to its canonical layout.",
    )
    converge.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="report required actions without changing the repository",
    )
    converge.add_argument("--source", type=Path, help=argparse.SUPPRESS)
    test = commands.add_parser(
        "test",
        help="inspect tests, measure coverage, or run test campaigns",
        description="Inspect tests, measure coverage, or run test campaigns.",
    )
    commands.add_parser(
        "args",
        help="list package CLI arguments or inspect their Git changes",
    )
    overview = commands.add_parser(
        "overview",
        help="browse package descriptions, arguments, dependencies, and tests",
        description=(
            "Show a package catalog or inspect package help, arguments, "
            "same-repository dependencies, tests, and suppressions."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""JSON format (schema_version 1):
  schema: canonical.overview; analysis: source-declarations
  profile: home or flake; focus: requested package ID, otherwise null
  nodes: sorted by id; edges: sorted by source, target, kind and declaration
Nodes have id, kind, name, repository and path. IDs use REPOSITORY:RESOURCE:
standalone flakes use '.'; home submodules use their .gitmodules paths.
Node kinds are repository, package, package-reference, host and check.
Repository nodes include profile and available=false for missing submodules.
Package nodes include package_type, description, overview and dependencies.
Package, host and check nodes include details: name, description, help, cli,
tests, dependencies, dependency_source, sources, diagnostics and source_metrics.
CLI records have path (a command-name list), text and command. Source records
have path, physical lines, suppressions (kind, scope, count) and diagnostic.
Analysis errors use diagnostics keys cli/tests/dependencies, separate from facts.
Sources include conventional root files and source assets under prm/, excluding
symbolic links and tmp/. Aggregate source_metrics cover root sources only.
Terminal overviews render the same details; clients need not parse overview text.
Host nodes include dependencies declared in configuration.nix.
Missing local packages remain package-reference nodes.
Dependencies have kind, target, expression, line and resolved. Only packages
within the same repository are included; external and unresolved dependencies
are omitted. Kinds are runtime, build, test, source and reference. Literal lists,
simple lexical aliases, concatenation, with scopes, local package selectors and
relative source paths are recognized without evaluating Nix. Comments and
ordinary strings do not create references. Computed expressions, functions,
overrides, generated names and imported lists are not guessed. Declarations in
embedded derivations are included; this is not an evaluated dependency closure.
Other local package references use reference/source kinds.
Edges have source, target and kind. Dependency arrows run from provider to
consumer. Their declaration has repository, path, line and expression.
Structural edges use contains/submodule; checked-by edges link matching package
checks and NAMEVmWithDisko host checks. Clients can derive dependants from edges.
The Python overview_data(Path) API returns the same document. Both interfaces
read the working tree without building packages, importing package Python or
contacting services. Identical sources produce identical sorted JSON, without
timestamps, absolute checkout paths or store hashes. overview_data(Path,
revision=REVISION) reads regular Git blobs without changing the checkout. Home
views use each registered checkout's revision. Repository revision_available is
false when HEAD does not exist; other invalid revisions raise an error.
Clients own filtering,
layout, icons and runtime status. Match nodes by ID, allow unknown added fields
and reject unsupported schema versions.""",
    )
    overview.add_argument(
        "target",
        nargs="?",
        type=Path,
        default=Path(),
        help="home, flake root, or packages/NAME (default: current directory)",
    )
    overview.add_argument(
        "--full",
        action="store_true",
        help="show full details for every package in a flake",
    )
    overview.add_argument(
        "--json",
        action="store_true",
        help=(
            "emit deterministic canonical.overview JSON (schema version 1) "
            "for visualization clients; includes only same-repository dependencies"
        ),
    )
    overview.add_argument(
        "--revision",
        help="read a Git revision instead of working sources (requires --json)",
    )
    test.set_defaults(test_command=None, test_parser=test)
    test_commands = test.add_subparsers(dest="test_command", metavar="COMMAND")
    test_commands.add_parser(
        "names",
        help="list Python test sentences or inspect their Git changes",
    )
    coverage = test_commands.add_parser(
        "coverage",
        help="build instrumented test checks and print HTML report paths",
        description="Measure explicit test examples in a separate cached Nix build.",
        epilog=(
            "Repository targets skip packages without tests and summarize results. "
            "Builds reuse Nix's cache, leave the checkout unchanged, and store HTML "
            "reports in the Nix store. Use converge to create or update checks."
        ),
    )
    coverage.add_argument(
        "target",
        type=Path,
        nargs="?",
        default=Path(),
        help="canonical packages/NAME or flake root (default: current directory)",
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
        default=Path(),
        help="canonical packages/NAME or flake root (default: current directory)",
    )
    hypothesis.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help=(
            "seconds per test-suite invocation, excluding environment build "
            "(default: 60)"
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
        help="run Cosmic Ray mutation tests in isolated package copies",
        description="Run Cosmic Ray mutation tests in isolated package copies.",
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
        default=Path(),
        help="canonical packages/NAME or flake root (default: current directory)",
    )
    mutation.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help=(
            "seconds per test-suite invocation, excluding environment build "
            "(default: 60)"
        ),
    )
    return result


def command_catalog() -> list[dict[str, str]]:
    """Describe public CLI commands for clients using the same parser as the CLI."""
    entries: list[dict[str, str]] = []

    def visit(command: argparse.ArgumentParser, path: tuple[str, ...]) -> None:
        if path:
            entries.append({"command": " ".join(path), "help": command.format_help()})
        for action in command._actions:  # noqa: SLF001 - argparse exposes no public traversal API
            if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
                for name, child in action.choices.items():
                    visit(child, (*path, name))

    visit(parser(), ())
    return entries


def _dispatch_test_command(
    options: argparse.Namespace,
    cli: argparse.ArgumentParser,
) -> bool:
    """Show testing help or dispatch the selected test operation."""
    if options.test_command is None:
        options.test_parser.print_help()
        return True
    if options.test_command == "coverage":
        try:
            success = _run_coverage(options.target.resolve())
        except KeyboardInterrupt:
            sys.stderr.write("git canonical test coverage: interrupted\n")
            sys.exit(130)
        sys.exit(0 if success else 1)
    if options.test_command in {"hypothesis", "mutation"}:
        _dispatch_test_runner(options, cli)
        return True
    return False


def _dispatch_overview(
    options: argparse.Namespace,
    cli: argparse.ArgumentParser,
) -> None:
    """Render current summaries or emit a selected source snapshot."""
    if options.json:
        sys.stdout.write(
            json.dumps(
                overview_data(options.target, revision=options.revision),
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
    else:
        if options.revision is not None:
            cli.error("--revision requires --json")
        _run_overview(options.target.resolve(), full=options.full)


def _dispatch_standalone_command(
    options: argparse.Namespace,
    cli: argparse.ArgumentParser,
) -> bool:
    """Dispatch commands that do not require discovering the current repository."""
    if options.command == "test":
        return _dispatch_test_command(options, cli)
    if options.command == "overview":
        _dispatch_overview(options, cli)
        return True
    if options.command != "init":
        return False
    if options.profile == "home":
        if options.remote is not None:
            msg = "init home does not accept a remote"
            raise CommandError(msg)
        initialize_home()
    elif options.profile == "flake":
        if options.remote is None:
            msg = "init flake requires REMOTE"
            raise CommandError(msg)
        initialize_flake(options.remote)
    else:
        if options.remote is not None:
            cli.error("init REMOTE accepts exactly one remote")
        initialize_submodule(options.profile)
    return True


def _normalize_help_arguments(arguments: list[str]) -> list[str]:
    """Translate the help convenience command to argparse's help option."""
    if arguments[:1] == ["help"]:
        return [*arguments[1:], "--help"]
    return arguments


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


def main() -> None:
    """Dispatch the git canonical CLI."""
    arguments = _normalize_help_arguments(sys.argv[1:])
    package_args = arguments[:1] == ["args"]
    if package_args or arguments[:2] == ["test", "names"]:
        _dispatch_test_names(
            arguments[1:] if package_args else arguments[2:],
            package_args=package_args,
        )
        return
    try:
        cli = parser()
        options = cli.parse_args(arguments)
        if _dispatch_standalone_command(options, cli):
            return
        if options.command == "converge" and options.source is not None:
            validate_flake_source(options.source.resolve())
            return
        root = repository_root()
        current_profile = profile(root)
        if options.command in {"add", "mv", "rm"} and current_profile != "flake":
            msg = f"{current_profile} repositories do not support flake resources"
            raise CommandError(  # noqa: TRY301
                msg,
            )
        if options.command == "converge":
            check_home(
                root,
                options.dry_run,
            ) if current_profile == "home" else check_flake(
                root,
                options.dry_run,
            )
        elif options.command == "add":
            _dispatch_add(root, options)
        elif options.command == "rm":
            remove_resource(root, options.resource, options.dry_run)
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

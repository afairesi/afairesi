#!/usr/bin/env python3
# Copyright (c) 2026- Paschalis Bizopoulos
"""Manage the persistent home repository and its registered Git submodules."""

from __future__ import annotations

import argparse
import contextlib
import re
import shutil
import sys
from pathlib import Path

from afairesi import (
    CommandError,
    canonical_remote_path,
    git,
    missing_directories,
    preserve_whitelist_on_error,
    read_regular,
    remove_empty_directories,
    report_change,
    repository_type,
    require_nix_flakes,
    run,
    validate_managed_path,
    write_managed,
)
from afairesi import (
    initialize_flake as initialize_flake_directory,
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
        msg = "$HOME must be a Git repository; run 'afairesi-home init' first"
        raise CommandError(msg)
    if Path(completed.stdout.strip()) != home or repository_type(home, "git") != "home":
        msg = (
            "$HOME must be an initialized canonical home repository; "
            "run 'afairesi-home init' first"
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


def initialize_flake(remote: str) -> None:
    """Create a canonical flake at its remote-derived home path."""
    relative = canonical_remote_path(remote)
    home = _require_home_repository()
    _home_ignore_source(home)
    home_submodules(home)
    validate_managed_path(home, relative)
    nix_executable = require_nix_flakes()
    if not _remote_is_empty(remote):
        msg = "init-flake requires an empty remote"
        raise CommandError(msg)
    directory = home / relative
    if directory.exists():
        msg = f"target already exists: {directory}"
        raise CommandError(msg)
    directory.parent.mkdir(parents=True, exist_ok=True)
    run(["git", "clone", remote, str(directory)])
    try:
        initialize_flake_directory(directory, nix_executable=nix_executable)
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


def parser() -> argparse.ArgumentParser:
    """Construct the home repository command-line parser."""
    result = argparse.ArgumentParser(
        prog="afairesi-home",
        description=(
            "Manage the Git repository at $HOME and its submodules. "
            "A whitelist tracks persistent files; hosted repositories live at "
            "$HOME/<remote-hostname>/<repository-path>. "
            "Repairs stage structural changes without committing them."
        ),
        epilog=(
            "Use afairesi inside flake repositories to manage packages and hosts. "
            "Use nix fmt to repair flake structure and format code. "
            "Run git submodule foreach 'afairesi-tests' to test all "
            "initialized submodules."
        ),
    )
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
    init_flake = commands.add_parser(
        "init-flake",
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
    init_flake.add_argument("remote", metavar="REMOTE", help="empty hosted Git remote")
    remove = commands.add_parser(
        "remove",
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
    return result


def main() -> None:
    """Dispatch the home repository lifecycle commands."""
    cli = parser()
    options = cli.parse_args()
    try:
        if options.command is None:
            cli.print_help()
        elif options.command == "init":
            initialize_home()
        elif options.command == "clone":
            initialize_submodule(options.remote)
        elif options.command == "init-flake":
            initialize_flake(options.remote)
        elif options.command == "repair":
            converge_home(_require_home_repository(), options.dry_run)
        elif options.command == "remove":
            root = _require_home_repository()
            remove_home_submodule(
                root,
                options.resource,
                dry_run=options.dry_run,
                force=options.force,
            )
    except (CommandError, OSError, UnicodeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)  # noqa: T201
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()

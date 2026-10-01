# Copyright (c) 2026- Paschalis Bizopoulos
"""Exercise resource management and convergence through the installed CLI."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING

import coverage
import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator


def _run(
    root: Path,
    *arguments: str,
    code: int = 0,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(  # noqa: S603
        [os.environ["PACKAGE_E2E_EXECUTABLE"], *arguments],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode != code:
        raise AssertionError(result.stdout + result.stderr)
    return result


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(  # noqa: S603
        ["git", *arguments],  # noqa: S607
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    ).stdout


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    """Create an isolated indexed flake without contacting a forge."""
    _git(tmp_path, "init", "--quiet")
    for name in (".gitignore", "flake.nix", "flake.lock", "README"):
        (tmp_path / name).write_text("", encoding="utf-8")
    _git(tmp_path, "add", ".")
    return tmp_path


def test_package_and_host_lifecycle(repository: Path) -> None:
    """Create, rename, and remove resources with their generated checks and assets."""
    root = repository
    _run(root, "add", "packages/report", "python", 'A "quoted" report.')
    package = root / "packages/report"
    tests = package / "test_main.py"
    if tests.exists() or (root / "checks/report").exists():
        message = "untested packages should not receive tests or test checks"
        raise AssertionError(message)
    resource = package / "prm/nested/asset.txt"
    resource.parent.mkdir(parents=True)
    resource.write_text("keep this resource", encoding="utf-8")
    tests.write_text("def test_result():\n    pass\n", encoding="utf-8")
    _run(root, "add", "hosts/demoHost")
    _run(root, "converge")
    tracked = _git(root, "ls-files").splitlines()
    for name in (
        "packages/report/test_main.py",
        "packages/report/prm/nested/asset.txt",
        "checks/report/default.nix",
        "checks/demoHostVmWithDisko/default.nix",
    ):
        if name not in tracked:
            raise AssertionError(name)
    _run(root, "mv", "packages/report", "packages/renamed", "--dry-run")
    if not package.is_dir() or (root / "packages/renamed").exists():
        message = "dry-run moved a package"
        raise AssertionError(message)
    _run(root, "mv", "packages/report", "packages/renamed")
    _run(root, "mv", "hosts/demoHost", "hosts/newHost")
    if (
        root / "packages/renamed/prm/nested/asset.txt"
    ).read_text() != "keep this resource":
        message = "rename lost package resources"
        raise AssertionError(message)
    for name in (
        "checks/renamed/default.nix",
        "checks/newHostVmWithDisko/default.nix",
    ):
        if not (root / name).is_file():
            raise AssertionError(name)
    _run(root, "rm", "packages/renamed", "--dry-run")
    if not (root / "packages/renamed/main.py").is_file():
        message = "dry-run removed the package"
        raise AssertionError(message)
    _run(root, "rm", "packages/renamed")
    _run(root, "rm", "hosts/newHost")
    if any(
        (root / name).exists()
        for name in (
            "packages/report",
            "packages/renamed",
            "hosts/demoHost",
            "hosts/newHost",
            "checks/report",
            "checks/renamed",
            "checks/demoHostVmWithDisko",
            "checks/newHostVmWithDisko",
        )
    ):
        message = "removed resources left package or check directories behind"
        raise AssertionError(message)
    _run(root, "converge")
    _run(root, "converge", "--dry-run")


def test_convergence_preserves_source_and_scratch_and_is_idempotent(
    repository: Path,
) -> None:
    """Convergence stages source, preserves scratch, and reaches a stable checkout."""
    root = repository
    _run(root, "add", "packages/example", "python")
    for name in (
        "tmp/root-state",
        "packages/example/tmp/package-state",
        "packages/example/prm/ms.tex",
    ):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name, encoding="utf-8")
    (root / "discarded").write_text("generated artifact", encoding="utf-8")
    _run(root, "converge")
    if (root / "discarded").exists():
        message = "convergence retained an unsupported artifact"
        raise AssertionError(message)
    tracked = _git(root, "ls-files").splitlines()
    if "packages/example/prm/ms.tex" not in tracked or any(
        "tmp/" in p for p in tracked
    ):
        raise AssertionError(tracked)
    before = {name: (root / name).read_bytes() for name in tracked}
    index = _git(root, "ls-files", "--stage")
    _run(root, "converge")
    if before != {
        name: (root / name).read_bytes() for name in tracked
    } or index != _git(root, "ls-files", "--stage"):
        message = "a second convergence changed the checkout"
        raise AssertionError(message)
    for name in ("tmp/root-state", "packages/example/tmp/package-state"):
        if (root / name).read_text() != name:
            raise AssertionError(name)


def test_structure_validation_does_not_traverse_excluded_trees(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Skip excluded subtrees while still rejecting unmanaged files and symlinks."""
    _run(repository, "add", "packages/example", "python")
    excluded = [
        repository / name
        for name in (
            ".git",
            "prm",
            "tmp",
            "packages/example/prm",
            "packages/example/tmp",
        )
    ]
    for directory in excluded[1:]:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "ignored").write_text("unrestricted content")
    package = repository / "packages/example"
    (package / "stray").write_text("unsupported")
    (package / "link").symlink_to(repository / "tmp", target_is_directory=True)
    subject = import_module("packages.git-canonical.main")
    original_scandir = os.scandir

    def guarded_scandir(path: str | os.PathLike[str]) -> Iterator[os.DirEntry[str]]:
        if any(Path(path).is_relative_to(directory) for directory in excluded):
            message = f"traversed excluded tree: {path}"
            raise AssertionError(message)
        return original_scandir(path)

    monkeypatch.setattr(os, "scandir", guarded_scandir)
    _, issues = subject.inspect_structure(repository)
    expected_issues = 2
    if len(issues) != expected_issues or not any(
        "stray: unsupported" in issue for issue in issues
    ):
        raise AssertionError(issues)
    if not any("link: expected regular" in issue for issue in issues):
        raise AssertionError(issues)


def test_html_template_keeps_prm_install_hook(repository: Path) -> None:
    """Keep bundled assets and package-specific resources across convergence."""
    subject = import_module("packages.git-canonical.main")
    package_root = repository / "packages/viewer"
    package_root.mkdir(parents=True)
    (package_root / "index.html").write_text("<!doctype html>", encoding="utf-8")
    package = subject.Package("viewer", "html", package_root)
    generated = subject.scaffold("html", "viewer", None)[
        Path("packages/viewer/default.nix")
    ]
    if "if [ -d ${./.}/prm ]; then" not in generated:
        msg = "HTML template does not copy prm assets"
        raise AssertionError(msg)
    custom = generated.replace(
        'prmInstall = "";',
        "prmInstall = ''\n    cp /example/sample.dcm \"$out/prm/sample.dcm\"\n  '';",
    )
    (package_root / "default.nix").write_text(custom, encoding="utf-8")
    canonical = subject.canonical_typed_default(package)
    if canonical != custom:
        msg = "HTML convergence discarded the prm install hook"
        raise AssertionError(msg)


def test_invalid_source_is_rejected_before_cleanup(repository: Path) -> None:
    """A failed convergence preserves both source and unrelated work."""
    _run(repository, "add", "packages/example", "python")
    source = repository / "packages/example/main.py"
    source.write_text("def test_misplaced(): pass\n", encoding="utf-8")
    artifact = repository / "work-in-progress"
    artifact.write_text("keep", encoding="utf-8")
    result = _run(repository, "converge", code=1)
    if "test_main.py" not in result.stderr or artifact.read_text() != "keep":
        raise AssertionError(result.stderr)
    if source.read_text() != "def test_misplaced(): pass\n":
        message = "failed convergence rewrote invalid source"
        raise AssertionError(message)


def test_unittest_methods_drive_checks_and_source_validation(repository: Path) -> None:
    """Discover aliased unittest cases consistently in tests and package source."""
    _run(repository, "add", "packages/example", "python")
    package = repository / "packages/example"
    tests = package / "test_main.py"
    tests.write_text(
        "from unittest import TestCase as Case\n"
        "class Behavior(Case):\n    def test_result(self): pass\n",
        encoding="utf-8",
    )
    _run(repository, "converge")
    check = repository / "checks/example/default.nix"
    if (
        not check.is_file()
        or "checks/example/default.nix"
        not in _git(
            repository,
            "ls-files",
        ).splitlines()
    ):
        message = "unittest methods did not generate a staged check"
        raise AssertionError(message)
    source = package / "main.py"
    source.write_text(
        source.read_text(encoding="utf-8") + tests.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    artifact = repository / "work-in-progress"
    artifact.write_text("keep", encoding="utf-8")
    result = _run(repository, "converge", code=1)
    if "move test definitions to test_main.py" not in result.stderr:
        raise AssertionError(result.stderr)
    if artifact.read_text(encoding="utf-8") != "keep":
        message = "failed validation cleaned unrelated work"
        raise AssertionError(message)


@pytest.mark.parametrize(
    "arguments",
    [
        ("mv", "packages/example", "hosts/example"),
        ("add", "packages/bad--name", "python"),
        ("add", "hosts/bad-name"),
        ("rm", "../outside"),
    ],
)
def test_invalid_resource_requests_do_not_change_the_checkout(
    repository: Path,
    arguments: tuple[str, ...],
) -> None:
    """Reject malformed resource operations without staging changes."""
    before = _git(repository, "status", "--porcelain")
    _run(repository, *arguments, code=1)
    if _git(repository, "status", "--porcelain") != before:
        message = "invalid operation changed the checkout"
        raise AssertionError(message)


def test_cli_help_and_retired_commands(tmp_path: Path) -> None:
    """Help works outside a repository and retired commands fail clearly."""
    for command in (
        (),
        ("add",),
        ("mv",),
        ("rm",),
        ("init",),
        ("converge",),
        ("overview",),
        ("test",),
        ("test", "names"),
        ("test", "coverage"),
        ("test", "hypothesis"),
        ("test", "mutation"),
    ):
        option = _run(tmp_path, *command, "--help").stdout
        alias = _run(tmp_path, "help", *command).stdout
        if option != alias or "usage:" not in option:
            raise AssertionError(option)
    _run(tmp_path, "status", code=2)
    _run(tmp_path, "check", code=2)
    for retired_command in ("test-names", "coverage", "hypothesis", "mutation"):
        _run(tmp_path, retired_command, code=2)
    help_output = _run(tmp_path, "test", "--help").stdout
    if _run(tmp_path, "test").stdout != help_output:
        message = "bare test should show help without running tests"
        raise AssertionError(message)
    _run(tmp_path, "test", "unknown", code=2)


@pytest.mark.parametrize(
    "remote",
    [
        "https://example.test/team/project.git",
        "ssh://git@example.test/team/project.git",
        "git@example.test:team/project.git",
    ],
)
def test_init_remote_adds_a_home_submodule_and_preserves_git_failures(  # noqa: C901
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    remote: str,
) -> None:
    """Add an existing remote from any directory without creating commits."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    origin = tmp_path / "remote"
    origin.mkdir()
    _git(origin, "init", "--quiet")
    (origin / "README").write_text("existing repository")
    _git(origin, "add", "README")
    _git(
        origin,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.test",
        "commit",
        "--quiet",
        "-m",
        "Existing content",
    )
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{origin.as_uri()}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", remote)
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")
    _run(tmp_path, "init", "home")
    _git(
        home,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.test",
        "commit",
        "--quiet",
        "-m",
        "Home policy",
    )
    head = _git(home, "rev-parse", "HEAD")
    relative = "example.test/team/project"
    result = _run(tmp_path, "init", remote)
    checkout = home / relative
    ignore = (home / ".gitignore").read_text(encoding="utf-8")
    for pattern in (
        "!/example.test/",
        "!/example.test/team/",
        "!/example.test/team/project",
    ):
        if ignore.splitlines().count(pattern) != 1:
            raise AssertionError(ignore)
    if _git(home, "show", ":.gitignore") != ignore:
        message = "init REMOTE did not stage the home whitelist"
        raise AssertionError(message)
    if (checkout / "README").read_text() != "existing repository":
        raise AssertionError(result.stdout + result.stderr)
    if _git(home, "rev-parse", "HEAD") != head:
        message = "init REMOTE created a home commit"
        raise AssertionError(message)
    if (
        _git(
            home,
            "config",
            "--file",
            ".gitmodules",
            f"submodule.{relative}.url",
        ).strip()
        != remote
    ):
        message = "init REMOTE changed the registered remote"
        raise AssertionError(message)
    if not _git(home, "ls-files", "--stage", "--", relative).startswith("160000 "):
        message = "init REMOTE did not stage the submodule"
        raise AssertionError(message)
    _run(tmp_path, "init", remote)
    if (home / ".gitignore").read_text(encoding="utf-8") != ignore:
        message = "repeated init changed the home whitelist"
        raise AssertionError(message)
    (checkout / ".git").unlink()
    native = subprocess.run(  # noqa: S603
        ["git", "submodule", "add", "--", remote, relative],  # noqa: S607
        cwd=home,
        capture_output=True,
        text=True,
        check=False,
    )
    if native.returncode == 0:
        message = "adding over a broken checkout should fail"
        raise AssertionError(message)
    duplicate = _run(tmp_path, "init", remote, code=native.returncode)
    if duplicate.stderr != native.stderr:
        raise AssertionError(duplicate.stderr)


def test_init_flake_stages_home_whitelist_without_force(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Register an empty remote through the home whitelist."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    origin = tmp_path / "remote.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "--quiet")
    remote = "https://example.test/team/new.git"
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{origin.as_uri()}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", remote)
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.test")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.test")
    fake_nix = tmp_path / "nix"
    fake_nix.write_text(
        '#!/bin/sh\nif [ "$1" = flake ]; then printf "{}\\n" > flake.lock; fi\n',
        encoding="utf-8",
    )
    fake_nix.chmod(0o755)
    monkeypatch.setenv("GIT_CANONICAL_NIX", str(fake_nix))
    _run(tmp_path, "init", "home")
    _git(home, "commit", "--quiet", "-m", "Home policy")
    _run(tmp_path, "init", "flake", remote)
    relative = "example.test/team/new"
    ignore = (home / ".gitignore").read_text(encoding="utf-8")
    if f"!/{relative}" not in ignore.splitlines():
        raise AssertionError(ignore)
    if _git(home, "show", ":.gitignore") != ignore:
        message = "init flake did not stage the home whitelist"
        raise AssertionError(message)
    if not _git(home, "ls-files", "--stage", "--", relative).startswith("160000 "):
        message = "init flake did not stage its submodule"
        raise AssertionError(message)


@pytest.fixture
def home_repository(tmp_path: Path) -> Path:
    """Create a home repository with a locally initialized submodule."""
    root = tmp_path
    relative = "forge.example/owner/demo"
    checkout = root / relative
    checkout.mkdir(parents=True)
    _git(root, "init", "--quiet")
    _git(checkout, "init", "--quiet")
    _git(checkout, "config", "user.name", "Test")
    _git(checkout, "config", "user.email", "test@example.org")
    _git(checkout, "config", "commit.gpgSign", "false")
    _git(checkout, "remote", "add", "origin", "git@forge.example:owner/demo")
    source = checkout / "README"
    source.write_text("first", encoding="utf-8")
    _git(checkout, "add", "README")
    _git(checkout, "commit", "--quiet", "-m", "first")
    first = _git(checkout, "rev-parse", "HEAD").strip()
    _git(checkout, "update-ref", "refs/remotes/origin/main", first)
    (root / ".gitmodules").write_text(
        f'[submodule "{relative}"]\npath = {relative}\n'
        "url = git@forge.example:owner/demo\n",
        encoding="utf-8",
    )
    (root / ".gitignore").write_text(
        "*\n!/.gitignore\n!/.gitmodules\n"
        "!/forge.example/\n!/forge.example/owner/\n!/forge.example/owner/demo\n",
        encoding="utf-8",
    )
    _git(root, "add", "--force", ".gitignore", ".gitmodules", relative)
    return root


def test_home_convergence_preserves_dirty_and_unpublished_submodule_state(
    home_repository: Path,
) -> None:
    """Leave dirty files and gitlink advancement to native Git."""
    root = home_repository
    relative = "forge.example/owner/demo"
    checkout = root / relative
    source = checkout / "README"
    first = _git(checkout, "rev-parse", "HEAD").strip()
    source.write_text("second", encoding="utf-8")
    _run(root, "converge")
    if source.read_text() != "second":
        message = "convergence changed dirty submodule content"
        raise AssertionError(message)
    _git(checkout, "add", "README")
    _git(checkout, "commit", "--quiet", "-m", "second")
    second = _git(checkout, "rev-parse", "HEAD").strip()
    for published in (False, True):
        if published:
            _git(checkout, "update-ref", "refs/remotes/origin/main", second)
        _run(root, "converge")
        if first not in _git(root, "ls-files", "--stage", relative):
            message = "convergence advanced the recorded submodule commit"
            raise AssertionError(message)
    _git(root, "add", relative)
    if second not in _git(root, "ls-files", "--stage", relative):
        message = "native Git could not advance the submodule commit"
        raise AssertionError(message)


def test_home_convergence_restores_submodule_whitelist(home_repository: Path) -> None:
    """Repair missing whitelist entries for a registered home submodule."""
    root = home_repository
    ignore = root / ".gitignore"
    expected = ignore.read_text(encoding="utf-8")
    ignore.write_text("*\n!/.gitignore\n!/.gitmodules\n", encoding="utf-8")
    before = _git(root, "show", ":.gitignore")
    preview = _run(root, "converge", "--dry-run", code=1)
    if "would write '.gitignore'" not in preview.stdout:
        raise AssertionError(preview.stdout)
    if (
        ignore.read_text(encoding="utf-8") == expected
        or _git(root, "show", ":.gitignore") != before
    ):
        message = "dry-run changed the home whitelist"
        raise AssertionError(message)
    _run(root, "converge")
    if ignore.read_text(encoding="utf-8") != expected:
        raise AssertionError(ignore.read_text(encoding="utf-8"))
    if _git(root, "show", ":.gitignore") != expected:
        message = "convergence did not stage the repaired whitelist"
        raise AssertionError(message)
    _run(root, "converge", "--dry-run")


def test_home_convergence_rejects_duplicate_submodule_fields(
    home_repository: Path,
) -> None:
    """Reject ambiguous Git configuration instead of silently choosing a URL."""
    root = home_repository
    modules = root / ".gitmodules"
    source = modules.read_text(encoding="utf-8")
    modules.write_text(
        source + "url = git@forge.example:owner/other\n",
        encoding="utf-8",
    )
    result = _run(root, "converge", code=1)
    if "duplicate url field" not in result.stderr:
        raise AssertionError(result.stderr)
    if (
        modules.read_text(encoding="utf-8")
        != source + "url = git@forge.example:owner/other\n"
    ):
        message = "invalid metadata was changed"
        raise AssertionError(message)


def test_home_rename_preserves_dirty_checkout_and_recorded_commit(
    home_repository: Path,
) -> None:
    """Move dirty submodules safely and reject collisions before mutation."""
    root = home_repository
    relative = "forge.example/owner/demo"
    checkout = root / relative
    source = checkout / "README"
    first = _git(checkout, "rev-parse", "HEAD").strip()
    source.write_text("second", encoding="utf-8")
    _git(checkout, "add", "README")
    _git(checkout, "commit", "--quiet", "-m", "second")
    _git(root, "submodule", "absorbgitdirs", relative)
    _git(root, "config", f"submodule.{relative}.url", "git@forge.example:owner/demo")
    destination = "forge.example/owner/renamed"
    _git(
        root,
        "config",
        "--file",
        ".gitmodules",
        f"submodule.{relative}.url",
        "git@forge.example:owner/renamed",
    )
    source.write_text("staged", encoding="utf-8")
    _git(checkout, "add", "README")
    source.write_text("unstaged", encoding="utf-8")
    (checkout / "untracked").write_text("keep", encoding="utf-8")
    (root / "unrelated").write_text("keep home file", encoding="utf-8")
    before = _git(checkout, "status", "--porcelain")
    indexed = _git(checkout, "show", ":README")
    target = root / destination
    target.mkdir()
    rejected = _run(root, "converge", code=1)
    if "target already exists" not in rejected.stderr or not checkout.exists():
        raise AssertionError(rejected.stderr)
    if destination in _git(
        root,
        "config",
        "--file",
        ".gitmodules",
        f"submodule.{relative}.path",
    ):
        message = "collision changed the submodule path"
        raise AssertionError(message)
    target.rmdir()
    rejected = _run(root, "converge", code=1)
    if "stage .gitmodules" not in rejected.stderr or not checkout.exists():
        raise AssertionError(rejected.stderr)
    _git(root, "add", ".gitmodules")
    _run(root, "converge", "--dry-run", code=1)
    if not checkout.exists() or target.exists():
        message = "dry-run moved the checkout"
        raise AssertionError(message)
    _run(root, "converge")
    if (
        checkout.exists()
        or _git(target, "status", "--porcelain") != before
        or _git(target, "show", ":README") != indexed
        or (target / "untracked").read_text() != "keep"
        or (root / "unrelated").read_text() != "keep home file"
        or first not in _git(root, "ls-files", "--stage", destination)
        or _git(target, "remote", "get-url", "origin").strip()
        != "git@forge.example:owner/renamed"
    ):
        message = "home rename failed to preserve Git state or synchronize the URL"
        raise AssertionError(message)
    _run(root, "converge", "--dry-run")


@pytest.mark.parametrize("kind", ["python", "html", "latex", "nix"])
def test_dash_case_packages_normalize_nix_names(repository: Path, kind: str) -> None:
    """Keep dashed resource paths and normalize generated package names."""
    _run(repository, "add", "packages/dash-case", kind)
    package = repository / "packages/dash-case"
    expected = 'builtins.replaceStrings [ "-" ] [ "_" ] (baseNameOf ./.)'
    if expected not in (package / "default.nix").read_text():
        msg = "generated Nix package name was not normalized"
        raise AssertionError(msg)
    _run(repository, "converge")
    _run(repository, "converge", "--dry-run")
    _run(repository, "mv", "packages/dash-case", "packages/another-name")
    _run(repository, "converge")
    _run(repository, "rm", "packages/another-name")


def test_git_discovers_canonical_subcommand(repository: Path) -> None:
    """Expose the installed CLI through Git's external command lookup."""
    environment = dict(os.environ)
    executable_directory = os.path.dirname(environment["PACKAGE_E2E_EXECUTABLE"])  # noqa: PTH120
    environment["PATH"] = executable_directory + os.pathsep + environment["PATH"]
    result = subprocess.run(
        ["git", "canonical", "help"],  # noqa: S607
        cwd=repository,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    if "usage: git canonical" not in result.stdout:
        msg = "Git did not discover the canonical CLI"
        raise AssertionError(msg)


def _make_test_names_package(root: Path, name: str, source: str) -> Path:
    """Create a canonical package fixture without executing its source."""
    package = root / "packages" / name
    package.mkdir(parents=True)
    (root / "flake.nix").touch()
    (package / "default.nix").touch()
    (package / "main.py").touch()
    (package / "test_main.py").write_text(source)
    return package


def _run_test_names(cwd: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    """Invoke the installed command from a chosen working directory."""
    return subprocess.run(  # noqa: S603
        [os.environ["PACKAGE_E2E_EXECUTABLE"], "test", "names", *arguments],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )


def test_args_prefers_conventional_public_parser(tmp_path: Path) -> None:
    """Keep internal helper parsers out of a package's public interface."""
    package = _make_test_names_package(tmp_path, "example", "")
    (package / "main.py").write_text(
        "import argparse\n"
        "def helper():\n"
        "    internal = argparse.ArgumentParser()\n"
        "    internal.add_argument('--internal')\n"
        "def parser():\n"
        "    public = argparse.ArgumentParser()\n"
        "    public.add_argument('--public')\n"
        "    return public\n",
        encoding="utf-8",
    )
    if _run(package, "args").stdout != "--public  optional\n":
        msg = "internal parser leaked into CLI summary"
        raise AssertionError(msg)


@pytest.mark.parametrize(
    ("imports", "constructor"),
    [
        ("import argparse", "argparse.ArgumentParser"),
        ("import argparse as cli", "cli.ArgumentParser"),
        ("from argparse import ArgumentParser as Parser", "Parser"),
    ],
)
def test_args_lists_static_interfaces_without_execution(
    tmp_path: Path,
    imports: str,
    constructor: str,
) -> None:
    """Expose aliases, subcommands and declared constraints without imports."""
    package = _make_test_names_package(tmp_path, "example", "")
    (package / "main.py").write_text(
        f"{imports}\nraise RuntimeError('must not execute')\n"
        f"parser = {constructor}()\n"
        "parser.add_argument('-o', '--output', default='out')\n"
        "commands = parser.add_subparsers()\n"
        "run = commands.add_parser('run')\n"
        "run.add_argument('mode', choices=['fast', 'slow'])\n",
    )
    result = _run(package, "args")
    if result.stdout != (
        "-o, --output  optional; default='out'\n"
        "run: command\n"
        "run: mode  required; choices=['fast', 'slow']\n"
    ):
        message = "Unexpected argument review result"
        raise AssertionError(message)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            (
                "import click\n"
                "@click.command()\n"
                "@click.option('--count', default=1, help='Number')\n"
                "def cli(count): pass\n"
            ),
            "cli: command\ncli: --count  help=Number\n",
        ),
        (
            (
                "import typer\n"
                "app = typer.Typer()\n"
                "@app.command()\n"
                "def greet(name: str, formal: bool = False): pass\n"
            ),
            (
                "greet: command\n"
                "greet: name  required; type=str\n"
                "greet: --formal  default=False; type=bool\n"
            ),
        ),
        (
            "import fire\ndef greet(name='world'): pass\nfire.Fire(greet)\n",
            "greet: command\ngreet: name  default='world'\n",
        ),
    ],
)
def test_args_support_click_fire_and_typer_static_interfaces(
    tmp_path: Path,
    source: str,
    expected: str,
) -> None:
    """Describe common Click, Fire, and Typer declarations without execution."""
    package = _make_test_names_package(tmp_path, "example", "")
    (package / "main.py").write_text(source)
    actual = _run(package, "args")
    if actual.stdout != expected:
        message = (
            f"Unexpected argument review result: {actual.stdout!r} {actual.stderr!r}"
        )
        raise AssertionError(message)


@pytest.mark.parametrize(
    "source",
    [
        "import sys\nprint(sys.argv)\n",
        "import argparse\np = argparse.ArgumentParser()\np.add_argument(dynamic)\n",
        (
            "import argparse\np = argparse.ArgumentParser()\n"
            "for name in names:\n p.add_argument(name)\n"
        ),
    ],
)
def test_args_rejects_unsupported_interfaces(tmp_path: Path, source: str) -> None:
    """Report unsupported interfaces instead of claiming an empty contract."""
    package = _make_test_names_package(tmp_path, "example", "")
    (package / "main.py").write_text(source)
    result = _run(package, "args", code=1)
    if "unsupported CLI interface" not in result.stderr:
        message = "Unexpected argument review result"
        raise AssertionError(message)
    if result.stdout != "":
        message = "Unexpected argument review result"
        raise AssertionError(message)


def test_args_repository_continues_after_unsupported_package(tmp_path: Path) -> None:
    """Keep repository listings useful when some interfaces are unsupported."""
    _make_test_names_package(tmp_path, "alpha", "")
    package = _make_test_names_package(tmp_path, "zebra", "")
    (package / "main.py").write_text(
        "import argparse\np = argparse.ArgumentParser()\n"
        "p.add_argument('--verbose', action='store_true')\n",
    )
    result = _run(tmp_path, "args", code=1)
    if (
        result.stdout
        != (
            "packages/alpha:\npackages/zebra:\n"
            "--verbose  optional; action='store_true'\n"
        )
        or "alpha: unsupported CLI interface" not in result.stderr
    ):
        message = "Repository review did not continue after an unsupported package"
        raise AssertionError(message)


def test_args_git_views_compare_interfaces(repository: Path) -> None:
    """Compare staged and historical declarations while ignoring body edits."""
    package = _make_test_names_package(repository, "example", "")
    source = package / "main.py"
    before = "import argparse\np = argparse.ArgumentParser()\np.add_argument('--old')\n"
    source.write_text(before)
    _git(repository, "add", ".")
    _git(
        repository,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-qm",
        "initial",
    )
    source.write_text(before + "print('implementation only')\n")
    if _run(repository, "args", "diff").stdout != "":
        message = "Unexpected argument review result"
        raise AssertionError(message)
    source.write_text(before.replace("--old", "--new"))
    diff = _run(repository, "args", "diff").stdout
    if "---old  optional" not in diff:
        message = "Unexpected argument review result"
        raise AssertionError(message)
    if "+--new  optional" not in diff:
        message = "Unexpected argument review result"
        raise AssertionError(message)
    if not ("add_argument" not in diff):
        message = "Unexpected argument review result"
        raise AssertionError(message)
    _git(repository, "add", ".")
    if "+--new  optional" not in _run(repository, "args", "diff", "--staged").stdout:
        message = "Unexpected argument review result"
        raise AssertionError(message)
    _git(
        repository,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-qm",
        "change",
    )
    if "+--new  optional" not in _run(repository, "args", "show", "HEAD").stdout:
        message = "Unexpected argument review result"
        raise AssertionError(message)


def test_overview_moves_from_catalog_to_package_behavior(repository: Path) -> None:
    """Expose the reading path without executing the package or its tests."""
    _run(repository, "add", "packages/alpha", "python", "Alpha package")
    _run(repository, "add", "packages/zeta", "nix", "Zeta package")
    package = repository / "packages/alpha"
    (package / "main.py").write_text(
        '"""Explain alpha in more detail."""\n'
        "import argparse\n"
        "raise RuntimeError('must not execute')\n"
        "parser = argparse.ArgumentParser()\n"
        "parser.add_argument('--output', help='Output path')\n",
        encoding="utf-8",
    )
    (package / "test_main.py").write_text(
        "def test_saves_output(): pass\ndef test_rejects_bad_paths(): pass\n",
        encoding="utf-8",
    )
    catalog = _run(repository, "overview").stdout
    if catalog != ("packages/alpha: Alpha package\npackages/zeta: Zeta package\n"):
        raise AssertionError(catalog)
    detail = _run(repository, "overview", "packages/alpha").stdout
    for expected in (
        "Name: alpha\n",
        "Description: Alpha package\n",
        "Help: Explain alpha in more detail.\n",
        "  --output  optional; help='Output path'\n",
        "  test saves output\n",
        "  test rejects bad paths\n",
    ):
        if expected not in detail:
            raise AssertionError(detail)
    full = _run(repository, "overview", "--full").stdout
    if "packages/alpha:\n" not in full or "packages/zeta:\n" not in full:
        raise AssertionError(full)
    if "  (not applicable)\n" not in full:
        raise AssertionError(full)
    if "  (not declared)\n" not in full:
        raise AssertionError(full)


def test_overview_counts_local_and_global_suppressions(repository: Path) -> None:
    """Count directives in comments across Python and HTML package sources."""
    _run(repository, "add", "packages/python_sample", "python")
    python_package = repository / "packages/python_sample"
    (python_package / "main.py").write_text(
        '"""# noqa is text."""\n'
        "# ruff: noqa: D\n"
        "value = 1  # noqa: E501, F401\n"
        "other = 2  # type: ignore[assignment]\n",
        encoding="utf-8",
    )
    (python_package / "test_main.py").write_text(
        "def test_one(): pass  # noqa: D103\n",
        encoding="utf-8",
    )
    detail = _run(repository, "overview", "packages/python_sample").stdout
    for expected in (
        "main.py: noqa (global): 1",
        "main.py: noqa (local): 1",
        "main.py: type: ignore (local): 1",
        "test_main.py: noqa (local): 1",
    ):
        if expected not in detail:
            raise AssertionError(detail)
    _run(repository, "add", "packages/html_sample", "html")
    html_package = repository / "packages/html_sample"
    (html_package / "index.html").write_text(
        "<!-- html-validate-disable -->\n"
        "<!-- html-validate-disable-next heading-level -->\n"
        "<p>eslint-disable is text</p>\n",
        encoding="utf-8",
    )
    (html_package / "script.js").write_text(
        'const text = "// eslint-disable";\n'
        "/* eslint-disable no-alert */\n"
        "alert(text); // eslint-disable-line no-alert\n",
        encoding="utf-8",
    )
    (html_package / "style.css").write_text(
        'p::before { content: "/* stylelint-disable */"; }\n'
        "/* stylelint-disable color-no-invalid-hex */\n",
        encoding="utf-8",
    )
    detail = _run(repository, "overview", "packages/html_sample").stdout
    for expected in (
        "index.html: html-validate-disable (global): 1",
        "index.html: html-validate-disable (local): 1",
        "script.js: eslint-disable (global): 1",
        "script.js: eslint-disable (local): 1",
        "style.css: stylelint-disable (global): 1",
    ):
        if expected not in detail:
            raise AssertionError(detail)


def test_overview_lists_checked_out_home_flakes(home_repository: Path) -> None:
    """Include the repository path above packages in a canonical home."""
    checkout = home_repository / "forge.example/owner/demo"
    (checkout / "flake.nix").write_text("", encoding="utf-8")
    (checkout / ".gitignore").write_text("", encoding="utf-8")
    _run(checkout, "add", "packages/example", "nix", "Example package")
    output = _run(home_repository, "overview").stdout
    if output != ("forge.example/owner/demo:\npackages/example: Example package\n"):
        raise AssertionError(output)


def test_overview_dependencies_resolve_aliases_sources_and_skip_comments(
    repository: Path,
) -> None:
    """Parse Canonical references without evaluating Nix or importing package code."""
    _run(repository, "add", "packages/consumer", "python", "Consumer")
    _run(repository, "add", "packages/core", "nix", "Core")
    source = """{ inputs, pkgs, ... }:
let
  python = pkgs.python3;
  local = inputs.self.packages.${pkgs.stdenv.system};
  pythonDeps = with python.pkgs; [ numpy local.core ];
  nativeDeps = [ pkgs.makeWrapper ];
in python.pkgs.buildPythonPackage {
  propagatedBuildInputs = pythonDeps ++ [ pkgs.git ];
  nativeBuildInputs = nativeDeps;
  # propagatedBuildInputs = [ inputs.self.packages.${system}.fake ];
  description = "inputs.self.packages.system.also_fake";
  installPhase = "cp ${../core/main.py} result";
}
"""
    package = repository / "packages/consumer"
    (package / "default.nix").write_text(source, encoding="utf-8")
    (package / "main.py").write_text(
        "raise RuntimeError('must not run')\n",
        encoding="utf-8",
    )
    detail = _run(repository, "overview", "packages/consumer").stdout
    for expected in (
        "Dependencies:",
        "runtime: packages/core",
        "source: packages/core",
    ):
        if expected not in detail:
            raise AssertionError(detail)
    if any(
        excluded in detail
        for excluded in (
            "packages/fake",
            "packages/also_fake",
            "pkgs.python3.pkgs.numpy",
            "pkgs.makeWrapper",
            "runtime: pkgs.git",
        )
    ):
        raise AssertionError(detail)
    first = _run(repository, "overview", "--json").stdout
    if _run(repository, "overview", "--json").stdout != first:
        msg = "Identical source must produce byte-identical JSON"
        raise AssertionError(msg)
    data = json.loads(first)
    if (
        data["schema"] != "canonical.overview"
        or data["analysis"] != "source-declarations"
        or any(node["kind"] == "dependency" for node in data["nodes"])
    ):
        raise AssertionError(data)
    if not any(
        edge["source"] == ".:packages/core"
        and edge["target"] == ".:packages/consumer"
        and edge["kind"] == "runtime"
        for edge in data["edges"]
    ):
        raise AssertionError(data)
    if any(
        not dependency["target"].startswith("packages/")
        for node in data["nodes"]
        for dependency in node.get("dependencies", [])
    ):
        raise AssertionError(data)
    focus = json.loads(
        _run(repository, "overview", "packages/consumer", "--json").stdout,
    )
    if focus["focus"] != ".:packages/consumer":
        raise AssertionError(focus)
    (package / "default.nix").write_text(
        source.replace("local.core", "local.missing"),
        encoding="utf-8",
    )
    changed = json.loads(_run(repository, "overview", "--json").stdout)
    if not any(
        node["id"] == ".:packages/missing" and node["kind"] == "package-reference"
        for node in changed["nodes"]
    ):
        raise AssertionError(changed)


def test_overview_dependency_cycles_and_computed_expressions_are_opaque(
    repository: Path,
) -> None:
    """Avoid inventing active dependencies for recursive aliases and conditions."""
    _run(repository, "add", "packages/computed", "nix", "Computed dependencies")
    (repository / "packages/computed/default.nix").write_text(
        """{ pkgs, ... }: let
      one = two; two = one;
    in { buildInputs = one;
      nativeBuildInputs = if pkgs.stdenv.isLinux then [ pkgs.git ] else []; }
    """,
        encoding="utf-8",
    )
    detail = _run(repository, "overview", "packages/computed").stdout
    if "Dependencies:\n  (none)" not in detail:
        raise AssertionError(detail)
    data = json.loads(_run(repository, "overview", "--json").stdout)
    if any(node.get("dependencies") for node in data["nodes"]):
        raise AssertionError(data)


def test_overview_json_namespaces_home_packages_and_links_checks(
    home_repository: Path,
) -> None:
    """Keep same-named packages distinct and expose Canonical check relationships."""
    for relative in ("forge.example/owner/demo", "forge.example/owner/second"):
        root = home_repository / relative
        root.mkdir(parents=True, exist_ok=True)
        (root / "flake.nix").write_text("", encoding="utf-8")
        (root / "packages/same").mkdir(parents=True)
        (root / "packages/same/default.nix").write_text(
            '{ meta.description = "Same"; }',
            encoding="utf-8",
        )
        (root / "checks/same").mkdir(parents=True)
        (root / "checks/same/default.nix").write_text("{}", encoding="utf-8")
    with (home_repository / ".gitmodules").open("a", encoding="utf-8") as stream:
        stream.write('[submodule "second"]\npath = forge.example/owner/second\n')
    data = json.loads(_run(home_repository, "overview", "--json").stdout)
    ids = {node["id"] for node in data["nodes"]}
    for scope in ("forge.example/owner/demo", "forge.example/owner/second"):
        if f"{scope}:packages/same" not in ids:
            raise AssertionError(data)
        if not any(
            edge["source"] == f"{scope}:packages/same"
            and edge["target"] == f"{scope}:checks/same"
            and edge["kind"] == "checked-by"
            for edge in data["edges"]
        ):
            raise AssertionError(data)
    if len(ids) != len(data["nodes"]):
        raise AssertionError(data)


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("package_name", ["example", "my-package"])
def test_names_package_names_become_sentences_without_executing_source(
    tmp_path: Path,
    package_name: str,
    *,
    explicit: bool,
) -> None:
    """Preserve the test prefix and parse decorators, async tests and test classes."""
    source = (
        "raise RuntimeError('must not execute')\n"
        "@unknown_decorator()\n"
        "def test_saves_valid_input(): pass\n"
        "async def test_async_behavior(): pass\n"
        "def helper():\n    def test_nested(): pass\n"
        "class Helper:\n    def test_hidden(self): pass\n"
        "class TestBehavior:\n    def test_method(self): pass\n"
        "def test_double__underscore(): pass\n"
    )
    package = _make_test_names_package(tmp_path, package_name, source)
    result = _run_test_names(
        tmp_path if explicit else package,
        *([str(package)] if explicit else []),
    )
    if not (result.returncode == 0):
        raise AssertionError(result.stderr)
    if result.stdout != (
        "test async behavior\ntest double  underscore\ntest method\n"
        "test saves valid input\n"
    ):
        msg = f"Unexpected CLI result: {result}"
        raise AssertionError(msg)
    if result.stderr != "":
        msg = "Expectation failed: result.stderr == ''"
        raise AssertionError(msg)
    if (package / "test_main.py").read_text() != source:
        msg = "Expectation failed: (package / 'test_main.py').read_text() == source"
        raise AssertionError(msg)
    if (tmp_path / "tmp").exists():
        msg = "Expectation failed: not (tmp_path / 'tmp').exists()"
        raise AssertionError(msg)


def test_names_unittest_aliases_and_local_subclasses_are_recognized(
    tmp_path: Path,
) -> None:
    """List methods of statically recognized unittest classes."""
    package = _make_test_names_package(
        tmp_path,
        "example",
        "import unittest as unit\n"
        "from unittest import TestCase as Case, IsolatedAsyncioTestCase\n"
        "class Reports(unit.TestCase):\n    def test_report(self): pass\n"
        "class Base(Case): pass\n"
        "class Derived(Base):\n    def test_derived(self): pass\n"
        "class AsyncChecks(IsolatedAsyncioTestCase):\n"
        "    async def test_async(self): pass\n",
    )
    result = _run_test_names(package)
    if not (result.returncode == 0):
        raise AssertionError(result.stderr)
    if result.stdout != "test async\ntest derived\ntest report\n":
        msg = f"Unexpected CLI result: {result}"
        raise AssertionError(msg)


@pytest.mark.parametrize("explicit", [False, True])
def names_repository_groups_sorted_packages_and_skips_untested_packages(
    tmp_path: Path,
    *,
    explicit: bool,
) -> None:
    """Match runner target discovery while reporting sentences by package."""
    _make_test_names_package(tmp_path, "zebra", "def test_last(): pass\n")
    _make_test_names_package(tmp_path, "alpha", "def test_first(): pass\n")
    missing = _make_test_names_package(tmp_path, "untested", "")
    (missing / "test_main.py").unlink()
    (tmp_path / "packages/linked").symlink_to(missing, target_is_directory=True)
    result = _run_test_names(tmp_path, *([str(tmp_path)] if explicit else []))
    if not (result.returncode == 0):
        raise AssertionError(result.stderr)
    if result.stdout != "packages/alpha:\ntest first\npackages/zebra:\ntest last\n":
        msg = f"Unexpected CLI result: {result}"
        raise AssertionError(msg)
    if result.stderr != "Skipping untested: no test_main.py\n":
        msg = f"Unexpected CLI result: {result}"
        raise AssertionError(msg)


def names_repository_continues_after_a_malformed_test_file(tmp_path: Path) -> None:
    """Report a parse error while still printing later packages."""
    _make_test_names_package(tmp_path, "broken", "def invalid(")
    _make_test_names_package(tmp_path, "valid", "def test_still_listed(): pass\n")
    result = _run_test_names(tmp_path)
    if result.returncode != 1:
        msg = "Expectation failed: result.returncode == 1"
        raise AssertionError(msg)
    if "test still listed\n" not in result.stdout:
        msg = "Expectation failed: 'test still listed\\n' in result.stdout"
        raise AssertionError(msg)
    if "git canonical test names: broken:" not in result.stderr:
        msg = "Expectation failed: 'git canonical test names: broken:' in result.stderr"
        raise AssertionError(msg)


@pytest.mark.parametrize("layout", ["missing", "syntax", "encoding", "linked"])
def test_names_invalid_test_files_return_failure(tmp_path: Path, layout: str) -> None:
    """Reject missing, malformed, undecodable and linked source files."""
    package = _make_test_names_package(tmp_path, "example", "")
    source = package / "test_main.py"
    if layout == "missing":
        source.unlink()
    elif layout == "syntax":
        source.write_text("def invalid(")
    elif layout == "encoding":
        source.write_bytes(b"\xff")
    else:
        source.unlink()
        source.symlink_to(package / "main.py")
    result = _run_test_names(package)
    if result.returncode != 1:
        msg = "Expectation failed: result.returncode == 1"
        raise AssertionError(msg)
    if "git canonical test names:" not in result.stderr:
        msg = "Expectation failed: 'git canonical test names:' in result.stderr"
        raise AssertionError(msg)
    if result.stdout != "":
        msg = "Expectation failed: result.stdout == ''"
        raise AssertionError(msg)


def test_names_empty_test_file_succeeds_without_sentences(tmp_path: Path) -> None:
    """An empty test file has no specifications to print."""
    package = _make_test_names_package(tmp_path, "example", "")
    result = _run_test_names(package)
    if result.returncode != 0:
        msg = "Expectation failed: result.returncode == 0"
        raise AssertionError(msg)
    if not (result.stdout == result.stderr == ""):
        msg = "Expectation failed: result.stdout == result.stderr == ''"
        raise AssertionError(msg)


def test_names_cli_help_invalid_targets_and_unsupported_options(tmp_path: Path) -> None:
    """Share target and help conventions without accepting execution budgets."""
    usage_error = 2
    result = _run_test_names(tmp_path, "--help")
    if result.returncode != 0:
        msg = "Expectation failed: result.returncode == 0"
        raise AssertionError(msg)
    if "[target]" not in result.stdout:
        msg = "Expectation failed: '[target]' in result.stdout"
        raise AssertionError(msg)
    if "current directory" not in result.stdout:
        msg = "Expectation failed: 'current directory' in result.stdout"
        raise AssertionError(msg)
    if _run_test_names(tmp_path).returncode != 1:
        msg = "Expectation failed: run_cli(tmp_path).returncode == 1"
        raise AssertionError(msg)
    if _run_test_names(tmp_path, "--timeout", "60").returncode != usage_error:
        msg = f"Unexpected CLI result: {result}"
        raise AssertionError(msg)
    if _run_test_names(tmp_path, "--max-examples", "100").returncode != usage_error:
        msg = f"Unexpected CLI result: {result}"
        raise AssertionError(msg)
    (tmp_path / "flake.nix").touch()
    result = _run_test_names(tmp_path)
    if result.returncode != 1:
        msg = "Expectation failed: result.returncode == 1"
        raise AssertionError(msg)
    if "no Python packages found" not in result.stderr:
        msg = "Expectation failed: 'no Python packages found' in result.stderr"
        raise AssertionError(msg)


def _test_names_git(root: Path, *arguments: str) -> str:
    """Run fixture Git commands with a local identity and no signing."""
    return subprocess.run(  # noqa: S603
        [  # noqa: S607
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *arguments,
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    ).stdout


@pytest.fixture
def names_repository(tmp_path: Path) -> Path:
    """Create distinct previous, HEAD, staged and working-tree sentences."""
    _test_names_git(tmp_path, "init", "-q")
    package = _make_test_names_package(
        tmp_path,
        "example",
        "def test_previous(): pass\n",
    )
    _test_names_git(tmp_path, "add", ".")
    _test_names_git(tmp_path, "commit", "-qm", "Initial tests")
    source = package / "test_main.py"
    source.write_text("def test_committed(): pass\n")
    _test_names_git(tmp_path, "add", ".")
    _test_names_git(tmp_path, "commit", "-qm", "Rename the test")
    source.write_text("def test_staged(): pass\n")
    _test_names_git(tmp_path, "add", ".")
    source.write_text("def test_working(): pass\n")
    (package / "main.py").write_text("PRIVATE_IMPLEMENTATION = 1\n")
    return tmp_path


@pytest.mark.parametrize(
    ("arguments", "removed", "added"),
    [
        (("diff",), "staged", "working"),
        (("diff", "--staged"), "committed", "staged"),
        (("diff", "--cached"), "committed", "staged"),
        (("diff", "HEAD"), "committed", "working"),
        (("diff", "HEAD~1", "HEAD"), "previous", "committed"),
        (("show",), "previous", "committed"),
        (("show", "HEAD"), "previous", "committed"),
        (("diff", "-R"), "working", "staged"),
    ],
)
def test_names_git_compares_sentences_using_native_revision_and_index_semantics(
    names_repository: Path,
    arguments: tuple[str, ...],
    removed: str,
    added: str,
) -> None:
    """Let Git select versions while exposing only test-name sentences."""
    result = _run_test_names(names_repository, *arguments)
    if result.returncode != 0 or result.stderr:
        raise AssertionError(result)
    if (
        f"-test {removed}\n" not in result.stdout
        or f"+test {added}\n" not in result.stdout
    ):
        raise AssertionError(result.stdout)
    if "def test_" in result.stdout or "PRIVATE_IMPLEMENTATION" in result.stdout:
        raise AssertionError(result.stdout)
    if arguments[0] == "show" and "Rename the test" not in result.stdout:
        raise AssertionError(result.stdout)


def test_names_git_path_filters_intersect_test_scope_and_work_from_subdirectories(
    names_repository: Path,
) -> None:
    """Explicit source paths never widen the test-only view."""
    _make_test_names_package(names_repository, "other", "def test_other(): pass\n")
    _test_names_git(names_repository, "add", "packages/other")
    result = _run_test_names(
        names_repository,
        "diff",
        "HEAD",
        "--",
        "packages/example",
    )
    if "+test working" not in result.stdout or "test other" in result.stdout:
        raise AssertionError(result)
    excluded = _run_test_names(
        names_repository,
        "diff",
        "HEAD",
        "--",
        "packages/example/main.py",
    )
    if excluded.returncode != 0 or excluded.stdout:
        raise AssertionError(excluded)
    local = _run_test_names(
        names_repository / "packages/example",
        "diff",
        "HEAD",
        "--",
        ".",
    )
    if local.returncode != 0 or local.stdout != result.stdout:
        raise AssertionError(local)
    all_packages = _run_test_names(
        names_repository / "packages/example",
        "diff",
        "HEAD",
    )
    if all_packages.returncode != 0 or "+test other" not in all_packages.stdout:
        raise AssertionError(all_packages)


def test_names_body_edits_and_untracked_files_have_no_sentence_patch(
    names_repository: Path,
) -> None:
    """Keep Git tracking rules and hide edits outside extracted names."""
    (names_repository / "packages/example/test_main.py").write_text(
        "raise RuntimeError('never execute')\ndef test_staged(): assert False\n",
    )
    _make_test_names_package(
        names_repository,
        "untracked",
        "def test_untracked(): pass\n",
    )
    result = _run_test_names(names_repository, "diff")
    if result.returncode != 0 or result.stdout or result.stderr:
        raise AssertionError(result)


def test_names_deleted_packages_and_initial_commits_use_historical_sources(
    names_repository: Path,
) -> None:
    """Do not require historical packages to exist in the working tree."""
    _test_names_git(names_repository, "rm", "-rf", "packages/example")
    _test_names_git(names_repository, "commit", "-qm", "Delete package")
    deleted = _run_test_names(names_repository, "show")
    initial = _run_test_names(names_repository, "show", "HEAD~2")
    historical = _run_test_names(names_repository, "diff", "HEAD~2", "HEAD~1")
    for result, sentence in (
        (deleted, "-test committed"),
        (initial, "+test previous"),
        (historical, "+test committed"),
    ):
        if result.returncode != 0 or sentence not in result.stdout:
            raise AssertionError(result)


def test_names_git_preserves_exit_codes_and_reports_parse_and_revision_errors(
    names_repository: Path,
) -> None:
    """Surface Git failures and converter failures without raw source output."""
    changed = _run_test_names(names_repository, "diff", "--exit-code")
    if changed.returncode != 1 or "+test working" not in changed.stdout:
        raise AssertionError(changed)
    missing = _run_test_names(
        names_repository,
        "diff",
        "nonexistent-revision",
        "--",
    )
    if missing.returncode == 0 or not missing.stderr:
        raise AssertionError(missing)
    (names_repository / "packages/example/test_main.py").write_text("def invalid(")
    malformed = _run_test_names(names_repository, "diff")
    if malformed.returncode == 0 or "git canonical test names:" not in malformed.stderr:
        raise AssertionError(malformed)


def test_names_git_rejects_conflicting_attributes_without_changing_repository(
    names_repository: Path,
) -> None:
    """Temporary Git configuration cannot silently lose to repository attributes."""
    attributes = names_repository / ".gitattributes"
    attributes.write_text("packages/*/test_main.py diff=custom\n")
    preserved = [
        names_repository / ".git/config",
        names_repository / ".git/index",
        attributes,
        names_repository / "packages/example/test_main.py",
    ]
    before = [path.read_bytes() for path in preserved]
    result = _run_test_names(names_repository, "diff")
    if result.returncode == 0 or "conflicting diff attribute" not in result.stderr:
        raise AssertionError(result)
    if result.stdout or before != [path.read_bytes() for path in preserved]:
        raise AssertionError(result)


def test_names_git_views_leave_configuration_index_sources_and_refs_unchanged(
    names_repository: Path,
) -> None:
    """Do not install attributes, stage files, or cache converted blobs."""
    paths = [
        names_repository / ".git/config",
        names_repository / ".git/index",
        names_repository / "packages/example/test_main.py",
    ]
    before = [path.read_bytes() for path in paths]
    refs = _test_names_git(names_repository, "show-ref")
    for arguments in (("diff",), ("diff", "--staged"), ("show",)):
        result = _run_test_names(names_repository, *arguments)
        if result.returncode != 0:
            raise AssertionError(result)
    if before != [path.read_bytes() for path in paths] or refs != _test_names_git(
        names_repository,
        "show-ref",
    ):
        message = "Git view changed repository state"
        raise AssertionError(message)
    if (names_repository / ".gitattributes").exists() or (
        names_repository / ".git/info/attributes"
    ).exists():
        message = "Git view installed repository attributes"
        raise AssertionError(message)


@pytest.mark.parametrize(
    "arguments",
    [
        ("diff", "--no-index"),
        ("diff", "--no-textconv"),
        ("diff", "--check"),
        ("show", "HEAD:packages/example/test_main.py"),
        ("show", "HEAD^{tree}"),
    ],
)
def test_names_git_rejects_views_that_cannot_produce_test_name_diffs(
    names_repository: Path,
    arguments: tuple[str, ...],
) -> None:
    """Blob/tree display and raw-source modes are not sentence diffs."""
    result = _run_test_names(names_repository, *arguments)
    if result.returncode == 0 or result.stdout or not result.stderr:
        raise AssertionError(result)


@pytest.mark.parametrize("committed", [False, True])
def test_names_git_rejects_symlink_diffs_in_working_and_historical_files(
    names_repository: Path,
    *,
    committed: bool,
) -> None:
    """Git must not display symlink targets as test sentences."""
    source = names_repository / "packages/example/test_main.py"
    source.unlink()
    source.symlink_to("PRIVATE_TARGET")
    if committed:
        _test_names_git(names_repository, "add", "packages/example/test_main.py")
        _test_names_git(names_repository, "commit", "-qm", "Link test file")
        source.unlink()
        source.write_text("def test_working(): pass\n")
    result = _run_test_names(names_repository, "show" if committed else "diff")
    if result.returncode == 0 or result.stdout or "regular files" not in result.stderr:
        raise AssertionError(result)


def test_names_git_rename_detection_and_formatting_remain_native(
    names_repository: Path,
) -> None:
    """Git handles file renames and user-selected patch presentation."""
    _test_names_git(names_repository, "reset", "--hard", "HEAD")
    _test_names_git(names_repository, "mv", "packages/example", "packages/renamed")
    renamed = _run_test_names(names_repository, "diff", "--staged", "-M")
    if (
        renamed.returncode != 0
        or "rename to packages/renamed/test_main.py" not in renamed.stdout
    ):
        raise AssertionError(renamed)
    if "main.py" in renamed.stdout.replace("test_main.py", ""):
        raise AssertionError(renamed.stdout)
    formatted = _run_test_names(
        names_repository,
        "show",
        "--format=%s",
        "--color=never",
        "-U0",
    )
    if formatted.returncode != 0 or not formatted.stdout.startswith(
        "Rename the test\n",
    ):
        raise AssertionError(formatted)


def test_names_merge_show_uses_gits_combined_sentence_diff(
    names_repository: Path,
) -> None:
    """Preserve combined merge presentation instead of inventing a first parent."""
    _test_names_git(names_repository, "reset", "--hard", "HEAD")
    _test_names_git(names_repository, "branch", "side", "HEAD~1")
    _test_names_git(names_repository, "checkout", "-q", "side")
    (names_repository / "packages/example/test_main.py").write_text(
        "def test_side(): pass\n",
    )
    _test_names_git(names_repository, "commit", "-qam", "Side change")
    _test_names_git(names_repository, "checkout", "-q", "-")
    subprocess.run(
        [  # noqa: S607
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "merge",
            "--no-commit",
            "side",
        ],
        cwd=names_repository,
        capture_output=True,
        check=False,
        timeout=10,
    )
    (names_repository / "packages/example/test_main.py").write_text(
        "def test_merged(): pass\n",
    )
    _test_names_git(names_repository, "add", "packages/example/test_main.py")
    _test_names_git(names_repository, "commit", "-qm", "Resolve merge")
    result = _run_test_names(names_repository, "show", "--color=never")
    if (
        result.returncode != 0
        or "diff --cc" not in result.stdout
        or "++test merged" not in result.stdout
    ):
        raise AssertionError(result)


def test_names_staged_diff_works_before_the_first_commit(tmp_path: Path) -> None:
    """Let Git compare an unborn HEAD to newly staged tests."""
    _test_names_git(tmp_path, "init", "-q")
    _make_test_names_package(tmp_path, "example", "def test_first(): pass\n")
    _test_names_git(tmp_path, "add", ".")
    result = _run_test_names(tmp_path, "diff", "--staged")
    if result.returncode != 0 or "+test first" not in result.stdout:
        raise AssertionError(result)


def test_names_git_views_preserve_definition_order_while_listings_remain_sorted(
    tmp_path: Path,
) -> None:
    """Appended tests remain appended in patches, including methods and async tests."""
    _test_names_git(tmp_path, "init", "-q")
    package = _make_test_names_package(tmp_path, "example", "def test_zebra(): pass\n")
    _test_names_git(tmp_path, "add", ".")
    _test_names_git(tmp_path, "commit", "-qm", "Initial test")
    source = package / "test_main.py"
    source.write_text(
        "def test_zebra(): pass\n"
        "class TestBehavior:\n    def test_middle(self): pass\n"
        "async def test_alpha(): pass\n",
    )
    expected = " test zebra\n+test middle\n+test alpha\n"
    working = _run_test_names(tmp_path, "diff", "--color=never")
    _test_names_git(tmp_path, "add", ".")
    staged = _run_test_names(tmp_path, "diff", "--staged", "--color=never")
    _test_names_git(tmp_path, "commit", "-qm", "Append tests")
    committed = _run_test_names(tmp_path, "show", "--color=never")
    for result in (working, staged, committed):
        if result.returncode != 0 or expected not in result.stdout:
            raise AssertionError(result)
    listing = _run_test_names(package)
    if (
        listing.returncode != 0
        or listing.stdout != "test alpha\ntest middle\ntest zebra\n"
    ):
        raise AssertionError(listing)


@pytest.mark.parametrize("arguments", [(), ("diff",), ("diff", "--staged"), ("show",)])
def test_names_are_available_through_gits_canonical_subcommand(
    names_repository: Path,
    arguments: tuple[str, ...],
) -> None:
    """The public Git command preserves listing, patch output and exit status."""
    environment = dict(os.environ)
    executable_directory = os.path.dirname(environment["PACKAGE_E2E_EXECUTABLE"])  # noqa: PTH120
    environment["PATH"] = executable_directory + os.pathsep + environment["PATH"]
    expected = _run_test_names(names_repository, *arguments)
    result = subprocess.run(  # noqa: S603
        ["git", "canonical", "test", "names", *arguments],  # noqa: S607
        cwd=names_repository,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if (result.returncode, result.stdout, result.stderr) != (
        expected.returncode,
        expected.stdout,
        expected.stderr,
    ):
        raise AssertionError(result)


def _make_runner_target(
    root: Path,
    source: str,
    tests: str,
    *,
    name: str = "example",
) -> Path:
    """Create a minimal canonical package for an isolated test."""
    package = root / "packages" / name
    package.mkdir(parents=True)
    (root / "flake.nix").write_text("{}", encoding="utf-8")
    (package / "default.nix").write_text("{}", encoding="utf-8")
    (package / "main.py").write_text(source, encoding="utf-8")
    (package / "test_main.py").write_text(tests, encoding="utf-8")
    return package


def _prepare_runner_flake(
    root: Path,
    tests: str,
    source: str = "def main():\n    print('ready')\n",
    *,
    name: str = "example",
) -> dict[str, str]:
    """Provide an offline flake backed by this check's real Python environment."""
    package = _make_runner_target(root, source, tests, name=name)
    (package / "main.py").write_text(source, encoding="utf-8")
    dependency = root / "prm/nixpkgs"
    dependency.mkdir(parents=True)
    (dependency / "flake.nix").write_text("{ outputs = _: {}; }", encoding="utf-8")
    (dependency / "default.nix").write_text(
        "_: { lib = { concatMap = f: xs: builtins.concatLists (map f xs); "
        'makeBinPath = _: ""; }; writeText = builtins.toFile; }',
        encoding="utf-8",
    )
    (root / "flake.nix").write_text(
        '{ inputs.nixpkgs.url = "path:./prm/nixpkgs"; '
        "outputs = _: { packages.${builtins.currentSystem}."
        f"{json.dumps(name)}.python"
        ".withPackages = "
        f"_ : {json.dumps(sys.prefix)}; }}; }}",
        encoding="utf-8",
    )
    for args in (["init", "--quiet"], ["add", "."]):
        subprocess.run(  # noqa: S603
            ["git", "-C", str(root), *args],  # noqa: S607
            check=True,
            capture_output=True,
            timeout=10,
        )
    environment = dict(os.environ)
    store = root.parent / "nix"
    environment["NIX_REMOTE"] = (
        f"local?store={store / 'store'}&state={store / 'state'}&log={store / 'log'}"
    )
    environment["NIX_CONFIG"] = (
        "experimental-features = nix-command flakes\nbuild-users-group =\n"
    )
    return environment


def _run_runner_cli(
    root: Path,
    environment: dict[str, str],
    command: str,
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    """Exercise test runners through Git's public external-command lookup."""
    environment = dict(environment)
    executable_directory = os.path.dirname(os.environ["PACKAGE_E2E_EXECUTABLE"])  # noqa: PTH120
    environment["PATH"] = executable_directory + os.pathsep + environment["PATH"]
    return subprocess.run(  # noqa: S603
        ["git", "canonical", "test", command, str(root), *arguments],  # noqa: S607
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def test_hypothesis_generates_examples_and_executes_cli_without_changing_source(
    tmp_path: Path,
) -> None:
    """Discover a package, generate examples, and retain diagnostics through the CLI."""
    root = tmp_path / "source with spaces"
    tests = (
        "import os, subprocess\n"
        "from hypothesis import given, example, strategies as st\n"
        "from pathlib import Path\n"
        "@given(st.integers())\n@example(0)\n"
        "def test_property(value):\n"
        "    with Path('examples').open('a') as output:\n"
        "        output.write(str(value) + '\\n')\n"
        "def test_cli():\n"
        "    result = subprocess.run([os.environ['PACKAGE_E2E_EXECUTABLE']],\n"
        "        capture_output=True, text=True)\n"
        "    assert result.returncode == 0\n"
        "    assert result.stdout == 'ready\\n'\n"
    )
    environment = _prepare_runner_flake(root, tests)
    original = (root / "packages/example/main.py").read_bytes()
    result = _run_runner_cli(root, environment, "hypothesis", "--max-examples", "5")
    if result.returncode or "1 passed, 0 failed, 0 skipped" not in result.stdout:
        raise AssertionError(result.stdout + result.stderr)
    (workspace,) = (root / "tmp").glob("python-hypothesis-example-*")
    expected_examples = 6
    if len((workspace / "examples").read_text().splitlines()) != expected_examples:
        msg = "one explicit and five generated examples must run"
        raise AssertionError(msg)
    if "5 passing examples" not in (workspace / "tests.log").read_text():
        msg = "retained diagnostics must include Hypothesis statistics"
        raise AssertionError(msg)
    if (root / "packages/example/main.py").read_bytes() != original or (
        root / "packages/example/examples"
    ).exists():
        msg = "running tests must leave the source untouched"
        raise AssertionError(msg)


@pytest.mark.parametrize(
    ("tests", "timeout", "diagnostic"),
    [
        (
            (
                "from hypothesis import given, strategies as st\n"
                "@given(st.integers())\n"
                "def test_failure(value):\n    assert value != 0\n"
            ),
            "20",
            "Falsifying example",
        ),
        ("import time\ndef test_stalled():\n    time.sleep(30)\n", "0.5", "timed out"),
    ],
)
def test_hypothesis_failed_or_stalled_suites_return_failure_and_retain_diagnostics(
    tmp_path: Path,
    tests: str,
    timeout: str,
    diagnostic: str,
) -> None:
    """Fail with retained diagnostics for counterexamples and stalled suites."""
    root = tmp_path / "source"
    environment = _prepare_runner_flake(root, tests)
    result = _run_runner_cli(root, environment, "hypothesis", "--timeout", timeout)
    (workspace,) = (root / "tmp").glob("python-hypothesis-example-*")
    if result.returncode != 1 or "0 passed, 1 failed, 0 skipped" not in result.stdout:
        raise AssertionError(result.stdout + result.stderr)
    if (
        diagnostic
        not in result.stdout + result.stderr + (workspace / "tests.log").read_text()
    ):
        msg = "failure diagnostics were not retained"
        raise AssertionError(msg)


@pytest.mark.parametrize("target_directory", [".", "packages/example"])
def test_hypothesis_omitted_target_runs_current_directory(
    tmp_path: Path,
    target_directory: str,
) -> None:
    """Run the current package or repository without an explicit target."""
    root = tmp_path / "source with spaces"
    environment = _prepare_runner_flake(
        root,
        "from packages.example import main\ndef test_import():\n"
        "    assert main is not None\n",
        "",
    )
    result = subprocess.run(  # noqa: S603
        [os.environ["PACKAGE_E2E_EXECUTABLE"], "test", "hypothesis"],
        cwd=root / target_directory,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if result.returncode:
        raise AssertionError(result.stdout + result.stderr)
    if not (root / "tmp").is_dir():
        msg = "current-directory run did not retain diagnostics"
        raise AssertionError(msg)
    if target_directory == "." and "1 passed, 0 failed, 0 skipped" not in result.stdout:
        raise AssertionError(result.stdout)


def test_hypothesis_cli_validation(tmp_path: Path) -> None:
    """The installed command validates its target and resource budgets."""
    for arguments, code in [
        (["--help"], 0),
        ([str(tmp_path)], 1),
        ([str(tmp_path), "--timeout", "nan"], 2),
        ([str(tmp_path), "--timeout", "0"], 2),
        ([str(tmp_path), "--max-examples", "0"], 2),
    ]:
        result = subprocess.run(  # noqa: S603
            [os.environ["PACKAGE_E2E_EXECUTABLE"], "test", "hypothesis", *arguments],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != code:
            raise AssertionError(result.stderr)


@pytest.mark.parametrize(
    "layout",
    ["empty", "nonpython", "untested", "single_untested"],
)
def test_hypothesis_cli_repository_without_runnable_packages(
    tmp_path: Path,
    layout: str,
) -> None:
    """Report empty repositories, skipped packages, and invalid explicit targets."""
    (tmp_path / "flake.nix").write_text("{}", encoding="utf-8")
    target = tmp_path
    if layout == "nonpython":
        package = tmp_path / "packages" / "web"
        package.mkdir(parents=True)
        (package / "default.nix").write_text("{}", encoding="utf-8")
        (package / "index.html").write_text("hello", encoding="utf-8")
    elif layout in {"untested", "single_untested"}:
        package = _make_runner_target(tmp_path, "", "")
        (package / "test_main.py").unlink()
        if layout == "single_untested":
            target = package
    result = subprocess.run(  # noqa: S603
        [os.environ["PACKAGE_E2E_EXECUTABLE"], "test", "hypothesis", str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    expected_code = 0 if layout == "untested" else 1
    if result.returncode != expected_code:
        raise AssertionError(result.stdout + result.stderr)
    if layout == "untested":
        if (
            "Skipping example: no test_main.py" not in result.stdout
            or "0 passed, 0 failed, 1 skipped" not in result.stdout
        ):
            msg = "repository did not report its skipped package"
            raise AssertionError(msg)
    elif (
        layout != "single_untested" and "no Python packages found" not in result.stderr
    ):
        msg = "empty repository diagnostic missing"
        raise AssertionError(msg)
    if (tmp_path / "tmp").exists():
        msg = "a non-runnable target started a workspace"
        raise AssertionError(msg)


def test_mutation_reports_killed_and_surviving_mutations_without_changing_source(
    tmp_path: Path,
) -> None:
    """Discover a package and report mutations exercised by CLI-only tests."""
    root = tmp_path / "source with spaces"
    source = (
        "def value():\n    return 1\n\n"
        "def unused():\n    return 2\n\ndef main():\n    print(value())\n"
    )
    tests = (
        "import os, subprocess\n"
        "def test_cli():\n"
        "    result = subprocess.run([os.environ['PACKAGE_E2E_EXECUTABLE']],\n"
        "        capture_output=True, text=True)\n"
        "    assert result.returncode == 0\n"
        "    assert result.stdout == '1\\n'\n"
    )
    environment = _prepare_runner_flake(root, tests, source)
    result = _run_runner_cli(root, environment, "mutation", "--timeout", "10")
    if result.returncode or "1 passed, 0 failed, 0 skipped" not in result.stdout:
        raise AssertionError(result.stdout + result.stderr)
    (workspace,) = (root / "tmp").glob("python-mutation-example-*")
    summary = json.loads((workspace / "summary.json").read_text())
    if summary.get("killed", 0) <= 0 or summary.get("survived", 0) <= 0:
        raise AssertionError(summary)
    if not (workspace / "report.html").stat().st_size:
        msg = "the campaign must retain a readable report"
        raise AssertionError(msg)
    if (root / "packages/example/main.py").read_text() != source:
        msg = "mutations must never alter the original source"
        raise AssertionError(msg)


@pytest.mark.parametrize(
    "tests",
    [
        "def test_failure():\n    assert False\n",
        "",
        "raise ImportError('missing dependency')\n",
    ],
)
def test_mutation_invalid_baselines_fail_without_publishing_scores(
    tmp_path: Path,
    tests: str,
) -> None:
    """An invalid baseline cannot produce a misleading mutation report."""
    root = tmp_path / "source"
    environment = _prepare_runner_flake(root, tests)
    result = _run_runner_cli(root, environment, "mutation")
    (workspace,) = (root / "tmp").glob("python-mutation-example-*")
    if result.returncode != 1 or "baseline.log" not in result.stderr:
        raise AssertionError(result.stdout + result.stderr)
    if (
        not (workspace / "baseline.log").is_file()
        or (workspace / "summary.json").exists()
    ):
        msg = "retain baseline diagnostics without publishing mutation scores"
        raise AssertionError(msg)


def test_mutation_a_package_without_mutable_code_completes_with_an_empty_report(
    tmp_path: Path,
) -> None:
    """A passing import-only suite needs no artificial mutations to succeed."""
    root = tmp_path / "source"
    environment = _prepare_runner_flake(
        root,
        "from packages.example import main\n"
        "from hypothesis import given, example, strategies as st\n"
        "@given(st.just(1))\n@example(0)\n"
        "def test_explicit_only(value):\n"
        "    assert value == 0\n"
        "    assert main is not None\n",
        "",
    )
    result = _run_runner_cli(root, environment, "mutation")
    (workspace,) = (root / "tmp").glob("python-mutation-example-*")
    if result.returncode or json.loads((workspace / "summary.json").read_text()) != {}:
        raise AssertionError(result.stdout + result.stderr)


@pytest.mark.parametrize("target_directory", [".", "packages/example"])
def test_mutation_omitted_target_runs_current_directory(
    tmp_path: Path,
    target_directory: str,
) -> None:
    """Run the current package or repository without an explicit target."""
    root = tmp_path / "source with spaces"
    environment = _prepare_runner_flake(
        root,
        "from packages.example import main\ndef test_import():\n"
        "    assert main is not None\n",
        "",
    )
    result = subprocess.run(  # noqa: S603
        [os.environ["PACKAGE_E2E_EXECUTABLE"], "test", "mutation"],
        cwd=root / target_directory,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if result.returncode:
        raise AssertionError(result.stdout + result.stderr)
    if not (root / "tmp").is_dir():
        msg = "current-directory run did not retain diagnostics"
        raise AssertionError(msg)
    if target_directory == "." and "1 passed, 0 failed, 0 skipped" not in result.stdout:
        raise AssertionError(result.stdout)


def test_mutation_cli_errors_and_help(tmp_path: Path) -> None:
    """The installed CLI rejects invalid targets and nonfinite timeouts."""
    executable = os.environ["PACKAGE_E2E_EXECUTABLE"]
    for arguments, code in [
        (["--help"], 0),
        ([str(tmp_path)], 1),
        ([str(tmp_path), "--timeout", "nan"], 2),
    ]:
        result = subprocess.run(  # noqa: S603
            [executable, "test", "mutation", *arguments],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != code:
            message = "mutation runner expectation failed"
            raise AssertionError(message)


@pytest.mark.parametrize(
    "layout",
    ["empty", "nonpython", "untested", "single_untested"],
)
def test_mutation_cli_repository_without_runnable_packages(
    tmp_path: Path,
    layout: str,
) -> None:
    """Report empty repositories, skipped packages, and invalid explicit targets."""
    (tmp_path / "flake.nix").write_text("{}", encoding="utf-8")
    target = tmp_path
    if layout == "nonpython":
        package = tmp_path / "packages" / "web"
        package.mkdir(parents=True)
        (package / "default.nix").write_text("{}", encoding="utf-8")
        (package / "index.html").write_text("hello", encoding="utf-8")
    elif layout in {"untested", "single_untested"}:
        package = _make_runner_target(tmp_path, "", "")
        (package / "test_main.py").unlink()
        if layout == "single_untested":
            target = package
    result = subprocess.run(  # noqa: S603
        [os.environ["PACKAGE_E2E_EXECUTABLE"], "test", "mutation", str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    expected_code = 0 if layout == "untested" else 1
    if result.returncode != expected_code:
        raise AssertionError(result.stdout + result.stderr)
    if layout == "untested":
        if (
            "Skipping example: no test_main.py" not in result.stdout
            or "0 passed, 0 failed, 1 skipped" not in result.stdout
        ):
            msg = "repository did not report its skipped package"
            raise AssertionError(msg)
    elif (
        layout != "single_untested" and "no Python packages found" not in result.stderr
    ):
        msg = "empty repository diagnostic missing"
        raise AssertionError(msg)
    if (tmp_path / "tmp").exists():
        msg = "a non-runnable target started a workspace"
        raise AssertionError(msg)


@pytest.mark.parametrize("command", ["hypothesis", "mutation"])
def test_runners_support_dash_case_and_execute_copied_git_subcommands(
    tmp_path: Path,
    command: str,
) -> None:
    """A copied git-canonical executable must win over the installed command."""
    root = tmp_path / "source with spaces"
    source = "def main():\n    print('isolated')\n"
    tests = (
        "import subprocess\n"
        "def test_copied_command():\n"
        "    result = subprocess.run(['git', 'canonical'],\n"
        "        capture_output=True, text=True)\n"
        "    assert result.returncode == 0\n"
        "    assert result.stdout == 'isolated\\n'\n"
    )
    environment = _prepare_runner_flake(root, tests, source, name="git-canonical")
    result = _run_runner_cli(root, environment, command)
    if result.returncode or "1 passed, 0 failed, 0 skipped" not in result.stdout:
        raise AssertionError(result.stdout + result.stderr)
    if (root / "packages/git-canonical/main.py").read_text() != source:
        message = "runner changed the original dash-case package"
        raise AssertionError(message)


def _prepare_coverage_flake(
    root: Path,
    *,
    failure: str | None = None,
) -> dict[str, str]:
    """Build generated checks and coverage variants with offline Nix inputs."""
    root.mkdir(parents=True)
    _git(root, "init", "--quiet")
    for filename in (".gitignore", "flake.nix", "flake.lock", "README"):
        (root / filename).write_text(
            "{}" if filename.endswith((".nix", ".lock")) else "",
        )
    (root / "flake.lock").write_text(
        json.dumps({"nodes": {"root": {}}, "root": "root", "version": 7}),
    )
    _git(root, "add", ".")
    environment = dict(os.environ)
    store = root.parent / "nix"
    environment["NIX_REMOTE"] = (
        f"local?store={store / 'store'}&state={store / 'state'}&log={store / 'log'}"
    )
    environment["NIX_CONFIG"] = (
        "experimental-features = nix-command flakes\nbuild-users-group =\n"
        "sandbox = false\nsandbox-build-dir = /coverage-build\neval-cache = false\n"
    )
    system = subprocess.run(
        ["nix", "eval", "--impure", "--raw", "--expr", "builtins.currentSystem"],  # noqa: S607
        env=environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout
    site_packages = (
        f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
    )
    coverage_root = Path(coverage.__file__).resolve().parents[4]
    bash = shutil.which("bash")
    if bash is None:
        message = "coverage fixtures require bash"
        raise AssertionError(message)
    packages = []
    checks = []
    for name in ("example", "z-last"):
        _run(root, "add", f"packages/{name}", "python")
        package = root / "packages" / name
        source = "def main():\n    print('ready')\n"
        tests = (
            "import os, subprocess\n"
            "from hypothesis import given, example, strategies as st\n"
            "@given(st.just(1))\n@example(0)\n"
            "def test_explicit(value):\n    assert value == 0\n"
            "def test_cli():\n"
            "    result = subprocess.run([os.environ['PACKAGE_E2E_EXECUTABLE']],\n"
            "        capture_output=True, text=True)\n"
            "    assert result.stdout == 'ready\\n'\n"
        )
        if name == "example" and failure == "build":
            tests = "def test_failure(): assert False\n"
        (package / "main.py").write_text(source)
        (package / "test_main.py").write_text(tests)
        installed = root / "prm/installed" / name
        module_name = name.replace("-", "_")
        module = installed / site_packages / module_name
        module.mkdir(parents=True)
        (module / "__init__.py").write_text(source)
        executable = installed / "bin" / name
        executable.parent.mkdir()
        executable.write_text(
            f"#!{sys.executable}\nimport sys\nfrom pathlib import Path\n"
            "sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "
            f"{site_packages!r}))\n"
            f"from {module_name} import main\nmain()\n",
        )
        executable.chmod(0o755)
        packages.append(
            f"{json.dumps(name)} = {{ "
            f"src = builtins.path {{ path = ./packages/{name}; "
            f'name = "{name}-src"; }}; '
            f"outPath = builtins.path {{ path = ./prm/installed/{name}; "
            f'name = "{name}-installed"; }}; '
            f"pname = {json.dumps(module_name)}; cliName = {json.dumps(name)}; "
            "propagatedBuildInputs = []; python = { "
            f"sitePackages = {json.dumps(site_packages)}; "
            f"pkgs.coverage = {json.dumps(str(coverage_root))}; "
            f"withPackages = _: {json.dumps(sys.prefix)}; }}; }};",
        )
        expression = (
            f"import ./checks/{name}/default.nix {{ inherit pkgs; "
            "inputs.self.packages.${system} = packages; }"
        )
        if name == "example" and failure == "report":
            expression = (
                f"({expression}).overrideAttrs "
                '(_: { buildCommand = "mkdir -p $out\\n"; })'
            )
        checks.append(f"{json.dumps(name)} = {expression};")
    _run(root, "converge")
    (root / "flake.nix").write_text(
        "{ outputs = _: let "
        f"system = {json.dumps(system)}; "
        "mkCheck = name: attrs: script: let build = current: (builtins.derivation { "
        "inherit system; inherit (current) name src PACKAGE_E2E_EXECUTABLE; "
        f"builder = {json.dumps(bash)}; "
        f"PATH = {json.dumps(sys.prefix + '/bin:' + environment['PATH'])}; "
        f"PYTHONPATH = {json.dumps(os.pathsep.join(sys.path))}; "
        'args = [ "-e" (builtins.toFile "check-builder" '
        '("export -n src PACKAGE_E2E_EXECUTABLE\\n" + current.buildCommand)) ]; '
        "}) // { overrideAttrs = f: build (current // f current); }; "
        "in build (attrs // { inherit name; buildCommand = script; }); "
        "pkgs = { stdenv.system = system; runCommand = mkCheck; lib = { "
        "concatMap = f: xs: builtins.concatLists (map f xs); "
        'getExe = p: "${p}/bin/${p.cliName}"; }; }; '
        "packages = { "
        + " ".join(packages)
        + " }; in { packages.${system} = packages; "
        "checks.${system} = { " + " ".join(checks) + " }; }; }\n",
    )
    _git(root, "add", ".")
    return environment


@pytest.fixture(scope="module")
def coverage_repository(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, dict[str, str]]:
    """Reuse an unchanged flake and its cached builds across successful CLI cases."""
    root = tmp_path_factory.mktemp("coverage") / "source with spaces"
    return root, _prepare_coverage_flake(root)


def test_ordinary_checks_run_explicit_examples_without_coverage(
    coverage_repository: tuple[Path, dict[str, str]],
) -> None:
    """The shared test invocation succeeds without producing report artifacts."""
    root, environment = coverage_repository
    plain = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "nix",
            "build",
            "--no-link",
            "--print-out-paths",
            "--impure",
            "--expr",
            (
                f"let f = builtins.getFlake {json.dumps('git+' + root.as_uri())}; "
                "in f.checks.${builtins.currentSystem}.example"
            ),
        ],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if plain.returncode:
        raise AssertionError(plain.stdout + plain.stderr)
    if any(
        path.name.startswith(".coverage") or path.name in {"html", "coverage.json"}
        for path in Path(plain.stdout.strip()).iterdir()
    ):
        message = "ordinary checks produced coverage artifacts"
        raise AssertionError(message)


@pytest.mark.parametrize("target", [".", "packages/example", "packages/z-last"])
def test_coverage_builds_instrumented_checks_and_preserves_the_checkout(
    coverage_repository: tuple[Path, dict[str, str]],
    target: str,
) -> None:
    """Build cached HTML reports for explicit and current-directory targets."""
    root, environment = coverage_repository
    before = _git(root, "status", "--porcelain")
    explicit = _run_runner_cli(root / target, environment, "coverage")
    current = subprocess.run(  # noqa: S603
        [os.environ["PACKAGE_E2E_EXECUTABLE"], "test", "coverage"],
        cwd=root / target,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    for result in (explicit, current):
        if result.returncode or "/html/index.html" not in result.stdout:
            raise AssertionError(result.stdout + result.stderr)
    for line in explicit.stdout.splitlines():
        if line.endswith("/html/index.html"):
            report = Path(line.split(": ", 1)[1]).parents[1] / "coverage.json"
            files = json.loads(report.read_text())["files"]
            if len(files) != 1 or not next(iter(files)).endswith("/main.py"):
                raise AssertionError(files)
            if not next(iter(files.values()))["executed_lines"]:
                message = "coverage missed the CLI subprocess"
                raise AssertionError(message)
    if explicit.stdout != current.stdout:
        raise AssertionError(current.stdout)
    if _git(root, "status", "--porcelain") != before or (root / "result").exists():
        message = "coverage changed the checkout"
        raise AssertionError(message)
    if (root / "tmp").exists():
        message = "coverage created local campaign state"
        raise AssertionError(message)


@pytest.mark.parametrize("failure", ["build", "report", "check", "syntax"])
def test_coverage_continues_after_failures_and_skips_packages_without_tests(
    tmp_path: Path,
    failure: str,
) -> None:
    """A failed or missing report cannot hide a later package's successful check."""
    root = tmp_path / "source"
    environment = _prepare_coverage_flake(root, failure=failure)
    if failure == "check":
        (root / "checks/example/default.nix").unlink()
    if failure == "syntax":
        (root / "packages/example/test_main.py").write_text("def invalid(")
    untested = _make_runner_target(root, "", "", name="untested")
    (untested / "test_main.py").unlink()
    (root / "flake.nix").write_text(_git(root, "show", ":flake.nix"))
    result = _run_runner_cli(root, environment, "coverage")
    if result.returncode != 1 or "1 passed, 1 failed, 1 skipped" not in result.stdout:
        raise AssertionError(result.stdout + result.stderr)
    if "z-last:" not in result.stdout or "/html/index.html" not in result.stdout:
        raise AssertionError(result.stdout)
    if "git canonical test coverage: example:" not in result.stderr:
        raise AssertionError(result.stderr)


def test_coverage_rejects_invalid_targets_without_starting_a_build(
    tmp_path: Path,
) -> None:
    """Coverage requires an existing canonical package or nonempty Python repository."""
    _run(tmp_path, "test", "coverage", code=1)
    (tmp_path / "flake.nix").write_text("{}")
    _run(tmp_path, "test", "coverage", code=1)

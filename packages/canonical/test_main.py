# Copyright (c) 2026- Paschalis Bizopoulos
"""Check Canonical's public contracts with explicit regressions and generated cases."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
from contextlib import contextmanager
from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, cast

import coverage
import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import Any


def _run(
    root: Path,
    *arguments: str,
    code: int = 0,
    executable: str | None = None,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(  # noqa: S603
        [executable or os.environ["PACKAGE_E2E_EXECUTABLE"], *arguments],
        cwd=root,
        env=environment,
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
        "outputs = _: { packages.${builtins.currentSystem} = { "
        f"{json.dumps(name)}.python"
        ".withPackages = "
        f"_ : {json.dumps(sys.prefix)}; }}; }}; }}",
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
    """Exercise test runners through the public standalone command."""
    environment = dict(environment)
    executable_directory = os.path.dirname(os.environ["PACKAGE_E2E_EXECUTABLE"])  # noqa: PTH120
    environment["PATH"] = executable_directory + os.pathsep + environment["PATH"]
    return subprocess.run(  # noqa: S603
        ["canonical", "test", command, str(root), *arguments],  # noqa: S607
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


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
        "experimental-features = nix-command flakes\n"
        "build-users-group =\n"
        "sandbox = false\n"
        "sandbox-build-dir = /coverage-build\n"
        "eval-cache = false\n"
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
            "@given(st.just(1))\n"
            "@example(0)\n"
            "def test_explicit(value):\n"
            "    assert value == 0\n"
            "def test_cli():\n"
            "    result = "
            "subprocess.run([os.environ['PACKAGE_E2E_EXECUTABLE']],\n"
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
            f"meta.mainProgram = {json.dumps(name)}; "
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
        "optionalAttrs = condition: attrs: if condition then attrs else {}; "
        "concatMap = f: xs: builtins.concatLists (map f xs); "
        'getExe = p: "${p}/bin/${p.cliName}"; }; }; '
        "packages = { "
        + " ".join(packages)
        + " }; in { packages.${system} = packages; "
        "checks.${system} = { " + " ".join(checks) + " }; }; }\n",
    )
    _git(root, "add", ".")
    return environment


def _home_repository(root: Path) -> Path:
    """Create a home repository with a locally initialized submodule."""
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


def _check_excluded_trees(
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
    subject = import_module("packages.canonical.main")
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


def _check_formatted_host(
    repository: Path,
) -> None:
    """Preserve formatted generated checks through the full formatting pipeline."""
    _run(repository, "add", "hosts/laptop")
    relative = "checks/laptopVmWithDisko/default.nix"
    check = repository / relative
    _run(repository, "--no-cache", relative, executable="treefmt")
    formatted = check.read_text(encoding="utf-8")
    _git(repository, "add", "--", relative)
    index = _git(repository, "ls-files", "--stage")
    result = _run(repository, "converge")
    if (
        check.read_text(encoding="utf-8") != formatted
        or _git(repository, "ls-files", "--stage") != index
        or f"write '{relative}'" in result.stdout
    ):
        message = "convergence regenerated a formatted host check"
        raise AssertionError(message)


def _check_host_upgrade(repository: Path) -> None:
    """Upgrade a generated check without changing its host or adding resources."""
    _run(repository, "add", "hosts/laptop")
    host = repository / "hosts/laptop/configuration.nix"
    original_host = host.read_bytes()
    original_paths = _git(repository, "ls-files").splitlines()
    relative = "checks/laptopVmWithDisko/default.nix"
    check = repository / relative
    current = check.read_bytes()
    legacy = b'{ pkgs, ... }: pkgs.runCommand "legacy-host-check" {} "mkdir $out"\n'
    check.write_bytes(legacy)
    _git(repository, "add", "--", relative)
    _run(repository, "converge", "--dry-run", code=1)
    if check.read_bytes() != legacy:
        message = "dry-run changed the generated check"
        raise AssertionError(message)
    _run(repository, "converge")
    if check.read_bytes() != current:
        message = "convergence did not upgrade the generated check"
        raise AssertionError(message)
    if host.read_bytes() != original_host:
        message = "check upgrade changed the host configuration"
        raise AssertionError(message)
    if _git(repository, "ls-files").splitlines() != original_paths:
        message = "check upgrade added consumer files"
        raise AssertionError(message)
    if _run(repository, "converge", "--dry-run").stdout:
        message = "check upgrade did not converge"
        raise AssertionError(message)


def _check_remote_initialization(  # noqa: C901
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


def _check_flake_initialization(
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
    monkeypatch.setenv("CANONICAL_NIX", str(fake_nix))
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


def _check_home_commits(
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


def _check_home_whitelist(home_repository: Path) -> None:
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


def _check_home_settings(
    home_repository: Path,
) -> None:
    """Allow Git settings beyond the path and URL managed by Canonical."""
    root = home_repository
    modules = root / ".gitmodules"
    source = modules.read_text() + (
        "branch = main\nignore = dirty\nshallow = true\nupdate = checkout\n"
    )
    modules.write_text(source)
    _git(root, "add", ".gitmodules")
    index = _git(root, "ls-files", "--stage")
    _run(root, "converge", "--dry-run")
    _run(root, "converge")
    if modules.read_text() != source or _git(root, "ls-files", "--stage") != index:
        message = "convergence changed optional submodule settings"
        raise AssertionError(message)


def _check_home_move(
    home_repository: Path,
) -> None:
    """Move dirty submodules safely and reject collisions before mutation."""
    root = home_repository
    relative = "forge.example/owner/demo"
    checkout = root / relative
    source = checkout / "README"
    first = _git(checkout, "rev-parse", "HEAD").strip()
    source.write_text("move snapshot", encoding="utf-8")
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


def _check_overview_details(
    repository: Path,
) -> None:
    """Expose structured declarations independently of terminal labels and layout."""
    package = repository / "packages/example"
    package.mkdir(parents=True)
    help_text = (
        "First paragraph.\n"
        "\n"
        "Arguments:\n"
        "  Documentation text.\n"
        "Description: Still documentation."
    )
    (package / "default.nix").write_text('{ meta.description = "Example"; }\n')
    (package / "main.py").write_text(
        f'"""{help_text}"""\nimport argparse\n'
        'raise RuntimeError("must not execute")\n'
        "p = argparse.ArgumentParser()\ncommands = p.add_subparsers()\n"
        'command = commands.add_parser("build")\n'
        'command.add_argument("--jobs", default=2)\n',
    )
    (package / "test_main.py").write_text(
        'raise RuntimeError("must not execute")\ndef test_result(): pass\n',
    )
    asset = package / "prm/nested/script.js"
    asset.parent.mkdir(parents=True)
    asset.write_text("/* eslint-disable no-alert */\n\n")
    (asset.parent / "picture.png").write_bytes(b"binary\n")
    (asset.parent / "linked.py").symlink_to(package / "main.py")
    (package / "tmp").mkdir()
    (package / "tmp/generated.py").write_text("runtime\n")
    data = _overview(repository)
    record = next(node for node in data["nodes"] if node["kind"] == "package")
    details = record["details"]
    if details["help"] != help_text or details["tests"] != ["test result"]:
        msg = "Documentation and test sentences must be preserved as separate facts"
        raise AssertionError(msg)
    if details["cli"] != [
        {"path": ["build"], "text": "command", "command": True},
        {"path": ["build"], "text": "--jobs  optional; default=2", "command": False},
    ]:
        msg = "Structured CLI entries must retain their command ownership"
        raise AssertionError(msg)
    sources = {source["path"]: source for source in details["sources"]}
    if set(sources) != {
        "default.nix",
        "main.py",
        "test_main.py",
        "prm/nested/script.js",
    }:
        msg = "Source inventories must exclude binary assets, links, and runtime output"
        raise AssertionError(msg)
    expected_lines = 2
    if sources["prm/nested/script.js"]["lines"] != expected_lines or sources[
        "prm/nested/script.js"
    ]["suppressions"] != [
        {"kind": "eslint-disable", "scope": "global", "count": 1},
    ]:
        msg = "Asset line counts and suppressions must be structured source facts"
        raise AssertionError(msg)
    expected = (
        f"Name: example\nDescription: Example\nHelp: {help_text}\n"
        "Arguments:\n  build: command\n  build: --jobs  optional; default=2\n"
        "Dependencies:\n  (none)\nTests:\n  test result\n"
        "Suppressions:\n  prm/nested/script.js: eslint-disable (global): 1"
    )
    _expect(record["overview"] == expected, record)
    _expect(
        _run(repository, "overview", "packages/example").stdout == expected + "\n",
        expected,
    )


def _check_overview_history(
    repository: Path,
) -> None:
    """Read historical regular blobs with the same inventory as current sources."""
    subject = import_module("packages.canonical.main")
    for relative, content in (
        ("packages/tool/default.nix", "{}\n"),
        ("packages/tool/main.py", '"""Original help."""\n'),
        ("packages/tool/prm/nested/script.js", "/* eslint-disable */\n"),
        (
            "hosts/laptop/configuration.nix",
            (
                "{ inputs, system, ... }: { environment.systemPackages = [ "
                "inputs.self.packages.${system}.tool ]; }\n"
            ),
        ),
        ("checks/laptopVmWithDisko/default.nix", "{}\n"),
    ):
        path = repository / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    (repository / "packages/tool/prm/linked.py").symlink_to("../main.py")
    _git(repository, "add", ".")
    _git(
        repository,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-qm",
        "baseline",
    )
    baseline = subject.overview_data(repository)
    historical = subject.overview_data(repository, revision="HEAD")
    for before, after in zip(baseline["nodes"], historical["nodes"], strict=True):
        if before.get("details") != after.get("details"):
            msg = "Historical and current inventories must agree for identical sources"
            raise AssertionError(msg)
    (repository / "packages/tool/main.py").write_text('"""Changed help."""\n')
    (repository / "packages/tool/prm/nested/script.js").unlink()
    (repository / "hosts/laptop/configuration.nix").unlink()
    (repository / "checks/laptopVmWithDisko/default.nix").unlink()
    status = _git(repository, "status", "--porcelain=v1", "--untracked-files=all")
    head = _git(repository, "rev-parse", "HEAD")
    if subject.overview_data(repository, revision="HEAD") != historical:
        msg = "Historical snapshots must retain removed resources and source assets"
        raise AssertionError(msg)
    current = subject.overview_data(repository)
    if any(node["kind"] in {"host", "check"} for node in current["nodes"]):
        msg = "Current snapshots must reflect removed resource sources"
        raise AssertionError(msg)
    with pytest.raises(subject.CommandError, match="could not read revision"):
        subject.overview_data(repository, revision="does-not-exist")
    with pytest.raises(subject.CommandError, match="could not read revision"):
        subject.overview_data(repository, revision="--help")
    if (
        _git(repository, "status", "--porcelain=v1", "--untracked-files=all") != status
        or _git(repository, "rev-parse", "HEAD") != head
    ):
        msg = "Source inspection must preserve the index, working tree, and refs"
        raise AssertionError(msg)
    output = _run(repository, "overview", "--json", "--revision", "HEAD")
    if json.loads(output.stdout) != historical:
        msg = "Python and CLI revision snapshots must share their public contract"
        raise AssertionError(msg)
    _run(repository, "overview", "--revision", "HEAD", code=2)
    (repository / "packages/tool/default.nix").unlink()
    focused = subject.overview_data(repository / "packages/tool", revision="HEAD")
    if focused["focus"] != ".:packages/tool" or focused["nodes"] != historical["nodes"]:
        msg = "Historical focus must work when current package markers are removed"
        raise AssertionError(msg)


def _check_missing_history(
    tmp_path: Path,
) -> None:
    """Recognize initialized home policy and distinguish absent HEAD from errors."""
    subject = import_module("packages.canonical.main")
    (tmp_path / ".gitignore").write_text("/*\n!/.gitignore\n!/.gitmodules\n")
    child = tmp_path / "forge.example"
    child.mkdir()
    if (
        subject.canonical_root(child) != tmp_path
        or subject.overview_data(tmp_path)["profile"] != "home"
    ):
        msg = "Root discovery must recognize home policy before any submodules exist"
        raise AssertionError(msg)
    (tmp_path / ".gitignore").unlink()
    (tmp_path / "flake.nix").write_text("{}\n")
    historical = subject.overview_data(tmp_path, revision="HEAD")
    if historical["nodes"] != [
        {
            "id": ".:repository",
            "kind": "repository",
            "name": ".",
            "repository": ".",
            "path": ".",
            "profile": "flake",
            "available": False,
            "revision_available": False,
        },
    ]:
        msg = "Missing history must remain explicit without inventing resources"
        raise AssertionError(msg)


def _check_home_graph(
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


def _check_host_graph(
    home_repository: Path,
) -> None:
    """Link host package usage without confusing repositories or external inputs."""
    scope = "forge.example/owner/demo"
    root = home_repository / scope
    root.mkdir(parents=True, exist_ok=True)
    (root / "flake.nix").write_text("", encoding="utf-8")
    package = root / "packages/same"
    package.mkdir(parents=True)
    (package / "default.nix").write_text("{}", encoding="utf-8")
    host = root / "hosts/same"
    host.mkdir(parents=True)
    (host / "configuration.nix").write_text(
        "{ inputs, pkgs, ... }: let local = inputs.self.packages.${pkgs.system}; in {\n"
        "  environment.systemPackages = [ local.same pkgs.git local.missing ];\n"
        "  service.package = local.same;\n"
        "  module = ../../packages/same;\n"
        "  external = inputs.other.packages.${pkgs.system}.same;\n"
        "}\n",
        encoding="utf-8",
    )
    data = json.loads(_run(home_repository, "overview", "--json").stdout)
    nodes = {node["id"]: node for node in data["nodes"]}
    host_id = f"{scope}:hosts/same"
    edges = [
        edge
        for edge in data["edges"]
        if edge["target"] == host_id and edge["kind"] != "contains"
    ]
    if {(edge["source"], edge["kind"]) for edge in edges} != {
        (f"{scope}:packages/same", "runtime"),
        (f"{scope}:packages/same", "source"),
        (f"{scope}:packages/missing", "runtime"),
    }:
        raise AssertionError(edges)
    if nodes[f"{scope}:packages/missing"]["kind"] != "package-reference":
        raise AssertionError(nodes)
    if not nodes[host_id]["dependencies"]:
        raise AssertionError(nodes[host_id])
    if any(
        edge["declaration"]["path"] != "hosts/same/configuration.nix"
        or edge["declaration"]["line"] not in {2, 4}
        for edge in edges
    ):
        raise AssertionError(edges)


def _check_merge_diff(
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


def _check_definition_order(
    tmp_path: Path,
) -> None:
    """Append methods and async tests in declaration order in patches."""
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


def _check_command_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep client command menus synchronized when CLI commands are added."""
    subject = import_module("packages.canonical.main")
    cli = argparse.ArgumentParser(prog="canonical")
    commands = cli.add_subparsers()
    added = commands.add_parser("future")
    added.add_argument("--visible", help="Public option")
    added.add_argument("--internal", help=argparse.SUPPRESS)
    children = added.add_subparsers()
    children.add_parser("nested").add_argument("target")
    monkeypatch.setattr(subject, "parser", lambda: cli)
    catalog = subject.command_catalog()
    if [entry["command"] for entry in catalog] != ["future", "future nested"]:
        msg = "Catalog must include new commands and their nested commands"
        raise AssertionError(msg)
    if "--visible" not in catalog[0]["help"] or "--internal" in catalog[0]["help"]:
        msg = "Catalog must publish public parser help without hidden options"
        raise AssertionError(msg)


def _names_repository(tmp_path: Path) -> Path:
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


CLI_CONTRACTS = (
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
    (
        (
            "import typer\n"
            "raise RuntimeError('must not execute')\n"
            "app = typer.Typer()\n"
            "@app.command()\n"
            "def greet(name: str, suffix: str = '!', *, language: str, "
            "formal: bool = False, note: str | None = None): pass\n"
        ),
        (
            "greet: command\n"
            "greet: name  required; type=str\n"
            "greet: --suffix  default='!'; type=str\n"
            "greet: language  required; type=str\n"
            "greet: --formal  default=False; type=bool\n"
            "greet: --note  default=None; type=str | None\n"
        ),
    ),
    (
        (
            "import fire\n"
            "raise RuntimeError('must not execute')\n"
            "def greet(name, /, suffix='!', *, language, count=2, "
            "note=None): pass\n"
            "fire.Fire(greet)\n"
        ),
        (
            "greet: command\n"
            "greet: name  default=required\n"
            "greet: suffix  default='!'\n"
            "greet: language  default=required\n"
            "greet: count  default=2\n"
            "greet: note  default=None\n"
        ),
    ),
    (
        (
            "import fire\n"
            "raise RuntimeError('must not execute')\n"
            "class Tools:\n"
            " def greet(self, name, /, suffix='!', *, language, count=2, "
            "note=None): pass\n"
            " def _hidden(self): pass\n"
            "fire.Fire(Tools)\n"
        ),
        (
            "greet: command\n"
            "greet: name  default=required\n"
            "greet: suffix  default='!'\n"
            "greet: language  default=required\n"
            "greet: count  default=2\n"
            "greet: note  default=None\n"
        ),
    ),
    ("import argparse\ndef parser():\n    return argparse.ArgumentParser()\n", ""),
    ("VALUE = 1\n", "(not applicable)\n"),
    (
        (
            "import argparse\n"
            "def helper():\n"
            " p = argparse.ArgumentParser()\n"
            ' p.add_argument("--internal")\n'
            "def parser():\n"
            " p = argparse.ArgumentParser()\n"
            ' p.add_argument("--public")\n'
            " return p\n"
        ),
        "--public  optional\n",
    ),
    (
        (
            "import argparse\n"
            "p = argparse.ArgumentParser()\n"
            'p.add_argument("-o", "--output", default="out")\n'
            "commands = p.add_subparsers()\n"
            'run = commands.add_parser("run")\n'
            'run.add_argument("mode", choices=["fast", "slow"])\n'
        ),
        (
            "-o, --output  optional; default='out'\n"
            "run: command\n"
            "run: mode  required; choices=['fast', 'slow']\n"
        ),
    ),
    (
        (
            "import argparse as cli\n"
            "p = cli.ArgumentParser()\n"
            'p.add_argument("-o", "--output", default="out")\n'
            "commands = p.add_subparsers()\n"
            'run = commands.add_parser("run")\n'
            'run.add_argument("mode", choices=["fast", "slow"])\n'
        ),
        (
            "-o, --output  optional; default='out'\n"
            "run: command\n"
            "run: mode  required; choices=['fast', 'slow']\n"
        ),
    ),
    (
        (
            "from argparse import ArgumentParser as Parser\n"
            "p = Parser()\n"
            'p.add_argument("-o", "--output", default="out")\n'
            "commands = p.add_subparsers()\n"
            'run = commands.add_parser("run")\n'
            'run.add_argument("mode", choices=["fast", "slow"])\n'
        ),
        (
            "-o, --output  optional; default='out'\n"
            "run: command\n"
            "run: mode  required; choices=['fast', 'slow']\n"
        ),
    ),
)
NESTED_CONTRACTS = (
    (
        (
            "import argparse\n"
            "p = argparse.ArgumentParser()\n"
            "p.add_argument('--verbose')\n"
            "commands = p.add_subparsers()\n"
            "test = commands.add_parser('test')\n"
            "children = test.add_subparsers()\n"
            "coverage = children.add_parser('coverage')\n"
            "coverage.add_argument('--jobs', default=2, type=int)\n"
        ),
        ("test", "coverage"),
        "--jobs  optional; default=2; type=int",
    ),
    (
        (
            "import click\n"
            "@click.group()\n"
            "def cli(): pass\n"
            "@cli.group(name='test')\n"
            "def tests(): pass\n"
            "@tests.command('coverage')\n"
            "@click.option('--jobs')\n"
            "def coverage(jobs): pass\n"
        ),
        ("cli", "test", "coverage"),
        "--jobs",
    ),
    (
        (
            "import typer\n"
            "app = typer.Typer()\n"
            "tests = typer.Typer()\n"
            "app.add_typer(tests, name='test')\n"
            "@tests.callback()\n"
            "def settings(verbose: bool = False): pass\n"
            "@tests.command(name='coverage')\n"
            "def coverage(jobs: int = 2): pass\n"
        ),
        ("test", "coverage"),
        "--jobs  default=2; type=int",
    ),
    (
        (
            "import fire\n"
            "class Tools:\n"
            " def coverage(self, jobs=2): pass\n"
            "fire.Fire(Tools)\n"
        ),
        ("coverage",),
        "jobs  default=2",
    ),
)
UNSUPPORTED_INTERFACES = (
    "def main():\n    pass\n",
    "async def main():\n    pass\n",
    "from example import main\n",
    "from example import cli as main\n",
    "main = lambda: None\n",
    "main: object = lambda: None\n",
    "import sys\nprint(sys.argv)\n",
    "import argparse\np = argparse.ArgumentParser()\np.add_argument(dynamic)\n",
    (
        "import argparse\n"
        "p = argparse.ArgumentParser()\n"
        "for name in names:\n"
        " p.add_argument(name)\n"
    ),
)


def _expect(condition: bool, detail: object) -> None:  # noqa: FBT001
    """Report an invariant failure with its observed state."""
    if not condition:
        raise AssertionError(detail)


def _repository(root: Path, *, object_format: str = "sha1") -> Path:
    """Index a minimal flake in an existing temporary directory."""
    _git(root, "init", "--quiet", f"--object-format={object_format}")
    for name in (".gitignore", "flake.nix", "flake.lock", "README"):
        (root / name).write_text("", encoding="utf-8")
    _git(root, "add", ".")
    return root


@contextmanager
def _fresh_repository(*, object_format: str = "sha1") -> Iterator[Path]:
    """Give every generated example its own repository and cleanup."""
    with TemporaryDirectory(prefix="canonical-contract-") as directory:
        yield _repository(Path(directory), object_format=object_format)


def _snapshot(root: Path, *, exclude: tuple[str, ...] = ()) -> tuple[object, ...]:
    """Observe paths, contents, modes, staged blobs, refs, and local Git settings."""
    files = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if relative.parts[0] in (".git", *exclude):
            continue
        mode = path.lstat().st_mode
        value = (
            str(path.readlink()).encode()
            if stat.S_ISLNK(mode)
            else path.read_bytes()
            if stat.S_ISREG(mode)
            else b""
        )
        files[relative.as_posix()] = (stat.S_IFMT(mode), stat.S_IMODE(mode), value)
    return (
        files,
        _git(root, "ls-files", "--stage", "-z"),
        _git(root, "for-each-ref", "--format=%(refname) %(objectname)"),
        _git(root, "config", "--local", "--list"),
    )


def _preview(root: Path, *arguments: str, code: int = 0) -> None:
    """Preserve the whole repository during successful and rejected previews."""
    before = _snapshot(root)
    _run(root, *arguments, "--dry-run", code=code)
    _expect(_snapshot(root) == before, arguments)


def _overview(root: Path) -> dict[str, Any]:
    """Read the public structured source contract."""
    return cast("dict[str, Any]", json.loads(_run(root, "overview", "--json").stdout))


PACKAGE_NAMES = st.from_regex(
    r"[a-z][a-z0-9]{0,5}(?:[-_][a-z0-9]{1,5})?",
    fullmatch=True,
)
DESCRIPTIONS = st.text(
    alphabet=st.characters(exclude_categories=["Cs"], exclude_characters="\x00"),
    min_size=1,
    max_size=30,
)
TEST_LABELS = st.lists(
    st.from_regex(r"[a-z][a-z0-9_]{0,8}", fullmatch=True),
    max_size=5,
    unique=True,
)


@settings(deadline=None)
@given(
    kind=st.sampled_from(("python", "html", "latex", "nix", "host", "untracked")),
    name=PACKAGE_NAMES,
    payload=st.binary(max_size=40),
)
@example(kind="python", name="dash-case", payload=b"\x00\xff")
@example(kind="html", name="snake_case", payload=b"HTML")
@example(kind="latex", name="document", payload=b"LaTeX")
@example(kind="nix", name="value", payload=b"Nix")
@example(kind="host", name="laptop", payload=b"host")
@example(kind="untracked", name="untracked", payload=b"untracked")
def test_resource_lifecycle_preserves_files_checks_and_git_state(  # noqa: PLR0915
    kind: str,
    name: str,
    payload: bytes,
) -> None:
    """Add, preview, rename, converge, and remove resources without losing Git state."""
    with _fresh_repository() as root:
        if kind == "host":
            name = name.replace("-", "").replace("_", "")
        collection = "hosts" if kind == "host" else "packages"
        original, destination = f"{collection}/a{name}", f"{collection}/b{name}"
        package = root / original
        if kind == "untracked":
            package.mkdir(parents=True)
            (package / "default.nix").write_text("{}\n")
        else:
            _run(root, "add", original, *(() if kind == "host" else (kind,)))
        check = f"checks/a{name}" + ("VmWithDisko" if kind == "host" else "")
        if kind == "python":
            _expect(
                not (package / "test_main.py").exists() and not (root / check).exists(),
                "new packages must be untested",
            )
            (package / "test_main.py").write_text(
                (
                    "from unittest import TestCase as Case\n"
                    "class Reports(Case):\n"
                    "    def test_result(self): pass\n"
                ),
            )
        resource = package / "prm/nested/asset.bin"
        resource.parent.mkdir(parents=True)
        resource.write_bytes(payload)
        if kind != "untracked":
            _run(root, "converge")
            _expect(
                f"{original}/prm/nested/asset.bin"
                in _git(root, "ls-files").splitlines(),
                "resources must be indexed",
            )
        expected_check = kind in {"host", "python"}
        _expect((root / check / "default.nix").is_file() == expected_check, check)
        tracked = _git(root, "ls-files", "--", original, check).splitlines()
        staged, working = {}, {}
        for relative in tracked:
            path = root / relative
            staged[relative] = path.read_bytes() + b"\n"
            working[relative] = staged[relative] + b"\n"
            path.write_bytes(staged[relative])
            _git(root, "add", "--", relative)
            path.write_bytes(working[relative])
        untracked = package / "prm/untracked.bin"
        untracked.write_bytes(payload)
        _preview(root, "mv", original, destination)
        _run(root, "mv", original, destination)
        new_check = check.replace(f"a{name}", f"b{name}")
        for relative in tracked:
            renamed = relative.replace(original, destination).replace(check, new_check)
            _expect(
                subprocess.run(  # noqa: S603
                    ["git", "show", f":{renamed}"],  # noqa: S607
                    cwd=root,
                    capture_output=True,
                    check=True,
                    timeout=10,
                ).stdout
                == staged[relative]
                and (root / renamed).read_bytes() == working[relative],
                renamed,
            )
        _expect(
            (root / destination / "prm/untracked.bin").read_bytes() == payload,
            "untracked contents",
        )
        _expect(
            not _git(root, "ls-files", "--", f"{destination}/prm/untracked.bin"),
            "rename must not stage untracked assets",
        )
        _expect(
            not package.exists() and not (root / check).exists(),
            "rename left old paths",
        )
        _expect(
            (root / new_check / "default.nix").is_file() == expected_check,
            new_check,
        )
        if kind == "untracked":
            _expect(
                not _git(root, "ls-files", "--", destination),
                "rename staged an untracked package",
            )
            return
        _run(root, "converge")
        _preview(root, "rm", destination)
        _run(root, "rm", destination)
        _expect(
            not (root / destination).exists() and not (root / new_check).exists(),
            "remove left resource or check",
        )
        _expect(
            not _git(root, "ls-files", "--", original, destination, check, new_check),
            "remove left indexed paths",
        )
        _run(root, "converge")
        _preview(root, "converge")


@settings(deadline=None)
@given(payload=st.binary(max_size=50), tracked_scratch=st.booleans())
@example(payload=b"\x00\xff", tracked_scratch=True)
@example(payload=b"scratch", tracked_scratch=False)
def test_convergence_preserves_sources_repairs_checks_and_reaches_a_fixed_point(
    payload: bytes,
    *,
    tracked_scratch: bool,
) -> None:
    """Repair generated state, retain resources and scratch, and respect formatting."""
    with _fresh_repository() as root:
        _run(root, "add", "packages/example", "python")
        source = root / "packages/example/main.py"
        original = source.read_bytes()
        for relative in (
            "tmp/root-state",
            "packages/example/tmp/package-state",
            "packages/example/prm/nested/asset.bin",
        ):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            if tracked_scratch:
                _git(root, "add", "--force", "--", relative)
        (root / "discarded").write_bytes(payload)
        _preview(root, "converge", code=1)
        _run(root, "converge")
        _expect(
            not (root / "discarded").exists() and source.read_bytes() == original,
            "cleanup or source preservation",
        )
        indexed = _git(root, "ls-files").splitlines()
        _expect(
            "packages/example/prm/nested/asset.bin" in indexed
            and not any("tmp/" in path for path in indexed),
            indexed,
        )
        for relative in (
            "tmp/root-state",
            "packages/example/tmp/package-state",
            "packages/example/prm/nested/asset.bin",
        ):
            _expect((root / relative).read_bytes() == payload, relative)
        stable = _snapshot(root)
        _run(root, "converge")
        _expect(_snapshot(root) == stable, "convergence is not idempotent")
        _preview(root, "converge")
        _check_formatted_host(root)
        with _fresh_repository() as upgrade:
            _check_host_upgrade(upgrade)
        with (
            _fresh_repository() as validation,
            pytest.MonkeyPatch.context() as patch,
        ):
            _check_excluded_trees(validation, patch)


@settings(deadline=None)
@given(
    arguments=st.sampled_from(
        (
            ("mv", "packages/example", "hosts/example"),
            ("add", "packages/bad--name", "python"),
            ("add", "hosts/bad-name"),
            ("rm", "../outside"),
            ("rm", "/outside"),
            ("mv", "packages/example", "packages/taken"),
            ("add", "packages/example", "python"),
        ),
    ),
    misplaced=st.sampled_from(
        (
            "def test_misplaced(): pass\n",
            (
                "from unittest import TestCase as Case\n"
                "class Reports(Case):\n"
                "    def test_result(self): pass\n"
            ),
        ),
    ),
)
@example(
    arguments=("mv", "packages/example", "hosts/example"),
    misplaced="def test_misplaced(): pass\n",
)
@example(
    arguments=("rm", "../outside"),
    misplaced=(
        "from unittest import TestCase as Case\n"
        "class Reports(Case):\n"
        "    def test_result(self): pass\n"
    ),
)
@example(arguments=("rm", "/outside"), misplaced="def test_misplaced(): pass\n")
@example(
    arguments=("add", "packages/bad--name", "python"),
    misplaced="def test_misplaced(): pass\n",
)
@example(arguments=("add", "hosts/bad-name"), misplaced="def test_misplaced(): pass\n")
@example(
    arguments=("mv", "packages/example", "packages/taken"),
    misplaced="def test_misplaced(): pass\n",
)
@example(
    arguments=("add", "packages/example", "python"),
    misplaced="def test_misplaced(): pass\n",
)
def test_rejected_operations_preserve_contents_modes_index_and_refs(
    arguments: tuple[str, ...],
    misplaced: str,
) -> None:
    """Reject invalid paths, collisions, and embedded tests before modifying work."""
    with _fresh_repository() as root:
        _run(root, "add", "packages/example", "python")
        _run(root, "add", "packages/taken", "nix")
        (root / "work-in-progress").write_text("preserve unrelated work")
        before = _snapshot(root)
        rejected = _run(root, *arguments, code=1)
        _expect(bool(rejected.stderr) and _snapshot(root) == before, rejected)
        source = root / "packages/example/main.py"
        source.write_text(misplaced)
        before = _snapshot(root)
        rejected = _run(root, "converge", code=1)
        _expect(
            "move test definitions to test_main.py" in rejected.stderr
            and _snapshot(root) == before,
            rejected,
        )


def _nix_environment(root: Path) -> dict[str, str]:
    """Keep offline Nix evaluation and builds inside this example's scratch tree."""
    environment = dict(os.environ)
    store = root / "tmp/nix"
    environment["NIX_REMOTE"] = (
        f"local?store={store / 'store'}&state={store / 'state'}&log={store / 'log'}"
    )
    environment["NIX_CONFIG"] = (
        "experimental-features = nix-command flakes\n"
        "build-users-group =\n"
        "sandbox = false\n"
        "eval-cache = false\n"
    )
    return environment


def _template_expression(root: Path, name: str) -> str:
    """Build generated install scripts with real Nix and offline tool stand-ins."""
    bash = shutil.which("bash")
    _expect(bash is not None, "template fixtures require bash")
    site_packages = (
        f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
    )
    builder_command = json.dumps('source "$scriptPath"')
    return (
        "let mk = name: script: builtins.derivation { inherit name; "
        "system = builtins.currentSystem; "
        f"builder = {json.dumps(bash)}; PATH = {json.dumps(os.environ['PATH'])}; "
        'inherit script; passAsFile = [ "script" ]; '
        f'args = [ "-e" "-c" {builder_command} ]; }}; '
        "python = { "
        f"interpreter = {json.dumps(sys.executable)}; "
        f"sitePackages = {json.dumps(site_packages)}; "
        f"withPackages = _: {json.dumps(sys.prefix)}; "
        "pkgs.buildPythonPackage = attrs: (mk attrs.pname "
        '("pname=${attrs.pname}\\ncp -R ${attrs.src}/. .\\n" '
        "+ attrs.installPhase)) // attrs; }; "
        'pkgs = { python3 = python; git = "git"; curl = "curl"; inner = "wrong-inner"; '
        'http-server = "server"; texliveFull = "tex"; '
        "stdenv.hostPlatform.isLinux = false; "
        "stdenv.mkDerivation = attrs: attrs; writeTextFile = attrs: attrs; "
        "lib.optionals = condition: values: if condition then values else []; "
        "runCommand = name: _: script: mk name script; "
        "writeShellApplication = attrs: (mk attrs.name "
        "("
        + json.dumps(
            'mkdir -p "$out/bin"\ncat > "$out/bin/${attrs.name}" '
            f"<<'CANONICAL_SCRIPT'\n#!{bash}\n",
        )
        + " + attrs.text + "
        + json.dumps('\nCANONICAL_SCRIPT\nchmod 755 "$out/bin/${attrs.name}"\n')
        + ")) // attrs; }; "
        + f"package = import {root / 'packages' / name / 'default.nix'} "
        + "{ inherit pkgs; }; in "
    )


def _evaluated_template(
    root: Path,
    name: str,
    environment: dict[str, str],
) -> dict[str, Any]:
    """Use Nix's evaluated values as the independent metadata and dependency oracle."""
    expression = _template_expression(root, name) + (
        "{ meta = package.meta; name = package.pname or "
        "package.name; dependencies = package.runtimeInputs or "
        "package.nativeBuildInputs or []; }"
    )
    result = _run(
        root,
        "eval",
        "--impure",
        "--json",
        "--expr",
        expression,
        executable="nix",
        environment=environment,
    )
    return cast("dict[str, Any]", json.loads(result.stdout))


def _check_launcher_environment(
    root: Path,
    name: str,
    environment: dict[str, str],
    *,
    library: bool,
) -> None:
    """Evaluate generated checks with an absent or independently named executable."""
    metadata = (
        "package.meta" if library else 'package.meta // { mainProgram = "other-name"; }'
    )
    expression = _template_expression(root, name) + (
        "let checkPkgs = pkgs // { stdenv.system = builtins.currentSystem; "
        "runCommand = _: attrs: _: attrs; lib = pkgs.lib // { "
        "optionalAttrs = condition: attrs: if condition then attrs else {}; "
        'getExe = p: if p.meta ? mainProgram then "/package/bin/${p.meta.mainProgram}" '
        'else abort "library check requested an executable"; }; }; '
        f"packageDrv = package // {{ inherit python; meta = {metadata}; }}; "
        f"in import {root / 'checks' / name / 'default.nix'} {{ pkgs = checkPkgs; "
        "inputs.self.packages.${builtins.currentSystem}."
        f"{json.dumps(name)} = packageDrv; }}"
    )
    result = _run(
        root,
        "eval",
        "--impure",
        "--json",
        "--expr",
        expression,
        executable="nix",
        environment=environment,
    )
    attributes = json.loads(result.stdout)
    expected = None if library else "/package/bin/other-name"
    _expect(attributes.get("PACKAGE_E2E_EXECUTABLE") == expected, attributes)


@settings(deadline=None)
@given(
    kind=st.sampled_from(("python", "html", "latex", "nix")),
    name=PACKAGE_NAMES,
    description=DESCRIPTIONS,
    library=st.booleans(),
)
@example(
    kind="python",
    name="dash-case",
    description='A "quoted" report.\n${literal}\\path',
    library=False,
)
@example(kind="python", name="snake_case", description="é\b\f\x01", library=True)
@example(
    kind="python",
    name="dash-library",
    description="Nested library metadata",
    library=True,
)
@example(kind="html", name="web-site", description="Δ report", library=False)
@example(kind="latex", name="document", description="LaTeX\tresources", library=False)
@example(kind="nix", name="my-package", description="é", library=False)
def test_generated_templates_evaluate_metadata_preserve_scopes_and_install_assets(  # noqa: C901, PLR0915
    kind: str,
    name: str,
    description: str,
    *,
    library: bool,
) -> None:
    """Evaluate generated Nix and run installed Python and HTML launchers."""
    with _fresh_repository() as root:
        _run(root, "add", f"packages/{name}", kind, description)
        package = root / "packages" / name
        asset = package / "prm/asset.bin"
        asset.parent.mkdir()
        asset.write_bytes(b"\x00\xff asset")
        definition = package / "default.nix"
        source = definition.read_text()
        if kind == "python":
            (package / "test_main.py").write_text("def test_result(): pass\n")
            if library:
                (package / "main.py").write_text("VALUE = 1\n")
            if library and "_" in name:
                source = (
                    source.replace("    mainProgram = pname;", "")
                    .replace("    mainProgram = baseNameOf ./.;", "")
                    .replace(
                        "  passthru.python",
                        "  meta.mainProgram = pname;\n  passthru.python",
                    )
                )
        elif kind in {"html", "latex"}:
            binding = "runtimeDeps" if kind == "html" else "nativeDeps"
            expression = (
                f"(let {binding} = [ pkgs.inner ]; in [ pkgs.git ]) ++ [ pkgs.curl ]"
            )
            source = source.replace(
                f"{binding} = [ ];",
                f"{binding} = {expression};",
            ).replace(
                "let\n",
                f"let\n  # {binding} = [ pkgs.fromComment ];\n"
                f"  ignored = let {binding} = [ pkgs.fromNested ]; in null;\n",
                1,
            )
            if kind == "html":
                resource = root / "prm/source.bin"
                resource.parent.mkdir()
                resource.write_bytes(b"copied resource")
                source = source.replace(
                    'prmInstall = "";',
                    (
                        "prmInstall = let prmInstall = \"inner\"; in ''cp "
                        "${../../prm/source.bin} \"$out/prm/copied.bin\"'';"
                    ),
                )
        definition.write_text(source)
        _run(root, "converge")
        _preview(root, "converge")
        environment = _nix_environment(root)
        actual = _evaluated_template(root, name, environment)
        _expect(actual["name"] == name.replace("-", "_"), actual)
        _expect(actual["meta"]["description"] == description, actual)
        graph = _overview(root)
        details = next(
            node["details"] for node in graph["nodes"] if node["kind"] == "package"
        )
        _expect(details["description"] == description, details)
        if kind == "latex":
            _expect(actual["dependencies"] == ["git", "curl", "tex"], actual)
        if kind not in {"python", "html"}:
            return
        expression = _template_expression(root, name) + "package"
        built = _run(
            root,
            "build",
            "--impure",
            "--no-link",
            "--print-out-paths",
            "--expr",
            expression,
            executable="nix",
            environment=environment,
        )
        output = Path(built.stdout.strip())
        if kind == "python":
            _check_launcher_environment(root, name, environment, library=library)
            module = name.replace("-", "_")
            version = f"{sys.version_info.major}.{sys.version_info.minor}"
            installed = output / f"lib/python{version}/site-packages"
            _expect(
                (installed / module / "prm/asset.bin").read_bytes()
                == asset.read_bytes(),
                "Python installation lost assets",
            )
            environment["PYTHONPATH"] = (
                str(installed) + os.pathsep + environment.get("PYTHONPATH", "")
            )
            if library:
                _expect(
                    "mainProgram" not in actual["meta"]
                    and not (output / "bin").exists(),
                    actual,
                )
                imported = _run(
                    root,
                    "-c",
                    f"import {module}; print({module}.VALUE)",
                    executable=sys.executable,
                    environment=environment,
                )
                _expect(imported.stdout == "1\n", imported)
                _expect(
                    _run(package, "args").stdout == "(not applicable)\n",
                    "library contract",
                )
                (package / "main.py").write_text(
                    (
                        "import argparse\n"
                        "def main():\n"
                        "    argparse.ArgumentParser().parse_args()\n"
                    ),
                )
                _run(root, "converge")
                restored = _evaluated_template(root, name, environment)
                _expect(restored["meta"]["mainProgram"] == name, restored)
            else:
                _expect(
                    actual["meta"]["mainProgram"] == name
                    and (output / "bin" / name).is_file(),
                    actual,
                )
                _expect(
                    "--help"
                    in _run(
                        root,
                        "--help",
                        executable=str(output / "bin" / name),
                        environment=environment,
                    ).stdout,
                    "installed help",
                )
                _run(
                    root,
                    "unexpected",
                    executable=str(output / "bin" / name),
                    environment=environment,
                    code=2,
                )
                _expect(
                    not _run(package, "args").stdout,
                    "scaffold must declare an empty CLI",
                )
        else:
            _expect(actual["dependencies"] == ["git", "curl", "server"], actual)
            tools = root / "tmp/tools"
            tools.mkdir(parents=True)
            server = tools / "http-server"
            server.write_text(
                f"#!{sys.executable}\n"
                "import json, sys\nprint(json.dumps(sys.argv[1:]))\n",
            )
            server.chmod(0o755)
            environment["PATH"] = str(tools) + os.pathsep + environment["PATH"]
            cases = (
                ("", "", (), ()),
                (":0", "", (), ("-o", "/")),
                ("", "wayland-0", (), ("-o", "/")),
                (":0", "wayland-0", ("--no-open",), ()),
                ("", "", ("--no-open",), ()),
                (":0", "", ("-o", "/prm/example.html"), ("-o", "/prm/example.html")),
                ("", "", ("-o", "/"), ("-o", "/")),
                (":0", "", ("--no-o",), ("--no-o",)),
                (
                    ":0",
                    "",
                    ("--no-open", "-p", "8761", "two words"),
                    ("-p", "8761", "two words"),
                ),
                (":0", "", ("--o=/prm/example.html",), ("--o=/prm/example.html",)),
            )
            for display, wayland, arguments, expected in cases:
                environment.update(DISPLAY=display, WAYLAND_DISPLAY=wayland)
                result = _run(
                    root,
                    *arguments,
                    executable=str(output / "bin" / name.replace("-", "_")),
                    environment=environment,
                )
                site, *options = json.loads(result.stdout)
                _expect(options == list(expected), result)
                _expect(
                    (Path(site) / "prm/asset.bin").read_bytes() == asset.read_bytes(),
                    site,
                )
                _expect(
                    (Path(site) / "prm/copied.bin").read_bytes() == b"copied resource",
                    site,
                )
                _expect(
                    (Path(site) / "script.js").is_file()
                    and (Path(site) / "style.css").is_file(),
                    site,
                )


def test_home_lifecycle_repairs_policy_and_preserves_dirty_submodules() -> None:
    """Initialize remotes and preserve checkout/index state through home convergence."""
    for remote in (
        "https://example.test/team/project.git",
        "ssh://git@example.test/team/project.git",
        "git@example.test:team/project.git",
    ):
        with (
            TemporaryDirectory(prefix="canonical-home-") as directory,
            pytest.MonkeyPatch.context() as patch,
        ):
            _check_remote_initialization(Path(directory), patch, remote)
    with (
        TemporaryDirectory(prefix="canonical-bootstrap-") as directory,
        pytest.MonkeyPatch.context() as patch,
    ):
        _check_flake_initialization(Path(directory), patch)
    with TemporaryDirectory(prefix="canonical-home-") as directory:
        root = _home_repository(Path(directory))
        _check_home_commits(root)
        _check_home_whitelist(root)
        _check_home_settings(root)
        _check_home_move(root)
        modules = root / ".gitmodules"
        modules.write_text(
            modules.read_text() + "url = git@forge.example:owner/other\n",
        )
        before = _snapshot(root)
        result = _run(root, "converge", code=1)
        _expect(
            "duplicate url field" in result.stderr and _snapshot(root) == before,
            result,
        )


def _check_discovery_boundaries() -> None:
    """Validate nested declarations and malformed inputs once per suite."""
    with _fresh_repository() as root:
        sentinel = (
            "from pathlib import Path\nPath('SENTINEL').touch()\n"
            "raise RuntimeError('must not execute')\n"
        )
        package = _make_test_names_package(
            root,
            "example",
            sentinel + "def test_result(): pass\n",
        )
        for nested, path, parameter in NESTED_CONTRACTS:
            (package / "main.py").write_text(sentinel + nested)
            before = _snapshot(root)
            data = next(
                node["details"]
                for node in _overview(root)["nodes"]
                if node["kind"] == "package"
            )
            _expect(
                _snapshot(root) == before,
                "nested discovery executed code or changed state",
            )
            _expect(
                {"path": list(path), "text": "command", "command": True} in data["cli"],
                data,
            )
            _expect(
                {"path": list(path), "text": parameter, "command": False}
                in data["cli"],
                data,
            )
            if nested.startswith("import typer"):
                _expect(
                    {
                        "path": ["test"],
                        "text": "--verbose  default=False; type=bool",
                        "command": False,
                    }
                    in data["cli"],
                    data,
                )
                _expect(
                    not any(
                        entry["path"][-1:] == ["settings"] for entry in data["cli"]
                    ),
                    data,
                )
        for unsupported in UNSUPPORTED_INTERFACES:
            (package / "main.py").write_text(unsupported)
            before = _snapshot(root)
            rejected = _run(package, "args", code=1)
            _expect(
                "unsupported CLI interface" in rejected.stderr and not rejected.stdout,
                rejected,
            )
            _expect(_snapshot(root) == before, "unsupported interface changed state")
        for layout in ("missing", "syntax", "encoding", "linked"):
            test_file = package / "test_main.py"
            test_file.unlink(missing_ok=True)
            if layout == "syntax":
                test_file.write_text("def invalid(")
            elif layout == "encoding":
                test_file.write_bytes(b"\xff")
            elif layout == "linked":
                test_file.symlink_to(package / "main.py")
            before = _snapshot(root)
            rejected = _run(package, "test", "names", code=1)
            _expect(
                "canonical test names:" in rejected.stderr and not rejected.stdout,
                rejected,
            )
            _expect(_snapshot(root) == before, "malformed test source changed state")


@settings(deadline=None)
@given(
    contract=st.sampled_from(CLI_CONTRACTS),
    labels=TEST_LABELS,
    form=st.sampled_from(("functions", "classes", "unittest")),
    explicit=st.booleans(),
)
@example(
    contract=CLI_CONTRACTS[0],
    labels=["double__underscore", "async_behavior"],
    form="functions",
    explicit=True,
)
@example(contract=CLI_CONTRACTS[1], labels=["result"], form="classes", explicit=False)
@example(
    contract=CLI_CONTRACTS[2],
    labels=["derived", "async"],
    form="unittest",
    explicit=True,
)
@example(contract=CLI_CONTRACTS[3], labels=["complex"], form="functions", explicit=True)
@example(contract=CLI_CONTRACTS[4], labels=["complex"], form="unittest", explicit=False)
@example(contract=CLI_CONTRACTS[5], labels=["complex"], form="classes", explicit=True)
@example(contract=CLI_CONTRACTS[7], labels=[], form="functions", explicit=True)
@example(contract=CLI_CONTRACTS[6], labels=[], form="functions", explicit=False)
@example(contract=CLI_CONTRACTS[8], labels=["public"], form="functions", explicit=False)
@example(contract=CLI_CONTRACTS[9], labels=["aliased"], form="classes", explicit=True)
@example(
    contract=CLI_CONTRACTS[10],
    labels=["aliased"],
    form="functions",
    explicit=False,
)
@example(contract=CLI_CONTRACTS[11], labels=["aliased"], form="unittest", explicit=True)
def test_static_interfaces_and_test_sentences_match_declarations_without_execution(
    contract: tuple[str, str],
    labels: list[str],
    form: str,
    *,
    explicit: bool,
) -> None:
    """Compare static declarations to expectations and continue after errors."""
    with _fresh_repository() as root:
        sentinel = (
            "from pathlib import Path\n"
            "Path('SENTINEL').touch()\n"
            "raise RuntimeError('must not execute')\n"
        )
        tests = sentinel + (
            "def helper():\n"
            "    def test_hidden(): pass\n"
            "class Helper:\n"
            "    def test_hidden(self): pass\n"
        )
        if form == "functions":
            tests += "".join(
                f"@unknown_decorator()\nasync def test_{label}():\n    assert False\n"
                for label in labels
            )
        else:
            header = (
                "class TestBehavior:\n"
                if form == "classes"
                else (
                    "import unittest as unit\n"
                    "from unittest import TestCase as Case, "
                    "IsolatedAsyncioTestCase\n"
                    "class Base(Case): pass\n"
                    "class Reports(Base):\n"
                )
            )
            tests += header + (
                "    pass\n"
                if not labels
                else "".join(
                    f"    async def test_{label}(self):\n        assert False\n"
                    for label in labels
                )
            )
        package = _make_test_names_package(root, "my-package", tests)
        source, expected_args = contract
        (package / "main.py").write_text(
            '"""First paragraph.\n\nArguments:\n  Documentation text."""\n'
            + sentinel
            + source,
        )
        expected_names = "".join(
            "test " + label.replace("_", " ") + "\n" for label in sorted(labels)
        )
        cwd = root if explicit else package
        target = (str(package),) if explicit else ()
        before = _snapshot(root)
        _expect(_run(cwd, "args", *target).stdout == expected_args, contract)
        _expect(_run(cwd, "test", "names", *target).stdout == expected_names, tests)
        details = next(
            node["details"]
            for node in _overview(root)["nodes"]
            if node["kind"] == "package"
        )
        _expect(
            details["tests"] == ["test " + label.replace("_", " ") for label in labels],
            details,
        )
        _expect(
            details["help"] == "First paragraph.\n\nArguments:\n  Documentation text.",
            details,
        )
        _expect(
            _snapshot(root) == before
            and not (root / "SENTINEL").exists()
            and not (package / "SENTINEL").exists(),
            "inspection changed source or executed code",
        )
        (package / "test_main.py").write_text(
            tests.replace("assert False", "raise RuntimeError('changed body')"),
        )
        _expect(
            _run(package, "test", "names").stdout == expected_names,
            "body edits changed sentences",
        )
        (package / "test_main.py").unlink(missing_ok=True)
        (package / "test_main.py").write_text(tests)
        broken = _make_test_names_package(root, "alpha", "def invalid(")
        (broken / "main.py").write_text("import sys\nprint(sys.argv)\n")
        (package / "main.py").write_text(source)
        missing = _make_test_names_package(root, "untested", "")
        (missing / "test_main.py").unlink()
        (root / "packages/linked").symlink_to(package, target_is_directory=True)
        listed = _run(root, "test", "names", code=1)
        _expect(
            listed.stdout == "packages/alpha:\npackages/my-package:\n" + expected_names,
            listed,
        )
        _expect(
            "canonical test names: alpha:" in listed.stderr
            and "Skipping untested: no test_main.py" in listed.stderr,
            listed,
        )
        args = _run(root, "args", code=1)
        _expect(
            "packages/my-package:\n" + expected_args in args.stdout
            and "alpha: unsupported CLI interface" in args.stderr,
            args,
        )


@settings(deadline=None)
@given(name=PACKAGE_NAMES, object_format=st.sampled_from(("sha1", "sha256")))
@example(name="core", object_format="sha1")
@example(name="dash-case", object_format="sha256")
def test_source_overviews_preserve_dependency_graphs_source_facts_and_history(  # noqa: PLR0915
    name: str,
    object_format: str,
) -> None:
    """Model declarations and compare current and historical source views."""
    with _fresh_repository(object_format=object_format) as root:
        provider = "p" + name
        _run(root, "add", f"packages/{provider}", "nix", "Provider")
        _run(root, "add", "packages/consumer", "python", "Consumer")
        _run(root, "add", "packages/computed", "nix", "Computed")
        consumer = root / "packages/consumer"
        (consumer / "default.nix").write_text(
            "{ inputs, pkgs, system, ... }: "
            "let local = inputs.self.packages.${system}; in {\n"
            f"  propagatedBuildInputs = [ local.{provider} local.missing pkgs.git ];\n"
            f'  installPhase = "cp ${{../{provider}/main.py}} result";\n'
            "  # propagatedBuildInputs = [ local.fake ];\n"
            '  text = "inputs.self.packages.system.also_fake";\n}\n',
        )
        (consumer / "main.py").write_text(
            (
                '"""Consumer help."""\n'
                "import argparse\n"
                'raise RuntimeError("must not run")\n'
                "p = argparse.ArgumentParser()\n"
                'p.add_argument("--output", help="Output path")\n'
                "literal = '# noqa: D103 and # type: ignore are text'\n"
                "# ruff: noqa: D\n"
                "value = 1  # noqa: E501\n"
                "other = 2  # type: ignore[assignment]\n"
            ),
        )
        (consumer / "test_main.py").write_text(
            "def test_result(): pass  # noqa: D103\n",
        )
        assets = consumer / "prm/assets"
        assets.mkdir(parents=True)
        snippets: tuple[tuple[str, str, list[dict[str, str | int]]], ...] = (
            (
                "directives.html",
                (
                    "<!-- html-validate-disable -->\n"
                    "<!-- html-validate-disable-next heading-level -->\n"
                    "<p>eslint-disable is text</p>\n"
                ),
                [
                    {"kind": "html-validate-disable", "scope": "global", "count": 1},
                    {"kind": "html-validate-disable", "scope": "local", "count": 1},
                ],
            ),
            (
                "index.html",
                '<script>const text = "<!-- htmlhint-disable -->";</script>',
                [],
            ),
            (
                "script.js",
                "const value = `${(() => { /* eslint-disable */ return 1; })()}`;",
                [{"kind": "eslint-disable", "scope": "global", "count": 1}],
            ),
            (
                "string.js",
                "const value = `/* eslint-disable */`; /* eslint-disable-next-line */",
                [{"kind": "eslint-disable", "scope": "local", "count": 1}],
            ),
            (
                "style.css",
                'p { content: "/* stylelint-disable */"; } /* stylelint-disable */',
                [{"kind": "stylelint-disable", "scope": "global", "count": 1}],
            ),
        )
        for filename, source, _ in snippets:
            (assets / filename).write_text(source)
        (assets / "picture.png").write_bytes(b"binary")
        (assets / "linked.py").symlink_to(consumer / "main.py")
        (consumer / "tmp").mkdir()
        (consumer / "tmp/generated.py").write_text("runtime")
        (root / "packages/computed/default.nix").write_text(
            (
                "{ pkgs, ... }: let one = two; two = one; in { buildInputs = "
                "one; nativeBuildInputs = if pkgs.stdenv.isLinux then [ "
                "pkgs.git ] else []; }"
            ),
        )
        graph = _overview(root)
        nodes = {node["id"]: node for node in graph["nodes"]}
        _expect(
            graph["schema"] == "canonical.overview"
            and graph["analysis"] == "source-declarations",
            graph,
        )
        _expect(
            len(nodes) == len(graph["nodes"])
            and nodes[".:packages/missing"]["kind"] == "package-reference",
            graph,
        )
        edges = {
            (edge["source"], edge["target"], edge["kind"])
            for edge in graph["edges"]
            if edge["target"] == ".:packages/consumer" and edge["kind"] != "contains"
        }
        _expect(
            edges
            == {
                (f".:packages/{provider}", ".:packages/consumer", "runtime"),
                (f".:packages/{provider}", ".:packages/consumer", "source"),
                (".:packages/missing", ".:packages/consumer", "runtime"),
            },
            edges,
        )
        _expect(not nodes[".:packages/computed"]["dependencies"], nodes)
        details = nodes[".:packages/consumer"]["details"]
        sources = {source["path"]: source for source in details["sources"]}
        _expect(
            set(sources)
            == {
                "default.nix",
                "main.py",
                "test_main.py",
                *("prm/assets/" + filename for filename, _, _ in snippets),
            },
            sources,
        )
        for filename, _, expected in snippets:
            _expect(
                sources["prm/assets/" + filename]["suppressions"] == expected,
                sources,
            )
        _expect(
            details["source_metrics"]["suppressions"]
            == {"noqa (global)": 1, "noqa (local)": 2, "type: ignore (local)": 1},
            details,
        )
        _expect(
            details["tests"] == ["test result"] and details["help"] == "Consumer help.",
            details,
        )
        focus = json.loads(_run(root, "overview", "packages/consumer", "--json").stdout)
        _expect(focus["focus"] == ".:packages/consumer", focus)
        terminal = _run(root, "overview", "--full").stdout
        _expect(
            "packages/consumer:\n" in terminal
            and "runtime: packages/" + provider in terminal
            and "  test result\n" in terminal,
            terminal,
        )
        _git(root, "add", "--force", ".")
        _test_names_git(root, "commit", "-qm", "Source snapshot")
        before = _snapshot(root)
        historical = json.loads(
            _run(root, "overview", "--json", "--revision", "HEAD").stdout,
        )
        _expect(
            [node.get("details") for node in historical["nodes"]]
            == [node.get("details") for node in graph["nodes"]],
            historical,
        )
        _expect(
            _snapshot(root) == before and _overview(root) == graph,
            "overview changed its repository",
        )
    with _fresh_repository() as root:
        _check_overview_details(root)
    with _fresh_repository() as root:
        _check_overview_history(root)
    with TemporaryDirectory(prefix="canonical-history-") as directory:
        _check_missing_history(Path(directory))
    with TemporaryDirectory(prefix="canonical-graph-") as directory:
        _check_home_graph(_home_repository(Path(directory)))
    with TemporaryDirectory(prefix="canonical-host-graph-") as directory:
        _check_host_graph(_home_repository(Path(directory)))


def _review_source(label: str, *, arguments: bool) -> str:
    """Describe one known interface or sentence independently of the converter."""
    if arguments:
        return (
            "import argparse\np = argparse.ArgumentParser()\n"
            f"p.add_argument('--{label}')\n"
        )
    return f"def test_{label}(): pass\n"


def _review_view(
    root: Path,
    arguments: bool,  # noqa: FBT001
    *options: str,
    code: int = 0,
) -> subprocess.CompletedProcess[str]:
    """Preserve repository state through successful and rejected Git views."""
    before = _snapshot(root)
    result = _run(
        root,
        *(("args",) if arguments else ("test", "names")),
        *options,
        code=code,
    )
    _expect(_snapshot(root) == before, options)
    _expect(
        "def test_" not in result.stdout
        and "add_argument" not in result.stdout
        and "PRIVATE_IMPLEMENTATION" not in result.stdout,
        result,
    )
    return result


@settings(deadline=None)
@given(suffix=st.text(alphabet="abcxyz", max_size=4))
@example(suffix="")
def test_git_views_preserve_native_history_filters_errors_and_source_state(  # noqa: C901, PLR0912, PLR0915
    suffix: str,
) -> None:
    """Compare known source versions through both public Git views."""
    with _fresh_repository() as root:
        package = _make_test_names_package(root, "example", "")
        files = {False: package / "test_main.py", True: package / "main.py"}
        previous, committed, staged, working = (
            prefix + suffix for prefix in ("previous", "committed", "staged", "working")
        )

        def write_version(label: str) -> None:
            for arguments, path in files.items():
                path.write_text(_review_source(label, arguments=arguments))

        def row(label: str, arguments: bool) -> str:  # noqa: FBT001
            return f"--{label}  optional" if arguments else "test " + label

        write_version(previous)
        _test_names_git(root, "add", ".")
        for arguments in files:
            result = _review_view(root, arguments, "diff", "--staged")
            _expect("+" + row(previous, arguments) + "\n" in result.stdout, result)
        _test_names_git(root, "commit", "-qm", "Initial declarations")
        initial = _git(root, "rev-parse", "HEAD").strip()
        write_version(committed)
        _test_names_git(root, "commit", "-qam", "Change declarations")
        write_version(staged)
        _test_names_git(root, "add", ".")
        write_version(working)
        cases = (
            (("diff",), staged, working),
            (("diff", "--staged"), committed, staged),
            (("diff", "--cached"), committed, staged),
            (("diff", "HEAD"), committed, working),
            (("diff", "HEAD~1", "HEAD"), previous, committed),
            (("show",), previous, committed),
            (("diff", "-R"), working, staged),
        )
        for arguments, path in files.items():
            for options, removed, added in cases:
                result = _review_view(root, arguments, *options)
                _expect(
                    "-" + row(removed, arguments) + "\n" in result.stdout
                    and "+" + row(added, arguments) + "\n" in result.stdout,
                    result,
                )
            changed = _review_view(root, arguments, "diff", "--exit-code", code=1)
            _expect("+" + row(working, arguments) in changed.stdout, changed)
            filename = "main.py" if arguments else "test_main.py"
            excluded = "test_main.py" if arguments else "main.py"
            scoped = _review_view(
                root,
                arguments,
                "diff",
                "HEAD",
                "--",
                "packages/example",
            )
            local = _review_view(package, arguments, "diff", "HEAD", "--", ".")
            _expect(local.stdout == scoped.stdout, local)
            _expect(
                not _review_view(
                    root,
                    arguments,
                    "diff",
                    "HEAD",
                    "--",
                    "packages/example/" + excluded,
                ).stdout,
                excluded,
            )
            unsupported = (
                ("diff", "--no-index"),
                ("diff", "--no-textconv"),
                ("diff", "--check"),
                ("diff", "--ext-diff"),
                ("diff", "--output=patch"),
            )
            for options in unsupported:
                rejected = _review_view(root, arguments, *options, code=1)
                _expect(not rejected.stdout and bool(rejected.stderr), rejected)
            for revision in (f"HEAD:packages/example/{filename}", "HEAD^{tree}"):
                rejected = _review_view(root, arguments, "show", revision, code=128)
                _expect(
                    not rejected.stdout and "commit type" in rejected.stderr,
                    rejected,
                )
            rejected = _review_view(
                root,
                arguments,
                "diff",
                "missing-revision",
                code=128,
            )
            _expect(not rejected.stdout and bool(rejected.stderr), rejected)
            for layout in ("attributes", "syntax", "linked", "historical-link"):
                source = path
                saved = source.read_bytes()
                attributes = root / ".gitattributes"
                if layout == "attributes":
                    attributes.write_text(f"packages/*/{filename} diff=custom\n")
                elif layout == "syntax":
                    source.write_text("def invalid(")
                else:
                    source.unlink()
                    source.symlink_to("PRIVATE_TARGET")
                    if layout == "historical-link":
                        _test_names_git(root, "add", str(source.relative_to(root)))
                        _test_names_git(root, "commit", "-qm", "Link source")
                        source.unlink()
                        source.write_bytes(saved)
                rejected = _review_view(
                    root,
                    arguments,
                    "show" if layout == "historical-link" else "diff",
                    code=128 if layout == "syntax" else 1,
                )
                _expect(not rejected.stdout and bool(rejected.stderr), rejected)
                attributes.unlink(missing_ok=True)
                source.unlink(missing_ok=True)
                source.write_bytes(saved)
                if layout == "historical-link":
                    _test_names_git(root, "reset", "--hard", "HEAD~1")
                    write_version(staged)
                    _test_names_git(root, "add", ".")
                    write_version(working)
        write_version(staged)
        for path in files.values():
            path.write_text(
                path.read_text() + "raise RuntimeError('body edit must not run')\n",
            )
        _make_test_names_package(root, "untracked", "def test_untracked(): pass\n")
        for arguments in files:
            _expect(
                not _review_view(root, arguments, "diff").stdout,
                "body or untracked edits leaked",
            )
        other = _make_test_names_package(root, "other", "def test_other(): pass\n")
        (other / "main.py").write_text(_review_source("other", arguments=True))
        _test_names_git(root, "add", "packages/other")
        for arguments in files:
            all_packages = _review_view(package, arguments, "diff", "HEAD")
            scoped = _review_view(
                root,
                arguments,
                "diff",
                "HEAD",
                "--",
                "packages/example",
            )
            _expect(
                "+" + row("other", arguments) in all_packages.stdout
                and row("other", arguments) not in scoped.stdout,
                all_packages,
            )
        _test_names_git(root, "reset", "--hard", "HEAD")
        _test_names_git(root, "mv", "packages/example", "packages/renamed")
        for arguments in files:
            renamed = _review_view(root, arguments, "diff", "--staged", "-M")
            filename = "main.py" if arguments else "test_main.py"
            _expect("rename to packages/renamed/" + filename in renamed.stdout, renamed)
            formatted = _review_view(
                root,
                arguments,
                "show",
                "--format=%s",
                "--color=never",
                "-U0",
            )
            _expect(formatted.stdout.startswith("Change declarations\n"), formatted)
        _test_names_git(root, "commit", "-qm", "Rename package")
        _test_names_git(root, "rm", "-rf", "packages/renamed")
        _test_names_git(root, "commit", "-qm", "Delete package")
        for arguments in files:
            _expect(
                "-" + row(committed, arguments)
                in _review_view(root, arguments, "show").stdout,
                "deleted source missing",
            )
            _expect(
                "+" + row(previous, arguments)
                in _review_view(root, arguments, "show", initial).stdout,
                "initial source missing",
            )
    with TemporaryDirectory(prefix="canonical-merge-") as directory:
        _check_merge_diff(_names_repository(Path(directory)))
    with TemporaryDirectory(prefix="canonical-order-") as directory:
        _check_definition_order(Path(directory))


def _runner_package(root: Path, name: str, tests: str, source: str = "") -> None:
    """Add another offline runnable package without replacing the fixture flake."""
    flake = (root / "flake.nix").read_text()
    _make_runner_target(root, source, tests, name=name)
    prefix, ending = flake.rsplit("}; }; }", 1)
    (root / "flake.nix").write_text(
        prefix
        + f" {json.dumps(name)}.python.withPackages = "
        + f"_: {json.dumps(sys.prefix)}; "
        + "}; }; }"
        + ending,
    )
    _git(root, "add", "flake.nix", f"packages/{name}")


def _campaign(
    root: Path,
    environment: dict[str, str],
    command: str,
    *arguments: str,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run an omitted target and preserve original sources, settings, and refs."""
    before = _snapshot(root, exclude=("tmp",))
    result = subprocess.run(  # noqa: S603
        [os.environ["PACKAGE_E2E_EXECUTABLE"], "test", command, *arguments],
        cwd=cwd or root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    _expect(
        _snapshot(root, exclude=("tmp",)) == before,
        "campaign changed original sources or Git state",
    )
    return result


@settings(deadline=None)
@given(
    max_examples=st.integers(min_value=1, max_value=5),
    failure=st.sampled_from(("counterexample", "timeout")),
)
@example(max_examples=5, failure="counterexample")
@example(max_examples=1, failure="timeout")
def test_hypothesis_campaigns_generate_cases_and_isolate_failures(
    max_examples: int,
    failure: str,
) -> None:
    """Run copied Git commands, count cases, and retain failure diagnostics."""
    with TemporaryDirectory(prefix="canonical-campaign-") as directory:
        root = Path(directory) / "source with spaces"
        source = "def main():\n    print('ready')\n"
        tests = (
            "import os, subprocess\n"
            "from pathlib import Path\n"
            "from hypothesis import given, example, strategies as st\n"
            "@given(st.integers())\n"
            "@example(0)\n"
            "def test_property(value):\n"
            "    with Path('examples').open('a') as output:\n"
            "        output.write(str(value) + '\\n')\n"
            "def test_cli():\n"
            "    result = subprocess.run(['git', 'example'], "
            "capture_output=True, text=True)\n"
            "    assert result.returncode == 0 and result.stdout == "
            "'ready\\n'\n"
        )
        environment = _prepare_runner_flake(root, tests, source, name="git-example")
        before = _snapshot(root, exclude=("tmp",))
        result = _run_runner_cli(
            root,
            environment,
            "hypothesis",
            "--max-examples",
            str(max_examples),
        )
        _expect(
            not result.returncode and "1 passed, 0 failed, 0 skipped" in result.stdout,
            result,
        )
        _expect(
            _snapshot(root, exclude=("tmp",)) == before,
            "explicit campaign modified source",
        )
        current = _campaign(
            root,
            environment,
            "hypothesis",
            "--max-examples",
            str(max_examples),
            cwd=root / "packages/git-example",
        )
        _expect(not current.returncode, current)
        workspaces = list((root / "tmp").glob("python-hypothesis-git-example-*"))
        expected_workspaces = 2
        _expect(len(workspaces) == expected_workspaces, workspaces)
        for workspace in workspaces:
            _expect(
                len((workspace / "examples").read_text().splitlines())
                == max_examples + 1,
                workspace,
            )
            _expect(
                f"{max_examples} passing examples"
                in (workspace / "tests.log").read_text(),
                workspace,
            )
        bad_tests = (
            (
                "from hypothesis import given, strategies as st\n"
                "@given(st.integers())\n"
                "def test_failure(value):\n"
                "    assert value != 0\n"
            )
            if failure == "counterexample"
            else (
                "import subprocess, sys, time\n"
                "from pathlib import Path\n"
                "def test_stalled():\n"
                "    child = subprocess.Popen([sys.executable, '-c', 'import "
                "time; time.sleep(30)'])\n"
                "    Path('child.pid').write_text(str(child.pid))\n"
                "    time.sleep(30)\n"
            )
        )
        _runner_package(root, "alpha", bad_tests)
        _runner_package(root, "untested", "")
        (root / "packages/untested/test_main.py").unlink()
        result = _campaign(
            root,
            environment,
            "hypothesis",
            "--max-examples",
            str(max_examples),
            "--timeout",
            "20" if failure == "counterexample" else "2",
        )
        _expect(
            result.returncode == 1 and "1 passed, 1 failed, 1 skipped" in result.stdout,
            result,
        )
        (failed,) = (root / "tmp").glob("python-hypothesis-alpha-*")
        diagnostic = (
            "Falsifying example" if failure == "counterexample" else "timed out"
        )
        _expect(
            diagnostic
            in result.stdout + result.stderr + (failed / "tests.log").read_text(),
            result,
        )
        if failure == "timeout":
            pid = int((failed / "child.pid").read_text())
            status = Path(f"/proc/{pid}/status")
            _expect(
                not status.exists() or "State:\tZ" in status.read_text(),
                "timed-out suite left a running descendant",
            )


@settings(deadline=None)
@given(value=st.integers(min_value=1, max_value=9))
@example(value=1)
def test_mutation_campaigns_report_outcomes_and_reject_invalid_baselines(
    value: int,
) -> None:
    """Report killed/surviving mutations and keep invalid baselines out of scores."""
    with TemporaryDirectory(prefix="canonical-mutations-") as directory:
        root = Path(directory) / "source with spaces"
        source = (
            "def value():\n"
            f"    return {value}\n"
            "\n"
            "def unused():\n"
            "    return 2\n"
            "\n"
            "def main():\n"
            "    print(value())\n"
        )
        tests = (
            "import subprocess\n"
            "def test_cli():\n"
            "    result = subprocess.run(['git', 'example'], "
            "capture_output=True, text=True)\n"
            "    assert result.returncode == 0 "
            f"and result.stdout == {str(value) + chr(10)!r}\n"
        )
        environment = _prepare_runner_flake(root, tests, source, name="git-example")
        result = _campaign(
            root,
            environment,
            "mutation",
            "--timeout",
            "10",
            cwd=root / "packages/git-example",
        )
        _expect(not result.returncode, result)
        (workspace,) = (root / "tmp").glob("python-mutation-git-example-*")
        summary = json.loads((workspace / "summary.json").read_text())
        _expect(
            summary.get("killed", 0) > 0
            and summary.get("survived", 0) > 0
            and (workspace / "report.html").stat().st_size > 0,
            summary,
        )
        baselines = {
            "alpha": "def test_failure():\n    assert False\n",
            "beta": "",
            "gamma": "raise ImportError('missing dependency')\n",
        }
        for name, baseline in baselines.items():
            _runner_package(root, name, baseline)
        _runner_package(root, "untested", "")
        (root / "packages/untested/test_main.py").unlink()
        result = _campaign(root, environment, "mutation", "--timeout", "10")
        _expect(
            result.returncode == 1
            and "1 passed, 3 failed, 1 skipped" in result.stdout
            and "baseline.log" in result.stderr,
            result,
        )
        for name in baselines:
            (failed,) = (root / "tmp").glob(f"python-mutation-{name}-*")
            _expect(
                (failed / "baseline.log").is_file()
                and not (failed / "summary.json").exists(),
                failed,
            )
    with TemporaryDirectory(prefix="canonical-empty-mutations-") as directory:
        root = Path(directory) / "source"
        tests = (
            "from packages.example import main\n"
            "from hypothesis import given, example, strategies as st\n"
            "@given(st.just(1))\n"
            "@example(0)\n"
            "def test_explicit(value):\n"
            "    assert value == 0 and main is not None\n"
        )
        environment = _prepare_runner_flake(root, tests, "")
        result = _campaign(root, environment, "mutation")
        (workspace,) = (root / "tmp").glob("python-mutation-example-*")
        _expect(
            not result.returncode
            and json.loads((workspace / "summary.json").read_text()) == {},
            result,
        )


def test_coverage_checks_measure_subprocesses_and_continue_after_failures() -> None:
    """Build real checks, measure CLI lines, and keep reports outside the checkout."""
    with TemporaryDirectory(prefix="canonical-coverage-") as directory:
        root = Path(directory) / "source with spaces"
        environment = _prepare_coverage_flake(root)
        before = _snapshot(root)
        expression = (
            f"let f = builtins.getFlake {json.dumps('git+' + root.as_uri())}; "
            "in f.checks.${builtins.currentSystem}.example"
        )
        plain = _run(
            root,
            "build",
            "--no-link",
            "--print-out-paths",
            "--impure",
            "--expr",
            expression,
            executable="nix",
            environment=environment,
        )
        _expect(
            not any(
                path.name.startswith(".coverage")
                or path.name in {"html", "coverage.json"}
                for path in Path(plain.stdout.strip()).iterdir()
            ),
            "ordinary checks produced coverage artifacts",
        )
        for target in (root, root / "packages/z-last"):
            explicit = _run_runner_cli(target, environment, "coverage")
            current = _campaign(root, environment, "coverage", cwd=target)
            _expect(
                not explicit.returncode
                and explicit.stdout == current.stdout
                and not current.returncode,
                (explicit, current),
            )
            reports = [
                line
                for line in explicit.stdout.splitlines()
                if line.endswith("/html/index.html")
            ]
            _expect(len(reports) == (2 if target == root else 1), explicit)
            for line in reports:
                report = Path(line.split(": ", 1)[1]).parents[1]
                files = json.loads((report / "coverage.json").read_text())["files"]
                _expect(
                    len(files) == 1 and next(iter(files)).endswith("/main.py"),
                    files,
                )
                _expect(
                    set(next(iter(files.values()))["executed_lines"]) == {1, 2},
                    files,
                )
        _expect(
            _snapshot(root) == before and not (root / "tmp").exists(),
            "coverage changed repository state",
        )
    for failure in ("build", "report", "check", "syntax"):
        with TemporaryDirectory(prefix="canonical-coverage-failure-") as directory:
            root = Path(directory) / "source"
            environment = _prepare_coverage_flake(root, failure=failure)
            if failure == "check":
                (root / "checks/example/default.nix").unlink()
            if failure == "syntax":
                (root / "packages/example/test_main.py").write_text("def invalid(")
            untested = _make_runner_target(root, "", "", name="untested")
            (untested / "test_main.py").unlink()
            (root / "flake.nix").write_text(_git(root, "show", ":flake.nix"))
            before = _snapshot(root)
            result = _run_runner_cli(root, environment, "coverage")
            _expect(
                result.returncode == 1
                and "1 passed, 1 failed, 1 skipped" in result.stdout,
                result,
            )
            _expect(
                "z-last:" in result.stdout
                and "/html/index.html" in result.stdout
                and "canonical test coverage: example:" in result.stderr,
                result,
            )
            _expect(
                _snapshot(root) == before,
                f"{failure} failure changed repository state",
            )


def test_cli_contracts_validate_help_interfaces_budgets_and_targets() -> None:  # noqa: C901, PLR0912
    """Expose consistent commands and reject invalid requests before creating state."""
    with TemporaryDirectory(prefix="canonical-cli-") as directory:
        root = Path(directory)
        commands = (
            (),
            ("add",),
            ("mv",),
            ("rm",),
            ("init",),
            ("converge",),
            ("overview",),
            ("args",),
            ("test",),
            ("test", "names"),
            ("test", "coverage"),
            ("test", "hypothesis"),
            ("test", "mutation"),
        )
        for path in commands:
            option = _run(root, *path, "--help").stdout
            _expect(
                option == _run(root, "help", *path).stdout and "usage:" in option,
                path,
            )
        environment = dict(os.environ)
        environment["PATH"] = (
            str(Path(os.environ["PACKAGE_E2E_EXECUTABLE"]).parent)
            + os.pathsep
            + environment["PATH"]
        )
        _expect(
            _run(root, "help", executable="canonical", environment=environment).stdout
            == _run(root, "--help").stdout,
            "standalone command unavailable through PATH",
        )
        for retired in (
            "status",
            "check",
            "test-names",
            "coverage",
            "hypothesis",
            "mutation",
        ):
            _run(root, retired, code=2)
        _expect(
            _run(root, "test").stdout == _run(root, "test", "--help").stdout,
            "bare test must show help",
        )
        _run(root, "test", "unknown", code=2)
        names_help = _run(root, "test", "names", "--help").stdout
        _expect(
            "[target]" in names_help and "current directory" in names_help,
            names_help,
        )
        for option in ("--timeout", "--max-examples"):
            _run(root, "test", "names", option, "10", code=2)
        for command in ("names", "coverage", "hypothesis", "mutation"):
            _run(root, "test", command, code=1)
        for command in ("hypothesis", "mutation"):
            for timeout in ("nan", "inf", "-inf", "0", "-1"):
                _run(root, "test", command, str(root), f"--timeout={timeout}", code=2)
        _run(root, "test", "hypothesis", str(root), "--max-examples", "0", code=2)
        _expect(not any(root.iterdir()), "invalid CLI request created state")
    for layout in ("empty", "nonpython", "untested", "single_untested"):
        with _fresh_repository() as root:
            target = root
            if layout == "nonpython":
                _run(root, "add", "packages/web", "html")
            elif layout in {"untested", "single_untested"}:
                target = _make_runner_target(root, "", "")
                (target / "test_main.py").unlink()
                if layout == "untested":
                    target = root
            before = _snapshot(root)
            for command in ("hypothesis", "mutation"):
                result = _run(
                    root,
                    "test",
                    command,
                    str(target),
                    code=0 if layout == "untested" else 1,
                )
                if layout == "untested":
                    _expect(
                        "Skipping example: no test_main.py" in result.stdout
                        and "0 passed, 0 failed, 1 skipped" in result.stdout,
                        result,
                    )
                elif layout != "single_untested":
                    _expect("no Python packages found" in result.stderr, result)
            if layout == "empty":
                for command in ("names", "coverage"):
                    _expect(
                        "no Python packages found"
                        in _run(root, "test", command, code=1).stderr,
                        command,
                    )
            _expect(_snapshot(root) == before, "nonrunnable target created state")
    with pytest.MonkeyPatch.context() as monkeypatch:
        _check_command_catalog(monkeypatch)
    _check_discovery_boundaries()

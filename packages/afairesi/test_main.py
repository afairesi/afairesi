# Copyright (c) 2026- Paschalis Bizopoulos
"""Check Afairesi's public contracts with explicit regressions and generated cases."""

from __future__ import annotations

import getpass
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
from contextlib import contextmanager
from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, cast

import coverage
import jsonpatch
import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st
from jsonpointer import JsonPointer

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


def _make_source_package(root: Path, name: str, source: str) -> Path:
    """Create a canonical package fixture without executing its source."""
    package = root / "packages" / name
    package.mkdir(parents=True)
    (root / "flake.nix").touch()
    (package / "default.nix").touch()
    (package / "main.py").touch()
    (package / "test_main.py").write_text(source)
    return package


def _fixture_git(root: Path, *arguments: str) -> str:
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
    builders = root / "prm/builders"
    shutil.copytree(Path(__file__).with_name("prm"), builders)
    environment = dict(os.environ)
    store = root.parent / "nix"
    environment["NIX_REMOTE"] = (
        f"local?store={store / 'store'}&state={store / 'state'}&log={store / 'log'}"
    )
    environment["NIX_CONFIG"] = (
        "experimental-features = nix-command flakes\nbuild-users-group =\n"
    )
    system = subprocess.run(
        ["nix", "eval", "--impure", "--raw", "--expr", "builtins.currentSystem"],  # noqa: S607
        env=environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout
    (root / "flake.nix").write_text(
        "{ outputs = _: let lib = import ./prm/builders/builders.nix; "
        "pkgs = { lib = { concatMap = f: xs: builtins.concatLists (map f xs); "
        'makeBinPath = _: ""; }; writeText = builtins.toFile; }; '
        f"packageDrv.python.withPackages = _ : {json.dumps(sys.prefix)}; "
        "in { "
        f"packages.{json.dumps(system)} = builtins.listToAttrs (map (name: {{ "
        'name = "${name}-test-environment"; '
        "value = (lib.mkTestEnvironment { inherit pkgs packageDrv; }).manifest; "
        "}) (builtins.attrNames (builtins.readDir ./packages))); }; }",
        encoding="utf-8",
    )
    for args in (["init", "--quiet"], ["add", "."]):
        subprocess.run(  # noqa: S603
            ["git", "-C", str(root), *args],  # noqa: S607
            check=True,
            capture_output=True,
            timeout=10,
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
        ["afairesi", "test", command, str(root), *arguments],  # noqa: S607
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def _prepare_coverage_flake(
    root: Path,
    *,
    fail_build: bool = False,
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
    reports = []
    shutil.copytree(Path(__file__).with_name("prm"), root / "prm/builders")
    for name in ("example", "z-last"):
        _run(root, "add", f"packages/{name}", "python")
        package = root / "packages" / name
        source = (
            "import os\n"
            "def main():\n"
            "    if os.getenv('AFAIRESI_COVERAGE_CHOICE') == 'alternate':\n"
            "        print('alternate')\n"
            "    else:\n"
            "        print('ready')\n"
        )
        tests = (
            "import os, subprocess, pytest\n"
            "from hypothesis import given, example, strategies as st\n"
            "@given(st.just(1))\n"
            "@example(0)\n"
            "def test_explicit(value):\n"
            "    assert value == 0\n"
            "@given(st.just(1))\n"
            "def test_generated_only(value):\n"
            "    assert value == 1\n"
            "@pytest.mark.skip(reason='optional browser')\n"
            "def test_optional_browser():\n"
            "    assert False\n"
            "@pytest.mark.parametrize('message', ['ready', 'alternate'])\n"
            "def test_cli(message, monkeypatch):\n"
            "    monkeypatch.setenv('AFAIRESI_COVERAGE_CHOICE', message)\n"
            "    result = "
            "subprocess.run([os.environ['PACKAGE_E2E_EXECUTABLE']],\n"
            "        capture_output=True, text=True)\n"
            "    assert result.stdout == message + '\\n'\n"
            "    assert result.stderr == ''\n"
        )
        if name == "example" and fail_build:
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
            "inputs.self = { inherit lib; packages.${system} = packages; }; }"
        )
        checks.append(f"{json.dumps(name)} = {expression};")
        reports.append(
            f"{json.dumps(name + '-coverage')} = lib.mkCoverage {{ "
            f"packageName = {json.dumps(name)}; "
            f"packageDrv = packages.{json.dumps(name)}; "
            f"check = checks.{json.dumps(name)}; }};",
        )
    _run(root, "converge")
    (root / "flake.nix").write_text(
        "{ outputs = _: let lib = import ./prm/builders/builders.nix; "
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
        + " }; checks = { "
        + " ".join(checks)
        + " }; "
        "in { packages.${system} = packages // { " + " ".join(reports) + " }; "
        "checks.${system} = checks; }; }\n",
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
    subject = import_module("packages.afairesi.main")
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


def _check_formatted_checks(
    repository: Path,
) -> None:
    """Preserve formatted generated checks through the full formatting pipeline."""
    _run(repository, "add", "hosts/laptop")
    (repository / "packages/example/test_main.py").write_text(
        "def test_result(): pass\n",
    )
    _run(repository, "converge")
    for relative in (
        "checks/laptopVmWithDisko/default.nix",
        "checks/example/default.nix",
    ):
        check = repository / relative
        _run(repository, "--no-cache", relative, executable="treefmt")
        formatted = check.read_text(encoding="utf-8")
        _git(repository, "add", "--", relative)
        index = _git(repository, "ls-files", "--stage")
        result = _run(repository, "converge")
        _expect(
            check.read_text(encoding="utf-8") == formatted
            and _git(repository, "ls-files", "--stage") == index
            and f"write '{relative}'" not in result.stdout,
            f"convergence regenerated formatted {relative}",
        )


def _check_host_repair(repository: Path) -> None:
    """Repair a changed check while preserving its host and resource inventory."""
    _run(repository, "add", "hosts/laptop")
    host = repository / "hosts/laptop/configuration.nix"
    original_host = host.read_bytes()
    original_paths = _git(repository, "ls-files").splitlines()
    relative = "checks/laptopVmWithDisko/default.nix"
    check = repository / relative
    current = check.read_bytes()
    changed = current.replace(b'"VmWithDisko"', b'"Changed"')
    check.write_bytes(changed)
    _git(repository, "add", "--", relative)
    _run(repository, "converge", "--dry-run", code=1)
    if check.read_bytes() != changed:
        message = "dry-run changed the generated check"
        raise AssertionError(message)
    _run(repository, "converge")
    if check.read_bytes() != current:
        message = "convergence did not repair the generated check"
        raise AssertionError(message)
    if host.read_bytes() != original_host:
        message = "check repair changed the host configuration"
        raise AssertionError(message)
    if _git(repository, "ls-files").splitlines() != original_paths:
        message = "check repair added consumer files"
        raise AssertionError(message)
    if _run(repository, "converge", "--dry-run").stdout:
        message = "check repair did not converge"
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
    monkeypatch.setenv("AFAIRESI_NIX", str(fake_nix))
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
    """Allow Git settings beyond the path and URL managed by Afairesi."""
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
    whitelist = (root / ".gitignore").read_text().splitlines()
    _expect(f"!/{relative}" not in whitelist, whitelist)
    _expect(f"!/{destination}" in whitelist, whitelist)


def _check_overview_details(
    repository: Path,
) -> None:
    """Expose structured declarations while preserving source facts."""
    subject = import_module("packages.afairesi.main")
    for files in (
        {},
        {"main.py": "VALUE = 1\n"},
        {"default.nix": "{}\n", "test_main.py": ""},
    ):
        details = subject.source_resource_data("empty", files)
        _expect(subject.resource_summary(details) == {}, details)
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
    asset.write_text("const value = 1;\n\n")
    (asset.parent / "picture.png").write_bytes(b"binary\n")
    (asset.parent / "linked.py").symlink_to(package / "main.py")
    (package / "tmp").mkdir()
    (package / "tmp/generated.py").write_text("runtime\n")
    data = _overview(repository)
    details = subject.resource_data(package)
    if details["description"] != "Example" or details["tests"] != ["result"]:
        msg = "Descriptions and test sentences must be preserved as separate facts"
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
    if sources["prm/nested/script.js"]["lines"] != expected_lines:
        msg = "Asset line counts must be structured source facts"
        raise AssertionError(msg)
    expected = {
        "commands": {"build": {"arguments": ["--jobs  optional; default=2"]}},
        "description": "Example",
        "tests": ["result"],
    }
    _expect(data == {"packages": {"example": expected}}, data)
    _expect(_overview(package) == data, data)
    tree = _overview_tree(repository)
    _expect(
        tree[socket.gethostname()][getpass.getuser()]["local"][getpass.getuser()][
            repository.name
        ]
        == data,
        tree,
    )
    (package / "test_main.py").write_text("def invalid(")
    unavailable = _overview(package)["packages"]["example"]
    _expect(
        "tests" not in unavailable and "tests" in unavailable["diagnostics"],
        unavailable,
    )
    (package / "test_main.py").write_text("def test_result(): pass\n")
    _expect(
        _overview_resources(_run(repository, "packages/example").stdout)["packages"][
            "example"
        ]
        == expected,
        expected,
    )


def _check_home_summaries(home_repository: Path) -> None:
    """Group same-named packages and hosts by repository without including checks."""
    scopes = ("forge.example/owner/demo", "forge.example/owner/second")
    for relative in scopes:
        root = home_repository / relative
        root.mkdir(parents=True, exist_ok=True)
        (root / "flake.nix").write_text("", encoding="utf-8")
        (root / "packages/same").mkdir(parents=True)
        (root / "packages/same/default.nix").write_text(
            '{ meta.description = "Same"; }',
            encoding="utf-8",
        )
        for collection, filename in (
            ("checks", "default.nix"),
            ("hosts", "configuration.nix"),
        ):
            (root / collection / "same").mkdir(parents=True)
            (root / collection / "same" / filename).write_text("{}")
    with (home_repository / ".gitmodules").open("a", encoding="utf-8") as stream:
        stream.write('[submodule "second"]\npath = forge.example/owner/second\n')
    before = _snapshot(home_repository)
    data = _overview_tree(home_repository)
    _expect(
        data
        == {
            socket.gethostname(): {
                str(home_repository): {
                    ".gitignore": None,
                    ".gitmodules": None,
                    "forge.example/": {
                        "owner/": {
                            name + "/": {
                                "packages": {"same": {"description": "Same"}},
                                "hosts": {"same": {}},
                            }
                            for name in ("demo", "second")
                        },
                    },
                },
            },
        },
        data,
    )
    _expect(_snapshot(home_repository) == before, "JSON inspection changed the home")
    _git(home_repository, "config", "-f", ".gitmodules", "submodule.second.path", "..")
    rejected = _run(home_repository, str(home_repository), code=1)
    _expect("submodule path escapes" in rejected.stderr, rejected)


def _check_host_summaries(repository: Path) -> None:
    """Summarize host-only flakes and preserve diagnostics and explicit scope."""
    host = repository / "hosts/laptop"
    host.mkdir(parents=True)
    configuration = host / "configuration.nix"
    configuration.write_text(
        '{ inputs, system, ... }: { meta.description = "Laptop"; '
        "environment.systemPackages = [ inputs.self.packages.${system}.tool ]; }\n",
    )
    (repository / "hosts/linked").symlink_to(host, target_is_directory=True)
    linked_source = repository / "hosts/linked-source"
    linked_source.mkdir()
    (linked_source / "configuration.nix").symlink_to(configuration)
    expected = {
        "hosts": {
            "laptop": {
                "description": "Laptop",
                "dependencies": ["runtime: packages/tool"],
            },
        },
    }
    before = _snapshot(repository)
    _expect(_overview(repository) == expected, expected)
    _expect(_overview(host) == expected, expected)
    _expect(
        _overview_tree(host) == _overview_tree(repository),
        expected,
    )
    _expect(_snapshot(repository) == before, "host inspection changed source")
    configuration.write_text("{ invalid =")
    broken = _overview(host)["hosts"]["laptop"]
    _expect("dependencies" in broken["diagnostics"] and "name" not in broken, broken)
    configuration.unlink()
    report = json.loads(_run(repository, str(repository)).stdout)
    _expect(report[socket.gethostname()]["migration"]["diagnostics"], report)


CLI_CONTRACTS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("import argparse\ndef parser():\n    return argparse.ArgumentParser()\n", {}),
    ("VALUE = 1\n", {}),
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
        {"arguments": ["--public  optional"]},
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
        {
            "arguments": ["-o, --output  optional; default='out'"],
            "commands": {
                "run": {"arguments": ["mode  required; choices=['fast', 'slow']"]},
            },
        },
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
        {
            "arguments": ["-o, --output  optional; default='out'"],
            "commands": {
                "run": {"arguments": ["mode  required; choices=['fast', 'slow']"]},
            },
        },
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
        {
            "arguments": ["-o, --output  optional; default='out'"],
            "commands": {
                "run": {"arguments": ["mode  required; choices=['fast', 'slow']"]},
            },
        },
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
)
UNSUPPORTED_INTERFACES = (
    "import click\n@click.command()\ndef cli(): pass\n",
    "import typer\napp = typer.Typer()\n@app.command()\ndef cli(): pass\n",
    "import fire\ndef cli(): pass\nfire.Fire(cli)\n",
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
    with TemporaryDirectory(prefix="afairesi-contract-") as directory:
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


def _overview_tree(root: Path) -> dict[str, Any]:
    """Read the resource hierarchy independently of migration assessment."""
    return _overview_details(_run(root, str(root)).stdout)


def _overview_details(output: str) -> dict[str, Any]:
    """Separate resource details from migration facts in explicit inspection."""
    tree = json.loads(output)
    tree[socket.gethostname()].pop("migration")
    return cast("dict[str, Any]", tree)


def _overview(root: Path) -> dict[str, Any]:
    """Read one repository's resource groups from the CLI."""
    return _overview_resources(_run(root, str(root)).stdout)


def _overview_resources(output: str) -> dict[str, Any]:
    """Extract resource groups after validating the machine and user hierarchy."""
    tree = _overview_details(output)
    _expect(set(tree) == {socket.gethostname()}, tree)
    machine = tree[socket.gethostname()]
    _expect(set(machine) == {getpass.getuser()}, machine)
    branch = machine[getpass.getuser()]
    while not set(branch).issubset({"hosts", "packages"}):
        _expect(len(branch) == 1, branch)
        branch = next(iter(branch.values()))
    return cast("dict[str, Any]", branch)


def _patch_values(patch: list[dict[str, Any]], action: str, *path: str) -> list[Any]:
    """Read changed values at a semantic field from standard JSON Pointer paths."""
    values = []
    for operation in patch:
        parts = JsonPointer(operation["path"]).parts[1:]
        if operation["op"] != action or parts[: len(path)] != list(path):
            continue
        value = operation["value"]
        if len(parts) == len(path) and isinstance(value, list):
            values.extend(value)
        else:
            values.append(value)
    return values


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
        "lib = { optionals = condition: values: if condition then values else []; "
        'optionalString = condition: value: if condition then value else ""; '
        "optionalAttrs = condition: attrs: if condition then attrs else {}; }; "
        "runCommand = name: attrs: script: mk name "
        '("runHook() {\\n" + (attrs.postInstall or ":") + "\\n}\\n" + script); '
        "writeShellApplication = attrs: (mk attrs.name "
        "("
        + json.dumps(
            'mkdir -p "$out/bin"\ncat > "$out/bin/${attrs.name}" '
            f"<<'AFAIRESI_SCRIPT'\n#!{bash}\n",
        )
        + " + attrs.text + "
        + json.dumps('\nAFAIRESI_SCRIPT\nchmod 755 "$out/bin/${attrs.name}"\n')
        + ")) // attrs; }; "
        + f"package = import {root / 'packages' / name / 'default.nix'} "
        + "{ inherit pkgs; "
        + "inputs.self.lib = import "
        + f"{Path(__file__).with_name('prm') / 'builders.nix'}; "
        + "}; in "
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
        "concatMap = f: xs: builtins.concatLists (map f xs); "
        "optionalAttrs = condition: attrs: if condition then attrs else {}; "
        'getExe = p: if p.meta ? mainProgram then "/package/bin/${p.meta.mainProgram}" '
        'else abort "library check requested an executable"; }; }; '
        "packageDrv = package // { "
        'checkInputs = ["check-input"]; nativeCheckInputs = ["native-check-input"]; '
        "python = python // { withPackages = select: { selected = select { "
        'hypothesis = "hypothesis"; pytest = "pytest"; }; }; }; '
        f"meta = {metadata}; }}; "
        f"in import {root / 'checks' / name / 'default.nix'} {{ pkgs = checkPkgs; "
        "inputs.self = { "
        f"lib = import {Path(__file__).with_name('prm') / 'builders.nix'}; "
        "packages.${builtins.currentSystem}."
        f"{json.dumps(name)} = packageDrv; }}; }}"
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
    native_inputs = attributes["nativeBuildInputs"]
    _expect("check-input" in native_inputs, native_inputs)
    _expect("native-check-input" in native_inputs, native_inputs)
    python_inputs = native_inputs[-1]["selected"]
    _expect("check-input" in python_inputs, python_inputs)
    _expect("native-check-input" in python_inputs, python_inputs)


def _runner_package(root: Path, name: str, tests: str, source: str = "") -> None:
    """Add another offline runnable package without replacing the fixture flake."""
    flake = (root / "flake.nix").read_text()
    _make_runner_target(root, source, tests, name=name)
    (root / "flake.nix").write_text(flake)
    _git(root, "add", ".")


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


def _test_declarations(labels: list[str], form: str) -> str:
    """Build static test definitions with hidden helpers and hostile top-level code."""
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
    return tests


@pytest.mark.parametrize("relative", [None, "..", "."])
def test_campaign_home_targets_reject_empty_or_escaping_submodules(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    relative: str | None,
) -> None:
    """Reject invalid home selections before any campaign runs."""
    subject = import_module("packages.afairesi.main")
    _git(tmp_path, "init", "--quiet")
    (tmp_path / ".gitmodules").write_text(
        "" if relative is None else f'[submodule "bad"]\n\tpath = {relative}\n',
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["afairesi", "test"])
    with pytest.raises(SystemExit) as completed:
        subject.main()
    output = capsys.readouterr()
    _expect(completed.value.code == 1, completed)
    _expect(
        ("no submodules found" if relative is None else "submodule path escapes")
        in output.err,
        output,
    )


@pytest.mark.parametrize("command", [None, "coverage", "hypothesis", "mutation"])
@pytest.mark.parametrize("failure", [None, "coverage", "hypothesis", "mutation"])
def test_campaign_home_targets_run_all_submodules_after_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str | None,
    failure: str | None,
) -> None:
    """Expand home scope while preserving campaign options and failure status."""
    subject = import_module("packages.afairesi.main")
    _git(tmp_path, "init", "--quiet")
    roots = [tmp_path / "repos" / name for name in ("alpha", "beta")]
    for root in roots:
        root.mkdir(parents=True)
        _repository(root)
    (tmp_path / ".gitmodules").write_text(
        "".join(
            f'[submodule "{root.name}"]\n\tpath = repos/{root.name}\n' for root in roots
        ),
    )
    nested = tmp_path / "outside"
    nested.mkdir()
    observed: list[tuple[str, Path]] = []

    def run(target: Path, campaign: str) -> bool:
        observed.append((campaign, target))
        if target == roots[0] and campaign == failure:
            message = "campaign failed"
            raise subject.CommandError(message)
        return True

    def coverage_run(target: Path) -> bool:
        return run(target, "coverage")

    def runner(
        target: Path,
        campaign: str,
        _timeout: float | None,
        _max_examples: int | None,
        selection: object,
    ) -> bool:
        if command in {"hypothesis", "mutation"}:
            _expect(getattr(selection, "keywords", None) == "chosen", selection)
        return run(target, campaign)

    monkeypatch.setattr(subject, "_run_coverage", coverage_run)
    monkeypatch.setattr(subject, "_run_test_repository", runner)
    commands = [command] if command else ["coverage", "hypothesis", "mutation"]
    arguments = ["test", command] if command else ["test"]
    if command in {"hypothesis", "mutation"}:
        arguments.extend(["-k", "chosen"])
    for cwd in (tmp_path, nested):
        monkeypatch.chdir(cwd)
        observed.clear()
        monkeypatch.setattr(sys, "argv", ["afairesi", *arguments])
        with pytest.raises(SystemExit) as completed:
            subject.main()
        _expect(completed.value.code == int(failure in commands), completed)
        _expect(
            observed == [(campaign, root) for campaign in commands for root in roots],
            observed,
        )
        output = capsys.readouterr()
        _expect(
            "Submodule summary" in output.out and "repos/beta: passed" in output.out,
            output,
        )
    if command:
        observed.clear()
        monkeypatch.setattr(sys, "argv", ["afairesi", *arguments, str(tmp_path)])
        with pytest.raises(SystemExit):
            subject.main()
        _expect(observed == [(command, root) for root in roots], observed)


def test_campaign_targets_respect_repository_defaults_and_explicit_packages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Route campaigns independently of their expensive test engines."""
    subject = import_module("packages.afairesi.main")
    root = _repository(tmp_path)
    package = _make_source_package(root, "alpha", "def test_alpha(): pass\n")
    nested = package / "prm/nested"
    nested.mkdir(parents=True)
    observed: list[Path] = []

    def run(target: Path, *_arguments: object) -> bool:
        observed.append(target)
        return True

    for runner in (
        "_run_coverage",
        "_run_test_package",
        "_run_test_repository",
    ):
        monkeypatch.setattr(subject, runner, run)
    for cwd in (package, nested):
        monkeypatch.chdir(cwd)
        observed.clear()
        monkeypatch.setattr(sys, "argv", ["afairesi", "test"])
        with pytest.raises(SystemExit) as combined:
            subject.main()
        _expect(combined.value.code == 0 and observed == [root] * 3, observed)
        for path in (
            ("test", "coverage"),
            ("test", "hypothesis"),
            ("test", "mutation"),
        ):
            targets = (
                (((), root), ((".",), package)) if cwd == package else (((), root),)
            )
            for arguments, expected in targets:
                monkeypatch.setattr(sys, "argv", ["afairesi", *path, *arguments])
                with pytest.raises(SystemExit) as completed:
                    subject.main()
                _expect(completed.value.code == 0, path)
                _expect(observed[-1] == expected, (path, arguments, observed))


def test_cli_contracts_validate_help_interfaces_budgets_and_targets() -> None:  # noqa: C901, PLR0912
    """Expose consistent commands and reject invalid requests before creating state."""
    with TemporaryDirectory(prefix="afairesi-cli-") as directory:
        root = Path(directory)
        commands = (
            (),
            ("add",),
            ("mv",),
            ("rm",),
            ("init",),
            ("converge",),
            ("test",),
            ("test", "coverage"),
            ("test", "hypothesis"),
            ("test", "mutation"),
        )
        for path in commands:
            option = _run(root, *path, "--help").stdout
            _expect(
                "usage:" in option,
                path,
            )
        environment = dict(os.environ)
        environment["PATH"] = (
            str(Path(os.environ["PACKAGE_E2E_EXECUTABLE"]).parent)
            + os.pathsep
            + environment["PATH"]
        )
        _expect(
            _run(root, "--help", executable="afairesi", environment=environment).stdout
            == _run(root, "--help").stdout,
            "standalone command unavailable through PATH",
        )
        for retired in (
            "help",
            "status",
            "check",
            "overview",
            "test-names",
            "coverage",
            "hypothesis",
            "mutation",
        ):
            _run(root, retired, code=2)
        _expect(
            any(
                name.startswith("/")
                for name in json.loads(_run(root).stdout)[socket.gethostname()][
                    "filesystem"
                ]
            ),
            "bare afairesi must show machine preservation outside repositories",
        )
        _expect(
            "not inside a Git repository" in _run(root, "test", code=1).stderr,
            "bare test must require a repository",
        )
        overview_help = _run(root, "--help").stdout
        _expect(
            "formatted JSON" in overview_help
            and "--json" not in overview_help
            and "--full" not in overview_help
            and "--revision" not in overview_help,
            overview_help,
        )
        for removed in (("--full",), ("--revision", "HEAD")):
            _run(root, *removed, code=2)
        _run(root, "test", "unknown", code=2)
        for path in (("args",), ("test", "names")):
            for arguments in (
                (),
                (str(root),),
                ("diff",),
                ("show",),
                ("--help",),
                ("_textconv", "main.py"),
            ):
                rejected = _run(root, *path, *arguments, code=2)
                _expect("error:" in rejected.stderr, rejected)
        for command in ("coverage", "hypothesis", "mutation"):
            _run(root, "test", command, code=1)
        for command in ("hypothesis", "mutation"):
            for timeout in ("nan", "inf", "-inf", "0", "-1"):
                _run(root, "test", command, str(root), f"--timeout={timeout}", code=2)
        _run(root, "test", "hypothesis", str(root), "--max-examples", "0", code=2)
        for arguments in (
            ("--max-mutations", "0"),
            ("--max-mutations", "-1"),
            ("--lines", "0"),
            ("--lines", "3:2"),
            ("--lines", "bad"),
            ("--operator", "["),
        ):
            _run(root, "test", "mutation", str(root), *arguments, code=2)
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
                _expect(
                    "no Python packages found"
                    in _run(root, "test", "coverage", code=1).stderr,
                    "coverage",
                )
            _expect(_snapshot(root) == before, "nonrunnable target created state")


def test_cli_summaries_nest_subcommands_and_preserve_argument_ownership(
    tmp_path: Path,
) -> None:
    """Keep root, parent, child, sibling, and empty command interfaces distinct."""
    _repository(tmp_path)
    package = _make_source_package(tmp_path, "example", "")
    source = (
        "import argparse\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('--verbose')\n"
        "commands = p.add_subparsers()\n"
        "init = commands.add_parser('init')\n"
        "test = commands.add_parser('test')\n"
        "test.add_argument('--timeout')\n"
        "children = test.add_subparsers()\n"
        "coverage = children.add_parser('coverage')\n"
        "coverage.add_argument('--jobs', default=2)\n"
        "hypothesis = children.add_parser('hypothesis')\n"
        "hypothesis.add_argument('--jobs', default=4)\n"
    )
    (package / "main.py").write_text(source)
    expected = {
        "arguments": ["--verbose  optional"],
        "commands": {
            "init": {},
            "test": {
                "arguments": ["--timeout  optional"],
                "commands": {
                    "coverage": {"arguments": ["--jobs  optional; default=2"]},
                    "hypothesis": {"arguments": ["--jobs  optional; default=4"]},
                },
            },
        },
    }
    output = _overview_resources(_run(package, ".").stdout)["packages"]["example"]
    _expect(output == expected, output)
    _git(tmp_path, "add", ".")
    (package / "main.py").write_text(source.replace("default=2", "default=3"))
    data = json.loads(_run(tmp_path, "diff", ".").stdout)
    path = (
        "packages",
        "example",
        "commands",
        "test",
        "commands",
        "coverage",
        "arguments",
    )
    _expect(
        _patch_values(data, "remove", *path) == ["--jobs  optional; default=2"],
        data,
    )
    _expect(_patch_values(data, "add", *path) == ["--jobs  optional; default=3"], data)


@pytest.mark.parametrize("failure", [None, "coverage", "hypothesis", "mutation"])
def test_combined_campaigns_continue_after_failures_and_report_one_exit_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str | None,
) -> None:
    """Execute every campaign in order even when an earlier campaign fails."""
    subject = import_module("packages.afairesi.main")
    _repository(tmp_path)
    monkeypatch.chdir(tmp_path)
    observed: list[str] = []

    def run(command: str) -> bool:
        observed.append(command)
        if command == failure and command != "mutation":
            message = "campaign failed"
            raise subject.CommandError(message)
        return command != failure

    def coverage_run(_target: Path) -> bool:
        return run("coverage")

    def runner(
        _target: Path,
        command: str,
        *_arguments: object,
    ) -> bool:
        return run(command)

    monkeypatch.setattr(subject, "_run_coverage", coverage_run)
    monkeypatch.setattr(subject, "_run_test_repository", runner)
    monkeypatch.setattr(sys, "argv", ["afairesi", "test", "--help"])
    with pytest.raises(SystemExit) as help_status:
        subject.main()
    _expect(help_status.value.code == 0 and not observed, observed)
    capsys.readouterr()
    monkeypatch.setattr(sys, "argv", ["afairesi", "test"])
    with pytest.raises(SystemExit) as completed:
        subject.main()
    _expect(completed.value.code == (0 if failure is None else 1), completed)
    _expect(observed == ["coverage", "hypothesis", "mutation"], observed)
    output = capsys.readouterr()
    _expect("Campaign summary:" in output.out, output)
    for command in observed:
        status = "failed" if command == failure else "passed"
        _expect(f"  {command}: {status}\n" in output.out, output)


def test_convergence_preserves_formatted_checks() -> None:
    """Keep generated checks stable after the real formatting pipeline."""
    with _fresh_repository() as root:
        _run(root, "add", "packages/example", "python")
        _check_formatted_checks(root)


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


@pytest.mark.parametrize(
    "misplaced",
    [
        "def test_misplaced(): pass\n",
        (
            "from unittest import TestCase as Case\n"
            "class Reports(Case):\n"
            "    def test_result(self): pass\n"
        ),
    ],
)
def test_convergence_rejects_embedded_tests_without_changing_state(
    misplaced: str,
) -> None:
    """Reject embedded function and unittest definitions independently of paths."""
    with _fresh_repository() as root:
        _run(root, "add", "packages/example", "python")
        source = root / "packages/example/main.py"
        source.write_text(misplaced)
        before = _snapshot(root)
        rejected = _run(root, "converge", code=1)
        _expect(
            "move test definitions to test_main.py" in rejected.stderr
            and _snapshot(root) == before,
            rejected,
        )


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("async def main(): pass\n", "main must be synchronous"),
        ("def main(value): pass\n", "main must accept a call without arguments"),
        ("def main(value, /): pass\n", "main must accept a call without arguments"),
        ("def main(*, value): pass\n", "main must accept a call without arguments"),
        ("main = 42\n", "main must be callable"),
        ("main = lambda value: None\n", "main must accept a call without arguments"),
        ("main: object = None\n", "main must be callable"),
        ("def main(): pass\nmain = []\n", "main must be callable"),
    ],
)
def test_convergence_rejects_invalid_entrypoints_before_repairs_and_cleanup(
    source: str,
    message: str,
) -> None:
    """Preserve files and Git state when an executable cannot call its entrypoint."""
    with _fresh_repository() as root:
        _run(root, "add", "packages/a", "python")
        _run(root, "add", "packages/z", "python")
        (root / "packages/a/default.nix").write_text(
            "{ ... }: (inputs.afairesi or inputs.self).lib.mkPythonPackage {}\n",
        )
        (root / "packages/z/main.py").write_text(source)
        (root / "unsupported").write_text("preserve until source errors are fixed")
        before = _snapshot(root)
        for arguments in (
            ("converge",),
            ("converge", "--dry-run"),
            ("converge", "--source", str(root)),
        ):
            result = _run(root, *arguments, code=1)
            _expect(message in result.stderr, result)
            _expect(_snapshot(root) == before, "invalid entrypoint changed state")


@pytest.mark.parametrize("invalid", ["def broken(:", "\xff"])
def test_convergence_rejects_invalid_test_sources_before_repairs(invalid: str) -> None:
    """Parse tests before repairing earlier packages or cleaning unsupported files."""
    with _fresh_repository() as root:
        _run(root, "add", "packages/a", "python")
        _run(root, "add", "packages/z", "python")
        (root / "packages/a/default.nix").write_text(
            "{ ... }: (inputs.afairesi or inputs.self).lib.mkPythonPackage {}\n",
        )
        (root / "packages/z/test_main.py").write_bytes(invalid.encode("latin-1"))
        before = _snapshot(root)
        result = _run(root, "converge", code=1)
        _expect("Python source could not be parsed" in result.stderr, result)
        _expect(_snapshot(root) == before, "invalid tests changed state")


def test_convergence_repairs_host_checks() -> None:
    """Repair generated host checks without rewriting host sources."""
    with _fresh_repository() as root:
        _check_host_repair(root)


@pytest.mark.parametrize(
    "source",
    [
        "def main(value=None, /, *, other=None): pass\n",
        "def main(*args, **kwargs): pass\n",
        "main = lambda: None\n",
        "main = lambda value=None: None\n",
        "def main(): pass\nmain: object\n",
        "main = 42\ndef main(): pass\n",
        "from example import main\n",
        "VALUE = 42\n",
        "",
    ],
)
def test_convergence_repairs_python_arguments_and_preserves_supported_entrypoints(
    source: str,
) -> None:
    """Repair missing constructor arguments while retaining library and CLI sources."""
    with _fresh_repository() as root:
        _run(root, "add", "packages/example", "python")
        package = root / "packages/example"
        (package / "main.py").write_text(source)
        definition = package / "default.nix"
        definition.write_text(
            "{ ... }: (inputs.afairesi or inputs.self).lib.mkPythonPackage {\n"
            '  meta.description = "Preserved";\n'
            '  passthru.custom = "Preserved too";\n'
            "}\n",
        )
        _preview(root, "converge", code=1)
        _run(root, "converge")
        evaluated = _evaluated_template(root, "example", _nix_environment(root))
        _expect(evaluated["meta"]["description"] == "Preserved", evaluated)
        _expect(evaluated["name"] == "example", evaluated)
        _expect(
            ("mainProgram" in evaluated["meta"])
            == (source not in {"", "VALUE = 42\n"}),
            evaluated,
        )
        _expect((package / "main.py").read_text() == source, source)
        _expect(
            'passthru.custom = "Preserved too"' in definition.read_text(),
            definition,
        )
        _preview(root, "converge")


def test_convergence_respects_excluded_trees() -> None:
    """Validate excluded trees once, independently of generated resource payloads."""
    with _fresh_repository() as root, pytest.MonkeyPatch.context() as patch:
        _check_excluded_trees(root, patch)


def test_coverage_checks_measure_subprocesses_without_changing_sources() -> None:
    """Build real checks, measure CLI lines, and keep reports outside the checkout."""
    with TemporaryDirectory(prefix="afairesi-coverage-") as directory:
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
        result = _run_runner_cli(root, environment, "coverage")
        _expect(not result.returncode, result)
        reports = [
            line
            for line in result.stdout.splitlines()
            if line.endswith("/html/index.html")
        ]
        expected_reports = 2
        _expect(len(reports) == expected_reports, result)
        for line in reports:
            report = Path(line.split(": ", 1)[1]).parents[1]
            files = json.loads((report / "coverage.json").read_text())["files"]
            _expect(
                len(files) == 1 and next(iter(files)).endswith("/main.py"),
                files,
            )
            _expect(
                set(next(iter(files.values()))["executed_lines"]) == {1, 2, 3, 4, 6},
                files,
            )
            measured = next(iter(files.values()))
            _expect(
                {tuple(branch) for branch in measured["executed_branches"]}
                == {(3, 4), (3, 6)}
                and measured["contexts"]["4"] == ["test_main.py::test_cli[alternate]"]
                and measured["contexts"]["6"] == ["test_main.py::test_cli[ready]"],
                measured,
            )
            audit = json.loads((report / "tests.json").read_text())
            rows = {row["nodeid"]: row for row in audit["tests"]}
            expected_counts = {"collected_cases": 5, "functions": 4, "skipped": 2}
            _expect(
                all(
                    audit["summary"][key] == value
                    for key, value in expected_counts.items()
                )
                and audit["summary"]["unexecuted_properties"]
                == ["test_main.py::test_generated_only"]
                and rows["test_main.py::test_generated_only"]["body_calls"] == 0
                and rows["test_main.py::test_explicit"]["body_calls"] == 1
                and rows["test_main.py::test_optional_browser"]["skip_reason"]
                == "optional browser"
                and all(row["duration"] >= 0 for row in audit["tests"]),
                audit,
            )
        _expect(
            _snapshot(root) == before and not (root / "tmp").exists(),
            "coverage changed repository state",
        )


def test_coverage_continues_after_failed_nix_builds() -> None:
    """Report each failure and still build later runnable packages."""
    with TemporaryDirectory(prefix="afairesi-coverage-failure-") as directory:
        root = Path(directory) / "source"
        environment = _prepare_coverage_flake(root, fail_build=True)
        untested = _make_runner_target(root, "", "", name="untested")
        (untested / "test_main.py").unlink()
        (root / "flake.nix").write_text(_git(root, "show", ":flake.nix"))
        before = _snapshot(root)
        result = _run_runner_cli(root, environment, "coverage")
        _expect(
            result.returncode == 1 and "1 passed, 1 failed, 1 skipped" in result.stdout,
            result,
        )
        _expect(
            "z-last:" in result.stdout
            and "/html/index.html" in result.stdout
            and "afairesi test coverage: example:" in result.stderr,
            result,
        )
        _expect(
            _snapshot(root) == before,
            "failed coverage build changed repository state",
        )


@pytest.mark.parametrize("missing", ["check", "html", "tests"])
def test_coverage_rejects_missing_checks_and_report_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    missing: str,
) -> None:
    """Validate coverage output guards without rebuilding a successful package."""
    subject = import_module("packages.afairesi.main")
    root = tmp_path / "source"
    root.mkdir()
    _repository(root)
    package = _make_source_package(root, "example", "def test_ready(): pass\n")
    check = root / "checks/example/default.nix"
    check.parent.mkdir(parents=True)
    if missing != "check":
        check.write_text("{}")
    report = tmp_path / "report"
    report.mkdir()
    if missing == "tests":
        (report / "html").mkdir()
        (report / "html/index.html").touch()
    original_run = subprocess.run
    builds: list[list[str]] = []

    def run(
        arguments: list[str],
        **keywords: Any,  # noqa: ANN401 - forward subprocess's keyword arguments
    ) -> subprocess.CompletedProcess[str]:
        if arguments[:2] == ["nix", "build"]:
            builds.append(arguments)
            return subprocess.CompletedProcess(arguments, 0, str(report) + "\n")
        return original_run(arguments, **keywords)

    monkeypatch.setattr(subject.subprocess, "run", run)
    before = _snapshot(root)
    message = {
        "check": f"missing {check}",
        "html": "no HTML report",
        "tests": "no test report",
    }[missing]
    monkeypatch.setattr(sys, "argv", ["afairesi", "test", "coverage", str(package)])
    with pytest.raises(SystemExit) as completed:
        subject.main()
    _expect(completed.value.code == 1 and message in capsys.readouterr().err, completed)
    _expect(len(builds) == (0 if missing == "check" else 1), builds)
    _expect(_snapshot(root) == before, "coverage validation modified source state")


def test_coverage_skips_untested_packages_and_continues_after_invalid_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Validate package eligibility and continuation separately from Nix builds."""
    subject = import_module("packages.afairesi.main")
    _repository(tmp_path)
    _make_source_package(tmp_path, "alpha", "def invalid(")
    untested = _make_source_package(tmp_path, "untested", "")
    (untested / "test_main.py").unlink()
    ready = _make_source_package(tmp_path, "z-last", "def test_ready(): pass\n")
    built: list[Path] = []
    monkeypatch.setattr(subject, "_build_package_coverage", built.append)
    before = _snapshot(tmp_path)
    monkeypatch.setattr(sys, "argv", ["afairesi", "test", "coverage", str(tmp_path)])
    with pytest.raises(SystemExit) as completed:
        subject.main()
    _expect(completed.value.code == 1, "invalid source must fail coverage")
    output = capsys.readouterr()
    _expect(built == [ready], built)
    _expect(
        "1 passed, 1 failed, 1 skipped" in output.out
        and "afairesi test coverage: alpha:" in output.err,
        output,
    )
    _expect(_snapshot(tmp_path) == before, "coverage eligibility modified sources")


def test_diff_does_not_follow_source_symlinks_and_reports_parse_errors(
    tmp_path: Path,
) -> None:
    """Do not execute sources or read link targets while comparing interface changes."""
    _repository(tmp_path)
    package = _make_source_package(tmp_path, "example", "def test_baseline(): pass\n")
    source = package / "main.py"
    source.write_text(
        "import argparse\np = argparse.ArgumentParser()\np.add_argument('--old')\n",
    )
    _git(tmp_path, "add", ".")
    source.unlink()
    source.symlink_to(tmp_path / "not-present")
    details = json.loads(_run(tmp_path, "diff", ".").stdout)
    _expect(
        _patch_values(details, "remove", "packages", "example", "arguments")
        == ["--old  optional"],
        details,
    )
    source.unlink()
    source.write_text("def invalid(")
    details = json.loads(_run(tmp_path, "diff", ".").stdout)
    _expect(
        bool(_patch_values(details, "add", "packages", "example", "diagnostics")),
        details,
    )


def test_diff_handles_a_tracked_symlink_replaced_with_a_regular_source(
    tmp_path: Path,
) -> None:
    """Discover a working interface that replaces an excluded index symlink."""
    _repository(tmp_path)
    package = _make_source_package(tmp_path, "example", "def test_result(): pass\n")
    source = package / "main.py"
    source.unlink()
    source.symlink_to(tmp_path / "not-present")
    _git(tmp_path, "add", ".")
    source.unlink()
    source.write_text(
        "import argparse\np = argparse.ArgumentParser()\np.add_argument('--new')\n",
    )
    data = json.loads(_run(tmp_path, "diff", ".").stdout)
    _expect(
        _patch_values(data, "add", "packages", "example", "arguments")
        == ["--new  optional"],
        data,
    )


def test_diff_handles_unborn_head_removed_packages_and_semantically_equal_edits(
    tmp_path: Path,
) -> None:
    """Support Git's empty initial baseline and semantic additions and removals."""
    _repository(tmp_path)
    package = _make_source_package(tmp_path, "example", "def test_result(): pass\n")
    _git(tmp_path, "add", ".")
    staged = json.loads(_run(tmp_path, "diff", ".", "--cached").stdout)
    _expect(
        _patch_values(staged, "add", "packages", "example") == [{"tests": ["result"]}],
        staged,
    )
    (package / "test_main.py").write_text(
        "# comment\ndef test_result():\n    raise RuntimeError('new body')\n",
    )
    _expect(json.loads(_run(tmp_path, "diff", ".").stdout) == [], package)
    for path in package.iterdir():
        path.unlink()
    removed = json.loads(_run(tmp_path, "diff", ".").stdout)
    _expect(
        _patch_values(removed, "remove", "packages", "example")
        == [{"tests": ["result"]}],
        removed,
    )


def test_diff_ignores_reordered_tests_and_named_options(
    tmp_path: Path,
) -> None:
    """Keep implementation observations and declaration ordering out of the diff."""
    _repository(tmp_path)
    package = _make_source_package(
        tmp_path,
        "example",
        "def test_first(): pass  # comment\ndef test_second(): pass\n",
    )
    source = package / "main.py"
    source.write_text(
        "import argparse\np = argparse.ArgumentParser()\n"
        "p.add_argument('--verbose')\np.add_argument('--jobs', default=2)\n",
    )
    _git(tmp_path, "add", ".")
    _fixture_git(tmp_path, "commit", "-qm", "Baseline")
    source.write_text(
        "import argparse\np = argparse.ArgumentParser()\n"
        "p.add_argument('--jobs', default=2)\np.add_argument('--verbose')\n",
    )
    (package / "test_main.py").write_text(
        "def test_second(): pass\ndef test_first():\n    assert True\n",
    )
    before = _snapshot(tmp_path)
    working = json.loads(_run(tmp_path, "diff", ".").stdout)
    _expect(working == [], working)
    _expect(_snapshot(tmp_path) == before, "semantic inspection changed source state")
    _git(tmp_path, "add", ".")
    staged = json.loads(_run(tmp_path, "diff", ".", "--cached").stdout)
    _expect(staged == [], staged)


def test_diff_preserves_positional_argument_order(
    tmp_path: Path,
) -> None:
    """Report changed positional meaning while omitting unchanged named options."""
    _repository(tmp_path)
    package = _make_source_package(tmp_path, "example", "")
    source = package / "main.py"
    prefix = "import argparse\np = argparse.ArgumentParser()\n"
    source.write_text(
        prefix + "p.add_argument('source')\np.add_argument('--verbose')\n"
        "p.add_argument('destination')\n",
    )
    _git(tmp_path, "add", ".")
    source.write_text(
        prefix + "p.add_argument('--verbose')\np.add_argument('destination')\n"
        "p.add_argument('source')\n",
    )
    data = json.loads(_run(tmp_path, "diff", ".").stdout)
    document = {
        str(tmp_path): {
            "packages": {
                "example": {
                    "arguments": [
                        "source  required",
                        "destination  required",
                        "--verbose  optional",
                    ],
                },
            },
            "hosts": {},
        },
    }
    patched = jsonpatch.apply_patch(document, data)
    _expect(
        patched[str(tmp_path)]["packages"]["example"]["arguments"]
        == ["destination  required", "source  required", "--verbose  optional"],
        data,
    )
    _expect(
        all(operation.get("value") != "--verbose  optional" for operation in data),
        data,
    )


@pytest.mark.parametrize("field", ["tests", "dependencies", "arguments"])
@pytest.mark.parametrize("operation", ["added", "removed"])
def test_diff_reports_list_membership_changes_and_preserves_duplicate_counts(
    tmp_path: Path,
    field: str,
    operation: str,
) -> None:
    """Distinguish one added or removed occurrence from a reordered retained entry."""
    _repository(tmp_path)
    package = _make_source_package(tmp_path, "example", "")
    parser_source = "import argparse\np = argparse.ArgumentParser()\n"
    dependency_source = (
        "{ inputs, system, ... }: let local = inputs.self.packages.${system}; in "
    )
    filename, before, after, entries = {
        "tests": (
            "test_main.py",
            (
                "def test_shared(): pass\n"
                "class TestFirst:\n    def test_repeated(self): pass\n"
            ),
            (
                "class TestFirst:\n    def test_repeated(self): pass\n"
                "def test_shared(): pass\n"
                "class TestSecond:\n    def test_repeated(self): pass\n"
                "def test_new(): pass\n"
            ),
            ["new", "repeated"],
        ),
        "arguments": (
            "main.py",
            parser_source
            + "p.add_argument('--shared')\np.add_argument('--repeated')\n",
            parser_source + "p.add_argument('--repeated')\np.add_argument('--shared')\n"
            "p.add_argument('--new')\n",
            ["--new  optional"],
        ),
        "dependencies": (
            "default.nix",
            dependency_source
            + "{ propagatedBuildInputs = [ local.shared local.repeated ]; }",
            dependency_source
            + "{ propagatedBuildInputs = [ local.repeated local.shared local.new ]; }",
            ["runtime: packages/new"],
        ),
    }[field]
    if operation == "removed":
        before, after = after, before
    source = package / filename
    source.write_text(before)
    _git(tmp_path, "add", ".")
    source.write_text(after)
    data = json.loads(_run(tmp_path, "diff", ".").stdout)
    action = "add" if operation == "added" else "remove"
    _expect(
        sorted(_patch_values(data, action, "packages", "example", field))
        == sorted(entries),
        data,
    )
    _expect(
        _patch_values(
            data,
            "remove" if action == "add" else "add",
            "packages",
            "example",
            field,
        )
        == [],
        data,
    )


def test_diff_reports_only_changed_list_entries(
    tmp_path: Path,
) -> None:
    """Show changed contracts without repeating retained declarations."""
    _repository(tmp_path)
    package = _make_source_package(
        tmp_path,
        "example",
        "def test_kept(): pass\ndef test_old(): pass\n",
    )
    definition = package / "default.nix"
    definition.write_text(
        "{ inputs, system, ... }: let local = inputs.self.packages.${system}; in "
        '{ meta.description = "Before"; '
        "propagatedBuildInputs = [ local.kept local.old ]; }",
    )
    source = package / "main.py"
    source.write_text(
        "import argparse\np = argparse.ArgumentParser()\n"
        "p.add_argument('--kept')\np.add_argument('--jobs', default=2)\n",
    )
    _git(tmp_path, "add", ".")
    definition.write_text(
        definition.read_text().replace("Before", "After").replace(".old", ".new"),
    )
    source.write_text(source.read_text().replace("default=2", "default=3"))
    (package / "test_main.py").write_text(
        "def test_new(): pass\ndef test_kept(): pass\n",
    )
    data = json.loads(_run(tmp_path, "diff", ".").stdout)
    for field, old, new in (
        ("arguments", "--jobs  optional; default=2", "--jobs  optional; default=3"),
        ("dependencies", "runtime: packages/old", "runtime: packages/new"),
        ("description", "Before", "After"),
        ("tests", "old", "new"),
    ):
        _expect(
            _patch_values(data, "remove", "packages", "example", field) == [old],
            data,
        )
        _expect(_patch_values(data, "add", "packages", "example", field) == [new], data)
    rendered = subprocess.run(
        ["jd", "-t", "patch2jd"],  # noqa: S607
        input=json.dumps(data),
        capture_output=True,
        text=True,
        check=True,
    )
    _expect(
        '- "Before"' in rendered.stdout
        and '+ "After"' in rendered.stdout
        and "kept" not in rendered.stdout,
        rendered,
    )


def test_diff_reports_unresolved_index_conflicts_without_modifying_state(
    tmp_path: Path,
) -> None:
    """Reject ambiguous staged sources instead of silently choosing a conflict side."""
    _repository(tmp_path)
    package = _make_source_package(tmp_path, "example", "def test_result(): pass\n")
    _git(tmp_path, "add", ".")
    source = package / "test_main.py"
    oid = _git(tmp_path, "hash-object", str(source)).strip()
    relative = source.relative_to(tmp_path).as_posix()
    subprocess.run(
        ["git", "update-index", "--index-info"],  # noqa: S607
        cwd=tmp_path,
        input=(
            f"0 {'0' * len(oid)}\t{relative}\n"
            f"100644 {oid} 1\t{relative}\n"
            f"100644 {oid} 2\t{relative}\n"
        ),
        text=True,
        check=True,
        capture_output=True,
    )
    before = _snapshot(tmp_path)
    result = _run(tmp_path, "diff", ".", code=1)
    _expect(json.loads(result.stdout) == [], result)
    _expect("unresolved index conflict" in result.stderr, result)
    _expect(_snapshot(tmp_path) == before, "diff changed conflicted index state")


def test_diff_separates_working_index_and_head_without_modifying_state(
    tmp_path: Path,
) -> None:
    """Compare staged and unstaged semantic facts while ignoring untracked sources."""
    _repository(tmp_path)
    package = _make_source_package(tmp_path, "example", "def test_baseline(): pass\n")
    (package / "default.nix").write_text('{ meta.description = "Baseline"; }')
    _git(tmp_path, "add", ".")
    _fixture_git(tmp_path, "commit", "-qm", "Baseline")
    (package / "test_main.py").write_text("def test_staged(): pass\n")
    _git(tmp_path, "add", ".")
    (package / "test_main.py").write_text("def test_working(): pass\n")
    _make_source_package(tmp_path, "untracked", "def test_ignored(): pass\n")
    before = _snapshot(tmp_path)
    working = json.loads(_run(tmp_path, "diff", ".").stdout)
    staged = json.loads(_run(tmp_path, "diff", ".", "--cached").stdout)
    for data, old, new in (
        (working, "staged", "working"),
        (staged, "baseline", "staged"),
    ):
        _expect(
            _patch_values(data, "remove", "packages", "example", "tests") == [old],
            data,
        )
        _expect(
            _patch_values(data, "add", "packages", "example", "tests") == [new],
            data,
        )
        _expect("untracked" not in json.dumps(data), data)
    _expect(json.loads(_run(package, "diff", ".").stdout) == working, working)
    _expect(
        _run(tmp_path, "diff", ".", "--staged").stdout
        == _run(tmp_path, "diff", ".", "--cached").stdout,
        staged,
    )
    _expect(_snapshot(tmp_path) == before, "diff modified repository or working files")


def test_discovery_handles_nested_interfaces_and_malformed_sources() -> None:
    """Validate nested declarations and malformed inputs once per suite."""
    with _fresh_repository() as root:
        sentinel = (
            "from pathlib import Path\nPath('SENTINEL').touch()\n"
            "raise RuntimeError('must not execute')\n"
        )
        package = _make_source_package(
            root,
            "example",
            sentinel + "def test_result(): pass\n",
        )
        for nested, path, parameter in NESTED_CONTRACTS:
            (package / "main.py").write_text(sentinel + nested)
            before = _snapshot(root)
            data = import_module("packages.afairesi.main").resource_data(package)
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
        for unsupported in UNSUPPORTED_INTERFACES:
            (package / "main.py").write_text(unsupported)
            before = _snapshot(root)
            rejected = _run(package, ".")
            _expect("unsupported CLI interface" in rejected.stdout, rejected)
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
            inspected = _run(package, ".")
            _expect(
                "tests"
                in _overview_resources(inspected.stdout)["packages"]["example"].get(
                    "diagnostics",
                    {},
                )
                if layout in {"syntax", "encoding"}
                else "tests"
                not in _overview_resources(inspected.stdout)["packages"]["example"],
                inspected,
            )
            _expect(_snapshot(root) == before, "malformed test source changed state")


def test_explicit_targets_preserve_repository_scope(
    tmp_path: Path,
) -> None:
    """Discover nested repositories and preserve explicit target scope."""
    home = tmp_path / "home"
    home.mkdir()
    _git(home, "init", "--quiet")
    (home / ".gitignore").write_text("*\n!/.gitmodules\n")
    (home / ".gitmodules").touch()
    root = home / "forge.example/team/demo"
    root.mkdir(parents=True)
    _repository(root)
    package = _make_source_package(root, "alpha", "def test_alpha(): pass\n")
    _make_source_package(root, "beta", "def test_beta(): pass\n")
    nested = package / "prm/nested"
    nested.mkdir(parents=True)
    catalog = _run(root, str(root)).stdout
    _expect(
        _overview_resources(catalog)
        == {"packages": {"alpha": {"tests": ["alpha"]}, "beta": {"tests": ["beta"]}}},
        catalog,
    )
    for cwd in (package, nested):
        _expect(_run(cwd, str(root)).stdout == catalog, cwd)
    _run(root, "--json", code=2)
    focused = _overview(package)
    _run(root, "overview", code=2)
    _run(root, "unknown-command", code=2)
    _expect(
        focused == {"packages": {"alpha": {"tests": ["alpha"]}}},
        focused,
    )
    _expect(
        _overview_resources(_run(package, ".").stdout)["packages"]["alpha"]
        == {"tests": ["alpha"]},
        package,
    )
    _run(nested, ".")
    _run(home, str(home))
    (home / ".gitmodules").write_text(
        '[submodule "demo"]\npath = forge.example/team/demo\n',
    )
    repository_tree = json.loads(catalog)[socket.gethostname()][getpass.getuser()]
    expected = {
        socket.gethostname(): {
            str(home): {
                ".gitmodules": None,
                **{
                    domain + "/": {
                        owner + "/": {
                            name + "/": facts for name, facts in repositories.items()
                        }
                        for owner, repositories in owners.items()
                    }
                    for domain, owners in repository_tree.items()
                },
            },
        },
    }
    _expect(_overview_tree(home) == expected, home)
    _run(nested, ".")
    _expect(_run(tmp_path, str(root)).stdout == catalog, tmp_path)
    for path in (
        ("test",),
        ("test", "coverage"),
        ("test", "hypothesis"),
        ("test", "mutation"),
    ):
        rejected = _run(tmp_path, *path, code=1)
        _expect("not inside a Git repository" in rejected.stderr, rejected)
    ordinary = tmp_path / "ordinary"
    ordinary.mkdir()
    _git(ordinary, "init", "--quiet")
    report = json.loads(_run(ordinary, ".").stdout)
    _expect(
        str(ordinary) in report[socket.gethostname()]["migration"]["repositories"],
        report,
    )


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
def test_generated_templates_evaluate_metadata_preserve_scopes_and_install_assets(  # noqa: PLR0915
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
        elif kind in {"html", "latex"}:
            binding = "runtimeInputs" if kind == "html" else "nativeBuildInputs"
            expression = (
                f"(let {binding} = [ pkgs.inner ]; in [ pkgs.git ]) ++ [ pkgs.curl ]"
            )
            dependency = "pkgs.http-server" if kind == "html" else "pkgs.texliveFull"
            source = (
                source.replace(
                    f"{binding} = [ {dependency} ]",
                    f"{binding} = {expression} ++ [ {dependency} ]",
                )
                .replace(
                    "let\n",
                    f"let\n  # {binding} = [ pkgs.fromComment ];\n"
                    f"  ignored = let {binding} = [ pkgs.fromNested ]; in null;\n",
                    1,
                )
                .replace(
                    "  meta.description =",
                    '  passthru.custom = "preserved";\n  meta.description =',
                )
            )
            if kind == "html":
                resource = root / "prm/source.bin"
                resource.parent.mkdir()
                resource.write_bytes(b"copied resource")
                source = source.replace(
                    'pkgs.runCommand "${pname}-site" { }',
                    (
                        'pkgs.runCommand "${pname}-site" { postInstall = '
                        "let postInstall = \"inner\"; in ''cp "
                        "${../../prm/source.bin} \"$out/prm/copied.bin\"''; }"
                    ),
                )
            else:
                source = source.replace(
                    f"{binding} = {expression} ++ [ {dependency} ];",
                    f"inherit {binding};",
                ).replace(
                    "let\n",
                    f"let\n  {binding} = {expression} ++ [ {dependency} ];\n",
                    1,
                )
        definition.write_text(source)
        _run(root, "converge")
        _preview(root, "converge")
        environment = _nix_environment(root)
        actual = _evaluated_template(root, name, environment)
        _expect(
            kind not in {"html", "latex"}
            or 'passthru.custom = "preserved";' in definition.read_text(),
            "convergence lost custom package attributes",
        )
        _expect(actual["name"] == name.replace("-", "_"), actual)
        _expect(actual["meta"]["description"] == description, actual)
        details = _overview(root)["packages"][name]
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
                    "arguments"
                    not in _overview_resources(_run(package, ".").stdout)["packages"][
                        name
                    ],
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
                    "arguments"
                    not in _overview_resources(_run(package, ".").stdout)["packages"][
                        name
                    ],
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
            TemporaryDirectory(prefix="afairesi-home-") as directory,
            pytest.MonkeyPatch.context() as patch,
        ):
            _check_remote_initialization(Path(directory), patch, remote)
    with (
        TemporaryDirectory(prefix="afairesi-bootstrap-") as directory,
        pytest.MonkeyPatch.context() as patch,
    ):
        _check_flake_initialization(Path(directory), patch)
    with TemporaryDirectory(prefix="afairesi-home-") as directory:
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


def test_home_overview_without_repositories_lists_whitelist(tmp_path: Path) -> None:
    """Inspect an empty home without requiring any checked-out flakes."""
    (tmp_path / ".gitmodules").touch()
    (tmp_path / ".gitignore").write_text("*\n# policy\n!/.ssh/\n!/.ssh/key\n")
    subject = import_module("packages.afairesi.main")
    _expect(
        subject.overview_summary(tmp_path)
        == {
            socket.gethostname(): {
                str(tmp_path): {".ssh/": ["key"]},
            },
        },
        tmp_path,
    )


def test_home_policy_diagnostics_preserve_git_and_whitelist_state(
    tmp_path: Path,
) -> None:
    """Report missing paths and ignored tracked files without changing home policy."""
    subject = import_module("packages.afairesi.main")
    _git(tmp_path, "init", "--quiet")
    (tmp_path / ".gitmodules").touch()
    ignore = tmp_path / ".gitignore"
    ignore.write_text(
        "*\n!/.gitignore\n!/.gitmodules\n!/allowed\n!/future\n"
        "!/assets/\n!/assets/*.png\n!/broken-link\n",
    )
    (tmp_path / "allowed").write_text("private contents")
    (tmp_path / "excluded file").write_text("private contents")
    (tmp_path / "assets").mkdir()
    (tmp_path / "broken-link").symlink_to(tmp_path / "missing-target")
    _git(tmp_path, "add", "--force", ".gitignore", ".gitmodules", "excluded file")
    before = ignore.read_text(), _git(tmp_path, "ls-files", "--stage")
    expected = {
        "missing_whitelist_paths": ["future"],
        "tracked_outside_whitelist": ["excluded file"],
    }
    tree = subject.overview_summary(tmp_path)[socket.gethostname()][str(tmp_path)]
    _expect(tree["diagnostics"] == expected, tree)
    _expect("private contents" not in json.dumps(tree), tree)
    _expect(
        before == (ignore.read_text(), _git(tmp_path, "ls-files", "--stage")),
        before,
    )
    ignore.write_text(ignore.read_text() + "!/excluded file\n")
    (tmp_path / "future").touch()
    _expect("diagnostics" not in subject.home_preservation(tmp_path), tmp_path)


@pytest.mark.parametrize("case", ["unknown", "parent", "dirty", "dirty-preview"])
def test_home_submodule_removal_rejects_invalid_or_dirty_targets(
    tmp_path: Path,
    case: str,
) -> None:
    """Reject ambiguous paths and local changes without changing home state."""
    root = _home_repository(tmp_path)
    relative = "forge.example/owner/demo"
    _git(root, "submodule", "absorbgitdirs")
    _fixture_git(root, "commit", "-qm", "Home snapshot")
    target = {"unknown": "missing", "parent": "forge.example/owner"}.get(
        case,
        relative,
    )
    if case.startswith("dirty"):
        (root / relative / "README").write_text("local changes")
    before = _snapshot(root)
    arguments = ("--dry-run",) if case == "dirty-preview" else ()
    _run(root, "rm", target, *arguments, code=1)
    _expect(_snapshot(root) == before, "rejected removal changed home state")


def test_home_submodule_removal_stages_metadata_and_preserves_shared_paths(
    tmp_path: Path,
) -> None:
    """Preview and remove a repository together with its owned whitelist entries."""
    root = _home_repository(tmp_path)
    relative = "forge.example/owner/demo"
    _git(root, "submodule", "absorbgitdirs")
    ignore = root / ".gitignore"
    ignore.write_text(
        ignore.read_text()
        + f"!/{relative}/\n!/{relative}/README\n"
        + "!/forge.example/owner/demo-other\n!/.ssh/\n",
    )
    _git(root, "add", ".gitignore")
    _fixture_git(root, "commit", "-qm", "Home snapshot")
    before = _snapshot(root)
    preview = _run(root, "rm", relative, "--dry-run")
    _expect(relative in preview.stdout and ".gitignore" in preview.stdout, preview)
    _expect(_snapshot(root) == before, "removal preview changed home state")
    _run(root, "rm", relative)
    _expect(not (root / relative).exists(), "submodule checkout was not removed")
    lines = ignore.read_text().splitlines()
    _expect(f"!/{relative}" not in lines and f"!/{relative}/README" not in lines, lines)
    for entry in (
        "!/forge.example/",
        "!/forge.example/owner/",
        "!/forge.example/owner/demo-other",
        "!/.ssh/",
    ):
        _expect(entry in lines, lines)
    subject = import_module("packages.afairesi.main")
    _expect(subject.home_submodules(root) == [], "submodule remains registered")
    staged = _git(root, "diff", "--cached", "--name-only").splitlines()
    _expect(set(staged) == {".gitignore", ".gitmodules", relative}, staged)
    _expect(not _git(root, "diff", "--name-only"), "metadata was not staged")
    _run(root, "converge", "--dry-run")


def test_home_submodule_whitelist_move_preserves_unrelated_entries(
    tmp_path: Path,
) -> None:
    """Preview moves safely and migrate subtree rules without touching sibling paths."""
    subject = import_module("packages.afairesi.main")
    _git(tmp_path, "init", "--quiet")
    ignore = tmp_path / ".gitignore"
    source = (
        "*\n!/.gitignore\n!/forge.example/\n!/forge.example/team/\n"
        "!/forge.example/team/old\n!/forge.example/team/old/prm/\n"
        "!/forge.example/team/older\n!/forge.example/team/new\n!/future\n"
    )
    ignore.write_text(source)
    _git(tmp_path, "add", ".gitignore")
    old = Path("forge.example/team/old")
    new = Path("forge.example/team/new")
    _expect(
        subject._move_home_whitelist(tmp_path, old, new, dry_run=True),  # noqa: SLF001
        tmp_path,
    )
    _expect(ignore.read_text() == source, ignore)
    _expect(_git(tmp_path, "show", ":.gitignore") == source, ignore)
    subject._move_home_whitelist(tmp_path, old, new, dry_run=False)  # noqa: SLF001
    expected = (
        "*\n!/.gitignore\n!/forge.example/\n!/forge.example/team/\n"
        "!/forge.example/team/older\n!/forge.example/team/new\n!/future\n"
        "!/forge.example/team/new/prm/\n"
    )
    _expect(ignore.read_text() == expected, ignore.read_text())
    _expect(_git(tmp_path, "show", ":.gitignore") == expected, ignore)
    _expect(
        not subject._move_home_whitelist(tmp_path, old, new, dry_run=True),  # noqa: SLF001
        ignore,
    )


def test_home_whitelist_merges_paths_and_rejects_escaping_entries(
    tmp_path: Path,
) -> None:
    """Merge shared parents without scanning or reading whitelisted files."""
    subject = import_module("packages.afairesi.main")
    ignore = tmp_path / ".gitignore"
    ignore.write_text(
        "*\n# comment\n!/.ssh\n!/.ssh/key\n!/.ssh/\n!/.ssh/public\n"
        "!/assets/*.png\n!/empty/\n",
    )
    _expect(
        subject.home_preservation(tmp_path)
        == {
            ".ssh/": ["key", "public"],
            "assets/": ["*.png"],
            "empty/": {},
        },
        tmp_path,
    )
    ignore.write_text("*\n!/../outside\n")
    with pytest.raises(subject.CommandError, match="invalid whitelist path"):
        subject.home_preservation(tmp_path)


@pytest.mark.parametrize("failure", ["counterexample", "timeout"])
def test_hypothesis_campaigns_continue_after_failures(failure: str) -> None:
    """Retain counterexamples and terminate stalled descendants before continuing."""
    with TemporaryDirectory(prefix="afairesi-failed-campaign-") as directory:
        root = Path(directory) / "source with spaces"
        environment = _prepare_runner_flake(
            root,
            "def test_ready(): pass\n",
            name="z-last",
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
            "1",
            "--timeout",
            "20" if failure == "counterexample" else "10",
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
@given(max_examples=st.integers(min_value=1, max_value=5))
@example(max_examples=5)
def test_hypothesis_campaigns_generate_cases_in_isolated_sources(
    max_examples: int,
) -> None:
    """Count generated cases and isolate copied Git commands and runtime state."""
    with TemporaryDirectory(prefix="afairesi-campaign-") as directory:
        root = Path(directory) / "source with spaces"
        source = (
            "import json, os\nfrom pathlib import Path\n"
            "def main():\n"
            "    assert not (Path.home() / 'sentinel').exists()\n"
            "    Path('cli-environment.json').write_text(json.dumps("
            "{name: os.environ[name] for name in "
            "('HOME', 'XDG_CACHE_HOME', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME', "
            "'XDG_STATE_HOME', 'XDG_RUNTIME_DIR', 'TMPDIR')}))\n"
            "    print('ready')\n"
        )
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
            "    repository = subprocess.run(['git', 'rev-parse', "
            "'--show-toplevel'], capture_output=True, text=True)\n"
            "    assert repository.returncode != 0\n"
            "    result = subprocess.run(['git', 'example'], "
            "capture_output=True, text=True)\n"
            "    assert result.returncode == 0 and result.stdout == "
            "'ready\\n'\n"
            "    import json\n"
            "    directories = json.loads(Path('cli-environment.json').read_text())\n"
            "    for name, directory in directories.items():\n"
            "        assert directory == os.environ[name]\n"
            "        assert Path(directory).is_relative_to(Path.cwd())\n"
            "    assert not (Path.home() / 'sentinel').exists()\n"
        )
        environment = _prepare_runner_flake(root, tests, source, name="git-example")
        caller_home = Path(directory) / "caller home"
        caller_home.mkdir()
        sentinel = caller_home / "sentinel"
        sentinel.write_text("preserve")
        for variable in (
            "HOME",
            "XDG_CACHE_HOME",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "XDG_STATE_HOME",
            "XDG_RUNTIME_DIR",
            "TMPDIR",
        ):
            environment[variable] = str(caller_home)
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
        workspaces = list((root / "tmp").glob("python-hypothesis-git-example-*"))
        _expect(len(workspaces) == 1, workspaces)
        expected_case_count = 2
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
            report = json.loads((workspace / "tests.json").read_text())
            rows = {row["nodeid"]: row for row in report["tests"]}
            _expect(
                report["summary"]["selected_cases"] == expected_case_count
                and rows["test_main.py::test_property"]["body_calls"]
                == max_examples + 1
                and rows["test_main.py::test_cli"]["body_calls"] == 1
                and not report["summary"]["unexecuted_properties"]
                and not list(workspace.glob("test-runtime-*")),
                report,
            )
        _expect(
            sentinel.read_text() == "preserve",
            caller_home,
        )


def test_inspection_distinguishes_convention_gaps_from_registration(
    tmp_path: Path,
) -> None:
    """Assess flake layout independently of registration and diagnose path drift."""
    subject = import_module("packages.afairesi.main")
    home = tmp_path / "home"
    home.mkdir()
    _home_repository(home)
    checkout = home / "forge.example/owner/demo"
    flake = home / "flake"
    flake.mkdir()
    _repository(flake)
    (flake / ".gitignore").write_text(
        subject.render_gitignore(subject.allowed_paths(flake, []), set()),
    )
    environment = {**os.environ, "HOME": str(home)}
    before = _snapshot(flake)
    report = json.loads(
        _run(tmp_path, str(flake), environment=environment).stdout,
    )[socket.gethostname()]["migration"]
    _expect(
        report["repositories"][str(flake)]
        == {
            "layout": "flake",
            "registered": False,
            "diagnostics": [],
        },
        report,
    )
    _expect(_snapshot(flake) == before, "assessment mutated flake state")
    (flake / "README").unlink()
    report = json.loads(
        _run(tmp_path, str(flake), environment=environment).stdout,
    )[socket.gethostname()]["migration"]
    _expect("README" in str(report["repositories"][str(flake)]["diagnostics"]), report)
    _git(
        home,
        "config",
        "-f",
        ".gitmodules",
        "submodule.forge.example/owner/demo.url",
        "git@forge.example:owner/other",
    )
    report = json.loads(
        _run(tmp_path, str(checkout), environment=environment).stdout,
    )[socket.gethostname()]["migration"]
    _expect(
        "submodule path: expected"
        in str(report["repositories"][str(checkout)]["diagnostics"]),
        report,
    )


def test_inspection_reports_worktrees_and_registration_without_mutation(
    tmp_path: Path,
) -> None:
    """Discover nested worktrees, skip symlinks, and retain existing Git state."""
    home = tmp_path / "home"
    home.mkdir()
    _home_repository(home)
    checkout = home / "forge.example/owner/demo"
    ordinary = home / "ordinary"
    ordinary.mkdir()
    _git(ordinary, "init", "--quiet")
    _fixture_git(ordinary, "commit", "--quiet", "--allow-empty", "-m", "fixture")
    (ordinary / "untracked").write_text("keep")
    worktree = home / "worktree"
    _git(ordinary, "worktree", "add", "--detach", str(worktree), "HEAD")
    _git(home, "submodule", "absorbgitdirs")
    outside = tmp_path / "outside"
    outside.mkdir()
    _git(outside, "init", "--quiet")
    (home / "link").symlink_to(outside, target_is_directory=True)
    broken = home / "broken"
    broken.mkdir()
    (broken / ".git").write_text("gitdir: missing\n")
    before = {
        str(path): _snapshot(path) for path in (home, checkout, ordinary, worktree)
    }
    result = _run(
        tmp_path,
        str(home),
        environment={**os.environ, "HOME": str(home)},
    )
    report = json.loads(result.stdout)[socket.gethostname()]["migration"]
    repositories = report["repositories"]
    _expect(
        set(repositories) == {str(home), str(checkout), str(ordinary), str(worktree)},
        report,
    )
    _expect(repositories[str(home)]["layout"] == "home", report)
    _expect(repositories[str(checkout)]["registered"], report)
    _expect(not repositories[str(worktree)]["registered"], report)
    _expect(repositories[str(ordinary)]["diagnostics"], report)
    _expect(any(str(broken) in issue for issue in report["diagnostics"]), report)
    parent = json.loads(
        _run(
            tmp_path,
            str(tmp_path),
            environment={**os.environ, "HOME": str(home)},
        ).stdout,
    )[socket.gethostname()]["migration"]
    _expect(set(parent["repositories"]) == {*repositories, str(outside)}, parent)
    _expect(
        before
        == {
            str(path): _snapshot(path) for path in (home, checkout, ordinary, worktree)
        },
        "discovery mutated repositories",
    )
    _run(tmp_path, str(tmp_path / "missing"), code=2)


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ({}, {"new": {"value": None}}),
        ({"removed": {"value": None}}, {}),
        ({"value": None}, {"value": "replacement"}),
        ({"values": ["a", "b"]}, {"values": ["b", "a"]}),
        ({"values": ["a", "a", "b"]}, {"values": ["a", "b", "c"]}),
        ({"old": "moved"}, {"new": "moved"}),
        ({"a/b~": {"key": "old"}}, {"a/b~": {"key": "new"}}),
    ],
)
def test_json_diff_patch_applies_and_renders_with_jd(
    tmp_path: Path,
    before: dict[str, Any],
    after: dict[str, Any],
) -> None:
    """Check standard patch application and jd rendering across JSON edge cases."""
    subject = import_module("packages.afairesi.main")
    original = json.dumps(before, sort_keys=True)
    patch = subject._json_diff_patch(before, after)  # noqa: SLF001 - renderer contract
    _expect(jsonpatch.apply_patch(before, patch) == after, patch)
    _expect(json.dumps(before, sort_keys=True) == original, before)
    _expect(
        {operation["op"] for operation in patch} <= {"test", "remove", "add"},
        patch,
    )
    patch_file = tmp_path / "patch.json"
    patch_file.write_text(json.dumps(patch))
    rendered = _run(tmp_path, "-t", "patch2jd", str(patch_file), executable="jd")
    _expect("@ [" in rendered.stdout, rendered)


def test_machine_diff_uses_independent_repository_baselines_and_reports_exclusions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Compare home policy and child sources without reading OS or system storage."""
    home = _home_repository(tmp_path)
    child = home / "forge.example/owner/demo"
    package = _make_source_package(child, "example", "def test_baseline(): pass\n")
    _git(child, "add", ".")
    _fixture_git(child, "commit", "-qm", "Child baseline")
    _fixture_git(home, "commit", "-qm", "Home baseline")
    (package / "test_main.py").write_text("def test_working(): pass\n")
    ignore = home / ".gitignore"
    ignore.write_text(ignore.read_text() + "!/.ssh/\n!/.ssh/key\n")
    subject = import_module("packages.afairesi.main")
    monkeypatch.setattr(Path, "home", lambda: home)
    before = (_snapshot(home), _snapshot(child))
    data = subject.diff_summary(None)
    _expect(
        _patch_values(data["patch"], "add", "paths", ".ssh/") == ["key"],
        data,
    )
    _expect(
        _patch_values(data["patch"], "remove", "packages", "example", "tests")
        == ["baseline"],
        data,
    )
    _expect(
        _patch_values(data["patch"], "add", "packages", "example", "tests")
        == ["working"],
        data,
    )
    _expect((_snapshot(home), _snapshot(child)) == before, "machine diff changed state")
    _git(home, "add", ".gitignore")
    changed = subject.diff_summary(None)
    _expect(
        {JsonPointer(op["path"]).parts[0] for op in changed["patch"]} == {str(child)},
        changed,
    )
    _git(
        home,
        "config",
        "-f",
        ".gitmodules",
        "submodule.missing.path",
        "forge.example/owner/missing",
    )
    missing = subject.diff_summary(None)
    _expect(
        bool(missing["diagnostics"]),
        missing,
    )
    monkeypatch.setattr(sys, "argv", ["afairesi", "diff"])
    with pytest.raises(SystemExit) as exit_status:
        subject.main()
    output = capsys.readouterr()
    _expect(exit_status.value.code == 1, exit_status)
    _expect(isinstance(json.loads(output.out), list), output)
    _expect("no Git baseline" in output.err and "error:" in output.err, output)


def test_machine_home_whitelist_replaces_stored_home_contents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Represent a stored home once through its policy even without submodules."""
    subject = import_module("packages.afairesi.main")
    (tmp_path / ".gitignore").write_text("*\n!/.ssh/\n")
    home_tree: dict[str, Any] | list[str] = ["untracked-file"]
    for component in reversed(tmp_path.parts[2:]):
        home_tree = {component + "/": home_tree}
    stored_tree = {
        "/" + tmp_path.parts[1] + "/": home_tree,
        "/etc/": ["machine-id"],
    }
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(subject, "persistent_summary", lambda _root: stored_tree)
    monkeypatch.setattr(
        subject,
        "system_summary",
        lambda: {
            "os": {"id": "nixos"},
            "preservation": {"status": "available"},
        },
    )
    machine = subject.machine_summary()[socket.gethostname()]["filesystem"]
    branch = machine["/" + tmp_path.parts[1] + "/"]
    for component in tmp_path.parts[2:]:
        branch = branch[component + "/"]
    _expect(branch == {".ssh/": {}}, branch)
    _expect(machine["/etc/"] == ["machine-id"], machine)


def test_machine_overview_defaults_ignore_working_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Show storage, home policy, and repositories from any working directory."""
    subject = import_module("packages.afairesi.main")
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitignore").write_text("*\n!/.gitmodules\n!/.ssh/\n!/.ssh/key\n")
    (home / ".gitmodules").write_text(
        '[submodule "demo"]\npath = forge.example/team/demo\n',
    )
    repository = home / "forge.example/team/demo"
    repository.mkdir(parents=True)
    _repository(repository)
    _make_source_package(repository, "example", "def test_example(): pass\n")
    storage = tmp_path / "persistent"
    (storage / "etc").mkdir(parents=True)
    (storage / "etc/machine-id").write_text("secret contents must not be shown")
    monkeypatch.setattr(Path, "home", lambda: home)
    inventory = subject.persistent_summary
    monkeypatch.setattr(subject, "persistent_summary", lambda _root: inventory(storage))
    system = {"os": {"id": "nixos"}, "preservation": {"status": "available"}}
    monkeypatch.setattr(subject, "system_summary", lambda: system)

    def unexpected_discovery(_target: Path) -> dict[str, Any]:
        message = "bare inspection must not recursively discover repositories"
        raise AssertionError(message)

    monkeypatch.setattr(subject, "migration_summary", unexpected_discovery)
    expected_home: dict[str, Any] = {
        ".gitmodules": None,
        ".ssh/": ["key"],
        "forge.example/": {
            "team/": {
                "demo/": {
                    "packages": {"example": {"tests": ["example"]}},
                },
            },
        },
    }
    expected_paths = expected_home
    for component in reversed(home.parts[2:]):
        expected_paths = {component + "/": expected_paths}
    expected = {
        socket.gethostname(): {
            "system": system,
            "filesystem": {
                "/etc/": ["machine-id"],
                "/" + home.parts[1] + "/": expected_paths,
            },
        },
    }
    for cwd in (tmp_path, repository, repository / "packages/example"):
        monkeypatch.chdir(cwd)
        monkeypatch.setattr(sys, "argv", ["afairesi"])
        subject.main()
        output = capsys.readouterr().out
        _expect(
            "persistent" not in output and "secret contents" not in output,
            output,
        )
        _expect(output == json.dumps(expected, indent=2, sort_keys=True) + "\n", output)


@pytest.mark.parametrize("status", ["missing", "not_applicable", "available"])
def test_machine_storage_status_is_reported_in_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status: str,
) -> None:
    """Report skipped or failed inspection while still showing parsed home paths."""
    subject = import_module("packages.afairesi.main")
    (tmp_path / ".gitignore").write_text("*\n!/.ssh/\n!/.ssh/key\n")
    system = {"os": {"id": "nixos"}, "preservation": {"status": status}}
    monkeypatch.setattr(subject, "system_summary", lambda: system)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    calls: list[Path] = []

    def inspect_storage(root: Path) -> dict[str, Any]:
        calls.append(root)
        return {"diagnostics": ["Permission denied"]}

    monkeypatch.setattr(subject, "persistent_summary", inspect_storage)
    monkeypatch.setattr(sys, "argv", ["afairesi"])
    subject.main()
    tree = json.loads(capsys.readouterr().out)
    machine = tree[socket.gethostname()]
    observed_status = "unavailable" if status == "available" else status
    _expect(machine["system"]["preservation"]["status"] == observed_status, machine)
    _expect(calls == ([Path("/persistent")] if status == "available" else []), calls)


def test_mutation_campaigns_reject_invalid_baselines_and_continue() -> None:
    """Check fixed baseline failures once, independently of generated values."""
    with TemporaryDirectory(prefix="afairesi-baselines-") as directory:
        root = Path(directory) / "source with spaces"
        environment = _prepare_runner_flake(
            root,
            "def test_ready(): pass\n",
            name="git-example",
        )
        baselines = {
            "alpha": "def test_failure():\n    assert False\n",
            "beta": "",
            "gamma": "raise ImportError('missing dependency')\n",
            "delta": (
                "import pytest\n@pytest.mark.skip(reason='disabled')\n"
                "def test_skipped():\n    assert False\n"
            ),
        }
        for name, baseline in baselines.items():
            _runner_package(root, name, baseline)
        _runner_package(root, "untested", "")
        (root / "packages/untested/test_main.py").unlink()
        result = _campaign(
            root,
            environment,
            "mutation",
            "--timeout",
            "10",
            "--max-mutations",
            "1",
        )
        _expect(
            result.returncode == 1
            and "1 passed, 4 failed, 1 skipped" in result.stdout
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


def test_mutation_campaigns_report_empty_plans() -> None:
    """Accept an empty mutation plan when explicit baseline examples pass."""
    with TemporaryDirectory(prefix="afairesi-empty-mutations-") as directory:
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


@settings(deadline=None)
@given(value=st.integers(min_value=1, max_value=9))
@example(value=1)
def test_mutation_campaigns_report_outcomes_and_replay_plans(
    value: int,
) -> None:
    """Report killed/surviving mutations and replay plans across generated values."""
    with TemporaryDirectory(prefix="afairesi-mutations-") as directory:
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
            "import subprocess\nfrom pathlib import Path\n"
            "def test_cli():\n"
            "    marker = Path.home() / 'baseline-marker'\n"
            "    assert not marker.exists()\n"
            "    marker.write_text('created')\n"
            "    result = subprocess.run(['git', 'example'], "
            "capture_output=True, text=True)\n"
            "    assert result.returncode == 0 "
            f"and result.stdout == {str(value) + chr(10)!r}\n"
            "def test_cli_process_success():\n"
            "    result = subprocess.run(['git', 'example'], "
            "capture_output=True, text=True)\n"
            "    assert result.returncode == 0\n"
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
        target_line = 2
        focused = _campaign(
            root,
            environment,
            "mutation",
            "--timeout",
            "10",
            "-k",
            "test_cli and not process_success",
            "--lines",
            str(target_line),
            "--operator",
            "NumberReplacer",
            "--max-mutations",
            "1",
            cwd=root / "packages/git-example",
        )
        _expect(not focused.returncode, focused)
        focused_workspace = next(
            path
            for path in (root / "tmp").glob("python-mutation-git-example-*")
            if path != workspace
        )
        plan_path = focused_workspace / "mutation-plan.json"
        plan = json.loads(plan_path.read_text())
        defects = json.loads((focused_workspace / "mutation-results.json").read_text())
        _expect(
            len(plan["mutations"]) == len(defects["mutations"]) == 1
            and defects["mutations"][0]["status"] == "killed"
            and defects["mutations"][0]["mutations"][0]["start_pos"][0] == target_line
            and defects["kills_by_test"]
            == {
                "test_main.py::test_cli": [plan["mutations"][0]["id"]],
            },
            defects,
        )
        existing_workspaces = set((root / "tmp").iterdir())
        weaker = _campaign(
            root,
            environment,
            "mutation",
            "--timeout",
            "10",
            "-k",
            "process_success",
            "--mutation-plan",
            str(plan_path),
            cwd=root / "packages/git-example",
        )
        _expect(not weaker.returncode, weaker)
        (weaker_workspace,) = set((root / "tmp").iterdir()) - existing_workspaces
        weaker_defects = json.loads(
            (weaker_workspace / "mutation-results.json").read_text(),
        )
        _expect(
            len(weaker_defects["mutations"]) == 1
            and weaker_defects["mutations"][0]["id"] == defects["mutations"][0]["id"]
            and weaker_defects["mutations"][0]["status"] == "survived"
            and not weaker_defects["kills_by_test"],
            weaker_defects,
        )
        package_source = root / "packages/git-example/main.py"
        package_source.write_text(
            source.replace(f"return {value}\n", f"return {value + 10}\n"),
        )
        mismatch = _campaign(
            root,
            environment,
            "mutation",
            "--timeout",
            "10",
            "-k",
            "process_success",
            "--mutation-plan",
            str(plan_path),
            cwd=root / "packages/git-example",
        )
        _expect(
            mismatch.returncode == 1 and "does not match" in mismatch.stderr,
            mismatch,
        )


def test_overview_groups_home_repositories_and_rejects_escaping_submodules() -> None:
    """Keep same-named resources separate across repositories."""
    with TemporaryDirectory(prefix="afairesi-summaries-") as directory:
        _check_home_summaries(_home_repository(Path(directory)))


@pytest.mark.parametrize(
    "remote",
    [
        "git@forge.example:owner/demo.git",
        "https://forge.example/owner/demo.git",
        "ssh://git@forge.example/owner/demo.git",
    ],
)
def test_overview_json_preserves_machine_user_repository_hierarchy(
    tmp_path: Path,
    remote: str,
) -> None:
    """Preserve hosted clones, packages, hosts, and formatted JSON output."""
    _repository(tmp_path)
    _git(tmp_path, "remote", "add", "origin", remote)
    package = tmp_path / "packages/shared"
    package.mkdir(parents=True)
    (package / "default.nix").write_text('{ meta.description = "Shared"; }\n')
    host = tmp_path / "hosts/shared"
    host.mkdir(parents=True)
    (host / "configuration.nix").write_text(
        "{ inputs, system, ... }: { environment.systemPackages = "
        "[ inputs.self.packages.${system}.shared ]; }\n",
    )
    groups = {
        "hosts": {"shared": {"dependencies": ["runtime: packages/shared"]}},
        "packages": {"shared": {"description": "Shared"}},
    }
    machine, user = socket.gethostname(), getpass.getuser()
    expected = {machine: {user: {"forge.example": {"owner": {"demo": groups}}}}}
    before = _snapshot(tmp_path)
    terminal = _run(tmp_path, str(tmp_path)).stdout
    _expect(_overview_details(terminal) == expected, terminal)
    _expect(
        terminal == json.dumps(json.loads(terminal), indent=2, sort_keys=True) + "\n",
        terminal,
    )
    for collection, selected in (("packages", package), ("hosts", host)):
        focused = {
            machine: {
                user: {
                    "forge.example": {
                        "owner": {"demo": {collection: groups[collection]}},
                    },
                },
            },
        }
        _expect(_overview_tree(selected) == focused, focused)
        output = _run(selected, str(tmp_path)).stdout
        _expect(_overview_details(output) == expected, output)
    _expect(_snapshot(tmp_path) == before, "inspection changed Git or source state")


def test_overview_preserves_structured_details_and_omits_empty_fields() -> None:
    """Keep CLI ownership, source inventories, and diagnostics in structured output."""
    with _fresh_repository() as root:
        _check_overview_details(root)


def test_overview_renders_declarations_without_executing_sources() -> None:
    """Exercise installed rendering without executing inspected package sources."""
    contract = next(contract for contract in CLI_CONTRACTS if contract[1])
    labels = ["double__underscore", "async_behavior"]
    with _fresh_repository() as root:
        sentinel = (
            "from pathlib import Path\n"
            "Path('SENTINEL').touch()\n"
            "raise RuntimeError('must not execute')\n"
        )
        tests = _test_declarations(labels, "functions")
        package = _make_source_package(root, "my-package", tests)
        source, expected_args = contract
        (package / "main.py").write_text(
            '"""First paragraph.\n\nArguments:\n  Documentation text."""\n'
            + sentinel
            + source,
        )
        expected_names = [label.replace("_", " ") for label in labels]
        before = _snapshot(root)
        inspected = _run(root, str(package))
        overview = _overview_resources(inspected.stdout)["packages"]["my-package"]
        expected_overview = dict(expected_args)
        if expected_names:
            expected_overview["tests"] = expected_names
        _expect(overview == expected_overview, contract)
        details = import_module("packages.afairesi.main").resource_data(package)
        _expect(
            details["tests"] == [label.replace("_", " ") for label in labels],
            details,
        )
        _expect("help" not in details and "Help:" not in inspected.stdout, details)
        _expect(
            _snapshot(root) == before
            and not (root / "SENTINEL").exists()
            and not (package / "SENTINEL").exists(),
            "inspection changed source or executed code",
        )
        broken = _make_source_package(root, "alpha", "def invalid(")
        (broken / "main.py").write_text("import sys\nprint(sys.argv)\n")
        (package / "main.py").write_text(source)
        missing = _make_source_package(root, "untested", "")
        (missing / "test_main.py").unlink()
        (root / "packages/linked").symlink_to(package, target_is_directory=True)
        listed = _run(root, str(root))
        listed_packages = _overview_resources(listed.stdout)["packages"]
        _expect(listed_packages["my-package"] == expected_overview, listed)
        _expect(
            "diagnostics" in listed_packages["alpha"]
            and "unsupported CLI interface" in listed.stdout
            and listed_packages["untested"] == {}
            and "linked" not in listed_packages,
            listed,
        )


def test_overview_summarizes_host_only_repositories() -> None:
    """Preserve host dependencies, diagnostics, and explicit scope."""
    with _fresh_repository() as root:
        _check_host_summaries(root)


def test_persistent_inventory_bounds_traversal_and_reports_unreadable_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep service data compact and do not follow links or hide access failures."""
    subject = import_module("packages.afairesi.main")
    storage = tmp_path / "persistent"
    (storage / "var/lib/service").mkdir(parents=True)
    (storage / "var/lib/service/hidden").write_text("runtime")
    (storage / "file").write_text("private contents")
    (storage / "empty").mkdir()
    (storage / "files").mkdir()
    (storage / "files/z-last").touch()
    (storage / "files/a-first").touch()
    (storage / "mixed/empty").mkdir(parents=True)
    (storage / "mixed/file").touch()
    (storage / "link").symlink_to(storage, target_is_directory=True)
    (storage / "files/link").symlink_to(storage, target_is_directory=True)
    blocked = storage / "private"
    blocked.mkdir()
    iterdir = Path.iterdir

    def restricted_iterdir(path: Path) -> Iterator[Path]:
        if path == blocked:
            raise PermissionError(13, "Permission denied", str(path))
        return iterdir(path)

    monkeypatch.setattr(Path, "iterdir", restricted_iterdir)
    details = subject.persistent_summary(storage)
    _expect(
        details
        == {
            "/empty/": {},
            "/file": None,
            "/files/": ["a-first", "link", "z-last"],
            "/link": None,
            "/mixed/": {"empty/": {}, "file": None},
            "/private/": {"diagnostics": ["Permission denied"]},
            "/var/": {"lib/": {"service/": {}}},
        },
        details,
    )
    missing = subject.persistent_summary(tmp_path / "missing")
    _expect(missing == {"diagnostics": ["No such file or directory"]}, missing)


def test_python_packages_require_the_shared_constructor() -> None:
    """Reject unsupported builders before convergence changes repository state."""
    with _fresh_repository() as root:
        _run(root, "add", "packages/example", "python")
        definition = root / "packages/example/default.nix"
        definition.write_text(
            "{ pkgs, ... }: pkgs.python3.pkgs.buildPythonPackage { src = ./.; }",
        )
        _git(root, "add", ".")
        before = _snapshot(root)
        result = _run(root, "converge", code=1)
        _expect("shared mkPythonPackage constructor" in result.stderr, result)
        _expect(_snapshot(root) == before, "unsupported builder changed repository")
        (root / "packages/example/ms.tex").touch()
        before = _snapshot(root)
        result = _run(root, "converge", code=1)
        _expect("ambiguous project markers" in result.stderr, result)
        _expect(_snapshot(root) == before, "ambiguous markers changed repository")


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
)
@example(arguments=("mv", "packages/example", "hosts/example"))
@example(arguments=("rm", "../outside"))
@example(arguments=("rm", "/outside"))
@example(arguments=("add", "packages/bad--name", "python"))
@example(arguments=("add", "hosts/bad-name"))
@example(arguments=("mv", "packages/example", "packages/taken"))
@example(arguments=("add", "packages/example", "python"))
def test_rejected_operations_preserve_contents_modes_index_and_refs(
    arguments: tuple[str, ...],
) -> None:
    """Reject invalid paths and collisions before modifying work."""
    with _fresh_repository() as root:
        _run(root, "add", "packages/example", "python")
        _run(root, "add", "packages/taken", "nix")
        (root / "work-in-progress").write_text("preserve unrelated work")
        before = _snapshot(root)
        rejected = _run(root, *arguments, code=1)
        _expect(bool(rejected.stderr) and _snapshot(root) == before, rejected)


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
@given(name=PACKAGE_NAMES, object_format=st.sampled_from(("sha1", "sha256")))
@example(name="core", object_format="sha1")
@example(name="dash-case", object_format="sha256")
def test_source_overviews_preserve_declarations_and_source_facts(
    name: str,
    object_format: str,
) -> None:
    """Model declarations and compare source facts with concise CLI summaries."""
    with _fresh_repository(object_format=object_format) as root:
        provider = "p" + name
        _run(root, "add", f"packages/{provider}", "nix", "Provider")
        _run(root, "add", "packages/consumer", "python", "Consumer")
        _run(root, "add", "packages/computed", "nix", "Computed")
        consumer = root / "packages/consumer"
        (consumer / "default.nix").write_text(
            "{ inputs, pkgs, system, ... }: "
            "let local = inputs.self.packages.${system}; in {\n"
            f'  propagatedBuildInputs = [ local.{provider} local."{provider}" '
            'local."\\missing" local."literal \\${name}" pkgs.git ];\n'
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
            ),
        )
        (consumer / "test_main.py").write_text(
            "def test_result(): pass\n",
        )
        assets = consumer / "prm/assets"
        assets.mkdir(parents=True)
        asset_sources = {
            "index.html": "<p>Example</p>\n",
            "script.js": "const value = 1;\n",
            "style.css": "p { color: red; }\n",
        }
        for filename, source in asset_sources.items():
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
        summaries = _overview(root)
        subject = import_module("packages.afairesi.main")
        details = subject.resource_data(consumer)
        _expect(
            {(item["target"], item["kind"]) for item in details["dependencies"]}
            == {
                (f"packages/{provider}", "runtime"),
                (f"packages/{provider}", "source"),
                ("packages/missing", "runtime"),
                ("packages/literal ${name}", "runtime"),
            },
            details,
        )
        _expect("dependencies" not in summaries["packages"]["computed"], summaries)
        summary_dependencies = summaries["packages"]["consumer"]["dependencies"]
        _expect(
            set(summary_dependencies)
            == {
                f"runtime: packages/{provider}",
                f"source: packages/{provider}",
                "runtime: packages/missing",
                "runtime: packages/literal ${name}",
            },
            summaries,
        )
        _expect(
            len(summary_dependencies) == len(set(summary_dependencies)),
            "Repeated references must appear once per dependency kind and target",
        )
        sources = {source["path"]: source for source in details["sources"]}
        _expect(
            set(sources)
            == {
                "default.nix",
                "main.py",
                "test_main.py",
                *("prm/assets/" + filename for filename in asset_sources),
            },
            sources,
        )
        _expect(
            details["tests"] == ["result"],
            details,
        )
        focus = _overview(consumer)
        _expect(
            focus == {"packages": {"consumer": summaries["packages"]["consumer"]}},
            focus,
        )
        _git(root, "add", "--force", ".")
        _fixture_git(root, "commit", "-qm", "Source snapshot")
        before = _snapshot(root)
        _expect(
            _overview(root) == summaries and _snapshot(root) == before,
            "overview changed its repository",
        )


def test_static_cli_declares_commands_and_campaign_filters() -> None:
    """Discover public CLI commands and campaign filters from source."""
    subject = import_module("packages.afairesi.main")
    source = Path(__file__).with_name("main.py").read_text()
    entries = subject.source_resource_data("afairesi", {"main.py": source})["cli"]
    discovered = {" ".join(entry["path"]) for entry in entries if entry["command"]}
    _expect(
        discovered
        == {
            "diff",
            "init",
            "add",
            "mv",
            "rm",
            "test",
            "test coverage",
            "test hypothesis",
            "test mutation",
            "converge",
        },
        "Afairesi must discover its own complete command interface",
    )
    for campaign in ("hypothesis", "mutation"):
        path = ("test", campaign)
        for flag in ("-k", "-m"):
            _expect(
                any(
                    entry["path"] == list(path)
                    and entry["text"].startswith(flag + "  ")
                    for entry in entries
                ),
                f"Static discovery must include {flag} for {campaign}",
            )
        options = subject.parser().parse_args(
            ["test", campaign, "-k", "keyword", "-m", "marker"],
        )
        _expect(
            options.keywords == "keyword" and options.markers == "marker",
            "Campaign filter arguments must retain their runtime behavior",
        )


@pytest.mark.parametrize("contract", CLI_CONTRACTS)
def test_static_cli_interfaces_match_declared_commands_and_arguments(
    contract: tuple[str, dict[str, Any]],
) -> None:
    """Parse each supported CLI declaration without executing its source."""
    subject = import_module("packages.afairesi.main")
    source, expected = contract
    details = subject.source_resource_data("example", {"main.py": source})
    summary = subject.resource_summary(details)
    _expect(summary == expected, details)
    _expect(not details["diagnostics"], details)


@settings(deadline=None)
@given(labels=TEST_LABELS, form=st.sampled_from(("functions", "classes", "unittest")))
@example(labels=["double__underscore", "async_behavior"], form="functions")
@example(labels=["result"], form="classes")
@example(labels=["derived", "async"], form="unittest")
@example(labels=[], form="functions")
@example(labels=[], form="classes")
@example(labels=[], form="unittest")
def test_static_test_sentences_follow_public_definitions_without_execution(
    labels: list[str],
    form: str,
) -> None:
    """Ignore hidden helpers and preserve public definition order and spelling."""
    subject = import_module("packages.afairesi.main")
    source = _test_declarations(labels, form)
    expected = [label.replace("_", " ") for label in labels]
    _expect(
        subject.source_test_names(source.encode(), "test_main.py") == expected,
        source,
    )
    changed = source.replace("assert False", "raise RuntimeError('changed body')")
    _expect(
        subject.source_test_names(changed.encode(), "test_main.py") == expected,
        changed,
    )


@pytest.mark.parametrize("os_id", ["nixos", "ubuntu", "unknown"])
def test_system_inspection_checks_os_before_preservation(
    os_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only inspect conventional system storage on a detected NixOS system."""
    subject = import_module("packages.afairesi.main")
    calls: list[Path] = []

    def check_storage(root: Path) -> dict[str, str]:
        calls.append(root)
        return {"path": str(root), "status": "missing", "message": "Storage missing"}

    monkeypatch.setattr(
        subject.platform,
        "freedesktop_os_release",
        lambda: {
            "ID": os_id,
            "PRETTY_NAME": "Example OS",
        },
    )
    monkeypatch.setattr(subject, "_preservation_status", check_storage)
    details = subject.system_summary()
    _expect(
        details["os"]
        == {
            "id": os_id,
            "name": "Example OS",
            "status": "detected",
        },
        details,
    )
    _expect(calls == ([Path("/persistent")] if os_id == "nixos" else []), calls)
    status = "missing" if os_id == "nixos" else "not_applicable"
    _expect(details["preservation"]["status"] == status, details)


def test_system_inspection_reports_unavailable_os(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not inspect storage when the OS cannot be identified."""
    subject = import_module("packages.afairesi.main")

    def unavailable_release() -> dict[str, str]:
        message = "os-release is missing"
        raise FileNotFoundError(message)

    def unexpected_storage(_root: Path) -> dict[str, str]:
        message = "storage inspected before OS identification"
        raise AssertionError(message)

    monkeypatch.setattr(subject.platform, "freedesktop_os_release", unavailable_release)
    monkeypatch.setattr(subject, "_preservation_status", unexpected_storage)
    details = subject.system_summary()
    _expect(details["os"]["status"] == "unavailable", details)
    _expect(details["preservation"]["status"] == "not_applicable", details)
    _expect("OS detection failed" in details["preservation"]["message"], details)


@pytest.mark.parametrize("storage_state", ["missing", "file", "directory"])
def test_system_preservation_reports_storage_state(
    tmp_path: Path,
    storage_state: str,
) -> None:
    """Distinguish missing storage from an available directory or invalid file."""
    subject = import_module("packages.afairesi.main")
    storage = tmp_path / "persistent"
    if storage_state == "file":
        storage.touch()
    elif storage_state == "directory":
        storage.mkdir()
    expected = {"missing": "missing", "file": "not_directory", "directory": "available"}
    details = subject._preservation_status(storage)  # noqa: SLF001 - status contract
    _expect(details["status"] == expected[storage_state], details)
    _expect(details["path"] == str(storage), details)
    _expect(("message" in details) == (storage_state != "directory"), details)

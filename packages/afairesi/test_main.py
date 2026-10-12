# Copyright (c) 2026- Paschalis Bizopoulos
"""Check Afairesi's public contracts with explicit regressions and generated cases."""

from __future__ import annotations

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


def _repair_flake(
    root: Path,
    *arguments: str,
    code: int = 0,
) -> subprocess.CompletedProcess[str]:
    """Exercise the private flake repair entry point used by the formatter."""
    return _run(
        root,
        "-c",
        "from packages.afairesi.main import formatter_main; formatter_main()",
        *arguments,
        code=code,
        executable=sys.executable,
    )


def _preview_flake_repair(root: Path, *, code: int = 0) -> None:
    """Verify that flake repair previews preserve contents and Git state."""
    before = _snapshot(root)
    _repair_flake(root, "--dry-run", code=code)
    _expect(_snapshot(root) == before, "flake repair preview changed state")


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
    _run(repository, "create", "packages/example", "python")
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
    _run(repository, "create", "hosts/laptop")
    (repository / "packages/example/test_main.py").write_text(
        "def test_result(): pass\n",
    )
    _repair_flake(repository)
    for relative in (
        "checks/laptopVmWithDisko/default.nix",
        "checks/example/default.nix",
    ):
        check = repository / relative
        _run(repository, "--no-cache", relative, executable="treefmt")
        formatted = check.read_text(encoding="utf-8")
        _git(repository, "add", "--", relative)
        index = _git(repository, "ls-files", "--stage")
        result = _repair_flake(repository)
        _expect(
            check.read_text(encoding="utf-8") == formatted
            and _git(repository, "ls-files", "--stage") == index
            and f"write '{relative}'" not in result.stdout,
            f"convergence regenerated formatted {relative}",
        )


def _check_host_repair(repository: Path) -> None:
    """Repair a changed check while preserving its host and resource inventory."""
    _run(repository, "create", "hosts/laptop")
    host = repository / "hosts/laptop/configuration.nix"
    original_host = host.read_bytes()
    original_paths = _git(repository, "ls-files").splitlines()
    relative = "checks/laptopVmWithDisko/default.nix"
    check = repository / relative
    current = check.read_bytes()
    changed = current.replace(b'"VmWithDisko"', b'"Changed"')
    check.write_bytes(changed)
    _git(repository, "add", "--", relative)
    _repair_flake(repository, "--dry-run", code=1)
    if check.read_bytes() != changed:
        message = "dry-run changed the generated check"
        raise AssertionError(message)
    _repair_flake(repository)
    if check.read_bytes() != current:
        message = "convergence did not repair the generated check"
        raise AssertionError(message)
    if host.read_bytes() != original_host:
        message = "check repair changed the host configuration"
        raise AssertionError(message)
    if _git(repository, "ls-files").splitlines() != original_paths:
        message = "check repair added consumer files"
        raise AssertionError(message)
    if _repair_flake(repository, "--dry-run").stdout:
        message = "check repair did not converge"
        raise AssertionError(message)


@pytest.mark.parametrize(
    "remote",
    [
        "https://example.test/team/project.git",
        "ssh://git@example.test/team/project.git",
        "git@example.test:team/project.git",
    ],
)
def test_clone_registers_existing_content_without_creating_commits(
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
    _run(tmp_path, "init-home")
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
    result = _run(tmp_path, "clone", remote)
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
        message = "clone REMOTE did not stage the home whitelist"
        raise AssertionError(message)
    if (checkout / "README").read_text() != "existing repository":
        raise AssertionError(result.stdout + result.stderr)
    if _git(home, "rev-parse", "HEAD") != head:
        message = "clone REMOTE created a home commit"
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
        message = "clone REMOTE changed the registered remote"
        raise AssertionError(message)
    if not _git(home, "ls-files", "--stage", "--", relative).startswith("160000 "):
        message = "clone REMOTE did not stage the submodule"
        raise AssertionError(message)
    _run(tmp_path, "clone", remote)
    if (home / ".gitignore").read_text(encoding="utf-8") != ignore:
        message = "repeated clone changed the home whitelist"
        raise AssertionError(message)


def test_init_flake_commits_registers_and_publishes_an_empty_remote(
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
        '#!/bin/sh\nif [ "$1" = flake ]; then printf "{}\\n" > flake.lock; '
        "else mkdir -p .git/hooks; cat > .git/hooks/pre-push <<'HOOK'\n"
        '#!/bin/sh\ngit -C "$HOME" config --file .gitmodules '
        "--get-regexp '^submodule\\..*\\.path$' >/dev/null\n"
        "HOOK\nchmod +x .git/hooks/pre-push\nfi\n",
        encoding="utf-8",
    )
    fake_nix.chmod(0o755)
    monkeypatch.setenv("AFAIRESI_NIX", str(fake_nix))
    _run(tmp_path, "init-home")
    _git(home, "commit", "--quiet", "-m", "Home policy")
    home_head = _git(home, "rev-parse", "HEAD")
    _run(tmp_path, "init-flake", remote)
    relative = "example.test/team/new"
    checkout = home / relative
    _expect(
        _git(checkout, "branch", "--show-current").strip() == "main",
        "init-flake did not select main",
    )
    _expect(
        _git(origin, "rev-parse", "refs/heads/main")
        == _git(checkout, "rev-parse", "HEAD"),
        "init-flake did not publish its initial commit",
    )
    _expect(
        _git(home, "rev-parse", "HEAD") == home_head,
        "init-flake committed the home registration",
    )
    _expect(
        set(_git(checkout, "ls-files").splitlines())
        == {".gitignore", "README", "flake.lock", "flake.nix"},
        "init-flake did not commit the canonical initial files",
    )
    _expect(
        (checkout / "README").read_text() == "# new\n"
        and "inputs.afairesi.url" in (checkout / "flake.nix").read_text(),
        "init-flake did not scaffold the requested repository",
    )
    ignore = (home / ".gitignore").read_text(encoding="utf-8")
    if f"!/{relative}" not in ignore.splitlines():
        raise AssertionError(ignore)
    if _git(home, "show", ":.gitignore") != ignore:
        message = "init-flake did not stage the home whitelist"
        raise AssertionError(message)
    if not _git(home, "ls-files", "--stage", "--", relative).startswith("160000 "):
        message = "init-flake did not stage its submodule"
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
    _run(root, "repair-home")
    if source.read_text() != "second":
        message = "convergence changed dirty submodule content"
        raise AssertionError(message)
    _git(checkout, "add", "README")
    _git(checkout, "commit", "--quiet", "-m", "second")
    second = _git(checkout, "rev-parse", "HEAD").strip()
    for published in (False, True):
        if published:
            _git(checkout, "update-ref", "refs/remotes/origin/main", second)
        _run(root, "repair-home")
        if first not in _git(root, "ls-files", "--stage", relative):
            message = "convergence advanced the recorded submodule commit"
            raise AssertionError(message)
    _git(root, "add", relative)


def _check_home_whitelist(home_repository: Path) -> None:
    """Repair missing whitelist entries for a registered home submodule."""
    root = home_repository
    ignore = root / ".gitignore"
    expected = ignore.read_text(encoding="utf-8")
    ignore.write_text("*\n!/.gitignore\n!/.gitmodules\n", encoding="utf-8")
    before = _git(root, "show", ":.gitignore")
    preview = _run(root, "repair-home", "--dry-run")
    if "would write '.gitignore'" not in preview.stdout:
        raise AssertionError(preview.stdout)
    if (
        ignore.read_text(encoding="utf-8") == expected
        or _git(root, "show", ":.gitignore") != before
    ):
        message = "dry-run changed the home whitelist"
        raise AssertionError(message)
    _run(root, "repair-home")
    if ignore.read_text(encoding="utf-8") != expected:
        raise AssertionError(ignore.read_text(encoding="utf-8"))
    if _git(root, "show", ":.gitignore") != expected:
        message = "convergence did not stage the repaired whitelist"
        raise AssertionError(message)
    _run(root, "repair-home", "--dry-run")


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
    _run(root, "repair-home", "--dry-run")
    _run(root, "repair-home")
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
    rejected = _run(root, "repair-home", code=1)
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
    rejected = _run(root, "repair-home", code=1)
    if "stage .gitmodules" not in rejected.stderr or not checkout.exists():
        raise AssertionError(rejected.stderr)
    _git(root, "add", ".gitmodules")
    _run(root, "repair-home", "--dry-run")
    if not checkout.exists() or target.exists():
        message = "dry-run moved the checkout"
        raise AssertionError(message)
    _run(root, "repair-home")
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
    _run(root, "repair-home", "--dry-run")
    whitelist = (root / ".gitignore").read_text().splitlines()
    _expect(f"!/{relative}" not in whitelist, whitelist)
    _expect(f"!/{destination}" in whitelist, whitelist)


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


PACKAGE_NAMES = st.from_regex(
    r"[a-z][a-z0-9]{0,5}(?:[-_][a-z0-9]{1,5})?",
    fullmatch=True,
)
DESCRIPTIONS = st.text(
    alphabet=st.characters(exclude_categories=["Cs"], exclude_characters="\x00"),
    min_size=1,
    max_size=30,
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


def test_init_home_stages_policy_without_cleaning_or_committing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Initialize HOME from another directory and preserve existing home data."""
    home = tmp_path / "home"
    home.mkdir()
    local = home / "personal-data"
    local.write_bytes(b"\x00keep home data\xff")
    monkeypatch.setenv("HOME", str(home))
    _run(tmp_path, "init-home")
    expected = "*\n!/.gitignore\n!/.gitmodules\n"
    _expect((home / ".gitignore").read_text() == expected, "wrong home policy")
    _expect(_git(home, "show", ":.gitignore") == expected, "policy was not staged")
    _expect(
        _git(home, "ls-files").splitlines() == [".gitignore"],
        "home data was staged",
    )
    _expect(not _git(home, "for-each-ref"), "init-home created a commit")
    _expect(local.read_bytes() == b"\x00keep home data\xff", "init-home cleaned HOME")
    before = _snapshot(home)
    _run(tmp_path, "init-home")
    _expect(_snapshot(home) == before, "repeated init-home changed state")
    _expect(
        not (tmp_path / ".git").exists(),
        "init-home initialized the working directory",
    )


@pytest.mark.parametrize(
    ("kind", "filenames"),
    [
        ("python", {"default.nix", "main.py"}),
        ("html", {"default.nix", "index.html", "script.js", "style.css"}),
        ("latex", {"default.nix", "ms.tex", "ms.bib"}),
        ("nix", {"default.nix"}),
        ("host", {"configuration.nix"}),
    ],
)
def test_create_stages_the_requested_resource_and_whitelist(
    kind: str,
    filenames: set[str],
) -> None:
    """Create each supported resource with its declared files and related metadata."""
    with _fresh_repository() as root:
        _fixture_git(root, "commit", "-qm", "Initial repository")
        head = _git(root, "rev-parse", "HEAD")
        relative = "hosts/example" if kind == "host" else "packages/example"
        arguments = () if kind == "host" else (kind, "Shared", "command description")
        _run(root, "create", relative, *arguments)
        resource = root / relative
        _expect(
            {path.name for path in resource.iterdir()} == filenames,
            "wrong resource files",
        )
        staged = {f"{relative}/{name}" for name in filenames} | {".gitignore"}
        if kind == "host":
            staged.add("checks/exampleVmWithDisko/default.nix")
        else:
            _expect(
                'meta.description = "Shared command description";'
                in (resource / "default.nix").read_text(),
                "create lost the description arguments",
            )
        _expect(
            set(_git(root, "diff", "--cached", "--name-only").splitlines()) == staged,
            "create staged unexpected paths",
        )
        for path in staged:
            _expect(_git(root, "show", f":{path}") == (root / path).read_text(), path)
        _expect(_git(root, "rev-parse", "HEAD") == head, "create committed changes")


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (("packages/example",), "package creation requires TYPE"),
        (("packages/example", "unknown"), "unsupported package type"),
        (
            ("hosts/example", "python"),
            "host creation does not accept a type or description",
        ),
    ],
)
def test_create_rejects_invalid_resource_arguments_without_changes(
    arguments: tuple[str, ...],
    message: str,
) -> None:
    """Reject missing or incompatible resource arguments with an actionable error."""
    with _fresh_repository() as root:
        before = _snapshot(root)
        result = _run(root, "create", *arguments, code=1)
        _expect(message in result.stderr, result)
        _expect(_snapshot(root) == before, "invalid create changed state")


def test_cli_help_lists_current_commands_and_rejects_invalid_requests() -> None:
    """Expose consistent commands and reject invalid requests before creating state."""
    with TemporaryDirectory(prefix="afairesi-cli-") as directory:
        root = Path(directory)
        commands = (
            (),
            ("create",),
            ("move",),
            ("remove",),
            ("init-home",),
            ("init-flake",),
            ("clone",),
            ("repair-home",),
        )
        for command_path in commands:
            option = _run(root, *command_path, "--help").stdout
            _expect(
                "usage:" in option,
                command_path,
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
        for retired in ("init", "add"):
            _run(root, retired, code=2)
        for invalid_arguments in (
            ("init-home", "unexpected"),
            ("init-flake",),
            ("init-flake", "remote", "unexpected"),
            ("clone",),
            ("clone", "remote", "unexpected"),
            ("create",),
        ):
            _run(root, *invalid_arguments, code=2)
        help_output = _run(root, "--help").stdout
        command_names = sorted(path[0] for path in commands if path)
        positions = [help_output.index(f"    {name} ") for name in command_names]
        _expect(positions == sorted(positions), "help commands must be alphabetical")
        _expect(_run(root).stdout == help_output, "bare afairesi must show help")
        _run(root, "repair-home", "--source", str(root), code=2)
        _expect(not any(root.iterdir()), "invalid CLI request created state")


@pytest.mark.parametrize("kind", ["python", "host"])
@pytest.mark.parametrize(
    "change",
    ["working", "staged", "untracked", "ignored", "check", "nested"],
)
def test_resource_removal_requires_force_to_discard_local_data(
    kind: str,
    change: str,
) -> None:
    """Reject destructive previews and removals unless deletion is explicitly forced."""
    with _fresh_repository() as root:
        relative = "hosts/example" if kind == "host" else "packages/example"
        _run(root, "create", relative, *(() if kind == "host" else (kind,)))
        _fixture_git(root, "commit", "-qm", "Resource snapshot")
        resource = root / relative
        if change in {"working", "staged"}:
            marker = "configuration.nix" if kind == "host" else "default.nix"
            path = resource / marker
            path.write_text(path.read_text() + "# local changes\n")
            if change == "staged":
                _git(root, "add", "--", str(path.relative_to(root)))
        else:
            check = "exampleVmWithDisko" if kind == "host" else "example"
            path = (
                root / "checks" / check / "asset"
                if change == "check"
                else resource / ("tmp/asset" if change == "ignored" else "prm/asset")
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("preserve local data")
            if change == "nested":
                _git(path.parent, "init", "--quiet")
        before = _snapshot(root)
        for arguments in (("--dry-run",), ()):
            rejected = _run(root, "remove", relative, *arguments, code=1)
            _expect("--force" in rejected.stderr, rejected)
            _expect(_snapshot(root) == before, "rejected removal changed state")
        _preview(root, "remove", relative, "--force")
        _run(root, "remove", relative, "--force")
        _expect(not resource.exists() and not path.exists(), "forced removal left data")


def test_resource_commands_resolve_paths_from_the_repository_root() -> None:
    """Resolve resource paths from the repository root from nested directories."""
    with _fresh_repository() as root:
        nested = root / "tmp/nested"
        nested.mkdir(parents=True)
        _run(nested, "create", "packages/example", "nix")
        _expect((root / "packages/example/default.nix").is_file(), "wrong create root")
        _run(nested, "move", "packages/example", "packages/renamed")
        _fixture_git(root, "commit", "-qm", "Renamed resource")
        _preview(nested, "remove", "packages/renamed")
        _run(nested, "remove", "packages/renamed")
        _expect(not (root / "packages/renamed").exists(), "wrong removal root")


@pytest.mark.parametrize("case", ["collection", "resource", "dangling-marker"])
def test_creation_rejects_symlinks_without_writing_outside_the_repository(
    tmp_path: Path,
    case: str,
) -> None:
    """Reject existing resource trees and symlink ancestors before writing files."""
    outside = tmp_path / "outside"
    outside.mkdir()
    with _fresh_repository() as root:
        package = root / "packages/example"
        if case == "collection":
            (root / "packages").symlink_to(outside, target_is_directory=True)
        elif case == "resource":
            package.parent.mkdir()
            package.symlink_to(outside, target_is_directory=True)
        else:
            package.mkdir(parents=True)
            (package / "default.nix").symlink_to(outside / "external.nix")
        before = _snapshot(root)
        _run(root, "create", "packages/example", "nix", code=1)
        _expect(_snapshot(root) == before, "rejected creation changed repository state")
        _expect(not any(outside.iterdir()), "creation wrote outside the repository")


@pytest.mark.parametrize("kind", ["python", "host"])
def test_failed_creation_restores_the_whitelist_and_index(kind: str) -> None:
    """Restore distinct staged and working whitelist contents after Git add fails."""
    with _fresh_repository() as root:
        ignore = root / ".gitignore"
        ignore.write_text("# staged whitelist\n")
        _git(root, "add", ".gitignore")
        ignore.write_text("# working whitelist\n")
        ignore.chmod(0o600)
        (root / ".git/index.lock").touch()
        before = _snapshot(root)
        resource = "hosts/example" if kind == "host" else "packages/example"
        _run(root, "create", resource, *(() if kind == "host" else (kind,)), code=1)
        _expect(_snapshot(root) == before, "failed creation left structural changes")


@pytest.mark.parametrize("case", ["symlink", "file"])
def test_removal_validates_all_targets_before_preview_or_deletion(
    tmp_path: Path,
    case: str,
) -> None:
    """Reject unsupported related checks before deleting the package."""
    with _fresh_repository() as root:
        _run(root, "create", "packages/example", "nix")
        check = root / "checks/example"
        check.parent.mkdir()
        if case == "symlink":
            check.symlink_to(tmp_path, target_is_directory=True)
        else:
            check.write_text("preserve check contents")
        before = _snapshot(root)
        for arguments in (("--dry-run",), ()):
            _run(root, "remove", "packages/example", "--force", *arguments, code=1)
            _expect(_snapshot(root) == before, "rejected removal deleted local files")


def test_failed_clone_restores_home_whitelist_and_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restore whitelist contents, staging, and directories after clone failure."""
    home = _home_repository(tmp_path / "home")
    monkeypatch.setenv("HOME", str(home))
    remote = "https://example.test/team/missing.git"
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv(
        "GIT_CONFIG_KEY_0",
        f"url.{(tmp_path / 'missing').as_uri()}.insteadOf",
    )
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", remote)
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")
    ignore = home / ".gitignore"
    ignore.write_text(ignore.read_text() + "!/working-only\n")
    before = _snapshot(home)
    _run(tmp_path, "clone", remote, code=128)
    _expect(_snapshot(home) == before, "failed clone changed the home repository")


@pytest.mark.parametrize("case", ["whitelist", "registration", "symlink"])
def test_flake_initialization_validates_home_before_creating_or_publishing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    """Reject invalid home state before creating a checkout or publishing a commit."""
    home = _home_repository(tmp_path / "home")
    origin = tmp_path / "remote.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "--quiet")
    remote = "https://example.test/team/new.git"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{origin.as_uri()}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", remote)
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")
    if case == "whitelist":
        ignore = home / ".gitignore"
        ignore.write_text(ignore.read_text() + "invalid\n")
    elif case == "registration":
        (home / ".gitmodules").write_text("invalid registration\n")
    else:
        (home / "example.test").symlink_to(tmp_path, target_is_directory=True)
    before = _snapshot(home)
    _run(tmp_path, "init-flake", remote, code=1)
    _expect(_snapshot(home) == before, "failed initialization changed home state")
    _expect(
        not _git(origin, "for-each-ref"),
        "failed initialization published a commit",
    )


@pytest.mark.parametrize("style", ["literal", "split", "annotated", "concatenated"])
def test_rename_updates_runtime_paths_without_staging_unrelated_changes(
    style: str,
) -> None:
    """Move runtime data and update owned and input paths while preserving Git state."""
    with _fresh_repository() as root:
        _git(root, "remote", "add", "origin", "https://forge.example/team/repo.git")
        for name in ("old", "consumer"):
            _run(root, "create", f"packages/{name}", "python")
        old_path = "forge.example/team/repo/packages/old/tmp"
        new_path = "forge.example/team/repo/packages/new/tmp"
        source = root / "packages/old/main.py"
        declaration = {
            "literal": f"RUNTIME_DIR = Path.home() / {old_path!r}\n",
            "split": (
                "RUNTIME_DIR = (\n    Path.home() / 'forge.example/team/repo'\n"
                "    / 'packages' / 'old'  # preserve path comment\n    / 'tmp'\n)\n"
            ),
            "annotated": (
                f"LABEL = 'Δ'; RUNTIME_DIR: Path = Path.home() / {old_path!r}\n"
            ),
            "concatenated": (
                "RUNTIME_DIR = Path.home() / "
                "('forge.example/team/repo/packages/ol' 'd/tmp')\n"
            ),
        }[style]
        source.write_text(
            source.read_text() + "\nfrom pathlib import Path\n" + declaration,
        )
        consumer = root / "packages/consumer/main.py"
        consumer.write_text(
            consumer.read_text() + "\nfrom pathlib import Path\n"
            f"INPUT_DIRS = (Path.home() / {old_path!r}, "
            "Path.home() / 'other.example/team/repo/packages/old/tmp',)\n",
        )
        _git(
            root,
            "add",
            "--",
            str(source.relative_to(root)),
            str(consumer.relative_to(root)),
        )
        source.write_text(source.read_text() + "# unstaged owner edit\n")
        consumer.write_text(consumer.read_text() + "# unstaged consumer edit\n")
        data = root / "packages/old/tmp/asset"
        data.parent.mkdir()
        data.write_text("preserve runtime data")
        _preview(root, "move", "packages/old", "packages/new")
        _run(root, "move", "packages/old", "packages/new")
        renamed_source = (root / "packages/new/main.py").read_text()
        if style == "split":
            _expect(
                declaration.replace("'old'", "'new'") in renamed_source,
                "rename changed path formatting or comments",
            )
        if style == "annotated":
            _expect(
                "LABEL = 'Δ'; RUNTIME_DIR: Path" in renamed_source,
                "annotation changed",
            )
        for name, marker in (("new", "owner"), ("consumer", "consumer")):
            relative = f"packages/{name}/main.py"
            working = (root / relative).read_text()
            indexed = _git(root, "show", f":{relative}")
            if name == "consumer" or style != "split":
                _expect(
                    new_path in working and new_path in indexed,
                    "runtime path stayed stale",
                )
            _expect(f"# unstaged {marker} edit" in working, "working edit was lost")
            _expect(
                f"# unstaged {marker} edit" not in indexed,
                "working edit was staged",
            )
        _expect(
            "other.example/team/repo/packages/old/tmp" in consumer.read_text(),
            "rename changed another repository's input path",
        )
        _expect(
            (root / "packages/new/tmp/asset").read_text() == "preserve runtime data",
            "rename lost runtime data",
        )
        subject = import_module("packages.afairesi.main")
        package = next(
            item for item in subject.detect_packages(root) if item.name == "new"
        )
        _expect(
            not subject._python_source_issues(package),  # noqa: SLF001
            "renamed runtime path is invalid",
        )


def test_convergence_preserves_formatted_checks() -> None:
    """Keep generated checks stable after the real formatting pipeline."""
    with _fresh_repository() as root:
        _run(root, "create", "packages/example", "python")
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
        _run(root, "create", "packages/example", "python")
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
        _preview_flake_repair(root, code=1)
        _repair_flake(root)
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
        _repair_flake(root)
        _expect(_snapshot(root) == stable, "convergence is not idempotent")
        _preview_flake_repair(root)


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
        _run(root, "create", "packages/example", "python")
        source = root / "packages/example/main.py"
        source.write_text(misplaced)
        before = _snapshot(root)
        rejected = _repair_flake(root, code=1)
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
        before = _snapshot(root)
        rejected = _run(
            root,
            "repair-home",
            code=1,
            environment={**os.environ, "HOME": str(root)},
        )
        _expect("requires a home repository" in rejected.stderr, rejected)
        _expect(_snapshot(root) == before, "home repair changed flake state")
        _run(root, "create", "packages/a", "python")
        _run(root, "create", "packages/z", "python")
        (root / "packages/a/default.nix").write_text(
            "{ ... }: (inputs.afairesi or inputs.self).lib.mkPythonPackage {}\n",
        )
        (root / "packages/z/main.py").write_text(source)
        (root / "unsupported").write_text("preserve until source errors are fixed")
        before = _snapshot(root)
        for arguments in (
            (),
            ("--dry-run",),
            ("--source", str(root)),
        ):
            result = _repair_flake(root, *arguments, code=1)
            _expect(message in result.stderr, result)
            _expect(_snapshot(root) == before, "invalid entrypoint changed state")


@pytest.mark.parametrize("invalid", ["def broken(:", "\xff"])
def test_convergence_rejects_invalid_test_sources_before_repairs(invalid: str) -> None:
    """Parse tests before repairing earlier packages or cleaning unsupported files."""
    with _fresh_repository() as root:
        _run(root, "create", "packages/a", "python")
        _run(root, "create", "packages/z", "python")
        (root / "packages/a/default.nix").write_text(
            "{ ... }: (inputs.afairesi or inputs.self).lib.mkPythonPackage {}\n",
        )
        (root / "packages/z/test_main.py").write_bytes(invalid.encode("latin-1"))
        before = _snapshot(root)
        result = _repair_flake(root, code=1)
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
        _run(root, "create", "packages/example", "python")
        package = root / "packages/example"
        (package / "main.py").write_text(source)
        definition = package / "default.nix"
        definition.write_text(
            "{ ... }: (inputs.afairesi or inputs.self).lib.mkPythonPackage {\n"
            '  meta.description = "Preserved";\n'
            '  passthru.custom = "Preserved too";\n'
            "}\n",
        )
        _preview_flake_repair(root, code=1)
        _repair_flake(root)
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
        _preview_flake_repair(root)


def test_convergence_respects_excluded_trees() -> None:
    """Validate excluded trees once, independently of generated resource payloads."""
    with _fresh_repository() as root, pytest.MonkeyPatch.context() as patch:
        _check_excluded_trees(root, patch)


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
        _run(root, "create", f"packages/{name}", kind, description)
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
        _repair_flake(root)
        _preview_flake_repair(root)
        environment = _nix_environment(root)
        actual = _evaluated_template(root, name, environment)
        _expect(
            kind not in {"html", "latex"}
            or 'passthru.custom = "preserved";' in definition.read_text(),
            "convergence lost custom package attributes",
        )
        _expect(actual["name"] == name.replace("-", "_"), actual)
        _expect(actual["meta"]["description"] == description, actual)
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
                (package / "main.py").write_text(
                    (
                        "import argparse\n"
                        "def main():\n"
                        "    argparse.ArgumentParser().parse_args()\n"
                    ),
                )
                _repair_flake(root)
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


def test_repair_home_preserves_dirty_submodules_and_repairs_registration() -> None:
    """Preserve checkout/index state through home convergence and relocation."""
    with (
        TemporaryDirectory(prefix="afairesi-home-") as directory,
        pytest.MonkeyPatch.context() as patch,
    ):
        root = _home_repository(Path(directory))
        patch.setenv("HOME", str(root))
        before = _snapshot(root)
        rejected = _repair_flake(root, code=1)
        _expect("run afairesi repair-home" in rejected.stderr, rejected)
        _expect(_snapshot(root) == before, "flake repair changed home state")
        _check_home_commits(root)
        _check_home_whitelist(root)
        _check_home_settings(root)
        _check_home_move(root)
        modules = root / ".gitmodules"
        modules.write_text(
            modules.read_text() + "url = git@forge.example:owner/other\n",
        )
        before = _snapshot(root)
        result = _run(root, "repair-home", code=1)
        _expect(
            "duplicate url field" in result.stderr and _snapshot(root) == before,
            result,
        )


@pytest.mark.parametrize(
    "location",
    ["home", "nested", "submodule", "flake", "outside"],
)
def test_home_repair_targets_home_from_any_working_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    location: str,
) -> None:
    """Repair only HOME and preserve preview state from repositories or plain paths."""
    home = _home_repository(tmp_path / "home")
    monkeypatch.setenv("HOME", str(home))
    checkout = home / "forge.example/owner/demo"
    (checkout / "flake.nix").touch()
    flake = tmp_path / "flake"
    flake.mkdir()
    _git(flake, "init", "--quiet")
    (flake / "flake.nix").touch()
    outside = tmp_path / "outside"
    outside.mkdir()
    cwd = {
        "home": home,
        "nested": checkout.parent,
        "submodule": checkout,
        "flake": flake,
        "outside": outside,
    }[location]
    ignore = home / ".gitignore"
    expected = ignore.read_text()
    ignore.write_text("*\n!/.gitignore\n!/.gitmodules\n")
    before = _snapshot(home)
    checkout_before = _snapshot(checkout)
    flake_before = _snapshot(flake)
    preview = _run(cwd, "repair-home", "--dry-run")
    _expect("would write '.gitignore'" in preview.stdout, preview)
    _expect(_snapshot(home) == before, "home repair preview changed state")
    _run(cwd, "repair-home")
    _expect(ignore.read_text() == expected, "home whitelist was not repaired")
    _expect(_git(home, "show", ":.gitignore") == expected, "repair was not staged")
    _expect(_snapshot(checkout) == checkout_before, "home repair changed submodule")
    _expect(_snapshot(flake) == flake_before, "home repair changed unrelated flake")
    _expect(not list(outside.iterdir()), "home repair changed outside path")
    _run(cwd, "repair-home", "--dry-run")


@pytest.mark.parametrize(
    "case",
    ["unknown", "parent", "dirty", "dirty-preview", "untracked", "ignored"],
)
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
    elif case in {"untracked", "ignored"}:
        if case == "ignored":
            exclude = _git(root / relative, "rev-parse", "--git-path", "info/exclude")
            (root / relative / exclude.strip()).write_text("asset\n")
        (root / relative / "asset").write_text("preserve local data")
    before = _snapshot(root)
    arguments = ("--dry-run",) if case == "dirty-preview" else ()
    _run(root, "remove", target, *arguments, code=1)
    _expect(_snapshot(root) == before, "rejected removal changed home state")
    if case.startswith("dirty") or case in {"untracked", "ignored"}:
        _preview(root, "remove", target, "--force")
        _run(root, "remove", target, "--force")
        _expect(not (root / target).exists(), "forced removal left a dirty submodule")


def test_home_submodule_removal_stages_metadata_and_preserves_shared_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preview and remove a repository together with its owned whitelist entries."""
    root = _home_repository(tmp_path)
    monkeypatch.setenv("HOME", str(root))
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
    preview = _run(root, "remove", relative, "--dry-run")
    _expect(relative in preview.stdout and ".gitignore" in preview.stdout, preview)
    _expect(_snapshot(root) == before, "removal preview changed home state")
    _run(root, "remove", relative)
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
    _run(root, "repair-home", "--dry-run")


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


@pytest.mark.parametrize(
    "source",
    [
        'OUTPUT = Path.cwd() / "tmp"',
        'OUTPUT = Path("packages/example/tmp")',
        'ROOT = Path(__file__).parent\nOUTPUT = ROOT / "tmp"',
        'ROOT = Path(os.environ.get("ROOT", Path.cwd()))\nOUTPUT = ROOT / "tmp"',
        'OUTPUT = Path.home() / "forge.example/team/repo/packages/example/tmp"',
        (
            'RUNTIME_DIR = Path.home() / "forge.example/team/repo/packages/example/tmp"'
            '\nOUTPUT = Path.cwd() / "tmp"'
        ),
    ],
)
def test_implicit_runtime_paths_cannot_bypass_declarations(
    source: str,
    tmp_path: Path,
) -> None:
    """Catch missing declarations and unused declarations masking cwd defaults."""
    subject = import_module("packages.afairesi.main")
    package = _make_source_package(tmp_path, "example", "")
    (package / "main.py").write_text(source)
    _, issues = subject.inspect_structure(tmp_path)
    _expect(bool(issues), "implicit runtime output accepted")


def test_python_packages_require_the_shared_constructor() -> None:
    """Reject unsupported builders before convergence changes repository state."""
    with _fresh_repository() as root:
        _run(root, "create", "packages/example", "python")
        definition = root / "packages/example/default.nix"
        definition.write_text(
            "{ pkgs, ... }: pkgs.python3.pkgs.buildPythonPackage { src = ./.; }",
        )
        _git(root, "add", ".")
        before = _snapshot(root)
        result = _repair_flake(root, code=1)
        _expect("shared mkPythonPackage constructor" in result.stderr, result)
        _expect(_snapshot(root) == before, "unsupported builder changed repository")
        (root / "packages/example/ms.tex").touch()
        before = _snapshot(root)
        result = _repair_flake(root, code=1)
        _expect("ambiguous project markers" in result.stderr, result)
        _expect(_snapshot(root) == before, "ambiguous markers changed repository")


@pytest.mark.parametrize(
    "arguments",
    [
        ("move", "packages/example", "hosts/example"),
        ("create", "packages/bad--name", "python"),
        ("create", "hosts/bad-name"),
        ("remove", "../outside"),
        ("remove", "/outside"),
        ("move", "packages/example", "packages/taken"),
        ("create", "packages/example", "python"),
    ],
)
def test_rejected_operations_preserve_contents_modes_index_and_refs(
    arguments: tuple[str, ...],
) -> None:
    """Reject invalid paths and collisions before modifying work."""
    with _fresh_repository() as root:
        _run(root, "create", "packages/example", "python")
        _run(root, "create", "packages/taken", "nix")
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
            _run(root, "create", original, *(() if kind == "host" else (kind,)))
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
            _repair_flake(root)
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
        _preview(root, "move", original, destination)
        _run(root, "move", original, destination)
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
        _repair_flake(root)
        _preview(root, "remove", destination, "--force")
        _run(root, "remove", destination, "--force")
        _expect(
            not (root / destination).exists() and not (root / new_check).exists(),
            "remove left resource or check",
        )
        _expect(
            not _git(root, "ls-files", "--", original, destination, check, new_check),
            "remove left indexed paths",
        )
        _repair_flake(root)
        _preview_flake_repair(root)


@pytest.mark.parametrize(
    ("declaration", "valid"),
    [
        (
            (
                "RUNTIME_DIR = Path.home() / "
                '"forge.example/team/repo/packages/example/tmp"'
            ),
            True,
        ),
        (
            (
                'RUNTIME_DIR = Path.home() / "forge.example" '
                '/ "team/repo/packages/example/tmp"'
            ),
            True,
        ),
        (
            'INPUT_DIRS = (Path.home() / "forge.example/team/repo/packages/data/tmp",)',
            True,
        ),
        ('RUNTIME_DIR = Path.cwd() / "tmp"', False),
        ('RUNTIME_DIR = Path(__file__).parent / "tmp"', False),
        (
            'RUNTIME_DIR = Path.home() / "forge.example/team/repo/packages/other/tmp"',
            False,
        ),
        ('RUNTIME_DIR = Path.home() / "/persistent/example"', False),
        (
            (
                "RUNTIME_DIR = Path.home() / "
                '"forge.example/team/repo/packages/example/../tmp"'
            ),
            False,
        ),
        ('RUNTIME_DIR = Path.home() / os.environ["OUTPUT"]', False),
        (
            'INPUT_DIRS = Path.home() / "forge.example/team/repo/packages/data/tmp"',
            False,
        ),
    ],
)
def test_runtime_directory_declarations_are_checked_without_execution(
    declaration: str,
    tmp_path: Path,
    *,
    valid: bool,
) -> None:
    """Check owned output and explicit inputs without executing declarations."""
    subject = import_module("packages.afairesi.main")
    package = _make_source_package(tmp_path, "example", "")
    source = (
        "from pathlib import Path\n"
        + declaration
        + "\nraise RuntimeError('do not execute')\n"
    )
    (package / "main.py").write_text(source)
    _, issues = subject.inspect_structure(tmp_path)
    _expect(bool(issues) != valid, issues)


def test_runtime_directory_must_match_the_repository_origin(tmp_path: Path) -> None:
    """Reject a valid-looking path that points to a different checkout."""
    subject = import_module("packages.afairesi.main")
    package = _make_source_package(tmp_path, "example", "")
    _git(tmp_path, "init", "--quiet")
    _git(tmp_path, "remote", "add", "origin", "git@forge.example:team/repo.git")
    main = package / "main.py"
    main.write_text(
        "from pathlib import Path\nRUNTIME_DIR = Path.home() / "
        '"forge.example/team/wrong/packages/example/tmp"\n',
    )
    _, issues = subject.inspect_structure(tmp_path)
    _expect(bool(issues), "foreign repository accepted")
    main.write_text(main.read_text().replace("team/wrong", "team/repo"))
    _, issues = subject.inspect_structure(tmp_path)
    _expect(not issues, "canonical runtime directory rejected")


def test_runtime_paths_can_derive_from_declared_directories(tmp_path: Path) -> None:
    """Accept owned outputs and reads through explicit shared inputs."""
    subject = import_module("packages.afairesi.main")
    package = _make_source_package(tmp_path, "example", "")
    (package / "main.py").write_text(
        'RUNTIME_DIR = Path.home() / "forge.example/team/repo/packages/example/tmp"\n'
        'INPUT_DIRS = (Path.home() / "forge.example/team/repo/packages/data/tmp",)\n'
        'OUTPUT = RUNTIME_DIR / "reports"\nDATA = INPUT_DIRS[0] / "data.sqlite3"\n',
    )
    _, issues = subject.inspect_structure(tmp_path)
    _expect(not issues, issues)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("def test_result(): pass", True),
        ("async def test_result(): pass", True),
        ("class TestBehavior:\n    def test_result(self): pass", True),
        (
            (
                "import unittest as unit\n"
                "class Reports(unit.TestCase):\n    def test_result(self): pass"
            ),
            True,
        ),
        (
            (
                "from unittest import IsolatedAsyncioTestCase as Case\n"
                "class Base(Case): pass\n"
                "class Reports(Base):\n    async def test_result(self): pass"
            ),
            True,
        ),
        ("class Helper:\n    def test_hidden(self): pass", False),
        ("def helper():\n    def test_hidden(): pass", False),
        ("class TestEmpty: pass", False),
        ("", False),
    ],
)
def test_static_test_detection_does_not_execute_source(
    tmp_path: Path,
    source: str,
    *,
    expected: bool,
) -> None:
    """Recognize supported test definitions without running imports or bodies."""
    subject = import_module("packages.afairesi.main")
    path = tmp_path / "test_main.py"
    path.write_text("raise RuntimeError('must not execute')\n" + source)
    _expect(subject.has_python_tests(path) == expected, source)

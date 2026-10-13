# Copyright (c) 2026- Paschalis Bizopoulos
"""Check Afairesi's public contracts with explicit regressions and generated cases."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest


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
        "from afairesi import formatter_main; formatter_main()",
        *arguments,
        code=code,
        executable=sys.executable,
    )


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(  # noqa: S603
        ["git", *arguments],  # noqa: S607
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    ).stdout


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
    _run(tmp_path, "init")
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
        '#!/bin/sh\nif [ "$1" = config ]; then printf "nix-command flakes\\n"; '
        'elif [ "$1" = flake ]; then printf "{}\\n" > flake.lock; '
        "else mkdir -p .git/hooks; cat > .git/hooks/pre-push <<'HOOK'\n"
        '#!/bin/sh\ngit -C "$HOME" config --file .gitmodules '
        "--get-regexp '^submodule\\..*\\.path$' >/dev/null\n"
        "HOOK\nchmod +x .git/hooks/pre-push\nfi\n",
        encoding="utf-8",
    )
    fake_nix.chmod(0o755)
    monkeypatch.setenv("AFAIRESI_NIX", str(fake_nix))
    _run(tmp_path, "init")
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


@pytest.mark.parametrize("features", ["", "nix-command", "flakes"])
def test_init_flake_checks_nix_features_before_creating_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    features: str,
) -> None:
    """Reject disabled features with instructions before cloning or staging."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv(
        "NIX_CONFIG",
        f"experimental-features = {features}\nextra-experimental-features =\n",
    )
    _run(tmp_path, "init")
    before = _snapshot(home)
    result = _run(
        tmp_path,
        "init-flake",
        "https://example.test/team/new.git",
        code=1,
    )
    _expect("experimental-features = nix-command flakes" in result.stderr, result)
    _expect(_snapshot(home) == before, "Nix prerequisite failure changed home")
    _expect(not (home / "example.test").exists(), "prerequisite check created paths")


def test_init_flake_reports_missing_nix_before_creating_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explain missing Nix without creating the checkout or changing the index."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AFAIRESI_NIX", str(tmp_path / "missing-nix"))
    _run(tmp_path, "init")
    before = _snapshot(home)
    result = _run(
        tmp_path,
        "init-flake",
        "https://example.test/team/new.git",
        code=1,
    )
    _expect("Nix executable not found" in result.stderr, result)
    _expect(_snapshot(home) == before, "missing Nix changed home")


@pytest.mark.parametrize("command", ["clone", "init-flake"])
@pytest.mark.parametrize("initialized_git", [False, True])
def test_home_prerequisites_explain_initialization_without_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    *,
    initialized_git: bool,
) -> None:
    """Direct missing home setup to home initialization without modifying the home."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    if initialized_git:
        _git(home, "init", "--quiet")
    before = _snapshot(home) if initialized_git else None
    result = _run(
        tmp_path,
        command,
        "https://example.test/team/new.git",
        code=1,
    )
    _expect("afairesi-home init" in result.stderr, result)
    if initialized_git:
        _expect(_snapshot(home) == before, "home prerequisite check changed Git state")
    else:
        _expect(not list(home.iterdir()), "home prerequisite check created files")


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
    _run(root, "repair")
    if source.read_text() != "second":
        message = "convergence changed dirty submodule content"
        raise AssertionError(message)
    _git(checkout, "add", "README")
    _git(checkout, "commit", "--quiet", "-m", "second")
    second = _git(checkout, "rev-parse", "HEAD").strip()
    for published in (False, True):
        if published:
            _git(checkout, "update-ref", "refs/remotes/origin/main", second)
        _run(root, "repair")
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
    preview = _run(root, "repair", "--dry-run")
    if "would write '.gitignore'" not in preview.stdout:
        raise AssertionError(preview.stdout)
    if (
        ignore.read_text(encoding="utf-8") == expected
        or _git(root, "show", ":.gitignore") != before
    ):
        message = "dry-run changed the home whitelist"
        raise AssertionError(message)
    _run(root, "repair")
    if ignore.read_text(encoding="utf-8") != expected:
        raise AssertionError(ignore.read_text(encoding="utf-8"))
    if _git(root, "show", ":.gitignore") != expected:
        message = "convergence did not stage the repaired whitelist"
        raise AssertionError(message)
    _run(root, "repair", "--dry-run")


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
    _run(root, "repair", "--dry-run")
    _run(root, "repair")
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
    rejected = _run(root, "repair", code=1)
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
    rejected = _run(root, "repair", code=1)
    if "stage .gitmodules" not in rejected.stderr or not checkout.exists():
        raise AssertionError(rejected.stderr)
    _git(root, "add", ".gitmodules")
    _run(root, "repair", "--dry-run")
    if not checkout.exists() or target.exists():
        message = "dry-run moved the checkout"
        raise AssertionError(message)
    _run(root, "repair")
    if (
        checkout.exists()
        or _git(target, "status", "--porcelain") != before
        or _git(target, "show", ":README") != indexed
        or (target / "untracked").read_text() != "keep"
        or (root / "unrelated").read_text() != "keep home file"
        or first not in _git(root, "ls-files", "--stage", destination)
        or _git(target, "remote", "get-url", "origin").strip()
        != "git@forge.example:owner/demo"
    ):
        message = "home rename failed to preserve Git state and local remote settings"
        raise AssertionError(message)
    _run(root, "repair", "--dry-run")
    whitelist = (root / ".gitignore").read_text().splitlines()
    _expect(f"!/{relative}" not in whitelist, whitelist)
    _expect(f"!/{destination}" in whitelist, whitelist)


def _expect(condition: bool, detail: object) -> None:  # noqa: FBT001
    """Report an invariant failure with its observed state."""
    if not condition:
        raise AssertionError(detail)


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


def _history_snapshot(root: Path) -> dict[Path, bytes]:
    """Observe stored Git files without requiring a surviving worktree."""
    return {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
        and path.relative_to(root).parts[0] in {"objects", "refs", "logs", "HEAD"}
    }


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
    _run(tmp_path, "init")
    expected = "*\n!/.gitignore\n!/.gitmodules\n"
    _expect((home / ".gitignore").read_text() == expected, "wrong home policy")
    _expect(_git(home, "show", ":.gitignore") == expected, "policy was not staged")
    _expect(
        _git(home, "ls-files").splitlines() == [".gitignore"],
        "home data was staged",
    )
    _expect(not _git(home, "for-each-ref"), "home initialization created a commit")
    _expect(
        local.read_bytes() == b"\x00keep home data\xff",
        "home initialization cleaned HOME",
    )
    before = _snapshot(home)
    _run(tmp_path, "init")
    _expect(_snapshot(home) == before, "repeated home initialization changed state")
    _expect(
        not (tmp_path / ".git").exists(),
        "home initialization initialized the working directory",
    )


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


def test_home_repair_rejects_collision_with_missing_checkout_registration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject a move onto a missing registered path before changing home state."""
    root = _home_repository(tmp_path)
    monkeypatch.setenv("HOME", str(root))
    relative = "forge.example/owner/demo"
    destination = "forge.example/owner/renamed"
    commit = _git(root / relative, "rev-parse", "HEAD").strip()
    _git(root, "update-index", "--add", "--cacheinfo", "160000", commit, destination)
    _git(
        root,
        "config",
        "--file",
        ".gitmodules",
        f"submodule.{relative}.url",
        "git@forge.example:owner/renamed",
    )
    _git(
        root,
        "config",
        "--file",
        ".gitmodules",
        f"submodule.{destination}.path",
        destination,
    )
    _git(
        root,
        "config",
        "--file",
        ".gitmodules",
        f"submodule.{destination}.url",
        "git@forge.example:owner/elsewhere",
    )
    _git(root, "add", ".gitmodules")
    before = _snapshot(root)
    for arguments in [("--dry-run",), ()]:
        result = _run(root, "repair", *arguments, code=1)
        _expect(
            "conflicts with a missing checkout registration" in result.stderr,
            result,
        )
        _expect(_snapshot(root) == before, "rejected repair changed home state")


@pytest.mark.parametrize("destination", ["demo", "renamed"])
@pytest.mark.parametrize("state", ["deinitialized", "missing", "placeholder"])
def test_home_repair_preserves_missing_checkouts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    destination: str,
    state: str,
) -> None:
    """Repair the whitelist without initializing or relocating missing checkouts."""
    root = _home_repository(tmp_path)
    monkeypatch.setenv("HOME", str(root))
    relative = "forge.example/owner/demo"
    checkout = root / relative
    _git(root, "submodule", "absorbgitdirs", "--", relative)
    _git(root, "config", f"submodule.{relative}.url", "git@mirror.example:owner/demo")
    _fixture_git(root, "commit", "-qm", "Home snapshot")
    if state == "deinitialized":
        _git(root, "submodule", "deinit", "--", relative)
    else:
        shutil.rmtree(checkout)
        if state == "placeholder":
            checkout.mkdir()
            (checkout / "README").write_text("unrelated local data")
    _git(
        root,
        "config",
        "--file",
        ".gitmodules",
        f"submodule.{relative}.url",
        f"git@forge.example:owner/{destination}",
    )
    modules_before = (root / ".gitmodules").read_bytes()
    gitlink_before = _git(root, "ls-files", "--stage", "--", relative)
    config_before = _git(root, "config", "--local", "--list")
    history = root / ".git/modules" / relative
    history_before = _history_snapshot(history)
    ignore = root / ".gitignore"
    expected_ignore = ignore.read_text()
    ignore.write_text("*\n!/.gitignore\n!/.gitmodules\n")
    before = _snapshot(root)
    _run(root, "repair", "--dry-run")
    _expect(_snapshot(root) == before, "repair preview changed missing checkout state")
    _run(root, "repair")
    _expect(not (checkout / ".git").exists(), "repair initialized a missing checkout")
    _expect(ignore.read_text() == expected_ignore, "repair failed to restore whitelist")
    _expect(
        (root / ".gitmodules").read_bytes() == modules_before,
        "repair moved missing checkout registration",
    )
    _expect(
        _git(root, "ls-files", "--stage", "--", relative) == gitlink_before,
        "repair changed missing checkout gitlink",
    )
    _expect(
        _git(root, "config", "--local", "--list") == config_before,
        "repair changed local registration",
    )
    _expect(
        _history_snapshot(history) == history_before,
        "repair changed stored history",
    )
    if state == "placeholder":
        _expect(
            (checkout / "README").read_text() == "unrelated local data",
            "repair changed placeholder data",
        )
    elif state == "missing":
        _expect(not checkout.exists(), "repair recreated missing checkout directory")
    if destination != "demo":
        _expect(
            not (root / f"forge.example/owner/{destination}").exists(),
            "repair relocated a missing checkout",
        )
    after = _snapshot(root)
    _run(root, "repair")
    _expect(_snapshot(root) == after, "repeated repair changed missing checkout state")


@pytest.mark.parametrize("destination", ["demo", "renamed"])
@pytest.mark.parametrize("remote", ["fork", "missing"])
def test_home_repair_preserves_local_remote_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    destination: str,
    remote: str,
) -> None:
    """Use declared URLs for layout while preserving independent local remotes."""
    root = _home_repository(tmp_path)
    monkeypatch.setenv("HOME", str(root))
    relative = "forge.example/owner/demo"
    checkout = root / relative
    _git(root, "submodule", "absorbgitdirs", "--", relative)
    local_url = "git@mirror.example:owner/demo"
    _git(root, "config", f"submodule.{relative}.url", local_url)
    if remote == "missing":
        _git(checkout, "remote", "remove", "origin")
    else:
        _git(checkout, "remote", "set-url", "origin", "git@forge.example:me/fork")
        _git(checkout, "remote", "set-url", "--push", "origin", local_url)
    expected = f"forge.example/owner/{destination}"
    declared_url = f"git@forge.example:owner/{destination}"
    _git(
        root,
        "config",
        "--file",
        ".gitmodules",
        f"submodule.{relative}.url",
        declared_url,
    )
    _git(root, "add", ".gitmodules")
    before = _snapshot(root)
    checkout_before = _snapshot(checkout)
    remotes_before = {
        line
        for line in _git(checkout, "config", "--local", "--list").splitlines()
        if line.startswith("remote.")
    }
    _run(root, "repair", "--dry-run")
    _expect(_snapshot(root) == before, "home repair preview changed state")
    _expect(_snapshot(checkout) == checkout_before, "preview changed checkout settings")
    _run(root, "repair")
    target = root / expected
    _expect(target.is_dir(), "repair did not use the declared URL for placement")
    remotes_after = {
        line
        for line in _git(target, "config", "--local", "--list").splitlines()
        if line.startswith("remote.")
    }
    _expect(remotes_after == remotes_before, "repair changed local remote settings")
    _expect(
        _snapshot(target)[:3] == checkout_before[:3],
        "repair changed checkout state",
    )
    _expect(
        _git(root, "config", "--local", "--get", f"submodule.{expected}.url").strip()
        == local_url,
        "repair overwrote the local submodule URL override",
    )
    subject = import_module("packages.afairesi-home.main")
    _expect(
        subject.home_submodules(root)[0]["url"] == declared_url,
        "repair changed declared URL",
    )


def test_home_repair_leaves_nested_submodule_settings_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preserve direct and nested remote settings during home repair."""
    root = _home_repository(tmp_path)
    monkeypatch.setenv("HOME", str(root))
    relative = "forge.example/owner/demo"
    checkout = root / relative
    nested = checkout / "nested"
    nested.mkdir()
    _git(nested, "init", "--quiet")
    (nested / "README").write_text("nested repository")
    _git(nested, "add", "README")
    _fixture_git(nested, "commit", "-qm", "Nested snapshot")
    mirror = "git@mirror.example:owner/child"
    _git(nested, "remote", "add", "origin", mirror)
    (checkout / ".gitmodules").write_text(
        '[submodule "nested"]\npath = nested\nurl = git@forge.example:owner/child\n',
    )
    _git(checkout, "add", ".gitmodules", "nested")
    _fixture_git(checkout, "commit", "-qm", "Register nested repository")
    _git(checkout, "config", "submodule.nested.url", mirror)
    _git(checkout, "config", "submodule.nested.active", "true")
    _git(root, "submodule", "absorbgitdirs", "--", relative)
    _git(root, "add", relative)
    _fixture_git(root, "commit", "-qm", "Home snapshot")
    _git(root, "config", f"submodule.{relative}.url", "git@forge.example:owner/demo")
    _git(checkout, "remote", "set-url", "origin", "git@mirror.example:owner/demo")
    before = _snapshot(root)
    checkout_before = _snapshot(checkout)
    nested_before = _snapshot(nested)
    _run(root, "repair", "--dry-run")
    _expect(_snapshot(root) == before, "home repair preview changed state")
    _expect(_snapshot(checkout) == checkout_before, "preview changed checkout settings")
    _expect(_snapshot(nested) == nested_before, "preview changed nested repository")
    _run(root, "repair")
    _expect(
        _git(checkout, "remote", "get-url", "origin").strip()
        == "git@mirror.example:owner/demo",
        "home repair changed the direct submodule remote",
    )
    _expect(
        _git(checkout, "config", "--local", "--get", "submodule.nested.url").strip()
        == mirror,
        "home repair changed nested submodule registration",
    )
    _expect(_snapshot(nested) == nested_before, "home repair changed nested repository")


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
        _expect("run afairesi-home repair" in rejected.stderr, rejected)
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
        result = _run(root, "repair", code=1)
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
    preview = _run(cwd, "repair", "--dry-run")
    _expect("would write '.gitignore'" in preview.stdout, preview)
    _expect(_snapshot(home) == before, "home repair preview changed state")
    _run(cwd, "repair")
    _expect(ignore.read_text() == expected, "home whitelist was not repaired")
    _expect(_git(home, "show", ":.gitignore") == expected, "repair was not staged")
    _expect(_snapshot(checkout) == checkout_before, "home repair changed submodule")
    _expect(_snapshot(flake) == flake_before, "home repair changed unrelated flake")
    _expect(not list(outside.iterdir()), "home repair changed outside path")
    _run(cwd, "repair", "--dry-run")


@pytest.mark.parametrize(
    "case",
    ["unknown", "parent", "dirty", "dirty-preview", "untracked", "ignored"],
)
def test_home_submodule_removal_rejects_invalid_or_dirty_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    """Reject ambiguous paths and local changes without changing home state."""
    root = _home_repository(tmp_path)
    monkeypatch.setenv("HOME", str(root))
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
    _run(root, "rm", target, *arguments, code=1)
    _expect(_snapshot(root) == before, "rejected removal changed home state")
    if case.startswith("dirty") or case in {"untracked", "ignored"}:
        _preview(root, "rm", target, "--force")
        _run(root, "rm", target, "--force")
        _expect(not (root / target).exists(), "forced removal left a dirty submodule")


@pytest.mark.parametrize(
    "location",
    ["home", "nested", "submodule", "flake", "outside"],
)
@pytest.mark.parametrize("name", ["forge.example/owner/demo", "legacy-name"])
def test_home_submodule_removal_stages_metadata_and_preserves_shared_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    location: str,
) -> None:
    """Preview and remove a repository together with its owned whitelist entries."""
    root = _home_repository(tmp_path / "home")
    monkeypatch.setenv("HOME", str(root))
    relative = "forge.example/owner/demo"
    flake = tmp_path / "flake"
    flake.mkdir()
    _git(flake, "init", "--quiet")
    (flake / "flake.nix").touch()
    outside = tmp_path / "outside"
    outside.mkdir()
    cwd = {
        "home": root,
        "nested": (root / relative).parent,
        "submodule": root / relative,
        "flake": flake,
        "outside": outside,
    }[location]
    flake_before = _snapshot(flake)
    _git(root, "submodule", "absorbgitdirs")
    if name != relative:
        _git(
            root,
            "config",
            "--file",
            ".gitmodules",
            "--rename-section",
            f"submodule.{relative}",
            f"submodule.{name}",
        )
        _git(root, "add", ".gitmodules")
    _git(root, "config", f"submodule.{name}.url", "git@forge.example:owner/demo")
    _git(root, "config", f"submodule.{name}.active", "true")
    _git(root, "config", "submodule.unrelated.active", "false")
    history = root / ".git/modules" / relative
    history_before = _history_snapshot(history)
    ignore = root / ".gitignore"
    ignore.write_text(
        ignore.read_text()
        + f"!/{relative}/\n!/{relative}/README\n"
        + "!/forge.example/owner/demo-other\n!/.ssh/\n",
    )
    _git(root, "add", ".gitignore")
    _fixture_git(root, "commit", "-qm", "Home snapshot")
    before = _snapshot(root)
    preview = _run(cwd, "rm", relative, "--dry-run")
    _expect(relative in preview.stdout and ".gitignore" in preview.stdout, preview)
    _expect(_snapshot(root) == before, "removal preview changed home state")
    _run(cwd, "rm", relative)
    _expect(_snapshot(flake) == flake_before, "removal changed unrelated flake")
    _expect(not list(outside.iterdir()), "removal changed outside path")
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
    subject = import_module("packages.afairesi-home.main")
    _expect(subject.home_submodules(root) == [], "submodule remains registered")
    _expect(
        _git(root, "config", "--local", "--get-regexp", r"^submodule\.").strip()
        == "submodule.unrelated.active false",
        "removal left local registration or changed unrelated settings",
    )
    _expect(
        _history_snapshot(history) == history_before,
        "removal changed stored history",
    )
    staged = _git(root, "diff", "--cached", "--name-only").splitlines()
    _expect(set(staged) == {".gitignore", ".gitmodules", relative}, staged)
    _expect(not _git(root, "diff", "--name-only"), "metadata was not staged")
    _run(root, "repair", "--dry-run")


@pytest.mark.parametrize("registered", [True, False])
@pytest.mark.parametrize("name", ["forge.example/owner/demo", "legacy-name"])
def test_home_repair_preserves_unrelated_local_submodule_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    *,
    registered: bool,
) -> None:
    """Keep unrelated local registrations while repairing convention metadata."""
    root = _home_repository(tmp_path)
    monkeypatch.setenv("HOME", str(root))
    relative = "forge.example/owner/demo"
    _git(root, "submodule", "absorbgitdirs")
    if name != relative:
        _git(
            root,
            "config",
            "--file",
            ".gitmodules",
            "--rename-section",
            f"submodule.{relative}",
            f"submodule.{name}",
        )
        _git(root, "add", ".gitmodules")
    else:
        _git(root, "config", f"submodule.{name}.url", "git@forge.example:owner/demo")
    _git(root, "config", f"submodule.{name}.active", "true")
    _git(root, "config", f"submodule.{name}.update", "none")
    _git(root, "config", "submodule.active", "forge.example/*")
    _git(
        root,
        "config",
        "submodule.removed.with.dots.url",
        "git@forge.example:old/repo",
    )
    _git(root, "config", "submodule.active-only.active", "true")
    _fixture_git(root, "commit", "-qm", "Home snapshot")
    if not registered:
        _git(root, "rm", "--", relative)
        (root / ".gitmodules").unlink()
    history = root / ".git/modules" / relative
    history_before = _history_snapshot(history)
    before = _snapshot(root)
    _run(root, "repair", "--dry-run")
    _expect(_snapshot(root) == before, "repair preview changed home state")
    _run(root, "repair")
    submodule_settings = {
        line
        for line in _git(root, "config", "--local", "--list").splitlines()
        if line.startswith("submodule.") and not line.startswith("submodule.active=")
    }
    expected_name = relative if registered else name
    expected_settings = {
        f"submodule.{expected_name}.active=true",
        f"submodule.{expected_name}.update=none",
        "submodule.removed.with.dots.url=git@forge.example:old/repo",
        "submodule.active-only.active=true",
    }
    if name == relative:
        expected_settings.add(f"submodule.{relative}.url=git@forge.example:owner/demo")
    _expect(
        submodule_settings == expected_settings,
        "repair changed unrelated registrations or lost active settings",
    )
    _expect(
        _git(root, "config", "--local", "--get", "submodule.active").strip()
        == "forge.example/*",
        "repair changed shared submodule settings",
    )
    if registered:
        _expect(
            _git(
                root,
                "config",
                "--local",
                "--get",
                f"submodule.{relative}.update",
            ).strip()
            == "none",
            "repair changed active submodule settings",
        )
    _expect(
        _history_snapshot(history) == history_before,
        "repair changed stored history",
    )
    after = _snapshot(root)
    _run(root, "repair")
    _expect(_snapshot(root) == after, "repeated repair changed home state")


def test_home_submodule_whitelist_move_preserves_unrelated_entries(
    tmp_path: Path,
) -> None:
    """Preview moves safely and migrate subtree rules without touching sibling paths."""
    subject = import_module("packages.afairesi-home.main")
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


def test_home_cli_exposes_only_home_lifecycle_commands(tmp_path: Path) -> None:
    """Keep home lifecycle commands separate from flake resource management."""
    commands = ("clone", "init", "init-flake", "repair", "rm")
    help_output = _run(tmp_path, "--help").stdout
    _expect(_run(tmp_path).stdout == help_output, help_output)
    positions = [help_output.index(f"    {name} ") for name in commands]
    _expect(positions == sorted(positions), help_output)
    for command in commands:
        _expect("usage:" in _run(tmp_path, command, "--help").stdout, command)
    for arguments in (
        ("create",),
        ("move",),
        ("remove", "github.com/example/repository"),
        ("clone",),
        ("init-flake",),
        ("init", "unexpected"),
    ):
        _run(tmp_path, *arguments, code=2)
    _expect(not list(tmp_path.iterdir()), "invalid requests created files")


def test_home_commands_reject_flake_repositories_without_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prevent home lifecycle commands from modifying a flake repository."""
    _git(tmp_path, "init", "--quiet")
    (tmp_path / "flake.nix").touch()
    monkeypatch.setenv("HOME", str(tmp_path))
    before = _snapshot(tmp_path)
    for arguments in (("init",), ("repair",), ("rm", "packages/example")):
        _run(tmp_path, *arguments, code=1)
        _expect(_snapshot(tmp_path) == before, "home command changed flake state")

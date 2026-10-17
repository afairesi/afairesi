# Copyright (c) 2026- Paschalis Bizopoulos
"""Check Afairesi's public contracts with explicit regressions and generated cases."""

from __future__ import annotations

import ast
import json
import keyword
import os
import pstats
import re
import shutil
import stat
import subprocess
import sys
from contextlib import contextmanager
from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import DEFAULT, Mock

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
        [
            executable or os.environ["PACKAGE_E2E_EXECUTABLE"],
            *(["repo"] if executable in (None, "afairesi") else []),
            *arguments,
        ],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode != code:
        msg = (
            f"expected exit {code}, got {result.returncode}\n"
            f"{result.stdout}{result.stderr}"
        )
        raise AssertionError(
            msg,
        )
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


def _fixture_add(root: Path, resource: str, kind: str | None = None) -> None:
    """Prepare resources directly when the test exercises another operation."""
    subject = import_module("packages.afairesi.main")
    collection, name = Path(resource).parts
    if collection == "hosts":
        subject.add_host(root, name)
    else:
        if kind is None:
            message = "package fixtures require a kind"
            raise AssertionError(message)
        subject.add_package(root, kind, name)


def _check_excluded_trees(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Skip excluded subtrees while still rejecting unmanaged files and symlinks."""
    _fixture_add(repository, "packages/example", "python")
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
    _fixture_add(repository, "hosts/laptop")
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
        _repair_flake(repository)
        _expect(
            check.read_text(encoding="utf-8") == formatted
            and _git(repository, "ls-files", "--stage") == index,
            f"convergence regenerated formatted {relative}",
        )


def _check_host_repair(repository: Path) -> None:
    """Repair a changed check while preserving its host and resource inventory."""
    _fixture_add(repository, "hosts/laptop")
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


def _expect(condition: bool, detail: object) -> None:  # noqa: FBT001
    """Report an invariant failure with its observed state."""
    if not condition:
        raise AssertionError(detail)


def _repository(root: Path, *, object_format: str = "sha1") -> Path:
    """Index a minimal flake in an existing temporary directory."""
    _git(root, "init", "--quiet", f"--object-format={object_format}")
    for name in (".gitignore", "flake.nix", "flake.lock", "LICENSE"):
        (root / name).write_text("", encoding="utf-8")
    _git(root, "add", ".")
    return root


@contextmanager
def _fresh_repository(*, object_format: str = "sha1") -> Iterator[Path]:
    """Give every generated example its own repository and cleanup."""
    with TemporaryDirectory(prefix="afairesi-contract-") as directory:
        yield _repository(Path(directory), object_format=object_format)


def _git_metadata_snapshot(root: Path) -> tuple[dict[str, bytes], str]:
    """Read stored refs and local settings without launching Git for each field."""
    directory = root / ".git"
    if directory.is_file():
        directory = root / directory.read_text().removeprefix("gitdir: ").strip()
    common = directory
    if (directory / "commondir").is_file():
        common = directory / (directory / "commondir").read_text().strip()
    refs = {
        path.relative_to(common).as_posix(): path.read_bytes()
        for path in (common / "refs").rglob("*")
        if path.is_file()
    }
    if (common / "packed-refs").is_file():
        refs["packed-refs"] = (common / "packed-refs").read_bytes()
    refs["HEAD"] = (directory / "HEAD").read_bytes()
    return refs, (common / "config").read_text()


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
        *_git_metadata_snapshot(root),
    )


@pytest.mark.parametrize(
    "arguments",
    [
        ("config", "fixture.changed", "true"),
        ("update-ref", "refs/heads/other", "HEAD"),
        ("pack-refs", "--all"),
        ("symbolic-ref", "HEAD", "refs/heads/other"),
    ],
)
def test_repository_snapshots_detect_git_metadata_changes(
    tmp_path: Path,
    arguments: tuple[str, ...],
) -> None:
    """Keep preservation assertions sensitive to refs, HEAD, and local settings."""
    _repository(tmp_path)
    _fixture_git(tmp_path, "commit", "--quiet", "-m", "initial")
    before = _snapshot(tmp_path)
    _git(tmp_path, *arguments)
    _expect(_snapshot(tmp_path) != before, arguments)


def _preview(root: Path, *arguments: str, code: int = 0) -> None:
    """Preserve the whole repository during successful and rejected previews."""
    before = _snapshot(root)
    _run(root, *arguments, "--dry-run", code=code)
    _expect(_snapshot(root) == before, arguments)


PACKAGE_NAMES = st.from_regex(
    r"[a-z][a-z0-9]{0,5}(?:[-_][a-z0-9]{1,5})?",
    fullmatch=True,
).filter(lambda name: not keyword.iskeyword(name.replace("-", "_")))
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
        "substituters =\n"
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
        arguments = () if kind == "host" else (kind,)
        _run(root, "add", relative, *arguments)
        resource = root / relative
        _expect(
            {path.name for path in resource.iterdir()} == filenames,
            "wrong resource files",
        )
        staged = {f"{relative}/{name}" for name in filenames} | {".gitignore"}
        if kind == "host":
            staged.add("checks/exampleVmWithDisko/default.nix")
        else:
            descriptions = {
                "python": "A Python package.",
                "html": "An HTML package.",
                "latex": "A LaTeX package.",
                "nix": "A Nix package.",
            }
            _expect(
                f'meta.description = "{descriptions[kind]}";'
                in (resource / "default.nix").read_text(),
                "create lost the default description",
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
        (("packages/example", "unknown"), "invalid choice"),
        (("packages/example", "python", "unexpected"), "unrecognized arguments"),
        (
            ("packages/example", "python", "--unknown-option"),
            "unrecognized arguments",
        ),
        (
            ("hosts/example", "python"),
            "host creation does not accept TYPE",
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
        result = _run(root, "add", *arguments, code=2)
        _expect(message in result.stderr, result)
        _expect(_snapshot(root) == before, "invalid create changed state")


def test_invalid_resource_arguments_are_usage_errors_outside_git(
    tmp_path: Path,
) -> None:
    """Reject malformed resource requests before requiring a Git repository."""
    for arguments in (
        ("add", "packages/example"),
        ("add", "hosts/example", "python"),
        ("add", "packages/example", "unknown"),
        ("rm", "other/example"),
        ("mv", "packages/example", "hosts/example"),
    ):
        result = _run(tmp_path, *arguments, code=2)
        _expect("not inside a Git repository" not in result.stderr, result.stderr)
    _expect(not any(tmp_path.iterdir()), "invalid arguments created files")


@pytest.mark.parametrize("kind", ["python", "html", "latex", "nix", "host"])
def test_add_targets_repository_root_and_rejects_existing_resources(kind: str) -> None:
    """Add from a nested cwd and reject existing resources without changes."""
    with _fresh_repository() as root:
        nested = root / "tmp/nested"
        nested.mkdir(parents=True)
        relative = "hosts/example" if kind == "host" else "packages/example"
        arguments = () if kind == "host" else (kind,)
        _run(nested, "add", relative, *arguments)
        _expect(
            (root / relative).is_dir(),
            "add used the working directory as its root",
        )
        before = _snapshot(root)
        _run(nested, "add", relative, *arguments, code=1)
        _expect(_snapshot(root) == before, "conflicting addition changed state")


@pytest.mark.parametrize(
    "tests",
    [
        "def test_example(): pass\n",
        (
            "class Cases:\n    def test_result(self):\n        assert True\n"
            "class TestExample(Cases):\n    pass\n"
        ),
    ],
)
def test_public_repo_repair_generates_checks_and_reaches_a_fixed_point(
    tests: str,
) -> None:
    """Repair from nested directories and preview without changing Git state."""
    with _fresh_repository() as root:
        nested = root / "tmp/nested"
        nested.mkdir(parents=True)
        _fixture_add(root, "packages/example", "python")
        (root / "packages/example/test_main.py").write_text(tests)
        before = _snapshot(root)
        result = _run(nested, "fix", "-n")
        _expect("checks/example/default.nix" in result.stdout, result.stdout)
        _expect(_snapshot(root) == before, "repair preview changed state")
        _run(nested, "fix")
        _expect(
            (root / "checks/example/default.nix").is_file(),
            "repair omitted the check",
        )
        after = _snapshot(root)
        _run(nested, "fix", "--dry-run")
        _expect(_snapshot(root) == after, "converged repair changed state")


def test_explicit_repository_paths_use_git_root_or_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolve paths predictably inside and outside Git, including absolute paths."""
    subject = import_module("packages.afairesi.main")
    monkeypatch.chdir(tmp_path)
    _expect(
        subject.resolve_repository_path(Path("example")) == tmp_path / "example",
        tmp_path,
    )
    _fixture_git(tmp_path, "init", "--quiet")
    nested = tmp_path / "prm/nested"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    _expect(subject.resolve_repository_path(Path()) == tmp_path, tmp_path)
    _expect(
        subject.resolve_repository_path(Path("packages/example"))
        == tmp_path / "packages/example",
        tmp_path,
    )
    _expect(subject.resolve_repository_path(nested) == nested, nested)


def test_cli_help_lists_current_commands_and_rejects_invalid_requests() -> None:
    """Expose consistent commands and reject invalid requests before creating state."""
    with TemporaryDirectory(prefix="afairesi-cli-") as directory:
        root = Path(directory)
        commands = (
            (),
            ("init",),
            ("add",),
            ("mv",),
            ("fix",),
            ("rm",),
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
        _run(root, "unknown-command", code=2)
        _run(root, "add", code=2)
        initialization = _run(root, "init", code=1)
        _expect("not inside a Git repository" in initialization.stderr, initialization)
        help_output = _run(root, "--help").stdout
        command_names = ["init", "add", "mv", "rm", "fix", "inspect", "test"]
        positions = [help_output.index(f"    {name} ") for name in command_names]
        _expect(
            positions == sorted(positions),
            "help commands must follow the repository workflow",
        )
        _expect(_run(root).stdout == help_output, "bare afairesi must show help")
        _expect(not any(root.iterdir()), "invalid CLI request created state")


@pytest.mark.parametrize("kind", ["python", "host"])
@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize(
    "change",
    ["working", "staged", "untracked", "ignored", "check", "nested"],
)
def test_resource_removal_uses_git_rules_and_preserves_untracked_data(
    kind: str,
    change: str,
    *,
    force: bool,
) -> None:
    """Let Git protect tracked edits and leave untracked and ignored files intact."""
    with _fresh_repository() as root:
        relative = "hosts/example" if kind == "host" else "packages/example"
        _fixture_add(root, relative, *(() if kind == "host" else (kind,)))
        if kind == "python":
            (root / relative / "test_main.py").write_text("def test_result(): pass\n")
            _repair_flake(root)
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
        arguments = ("--force",) if force else ()
        if change in {"working", "staged"} and not force:
            rejected = _run(root, "rm", relative, code=1)
            _expect("-f" in rejected.stderr, rejected)
            _expect(_snapshot(root) == before, "rejected removal changed state")
            return
        _run(root, "rm", relative, *arguments)
        if change in {"working", "staged"}:
            _expect(not resource.exists(), "forced removal left tracked resource")
        else:
            _expect(
                path.read_text() == "preserve local data",
                "removal lost local data",
            )
            marker = "configuration.nix" if kind == "host" else "default.nix"
            _expect(
                not (resource / marker).exists(),
                "removal left tracked resource marker",
            )


@pytest.mark.parametrize("force", [False, True])
def test_resource_removal_rejects_untracked_resources(
    *,
    force: bool,
) -> None:
    """Preserve Git's unmatched-path error without deleting untracked resources."""
    with _fresh_repository() as root:
        resource = root / "packages/example"
        resource.mkdir(parents=True)
        (resource / "default.nix").write_text("{}\n")
        arguments = []
        if force:
            arguments.append("--force")
        before = _snapshot(root)
        rejected = _run(root, "rm", "packages/example", *arguments, code=128)
        _expect("did not match any files" in rejected.stderr, rejected)
        _expect(_snapshot(root) == before, "failed removal changed untracked resource")


def test_resource_commands_resolve_paths_from_the_repository_root() -> None:
    """Resolve resource paths from the repository root from nested directories."""
    with _fresh_repository() as root:
        nested = root / "tmp/nested"
        nested.mkdir(parents=True)
        _run(nested, "add", "packages/example", "nix")
        _expect((root / "packages/example/default.nix").is_file(), "wrong create root")
        _run(nested, "mv", "packages/example", "packages/renamed")
        _fixture_git(root, "commit", "-qm", "Renamed resource")
        _run(nested, "rm", "packages/renamed")
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
        _run(root, "add", "packages/example", "nix", code=1)
        _expect(_snapshot(root) == before, "rejected creation changed repository state")
        _expect(not any(outside.iterdir()), "creation wrote outside the repository")


@pytest.mark.parametrize(
    "relative",
    ["checks", "checks/example", "checks/example/default.nix"],
)
@pytest.mark.parametrize("dry_run", [False, True])
def test_repair_rejects_check_symlinks_before_changing_state(
    tmp_path: Path,
    relative: str,
    *,
    dry_run: bool,
) -> None:
    """Reject check symlinks before writing templates or changing the index."""
    outside = tmp_path / "outside"
    outside.mkdir()
    external = outside / "default.nix"
    external.write_text("{}\n")
    with _fresh_repository() as root:
        _fixture_add(root, "packages/example", "python")
        (root / "packages/example/test_main.py").write_text(
            "def test_example(): pass\n",
        )
        _run(root, "fix")
        path = root / relative
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        path.symlink_to(external if path.suffix else outside)
        before = _snapshot(root)
        rejected = _run(root, "fix", *(("--dry-run",) if dry_run else ()), code=1)
        _expect("managed layout" in rejected.stderr, rejected)
        _expect(_snapshot(root) == before, "rejected repair changed repository state")
        _expect(external.read_text() == "{}\n", "repair wrote outside the repository")


@pytest.mark.parametrize("command", ["mv", "rm"])
@pytest.mark.parametrize("invalid", ["tests", "whitelist"])
def test_resource_changes_validate_metadata_before_mutation(
    command: str,
    invalid: str,
) -> None:
    """Leave resources and their index entries intact when metadata cannot refresh."""
    with _fresh_repository() as root:
        _run(root, "add", "packages/example", "python")
        _run(root, "add", "packages/other", "python")
        _run(root, "fix")
        _fixture_git(root, "commit", "-qm", "Resources")
        if invalid == "tests":
            (root / "packages/other/test_main.py").write_text("def test_broken(:\n")
        else:
            (root / ".gitignore").unlink()
            (root / ".gitignore").symlink_to("LICENSE")
        before = _snapshot(root)
        arguments = ("packages/renamed",) if command == "mv" else ()
        _run(root, command, "packages/example", *arguments, code=1)
        _expect(_snapshot(root) == before, "failed resource change left partial state")


def test_removal_can_delete_a_package_with_invalid_test_source() -> None:
    """Validate the remaining metadata without requiring removed tests to parse."""
    with _fresh_repository() as root:
        _fixture_add(root, "packages/example", "python")
        (root / "packages/example/test_main.py").write_text(
            "def test_example(): pass\n",
        )
        _run(root, "fix")
        _fixture_git(root, "commit", "-qm", "Resource")
        (root / "packages/example/test_main.py").write_text("def test_broken(:\n")
        _run(root, "rm", "packages/example", "--force")
        _expect(
            not (root / "packages/example").exists(),
            "broken package was not removed",
        )


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
        failed = _run(
            root,
            "add",
            resource,
            *(() if kind == "host" else (kind,)),
            code=128,
        )
        _expect("index.lock" in failed.stderr, failed)
        _expect(_snapshot(root) == before, "failed creation left structural changes")


@pytest.mark.parametrize("case", ["symlink", "file"])
def test_removal_validates_all_targets_before_deletion(
    tmp_path: Path,
    case: str,
) -> None:
    """Reject unsupported related checks before deleting the package."""
    with _fresh_repository() as root:
        _fixture_add(root, "packages/example", "nix")
        check = root / "checks/example"
        check.parent.mkdir()
        if case == "symlink":
            check.symlink_to(tmp_path, target_is_directory=True)
        else:
            check.write_text("preserve check contents")
        before = _snapshot(root)
        _run(root, "rm", "packages/example", "--force", code=1)
        _expect(_snapshot(root) == before, "rejected removal deleted local files")


@pytest.mark.parametrize("style", ["literal", "split", "annotated", "concatenated"])
def test_rename_updates_runtime_paths_without_staging_unrelated_changes(
    style: str,
) -> None:
    """Move runtime data and update owned and input paths while preserving Git state."""
    with _fresh_repository() as root:
        _git(root, "remote", "add", "origin", "https://forge.example/team/repo.git")
        for name in ("old", "consumer"):
            _fixture_add(root, f"packages/{name}", "python")
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
        _run(root, "mv", "packages/old", "packages/new")
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
        _packages, issues = subject.inspect_structure(root)
        issues = [issue for issue in issues if issue.startswith("packages/new/")]
        _expect(
            not issues,
            issues,
        )


def test_convergence_preserves_formatted_checks() -> None:
    """Keep generated checks stable after the real formatting pipeline."""
    with _fresh_repository() as root:
        _fixture_add(root, "packages/example", "python")
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
        _fixture_add(root, "packages/example", "python")
        source = root / "packages/example/main.py"
        original = source.read_bytes()
        for relative in (
            "tmp/root-state",
            "packages/example/tmp/package-state",
            "prm/nested/asset.bin",
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
            "prm/nested/asset.bin" in indexed
            and "packages/example/prm/nested/asset.bin" in indexed
            and not any("tmp/" in path for path in indexed),
            indexed,
        )
        for relative in (
            "prm/nested/asset.bin",
            "packages/example/prm/nested/asset.bin",
        ):
            _expect(
                _git(root, "rev-parse", f":{relative}")
                == _git(root, "hash-object", relative),
                f"{relative}: staged contents differ from working contents",
            )
        for relative in (
            "tmp/root-state",
            "packages/example/tmp/package-state",
            "prm/nested/asset.bin",
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
        _fixture_add(root, "packages/example", "python")
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
    "definition",
    [
        'pkgs.runCommand "integration" {} "python -m pytest ./prm/test_live.py"',
        'pkgs.runCommand "site" {} "cp -r ./prm/site $out"',
        'pkgs.runCommandLocal "data" {} "touch $out"',
        'pkgs.stdenv.mkDerivation { name = "parallel"; src = ./prm; }',
        'pkgs.python3.pkgs.buildPythonPackage { pname = "parallel"; src = ./prm; }',
        "inputs.afairesi.lib.mkPythonPackage { inherit pkgs; src = ./prm; }",
        "inputs.afairesi.lib.mkPythonCheck { inherit pkgs; packageDrv = base.foo; }",
        "inputs.nixpkgs.lib.nixosSystem { modules = [ ./prm/host.nix ]; }",
        'builtins.derivation { name = "parallel"; builder = "/bin/sh"; }',
        '(let build = pkgs.runCommand; in build "parallel" {} "touch $out")',
    ],
)
def test_flake_resource_ownership_rejects_parallel_definitions(
    definition: str,
) -> None:
    """Reject independently defined resources before repair or source validation."""
    with _fresh_repository() as root:
        _repair_flake(root)
        (root / "flake.nix").write_text(
            "{ outputs = inputs: let base = inputs.afairesi.blueprint "
            "{ inherit inputs; }; pkgs = inputs.nixpkgs.legacyPackages.x86_64-linux; "
            f"in base // {{ checks.x86_64-linux.parallel = {definition}; }}; }}",
        )
        (root / "prm").mkdir()
        (root / "prm/test_live.py").write_text("def test_live(): pass\n")
        (root / "discarded").write_text("preserve until ownership is resolved")
        before = _snapshot(root)
        rejected = _repair_flake(root, code=1)
        _expect(
            "resource outside its owner" in rejected.stderr
            and "flake.nix:" in rejected.stderr
            and _snapshot(root) == before,
            rejected,
        )
        (root / "discarded").unlink()
        rejected_source = _repair_flake(root, "--source", str(root), code=1)
        _expect("resource outside its owner" in rejected_source.stderr, rejected_source)


@pytest.mark.parametrize(
    "composition",
    [
        "base // { formatter = inputs.afairesi.lib.mkFormatter { inherit inputs; }; }",
        (
            "base // { packages = builtins.mapAttrs "
            "(_: packages: packages // { default = packages.foo; }) base.packages; }"
        ),
        "base // { inherit (inputs.afairesi) blueprint lib; }",
        (
            "base // { checks = base.checks // { x86_64-linux = "
            "base.checks.x86_64-linux // { foo = pkgs.vmTools.runInLinuxVM "
            '(base.checks.x86_64-linux.foo.overrideAttrs { QEMU_OPTS = "-smp 2"; '
            "memSize = 4096; }); }; }; }"
        ),
        (
            "base // { packages.x86_64-linux.foo = import ./packages/foo/default.nix "
            "{ inherit inputs pkgs; }; }"
        ),
        'base // { note = "pkgs.runCommand \\"fake\\" {} \\"touch $out\\""; }',
        (
            "let build = pkgs.runCommand; in base // { lib.wrapper = build: "
            'build "external" {} "touch $out"; }'
        ),
        (
            "let build = pkgs.runCommand; in base // { lib.wrapper = { build }: "
            'build "external" {} "touch $out"; }'
        ),
        (
            "let build = pkgs.runCommand; in let build = x: x; in base // "
            '{ lib.wrapper = build "external" {} "touch $out"; }'
        ),
        "base // { lib.command = pkgs.runCommand; }",
        'base // { lib.command = pkgs.runCommand "partial"; }',
    ],
)
def test_flake_resource_ownership_preserves_composition(composition: str) -> None:
    """Preserve policies, output aliases, VM wrappers, imports, and lexical scopes."""
    with _fresh_repository() as root:
        source = (
            "{ outputs = inputs: let base = inputs.afairesi.blueprint "
            "{ inherit inputs; nixpkgs.config.allowUnfree = true; }; "
            f"pkgs = inputs.nixpkgs.legacyPackages.x86_64-linux; in {composition}; }}"
        )
        (root / "flake.nix").write_text(source)
        _repair_flake(root)
        stable = _snapshot(root)
        _repair_flake(root)
        _expect(
            _snapshot(root) == stable and (root / "flake.nix").read_text() == source,
            "composition was rewritten",
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
        _fixture_add(root, "packages/a", "python")
        _fixture_add(root, "packages/z", "python")
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
        _fixture_add(root, "packages/a", "python")
        _fixture_add(root, "packages/z", "python")
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
        _fixture_add(root, "packages/example", "python")
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
def test_generated_templates_evaluate_metadata_preserve_scopes_and_install_assets(
    kind: str,
    name: str,
    description: str,
    *,
    library: bool,
) -> None:
    """Evaluate generated Nix and run installed Python and HTML launchers."""
    with _fresh_repository() as root:
        subject = import_module("packages.afairesi.main")
        subject.add_package(root, kind, name, description)
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
            _check_installed_python_template(
                package,
                output,
                environment,
                actual,
                library=library,
            )
        else:
            _check_installed_html_template(package, output, environment, actual)


def _check_installed_python_template(
    package: Path,
    output: Path,
    environment: dict[str, str],
    actual: dict[str, Any],
    *,
    library: bool,
) -> None:
    """Check installed assets, imports, and executable behavior."""
    root = package.parent.parent
    name = package.name
    asset = package / "prm/asset.bin"
    _check_launcher_environment(root, name, environment, library=library)
    module = name.replace("-", "_")
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    installed = output / f"lib/python{version}/site-packages"
    _expect(
        (installed / module / "prm/asset.bin").read_bytes() == asset.read_bytes(),
        "Python installation lost assets",
    )
    environment["PYTHONPATH"] = (
        str(installed) + os.pathsep + environment.get("PYTHONPATH", "")
    )
    if library:
        _expect(
            "mainProgram" not in actual["meta"] and not (output / "bin").exists(),
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
            actual["meta"]["mainProgram"] == name and (output / "bin" / name).is_file(),
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


def _check_installed_html_template(
    package: Path,
    output: Path,
    environment: dict[str, str],
    actual: dict[str, Any],
) -> None:
    """Check browser options and resources through the installed launcher."""
    root = package.parent.parent
    name = package.name
    asset = package / "prm/asset.bin"
    _expect(actual["dependencies"] == ["git", "curl", "server"], actual)
    tools = root / "tmp/tools"
    tools.mkdir(parents=True)
    server = tools / "http-server"
    server.write_text(
        f"#!{sys.executable}\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n",
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
        _fixture_add(root, "packages/example", "python")
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
    ("arguments", "code"),
    [
        (("mv", "packages/example", "hosts/example"), 2),
        (("add", "packages/bad--name", "python"), 2),
        (("add", "hosts/bad-name"), 2),
        (("rm", "../outside"), 2),
        (("rm", "/outside"), 2),
        (("mv", "packages/example", "packages/taken"), 1),
        (("add", "packages/example", "python"), 1),
    ],
)
def test_rejected_operations_preserve_contents_modes_index_and_refs(
    arguments: tuple[str, ...],
    code: int,
) -> None:
    """Reject invalid paths and collisions before modifying work."""
    with _fresh_repository() as root:
        _run(root, "add", "packages/example", "python")
        _run(root, "add", "packages/taken", "nix")
        (root / "work-in-progress").write_text("preserve unrelated work")
        before = _snapshot(root)
        rejected = _run(root, *arguments, code=code)
        _expect(bool(rejected.stderr) and _snapshot(root) == before, rejected)


def _stage_resource_edits(
    root: Path,
    tracked: list[str],
) -> tuple[dict[str, bytes], dict[str, bytes]]:
    """Create distinct indexed and working versions for rename checks."""
    staged, working = {}, {}
    for relative in tracked:
        path = root / relative
        staged[relative] = path.read_bytes() + b"\n"
        working[relative] = staged[relative] + b"\n"
        path.write_bytes(staged[relative])
        _git(root, "add", "--", relative)
        path.write_bytes(working[relative])
    return staged, working


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
def test_resource_lifecycle_preserves_files_checks_and_git_state(
    kind: str,
    name: str,
    payload: bytes,
) -> None:
    """Add, rename, converge, and remove resources without losing Git state."""
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
            _repair_flake(root)
            _expect(
                f"{original}/prm/nested/asset.bin"
                in _git(root, "ls-files").splitlines(),
                "resources must be indexed",
            )
        expected_check = kind in {"host", "python"}
        _expect((root / check / "default.nix").is_file() == expected_check, check)
        tracked = _git(root, "ls-files", "--", original, check).splitlines()
        staged, working = _stage_resource_edits(root, tracked)
        untracked = package / "prm/untracked.bin"
        untracked.write_bytes(payload)
        if kind == "untracked":
            before = _snapshot(root)
            rejected = _run(root, "mv", original, destination, code=128)
            _expect("fatal:" in rejected.stderr, rejected)
            _expect(_snapshot(root) == before, "failed move changed untracked data")
            return
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
        _repair_flake(root)
        survivor = root / destination / "prm/survivor.bin"
        survivor.write_bytes(payload)
        _run(root, "rm", destination, "--force")
        _expect(
            survivor.read_bytes() == payload and not (root / new_check).exists(),
            "remove lost untracked data or left tracked check",
        )
        _expect(
            not _git(root, "ls-files", "--", original, destination, check, new_check),
            "remove left indexed paths",
        )


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
                "class Cases:\n    def test_result(self): pass\n"
                "class Intermediate(Cases): pass\n"
                "class TestBehavior(Intermediate): pass"
            ),
            True,
        ),
        (
            (
                "class Cases:\n    def test_result(self): pass\n"
                "class TestBehavior(Cases):\n    test_result = None"
            ),
            False,
        ),
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


def _home_test_run(
    root: Path,
    *arguments: str,
    code: int = 0,
    executable: str | None = None,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(  # noqa: S603
        [
            executable or os.environ["PACKAGE_E2E_EXECUTABLE"],
            *(["home"] if executable in (None, "afairesi") else []),
            *arguments,
        ],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode != code:
        msg = (
            f"expected exit {code}, got {result.returncode}\n"
            f"{result.stdout}{result.stderr}"
        )
        raise AssertionError(
            msg,
        )
    return result


def _home_test_repair_flake(
    root: Path,
    *arguments: str,
    code: int = 0,
) -> subprocess.CompletedProcess[str]:
    """Exercise the private flake repair entry point used by the formatter."""
    return _home_test_run(
        root,
        "-c",
        "from packages.afairesi.main import formatter_main; formatter_main()",
        *arguments,
        code=code,
        executable=sys.executable,
    )


def _home_test_git(root: Path, *arguments: str) -> str:
    return subprocess.run(  # noqa: S603
        ["git", *arguments],  # noqa: S607
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    ).stdout


def _home_test_fixture_git(root: Path, *arguments: str) -> str:
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


def _home_test_home_repository(root: Path) -> Path:
    """Create a home repository with a locally initialized submodule."""
    relative = "forge.example/owner/demo"
    checkout = root / relative
    checkout.mkdir(parents=True)
    _home_test_git(root, "init", "--quiet")
    _home_test_git(checkout, "init", "--quiet")
    with (root / ".git/config").open("a", encoding="utf-8") as config:
        config.write("[maintenance]\n\tauto = false\n")
    with (checkout / ".git/config").open("a", encoding="utf-8") as config:
        config.write(
            "[maintenance]\n\tauto = false\n"
            "[user]\n\tname = Test\n\temail = test@example.org\n"
            "[commit]\n\tgpgSign = false\n"
            '[remote "origin"]\n\turl = git@forge.example:owner/demo\n'
            "\tfetch = +refs/heads/*:refs/remotes/origin/*\n",
        )
    source = checkout / "LICENSE"
    source.write_text("first", encoding="utf-8")
    _home_test_git(checkout, "add", "LICENSE")
    _home_test_git(checkout, "commit", "--quiet", "-m", "first")
    _home_test_git(checkout, "update-ref", "refs/remotes/origin/main", "HEAD")
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
    _home_test_git(root, "add", "--force", ".gitignore", ".gitmodules", relative)
    return root


@pytest.mark.parametrize(
    "remote",
    [
        "https://example.test/team/project.git",
        "ssh://git@example.test/team/project.git",
        "git@example.test:team/project.git",
    ],
)
def test_merged_clone_registers_existing_content_without_creating_commits(
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
    _home_test_git(origin, "init", "--quiet")
    (origin / "LICENSE").write_text("existing repository")
    _home_test_git(origin, "add", "LICENSE")
    _home_test_git(
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
    _home_test_run(tmp_path, "init")
    _home_test_git(
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
    head = _home_test_git(home, "rev-parse", "HEAD")
    relative = "example.test/team/project"
    result = _home_test_run(tmp_path, "clone", remote)
    checkout = home / relative
    ignore = (home / ".gitignore").read_text(encoding="utf-8")
    for pattern in (
        "!/example.test/",
        "!/example.test/team/",
        "!/example.test/team/project",
    ):
        if ignore.splitlines().count(pattern) != 1:
            raise AssertionError(ignore)
    if _home_test_git(home, "show", ":.gitignore") != ignore:
        message = "clone REMOTE did not stage the home whitelist"
        raise AssertionError(message)
    if (checkout / "LICENSE").read_text() != "existing repository":
        raise AssertionError(result.stdout + result.stderr)
    if _home_test_git(home, "rev-parse", "HEAD") != head:
        message = "clone REMOTE created a home commit"
        raise AssertionError(message)
    if (
        _home_test_git(
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
    if not _home_test_git(home, "ls-files", "--stage", "--", relative).startswith(
        "160000 ",
    ):
        message = "clone REMOTE did not stage the submodule"
        raise AssertionError(message)
    repeated = _home_test_run(tmp_path, "clone", remote, code=128)
    _home_test_expect("already exists in the index" in repeated.stderr, repeated)
    if (home / ".gitignore").read_text(encoding="utf-8") != ignore:
        message = "repeated clone changed the home whitelist"
        raise AssertionError(message)


def _local_init(root: Path, *, code: int = 0) -> subprocess.CompletedProcess[str]:
    """Run source initialization with the caller's Nix tool configuration."""
    return _run(
        root,
        "-c",
        "from packages.afairesi.main import main; main()",
        "repo",
        "init",
        code=code,
        executable=sys.executable,
    )


@pytest.mark.parametrize("branch", ["main", "trunk"])
def test_local_init_stages_files_without_committing_or_requiring_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    branch: str,
) -> None:
    """Initialize a local worktree, preserving Git configuration and formatted files."""
    tools = tmp_path / "tools"
    tools.mkdir()
    fake_nix = tools / "nix"
    fake_nix.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = config ]; then\n'
        "    printf 'nix-command flakes\\n'\n"
        'elif [ "$1" = flake ]; then\n'
        "    git ls-files --error-unmatch flake.nix >/dev/null || exit 7\n"
        "    printf '{}\\n' > flake.lock\n"
        "else\n"
        "    git ls-files --error-unmatch .gitignore flake.lock >/dev/null || exit 8\n"
        "    printf '\\n' >> flake.nix\n"
        "fi\n",
    )
    fake_nix.chmod(0o755)
    monkeypatch.setenv("PATH", str(tools) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("HOME", str(tmp_path / "uninitialized-home"))
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "--quiet", f"--initial-branch={branch}")
    config = _git(root, "config", "--local", "--list")
    _local_init(root)
    files = {".gitignore", "flake.lock", "flake.nix"}
    _expect(set(_git(root, "ls-files").splitlines()) == files, "wrong staged files")
    for name in files:
        _expect(_git(root, "show", f":{name}") == (root / name).read_text(), name)
    _expect(_git(root, "symbolic-ref", "--short", "HEAD").strip() == branch, branch)
    _expect(not _git(root, "for-each-ref"), "initialization created a commit")
    _expect(_git(root, "config", "--local", "--list") == config, "Git config changed")
    _expect(not Path.home().exists(), "initialization required home setup")
    before = _snapshot(root)
    _local_init(root, code=1)
    _expect(_snapshot(root) == before, "repeated initialization changed files")


@pytest.mark.parametrize("failure", ["lock", "format", "interrupt"])
@pytest.mark.parametrize("index_exists", [False, True])
def test_local_init_rolls_back_failed_nix_commands_and_can_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    *,
    index_exists: bool,
) -> None:
    """Restore the empty worktree and original index after errors or interruption."""
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "--quiet")
    index = root / ".git/index"
    if index_exists:
        _git(root, "read-tree", "--empty")
        index.chmod(0o640)
    original_index = (
        (index.read_bytes(), index.stat().st_mode) if index_exists else None
    )
    before = _snapshot(root)
    tools = tmp_path / "tools"
    tools.mkdir()
    fake_nix = tools / "nix"
    fake_nix.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = config ]; then\n'
        "    printf 'nix-command flakes\\n'\n"
        "    exit 0\n"
        "fi\n"
        "printf '{}\\n' > flake.lock\n"
        'if [ "$1" = flake ] && [ "$FAILURE" != lock ]; then exit 0; fi\n'
        "mkdir -p tmp/cache\n"
        "printf 'partial output' > tmp/cache/result\n"
        "ln -s tmp/cache partial-link\n"
        "git add --force flake.lock tmp/cache/result partial-link\n"
        'if [ "$FAILURE" = interrupt ]; then kill -INT "$PPID"; exit 0; fi\n'
        "echo 'test Nix failure' >&2\n"
        "exit 23\n",
    )
    fake_nix.chmod(0o755)
    monkeypatch.setenv("PATH", str(tools) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("FAILURE", failure)
    result = _local_init(root, code=130 if failure == "interrupt" else 23)
    if failure != "interrupt":
        _expect("test Nix failure" in result.stderr, result)
    _expect(_snapshot(root) == before, "failed initialization changed repository state")
    restored_index = (
        (index.read_bytes(), index.stat().st_mode) if index.exists() else None
    )
    _expect(restored_index == original_index, "original Git index was not restored")
    fake_nix.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = config ]; then\n'
        "    printf 'nix-command flakes\\n'\n"
        'elif [ "$1" = flake ]; then\n'
        "    printf '{}\\n' > flake.lock\n"
        "fi\n",
    )
    _local_init(root)
    _expect(
        set(_git(root, "ls-files").splitlines())
        == {".gitignore", "flake.lock", "flake.nix"},
        "initialization could not be retried",
    )


@pytest.mark.parametrize("existing", ["untracked", "staged", "deleted", "symlink"])
def test_local_init_rejects_existing_content_without_changes(
    tmp_path: Path,
    existing: str,
) -> None:
    """Reject populated worktrees and retained index entries before mutation."""
    _git(tmp_path, "init", "--quiet")
    path = tmp_path / "personal-data"
    if existing == "symlink":
        path.symlink_to(tmp_path / "missing")
    else:
        path.write_text("preserve this")
        if existing in {"staged", "deleted"}:
            _git(tmp_path, "add", "personal-data")
        if existing == "deleted":
            path.unlink()
    before = _snapshot(tmp_path)
    result = _run(tmp_path, "init", code=1)
    _expect("empty Git worktree" in result.stderr, result)
    _expect(_snapshot(tmp_path) == before, "initialization changed existing content")


@pytest.mark.parametrize("features", ["", "nix-command", "flakes"])
def test_local_init_checks_nix_features_before_creating_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    features: str,
) -> None:
    """Explain disabled Nix features before writing files or staging."""
    _git(tmp_path, "init", "--quiet")
    monkeypatch.setenv(
        "NIX_CONFIG",
        f"experimental-features = {features}\nextra-experimental-features =\n",
    )
    before = _snapshot(tmp_path)
    result = _run(tmp_path, "init", code=1)
    _expect("experimental-features = nix-command flakes" in result.stderr, result)
    _expect(_snapshot(tmp_path) == before, "Nix prerequisite failure changed state")


def test_local_init_reports_missing_nix_without_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explain missing Nix before writing files or staging."""
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "--quiet")
    tools = tmp_path / "tools"
    tools.mkdir()
    git_executable = shutil.which("git")
    if git_executable is None:
        pytest.fail("Missing Git executable")
    (tools / "git").symlink_to(git_executable)
    monkeypatch.setenv("PATH", str(tools))
    before = _snapshot(root)
    result = _local_init(root, code=1)
    _expect("Nix executable not found" in result.stderr, result)
    _expect(_snapshot(root) == before, "missing Nix changed state")


@pytest.mark.parametrize("initialized_git", [False, True])
def test_merged_home_prerequisites_explain_initialization_without_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    initialized_git: bool,
) -> None:
    """Direct missing home setup to home initialization without modifying the home."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    if initialized_git:
        _home_test_git(home, "init", "--quiet")
    before = _home_test_snapshot(home) if initialized_git else None
    result = _home_test_run(
        tmp_path,
        "clone",
        "https://example.test/team/new.git",
        code=1,
    )
    _home_test_expect("afairesi home init" in result.stderr, result)
    if initialized_git:
        _home_test_expect(
            _home_test_snapshot(home) == before,
            "home prerequisite check changed Git state",
        )
    else:
        _home_test_expect(
            not list(home.iterdir()),
            "home prerequisite check created files",
        )


def _home_test_check_home_commits(
    home_repository: Path,
) -> None:
    """Leave dirty files and gitlink advancement to native Git."""
    root = home_repository
    relative = "forge.example/owner/demo"
    checkout = root / relative
    source = checkout / "LICENSE"
    first = _home_test_git(checkout, "rev-parse", "HEAD").strip()
    source.write_text("second", encoding="utf-8")
    _home_test_run(root, "fix")
    if source.read_text() != "second":
        message = "convergence changed dirty submodule content"
        raise AssertionError(message)
    _home_test_git(checkout, "add", "LICENSE")
    _home_test_git(checkout, "commit", "--quiet", "-m", "second")
    second = _home_test_git(checkout, "rev-parse", "HEAD").strip()
    for published in (False, True):
        if published:
            _home_test_git(checkout, "update-ref", "refs/remotes/origin/main", second)
        _home_test_run(root, "fix")
        if first not in _home_test_git(root, "ls-files", "--stage", relative):
            message = "convergence advanced the recorded submodule commit"
            raise AssertionError(message)
    _home_test_git(root, "add", relative)


def _home_test_check_home_whitelist(home_repository: Path) -> None:
    """Repair missing whitelist entries for a registered home submodule."""
    root = home_repository
    ignore = root / ".gitignore"
    expected = ignore.read_text(encoding="utf-8")
    ignore.write_text("*\n!/.gitignore\n!/.gitmodules\n", encoding="utf-8")
    before = _home_test_git(root, "show", ":.gitignore")
    preview = _home_test_run(root, "fix", "--dry-run")
    if "would write '.gitignore'" not in preview.stdout:
        raise AssertionError(preview.stdout)
    if (
        ignore.read_text(encoding="utf-8") == expected
        or _home_test_git(root, "show", ":.gitignore") != before
    ):
        message = "dry-run changed the home whitelist"
        raise AssertionError(message)
    _home_test_run(root, "fix")
    if ignore.read_text(encoding="utf-8") != expected:
        raise AssertionError(ignore.read_text(encoding="utf-8"))
    if _home_test_git(root, "show", ":.gitignore") != expected:
        message = "convergence did not stage the repaired whitelist"
        raise AssertionError(message)
    _home_test_run(root, "fix", "--dry-run")


def _home_test_check_home_settings(
    home_repository: Path,
) -> None:
    """Allow Git settings beyond the path and URL managed by Afairesi."""
    root = home_repository
    modules = root / ".gitmodules"
    source = modules.read_text() + (
        "branch = main\nignore = dirty\nshallow = true\nupdate = checkout\n"
    )
    modules.write_text(source)
    _home_test_git(root, "add", ".gitmodules")
    index = _home_test_git(root, "ls-files", "--stage")
    _home_test_run(root, "fix", "--dry-run")
    _home_test_run(root, "fix")
    if (
        modules.read_text() != source
        or _home_test_git(root, "ls-files", "--stage") != index
    ):
        message = "convergence changed optional submodule settings"
        raise AssertionError(message)


def _home_test_check_home_move(
    home_repository: Path,
) -> None:
    """Move dirty submodules safely and reject collisions before mutation."""
    root = home_repository
    relative = "forge.example/owner/demo"
    checkout = root / relative
    source = checkout / "LICENSE"
    first = _home_test_git(checkout, "rev-parse", "HEAD").strip()
    source.write_text("move snapshot", encoding="utf-8")
    _home_test_git(checkout, "add", "LICENSE")
    _home_test_git(checkout, "commit", "--quiet", "-m", "second")
    _home_test_git(root, "submodule", "absorbgitdirs", relative)
    _home_test_git(
        root,
        "config",
        f"submodule.{relative}.url",
        "git@forge.example:owner/demo",
    )
    destination = "forge.example/owner/renamed"
    _home_test_git(
        root,
        "config",
        "--file",
        ".gitmodules",
        f"submodule.{relative}.url",
        "git@forge.example:owner/renamed",
    )
    source.write_text("staged", encoding="utf-8")
    _home_test_git(checkout, "add", "LICENSE")
    source.write_text("unstaged", encoding="utf-8")
    (checkout / "untracked").write_text("keep", encoding="utf-8")
    (root / "unrelated").write_text("keep home file", encoding="utf-8")
    before = _home_test_git(checkout, "status", "--porcelain")
    indexed = _home_test_git(checkout, "show", ":LICENSE")
    target = root / destination
    target.mkdir()
    rejected = _home_test_run(root, "fix", code=1)
    if "target already exists" not in rejected.stderr or not checkout.exists():
        raise AssertionError(rejected.stderr)
    if destination in _home_test_git(
        root,
        "config",
        "--file",
        ".gitmodules",
        f"submodule.{relative}.path",
    ):
        message = "collision changed the submodule path"
        raise AssertionError(message)
    target.rmdir()
    home_before = _home_test_snapshot(root)
    for arguments in (("--dry-run",), ()):
        rejected = _home_test_run(root, "fix", *arguments, code=128)
        _home_test_expect(
            "stage your changes to .gitmodules" in rejected.stderr,
            rejected,
        )
        _home_test_expect(
            _home_test_snapshot(root) == home_before,
            "rejected move changed home state",
        )
    _home_test_git(root, "add", ".gitmodules")
    _home_test_run(root, "fix", "--dry-run")
    if not checkout.exists() or target.exists():
        message = "dry-run moved the checkout"
        raise AssertionError(message)
    _home_test_run(root, "fix")
    if (
        checkout.exists()
        or _home_test_git(target, "status", "--porcelain") != before
        or _home_test_git(target, "show", ":LICENSE") != indexed
        or (target / "untracked").read_text() != "keep"
        or (root / "unrelated").read_text() != "keep home file"
        or first not in _home_test_git(root, "ls-files", "--stage", destination)
        or _home_test_git(target, "remote", "get-url", "origin").strip()
        != "git@forge.example:owner/demo"
    ):
        message = "home rename failed to preserve Git state and local remote settings"
        raise AssertionError(message)
    _home_test_run(root, "fix", "--dry-run")
    whitelist = (root / ".gitignore").read_text().splitlines()
    _home_test_expect(f"!/{relative}" not in whitelist, whitelist)
    _home_test_expect(f"!/{destination}" in whitelist, whitelist)


def _home_test_expect(condition: bool, detail: object) -> None:  # noqa: FBT001
    """Report an invariant failure with its observed state."""
    if not condition:
        raise AssertionError(detail)


def _home_test_snapshot(
    root: Path,
    *,
    exclude: tuple[str, ...] = (),
) -> tuple[object, ...]:
    """Observe home state with the shared repository snapshot."""
    return _snapshot(root, exclude=exclude)


def _home_test_history_snapshot(root: Path) -> dict[Path, bytes]:
    """Observe stored Git files without requiring a surviving worktree."""
    return {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
        and path.relative_to(root).parts[0] in {"objects", "refs", "logs", "HEAD"}
    }


def _git_rule_snapshot(
    root: Path,
    *,
    allow_whitelist_edits: bool = False,
) -> dict[str, object]:
    """Compare native Git state, allowing only afairesi's root whitelist edits."""
    files, index, refs, config = _snapshot(
        root,
        exclude=(".gitignore",) if allow_whitelist_edits else (),
    )
    contents = cast("dict[str, tuple[int, int, bytes]]", files)
    root_bytes = os.fsencode(root)
    modules = {}
    storage = root / ".git/modules"
    module_configs = [
        *storage.rglob("config"),
        *(
            marker / "config"
            for marker in root.rglob(".git")
            if marker != root / ".git" and marker.is_dir()
        ),
    ]
    for module_config in sorted(module_configs):
        directory = module_config.parent
        modules[directory.relative_to(root).as_posix()] = (
            module_config.read_bytes().replace(root_bytes, b"<ROOT>"),
            {
                path: value.replace(root_bytes, b"<ROOT>")
                for path, value in _home_test_history_snapshot(directory).items()
            },
            _git(
                root,
                "--git-dir",
                str(directory),
                "--work-tree",
                str(root),
                "ls-files",
                "--stage",
                "-z",
            ),
        )
    return {
        "files": {
            path: (kind, mode, value.replace(root_bytes, b"<ROOT>"))
            for path, (kind, mode, value) in contents.items()
            if ".git" not in Path(path).parts[:-1]
        },
        "index": [
            record
            for record in cast("str", index).split("\0")
            if record
            and not (
                allow_whitelist_edits and record.partition("\t")[2] == ".gitignore"
            )
        ],
        "refs": refs,
        "config": cast("str", config).replace(str(root), "<ROOT>"),
        "modules": modules,
        "locks": {
            path.relative_to(root / ".git"): path.read_bytes()
            for path in (root / ".git").rglob("*.lock")
            if path.is_file()
        },
    }


def _expect_git_rule_state(
    native: Path,
    wrapped: Path,
    *,
    allow_whitelist_edits: bool,
) -> None:
    """Report the state fields that diverge from the native Git oracle."""
    expected = _git_rule_snapshot(native, allow_whitelist_edits=allow_whitelist_edits)
    actual = _git_rule_snapshot(wrapped, allow_whitelist_edits=allow_whitelist_edits)
    differences = {
        key: {"git": expected[key], "afairesi": actual[key]}
        for key in expected
        if expected[key] != actual[key]
    }
    _expect(not differences, differences)


def _git_rule_home_fixture(root: Path, state: str) -> Path:
    """Prepare equivalent submodule states before copying the oracle fixture."""
    native = _home_test_home_repository(root)
    relative = "forge.example/owner/demo"
    checkout = native / relative
    if state != "legacy":
        _home_test_git(native, "submodule", "absorbgitdirs")
    _home_test_git(native, "config", f"submodule.{relative}.active", "true")
    _home_test_git(
        native,
        "config",
        f"submodule.{relative}.url",
        "git@mirror.test:repo",
    )
    _home_test_fixture_git(native, "commit", "-qm", "Home snapshot")
    if state in {"dirty", "staged", "advanced"}:
        (checkout / "LICENSE").write_text("local edits")
        if state in {"staged", "advanced"}:
            _home_test_git(checkout, "add", "LICENSE")
        if state == "advanced":
            _home_test_fixture_git(checkout, "commit", "-qm", "Local commit")
    elif state in {"untracked", "ignored"}:
        if state == "ignored":
            exclude = _home_test_git(
                checkout,
                "rev-parse",
                "--path-format=absolute",
                "--git-path",
                "info/exclude",
            ).strip()
            Path(exclude).write_text("asset\n")
        (checkout / "asset").write_bytes(b"local data\x00\xff")
    elif state == "deinitialized":
        _home_test_git(native, "submodule", "deinit", "--", relative)
    elif state == "dirty-modules":
        modules = native / ".gitmodules"
        modules.write_text(modules.read_text() + "# unstaged change\n")
    elif state == "index-lock":
        (native / ".git/index.lock").touch()
    return native


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize(
    "state",
    [
        "clean",
        "dirty",
        "staged",
        "advanced",
        "untracked",
        "ignored",
        "legacy",
        "deinitialized",
        "dirty-modules",
        "index-lock",
    ],
)
def test_home_rm_matches_native_git(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    *,
    force: bool,
) -> None:
    """Use real Git as the oracle for removal errors, force rules, and side effects."""
    native = _git_rule_home_fixture(tmp_path / "native", state)
    wrapped = tmp_path / "wrapped"
    relative = "forge.example/owner/demo"
    shutil.copytree(native, wrapped)
    monkeypatch.setenv("HOME", str(wrapped))
    arguments = []
    if force:
        arguments.append("--force")
    expected = subprocess.run(  # noqa: S603
        ["git", "rm", "-r", *arguments, "--", relative],  # noqa: S607
        cwd=native,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    actual = _home_test_run(
        wrapped,
        "rm",
        relative,
        *arguments,
        code=expected.returncode,
    )
    _expect(
        actual.stderr.replace(str(wrapped), "<ROOT>")
        == expected.stderr.replace(str(native), "<ROOT>"),
        (expected.stderr, actual.stderr),
    )
    _expect_git_rule_state(
        native,
        wrapped,
        allow_whitelist_edits=expected.returncode == 0,
    )


@pytest.mark.parametrize(
    "state",
    [
        "clean",
        "dirty",
        "staged",
        "untracked",
        "ignored",
        "untracked-resource",
        "index-lock",
    ],
)
def test_repo_mv_matches_native_git(
    tmp_path: Path,
    state: str,
) -> None:
    """Keep Git's move semantics for staged edits, local data, and rejected moves."""
    native = tmp_path / "native"
    native.mkdir()
    _repository(native)
    _fixture_add(native, "packages/old", "nix")
    _fixture_git(native, "commit", "-qm", "Resource snapshot")
    resource = native / "packages/old"
    if state in {"dirty", "staged"}:
        source = resource / "default.nix"
        source.write_text(source.read_text() + "# staged edit\n")
        if state == "staged":
            _git(native, "add", "packages/old/default.nix")
            source.write_text(source.read_text() + "# unstaged edit\n")
    elif state in {"untracked", "ignored"}:
        asset = resource / ("tmp/asset" if state == "ignored" else "prm/asset")
        asset.parent.mkdir()
        asset.write_bytes(b"local data\x00\xff")
        asset.chmod(0o700)
    elif state == "untracked-resource":
        _git(native, "rm", "--cached", "packages/old/default.nix")
    elif state == "index-lock":
        (native / ".git/index.lock").touch()
    wrapped = tmp_path / "wrapped"
    shutil.copytree(native, wrapped)
    expected = subprocess.run(
        ["git", "mv", "--", "packages/old", "packages/new"],  # noqa: S607
        cwd=native,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    actual = _run(
        wrapped,
        "mv",
        "packages/old",
        "packages/new",
        code=expected.returncode,
    )
    _expect(
        actual.stderr.replace(str(wrapped), "<ROOT>")
        == expected.stderr.replace(str(native), "<ROOT>"),
        (expected.stderr, actual.stderr),
    )
    _expect_git_rule_state(
        native,
        wrapped,
        allow_whitelist_edits=expected.returncode == 0,
    )


@pytest.mark.parametrize("branch", ["main", "trunk"])
def test_merged_init_home_stages_policy_without_cleaning_or_committing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    branch: str,
) -> None:
    """Initialize HOME from another directory and preserve existing home data."""
    home = tmp_path / "home"
    home.mkdir()
    local = home / "personal-data"
    local.write_bytes(b"\x00keep home data\xff")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "init.defaultBranch")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", branch)
    _home_test_run(tmp_path, "init")
    _home_test_expect(
        _home_test_git(home, "branch", "--show-current").strip() == branch,
        "home initialization changed Git's initial branch",
    )
    expected = "*\n!/.gitignore\n!/.gitmodules\n"
    _home_test_expect(
        (home / ".gitignore").read_text() == expected,
        "wrong home policy",
    )
    _home_test_expect(
        _home_test_git(home, "show", ":.gitignore") == expected,
        "policy was not staged",
    )
    _home_test_expect(
        _home_test_git(home, "ls-files").splitlines() == [".gitignore"],
        "home data was staged",
    )
    _home_test_expect(
        not _home_test_git(home, "for-each-ref"),
        "home initialization created a commit",
    )
    _home_test_expect(
        local.read_bytes() == b"\x00keep home data\xff",
        "home initialization cleaned HOME",
    )
    before = _home_test_snapshot(home)
    template = tmp_path / "template"
    (template / "hooks").mkdir(parents=True)
    (template / "hooks/custom-hook").write_text("custom Git template")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
    monkeypatch.setenv("GIT_CONFIG_KEY_1", "init.templateDir")
    monkeypatch.setenv("GIT_CONFIG_VALUE_1", str(template))
    _home_test_run(tmp_path, "init")
    _home_test_expect(
        (home / ".git/hooks/custom-hook").read_text() == "custom Git template",
        "home initialization skipped native Git reinitialization",
    )
    _home_test_expect(
        _home_test_snapshot(home) == before,
        "repeated home initialization changed state",
    )
    _home_test_expect(
        not (tmp_path / ".git").exists(),
        "home initialization initialized the working directory",
    )


@pytest.mark.parametrize("case", ["flake", "whitelist", "gitfile"])
def test_home_initialization_rejects_invalid_state_without_creating_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    """Reject invalid home policy and preserve Git initialization errors."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    name, source = {
        "flake": ("flake.nix", "{}\n"),
        "whitelist": (".gitignore", "invalid whitelist\n"),
        "gitfile": (".git", "invalid gitfile\n"),
    }[case]
    (home / name).write_text(source)
    before = {path.name: path.read_bytes() for path in home.iterdir()}
    result = _home_test_run(tmp_path, "init", code=128 if case == "gitfile" else 1)
    _home_test_expect(not (home / ".git").is_dir(), "failed init created Git metadata")
    _home_test_expect(
        {path.name: path.read_bytes() for path in home.iterdir()} == before,
        "failed initialization changed home files",
    )
    if case == "gitfile":
        _home_test_expect("fatal:" in result.stderr, result)


def test_merged_failed_clone_restores_home_whitelist_and_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restore whitelist contents, staging, and directories after clone failure."""
    home = _home_test_home_repository(tmp_path / "home")
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
    before = _home_test_snapshot(home)
    _home_test_run(tmp_path, "clone", remote, code=128)
    _home_test_expect(
        _home_test_snapshot(home) == before,
        "failed clone changed the home repository",
    )


def test_merged_home_repair_rejects_collision_with_missing_checkout_registration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject a move onto a missing registered path before changing home state."""
    root = _home_test_home_repository(tmp_path)
    monkeypatch.setenv("HOME", str(root))
    relative = "forge.example/owner/demo"
    destination = "forge.example/owner/renamed"
    commit = _home_test_git(root / relative, "rev-parse", "HEAD").strip()
    _home_test_git(
        root,
        "update-index",
        "--add",
        "--cacheinfo",
        "160000",
        commit,
        destination,
    )
    _home_test_git(
        root,
        "config",
        "--file",
        ".gitmodules",
        f"submodule.{relative}.url",
        "git@forge.example:owner/renamed",
    )
    _home_test_git(
        root,
        "config",
        "--file",
        ".gitmodules",
        f"submodule.{destination}.path",
        destination,
    )
    _home_test_git(
        root,
        "config",
        "--file",
        ".gitmodules",
        f"submodule.{destination}.url",
        "git@forge.example:owner/elsewhere",
    )
    _home_test_git(root, "add", ".gitmodules")
    before = _home_test_snapshot(root)
    for arguments in [("--dry-run",), ()]:
        result = _home_test_run(root, "fix", *arguments, code=1)
        _home_test_expect(
            "conflicts with a missing checkout registration" in result.stderr,
            result,
        )
        _home_test_expect(
            _home_test_snapshot(root) == before,
            "rejected repair changed home state",
        )


@pytest.mark.parametrize("destination", ["demo", "renamed"])
@pytest.mark.parametrize("state", ["deinitialized", "missing", "placeholder"])
def test_merged_home_repair_preserves_missing_checkouts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    destination: str,
    state: str,
) -> None:
    """Repair the whitelist without initializing or relocating missing checkouts."""
    root = _home_test_home_repository(tmp_path)
    monkeypatch.setenv("HOME", str(root))
    relative = "forge.example/owner/demo"
    checkout = root / relative
    _home_test_git(root, "submodule", "absorbgitdirs", "--", relative)
    _home_test_git(
        root,
        "config",
        f"submodule.{relative}.url",
        "git@mirror.example:owner/demo",
    )
    _home_test_fixture_git(root, "commit", "-qm", "Home snapshot")
    if state == "deinitialized":
        _home_test_git(root, "submodule", "deinit", "--", relative)
    else:
        shutil.rmtree(checkout)
        if state == "placeholder":
            checkout.mkdir()
            (checkout / "LICENSE").write_text("unrelated local data")
    _home_test_git(
        root,
        "config",
        "--file",
        ".gitmodules",
        f"submodule.{relative}.url",
        f"git@forge.example:owner/{destination}",
    )
    modules_before = (root / ".gitmodules").read_bytes()
    gitlink_before = _home_test_git(root, "ls-files", "--stage", "--", relative)
    config_before = _home_test_git(root, "config", "--local", "--list")
    history = root / ".git/modules" / relative
    history_before = _home_test_history_snapshot(history)
    ignore = root / ".gitignore"
    expected_ignore = ignore.read_text()
    ignore.write_text("*\n!/.gitignore\n!/.gitmodules\n")
    before = _home_test_snapshot(root)
    _home_test_run(root, "fix", "--dry-run")
    _home_test_expect(
        _home_test_snapshot(root) == before,
        "repair preview changed missing checkout state",
    )
    _home_test_run(root, "fix")
    _home_test_expect(
        not (checkout / ".git").exists(),
        "repair initialized a missing checkout",
    )
    _home_test_expect(
        ignore.read_text() == expected_ignore,
        "repair failed to restore whitelist",
    )
    _home_test_expect(
        (root / ".gitmodules").read_bytes() == modules_before,
        "repair moved missing checkout registration",
    )
    _home_test_expect(
        _home_test_git(root, "ls-files", "--stage", "--", relative) == gitlink_before,
        "repair changed missing checkout gitlink",
    )
    _home_test_expect(
        _home_test_git(root, "config", "--local", "--list") == config_before,
        "repair changed local registration",
    )
    _home_test_expect(
        _home_test_history_snapshot(history) == history_before,
        "repair changed stored history",
    )
    if state == "placeholder":
        _home_test_expect(
            (checkout / "LICENSE").read_text() == "unrelated local data",
            "repair changed placeholder data",
        )
    elif state == "missing":
        _home_test_expect(
            not checkout.exists(),
            "repair recreated missing checkout directory",
        )
    if destination != "demo":
        _home_test_expect(
            not (root / f"forge.example/owner/{destination}").exists(),
            "repair relocated a missing checkout",
        )
    after = _home_test_snapshot(root)
    _home_test_run(root, "fix")
    _home_test_expect(
        _home_test_snapshot(root) == after,
        "repeated repair changed missing checkout state",
    )


@pytest.mark.parametrize("destination", ["demo", "renamed", "new/team/repo"])
@pytest.mark.parametrize("remote", ["fork", "missing"])
def test_merged_home_repair_preserves_local_remote_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    destination: str,
    remote: str,
) -> None:
    """Use declared URLs for layout while preserving independent local remotes."""
    root = _home_test_home_repository(tmp_path)
    monkeypatch.setenv("HOME", str(root))
    relative = "forge.example/owner/demo"
    checkout = root / relative
    _home_test_git(root, "submodule", "absorbgitdirs", "--", relative)
    local_url = "git@mirror.example:owner/demo"
    _home_test_git(root, "config", f"submodule.{relative}.url", local_url)
    if remote == "missing":
        _home_test_git(checkout, "remote", "remove", "origin")
    else:
        _home_test_git(
            checkout,
            "remote",
            "set-url",
            "origin",
            "git@forge.example:me/fork",
        )
        _home_test_git(checkout, "remote", "set-url", "--push", "origin", local_url)
    expected = f"forge.example/owner/{destination}"
    declared_url = f"git@forge.example:owner/{destination}"
    _home_test_git(
        root,
        "config",
        "--file",
        ".gitmodules",
        f"submodule.{relative}.url",
        declared_url,
    )
    _home_test_git(root, "add", ".gitmodules")
    before = _home_test_snapshot(root)
    checkout_before = _home_test_snapshot(checkout)
    home_config_before = _home_test_git(root, "config", "--local", "--list")
    remotes_before = {
        line
        for line in _home_test_git(checkout, "config", "--local", "--list").splitlines()
        if line.startswith("remote.")
    }
    _home_test_run(root, "fix", "--dry-run")
    _home_test_expect(
        _home_test_snapshot(root) == before,
        "home repair preview changed state",
    )
    _home_test_expect(
        _home_test_snapshot(checkout) == checkout_before,
        "preview changed checkout settings",
    )
    _home_test_run(root, "fix")
    target = root / expected
    _home_test_expect(
        target.is_dir(),
        "repair did not use the declared URL for placement",
    )
    remotes_after = {
        line
        for line in _home_test_git(target, "config", "--local", "--list").splitlines()
        if line.startswith("remote.")
    }
    _home_test_expect(
        remotes_after == remotes_before,
        "repair changed local remote settings",
    )
    _home_test_expect(
        _home_test_snapshot(target)[:3] == checkout_before[:3],
        "repair changed checkout state",
    )
    _home_test_expect(
        _home_test_git(
            root,
            "config",
            "--local",
            "--get",
            f"submodule.{relative}.url",
        ).strip()
        == local_url,
        "repair overwrote the local submodule URL override",
    )
    subject = import_module("packages.afairesi.main")
    _home_test_expect(
        subject.home_submodules(root)[0]["url"] == declared_url,
        "repair changed declared URL",
    )
    _home_test_expect(
        subject.home_submodules(root)[0]["name"] == relative,
        "repair renamed Git's submodule section",
    )
    _home_test_expect(
        _home_test_git(root, "config", "--local", "--list") == home_config_before,
        "repair changed local submodule configuration",
    )


def test_merged_home_repair_leaves_nested_submodule_settings_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preserve direct and nested remote settings during home repair."""
    root = _home_test_home_repository(tmp_path)
    monkeypatch.setenv("HOME", str(root))
    relative = "forge.example/owner/demo"
    checkout = root / relative
    nested = checkout / "nested"
    nested.mkdir()
    _home_test_git(nested, "init", "--quiet")
    (nested / "LICENSE").write_text("nested repository")
    _home_test_git(nested, "add", "LICENSE")
    _home_test_fixture_git(nested, "commit", "-qm", "Nested snapshot")
    mirror = "git@mirror.example:owner/child"
    _home_test_git(nested, "remote", "add", "origin", mirror)
    (checkout / ".gitmodules").write_text(
        '[submodule "nested"]\npath = nested\nurl = git@forge.example:owner/child\n',
    )
    _home_test_git(checkout, "add", ".gitmodules", "nested")
    _home_test_fixture_git(checkout, "commit", "-qm", "Register nested repository")
    _home_test_git(checkout, "config", "submodule.nested.url", mirror)
    _home_test_git(checkout, "config", "submodule.nested.active", "true")
    _home_test_git(root, "submodule", "absorbgitdirs", "--", relative)
    _home_test_git(root, "add", relative)
    _home_test_fixture_git(root, "commit", "-qm", "Home snapshot")
    _home_test_git(
        root,
        "config",
        f"submodule.{relative}.url",
        "git@forge.example:owner/demo",
    )
    _home_test_git(
        checkout,
        "remote",
        "set-url",
        "origin",
        "git@mirror.example:owner/demo",
    )
    before = _home_test_snapshot(root)
    checkout_before = _home_test_snapshot(checkout)
    nested_before = _home_test_snapshot(nested)
    _home_test_run(root, "fix", "--dry-run")
    _home_test_expect(
        _home_test_snapshot(root) == before,
        "home repair preview changed state",
    )
    _home_test_expect(
        _home_test_snapshot(checkout) == checkout_before,
        "preview changed checkout settings",
    )
    _home_test_expect(
        _home_test_snapshot(nested) == nested_before,
        "preview changed nested repository",
    )
    _home_test_run(root, "fix")
    _home_test_expect(
        _home_test_git(checkout, "remote", "get-url", "origin").strip()
        == "git@mirror.example:owner/demo",
        "home repair changed the direct submodule remote",
    )
    _home_test_expect(
        _home_test_git(
            checkout,
            "config",
            "--local",
            "--get",
            "submodule.nested.url",
        ).strip()
        == mirror,
        "home repair changed nested submodule registration",
    )
    _home_test_expect(
        _home_test_snapshot(nested) == nested_before,
        "home repair changed nested repository",
    )


def test_merged_home_repair_preserves_dirty_submodules() -> None:
    """Preserve checkout/index state through home convergence and relocation."""
    with (
        TemporaryDirectory(prefix="afairesi-home-") as directory,
        pytest.MonkeyPatch.context() as patch,
    ):
        root = _home_test_home_repository(Path(directory))
        patch.setenv("HOME", str(root))
        before = _home_test_snapshot(root)
        rejected = _home_test_repair_flake(root, code=1)
        _home_test_expect("run afairesi home fix" in rejected.stderr, rejected)
        _home_test_expect(
            _home_test_snapshot(root) == before,
            "flake repair changed home state",
        )
        _home_test_check_home_commits(root)
        _home_test_check_home_whitelist(root)
        _home_test_check_home_settings(root)
        _home_test_check_home_move(root)
        modules = root / ".gitmodules"
        modules.write_text(
            modules.read_text() + "url = git@forge.example:owner/other\n",
        )
        before = _home_test_snapshot(root)
        result = _home_test_run(root, "fix", code=1)
        _home_test_expect(
            "duplicate url field" in result.stderr
            and _home_test_snapshot(root) == before,
            result,
        )


@pytest.mark.parametrize(
    "location",
    ["home", "nested", "submodule", "flake", "outside"],
)
def test_merged_home_repair_targets_home_from_any_working_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    location: str,
) -> None:
    """Repair only HOME and preserve preview state from repositories or plain paths."""
    home = _home_test_home_repository(tmp_path / "home")
    monkeypatch.setenv("HOME", str(home))
    checkout = home / "forge.example/owner/demo"
    (checkout / "flake.nix").touch()
    flake = tmp_path / "flake"
    flake.mkdir()
    _home_test_git(flake, "init", "--quiet")
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
    before = _home_test_snapshot(home)
    checkout_before = _home_test_snapshot(checkout)
    flake_before = _home_test_snapshot(flake)
    preview = _home_test_run(cwd, "fix", "--dry-run")
    _home_test_expect("would write '.gitignore'" in preview.stdout, preview)
    _home_test_expect(
        _home_test_snapshot(home) == before,
        "home repair preview changed state",
    )
    _home_test_run(cwd, "fix")
    _home_test_expect(ignore.read_text() == expected, "home whitelist was not repaired")
    _home_test_expect(
        _home_test_git(home, "show", ":.gitignore") == expected,
        "repair was not staged",
    )
    _home_test_expect(
        _home_test_snapshot(checkout) == checkout_before,
        "home repair changed submodule",
    )
    _home_test_expect(
        _home_test_snapshot(flake) == flake_before,
        "home repair changed unrelated flake",
    )
    _home_test_expect(not list(outside.iterdir()), "home repair changed outside path")
    _home_test_run(cwd, "fix", "--dry-run")


@pytest.mark.parametrize(
    "case",
    [
        "unknown",
        "parent",
        "dirty",
        "staged",
        "advanced",
        "untracked",
        "ignored",
    ],
)
def test_merged_home_submodule_removal_uses_native_git_rules(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    """Reject invalid paths and let Git decide which local data blocks removal."""
    root = _home_test_home_repository(tmp_path)
    monkeypatch.setenv("HOME", str(root))
    relative = "forge.example/owner/demo"
    _home_test_git(root, "submodule", "absorbgitdirs")
    _home_test_fixture_git(root, "commit", "-qm", "Home snapshot")
    target = {"unknown": "missing", "parent": "forge.example/owner"}.get(
        case,
        relative,
    )
    if case.startswith("dirty") or case in {"staged", "advanced"}:
        (root / relative / "LICENSE").write_text("local changes")
        if case in {"staged", "advanced"}:
            _home_test_git(root / relative, "add", "LICENSE")
        if case == "advanced":
            _home_test_fixture_git(root / relative, "commit", "-qm", "Local commit")
    elif case in {"untracked", "ignored"}:
        if case == "ignored":
            exclude = _home_test_git(
                root / relative,
                "rev-parse",
                "--git-path",
                "info/exclude",
            )
            (root / relative / exclude.strip()).write_text("asset\n")
        (root / relative / "asset").write_text("preserve local data")
    before = _home_test_snapshot(root)
    if case == "ignored":
        _home_test_run(root, "rm", target)
        _home_test_expect(not (root / target).exists(), "native removal left submodule")
        return
    _home_test_run(root, "rm", target, code=1)
    _home_test_expect(
        _home_test_snapshot(root) == before,
        "rejected removal changed home state",
    )
    if case.startswith("dirty") or case in {"staged", "advanced", "untracked"}:
        _home_test_run(root, "rm", target, "--force")
        _home_test_expect(
            not (root / target).exists(),
            "forced removal left a dirty submodule",
        )


@pytest.mark.parametrize(
    "location",
    ["home", "nested", "submodule", "flake", "outside"],
)
@pytest.mark.parametrize("name", ["forge.example/owner/demo", "legacy-name"])
def test_merged_home_submodule_removal_stages_metadata_and_preserves_shared_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    location: str,
) -> None:
    """Preview and remove a repository together with its owned whitelist entries."""
    root = _home_test_home_repository(tmp_path / "home")
    monkeypatch.setenv("HOME", str(root))
    relative = "forge.example/owner/demo"
    flake = tmp_path / "flake"
    flake.mkdir()
    _home_test_git(flake, "init", "--quiet")
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
    flake_before = _home_test_snapshot(flake)
    _home_test_git(root, "submodule", "absorbgitdirs")
    if name != relative:
        _home_test_git(
            root,
            "config",
            "--file",
            ".gitmodules",
            "--rename-section",
            f"submodule.{relative}",
            f"submodule.{name}",
        )
        _home_test_git(root, "add", ".gitmodules")
    _home_test_git(
        root,
        "config",
        f"submodule.{name}.url",
        "git@forge.example:owner/demo",
    )
    _home_test_git(root, "config", f"submodule.{name}.active", "true")
    _home_test_git(root, "config", "submodule.unrelated.active", "false")
    local_config_before = _home_test_git(root, "config", "--local", "--list")
    history = root / ".git/modules" / relative
    history_before = _home_test_history_snapshot(history)
    ignore = root / ".gitignore"
    ignore.write_text(
        ignore.read_text()
        + f"!/{relative}/\n!/{relative}/LICENSE\n"
        + "!/forge.example/owner/demo-other\n!/.ssh/\n",
    )
    _home_test_git(root, "add", ".gitignore")
    _home_test_fixture_git(root, "commit", "-qm", "Home snapshot")
    _home_test_run(cwd, "rm", relative)
    _home_test_expect(
        _home_test_snapshot(flake) == flake_before,
        "removal changed unrelated flake",
    )
    _home_test_expect(not list(outside.iterdir()), "removal changed outside path")
    _home_test_expect(
        not (root / relative).exists(),
        "submodule checkout was not removed",
    )
    lines = ignore.read_text().splitlines()
    _home_test_expect(
        f"!/{relative}" not in lines and f"!/{relative}/LICENSE" not in lines,
        lines,
    )
    for entry in (
        "!/forge.example/",
        "!/forge.example/owner/",
        "!/forge.example/owner/demo-other",
        "!/.ssh/",
    ):
        _home_test_expect(entry in lines, lines)
    subject = import_module("packages.afairesi.main")
    _home_test_expect(
        subject.home_submodules(root) == [],
        "submodule remains registered",
    )
    _home_test_expect(
        _home_test_git(root, "config", "--local", "--list") == local_config_before,
        "removal changed local submodule settings",
    )
    _home_test_expect(
        _home_test_history_snapshot(history) == history_before,
        "removal changed stored history",
    )
    staged = _home_test_git(root, "diff", "--cached", "--name-only").splitlines()
    _home_test_expect(set(staged) == {".gitignore", ".gitmodules", relative}, staged)
    _home_test_expect(
        not _home_test_git(root, "diff", "--name-only"),
        "metadata was not staged",
    )
    _home_test_run(root, "fix", "--dry-run")


@pytest.mark.parametrize("storage", ["directory", "absorbed", "deinitialized"])
@pytest.mark.parametrize("url", [False, True])
def test_home_submodule_removal_leaves_storage_to_git(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    storage: str,
    *,
    url: bool,
) -> None:
    """Let native removal preserve history and handle legacy or absent checkouts."""
    root = _home_test_home_repository(tmp_path)
    monkeypatch.setenv("HOME", str(root))
    relative = "forge.example/owner/demo"
    checkout = root / relative
    history = root / ".git/modules" / relative
    if storage != "directory":
        _home_test_git(root, "submodule", "absorbgitdirs")
    history_before = _home_test_history_snapshot(
        checkout / ".git" if storage == "directory" else history,
    )
    _home_test_fixture_git(root, "commit", "-qm", "Home snapshot")
    if storage == "deinitialized":
        _home_test_git(root, "submodule", "deinit", "--", relative)
    if not url:
        _home_test_git(
            root,
            "config",
            "--file",
            ".gitmodules",
            "--unset",
            f"submodule.{relative}.url",
        )
        _home_test_git(root, "add", ".gitmodules")
    config_before = _home_test_git(root, "config", "--local", "--list")
    _home_test_run(root, "rm", relative)
    _home_test_expect(not checkout.exists(), "removal left checkout")
    _home_test_expect(
        _home_test_history_snapshot(history) == history_before,
        "removal changed stored history",
    )
    _home_test_expect(
        _home_test_git(root, "config", "--local", "--list") == config_before,
        "removal changed local configuration",
    )


@pytest.mark.parametrize("registered", [True, False])
@pytest.mark.parametrize("name", ["forge.example/owner/demo", "legacy-name"])
def test_merged_home_repair_preserves_unrelated_local_submodule_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    *,
    registered: bool,
) -> None:
    """Keep unrelated local registrations while repairing convention metadata."""
    root = _home_test_home_repository(tmp_path)
    monkeypatch.setenv("HOME", str(root))
    relative = "forge.example/owner/demo"
    _home_test_git(root, "submodule", "absorbgitdirs")
    if name != relative:
        _home_test_git(
            root,
            "config",
            "--file",
            ".gitmodules",
            "--rename-section",
            f"submodule.{relative}",
            f"submodule.{name}",
        )
        _home_test_git(root, "add", ".gitmodules")
    else:
        _home_test_git(
            root,
            "config",
            f"submodule.{name}.url",
            "git@forge.example:owner/demo",
        )
    _home_test_git(root, "config", f"submodule.{name}.active", "true")
    _home_test_git(root, "config", f"submodule.{name}.update", "none")
    _home_test_git(root, "config", "submodule.active", "forge.example/*")
    _home_test_git(
        root,
        "config",
        "submodule.removed.with.dots.url",
        "git@forge.example:old/repo",
    )
    _home_test_git(root, "config", "submodule.active-only.active", "true")
    _home_test_fixture_git(root, "commit", "-qm", "Home snapshot")
    if not registered:
        _home_test_git(root, "rm", "--", relative)
        (root / ".gitmodules").unlink()
    history = root / ".git/modules" / relative
    history_before = _home_test_history_snapshot(history)
    before = _home_test_snapshot(root)
    _home_test_run(root, "fix", "--dry-run")
    _home_test_expect(
        _home_test_snapshot(root) == before,
        "repair preview changed home state",
    )
    _home_test_run(root, "fix")
    submodule_settings = {
        line
        for line in _home_test_git(root, "config", "--local", "--list").splitlines()
        if line.startswith("submodule.") and not line.startswith("submodule.active=")
    }
    expected_settings = {
        f"submodule.{name}.active=true",
        f"submodule.{name}.update=none",
        "submodule.removed.with.dots.url=git@forge.example:old/repo",
        "submodule.active-only.active=true",
    }
    if name == relative:
        expected_settings.add(f"submodule.{relative}.url=git@forge.example:owner/demo")
    _home_test_expect(
        submodule_settings == expected_settings,
        "repair changed unrelated registrations or lost active settings",
    )
    _home_test_expect(
        _home_test_git(root, "config", "--local", "--get", "submodule.active").strip()
        == "forge.example/*",
        "repair changed shared submodule settings",
    )
    if registered:
        _home_test_expect(
            _home_test_git(
                root,
                "config",
                "--local",
                "--get",
                f"submodule.{name}.update",
            ).strip()
            == "none",
            "repair changed active submodule settings",
        )
    _home_test_expect(
        _home_test_history_snapshot(history) == history_before,
        "repair changed stored history",
    )
    after = _home_test_snapshot(root)
    _home_test_run(root, "fix")
    _home_test_expect(
        _home_test_snapshot(root) == after,
        "repeated repair changed home state",
    )


def test_merged_home_submodule_whitelist_move_preserves_unrelated_entries(
    tmp_path: Path,
) -> None:
    """Preview moves safely and migrate subtree rules without touching sibling paths."""
    subject = import_module("packages.afairesi.main")
    _home_test_git(tmp_path, "init", "--quiet")
    ignore = tmp_path / ".gitignore"
    source = (
        "*\n!/.gitignore\n!/forge.example/\n!/forge.example/team/\n"
        "!/forge.example/team/old\n!/forge.example/team/old/prm/\n"
        "!/forge.example/team/older\n!/forge.example/team/new\n!/future\n"
    )
    ignore.write_text(source)
    _home_test_git(tmp_path, "add", ".gitignore")
    old = Path("forge.example/team/old")
    new = Path("forge.example/team/new")
    _home_test_expect(
        subject._move_home_whitelist(tmp_path, old, new, dry_run=True),  # noqa: SLF001
        tmp_path,
    )
    _home_test_expect(ignore.read_text() == source, ignore)
    _home_test_expect(_home_test_git(tmp_path, "show", ":.gitignore") == source, ignore)
    subject._move_home_whitelist(tmp_path, old, new, dry_run=False)  # noqa: SLF001
    expected = (
        "*\n!/.gitignore\n!/forge.example/\n!/forge.example/team/\n"
        "!/forge.example/team/older\n!/forge.example/team/new\n!/future\n"
        "!/forge.example/team/new/prm/\n"
    )
    _home_test_expect(ignore.read_text() == expected, ignore.read_text())
    _home_test_expect(
        _home_test_git(tmp_path, "show", ":.gitignore") == expected,
        ignore,
    )
    _home_test_expect(
        not subject._move_home_whitelist(tmp_path, old, new, dry_run=True),  # noqa: SLF001
        ignore,
    )


def test_merged_home_cli_exposes_only_home_lifecycle_commands(
    tmp_path: Path,
) -> None:
    """Keep home lifecycle commands separate from flake resource management."""
    commands = ("init", "clone", "fix", "rm")
    help_output = _home_test_run(tmp_path, "--help").stdout
    _home_test_expect(_home_test_run(tmp_path).stdout == help_output, help_output)
    positions = [help_output.index(f"    {name} ") for name in commands]
    _home_test_expect(positions == sorted(positions), help_output)
    for command in commands:
        _home_test_expect(
            "usage:" in _home_test_run(tmp_path, command, "--help").stdout,
            command,
        )
    for arguments in (
        ("unknown-command",),
        ("clone",),
        ("init", "unexpected"),
        ("rm",),
    ):
        _home_test_run(tmp_path, *arguments, code=2)
    _home_test_expect(not list(tmp_path.iterdir()), "invalid requests created files")


def test_merged_home_commands_reject_flake_repositories_without_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prevent home lifecycle commands from modifying a flake repository."""
    _home_test_git(tmp_path, "init", "--quiet")
    (tmp_path / "flake.nix").touch()
    monkeypatch.setenv("HOME", str(tmp_path))
    before = _home_test_snapshot(tmp_path)
    for arguments in (("init",), ("fix",), ("rm", "packages/example")):
        _home_test_run(tmp_path, *arguments, code=1)
        _home_test_expect(
            _home_test_snapshot(tmp_path) == before,
            "home command changed flake state",
        )


def test_cli_help_does_not_load_runtime_rewriter(tmp_path: Path) -> None:
    """Keep the expensive runtime path rewriter out of help startup."""
    result = _run(
        tmp_path,
        "-c",
        "import sys\n"
        "from packages.afairesi.main import main\n"
        "sys.argv = ['afairesi', '--help']\n"
        "try:\n"
        "    main()\n"
        "finally:\n"
        "    assert 'libcst' not in sys.modules\n",
        executable=sys.executable,
    )
    _expect("repo" in result.stdout and "home" in result.stdout, result)


def test_workspace_cli_requires_explicit_scope_and_shows_scoped_help(
    tmp_path: Path,
) -> None:
    """Expose both scopes and reject unscoped or cross-scope commands."""
    executable = os.environ["PACKAGE_E2E_EXECUTABLE"]
    help_output = _run(tmp_path, "--help", executable=executable).stdout
    _expect(
        "aims to minimize the description of a software system" in help_output,
        help_output,
    )
    _expect("    repo " in help_output and "    home " in help_output, help_output)
    _expect(_run(tmp_path, executable=executable).stdout == help_output, help_output)
    for scope in ("repo", "home"):
        scoped_help = _run(tmp_path, scope, "--help", executable=executable).stdout
        _expect(
            _run(tmp_path, scope, executable=executable).stdout == scoped_help,
            scoped_help,
        )
    arguments: tuple[str, ...]
    for arguments in (
        ("add", "packages/example", "python"),
        ("init",),
        ("repo", "init", "unexpected"),
        ("home", "add", "packages/example", "python"),
        ("unknown-scope",),
    ):
        _run(tmp_path, *arguments, executable=executable, code=2)
    for arguments in (
        ("repo", "init"),
        ("repo", "add", "packages/example", "python"),
        ("repo", "mv", "packages/example", "packages/renamed"),
        ("repo", "rm", "packages/example"),
        ("home", "rm", "forge.example/owner/demo"),
    ):
        command_help = _run(
            tmp_path,
            *arguments[:2],
            "--help",
            executable=executable,
        ).stdout
        _expect("--dry-run" not in command_help, command_help)
        for flag in ("-n", "--dry-run"):
            rejected = _run(
                tmp_path,
                *arguments,
                flag,
                executable=executable,
                code=2,
            )
            _expect("unrecognized arguments" in rejected.stderr, rejected)
    for scope in ("repo", "home"):
        fix_help = _run(
            tmp_path,
            scope,
            "fix",
            "--help",
            executable=executable,
        ).stdout
        _expect("--dry-run" in fix_help, fix_help)
    _expect(not any(tmp_path.iterdir()), "invalid scope created state")


@pytest.mark.parametrize("name", ["123", "1app", "class", "async"])
def test_python_names_are_rejected_before_creation_or_rename(name: str) -> None:
    """Reject names that cannot be imported by the installed executable."""
    with _fresh_repository() as root:
        _run(root, "add", "packages/example", "python")
        before = _snapshot(root)
        for arguments in (
            ("add", f"packages/{name}", "python"),
            ("mv", "packages/example", f"packages/{name}"),
        ):
            _run(root, *arguments, code=1)
            _expect(_snapshot(root) == before, arguments)
        _run(root, "add", f"packages/{name}", "nix")


def test_python_module_collisions_are_rejected_before_changes() -> None:
    """Keep distinct resources from sharing one installed Python module."""
    with _fresh_repository() as root:
        _run(root, "add", "packages/foo-bar", "python")
        _run(root, "add", "packages/example", "python")
        before = _snapshot(root)
        for arguments in (
            ("add", "packages/foo_bar", "python"),
            ("mv", "packages/example", "packages/foo_bar"),
        ):
            rejected = _run(root, *arguments, code=1)
            _expect("collide" in rejected.stderr, rejected)
            _expect(_snapshot(root) == before, arguments)
        _run(root, "mv", "packages/foo-bar", "packages/foo_bar")


@pytest.mark.parametrize("remote_kind", ["path", "file"])
def test_package_rename_and_repair_accept_local_origins(remote_kind: str) -> None:
    """Local Git origins do not require a hosted home-repository identity."""
    with _fresh_repository() as root:
        upstream = root / "upstream.git"
        remote = str(upstream) if remote_kind == "path" else upstream.as_uri()
        _fixture_add(root, "packages/example", "python")
        _git(root, "remote", "add", "origin", remote)
        _run(root, "mv", "packages/example", "packages/renamed")
        _run(root, "fix")
        _expect((root / "packages/renamed/main.py").is_file(), "rename failed")
        _preview(root, "fix")


def test_web_formatting_preserves_literal_line_breaks() -> None:
    """Automatic formatting preserves JavaScript strings and HTML preformatted text."""
    with _fresh_repository() as root:
        _fixture_add(root, "packages/site", "html")
        script = root / "packages/site/script.js"
        script.write_text("document.body.dataset.value = `first\n\nsecond`;\n")
        html = root / "packages/site/index.html"
        html.write_text(
            "<!doctype html><html><body><pre>first\n\nsecond</pre></body></html>\n",
        )
        _run(
            root,
            "--no-cache",
            "packages/site/script.js",
            "packages/site/index.html",
            executable="treefmt",
        )
        _expect("`first\n\nsecond`" in script.read_text(), script.read_text())
        _expect("first\n\nsecond" in html.read_text(), html.read_text())


if TYPE_CHECKING:
    from collections.abc import Iterator


def _campaign_run(
    root: Path,
    *arguments: str,
    code: int = 0,
    executable: str | None = None,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(  # noqa: S603
        [
            executable or os.environ["PACKAGE_E2E_EXECUTABLE"],
            *(["repo", "test"] if executable in (None, "afairesi") else []),
            *arguments,
        ],
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


def _campaign_repair_flake(
    root: Path,
    *arguments: str,
    code: int = 0,
) -> subprocess.CompletedProcess[str]:
    """Exercise the private flake repair entry point used by the formatter."""
    return _campaign_run(
        root,
        "-c",
        "from packages.afairesi.main import formatter_main; formatter_main()",
        *arguments,
        code=code,
        executable=sys.executable,
    )


def _campaign_git(root: Path, *arguments: str) -> str:
    return subprocess.run(  # noqa: S603
        ["git", *arguments],  # noqa: S607
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    ).stdout


def _campaign_make_source_package(root: Path, name: str, source: str) -> Path:
    """Create a canonical package fixture without executing its source."""
    package = root / "packages" / name
    package.mkdir(parents=True)
    (root / "flake.nix").touch()
    (package / "default.nix").touch()
    (package / "main.py").touch()
    (package / "test_main.py").write_text(source)
    return package


def _campaign_make_runner_target(
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


def _campaign_prepare_runner_flake(
    root: Path,
    tests: str,
    source: str = "def main():\n    print('ready')\n",
    *,
    name: str = "example",
) -> dict[str, str]:
    """Provide an offline flake backed by this check's real Python environment."""
    package = _campaign_make_runner_target(root, source, tests, name=name)
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


def _campaign_run_runner_cli(
    root: Path,
    environment: dict[str, str],
    command: str,
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    """Exercise test runners through the public standalone command."""
    environment = dict(environment)
    executable_directory = str(Path(os.environ["PACKAGE_E2E_EXECUTABLE"]).parent)
    environment["PATH"] = executable_directory + os.pathsep + environment["PATH"]
    return subprocess.run(  # noqa: S603
        ["afairesi", "repo", "test", command, str(root), *arguments],  # noqa: S607
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def _campaign_prepare_coverage_flake(
    root: Path,
) -> dict[str, str]:
    """Build generated checks and coverage variants with offline Nix inputs."""
    root.mkdir(parents=True)
    _campaign_git(root, "init", "--quiet")
    for filename in (".gitignore", "flake.nix", "flake.lock", "LICENSE"):
        (root / filename).write_text(
            "{}" if filename.endswith((".nix", ".lock")) else "",
        )
    (root / "flake.lock").write_text(
        json.dumps({"nodes": {"root": {}}, "root": "root", "version": 7}),
    )
    _campaign_git(root, "add", ".")
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
        _campaign_manage(root, "add", f"packages/{name}", "python")
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
            "    assert value in {0, 1}\n"
            "@given(st.just(1))\n"
            "def test_generated_only(value):\n"
            "    assert value == 1\n"
            "@pytest.mark.parametrize('message', ['ready', 'alternate'])\n"
            "def test_cli(message, monkeypatch):\n"
            "    monkeypatch.setenv('AFAIRESI_COVERAGE_CHOICE', message)\n"
            "    result = "
            "subprocess.run([os.environ['PACKAGE_E2E_EXECUTABLE']],\n"
            "        capture_output=True, text=True)\n"
            "    assert result.stdout == message + '\\n'\n"
            "    assert result.stderr == ''\n"
        )
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
            f"packageDrv = packages.{json.dumps(name)}; "
            f"check = checks.{json.dumps(name)}; }};",
        )
    _campaign_repair_flake(root)
    (root / "flake.nix").write_text(
        "{ outputs = _: let lib = import ./prm/builders/builders.nix; "
        f"system = {json.dumps(system)}; "
        "mkCheck = name: attrs: script: let build = current: (builtins.derivation { "
        "inherit system; inherit (current) name src PACKAGE_E2E_EXECUTABLE; "
        f"builder = {json.dumps(bash)}; "
        f"PATH = {json.dumps(sys.prefix + '/bin:' + environment['PATH'])}; "
        f"PYTHONPATH = {json.dumps(os.pathsep.join(sys.path))}; "
        'args = [ "-e" (builtins.toFile "check-builder" current.buildCommand) ]; '
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
    _campaign_git(root, "add", ".")
    return environment


def _campaign_expect(condition: bool, detail: object) -> None:  # noqa: FBT001
    """Report an invariant failure with its observed state."""
    if not condition:
        raise AssertionError(detail)


def _campaign_repository(root: Path, *, object_format: str = "sha1") -> Path:
    """Index a minimal flake in an existing temporary directory."""
    _campaign_git(root, "init", "--quiet", f"--object-format={object_format}")
    for name in (".gitignore", "flake.nix", "flake.lock", "LICENSE"):
        (root / name).write_text("", encoding="utf-8")
    _campaign_git(root, "add", ".")
    return root


@contextmanager
def _campaign_fresh_repository(*, object_format: str = "sha1") -> Iterator[Path]:
    """Give every generated example its own repository and cleanup."""
    with TemporaryDirectory(prefix="afairesi-contract-") as directory:
        yield _campaign_repository(Path(directory), object_format=object_format)


def _campaign_snapshot(
    root: Path,
    *,
    exclude: tuple[str, ...] = (),
) -> tuple[object, ...]:
    """Observe campaign state with the shared repository snapshot."""
    return _snapshot(root, exclude=exclude)


def _campaign_runner_package(
    root: Path,
    name: str,
    tests: str,
    source: str = "",
) -> None:
    """Add another offline runnable package without replacing the fixture flake."""
    flake = (root / "flake.nix").read_text()
    _campaign_make_runner_target(root, source, tests, name=name)
    (root / "flake.nix").write_text(flake)
    _campaign_git(root, "add", ".")


def _campaign_campaign(
    root: Path,
    environment: dict[str, str],
    command: str,
    *arguments: str,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run an omitted target and preserve original sources, settings, and refs."""
    before = _campaign_snapshot(root, exclude=("tmp",))
    result = subprocess.run(  # noqa: S603
        [os.environ["PACKAGE_E2E_EXECUTABLE"], "repo", "test", command, *arguments],
        cwd=cwd or root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    _campaign_expect(
        _campaign_snapshot(root, exclude=("tmp",)) == before,
        "campaign changed original sources or Git state",
    )
    return result


def _campaign_manage(
    root: Path,
    *arguments: str,
    code: int = 0,
) -> subprocess.CompletedProcess[str]:
    """Set up repository fixtures through the management executable."""
    return _run(root, *arguments, code=code)


def test_campaigns_route_targets_and_forward_test_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Route repository and package campaigns with their requested test filters."""
    campaign_subject = import_module("packages.afairesi.main")
    root = _campaign_repository(tmp_path)
    package = _campaign_make_source_package(root, "alpha", "def test_alpha(): pass\n")
    nested = package / "prm/nested"
    nested.mkdir(parents=True)
    observed: list[Path] = []
    forwarded: list[tuple[object, ...]] = []

    def run(target: Path, *arguments: object) -> bool:
        observed.append(target)
        forwarded.append(arguments)
        return True

    for runner in (
        "_run_coverage",
        "_run_test_package",
        "_run_test_repository",
    ):
        monkeypatch.setattr(campaign_subject, runner, run)
    for cwd in (package, nested):
        monkeypatch.chdir(cwd)
        observed.clear()
        monkeypatch.setattr(sys, "argv", ["afairesi", "repo", "test"])
        with pytest.raises(SystemExit) as combined:
            campaign_subject.main()
        _campaign_expect(combined.value.code == 0 and observed == [root] * 4, observed)
        for path in (
            ("coverage",),
            ("property",),
            ("mutation",),
            ("performance",),
        ):
            targets = (
                ((), root),
                ((".",), root),
                (("packages/alpha",), package),
                ((str(package),), package),
            )
            for arguments, expected in targets:
                filters = (
                    ["-k", "keyword", "-m", "marker"] if path != ("coverage",) else []
                )
                monkeypatch.setattr(
                    sys,
                    "argv",
                    ["afairesi", "repo", "test", *path, *arguments, *filters],
                )
                with pytest.raises(SystemExit) as completed:
                    campaign_subject.main()
                _campaign_expect(completed.value.code == 0, path)
                _campaign_expect(observed[-1] == expected, (path, arguments, observed))
                if filters:
                    _campaign_expect(
                        forwarded[-1][-1]
                        == campaign_subject.TestSelection(
                            keywords="keyword",
                            markers="marker",
                        ),
                        forwarded[-1],
                    )


def test_cli_contracts_validate_help_interfaces_and_budgets() -> None:
    """Expose consistent commands and reject invalid requests before creating state."""
    with TemporaryDirectory(prefix="afairesi-cli-") as directory:
        root = Path(directory)
        commands = ((), ("coverage",), ("property",), ("mutation",), ("performance",))
        help_output = _campaign_run(root, "--help").stdout
        positions = [help_output.index(f"    {path[0]} ") for path in commands[1:]]
        _campaign_expect(positions == sorted(positions), help_output)
        for path in commands:
            option = _campaign_run(root, *path, "--help").stdout
            _campaign_expect(
                "usage:" in option,
                path,
            )
        environment = dict(os.environ)
        environment["PATH"] = (
            str(Path(os.environ["PACKAGE_E2E_EXECUTABLE"]).parent)
            + os.pathsep
            + environment["PATH"]
        )
        _campaign_expect(
            _campaign_run(
                root,
                "--help",
                executable="afairesi",
                environment=environment,
            ).stdout
            == _campaign_run(root, "--help").stdout,
            "standalone command unavailable through PATH",
        )
        _campaign_expect(
            "not inside a Git repository" in _campaign_run(root, code=1).stderr,
            "bare test must require a repository",
        )
        for command in ("coverage", "property", "mutation", "performance"):
            _campaign_run(root, command, code=1)
        for command in ("property", "mutation", "performance"):
            for timeout in ("nan", "inf", "-inf", "0", "-1"):
                _campaign_run(root, command, str(root), f"--timeout={timeout}", code=2)
        _campaign_run(root, "property", str(root), "--max-examples", "0", code=2)
        for arguments in (
            ("--max-mutations", "0"),
            ("--max-mutations", "-1"),
            ("--lines", "0"),
            ("--lines", "3:2"),
            ("--lines", "bad"),
            ("--operator", "["),
        ):
            _campaign_run(root, "mutation", str(root), *arguments, code=2)
        _campaign_expect(not any(root.iterdir()), "invalid CLI request created state")


@pytest.mark.parametrize("failing", [False, True])
def test_performance_profiles_tests_and_cli_without_changing_sources(
    failing: bool,  # noqa: FBT001
) -> None:
    """Retain native parent and CLI profiles, even when selected tests fail."""
    with TemporaryDirectory(prefix="afairesi-profile-") as directory:
        root = Path(directory) / "source with spaces"
        source = (
            "import sys\n"
            "def work():\n"
            "    return sum(range(10000))\n"
            "def main():\n"
            "    work()\n"
            "    print(sys.argv[1])\n"
        )
        tests = (
            "import os, subprocess\n"
            "from pathlib import Path\n"
            "from hypothesis import given, example, strategies as st\n"
            "def test_cli():\n"
            "    result = subprocess.run([os.environ['PACKAGE_E2E_EXECUTABLE'], "
            "'hello world'], capture_output=True, text=True)\n"
            "    assert result.returncode == 0, result.stderr\n"
            "    assert result.stdout == 'hello world\\n'\n"
            "    source = Path('packages/example/main.py')\n"
            "    stamp = source.stat()\n"
            "    source.write_text(source.read_text().replace('sys.argv[1]', "
            "'sys.argv[2]'))\n"
            "    os.utime(source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))\n"
            "    result = subprocess.run([os.environ['PACKAGE_E2E_EXECUTABLE'], "
            "'stale', 'fresh'], capture_output=True, text=True)\n"
            "    assert result.returncode == 0, result.stderr\n"
            "    assert result.stdout == 'fresh\\n'\n"
            f"    assert {not failing!r}\n"
            "@given(st.integers())\n"
            "@example(7)\n"
            "def test_explicit(value):\n"
            "    with Path('examples').open('a') as output:\n"
            "        output.write(str(value) + '\\n')\n"
            "def test_unselected():\n"
            "    raise AssertionError('selection ignored')\n"
        )
        environment = _campaign_prepare_runner_flake(root, tests, source)
        result = _campaign_campaign(
            root,
            environment,
            "performance",
            "-k",
            "cli or explicit",
        )
        _campaign_expect(result.returncode == int(failing), result)
        output = result.stdout + result.stderr
        _campaign_expect("slowest" in output and "test_cli" in output, result)
        (workspace,) = (root / "tmp").glob("python-performance-example-*")
        _campaign_expect(
            "7" in (workspace / "examples").read_text().splitlines()
            and len((workspace / "examples").read_text().splitlines()) > 1,
            workspace,
        )
        profiles = workspace / "profiles"
        _campaign_expect(str(profiles) in result.stdout, result)
        _campaign_expect(
            (root / "packages/example/main.py").read_text() == source,
            root,
        )
        (bytecode,) = (workspace / "packages/example/__pycache__").glob("main.*.pyc")
        _campaign_expect(bytecode.is_file(), workspace)
        for script, function in (("run-tests", "test_cli"), ("package-entry", "work")):
            reports = list(profiles.glob(f"{script}-*.prof"))
            _campaign_expect(
                len(reports) == (1 if script == "run-tests" else 2),
                reports,
            )
            for report in reports:
                stats = pstats.Stats(str(report))
                functions = stats.get_stats_profile().func_profiles
                _campaign_expect(function in functions, functions)


@pytest.mark.parametrize(
    "layout",
    ["empty", "nonpython", "untested", "single_untested"],
)
def test_campaigns_handle_nonrunnable_targets_without_changes(layout: str) -> None:
    """Skip untested repository packages and reject nonrunnable targets safely."""
    with _campaign_fresh_repository() as root:
        target = root
        if layout == "nonpython":
            _campaign_manage(root, "add", "packages/web", "html")
        elif layout in {"untested", "single_untested"}:
            target = _campaign_make_runner_target(root, "", "")
            (target / "test_main.py").unlink()
            if layout == "untested":
                target = root
        before = _campaign_snapshot(root)
        for command in ("property", "mutation"):
            result = _campaign_run(
                root,
                command,
                str(target),
                code=0 if layout == "untested" else 1,
            )
            if layout == "untested":
                _campaign_expect(
                    "Skipping example: no test_main.py" in result.stdout
                    and "0 passed, 0 failed, 1 skipped" in result.stdout,
                    result,
                )
            elif layout != "single_untested":
                _campaign_expect("no Python packages found" in result.stderr, result)
        if layout == "empty":
            _campaign_expect(
                "no Python packages found"
                in _campaign_run(root, "coverage", code=1).stderr,
                "coverage",
            )
        _campaign_expect(
            _campaign_snapshot(root) == before,
            "nonrunnable target created state",
        )


@pytest.mark.parametrize(
    "failure",
    [None, "coverage", "property", "mutation", "performance"],
)
@pytest.mark.parametrize("failure_kind", ["policy", "process"])
def test_combined_campaigns_continue_after_failures_and_report_one_exit_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str | None,
    failure_kind: str,
) -> None:
    """Execute every campaign in order even when an earlier campaign fails."""
    campaign_subject = import_module("packages.afairesi.main")
    _campaign_repository(tmp_path)
    monkeypatch.chdir(tmp_path)
    observed: list[str] = []

    def run(command: str) -> bool:
        observed.append(command)
        if command == failure and failure_kind == "process":
            raise subprocess.CalledProcessError(
                17,
                ["native-tool"],
                output="native campaign failure\n",
            )
        if command == failure and command != "mutation":
            message = "campaign failed"
            raise campaign_subject.CommandError(message)
        return command != failure

    def coverage_run(_target: Path) -> bool:
        return run("coverage")

    def runner(
        _target: Path,
        command: str,
        *_arguments: object,
    ) -> bool:
        return run(command)

    monkeypatch.setattr(campaign_subject, "_run_coverage", coverage_run)
    monkeypatch.setattr(campaign_subject, "_run_test_repository", runner)
    monkeypatch.setattr(sys, "argv", ["afairesi", "repo", "test", "--help"])
    with pytest.raises(SystemExit) as help_status:
        campaign_subject.main()
    _campaign_expect(help_status.value.code == 0 and not observed, observed)
    capsys.readouterr()
    monkeypatch.setattr(sys, "argv", ["afairesi", "repo", "test"])
    with pytest.raises(SystemExit) as completed:
        campaign_subject.main()
    _campaign_expect(completed.value.code == (0 if failure is None else 1), completed)
    _campaign_expect(
        observed == ["coverage", "property", "mutation", "performance"],
        observed,
    )
    output = capsys.readouterr()
    _campaign_expect("Campaign summary:" in output.out, output)
    for command in observed:
        status = "failed" if command == failure else "passed"
        _campaign_expect(f"  {command}: {status}\n" in output.out, output)
    if failure is not None and failure_kind == "process":
        _campaign_expect("native campaign failure" in output.err, output)


def test_coverage_checks_measure_subprocesses_without_changing_sources() -> None:
    """Build real checks, measure CLI lines, and keep reports outside the checkout."""
    with TemporaryDirectory(prefix="afairesi-coverage-") as directory:
        root = Path(directory) / "source with spaces"
        environment = _campaign_prepare_coverage_flake(root)
        before = _campaign_snapshot(root)
        expression = (
            f"let f = builtins.getFlake {json.dumps('git+' + root.as_uri())}; "
            "in f.checks.${builtins.currentSystem}.example"
        )
        plain = _campaign_run(
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
        _campaign_expect(
            not any(
                path.name.startswith(".coverage")
                or path.name in {"html", "coverage.json"}
                for path in Path(plain.stdout.strip()).iterdir()
            ),
            "ordinary checks produced coverage artifacts",
        )
        result = _campaign_run_runner_cli(root, environment, "coverage")
        _campaign_expect(not result.returncode, result)
        reports = [
            line
            for line in result.stdout.splitlines()
            if line.endswith("/html/index.html")
        ]
        expected_reports = 2
        _campaign_expect(len(reports) == expected_reports, result)
        for line in reports:
            report = Path(line.split(": ", 1)[1]).parents[1]
            files = json.loads((report / "coverage.json").read_text())["files"]
            _campaign_expect(
                len(files) == 1 and next(iter(files)).endswith("/main.py"),
                files,
            )
            _campaign_expect(
                set(next(iter(files.values()))["executed_lines"]) == {1, 2, 3, 4, 6},
                files,
            )
            measured = next(iter(files.values()))
            _campaign_expect(
                {tuple(branch) for branch in measured["executed_branches"]}
                == {(3, 4), (3, 6)}
                and any(
                    context.endswith("test_main.py::test_cli[alternate]")
                    for context in measured["contexts"]["4"]
                )
                and any(
                    context.endswith("test_main.py::test_cli[ready]")
                    for context in measured["contexts"]["6"]
                ),
                measured,
            )
        _campaign_expect(
            _campaign_snapshot(root) == before and not (root / "tmp").exists(),
            "coverage changed repository state",
        )


def test_coverage_continues_after_failed_nix_builds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Check build failures and continuation at the Nix process boundary."""
    campaign_subject = import_module("packages.afairesi.main")
    _campaign_repository(tmp_path)
    failed = _campaign_make_source_package(
        tmp_path,
        "example",
        "def test_ready(): pass\n",
    )
    _campaign_make_source_package(tmp_path, "z-last", "def test_ready(): pass\n")
    untested = _campaign_make_source_package(tmp_path, "untested", "")
    (untested / "test_main.py").unlink()
    report = tmp_path / "tmp/report"
    (report / "html").mkdir(parents=True)
    (report / "html/index.html").touch()
    builds: list[str] = []

    def run(arguments: list[str], **_keywords: object) -> object:
        if arguments[:2] != ["nix", "build"]:
            return DEFAULT
        builds.append(arguments[-1])
        if arguments[-1].endswith("#example-coverage"):
            raise subprocess.CalledProcessError(
                17,
                arguments,
                stderr="error: coverage builder failed\n",
            )
        return subprocess.CompletedProcess(arguments, 0, str(report) + "\n")

    monkeypatch.setattr(
        campaign_subject.subprocess,
        "run",
        Mock(wraps=subprocess.run, side_effect=run),
    )
    before = _campaign_snapshot(tmp_path)
    for target, status in ((tmp_path, 1), (failed, 17)):
        monkeypatch.setattr(
            sys,
            "argv",
            ["afairesi", "repo", "test", "coverage", str(target)],
        )
        with pytest.raises(SystemExit) as completed:
            campaign_subject.main()
        _campaign_expect(completed.value.code == status, completed)
        output = capsys.readouterr()
        _campaign_expect("error: coverage builder failed" in output.err, output)
        if target == tmp_path:
            _campaign_expect(
                "1 passed, 1 failed, 1 skipped" in output.out
                and "z-last:" in output.out
                and str(report / "html/index.html") in output.out
                and "afairesi repo test coverage: example:" in output.err,
                output,
            )
        _campaign_expect(_campaign_snapshot(tmp_path) == before, target)
    prefix = f"git+{tmp_path.as_uri()}#"
    _campaign_expect(
        builds
        == [
            prefix + name
            for name in ("example-coverage", "z-last-coverage", "example-coverage")
        ],
        builds,
    )


def test_coverage_rejects_missing_report_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Validate coverage output guards without rebuilding a successful package."""
    campaign_subject = import_module("packages.afairesi.main")
    root = tmp_path / "source"
    root.mkdir()
    _campaign_repository(root)
    package = _campaign_make_source_package(root, "example", "def test_ready(): pass\n")
    report = tmp_path / "report"
    report.mkdir()
    builds: list[list[str]] = []

    def run(
        arguments: list[str],
        **_keywords: object,
    ) -> object:
        if arguments[:2] == ["nix", "build"]:
            builds.append(arguments)
            return subprocess.CompletedProcess(arguments, 0, str(report) + "\n")
        return DEFAULT

    monkeypatch.setattr(
        campaign_subject.subprocess,
        "run",
        Mock(wraps=subprocess.run, side_effect=run),
    )
    before = _campaign_snapshot(root)
    message = "no HTML report"
    monkeypatch.setattr(
        sys,
        "argv",
        ["afairesi", "repo", "test", "coverage", str(package)],
    )
    with pytest.raises(SystemExit) as completed:
        campaign_subject.main()
    _campaign_expect(
        completed.value.code == 1 and message in capsys.readouterr().err,
        completed,
    )
    _campaign_expect(len(builds) == 1, builds)
    _campaign_expect(
        _campaign_snapshot(root) == before,
        "coverage validation modified source state",
    )


@pytest.mark.parametrize(
    "command",
    ["coverage", "property", "mutation", "performance"],
)
@pytest.mark.parametrize("failure", ["missing-output", "invalid-flake"])
def test_package_campaign_preserves_native_nix_errors(
    tmp_path: Path,
    command: str,
    failure: str,
) -> None:
    """Use Nix as the oracle rather than guessing whether a flake output exists."""
    root = tmp_path / "source with spaces"
    environment = _campaign_prepare_runner_flake(root, "def test_ready(): pass\n")
    (root / "flake.nix").write_text(
        "{ outputs = _: {}; }" if failure == "missing-output" else "{",
    )
    _campaign_git(root, "add", "flake.nix")
    attribute = (
        "example-coverage" if command == "coverage" else "example-test-environment"
    )
    expected = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "nix",
            "build",
            "--no-link",
            "--no-update-lock-file",
            "--print-out-paths",
            f"git+{root.as_uri()}#{attribute}",
        ],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    _campaign_expect(expected.returncode != 0, expected)
    before = _campaign_snapshot(root, exclude=("tmp",))
    actual = _campaign_run(
        root,
        command,
        str(root / "packages/example"),
        code=expected.returncode,
        environment=environment,
    )
    _campaign_expect(
        expected.stderr.strip() in actual.stderr,
        (expected.stderr, actual.stderr),
    )
    _campaign_expect(
        _campaign_snapshot(root, exclude=("tmp",)) == before,
        "failed campaign changed package sources or Git state",
    )


@pytest.mark.parametrize("failure", ["assertion", "collection", "selection", "usage"])
def test_package_property_preserves_native_pytest_status(
    tmp_path: Path,
    failure: str,
) -> None:
    """Preserve pytest's distinct failures instead of collapsing them to exit one."""
    root = tmp_path / "source"
    tests = {
        "assertion": "def test_failure(): assert False, 'native assertion failure'\n",
        "collection": "def test_broken(:\n    pass\n",
        "selection": "def test_ready(): pass\n",
        "usage": "def test_ready(): pass\n",
    }[failure]
    environment = _campaign_prepare_runner_flake(root, tests)
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    environment.pop("PYTEST_ADDOPTS", None)
    selectors = {
        "assertion": (),
        "collection": (),
        "selection": ("-k", "missing_test"),
        "usage": ("-k", "("),
    }[failure]
    package = root / "packages/example"
    expected = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-B",
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-q",
            *selectors,
            str(package / "test_main.py"),
        ],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    _campaign_expect(expected.returncode != 0, expected)
    before = _campaign_snapshot(root, exclude=("tmp",))
    actual = _campaign_run(
        root,
        "property",
        str(package),
        *selectors,
        code=expected.returncode,
        environment=environment,
    )
    diagnostics = [
        line.strip()
        for line in (expected.stdout + expected.stderr).splitlines()
        if line.startswith("ERROR:") or "SyntaxError:" in line
    ]
    for diagnostic in diagnostics:
        _campaign_expect(diagnostic in actual.stderr, (diagnostic, actual.stderr))
    _campaign_expect(
        _campaign_snapshot(root, exclude=("tmp",)) == before,
        "pytest failure changed original package sources or Git state",
    )


def test_coverage_skips_untested_packages_and_continues_after_invalid_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Validate package eligibility and continuation separately from Nix builds."""
    campaign_subject = import_module("packages.afairesi.main")
    _campaign_repository(tmp_path)
    _campaign_make_source_package(tmp_path, "alpha", "def invalid(")
    untested = _campaign_make_source_package(tmp_path, "untested", "")
    (untested / "test_main.py").unlink()
    ready = _campaign_make_source_package(
        tmp_path,
        "z-last",
        "def test_ready(): pass\n",
    )
    built: list[Path] = []
    monkeypatch.setattr(campaign_subject, "_build_package_coverage", built.append)
    before = _campaign_snapshot(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        ["afairesi", "repo", "test", "coverage", str(tmp_path)],
    )
    with pytest.raises(SystemExit) as completed:
        campaign_subject.main()
    _campaign_expect(completed.value.code == 1, "invalid source must fail coverage")
    output = capsys.readouterr()
    _campaign_expect(built == [ready], built)
    _campaign_expect(
        "1 passed, 1 failed, 1 skipped" in output.out
        and "afairesi repo test coverage: alpha:" in output.err,
        output,
    )
    _campaign_expect(
        _campaign_snapshot(tmp_path) == before,
        "coverage eligibility modified sources",
    )


@pytest.mark.parametrize("failure", ["counterexample", "timeout"])
def test_property_campaigns_continue_after_failures(failure: str) -> None:
    """Retain counterexamples and terminate stalled descendants before continuing."""
    with TemporaryDirectory(prefix="afairesi-failed-campaign-") as directory:
        root = Path(directory) / "source with spaces"
        environment = _campaign_prepare_runner_flake(
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
        _campaign_runner_package(root, "alpha", bad_tests)
        _campaign_runner_package(root, "untested", "")
        (root / "packages/untested/test_main.py").unlink()
        result = _campaign_campaign(
            root,
            environment,
            "property",
            "--max-examples",
            "1",
            "--timeout",
            "20" if failure == "counterexample" else "2",
        )
        _campaign_expect(
            result.returncode == 1 and "1 passed, 1 failed, 1 skipped" in result.stdout,
            result,
        )
        (failed,) = (root / "tmp").glob("python-property-alpha-*")
        diagnostic = (
            "Falsifying example" if failure == "counterexample" else "timed out"
        )
        _campaign_expect(
            diagnostic
            in result.stdout + result.stderr + (failed / "tests.log").read_text(),
            result,
        )
        if failure == "timeout":
            pid = int((failed / "child.pid").read_text())
            status = Path(f"/proc/{pid}/status")
            _campaign_expect(
                not status.exists() or "State:\tZ" in status.read_text(),
                "timed-out suite left a running descendant",
            )


@settings(deadline=None)
@given(max_examples=st.integers(min_value=1, max_value=5))
@example(max_examples=5)
def test_property_campaigns_generate_cases_in_isolated_sources(
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
            "import os, subprocess, sys\n"
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
            "    child = subprocess.run([sys.executable, '-c', "
            '"import importlib; '
            "print(importlib.import_module('packages.git-example.main').__file__)\"], "
            "cwd=Path.home(), capture_output=True, text=True)\n"
            "    assert child.returncode == 0, child.stderr\n"
            "    assert Path(child.stdout.strip()).is_relative_to("
            "Path.cwd() / 'packages')\n"
        )
        environment = _campaign_prepare_runner_flake(
            root,
            tests,
            source,
            name="git-example",
        )
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
        before = _campaign_snapshot(root, exclude=("tmp",))
        result = _campaign_run_runner_cli(
            root,
            environment,
            "property",
            "--max-examples",
            str(max_examples),
        )
        _campaign_expect(
            not result.returncode and "1 passed, 0 failed, 0 skipped" in result.stdout,
            result,
        )
        _campaign_expect(
            _campaign_snapshot(root, exclude=("tmp",)) == before,
            "property campaign modified source",
        )
        workspaces = list((root / "tmp").glob("python-property-git-example-*"))
        _campaign_expect(len(workspaces) == 1, workspaces)
        for workspace in workspaces:
            _campaign_expect(
                len((workspace / "examples").read_text().splitlines())
                == max_examples + 1,
                workspace,
            )
            _campaign_expect(
                f"{max_examples} passing examples"
                in (workspace / "tests.log").read_text(),
                workspace,
            )
            _campaign_expect(
                "2 passed" in (workspace / "tests.log").read_text()
                and not list(workspace.glob("test-runtime-*")),
                workspace,
            )
        _campaign_expect(
            sentinel.read_text() == "preserve",
            caller_home,
        )


def test_mutation_campaigns_reject_invalid_baselines_and_continue() -> None:
    """Check fixed baseline failures once, independently of generated values."""
    with TemporaryDirectory(prefix="afairesi-baselines-") as directory:
        root = Path(directory) / "source with spaces"
        environment = _campaign_prepare_runner_flake(
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
            _campaign_runner_package(root, name, baseline)
        _campaign_runner_package(root, "untested", "")
        (root / "packages/untested/test_main.py").unlink()
        result = _campaign_campaign(
            root,
            environment,
            "mutation",
            "--timeout",
            "10",
            "--max-mutations",
            "1",
        )
        _campaign_expect(
            result.returncode == 1
            and "1 passed, 4 failed, 1 skipped" in result.stdout
            and "baseline.log" in result.stderr,
            result,
        )
        for name in baselines:
            (failed,) = (root / "tmp").glob(f"python-mutation-{name}-*")
            _campaign_expect(
                (failed / "baseline.log").is_file()
                and not (failed / "session.sqlite").exists(),
                failed,
            )


def test_mutation_campaigns_report_empty_plans() -> None:
    """Accept an empty mutation plan when the baseline suite passes."""
    with TemporaryDirectory(prefix="afairesi-empty-mutations-") as directory:
        root = Path(directory) / "source"
        tests = (
            "from packages.example import main\n"
            "from hypothesis import given, example, strategies as st\n"
            "@given(st.just(1))\n"
            "@example(0)\n"
            "def test_explicit(value):\n"
            "    assert value in {0, 1} and main is not None\n"
        )
        environment = _campaign_prepare_runner_flake(root, tests, "")
        result = _campaign_campaign(root, environment, "mutation")
        (workspace,) = (root / "tmp").glob("python-mutation-example-*")
        _campaign_expect(
            not result.returncode
            and not (workspace / "results.jsonl").read_text().strip(),
            result,
        )


@pytest.mark.parametrize(
    "problem",
    [None, "timeout", "error", "pending", "skipped", "incompetent"],
)
def test_mutation_runs_preserve_engine_failure_status(
    tmp_path: Path,
    problem: str | None,
) -> None:
    """Check mixed results without running another mutation campaign."""
    campaign_subject = import_module("packages.afairesi.main")
    outcomes = ["killed", "survived", *([problem] if problem is not None else [])]
    records = []
    for outcome in outcomes:
        result = (
            None
            if outcome == "pending"
            else {
                "worker_outcome": "exception"
                if outcome == "error"
                else "skipped"
                if outcome == "skipped"
                else "normal",
                "test_outcome": outcome
                if outcome in {"killed", "survived", "incompetent"}
                else "killed",
                "output": "timeout" if outcome == "timeout" else "native pytest output",
            }
        )
        records.append(json.dumps([{}, result]))
    (tmp_path / "results.jsonl").write_text("\n".join(records) + "\n")
    succeeded = campaign_subject._mutation_run_succeeded(tmp_path)  # noqa: SLF001 - Check engine status without running mutations.
    _campaign_expect(
        succeeded == (problem not in {"error", "pending", "incompetent"}),
        problem,
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
        environment = _campaign_prepare_runner_flake(
            root,
            tests,
            source,
            name="git-example",
        )
        target_line = 2
        focused = _campaign_campaign(
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
        _campaign_expect(not focused.returncode, focused)
        (focused_workspace,) = (root / "tmp").glob("python-mutation-git-example-*")
        _campaign_expect(
            (focused_workspace / "report.html").stat().st_size > 0,
            focused,
        )
        plan_path = focused_workspace / "mutation-plan.json"
        plan = json.loads(plan_path.read_text())
        defects = [
            json.loads(line)
            for line in (focused_workspace / "results.jsonl").read_text().splitlines()
        ]
        selected = [
            (item, result)
            for item, result in defects
            if result["worker_outcome"] != "skipped"
        ]
        _campaign_expect(
            len(plan["mutations"]) == len(selected) == 1
            and selected[0][1]["test_outcome"] == "killed"
            and selected[0][0]["mutations"][0]["start_pos"][0] == target_line,
            selected,
        )
        existing_workspaces = set((root / "tmp").iterdir())
        weaker = _campaign_campaign(
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
        _campaign_expect(not weaker.returncode, weaker)
        (weaker_workspace,) = set((root / "tmp").iterdir()) - existing_workspaces
        weaker_defects = [
            json.loads(line)
            for line in (weaker_workspace / "results.jsonl").read_text().splitlines()
        ]
        weaker_selected = [
            (item, result)
            for item, result in weaker_defects
            if result["worker_outcome"] != "skipped"
        ]
        _campaign_expect(
            len(weaker_selected) == 1
            and weaker_selected[0][0]["mutations"] == selected[0][0]["mutations"]
            and weaker_selected[0][1]["test_outcome"] == "survived",
            weaker_selected,
        )
        campaign_subject = import_module("packages.afairesi.main")
        package_source = weaker_workspace / "packages/git-example/main.py"
        package_source.write_text(
            source.replace(f"return {value}\n", f"return {value + 10}\n"),
        )
        with pytest.raises(campaign_subject.CommandError, match="does not match"):
            campaign_subject._select_mutations(  # noqa: SLF001 - Check validation without rerunning a campaign.
                weaker_workspace,
                "git-example",
                campaign_subject.TestSelection(mutation_plan=plan_path),
            )
        campaign_subject._select_mutations(  # noqa: SLF001 - Check unfiltered selection without executing every mutant.
            focused_workspace,
            "git-example",
            campaign_subject.TestSelection(),
        )
        unfiltered = json.loads(plan_path.read_text())
        _campaign_expect(
            len(unfiltered["mutations"]) == len(defects)
            and len(unfiltered["mutations"]) > 1,
            unfiltered,
        )


def test_campaign_home_target_requires_native_git_traversal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Reject home scope instead of silently traversing registered submodules."""
    campaign_subject = import_module("packages.afairesi.main")
    _campaign_git(tmp_path, "init", "--quiet")
    (tmp_path / ".gitmodules").touch()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["afairesi", "repo", "test"])
    with pytest.raises(SystemExit) as completed:
        campaign_subject.main()
    _campaign_expect(completed.value.code == 1, completed)
    _campaign_expect("git submodule foreach" in capsys.readouterr().err, completed)


inspection_subject = import_module("packages.afairesi.main")


def _inspection_expect(condition: bool, message: str) -> None:  # noqa: FBT001
    if not condition:
        raise AssertionError(message)


def _inspection_discovery(source: str) -> dict[str, Any]:
    return cast(
        "dict[str, Any]",
        inspection_subject.ArgumentDiscovery(ast.parse(source)).report(),
    )


def test_argparse_helpers_factories_and_subcommands() -> None:
    """Resolve aliases, returned parsers, groups, and helper contexts."""
    report = _inspection_discovery("""
import argparse as ap
def configure(cli):
    group = cli.add_mutually_exclusive_group()
    group.add_argument("--quiet", action="store_true")
def parser():
    cli = ap.ArgumentParser()
    cli.add_argument("input", type=str)
    subs = cli.add_subparsers(dest="command")
    build = subs.add_parser("build")
    build.add_argument("-j", "--jobs", type=int, default=2, choices=[1, 2])
    configure(build)
    return cli
def main():
    parser().parse_args()
""")
    _inspection_expect(list(report["commands"]) == ["build"], "lost subcommand")
    _inspection_expect(
        report["arguments"] == [{"names": ["input"], "options": {"type": "str"}}],
        "wrong positional scope",
    )
    rows = {
        tuple(row["names"]): row for row in report["commands"]["build"]["arguments"]
    }
    _inspection_expect(
        rows[("-j", "--jobs")]["options"]
        == {"type": "int", "default": 2, "choices": [1, 2]},
        "lost metadata",
    )
    _inspection_expect(
        rows[("--quiet",)]
        == {"names": ["--quiet"], "options": {"action": "store_true"}},
        "lost helper context",
    )
    _inspection_expect(
        not report["warnings"],
        "resolved declarations produced warnings",
    )


def test_nested_commands_and_commands_without_arguments() -> None:
    """Keep empty commands and arguments with the same name in distinct scopes."""
    report = _inspection_discovery("""
import argparse
cli = argparse.ArgumentParser()
cli.add_argument("--verbose", action="store_true")
commands = cli.add_subparsers()
commands.add_parser("version")
remote = commands.add_parser("remote")
remote.add_argument("--verbose", action="count")
subcommands = remote.add_subparsers()
subcommands.add_parser("list")
add = subcommands.add_parser("add")
add.add_argument("url")
""")
    _inspection_expect(
        report
        == {
            "status": "argparse",
            "arguments": [
                {"names": ["--verbose"], "options": {"action": "store_true"}},
            ],
            "commands": {
                "version": {"arguments": [], "commands": {}},
                "remote": {
                    "arguments": [
                        {"names": ["--verbose"], "options": {"action": "count"}},
                    ],
                    "commands": {
                        "list": {"arguments": [], "commands": {}},
                        "add": {
                            "arguments": [{"names": ["url"], "options": {}}],
                            "commands": {},
                        },
                    },
                },
            },
            "warnings": [],
        },
        "command tree lost scope or empty commands",
    )


def test_unknown_declarations_are_explicit() -> None:
    """Keep unresolved metadata and parser bindings visible."""
    report = _inspection_discovery("""
from argparse import ArgumentParser as Parser
cli = Parser()
cli.add_argument("--output", default=compute_default(), **options)
other.add_argument("--external")
""")
    options = report["arguments"][0]["options"]
    _inspection_expect(
        options["default"] == {"unknown": "compute_default()"},
        "default was guessed",
    )
    _inspection_expect(
        options["**kwargs"] == {"unknown": "options"},
        "unresolved kwargs were hidden",
    )
    _inspection_expect(bool(report["warnings"]), "unresolved parser was hidden")


def test_factory_return_and_keyword_helper() -> None:
    """Follow returned constructors and keyword helper arguments."""
    report = _inspection_discovery("""
from argparse import ArgumentParser
def configure(target):
    target.add_argument("--required", required=True)
def parser():
    return ArgumentParser()
def main():
    cli = parser()
    configure(target=cli)
""")
    _inspection_expect(
        report["arguments"]
        == [{"names": ["--required"], "options": {"required": True}}],
        "factory or keyword binding was lost",
    )


@pytest.mark.parametrize("entrypoint", ["parser().parse_args()", "parse(parser())"])
def test_inspection_keeps_internal_parsers_out_of_the_public_cli(
    entrypoint: str,
) -> None:
    """Select the parser consumed by main while retaining its configured helpers."""
    source = """
import argparse
def configure(cli):
    cli.add_argument("--shared")
def formatter_main():
    cli = argparse.ArgumentParser()
    configure(cli)
    cli.add_argument("--source")
    cli.add_subparsers().add_parser("internal")
    cli.parse_args()
def parser():
    cli = argparse.ArgumentParser()
    configure(cli)
    cli.add_argument("--public")
    cli.add_subparsers().add_parser("repo")
    return cli
def main():
    ENTRYPOINT
def parse(cli):
    cli.parse_args()
"""
    report = _inspection_discovery(source.replace("ENTRYPOINT", entrypoint))
    _inspection_expect(
        [row["names"] for row in report["arguments"]] == [["--shared"], ["--public"]],
        "internal parser arguments leaked into the public CLI",
    )
    _inspection_expect(list(report["commands"]) == ["repo"], "internal command leaked")
    _inspection_expect(
        not report["warnings"],
        "known internal parser produced warnings",
    )


def test_declared_test_identifiers() -> None:
    """Retain sorted test identifiers and class context."""
    module = ast.parse("""
from unittest import TestCase as Case
def test_default_output(): pass
def helper():
    def test_nested(): pass
class TestArguments:
    async def test_missing_input(self): pass
class Arguments(Case):
    def test_valid_input(self): pass
class Derived(Arguments):
    def test_other_input(self): pass
class Helper:
    def test_not_collected(self): pass
""")
    rows = inspection_subject.declared_tests(module)
    _inspection_expect(
        rows
        == [
            "Arguments::test_valid_input",
            "Derived::test_other_input",
            "Derived::test_valid_input",
            "TestArguments::test_missing_input",
            "test_default_output",
        ],
        "wrong test identifiers or collection scope",
    )


def _inspection_repository(root: Path) -> None:
    (root / "flake.nix").touch()
    package = root / "packages" / "example"
    package.mkdir(parents=True)
    (package / "default.nix").write_text('{ meta.description = "Example"; }')
    (package / "main.py").write_text("""
import argparse
raise RuntimeError("inspection must not execute this")
def parser():
    cli = argparse.ArgumentParser()
    cli.add_argument("--output", default="result")
    return cli
""")
    (package / "test_main.py").write_text("""
raise RuntimeError("inspection must not collect or run tests")
def test_default_output(): pass
""")
    host = root / "hosts" / "laptop"
    host.mkdir(parents=True)
    (host / "configuration.nix").touch()
    library = root / "packages" / "library"
    library.mkdir()
    (library / "default.nix").write_text("{}")


def test_inspection_is_read_only(tmp_path: Path) -> None:
    """Inspect source containing failing statements without executing or writing."""
    _inspection_repository(tmp_path)
    before = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    report = inspection_subject.inspect_repository(tmp_path)
    after = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    _inspection_expect(before == after, "inspection wrote to the repository")
    _inspection_expect(
        report["hosts"] == ["laptop"],
        "wrong hosts",
    )
    _inspection_expect(
        report["packages"]["library"]["cli"]["status"] == "not applicable",
        "non-Python package has CLI metadata",
    )
    _inspection_expect(
        report["packages"]["example"]["tests"] == ["test_default_output"],
        "wrong test identifiers",
    )
    _inspection_expect(
        report["packages"]["library"]["tests"] == [],
        "wrong empty tests",
    )


@pytest.mark.parametrize("location", ["outside", "root", "nested"])
def test_cli_inventory_resolves_repository_paths(
    tmp_path: Path,
    location: str,
) -> None:
    """Return the same JSON inventory through supported paths inside and outside Git."""
    _inspection_repository(tmp_path)
    expected = inspection_subject.inspect_repository(tmp_path)
    _inspection_expect(
        "test_default_output" in expected["packages"]["example"]["tests"],
        "inventory lost test identifier",
    )
    if location != "outside":
        subprocess.run(  # noqa: S603
            ["git", "init", "--quiet", str(tmp_path)],  # noqa: S607
            check=True,
            capture_output=True,
            timeout=10,
        )
    cwd = tmp_path / "packages/example" if location == "nested" else tmp_path
    paths = [["."], [str(tmp_path)]]
    if location != "outside":
        paths.append([])
    for arguments in paths:
        completed = subprocess.run(  # noqa: S603
            [os.environ["PACKAGE_E2E_EXECUTABLE"], "repo", "inspect", *arguments],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        _inspection_expect(
            json.loads(completed.stdout) == expected,
            "path resolution changed the CLI inventory",
        )


def test_invalid_repository_and_source(tmp_path: Path) -> None:
    """Report invalid repositories and malformed source with useful errors."""
    with pytest.raises(inspection_subject.CommandError, match=r"flake\.nix"):
        inspection_subject.inspect_repository(tmp_path)
    _inspection_repository(tmp_path)
    (tmp_path / "packages" / "example" / "main.py").write_text("def invalid(")
    with pytest.raises(
        inspection_subject.CommandError,
        match="could not parse Python source",
    ):
        inspection_subject.inspect_repository(tmp_path)


def test_empty_repository(tmp_path: Path) -> None:
    """Report missing hosts as a list and missing packages as an object."""
    (tmp_path / "flake.nix").touch()
    _inspection_expect(
        inspection_subject.inspect_repository(tmp_path)
        == {"hosts": [], "packages": {}},
        "empty collections were omitted",
    )


def _inspection_remote_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path, str]:
    """Serve a hosted URL from a local repository without network access."""
    home, source = tmp_path / "home", tmp_path / "source"
    home.mkdir()
    source.mkdir()
    _inspection_repository(source)
    inspection_subject.git(source, ["init", "--quiet"])
    inspection_subject.git(source, ["config", "user.name", "Test"])
    inspection_subject.git(source, ["config", "user.email", "test@example.test"])
    inspection_subject.git(source, ["add", "."])
    inspection_subject.git(source, ["commit", "--quiet", "-m", "initial"])
    remote = "https://example.test/team/repository.git"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{source.as_uri()}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", remote)
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")
    return home, source, remote


def _inspection_inspect_target(target: str) -> dict[str, Any]:
    """Exercise the installed CLI with an unchanged path or Git URL."""
    completed = subprocess.run(  # noqa: S603
        [os.environ["PACKAGE_E2E_EXECUTABLE"], "repo", "inspect", target],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return cast("dict[str, Any]", json.loads(completed.stdout))


def _inspection_tree_snapshot(root: Path) -> dict[Path, tuple[int, bytes]]:
    """Observe contents and modes, including Git metadata and symbolic links."""
    return {
        path.relative_to(root): (
            path.lstat().st_mode,
            os.fsencode(path.readlink())
            if path.is_symlink()
            else path.read_bytes()
            if stat.S_ISREG(path.lstat().st_mode)
            else b"",
        )
        for path in root.rglob("*")
    }


def _inspection_advance_remote(source: Path) -> None:
    """Publish a fixture change that existing checkouts and cached refs lack."""
    (source / "hosts/new").mkdir(parents=True)
    (source / "hosts/new/configuration.nix").touch()
    inspection_subject.git(source, ["add", "."])
    inspection_subject.git(source, ["commit", "--quiet", "-m", "add host"])


@pytest.mark.parametrize("initialized", [False, True])
def test_remote_inspection_preserves_home_and_does_not_block_submodule_add(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    initialized: bool,
) -> None:
    """Inspect fresh remote contents without creating persistent home state."""
    home, source, remote = _inspection_remote_fixture(tmp_path, monkeypatch)
    if initialized:
        inspection_subject.initialize_home()
    before = _inspection_tree_snapshot(home)
    _inspection_expect(
        _inspection_inspect_target(remote)
        == inspection_subject.inspect_repository(source),
        "wrong remote inventory",
    )
    _inspection_advance_remote(source)
    _inspection_expect(
        _inspection_inspect_target(remote)
        == inspection_subject.inspect_repository(source),
        "remote inspection was stale",
    )
    _inspection_expect(
        _inspection_tree_snapshot(home) == before,
        "remote inspection changed HOME",
    )
    if initialized:
        inspection_subject.initialize_submodule(remote)
        checkout = home / "example.test/team/repository"
        _inspection_expect(
            inspection_subject.git(checkout, ["rev-parse", "HEAD"]).stdout
            == inspection_subject.git(source, ["rev-parse", "HEAD"]).stdout,
            "inspection interfered with native submodule add",
        )


@pytest.mark.parametrize("malformed", [False, True])
def test_remote_inspection_cleans_temporary_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    malformed: bool,
) -> None:
    """Remove temporary clones after successful inspection and parser errors."""
    home, source, remote = _inspection_remote_fixture(tmp_path, monkeypatch)
    if malformed:
        (source / "packages/example/main.py").write_text("def invalid(")
        inspection_subject.git(source, ["add", "."])
        inspection_subject.git(source, ["commit", "--quiet", "-m", "malformed source"])
        with (
            pytest.raises(
                inspection_subject.CommandError,
                match="could not parse Python source",
            ),
            inspection_subject.inspection_remote_repository(remote) as checkout,
        ):
            inspection_subject.inspect_repository(checkout)
    else:
        with inspection_subject.inspection_remote_repository(remote) as checkout:
            _inspection_expect(
                inspection_subject.inspect_repository(checkout)
                == inspection_subject.inspect_repository(source),
                "wrong temporary checkout contents",
            )
    _inspection_expect(not checkout.exists(), "temporary checkout survived inspection")
    _inspection_expect(not list(home.iterdir()), "temporary inspection changed HOME")


def test_local_path_and_remote_url_inspect_the_requested_contents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep local dirty content visible only when explicitly inspecting its path."""
    home, source, remote = _inspection_remote_fixture(tmp_path, monkeypatch)
    inspection_subject.initialize_home()
    inspection_subject.initialize_submodule(remote)
    checkout = home / "example.test/team/repository"
    (checkout / "hosts/local").mkdir(parents=True)
    (checkout / "hosts/local/configuration.nix").touch()
    inspection_subject.git(
        checkout,
        ["remote", "set-url", "origin", "https://fork.test/team/repository"],
    )
    before = _inspection_tree_snapshot(home)
    _inspection_expect(
        _inspection_inspect_target(str(checkout))
        == inspection_subject.inspect_repository(checkout),
        "explicit local inspection ignored local content",
    )
    _inspection_expect(
        _inspection_inspect_target(remote)
        == inspection_subject.inspect_repository(source),
        "remote URL inspected a local fork or untracked files",
    )
    _inspection_expect(
        _inspection_tree_snapshot(home) == before,
        "inspection changed local Git state",
    )


@pytest.mark.parametrize("state", ["checkout", "deinitialized", "cache", "placeholder"])
def test_remote_inspection_ignores_existing_home_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    """Leave existing worktrees, retained Git data, and unrelated paths untouched."""
    home, source, remote = _inspection_remote_fixture(tmp_path, monkeypatch)
    inspection_subject.initialize_home()
    relative = Path("example.test/team/repository")
    checkout = home / relative
    if state == "cache":
        modules = Path(
            inspection_subject.git(
                home,
                ["rev-parse", "--path-format=absolute", "--git-path", "modules"],
            ).stdout.strip(),
        )
        storage = modules / relative
        storage.parent.mkdir(parents=True)
        inspection_subject.git(
            home,
            ["clone", "--bare", "--", str(source), str(storage)],
        )
    elif state == "placeholder":
        checkout.mkdir(parents=True)
        (checkout / "personal-data").write_bytes(b"preserve data\\x00\\xff")
    else:
        inspection_subject.initialize_submodule(remote)
        if state == "deinitialized":
            inspection_subject.git(
                home,
                ["submodule", "deinit", "--force", "--", str(relative)],
            )
        else:
            inspection_subject.git(checkout, ["remote", "remove", "origin"])
            (checkout / "LICENSE").write_text("local changes")
    _inspection_advance_remote(source)
    before = _inspection_tree_snapshot(home)
    _inspection_expect(
        _inspection_inspect_target(remote)
        == inspection_subject.inspect_repository(source),
        "local state affected remote inspection",
    )
    _inspection_expect(
        _inspection_tree_snapshot(home) == before,
        "inspection changed existing Git storage",
    )


def test_remote_inspection_accepts_native_file_urls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Let Git handle transports without imposing hosted repository conventions."""
    home, source, _remote = _inspection_remote_fixture(tmp_path, monkeypatch)
    _inspection_expect(
        _inspection_inspect_target(source.as_uri())
        == inspection_subject.inspect_repository(source),
        "native file URL was rejected",
    )
    _inspection_expect(not list(home.iterdir()), "file URL inspection changed HOME")


@pytest.mark.parametrize("remote_kind", ["missing", "unsupported", "invalid"])
def test_remote_inspection_preserves_native_clone_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    remote_kind: str,
) -> None:
    """Compare failed remote inspection with Git's exit status and diagnostics."""
    home, _source, _remote = _inspection_remote_fixture(tmp_path, monkeypatch)
    remote = {
        "missing": (tmp_path / "missing.git").as_uri(),
        "unsupported": "unsupported://example.test/team/repository",
        "invalid": "https://",
    }[remote_kind]
    scratch = tmp_path / "temporary"
    scratch.mkdir()
    monkeypatch.setenv("TMPDIR", str(scratch))
    native_checkout = tmp_path / "native-checkout"
    expected = subprocess.run(  # noqa: S603
        ["git", "clone", "--", remote, str(native_checkout)],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    actual = subprocess.run(  # noqa: S603
        [os.environ["PACKAGE_E2E_EXECUTABLE"], "repo", "inspect", remote],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    actual_error = re.sub(
        re.escape(str(scratch)) + r"/afairesi-inspect-[^/]+/repository",
        "<CHECKOUT>",
        actual.stderr,
    )
    _inspection_expect(
        (actual.returncode, actual.stdout, actual_error)
        == (
            expected.returncode,
            expected.stdout,
            expected.stderr.replace(str(native_checkout), "<CHECKOUT>"),
        ),
        f"native clone error changed: {expected.stderr!r}, {actual.stderr!r}",
    )
    _inspection_expect(not list(scratch.iterdir()), "failed clone left temporary files")
    _inspection_expect(not list(home.iterdir()), "failed clone changed HOME")


def test_inspection_discovers_the_merged_repository_commands() -> None:
    """Inventory the public nested campaign CLI while excluding formatter internals."""
    subject = import_module("packages.afairesi.main")
    report = subject.ArgumentDiscovery(
        ast.parse(Path(__file__).with_name("main.py").read_bytes()),
    ).report()
    _expect(set(report["commands"]) == {"repo", "home"}, report)
    commands = report["commands"]["repo"]["commands"]
    _expect({"inspect", "test"} <= commands.keys(), commands)
    campaigns = commands["test"]["commands"]
    _expect(
        set(campaigns) == {"coverage", "property", "mutation", "performance"},
        campaigns,
    )
    _expect(not report["warnings"], report["warnings"])
    _expect(not report["arguments"], report["arguments"])

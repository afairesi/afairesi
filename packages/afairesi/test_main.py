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
        (("packages/example", "python", "description"), "unrecognized arguments"),
        (
            ("packages/example", "python", "--description", "text"),
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
def test_add_preview_preserves_state_and_rejects_existing_resources(kind: str) -> None:
    """Preview additions from a nested cwd without writing files or staging data."""
    with _fresh_repository() as root:
        nested = root / "tmp/nested"
        nested.mkdir(parents=True)
        relative = "hosts/example" if kind == "host" else "packages/example"
        arguments = () if kind == "host" else (kind,)
        before = _snapshot(root)
        preview = _run(nested, "add", relative, *arguments, "-n")
        _expect(f"would write '{relative}/" in preview.stdout, preview.stdout)
        if kind == "host":
            _expect(
                "checks/exampleVmWithDisko/default.nix" in preview.stdout,
                preview.stdout,
            )
        _expect(_snapshot(root) == before, "add preview changed files or Git state")
        _run(nested, "add", relative, *arguments)
        _expect(
            (root / relative).is_dir(),
            "add used the working directory as its root",
        )
        before = _snapshot(root)
        _run(nested, "add", relative, *arguments, "--dry-run", code=1)
        _expect(_snapshot(root) == before, "conflicting preview changed state")


def test_public_repo_repair_matches_formatter_repair() -> None:
    """Repair from nested directories and preview without changing Git state."""
    with _fresh_repository() as root:
        nested = root / "tmp/nested"
        nested.mkdir(parents=True)
        _run(root, "add", "packages/example", "python")
        (root / "packages/example/test_main.py").write_text(
            "def test_example(): pass\n",
        )
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
        for retired in (
            "create",
            "move",
            "remove",
            "init",
            "init-home",
            "init-repo",
            "clone",
            "repair",
            "repair-home",
        ):
            _run(root, retired, code=2)
        _run(root, "add", code=2)
        help_output = _run(root, "--help").stdout
        command_names = ["add", "fix", "mv", "rm"]
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
        _run(root, "add", relative, *(() if kind == "host" else (kind,)))
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
            rejected = _run(root, "rm", relative, *arguments, code=1)
            _expect("--force" in rejected.stderr, rejected)
            _expect(_snapshot(root) == before, "rejected removal changed state")
        _preview(root, "rm", relative, "--force")
        _run(root, "rm", relative, "--force")
        _expect(not resource.exists() and not path.exists(), "forced removal left data")


def test_resource_commands_resolve_paths_from_the_repository_root() -> None:
    """Resolve resource paths from the repository root from nested directories."""
    with _fresh_repository() as root:
        nested = root / "tmp/nested"
        nested.mkdir(parents=True)
        _run(nested, "add", "packages/example", "nix")
        _expect((root / "packages/example/default.nix").is_file(), "wrong create root")
        _run(nested, "mv", "packages/example", "packages/renamed")
        _fixture_git(root, "commit", "-qm", "Renamed resource")
        _preview(nested, "rm", "packages/renamed")
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
        _run(root, "add", resource, *(() if kind == "host" else (kind,)), code=1)
        _expect(_snapshot(root) == before, "failed creation left structural changes")


@pytest.mark.parametrize("case", ["symlink", "file"])
def test_removal_validates_all_targets_before_preview_or_deletion(
    tmp_path: Path,
    case: str,
) -> None:
    """Reject unsupported related checks before deleting the package."""
    with _fresh_repository() as root:
        _run(root, "add", "packages/example", "nix")
        check = root / "checks/example"
        check.parent.mkdir()
        if case == "symlink":
            check.symlink_to(tmp_path, target_is_directory=True)
        else:
            check.write_text("preserve check contents")
        before = _snapshot(root)
        for arguments in (("--dry-run",), ()):
            _run(root, "rm", "packages/example", "--force", *arguments, code=1)
            _expect(_snapshot(root) == before, "rejected removal deleted local files")


@pytest.mark.parametrize("style", ["literal", "split", "annotated", "concatenated"])
def test_rename_updates_runtime_paths_without_staging_unrelated_changes(
    style: str,
) -> None:
    """Move runtime data and update owned and input paths while preserving Git state."""
    with _fresh_repository() as root:
        _git(root, "remote", "add", "origin", "https://forge.example/team/repo.git")
        for name in ("old", "consumer"):
            _run(root, "add", f"packages/{name}", "python")
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
        _preview(root, "mv", "packages/old", "packages/new")
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
        _run(root, "add", "packages/example", "python")
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
        _run(root, "add", "packages/a", "python")
        _run(root, "add", "packages/z", "python")
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
        _run(root, "add", "packages/a", "python")
        _run(root, "add", "packages/z", "python")
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
        _run(root, "add", "packages/example", "python")
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
        _repair_flake(root)
        _preview(root, "rm", destination, "--force")
        _run(root, "rm", destination, "--force")
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
        raise AssertionError(result.stdout + result.stderr)
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
    _home_test_git(checkout, "config", "user.name", "Test")
    _home_test_git(checkout, "config", "user.email", "test@example.org")
    _home_test_git(checkout, "config", "commit.gpgSign", "false")
    _home_test_git(checkout, "remote", "add", "origin", "git@forge.example:owner/demo")
    source = checkout / "README"
    source.write_text("first", encoding="utf-8")
    _home_test_git(checkout, "add", "README")
    _home_test_git(checkout, "commit", "--quiet", "-m", "first")
    first = _home_test_git(checkout, "rev-parse", "HEAD").strip()
    _home_test_git(checkout, "update-ref", "refs/remotes/origin/main", first)
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
    (origin / "README").write_text("existing repository")
    _home_test_git(origin, "add", "README")
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
    if (checkout / "README").read_text() != "existing repository":
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
    _home_test_run(tmp_path, "clone", remote)
    if (home / ".gitignore").read_text(encoding="utf-8") != ignore:
        message = "repeated clone changed the home whitelist"
        raise AssertionError(message)


def test_merged_create_flake_commits_registers_and_publishes_an_empty_remote(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Register an empty remote through the home whitelist."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    origin = tmp_path / "remote.git"
    origin.mkdir()
    _home_test_git(origin, "init", "--bare", "--quiet")
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
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    _home_test_run(tmp_path, "init")
    _home_test_git(home, "commit", "--quiet", "-m", "Home policy")
    home_head = _home_test_git(home, "rev-parse", "HEAD")
    _home_test_run(
        tmp_path,
        "-c",
        "from packages.afairesi.main import main; main()",
        "home",
        "create",
        remote,
        executable=sys.executable,
    )
    relative = "example.test/team/new"
    checkout = home / relative
    _home_test_expect(
        _home_test_git(checkout, "branch", "--show-current").strip() == "main",
        "create did not select main",
    )
    _home_test_expect(
        _home_test_git(origin, "rev-parse", "refs/heads/main")
        == _home_test_git(checkout, "rev-parse", "HEAD"),
        "create did not publish its initial commit",
    )
    _home_test_expect(
        _home_test_git(home, "rev-parse", "HEAD") == home_head,
        "create committed the home registration",
    )
    _home_test_expect(
        set(_home_test_git(checkout, "ls-files").splitlines())
        == {".gitignore", "README", "flake.lock", "flake.nix"},
        "create did not commit the canonical initial files",
    )
    _home_test_expect(
        (checkout / "README").read_text() == "# new\n"
        and "inputs.afairesi.url" in (checkout / "flake.nix").read_text(),
        "create did not scaffold the requested repository",
    )
    ignore = (home / ".gitignore").read_text(encoding="utf-8")
    if f"!/{relative}" not in ignore.splitlines():
        raise AssertionError(ignore)
    if _home_test_git(home, "show", ":.gitignore") != ignore:
        message = "create did not stage the home whitelist"
        raise AssertionError(message)
    if not _home_test_git(home, "ls-files", "--stage", "--", relative).startswith(
        "160000 ",
    ):
        message = "create did not stage its submodule"
        raise AssertionError(message)


@pytest.mark.parametrize("features", ["", "nix-command", "flakes"])
def test_merged_create_flake_checks_nix_features_before_creating_state(
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
    _home_test_run(tmp_path, "init")
    before = _home_test_snapshot(home)
    result = _home_test_run(
        tmp_path,
        "create",
        "https://example.test/team/new.git",
        code=1,
    )
    _home_test_expect(
        "experimental-features = nix-command flakes" in result.stderr,
        result,
    )
    _home_test_expect(
        _home_test_snapshot(home) == before,
        "Nix prerequisite failure changed home",
    )
    _home_test_expect(
        not (home / "example.test").exists(),
        "prerequisite check created paths",
    )


def test_merged_create_flake_reports_missing_nix_before_creating_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explain missing Nix without creating the checkout or changing the index."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _home_test_run(tmp_path, "init")
    before = _home_test_snapshot(home)
    tools = tmp_path / "bin"
    tools.mkdir()
    git_executable = shutil.which("git")
    if git_executable is None:
        pytest.fail("Missing Git executable")
    (tools / "git").symlink_to(git_executable)
    monkeypatch.setenv("PATH", str(tools))
    result = _home_test_run(
        tmp_path,
        "-c",
        "from packages.afairesi.main import main; main()",
        "home",
        "create",
        "https://example.test/team/new.git",
        code=1,
        executable=sys.executable,
    )
    _home_test_expect("Nix executable not found" in result.stderr, result)
    _home_test_expect(_home_test_snapshot(home) == before, "missing Nix changed home")


@pytest.mark.parametrize("command", ["clone", "create"])
@pytest.mark.parametrize("initialized_git", [False, True])
def test_merged_home_prerequisites_explain_initialization_without_changes(
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
        _home_test_git(home, "init", "--quiet")
    before = _home_test_snapshot(home) if initialized_git else None
    result = _home_test_run(
        tmp_path,
        command,
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
    source = checkout / "README"
    first = _home_test_git(checkout, "rev-parse", "HEAD").strip()
    source.write_text("second", encoding="utf-8")
    _home_test_run(root, "fix")
    if source.read_text() != "second":
        message = "convergence changed dirty submodule content"
        raise AssertionError(message)
    _home_test_git(checkout, "add", "README")
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
    source = checkout / "README"
    first = _home_test_git(checkout, "rev-parse", "HEAD").strip()
    source.write_text("move snapshot", encoding="utf-8")
    _home_test_git(checkout, "add", "README")
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
    _home_test_git(checkout, "add", "README")
    source.write_text("unstaged", encoding="utf-8")
    (checkout / "untracked").write_text("keep", encoding="utf-8")
    (root / "unrelated").write_text("keep home file", encoding="utf-8")
    before = _home_test_git(checkout, "status", "--porcelain")
    indexed = _home_test_git(checkout, "show", ":README")
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
    rejected = _home_test_run(root, "fix", code=1)
    if "stage .gitmodules" not in rejected.stderr or not checkout.exists():
        raise AssertionError(rejected.stderr)
    _home_test_git(root, "add", ".gitmodules")
    _home_test_run(root, "fix", "--dry-run")
    if not checkout.exists() or target.exists():
        message = "dry-run moved the checkout"
        raise AssertionError(message)
    _home_test_run(root, "fix")
    if (
        checkout.exists()
        or _home_test_git(target, "status", "--porcelain") != before
        or _home_test_git(target, "show", ":README") != indexed
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
        _home_test_git(root, "ls-files", "--stage", "-z"),
        _home_test_git(root, "for-each-ref", "--format=%(refname) %(objectname)"),
        _home_test_git(root, "config", "--local", "--list"),
    )


def _home_test_preview(root: Path, *arguments: str, code: int = 0) -> None:
    """Preserve the whole repository during successful and rejected previews."""
    before = _home_test_snapshot(root)
    _home_test_run(root, *arguments, "--dry-run", code=code)
    _home_test_expect(_home_test_snapshot(root) == before, arguments)


def _home_test_history_snapshot(root: Path) -> dict[Path, bytes]:
    """Observe stored Git files without requiring a surviving worktree."""
    return {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
        and path.relative_to(root).parts[0] in {"objects", "refs", "logs", "HEAD"}
    }


def test_merged_init_home_stages_policy_without_cleaning_or_committing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Initialize HOME from another directory and preserve existing home data."""
    home = tmp_path / "home"
    home.mkdir()
    local = home / "personal-data"
    local.write_bytes(b"\x00keep home data\xff")
    monkeypatch.setenv("HOME", str(home))
    _home_test_run(tmp_path, "init")
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
    _home_test_run(tmp_path, "init")
    _home_test_expect(
        _home_test_snapshot(home) == before,
        "repeated home initialization changed state",
    )
    _home_test_expect(
        not (tmp_path / ".git").exists(),
        "home initialization initialized the working directory",
    )


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


@pytest.mark.parametrize("case", ["whitelist", "registration", "symlink"])
def test_merged_flake_initialization_validates_home_before_creating_or_publishing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    """Reject invalid home state before creating a checkout or publishing a commit."""
    home = _home_test_home_repository(tmp_path / "home")
    origin = tmp_path / "remote.git"
    origin.mkdir()
    _home_test_git(origin, "init", "--bare", "--quiet")
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
    before = _home_test_snapshot(home)
    _home_test_run(tmp_path, "create", remote, code=1)
    _home_test_expect(
        _home_test_snapshot(home) == before,
        "failed initialization changed home state",
    )
    _home_test_expect(
        not _home_test_git(origin, "for-each-ref"),
        "failed initialization published a commit",
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
            (checkout / "README").write_text("unrelated local data")
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
            (checkout / "README").read_text() == "unrelated local data",
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


@pytest.mark.parametrize("destination", ["demo", "renamed"])
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
            f"submodule.{expected}.url",
        ).strip()
        == local_url,
        "repair overwrote the local submodule URL override",
    )
    subject = import_module("packages.afairesi.main")
    _home_test_expect(
        subject.home_submodules(root)[0]["url"] == declared_url,
        "repair changed declared URL",
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
    (nested / "README").write_text("nested repository")
    _home_test_git(nested, "add", "README")
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
    ["unknown", "parent", "dirty", "dirty-preview", "untracked", "ignored"],
)
def test_merged_home_submodule_removal_rejects_invalid_or_dirty_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    """Reject ambiguous paths and local changes without changing home state."""
    root = _home_test_home_repository(tmp_path)
    monkeypatch.setenv("HOME", str(root))
    relative = "forge.example/owner/demo"
    _home_test_git(root, "submodule", "absorbgitdirs")
    _home_test_fixture_git(root, "commit", "-qm", "Home snapshot")
    target = {"unknown": "missing", "parent": "forge.example/owner"}.get(
        case,
        relative,
    )
    if case.startswith("dirty"):
        (root / relative / "README").write_text("local changes")
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
    arguments = ("--dry-run",) if case == "dirty-preview" else ()
    _home_test_run(root, "rm", target, *arguments, code=1)
    _home_test_expect(
        _home_test_snapshot(root) == before,
        "rejected removal changed home state",
    )
    if case.startswith("dirty") or case in {"untracked", "ignored"}:
        _home_test_preview(root, "rm", target, "--force")
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
    history = root / ".git/modules" / relative
    history_before = _home_test_history_snapshot(history)
    ignore = root / ".gitignore"
    ignore.write_text(
        ignore.read_text()
        + f"!/{relative}/\n!/{relative}/README\n"
        + "!/forge.example/owner/demo-other\n!/.ssh/\n",
    )
    _home_test_git(root, "add", ".gitignore")
    _home_test_fixture_git(root, "commit", "-qm", "Home snapshot")
    before = _home_test_snapshot(root)
    preview = _home_test_run(cwd, "rm", relative, "--dry-run")
    _home_test_expect(
        relative in preview.stdout and ".gitignore" in preview.stdout,
        preview,
    )
    _home_test_expect(
        _home_test_snapshot(root) == before,
        "removal preview changed home state",
    )
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
        f"!/{relative}" not in lines and f"!/{relative}/README" not in lines,
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
        _home_test_git(
            root,
            "config",
            "--local",
            "--get-regexp",
            r"^submodule\.",
        ).strip()
        == "submodule.unrelated.active false",
        "removal left local registration or changed unrelated settings",
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
    expected_name = relative if registered else name
    expected_settings = {
        f"submodule.{expected_name}.active=true",
        f"submodule.{expected_name}.update=none",
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
                f"submodule.{relative}.update",
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
    commands = ("clone", "create", "fix", "init", "rm")
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
        ("init-repo", "https://example.test/team/new.git"),
        ("move",),
        ("repair",),
        ("remove", "github.com/example/repository"),
        ("clone",),
        ("create",),
        ("init", "unexpected"),
        ("init-flake", "git@forge.example:owner/example"),
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


def test_workspace_cli_requires_explicit_scope_and_shows_scoped_help(
    tmp_path: Path,
) -> None:
    """Expose both scopes and reject unscoped or cross-scope commands."""
    executable = os.environ["PACKAGE_E2E_EXECUTABLE"]
    help_output = _run(tmp_path, "--help", executable=executable).stdout
    _expect("manages repository structure" in help_output, help_output)
    _expect("    repo " in help_output and "    home " in help_output, help_output)
    _expect(_run(tmp_path, executable=executable).stdout == help_output, help_output)
    for scope in ("repo", "home"):
        scoped_help = _run(tmp_path, scope, "--help", executable=executable).stdout
        _expect(
            _run(tmp_path, scope, executable=executable).stdout == scoped_help,
            scoped_help,
        )
    for arguments in (
        ("add", "packages/example", "python"),
        ("init",),
        ("repo", "init"),
        ("home", "add", "packages/example", "python"),
        ("tests",),
    ):
        _run(tmp_path, *arguments, executable=executable, code=2)
    _expect(not any(tmp_path.iterdir()), "invalid scope created state")

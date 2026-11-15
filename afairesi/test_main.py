# Copyright (c) 2026- Paschalis Bizopoulos
"""Check Afairesi's public contracts with explicit regressions and generated cases."""

from __future__ import annotations

import ast
import json
import os
import pstats
import re
import selectors
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
from cosmic_ray.work_db import WorkDB, use_db
from cosmic_ray.work_item import TestOutcome as MutationTestOutcome
from cosmic_ray.work_item import WorkerOutcome, WorkItem, WorkResult

import nix_syntax

if TYPE_CHECKING:
    from collections.abc import Iterator


def _nix_source() -> Path:
    """Locate the Nix library in the checkout or the isolated check environment."""
    return Path(
        os.environ.get(
            "AFAIRESI_NIX_SOURCE",
            str(Path(__file__).resolve().parents[1] / "afairesi_nix"),
        ),
    )


def _run(
    root: Path,
    *arguments: str,
    code: int = 0,
    executable: str | None = None,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(  # noqa: S603 - Test harness selects the executable and fixture arguments.
        [
            executable or os.environ["PACKAGE_E2E_EXECUTABLE"],
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


def _write_fixture_source(
    path: Path,
    source: str,
    *,
    encoding: str | None = None,
) -> int:
    """Write fixture source and explicitly select newly created conventional files."""
    result = path.write_text(source, encoding=encoding)
    if path.name in {
        "main.py",
        "test_main.py",
        "main.nix",
        "test_main.nix",
        "main.lean",
        "test_main.lean",
        "default.nix",
        "configuration.nix",
        "hardware-configuration.nix",
    }:
        root = path.parent.parent
        if (root / ".git").exists():
            relative = path.relative_to(root).as_posix()
            if relative not in _git(root, "ls-files").splitlines():
                _git(root, "add", "--force", "--", relative)
    return result


@pytest.fixture(autouse=True)
def _isolated_report_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep retained campaign reports outside each test's project checkout."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


def _report_directory() -> Path:
    """Locate retained reports using the same public state-directory setting."""
    return Path(os.environ["XDG_STATE_HOME"]) / "afairesi"


def _check_flake_source(
    root: Path, *, code: int = 0
) -> subprocess.CompletedProcess[str]:
    """Exercise the source-only validator used by the structure check."""
    return _run(
        root,
        "-c",
        "from afairesi.main import check_source_main; check_source_main()",
        str(root),
        code=code,
        executable=sys.executable,
    )


def _git(root: Path, *arguments: str) -> str:
    if (
        arguments
        and arguments[0] == "add"
        and "--force" not in arguments
        and "-f" not in arguments
    ):
        arguments = ("add", "--force", *arguments[1:])
    return subprocess.run(  # noqa: S603 - Fixed Git executable with test-controlled arguments.
        ["git", *arguments],  # noqa: S607
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    ).stdout


@pytest.mark.parametrize("code", [0, 7])
def test_commands_stream_output_before_exit(code: int) -> None:
    """Forward both streams while a child is still waiting for terminal input."""
    child = (
        "import sys; print('native stdout', flush=True); "
        "print('native stderr', file=sys.stderr, flush=True); "
        f"sys.stdin.readline(); sys.exit({code})"
    )
    command = (
        "from afairesi.main import run; "
        f"run({[sys.executable, '-c', child]!r}, capture_output=False)"
    )
    with (
        subprocess.Popen(  # noqa: S603 - Use this interpreter with test-authored code and arguments.
            [sys.executable, "-c", command],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ) as process,
        selectors.DefaultSelector() as selector,
    ):
        try:
            if process.stdout is None or process.stderr is None:
                msg = "native output test requires both output pipes"
                raise AssertionError(msg)
            selector.register(process.stdout, selectors.EVENT_READ, "native stdout\n")
            selector.register(process.stderr, selectors.EVENT_READ, "native stderr\n")
            while selector.get_map():
                ready = selector.select(timeout=10)
                _expect(bool(ready), "native output was buffered until process exit")
                for key, _ in ready:
                    _expect(os.read(key.fd, 4096).decode() == key.data, key.data)
                    selector.unregister(key.fileobj)
            stdout, stderr = process.communicate("\n", timeout=10)
            _expect(process.returncode == code, process.returncode)
            _expect(not stdout and not stderr, "native output was replayed")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


def _make_source_package(root: Path, name: str, source: str) -> Path:
    """Create a canonical package fixture without executing its source."""
    package = root / name
    package.mkdir(parents=True)
    (root / "flake.nix").touch()
    (package / "default.nix").touch()
    (package / "main.py").touch()
    _write_fixture_source(package / "test_main.py", source)
    if (root / ".git").exists():
        _git(root, "add", "--force", "--", str(package.relative_to(root)))
    return package


def _fixture_git(root: Path, *arguments: str) -> str:
    """Run fixture Git commands without signing or background maintenance."""
    return _git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "-c",
        "maintenance.auto=false",
        *arguments,
    )


def _fixture_add(
    root: Path,
    name: str,
    kind: str,
    description: str = "Fixture package.",
) -> None:
    """Write the minimal tracked source needed by a test."""
    sources = {
        "host": {"configuration.nix": "{ ... }: {}\n"},
        "python": {"main.py": "def main():\n    pass\n"},
        "html": {"index.html": "<!doctype html><html><body></body></html>\n"},
        "latex": {
            "ms.tex": (
                "\\documentclass{article}\n\\begin{document}\nHello.\n\\end{document}\n"
            )
        },
        "lean4": {"main.lean": "def main : IO Unit := pure ()\n"},
        "nix": {},
    }[kind]
    if kind != "host":
        builder = {
            "python": "inputs.self.lib.mkPythonPackage",
            "html": "inputs.self.lib.mkHtmlPackage",
            "latex": "inputs.self.lib.mkLatexPackage",
            "lean4": "inputs.self.lib.mkLean4Package",
            "nix": "pkgs.writeTextFile",
        }[kind]
        settings = (
            '  name = baseNameOf ./.;\n  text = "";\n'
            if kind == "nix"
            else "  inherit pkgs;\n  src = ./.;\n"
        )
        if kind in {"python", "lean4"}:
            settings += "  executable = true;\n"
        sources["default.nix"] = (
            "{ inputs, pkgs, ... }:\n"
            + builder
            + " {\n"
            + settings
            + f"  meta.description = {nix_syntax.quote_string(description)};\n"
            + "}\n"
        )
    package = root / name
    package.mkdir()
    for filename, source in sources.items():
        (package / filename).write_text(source)
    _git(root, "add", "--force", "--", name)


def _check_excluded_trees(repository: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Discover tracked files without scanning arbitrary local trees."""
    _fixture_add(repository, "example", "python")
    local = repository / "experiments"
    local.mkdir()
    (local / "main.py").write_text("invalid untracked source")
    subject = import_module("afairesi.main")
    original = os.scandir

    def guarded(path: str | os.PathLike[str]) -> Iterator[os.DirEntry[str]]:
        if Path(path).is_relative_to(local):
            message = "traversed local files"
            raise AssertionError(message)
        return original(path)

    monkeypatch.setattr(os, "scandir", guarded)
    packages, issues = subject.inspect_structure(repository)
    _expect(
        [package.name for package in packages] == ["example"] and not issues,
        issues,
    )


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
        '("pname=${attrs.pname}\\nrunHook() {\\ncase \\"$1\\" in\\npreInstall)\\n" '
        '+ (attrs.preInstall or ":") + "\\n;;\\npostInstall)\\n" '
        '+ (attrs.postInstall or ":") + "\\n;;\\nesac\\n}\\ncp -R ${attrs.src}/. .\\n" '
        "+ attrs.installPhase)) // attrs; }; "
        'pkgs = { python3 = python; git = "git"; curl = "curl"; inner = "wrong-inner"; '
        'http-server = "server"; texliveFull = "tex"; '
        "stdenv.hostPlatform.isLinux = false; "
        "stdenv.mkDerivation = attrs: attrs; writeTextFile = attrs: attrs; "
        'lib = { escapeShellArgs = xs: builtins.concatStringsSep " " '
        "(map builtins.toJSON xs); "
        "optionals = condition: values: if condition then values else []; "
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
        + f"package = import {root / name / 'default.nix'} "
        + "{ inherit pkgs; "
        + "inputs.self.lib = (import "
        + f"{_nix_source() / 'main.nix'} {{}}).lib; "
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
        f"nixLibrary = import {_nix_source() / 'main.nix'} {{}}; "
        "lib = nixLibrary.lib; in nixLibrary.pythonCheck { "
        "pkgs = checkPkgs; inherit packageDrv; "
        "testEnvironment = lib.mkTestEnvironment { "
        "pkgs = checkPkgs; inherit packageDrv; }; "
        f"packageName = {json.dumps(name)}; }}"
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


@pytest.mark.parametrize("implementation", [False, True])
@pytest.mark.parametrize("tests", [False, True])
def test_inspect_finds_nix_optional_sources_after_git_move(
    *,
    implementation: bool,
    tests: bool,
) -> None:
    """Keep all four optional-file combinations without evaluating Nix source."""
    with _fresh_repository() as root:
        _fixture_add(root, "example", "nix")
        files = {"default.nix": (root / "example/default.nix").read_text()}
        for name, enabled in (("main.nix", implementation), ("test_main.nix", tests)):
            if enabled:
                files[name] = 'throw "source validation must not evaluate this"\n'
                _write_fixture_source(root / "example" / name, files[name])
        _git(root, "mv", "--", "example", "renamed")
        _expect(
            {path.name: path.read_text() for path in (root / "renamed").iterdir()}
            == files,
            "inspection or rename changed the optional Nix sources",
        )
        _expect(
            set(_git(root, "ls-files", "--", "renamed").splitlines())
            == {f"renamed/{name}" for name in files},
            "optional Nix sources were not tracked",
        )
        report = json.loads(_run(root, "inspect").stdout)["renamed"]
        _expect(report["type"] == "nix", report)
        _expect(report.get("tests", []) == (["test_main.nix"] if tests else []), report)


@pytest.mark.parametrize("filename", ["main.nix", "test_main.nix"])
def test_nix_optional_sources_are_syntax_checked(filename: str) -> None:
    """Reject malformed optional Nix files during source validation."""
    with _fresh_repository() as root:
        _fixture_add(root, "example", "nix")
        _write_fixture_source(root / "example" / filename, "{ invalid = ; }\n")
        result = _check_flake_source(root, code=1)
        _expect(f"example/{filename}:" in result.stderr, result)
        _expect("parse error" in result.stderr, result)


def test_explicit_repository_paths_use_git_root_or_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolve paths predictably inside and outside Git, including absolute paths."""
    subject = import_module("afairesi.main")
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
        subject.resolve_repository_path(Path("example")) == tmp_path / "example",
        tmp_path,
    )
    _expect(subject.resolve_repository_path(nested) == nested, nested)


def test_cli_help_lists_current_commands_and_rejects_invalid_requests() -> None:
    """Expose consistent commands and reject invalid requests before creating state."""
    with TemporaryDirectory(prefix="afairesi-cli-") as directory:
        root = Path(directory)
        help_output = _run(root, "--help").stdout
        _expect("usage:" in help_output, help_output)
        commands = (("inspect",), ("test",))
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
            == help_output,
            "standalone command unavailable through PATH",
        )
        rejected = _run(root, "unknown-command", code=2)
        _expect("invalid choice" in rejected.stderr, rejected)
        command_names = ["inspect", "test"]
        positions = [help_output.index(f"    {name} ") for name in command_names]
        _expect(
            positions == sorted(positions),
            "help commands must be alphabetical",
        )
        _expect(_run(root).stdout == help_output, "bare afairesi must show help")
        _expect(not any(root.iterdir()), "invalid CLI request created state")


@pytest.mark.parametrize("kind", ["html", "latex", "lean4", "python"])
@pytest.mark.parametrize(
    "constructor",
    [
        "inputs.self.lib.BUILDER",
        "inputs.afairesi.lib.BUILDER",
        "(inputs.self).lib.BUILDER",
        "let lib = inputs.self.lib; in lib.BUILDER",
        "let build = inputs.self.lib.BUILDER; in build",
    ],
)
def test_declared_package_kind_allows_supporting_entrypoint_names(
    kind: str,
    constructor: str,
) -> None:
    """Keep the declared build kind when supporting files resemble entrypoints."""
    subject = import_module("afairesi.main")
    with _fresh_repository() as root:
        _fixture_add(root, "example", kind)
        package = root / "example"
        definition = package / "default.nix"
        builder = subject.PACKAGE_BUILDERS[kind]
        definition.write_text(
            definition.read_text().replace(
                f"inputs.self.lib.{builder}",
                "let dependency = inputs.self.lib.mkHtmlPackage {}; in "
                + constructor.replace("BUILDER", builder),
                1,
            ),
        )
        for marker in subject.KIND_MARKERS.values():
            if not (package / marker).exists():
                _write_fixture_source(package / marker, "")
        _git(root, "add", "--force", "example")
        report = subject.inspect_repository(root)
        _expect(report["example"]["type"] == kind, report)
        _check_flake_source(root)
        (package / subject.KIND_MARKERS[kind]).unlink()
        _expect(subject.detect_packages(root)[0].kind == kind, "lost declared kind")
        rejected = _check_flake_source(root, code=1)
        _expect(subject.KIND_MARKERS[kind] in rejected.stderr, rejected)


def test_untracked_definition_does_not_resolve_ambiguous_sources() -> None:
    """Use only selected source when choosing among multiple entrypoint files."""
    with _fresh_repository() as root:
        _fixture_add(root, "example", "python")
        _write_fixture_source(root / "example/ms.tex", "")
        _git(root, "add", "--force", "example/ms.tex")
        _git(root, "rm", "--cached", "example/default.nix")
        before = _snapshot(root)
        rejected = _check_flake_source(root, code=1)
        _expect("ambiguous project markers" in rejected.stderr, rejected)
        _expect(_snapshot(root) == before, "untracked definition changed state")


@pytest.mark.parametrize("kind", ["html", "latex", "lean4", "nix"])
def test_inspect_selects_tests_by_package_kind(kind: str) -> None:
    """Supporting Python files never replace another package kind's test report."""
    with _fresh_repository() as root:
        _fixture_add(root, "example", kind)
        package = root / "example"
        _write_fixture_source(package / "test_main.py", "not valid python!")
        _write_fixture_source(package / "test_main.lean", "example : 1 = 1 := rfl\n")
        _write_fixture_source(package / "test_main.nix", "{}\n")
        report = json.loads(_run(root, "inspect").stdout)["example"]
        expected = {"lean4": ["test_main.lean"], "nix": ["test_main.nix"]}.get(kind, [])
        _expect(report.get("tests", []) == expected, report)


def test_python_initializer_collision_is_rejected_before_build() -> None:
    """Reject a selected initializer without changing source or installing over main."""
    with _fresh_repository() as root:
        _fixture_add(root, "example", "python")
        initializer = root / "example/__init__.py"
        initializer.write_text('"""Personal initialization."""\n')
        _git(root, "add", "example/__init__.py")
        before = _snapshot(root)
        result = _check_flake_source(root, code=1)
        _expect("__init__.py: reserved" in result.stderr, result)
        result = _run(
            root,
            "eval",
            "--impure",
            "--json",
            "--expr",
            _template_expression(root, "example") + "package.meta",
            executable="nix",
            environment=_nix_environment(root),
            code=1,
        )
        _expect("reserves __init__.py" in result.stderr, result)
        _expect(
            _snapshot(root, exclude=("tmp",)) == before, "rejection changed sources"
        )


def test_source_validation_respects_excluded_trees() -> None:
    """Validate excluded trees once, independently of generated resource payloads."""
    with _fresh_repository() as root, pytest.MonkeyPatch.context() as patch:
        _check_excluded_trees(root, patch)


@pytest.mark.parametrize(
    ("kind", "name", "description", "library"),
    [
        ("python", "dash-case", 'A "quoted" report.\n${literal}\\path', False),
        ("python", "snake_case", "é\x08\x0c\x01", True),
        ("python", "dash-library", "Nested library metadata", True),
        ("python", "Mixed_Package--v2", "Mixed spelling", False),
        ("python", "_Private", "Leading underscore", True),
        ("html", "web-site", "Δ report", False),
        ("html", "Web.Site+v2", "Versioned site", False),
        ("latex", "my-document", "LaTeX\tresources", False),
        ("nix", "my-package", "é", False),
        ("python", "a", "x", False),
    ],
)
def test_package_builders_preserve_metadata_and_install_runnable_programs_and_assets(
    kind: str,
    name: str,
    description: str,
    *,
    library: bool,
) -> None:
    """Evaluate generated Nix and run installed Python and HTML launchers."""
    with _fresh_repository() as root:
        _fixture_add(root, name, kind, description)
        package = root / name
        asset = package / "assets/asset.bin"
        asset.parent.mkdir()
        asset.write_bytes(b"\x00\xff asset")
        definition = package / "default.nix"
        source = definition.read_text()
        if kind == "python":
            _write_fixture_source(package / "test_main.py", "def test_result(): pass\n")
            source = source.replace(
                "  meta.description =",
                '  preInstall = \'\'mkdir -p "$out"; test ! -d "$out/lib"; '
                "printf pre > \"$out/install-hooks\"'';\n"
                "  postInstall = ''test -d \"$out/lib\"; "
                "printf post >> \"$out/install-hooks\"'';\n"
                "  meta.description =",
            )
            if library:
                source = source.replace("executable = true;", "executable = false;")
                _write_fixture_source(package / "main.py", "VALUE = 1\n")
            else:
                _write_fixture_source(
                    package / "main.py",
                    "def main():\n    print('installed entrypoint')\n",
                )
        elif kind in {"html", "latex"}:
            binding = "runtimeInputs" if kind == "html" else "nativeBuildInputs"
            expression = (
                f"(let {binding} = [ pkgs.inner ]; in [ pkgs.git ]) ++ [ pkgs.curl ]"
            )
            dependency = "pkgs.http-server" if kind == "html" else "pkgs.texliveFull"
            source = source.replace(
                "  meta.description =",
                f"  {binding} = {expression} ++ [ {dependency} ];\n"
                '  passthru.custom = "preserved";\n  meta.description =',
            )
            if kind == "html":
                (package / "script.js").write_text("// Supporting JavaScript\n")
                (package / "style.css").write_text("/* Supporting CSS */\n")
                resource = root / "prm/source.bin"
                resource.parent.mkdir()
                resource.write_bytes(b"copied resource")
                source = source.replace(
                    "  meta.description =",
                    '  postInstall = let postInstall = "inner"; in '
                    "''cp ${../prm/source.bin} \"$out/assets/copied.bin\"'';\n"
                    "  meta.description =",
                )
            else:
                source = source.replace(
                    f"{binding} = {expression} ++ [ {dependency} ];",
                    f"inherit {binding};",
                ).replace(
                    "inputs.self",
                    f"let {binding} = {expression} ++ [ {dependency} ]; in inputs.self",
                    1,
                )
        definition.write_text(source)
        environment = _nix_environment(root)
        actual = _evaluated_template(root, name, environment)
        _expect(
            kind not in {"html", "latex"}
            or 'passthru.custom = "preserved";' in definition.read_text(),
            "evaluation changed custom package attributes",
        )
        expected_name = name.replace("-", "_") if kind == "python" else name
        _expect(actual["name"] == expected_name, actual)
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
    root = package.parent
    name = package.name
    asset = package / "assets/asset.bin"
    _check_launcher_environment(root, name, environment, library=library)
    module = name.replace("-", "_")
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    installed = output / f"lib/python{version}/site-packages"
    _expect(
        (output / "install-hooks").read_text() == "prepost",
        "Python install hooks did not run around installation",
    )
    _expect(
        (installed / module / "assets/asset.bin").read_bytes() == asset.read_bytes(),
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
        _write_fixture_source(
            package / "main.py",
            (
                "import argparse\n"
                "def main():\n"
                "    argparse.ArgumentParser().parse_args()\n"
            ),
        )
        definition = package / "default.nix"
        definition.write_text(
            definition.read_text().replace("executable = false;", "executable = true;")
        )
        restored = _evaluated_template(root, name, environment)
        _expect(restored["meta"]["mainProgram"] == name, restored)
    else:
        _expect(
            actual["meta"]["mainProgram"] == name and (output / "bin" / name).is_file(),
            actual,
        )
        launched = _run(
            root,
            executable=str(output / "bin" / name),
            environment=environment,
        )
        _expect(launched.stdout == "installed entrypoint\n", launched)
        _expect(not launched.stderr, launched)


def _check_installed_html_template(
    package: Path,
    output: Path,
    environment: dict[str, str],
    actual: dict[str, Any],
) -> None:
    """Check browser options and resources through the installed launcher."""
    root = package.parent
    name = package.name
    asset = package / "assets/asset.bin"
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
            executable=str(output / "bin" / name),
            environment=environment,
        )
        site, *options = json.loads(result.stdout)
        _expect(options == list(expected), result)
        _expect(
            (Path(site) / "assets/asset.bin").read_bytes() == asset.read_bytes(),
            site,
        )
        _expect(
            (Path(site) / "assets/copied.bin").read_bytes() == b"copied resource",
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
        'OUTPUT = Path("example/tmp")',
        'ROOT = Path(__file__).parent\nOUTPUT = ROOT / "tmp"',
        'ROOT = Path(os.environ.get("ROOT", Path.cwd()))\nOUTPUT = ROOT / "tmp"',
        (
            'RUNTIME_DIR = Path.home() / "forge.example/team/other/custom"\n'
            'OUTPUT = RUNTIME_DIR / "reports"'
        ),
        'OUTPUT = Path("/persistent/custom/report")',
        'OUTPUT = Path(os.environ["OUTPUT"])',
        (
            'INPUT_DIRS = (Path.home() / "shared-data",)\n'
            'OUTPUT = INPUT_DIRS[0] / "report"'
        ),
    ],
)
def test_application_runtime_paths_are_unrestricted(
    source: str,
    tmp_path: Path,
) -> None:
    """Inspect tracked application paths without restricting or executing them."""
    subject = import_module("afairesi.main")
    _git(tmp_path, "init", "--quiet")
    _git(tmp_path, "remote", "add", "origin", "git@forge.example:team/repo.git")
    package = _make_source_package(tmp_path, "example", "")
    _write_fixture_source(
        package / "main.py",
        "import os\nfrom pathlib import Path\n"
        + source
        + "\nraise RuntimeError('do not execute')\n",
    )
    packages, issues = subject.inspect_structure(tmp_path)
    _expect([package.name for package in packages] == ["example"], packages)
    _expect(not issues, issues)


@pytest.mark.parametrize(
    "expression",
    [
        "pkgs.python3.pkgs.buildPythonPackage { src = ./.; }",
        (
            "let args = { inherit pkgs; src = ./.; executable = true; }; "
            "in inputs.self.lib.mkPythonPackage args"
        ),
        (
            "with inputs.self.lib; "
            "mkPythonPackage { inherit pkgs; src = ./.; executable = true; }"
        ),
        "import ./builder.nix { inherit inputs pkgs; }",
    ],
)
def test_source_validation_accepts_custom_package_expressions(
    expression: str,
) -> None:
    """Accept Nix expressions without rewriting builders or evaluating source."""
    with _fresh_repository() as root:
        _fixture_add(root, "example", "python")
        definition = root / "example/default.nix"
        definition.write_text("{ inputs, pkgs, ... }: " + expression + "\n")
        _git(root, "add", ".")
        before = _snapshot(root)
        _check_flake_source(root)
        _expect(_snapshot(root) == before, "validation changed repository")


def test_equivalent_package_bindings_are_valid_and_preserved() -> None:
    """Evaluate aliases and inherited settings without normalizing their spelling."""
    with _fresh_repository() as root:
        _fixture_add(root, "example", "python")
        definition = root / "example/default.nix"
        definition.write_text(
            "{ inputs, pkgs, ... }: let\n"
            "  lib = inputs.self.lib;\n"
            "  packageSet = pkgs;\n"
            "  source = ./.;\n"
            "  executable = true;\n"
            "in lib.mkPythonPackage {\n"
            "  pkgs = packageSet; src = source; inherit executable;\n"
            '  meta.description = "Equivalent definition";\n'
            "}\n",
        )
        _git(root, "add", ".")
        before = _snapshot(root)
        _check_flake_source(root)
        _expect(_snapshot(root) == before, "equivalent bindings were rewritten")
        evaluated = _evaluated_template(root, "example", _nix_environment(root))
        _expect(evaluated["meta"]["description"] == "Equivalent definition", evaluated)
        _expect(evaluated["meta"]["mainProgram"] == "example", evaluated)


@pytest.mark.parametrize("relative", ["flake.nix", "example/default.nix"])
def test_source_validation_rejects_malformed_nix(relative: str) -> None:
    """Separate expression flexibility from Nix syntax errors."""
    with _fresh_repository() as root:
        _fixture_add(root, "example", "python")
        (root / relative).write_text("{ inputs, pkgs, ... }: (\n")
        before = _snapshot(root)
        result = _check_flake_source(root, code=1)
        _expect("parse error" in result.stderr, result)
        _expect(_snapshot(root) == before, "syntax validation changed repository")


def test_opaque_package_definitions_allow_supporting_language_markers() -> None:
    """Keep opaque definitions generic when source markers cannot identify a kind."""
    with _fresh_repository() as root:
        _fixture_add(root, "example", "python")
        (root / "example/default.nix").write_text(
            "{ inputs, pkgs, ... }: import ./builder.nix { inherit inputs pkgs; }\n",
        )
        _write_fixture_source(root / "example/ms.tex", "Supporting LaTeX")
        _git(root, "add", ".")
        before = _snapshot(root)
        _check_flake_source(root)
        _expect(_snapshot(root) == before, "opaque definition was rewritten")
        subject = import_module("afairesi.main")
        _expect(subject.detect_packages(root)[0].kind == "nix", "guessed a language")


def test_source_inspection_handles_deep_unrelated_calls(tmp_path: Path) -> None:
    """Inspect large call expressions without recursively rendering their source."""
    subject = import_module("afairesi.main")
    package = _make_source_package(tmp_path, "example", "")
    _write_fixture_source(
        package / "main.py",
        "service" + ".client" * 1500 + "()\n",
    )
    packages, issues = subject.inspect_structure(tmp_path)
    _expect([package.name for package in packages] == ["example"], packages)
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
    subject = import_module("afairesi.main")
    path = tmp_path / "test_main.py"
    path.write_text("raise RuntimeError('must not execute')\n" + source)
    _expect(subject.has_python_tests(path) == expected, source)


@pytest.mark.parametrize(
    "source",
    [
        "def testCamelCase(): pass\ndef test(): pass\n",
        "class TestBehavior:\n    def testCamelCase(self): pass\n",
        (
            "from unittest import TestCase\n"
            "class Reports(TestCase):\n    def testCamelCase(self): pass\n"
        ),
    ],
)
def test_static_test_names_match_native_collection(tmp_path: Path, source: str) -> None:
    """Use pytest's collection as the oracle for ordinary and unittest test names."""
    path = tmp_path / "test_main.py"
    path.write_text(source)
    result = subprocess.run(  # noqa: S603 - Use this interpreter with test-authored code and arguments.
        [
            sys.executable,
            "-B",
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "--import-mode=importlib",
            "--collect-only",
            "-q",
            str(path),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    collected = sorted(
        line.split("::", 1)[1] for line in result.stdout.splitlines() if "::" in line
    )
    subject = import_module("afairesi.main")
    declared = sorted(
        f"{owner}::{definition.name}" if owner else definition.name
        for owner, definition in subject.python_test_definitions(ast.parse(source))
    )
    _expect(declared == collected, (declared, collected))


@pytest.mark.parametrize(
    ("kind", "name"),
    [
        ("python", "Mixed_Name-v2"),
        ("python", "double--dash"),
        ("python", "_Private"),
        ("html", "Web.Site+v2"),
        ("latex", "Report.v2"),
        ("lean4", "Proof_Library-v2"),
        ("nix", "Package Name"),
        ("nix", "δοκιμή"),
        ("host", "workLaptop"),
    ],
)
def test_resource_names_have_no_spelling_convention(kind: str, name: str) -> None:
    """Validate and inspect names without a shared style rule."""
    with _fresh_repository() as root:
        _fixture_add(root, name, kind)
        before = _snapshot(root)
        _check_flake_source(root)
        report = json.loads(_run(root, "inspect").stdout)
        _expect(report[name]["type"] == kind, report)
        _expect(_snapshot(root) == before, "validation changed resource spelling")


@pytest.mark.parametrize("filename", ["main.nix", "test_main.nix"])
def test_nix_optional_sources_are_formatted(filename: str) -> None:
    """Apply the same formatting to entry files and nested supporting Nix files."""
    with _fresh_repository() as root:
        _fixture_add(root, "example", "nix")
        source = root / "example" / filename
        original = "{z=2;a=1;}\n"
        _write_fixture_source(source, original)
        supporting = root / "example/prm" / filename
        supporting.parent.mkdir()
        supporting.write_text(original)
        _run(
            root,
            "--no-cache",
            f"example/{filename}",
            f"example/prm/{filename}",
            executable="treefmt",
        )
        _expect(source.read_text() != original, "optional Nix source was not formatted")
        _expect(
            supporting.read_text() == source.read_text(),
            "supporting source was skipped",
        )
        _run(
            root,
            "--no-cache",
            "--fail-on-change",
            f"example/{filename}",
            executable="treefmt",
        )
        result = _run(
            root,
            "eval",
            "--json",
            "--file",
            str(source),
            executable="nix",
            environment=_nix_environment(root),
        )
        _expect(json.loads(result.stdout) == {"a": 1, "z": 2}, result)


def test_web_formatting_preserves_literal_line_breaks() -> None:
    """Automatic formatting preserves JavaScript strings and HTML preformatted text."""
    with _fresh_repository() as root:
        _fixture_add(root, "site", "html")
        script = root / "site/script.js"
        script.write_text("document.body.dataset.value = `first\n\nsecond`;\n")
        html = root / "site/index.html"
        html.write_text(
            "<!doctype html><html><body><pre>first\n\nsecond</pre></body></html>\n",
        )
        _run(
            root,
            "--no-cache",
            "site/script.js",
            "site/index.html",
            executable="treefmt",
        )
        _expect("`first\n\nsecond`" in script.read_text(), script.read_text())
        _expect("first\n\nsecond" in html.read_text(), html.read_text())


def test_formatting_transforms_tracked_sources_without_staging() -> None:
    """Remove ordinary comments while preserving lint findings and Git selection."""
    with _fresh_repository() as root:
        _fixture_add(root, "site", "html")
        _write_fixture_source(root / "flake.nix", "{}\n")
        sources = {
            "site/script.js": "// Keep this comment\nconst value={answer:42};\n",
            "site/assets/app.js": "// Keep this comment\nconst value={answer:42};\n",
            "site/style.css": "/* Keep this comment */\nbody{color:red}\n",
            "site/assets/theme.css": "/* Keep this comment */\nbody{color:red}\n",
            "site/helpers/extra.py": "# Keep this comment\nimport os\nVALUE=  1\n",
        }
        for name, source in sources.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source)
            _git(root, "add", "--force", "--", name)
        local = root / "site/local.py"
        local.write_text("# Keep this comment\nVALUE=  2\n")
        ignore = root / ".gitignore"
        ignore.write_text("*\n\n!*/\n")
        staged = _git(root, "diff", "--cached", "--binary")
        _run(root, "--no-cache", executable="treefmt")
        _run(root, "--no-cache", "--fail-on-change", executable="treefmt")
        for name, source in sources.items():
            formatted = (root / name).read_text()
            _expect(formatted != source and "Keep this comment" not in formatted, name)
        _expect(
            (root / "site/script.js").read_text()
            == (root / "site/assets/app.js").read_text(),
            "JavaScript formatting depends on filename or depth",
        )
        _expect(
            (root / "site/style.css").read_text()
            == (root / "site/assets/theme.css").read_text(),
            "CSS formatting depends on filename or depth",
        )
        _expect(
            "import os" in (root / "site/helpers/extra.py").read_text(),
            "lint fix applied",
        )
        _expect(
            local.read_text() == "# Keep this comment\nVALUE=  2\n",
            "local file changed",
        )
        _expect(ignore.read_text() == "*\n\n!*/\n", "formatter changed ignore rules")
        _expect(
            _git(root, "diff", "--cached", "--binary") == staged,
            "formatter staged changes",
        )


@pytest.mark.parametrize("tracked", [False, True])
def test_formatting_ignores_repository_tool_configuration(*, tracked: bool) -> None:
    """Selected sources receive the same policy with tracked or local tool settings."""
    with _fresh_repository() as root:
        files = {
            "flake.nix": "{}\n",
            "example.py": '# ordinary explanation\nvalue = "hello"\n',
            "site/example.js": (
                "// ordinary explanation\nexport const value = () => {\n"
                '  return "hello";\n};\n'
            ),
            "paper/ms.tex": (
                "\\documentclass{article}\n% ordinary explanation\n"
                "\\begin{document}\nHello.\n\\end{document}\n"
            ),
        }
        for name, source in files.items():
            path = root / name
            path.parent.mkdir(exist_ok=True)
            path.write_text(source)
        _git(root, "add", ".")
        _run(root, "--no-cache", executable="treefmt")
        expected = {name: (root / name).read_bytes() for name in files}
        configs = {
            "pyproject.toml": '[tool.ruff.format]\nquote-style = "single"\n',
            ".oxfmtrc.json": '{"singleQuote": true, "semi": false}\n',
            "site/.oxfmtrc.json": '{"singleQuote": true, "semi": false}\n',
            ".uncomment.toml": 'preserve_patterns = ["ordinary explanation"]\n',
            "tex-fmt.toml": "wraplen = 10\n",
            ".editorconfig": (
                "root = true\n[*]\nindent_style = tab\nindent_size = 8\n"
                "end_of_line = crlf\ninsert_final_newline = false\n"
                "max_line_length = 20\n"
            ),
        }
        for name, source in configs.items():
            (root / name).write_text(source)
        if tracked:
            _git(root, "add", "--force", *configs)
        for name, source in files.items():
            (root / name).write_text(source)
        _run(root, "--no-cache", executable="treefmt")
        for name, content in expected.items():
            _expect((root / name).read_bytes() == content, name)
            _expect(b"ordinary explanation" not in content, name)


def test_formatting_normalizes_metadata_and_supporting_sources() -> None:
    """Normalize selected metadata and supporting files consistently at every depth."""
    with _fresh_repository() as root:
        _fixture_add(root, "site", "html")
        metadata = {
            ".gitignore": "*\n\n!*/\n",
            "LICENSE": "first\n\nsecond\n",
            "flake.lock": "{\n\n}\n",
        }
        for name in ("prm", "tmp"):
            _fixture_add(root, name, "nix")
            _write_fixture_source(root / name / "main.nix", "{z=2;a=1;}\n")
        for name, source in metadata.items():
            (root / name).write_text(source)
        supporting = ("site/assets", "site/fixtures", "site/prm", "site/tmp")
        supporting_source = "# Explanation\nvalue=  1\n"
        for name in supporting:
            directory = root / name
            directory.mkdir(parents=True)
            _write_fixture_source(directory / "main.py", supporting_source)
        _run(
            root,
            "--no-cache",
            *metadata,
            "prm/main.nix",
            "tmp/main.nix",
            *(f"{name}/main.py" for name in supporting),
            executable="treefmt",
        )
        expected_metadata = {
            ".gitignore": metadata[".gitignore"],
            "LICENSE": metadata["LICENSE"],
            "flake.lock": metadata["flake.lock"],
        }
        for name, source in expected_metadata.items():
            _expect((root / name).read_text() == source, name)
        for name in ("prm", "tmp"):
            _expect("a = 1;" in (root / name / "main.nix").read_text(), name)
        for name in supporting:
            _expect(
                (root / name / "main.py").read_text() == "value = 1\n",
                name,
            )


def _campaign_make_runner_target(
    root: Path,
    source: str,
    tests: str,
    *,
    name: str = "example",
) -> Path:
    """Create a minimal canonical package for an isolated test."""
    package = root / name
    package.mkdir(parents=True)
    _write_fixture_source(root / "flake.nix", "{}", encoding="utf-8")
    _write_fixture_source(package / "default.nix", "{}", encoding="utf-8")
    _write_fixture_source(package / "main.py", source, encoding="utf-8")
    _write_fixture_source(package / "test_main.py", tests, encoding="utf-8")
    return package


def _campaign_prepare_runner_flake(
    root: Path,
    tests: str,
    source: str = "def main():\n    print('ready')\n",
    *,
    name: str = "example",
) -> dict[str, str]:
    """Provide an offline flake backed by this check's real Python environment."""
    _campaign_make_runner_target(root, source, tests, name=name)
    builders = root / "prm/builders"
    shutil.copytree(_nix_source(), builders)
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
    _write_fixture_source(
        root / "flake.nix",
        "{ outputs = _: let nixLibrary = import ./prm/builders/main.nix "
        "{ self.outPath = ./prm; }; lib = nixLibrary.lib; "
        "pkgs = { lib = { concatMap = f: xs: builtins.concatLists (map f xs); "
        'makeBinPath = _: ""; }; writeText = builtins.toFile; }; '
        f"packageDrv.python.withPackages = _ : {json.dumps(sys.prefix)}; "
        "in { "
        f"packages.{json.dumps(system)} = builtins.listToAttrs (map (name: {{ "
        'name = "${name}-test-environment"; '
        "value = (lib.mkTestEnvironment { inherit pkgs packageDrv; }).manifest; "
        "}) (builtins.attrNames (builtins.readDir ./.))); }; }",
        encoding="utf-8",
    )
    for args in (["init", "--quiet"], ["add", "."]):
        _git(root, *args)
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
    return subprocess.run(  # noqa: S603 - Test PATH selects afairesi; arguments come from fixtures.
        ["afairesi", "test", command, str(root), *arguments],  # noqa: S607
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
    _git(root, "init", "--quiet")
    for filename in (".gitignore", "flake.nix", "flake.lock", "LICENSE"):
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
    shutil.copytree(_nix_source(), root / "prm/builders")
    (root / "prm/afairesi").mkdir()
    shutil.copyfile(Path(__file__).with_name("main.py"), root / "prm/afairesi/main.py")
    name = "z-last"
    _fixture_add(root, name, "python")
    package = root / name
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
        "from z_last import main as subject\n"
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
        "    assert callable(subject.main)\n"
        "    monkeypatch.setenv('AFAIRESI_COVERAGE_CHOICE', message)\n"
        "    result = "
        "subprocess.run([os.environ['PACKAGE_E2E_EXECUTABLE']],\n"
        "        capture_output=True, text=True)\n"
        "    assert result.stdout == message + '\\n'\n"
        "    assert result.stderr == ''\n"
    )
    _write_fixture_source(package / "main.py", source)
    _write_fixture_source(package / "test_main.py", tests)
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
    packages = (
        f"{json.dumps(name)} = {{ "
        f"src = builtins.path {{ path = ./{name}; "
        f'name = "{name}-src"; }}; '
        f"outPath = builtins.path {{ path = ./prm/installed/{name}; "
        f'name = "{name}-installed"; }}; '
        f"pname = {json.dumps(module_name)}; cliName = {json.dumps(name)}; "
        f"meta.mainProgram = {json.dumps(name)}; "
        "propagatedBuildInputs = []; python = { "
        f"sitePackages = {json.dumps(site_packages)}; "
        f"pkgs.coverage = {json.dumps(str(coverage_root))}; "
        f"withPackages = _: {json.dumps(sys.prefix)}; }}; }};"
    )
    expression = (
        "nixLibrary.pythonCheck { inherit pkgs; "
        f"packageName = {json.dumps(name)}; "
        f"packageDrv = packages.{json.dumps(name)}; "
        "testEnvironment = lib.mkTestEnvironment { inherit pkgs; "
        f"packageDrv = packages.{json.dumps(name)}; }}; }}"
    )
    checks = f"{json.dumps(name)} = {expression};"
    reports = (
        f"{json.dumps(name + '-coverage')} = lib.mkCoverage {{ "
        f"packageDrv = packages.{json.dumps(name)}; "
        f"check = checks.{json.dumps(name)}; }};"
    )
    _check_flake_source(root)
    _write_fixture_source(
        root / "flake.nix",
        "{ outputs = _: let nixLibrary = import ./prm/builders/main.nix "
        "{ self.outPath = ./prm; }; lib = nixLibrary.lib; "
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
        "packages = { " + packages + " }; checks = { " + checks + " }; "
        "in { packages.${system} = packages // { " + reports + " }; "
        "checks.${system} = checks; }; }\n",
    )
    _git(root, "add", ".")
    return environment


def _campaign_runner_package(
    root: Path,
    name: str,
    tests: str,
    source: str = "",
) -> None:
    """Add another offline runnable package without replacing the fixture flake."""
    flake = (root / "flake.nix").read_text()
    _campaign_make_runner_target(root, source, tests, name=name)
    _write_fixture_source(root / "flake.nix", flake)
    _git(root, "add", ".")


def _run_campaign(
    root: Path,
    environment: dict[str, str],
    command: str,
    *arguments: str,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run an omitted target and preserve original sources, settings, and refs."""
    before = _snapshot(root, exclude=("tmp",))
    result = subprocess.run(  # noqa: S603 - Test harness selects the executable and fixture arguments.
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


@pytest.mark.parametrize(
    ("command", "target", "nested"),
    [
        ("property", "omitted", False),
        ("property", "omitted", True),
        ("property", "root", False),
        ("property", "root", True),
        ("property", "relative", False),
        ("property", "relative", True),
        ("property", "absolute", False),
        ("property", "absolute", True),
        ("coverage", "omitted", False),
        ("coverage", "absolute", True),
        ("fuzz", "omitted", False),
        ("fuzz", "absolute", True),
        ("mutation", "omitted", False),
        ("mutation", "absolute", True),
        ("performance", "omitted", False),
        ("performance", "absolute", True),
    ],
)
def test_campaign_routes_targets_and_forwards_test_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    target: str,
    *,
    nested: bool,
) -> None:
    """Cover all path forms with property tests and both target kinds per campaign."""
    subject = import_module("afairesi.main")
    root = _repository(tmp_path)
    (root / ".gitmodules").touch()
    _git(root, "add", ".gitmodules")
    package = _make_source_package(root, "alpha", "def test_alpha(): pass\n")
    directory = package / "prm/nested" if nested else package
    directory.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(directory)
    arguments = {
        "omitted": [],
        "root": ["."],
        "relative": ["alpha"],
        "absolute": [str(package)],
    }[target]
    expected = root if target in {"omitted", "root"} else package
    runner = Mock(return_value=True)
    route = "_run_test_repository" if expected == root else "_run_test_package"
    route = "_run_coverage" if command == "coverage" else route
    monkeypatch.setattr(subject, route, runner)
    selectors = [] if command == "coverage" else ["-k", "keyword", "-m", "marker"]
    monkeypatch.setattr(
        sys,
        "argv",
        ["afairesi", "test", command, *arguments, *selectors],
    )
    with pytest.raises(SystemExit) as completed:
        subject.main()
    if completed.value.code != 0:
        raise AssertionError((completed.value.code, 0))
    if runner.call_count != 1:
        raise AssertionError((runner.call_count, 1))
    if runner.call_args.args[0] != expected:
        raise AssertionError((runner.call_args.args[0], expected))
    if command != "coverage" and runner.call_args.args[-1] != subject.TestSelection(
        keywords="keyword",
        markers="marker",
        max_examples=10 if command == "fuzz" else None,
    ):
        raise AssertionError(
            (
                runner.call_args.args[-1],
                subject.TestSelection(
                    keywords="keyword",
                    markers="marker",
                    max_examples=10 if command == "fuzz" else None,
                ),
            ),
        )


@pytest.mark.parametrize("location", ["outside", "root", "nested"])
def test_campaign_without_a_campaign_shows_help_without_running_tests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    location: str,
) -> None:
    """Show the same help inside and outside Git without creating state."""
    subject = import_module("afairesi.main")
    root = tmp_path / "repository"
    root.mkdir()
    _repository(root)
    nested = root / "example/prm/nested"
    nested.mkdir(parents=True)
    monkeypatch.chdir({"outside": tmp_path, "root": root, "nested": nested}[location])
    coverage_run = Mock(side_effect=AssertionError("unexpected coverage run"))
    other_campaigns = Mock(side_effect=AssertionError("unexpected campaign"))
    monkeypatch.setattr(subject, "_run_coverage", coverage_run)
    monkeypatch.setattr(subject, "_dispatch_test_runner", other_campaigns)
    before = _snapshot(root)
    monkeypatch.setattr(sys, "argv", ["afairesi", "test", "--help"])
    with pytest.raises(SystemExit) as completed:
        subject.main()
    if completed.value.code != 0:
        raise AssertionError((completed.value.code, 0))
    expected = capsys.readouterr()
    monkeypatch.setattr(sys, "argv", ["afairesi", "test"])
    subject.main()
    actual = capsys.readouterr()
    if actual.out != expected.out:
        raise AssertionError((actual.out, expected.out))
    if actual.err != "":
        raise AssertionError((actual.err, ""))
    if coverage_run.call_count != 0:
        raise AssertionError((coverage_run.call_count, 0))
    if other_campaigns.call_count != 0:
        raise AssertionError((other_campaigns.call_count, 0))
    if _snapshot(root) != before:
        raise AssertionError((_snapshot(root), before))

    if list(tmp_path.iterdir()) != [root]:
        raise AssertionError(list(tmp_path.iterdir()))


@pytest.mark.parametrize(
    "command",
    [None, "coverage", "property", "fuzz", "mutation", "performance"],
)
def test_campaign_help_is_available_outside_git(
    tmp_path: Path,
    command: str | None,
) -> None:
    """Describe each campaign without requiring repository or build state."""
    arguments = [] if command is None else [command]
    result = _run(tmp_path, "test", *arguments, "--help")
    if "usage: afairesi test" not in result.stdout:
        raise AssertionError(result)
    if list(tmp_path.iterdir()) != []:
        raise AssertionError(list(tmp_path.iterdir()))


@pytest.mark.parametrize(
    "command",
    ["coverage", "property", "fuzz", "mutation", "performance"],
)
def test_campaign_requires_repository(tmp_path: Path, command: str) -> None:
    """Keep explicit execution errors distinct from the bare help command."""
    result = _run(tmp_path, "test", command, code=1)
    if "not inside a Git repository" not in result.stderr:
        raise AssertionError(result)
    if list(tmp_path.iterdir()) != []:
        raise AssertionError(list(tmp_path.iterdir()))


@pytest.mark.parametrize(
    ("command", "timeout"),
    [
        ("property", "nan"),
        ("property", "inf"),
        ("property", "-inf"),
        ("property", "0"),
        ("property", "-1"),
        ("fuzz", "nan"),
        ("mutation", "nan"),
        ("performance", "nan"),
    ],
)
def test_campaign_rejects_invalid_timeout_without_changes(
    tmp_path: Path,
    command: str,
    timeout: str,
) -> None:
    """Cover each invalid budget and ensure every campaign uses the validator."""
    result = _run(
        tmp_path,
        "test",
        command,
        str(tmp_path),
        f"--timeout={timeout}",
        code=2,
    )
    if "--timeout must be positive and finite" not in result.stderr:
        raise AssertionError(result)
    if list(tmp_path.iterdir()) != []:
        raise AssertionError(list(tmp_path.iterdir()))


@pytest.mark.parametrize(
    ("command", "option"),
    [("mutation", "--max-mutations"), ("fuzz", "--max-examples")],
)
@pytest.mark.parametrize("limit", ["0", "-1"])
def test_campaign_rejects_invalid_count_without_changes(
    tmp_path: Path,
    command: str,
    option: str,
    limit: str,
) -> None:
    """Require a positive generation or mutation count."""
    result = _run(tmp_path, "test", command, str(tmp_path), option, limit, code=2)
    if f"{option} must be positive" not in result.stderr:
        raise AssertionError(result)
    if list(tmp_path.iterdir()) != []:
        raise AssertionError(list(tmp_path.iterdir()))


@pytest.mark.parametrize("limit", [None, 3])
def test_campaign_fuzz_selects_targets_and_applies_example_budgets(
    tmp_path: Path,
    limit: int | None,
) -> None:
    """Fuzz consumer packages with bounded generation and isolated CLI calls."""
    root = tmp_path / "consumer with spaces"
    tests = (
        "import os, subprocess\nfrom pathlib import Path\n"
        "from hypothesis import given, strategies as st\n"
        "@given(st.integers())\n"
        "def test_fuzz_cli(value):\n"
        "    result = subprocess.run([os.environ['PACKAGE_E2E_EXECUTABLE']], "
        "capture_output=True, text=True)\n"
        "    assert result.returncode == 0 and result.stdout == 'ready\\n'\n"
        "    with Path('examples').open('a') as output:\n"
        "        output.write(str(value) + '\\n')\n"
        "def test_regular():\n"
        "    raise AssertionError('ordinary test ran during fuzz campaign')\n"
        "@given(st.integers())\n"
        "def test_fuzz_unselected(value):\n"
        "    raise AssertionError('pytest selection ignored')\n"
    )
    environment = _campaign_prepare_runner_flake(root, tests)
    arguments = ["-k", "cli", "--timeout", "30"]
    if limit is not None:
        arguments.extend(["--max-examples", str(limit)])
    result = _run_campaign(root, environment, "fuzz", *arguments)
    _expect(result.returncode == 0 and "1 passed" in result.stdout, result)
    (workspace,) = _report_directory().glob("python-fuzz-example-*")
    _expect(
        len((workspace / "source/examples").read_text().splitlines()) == (limit or 10),
        workspace,
    )
    _expect((workspace / "hypothesis").is_dir(), workspace)
    _expect(not list(workspace.glob("test-runtime-*")), workspace)


def test_campaign_fuzz_skips_missing_tests_and_reports_failures(
    tmp_path: Path,
) -> None:
    """Continue across packages without hiding failures or invalid fuzz definitions."""
    root = tmp_path / "consumer"
    environment = _campaign_prepare_runner_flake(
        root,
        "def test_ordinary(): raise AssertionError('should be deselected')\n",
    )
    _campaign_runner_package(
        root,
        "broken",
        "from hypothesis import given, strategies as st\n"
        "@given(st.binary())\n"
        "def test_fuzz_failure(value): assert False, value\n",
    )
    _campaign_runner_package(root, "invalid", "def test_fuzz_plain(): pass\n")
    _campaign_runner_package(root, "last", "def test_regular(): pass\n")
    _campaign_runner_package(
        root,
        "import_failure",
        "raise ImportError('broken import')\n",
    )
    result = _run_campaign(root, environment, "fuzz")
    _expect(result.returncode == 1, result)
    _expect("0 passed, 3 failed, 2 skipped" in result.stdout, result)
    _expect("No fuzz tests selected; skipping." in result.stdout, result)
    _expect("Failing test case:" in result.stderr, result)
    _expect("fuzz tests require @given" in result.stderr, result)
    _expect("broken import" in result.stderr, result)
    (workspace,) = _report_directory().glob("python-fuzz-broken-*")
    _expect((workspace / "tests.log").is_file(), workspace)


def test_campaign_performance_profiles_tests_and_cli_without_changing_sources() -> None:
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
            "    source = Path('example/main.py')\n"
            "    stamp = source.stat()\n"
            "    source.write_text(source.read_text().replace('sys.argv[1]', "
            "'sys.argv[2]'))\n"
            "    os.utime(source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))\n"
            "    result = subprocess.run([os.environ['PACKAGE_E2E_EXECUTABLE'], "
            "'stale', 'fresh'], capture_output=True, text=True)\n"
            "    assert result.returncode == 0, result.stderr\n"
            "    assert result.stdout == 'fresh\\n'\n"
            "    assert False\n"
            "@given(st.integers())\n"
            "@example(7)\n"
            "def test_explicit(value):\n"
            "    with Path('examples').open('a') as output:\n"
            "        output.write(str(value) + '\\n')\n"
            "def test_unselected():\n"
            "    raise AssertionError('selection ignored')\n"
        )
        environment = _campaign_prepare_runner_flake(root, tests, source)
        result = _run_campaign(
            root,
            environment,
            "performance",
            "-k",
            "cli or explicit",
        )
        _expect(result.returncode == 1, result)
        output = result.stdout + result.stderr
        _expect("slowest" in output and "test_cli" in output, result)
        (workspace,) = _report_directory().glob("python-performance-example-*")
        _expect(
            "7" in (workspace / "source/examples").read_text().splitlines()
            and len((workspace / "source/examples").read_text().splitlines()) > 1,
            workspace,
        )
        profiles = workspace / "profiles"
        _expect(str(profiles) in result.stdout, result)
        _expect(
            (root / "example/main.py").read_text() == source,
            root,
        )
        for script, function in (("run-tests", "test_cli"), ("package-entry", "work")):
            reports = list(profiles.glob(f"{script}-*.prof"))
            _expect(
                len(reports) == (1 if script == "run-tests" else 2),
                reports,
            )
            for report in reports:
                stats = pstats.Stats(str(report))
                functions = stats.get_stats_profile().func_profiles
                _expect(function in functions, functions)


@pytest.mark.parametrize(
    "layout",
    ["empty", "nonpython", "untested", "single_untested"],
)
def test_campaign_handles_nonrunnable_targets_without_changes(layout: str) -> None:
    """Skip untested repository packages and reject nonrunnable targets safely."""
    with _fresh_repository() as root:
        target = root
        if layout == "nonpython":
            _fixture_add(root, "web", "html")
        elif layout in {"untested", "single_untested"}:
            target = _campaign_make_runner_target(root, "", "")
            (target / "test_main.py").unlink()
            if layout == "untested":
                target = root
        before = _snapshot(root)
        for command in ("property", "mutation"):
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
        _expect(
            _snapshot(root) == before,
            "nonrunnable target created state",
        )


def test_campaign_coverage_measures_subprocesses_without_changing_sources() -> None:
    """Build real checks, measure CLI lines, and keep reports outside the checkout."""
    with TemporaryDirectory(prefix="afairesi-coverage-") as directory:
        root = Path(directory) / "source with spaces"
        environment = _campaign_prepare_coverage_flake(root)
        before = _snapshot(root)
        expression = (
            f"let f = builtins.getFlake {json.dumps('git+' + root.as_uri())}; "
            'in f.checks.${builtins.currentSystem}."z-last"'
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
        result = _campaign_run_runner_cli(root, environment, "coverage")
        _expect(not result.returncode, result)
        reports = [
            line
            for line in result.stdout.splitlines()
            if line.endswith("/html/index.html")
        ]
        _expect(len(reports) == 1, result)
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
        _expect(
            _snapshot(root) == before and not (root / "tmp").exists(),
            "coverage changed repository state",
        )


def test_campaign_coverage_continues_after_failed_nix_builds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Check build failures and continuation at the Nix process boundary."""
    campaign_subject = import_module("afairesi.main")
    _repository(tmp_path)
    failed = _make_source_package(
        tmp_path,
        "example",
        "def test_ready(): pass\n",
    )
    _make_source_package(tmp_path, "z-last", "def test_ready(): pass\n")
    untested = _make_source_package(tmp_path, "untested", "")
    (untested / "test_main.py").unlink()
    report = tmp_path / "tmp/report"
    (report / "html").mkdir(parents=True)
    (report / "html/index.html").touch()
    (report / "coverage.json").write_text(
        json.dumps(
            {
                "totals": {
                    "percent_covered": 100,
                    "missing_lines": 0,
                    "missing_branches": 0,
                },
                "files": {},
            },
        ),
    )
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
    before = _snapshot(tmp_path)
    for target, status in ((tmp_path, 1), (failed, 17)):
        monkeypatch.setattr(
            sys,
            "argv",
            ["afairesi", "test", "coverage", str(target)],
        )
        with pytest.raises(SystemExit) as completed:
            campaign_subject.main()
        _expect(completed.value.code == status, completed)
        output = capsys.readouterr()
        _expect("error: coverage builder failed" in output.err, output)
        if target == tmp_path:
            _expect(
                "1 passed, 1 failed, 1 skipped" in output.out
                and "z-last:" in output.out
                and str(report / "html/index.html") in output.out
                and "afairesi test coverage: example:" in output.err,
                output,
            )
        _expect(_snapshot(tmp_path) == before, target)
    prefix = f"git+{tmp_path.as_uri()}#"
    _expect(
        builds
        == [
            prefix + name
            for name in ("example-coverage", "z-last-coverage", "example-coverage")
        ],
        builds,
    )


@pytest.mark.parametrize(
    ("missing", "expected"),
    [
        ([], ""),
        ([[3, 6]], "  main.py: missing branches 3 -> 6\n"),
        ([[3, -1]], "  main.py: missing branches 3 -> exit\n"),
    ],
)
def test_campaign_coverage_describes_uncovered_branches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    missing: list[list[int]],
    expected: str,
) -> None:
    """Print branch destinations alongside the retained HTML report."""
    subject = import_module("afairesi.main")
    root = tmp_path / "source"
    root.mkdir()
    package = _campaign_make_runner_target(root, "", "")
    report = tmp_path / "report"
    (report / "html").mkdir(parents=True)
    (report / "html/index.html").touch()
    (report / "coverage.json").write_text(
        json.dumps(
            {
                "totals": {
                    "percent_covered": 60,
                    "missing_lines": 2,
                    "missing_branches": len(missing),
                },
                "files": {"main.py": {"missing_branches": missing}},
            },
        ),
    )
    monkeypatch.setattr(
        subject.subprocess,
        "run",
        Mock(return_value=subprocess.CompletedProcess([], 0, str(report) + "\n")),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["afairesi", "test", "coverage", str(package)],
    )
    with pytest.raises(SystemExit) as completed:
        subject.main()
    _expect(completed.value.code == 0, completed)
    actual = capsys.readouterr()
    summary = f"  60.0% coverage; 2 missing lines, {len(missing)} missing branches\n"
    expected_output = (
        f"Building coverage for example...\nexample: {report}/html/index.html\n"
        + summary
        + expected
    )
    if actual.out != expected_output:
        raise AssertionError((actual.out, expected_output))
    if actual.err != "":
        raise AssertionError(actual.err)


@pytest.mark.parametrize(
    ("missing", "message"),
    [("html", "no HTML report"), ("json", "coverage.json")],
)
def test_campaign_coverage_rejects_missing_report_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    missing: str,
    message: str,
) -> None:
    """Validate coverage output guards without rebuilding a successful package."""
    campaign_subject = import_module("afairesi.main")
    root = tmp_path / "source"
    root.mkdir()
    _repository(root)
    package = _make_source_package(root, "example", "def test_ready(): pass\n")
    report = tmp_path / "report"
    report.mkdir()
    if missing == "json":
        (report / "html").mkdir()
        (report / "html/index.html").touch()
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
    before = _snapshot(root)
    monkeypatch.setattr(
        sys,
        "argv",
        ["afairesi", "test", "coverage", str(package)],
    )
    with pytest.raises(SystemExit) as completed:
        campaign_subject.main()
    _expect(
        completed.value.code == 1 and message in capsys.readouterr().err,
        completed,
    )
    _expect(len(builds) == 1, builds)
    _expect(
        _snapshot(root) == before,
        "coverage validation modified source state",
    )


@pytest.mark.parametrize(
    "command",
    ["coverage", "property", "fuzz", "mutation", "performance"],
)
@pytest.mark.parametrize("failure", ["missing-output", "invalid-flake"])
def test_campaign_preserves_native_nix_errors_for_package_targets(
    tmp_path: Path,
    command: str,
    failure: str,
) -> None:
    """Use Nix as the oracle rather than guessing whether a flake output exists."""
    root = tmp_path / "source with spaces"
    environment = _campaign_prepare_runner_flake(root, "def test_ready(): pass\n")
    _write_fixture_source(
        root / "flake.nix",
        "{ outputs = _: {}; }" if failure == "missing-output" else "{",
    )
    _git(root, "add", "flake.nix")
    attribute = (
        "example-coverage" if command == "coverage" else "example-test-environment"
    )
    expected = subprocess.run(  # noqa: S603 - Fixed Nix executable with test-authored expressions.
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
    _expect(expected.returncode != 0, expected)
    before = _snapshot(root, exclude=("tmp",))
    actual = _run(
        root,
        "test",
        command,
        str(root / "example"),
        code=expected.returncode,
        environment=environment,
    )
    _expect(
        expected.stderr.strip() in actual.stderr,
        (expected.stderr, actual.stderr),
    )
    _expect(
        _snapshot(root, exclude=("tmp",)) == before,
        "failed campaign changed package sources or Git state",
    )


@pytest.mark.parametrize("failure", ["assertion", "collection", "selection", "usage"])
def test_campaign_property_preserves_native_pytest_status(
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
    package = root / "example"
    expected = subprocess.run(  # noqa: S603 - Use this interpreter with test-authored code and arguments.
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
    _expect(expected.returncode != 0, expected)
    before = _snapshot(root, exclude=("tmp",))
    actual = _run(
        root,
        "test",
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
        _expect(diagnostic in actual.stderr, (diagnostic, actual.stderr))
    _expect(
        _snapshot(root, exclude=("tmp",)) == before,
        "pytest failure changed original package sources or Git state",
    )


def test_campaign_coverage_skips_untested_packages_and_continues_after_invalid_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Validate package eligibility and continuation separately from Nix builds."""
    campaign_subject = import_module("afairesi.main")
    _repository(tmp_path)
    _make_source_package(tmp_path, "alpha", "def invalid(")
    untested = _make_source_package(tmp_path, "untested", "")
    (untested / "test_main.py").unlink()
    ready = _make_source_package(
        tmp_path,
        "z-last",
        "def test_ready(): pass\n",
    )
    built: list[Path] = []
    monkeypatch.setattr(campaign_subject, "_build_package_coverage", built.append)
    before = _snapshot(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        ["afairesi", "test", "coverage", str(tmp_path)],
    )
    with pytest.raises(SystemExit) as completed:
        campaign_subject.main()
    _expect(completed.value.code == 1, "invalid source must fail coverage")
    output = capsys.readouterr()
    _expect(built == [ready], built)
    _expect(
        "1 passed, 1 failed, 1 skipped" in output.out
        and "afairesi test coverage: alpha:" in output.err,
        output,
    )
    _expect(
        _snapshot(tmp_path) == before,
        "coverage eligibility modified sources",
    )


@pytest.mark.parametrize("failure", ["counterexample", "timeout"])
def test_campaign_property_continues_after_failures(failure: str) -> None:
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
        (root / "untested/test_main.py").unlink()
        result = _run_campaign(
            root,
            environment,
            "property",
            "--timeout",
            "20" if failure == "counterexample" else "10",
        )
        _expect(
            result.returncode == 1 and "1 passed, 1 failed, 1 skipped" in result.stdout,
            result,
        )
        (failed,) = _report_directory().glob("python-property-alpha-*")
        diagnostic = "Failing test case" if failure == "counterexample" else "timed out"
        _expect(
            diagnostic
            in result.stdout + result.stderr + (failed / "tests.log").read_text(),
            result,
        )
        if failure == "timeout":
            pid = int((failed / "source/child.pid").read_text())
            status = Path(f"/proc/{pid}/status")
            _expect(
                not status.exists() or "State:\tZ" in status.read_text(),
                "timed-out suite left a running descendant",
            )


def test_campaign_property_generates_cases_in_isolated_sources() -> None:
    """Count generated cases and isolate copied Git commands and runtime state."""
    with TemporaryDirectory(prefix="afairesi-campaign-") as directory:
        root = Path(directory) / "source with spaces"
        max_examples = 5
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
            "from git_example import main as subject\n"
            "from pathlib import Path\n"
            "from hypothesis import given, example, settings, strategies as st\n"
            f"settings.register_profile('package', max_examples={max_examples})\n"
            "settings.load_profile('package')\n"
            "@given(st.integers())\n"
            "@example(0)\n"
            "def test_property(value):\n"
            "    with Path('examples').open('a') as output:\n"
            "        output.write(str(value) + '\\n')\n"
            "def test_cli():\n"
            "    assert callable(subject.main)\n"
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
            "        assert Path(directory).is_relative_to(Path.cwd().parent)\n"
            "    assert not (Path.home() / 'sentinel').exists()\n"
            "    child = subprocess.run([sys.executable, '-c', "
            '"import importlib; '
            "print(importlib.import_module('git_example.main').__file__)\"], "
            "cwd=Path.home(), capture_output=True, text=True)\n"
            "    assert child.returncode == 0, child.stderr\n"
            "    assert Path(child.stdout.strip()).resolve().is_relative_to("
            "Path.cwd())\n"
        )
        environment = _campaign_prepare_runner_flake(
            root,
            tests,
            source,
            name="git-example",
        )
        environment["PYTEST_ADDOPTS"] = "--hypothesis-show-statistics"
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
        result = _campaign_run_runner_cli(
            root,
            environment,
            "property",
        )
        _expect(
            not result.returncode and "1 passed, 0 failed, 0 skipped" in result.stdout,
            result,
        )
        _expect(
            _snapshot(root, exclude=("tmp",)) == before,
            "property campaign modified source",
        )
        workspaces = list(
            (Path(environment["XDG_STATE_HOME"]) / "afairesi").glob(
                "python-property-git-example-*",
            ),
        )
        _expect(len(workspaces) == 1, workspaces)
        for workspace in workspaces:
            _expect(
                len((workspace / "source/examples").read_text().splitlines())
                == max_examples + 1,
                workspace,
            )
            _expect(
                f"{max_examples} passing, 0 failing"
                in (workspace / "tests.log").read_text(),
                workspace,
            )
            _expect(
                "2 passed" in (workspace / "tests.log").read_text()
                and not list(workspace.glob("test-runtime-*")),
                workspace,
            )
        _expect(
            sentinel.read_text() == "preserve",
            caller_home,
        )


@pytest.mark.parametrize("command", ["property", "fuzz", "performance"])
def test_campaign_preserves_native_plugins_options_and_hypothesis_profiles(
    command: str,
) -> None:
    """Respect package plugins, pytest options, and the active example budget."""
    with TemporaryDirectory(prefix="afairesi-native-pytest-") as directory:
        root = Path(directory) / "source"
        tests = (
            "from datetime import timedelta\n"
            "from pathlib import Path\n"
            "from hypothesis import given, settings, strategies as st\n"
            "@given(st.integers())\n"
            "def test_fuzz_property(value):\n"
            "    assert settings.default.deadline == timedelta(milliseconds=1234)\n"
            "    assert settings.default.max_examples == 3\n"
            "    with Path('examples').open('a') as output:\n"
            "        output.write(str(value) + '\\n')\n"
        )
        environment = _campaign_prepare_runner_flake(root, tests)
        plugin = root / "example/prm/native_plugin.py"
        plugin.parent.mkdir()
        plugin.write_text(
            "from hypothesis import settings\n"
            "settings.register_profile('package', max_examples=3, deadline=1234)\n"
            "def pytest_addoption(parser):\n"
            "    parser.addoption('--native-option', action='store_true')\n"
            "def pytest_configure(config):\n"
            "    assert config.getoption('--native-option')\n",
        )
        _git(root, "add", "--force", "--", str(plugin.relative_to(root)))
        environment["PYTEST_PLUGINS"] = "example.prm.native_plugin"
        environment["PYTEST_ADDOPTS"] = "--native-option --hypothesis-profile=package"
        result = _run_campaign(root, environment, command)
        _expect(result.returncode == 0, result)
        (workspace,) = _report_directory().glob(f"python-{command}-example-*")
        expected_examples = 3
        _expect(
            len((workspace / "source/examples").read_text().splitlines())
            == expected_examples,
            workspace,
        )


def test_campaign_mutation_rejects_invalid_baselines_and_continues() -> None:
    """Reject invalid baselines and accept a passing suite with no mutations."""
    with TemporaryDirectory(prefix="afairesi-baselines-") as directory:
        root = Path(directory) / "source with spaces"
        environment = _campaign_prepare_runner_flake(
            root,
            "from importlib import import_module\n"
            "from hypothesis import given, example, strategies as st\n"
            "main = import_module('git-example.main')\n"
            "@given(st.just(1))\n"
            "@example(0)\n"
            "def test_explicit(value):\n"
            "    assert value in {0, 1} and main is not None\n",
            "",
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
        (root / "untested/test_main.py").unlink()
        result = _run_campaign(
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
            (failed,) = _report_directory().glob(f"python-mutation-{name}-*")
            _expect(
                (failed / "baseline.log").is_file()
                and not (failed / "session.sqlite").exists(),
                failed,
            )
        (workspace,) = _report_directory().glob("python-mutation-git-example-*")
        with use_db(workspace / "session.sqlite", WorkDB.Mode.open) as database:
            _expect(database.num_work_items == 0, workspace)


@pytest.mark.parametrize(
    "problem",
    [None, "timeout", "error", "pending", "skipped", "incompetent"],
)
def test_campaign_mutation_preserves_engine_failure_status(
    tmp_path: Path,
    problem: str | None,
) -> None:
    """Check mixed results without running another mutation campaign."""
    campaign_subject = import_module("afairesi.main")
    outcomes = ["killed", "survived", *([problem] if problem is not None else [])]
    with use_db(tmp_path / "session.sqlite", WorkDB.Mode.create) as database:
        for outcome in outcomes:
            database.add_work_item(WorkItem(outcome, ()))
            if outcome == "pending":
                continue
            database.set_result(
                outcome,
                WorkResult(
                    worker_outcome=WorkerOutcome.EXCEPTION
                    if outcome == "error"
                    else WorkerOutcome.SKIPPED
                    if outcome == "skipped"
                    else WorkerOutcome.NORMAL,
                    test_outcome=MutationTestOutcome(outcome)
                    if outcome in {"killed", "survived", "incompetent"}
                    else MutationTestOutcome.KILLED,
                    output="timeout"
                    if outcome == "timeout"
                    else "native pytest output",
                ),
            )
    succeeded = campaign_subject._mutation_run_succeeded(tmp_path)  # noqa: SLF001 - Check engine status without running mutations.
    _expect(
        succeeded == (problem not in {"error", "pending", "incompetent"}),
        problem,
    )


def test_campaign_mutation_reports_outcomes_and_filters_mutations() -> None:
    """Check killed/surviving mutations and native filters once with a fixed value."""
    with TemporaryDirectory(prefix="afairesi-mutations-") as directory:
        root = Path(directory) / "source with spaces"
        source = (
            "def value():\n"
            "    return 1\n"
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
            "and result.stdout == '1\\n'\n"
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
        focused = _run_campaign(
            root,
            environment,
            "mutation",
            "--timeout",
            "10",
            "-k",
            "test_cli and not process_success",
            "--lines",
            f"{target_line}-{target_line}",
            "--exclude-operator",
            "^(?!.*NumberReplacer)",
            "--max-mutations",
            "1",
            cwd=root / "git-example",
        )
        _expect(not focused.returncode, focused)
        (focused_workspace,) = _report_directory().glob("python-mutation-git-example-*")
        _expect(
            (focused_workspace / "report.html").stat().st_size > 0,
            focused,
        )
        with use_db(focused_workspace / "session.sqlite", WorkDB.Mode.open) as database:
            selected = [
                (item, result)
                for item, result in database.completed_work_items
                if result.worker_outcome != WorkerOutcome.SKIPPED
            ]
        _expect(
            len(selected) == 1
            and selected[0][1].test_outcome == MutationTestOutcome.KILLED
            and selected[0][0].mutations[0].start_pos[0] == target_line,
            selected,
        )
        existing_workspaces = set(_report_directory().iterdir())
        weaker = _run_campaign(
            root,
            environment,
            "mutation",
            "--timeout",
            "10",
            "-k",
            "process_success",
            "--lines",
            f"{target_line}-{target_line}",
            "--exclude-operator",
            "^(?!.*NumberReplacer)",
            "--max-mutations",
            "1",
            cwd=root / "git-example",
        )
        _expect(not weaker.returncode, weaker)
        (weaker_workspace,) = set(_report_directory().iterdir()) - existing_workspaces
        with use_db(weaker_workspace / "session.sqlite", WorkDB.Mode.open) as database:
            weaker_selected = [
                (item, result)
                for item, result in database.completed_work_items
                if result.worker_outcome != WorkerOutcome.SKIPPED
            ]
        _expect(
            len(weaker_selected) == 1
            and weaker_selected[0][0].mutations[0].start_pos[0] == target_line
            and "NumberReplacer" in weaker_selected[0][0].mutations[0].operator_name
            and weaker_selected[0][1].test_outcome == MutationTestOutcome.SURVIVED,
            weaker_selected,
        )


inspection_subject = import_module("afairesi.main")


@pytest.mark.parametrize("description", [None, "package"])
def test_inspect_reports_package_descriptions_without_dependency_metadata(
    description: str | None,
) -> None:
    """Read only the returned package's description, including curried builders."""
    metadata = "" if description is None else f'meta.description = "{description}";'
    source = (
        '{ pkgs }: let dependency = { meta.description = "dependency"; }; '
        f'in pkgs.runCommand "example" {{ {metadata} }} "touch $out"'
    )
    _expect(
        inspection_subject.source_package_description(source) == description,
        source,
    )


@pytest.mark.parametrize("kind", ["python", "lean4"])
def test_formatter_discovery_only_uses_available_packages(
    tmp_path: Path,
    kind: str,
) -> None:
    """Select available declared packages, ignoring aliases and supporting sources."""
    marker = "main.py" if kind == "python" else "main.lean"
    for name in ("example", "orphan", "unavailable", "supporting"):
        directory = tmp_path / name
        directory.mkdir()
        (directory / marker).touch()
        if name != "orphan":
            _write_fixture_source(directory / "default.nix", "{}")
    nix_library = _nix_source() / "main.nix"
    selection = (
        "module.programs.mypy.directories"
        if kind == "python"
        else 'pkgs.lib.filterAttrs (name: _: builtins.match "lean4-.*" name != null) '
        "module.settings.formatter"
    )
    tool = "lint" if kind == "python" else "formatter"
    expression = f"""let
      self = {{ outPath = {json.dumps(str(tmp_path))};
        packages.test = {{
          example.afairesiKind = {json.dumps(kind)};
          default.afairesiKind = {json.dumps(kind)};
          supporting.afairesiKind = "html";
        }}; }};
      pkgs = {{ stdenv.system = "test";
        writeShellApplication = _: {{ passthru = {{}}; }};
        lib.filterAttrs = predicate: attrs: builtins.listToAttrs
          (map (name: {{ inherit name; value = attrs.${{name}}; }})
          (builtins.filter (name: predicate name attrs.${{name}})
            (builtins.attrNames attrs)));
        lib.genAttrs = names: value: builtins.listToAttrs
          (map (name: {{ inherit name; value = value name; }}) names);
        lib.genAttrs' = names: value: builtins.listToAttrs (map value names);
      }};
      inputs.treefmt-nix.lib.evalModule = _: module: {{
        config.build.wrapper = {selection};
      }};
      formatter = (import {nix_library} inputs).{tool} {{
        inherit inputs pkgs self;
      }};
      in builtins.attrNames formatter.passthru.raw"""
    result = _run(
        tmp_path,
        "eval",
        "--impure",
        "--json",
        "--expr",
        expression,
        executable="nix",
        environment=_nix_environment(tmp_path),
    )
    expected = "example" if kind == "python" else "lean4-example"
    _expect(json.loads(result.stdout) == [expected], result)


def _inspection_discovery(source: str) -> dict[str, Any]:
    return cast(
        "dict[str, Any]",
        inspection_subject.ArgumentDiscovery(source).report(),
    )


@pytest.mark.parametrize(
    "source",
    [
        "def main(): pass",
        "raise RuntimeError('do not execute')\ndef main(): pass",
        "import sys\ndef main(): return sys.version",
        """
import argparse
def main():
    argparse.ArgumentParser().parse_args()
""",
    ],
)
def test_inspect_reports_argument_free_executables(tmp_path: Path, source: str) -> None:
    """Argument-free scripts need no parser and remain statically inspectable."""
    report = _inspection_discovery(source)
    _expect(report == {"arguments": [], "commands": {}}, report)
    _inspection_repository(tmp_path)
    _write_fixture_source(tmp_path / "example/main.py", source)
    completed = _run(tmp_path, "inspect", "--repository", str(tmp_path))
    resource = json.loads(completed.stdout)["example"]
    _expect(resource["cli"] == {}, resource)
    _expect(resource["tests"] == ["test_default_output"], resource)


@pytest.mark.parametrize(
    "source",
    [
        "import argparse",
        "import click\n@click.command()\ndef main(): pass",
        "import typer\napp = typer.Typer()",
        "import sys\ndef main(): return sys.argv[1:]",
        "import sys as system\ndef main(): return system.argv[1:]",
        "from sys import argv as args\ndef main(): return args[1:]",
    ],
)
def test_inspect_requires_supported_cli(tmp_path: Path, source: str) -> None:
    """Fail inspection when a Python package's interface cannot be discovered."""
    _inspection_repository(tmp_path)
    path = tmp_path / "example/main.py"
    path.write_text(source)
    completed = _run(tmp_path, "inspect", "--repository", str(tmp_path), code=1)
    _expect(completed.stdout == "", completed)
    _expect(
        f"{path}: cannot inspect CLI: no supported parser found" in completed.stderr,
        completed,
    )


def test_inspect_reports_arguments_in_their_public_commands() -> None:
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
    _expect(list(report["commands"]) == ["build"], "lost subcommand")
    _expect(
        report["arguments"] == [{"names": ["input"], "type": "str"}],
        "wrong positional scope",
    )
    rows = {
        tuple(row["names"]): row for row in report["commands"]["build"]["arguments"]
    }
    _expect(
        rows[("-j", "--jobs")]
        == {"names": ["-j", "--jobs"], "type": "int", "default": 2, "choices": [1, 2]},
        "lost metadata",
    )
    _expect(
        rows[("--quiet",)] == {"names": ["--quiet"], "action": "store_true"},
        "lost helper context",
    )


def test_inspect_preserves_nested_and_argument_free_commands() -> None:
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
    _expect(
        report
        == {
            "arguments": [
                {"names": ["--verbose"], "action": "store_true"},
            ],
            "commands": {
                "version": {"arguments": [], "commands": {}},
                "remote": {
                    "arguments": [
                        {"names": ["--verbose"], "action": "count"},
                    ],
                    "commands": {
                        "list": {"arguments": [], "commands": {}},
                        "add": {
                            "arguments": [{"names": ["url"]}],
                            "commands": {},
                        },
                    },
                },
            },
        },
        "command tree lost scope or empty commands",
    )


def test_inspect_marks_unresolved_argument_metadata_as_unknown() -> None:
    """Keep unresolved metadata when argument names and scope are known."""
    report = _inspection_discovery("""
from argparse import ArgumentParser as Parser
cli = Parser()
cli.add_argument("--output", default=compute_default(), **options)
""")
    argument = report["arguments"][0]
    _expect(
        argument["default"] == {"unknown": "compute_default()"},
        "default was guessed",
    )
    _expect(
        argument["**kwargs"] == {"unknown": "options"},
        "unresolved kwargs were hidden",
    )


@pytest.mark.parametrize(
    "declaration",
    [
        'if enabled:\n    cli.add_argument("--token")',
        'for item in items:\n    cli.add_argument("--token")',
        'while enabled:\n    cli.add_argument("--token")',
        '[cli.add_argument("--token") for item in items]',
        (
            'def configure(target):\n    target.add_argument("--token")\n'
            "if enabled:\n    configure(cli)"
        ),
        'if enabled:\n    target = cli\ntarget.add_argument("--token")',
    ],
)
def test_inspect_rejects_runtime_dependent_structure(declaration: str) -> None:
    """Reject uncertain argument membership, including helper and alias dependencies."""
    source = "import argparse\ncli = argparse.ArgumentParser()\n" + declaration
    with pytest.raises(
        inspection_subject.CommandError,
        match="unresolved control flow",
    ):
        _inspection_discovery(source)


@pytest.mark.parametrize(
    "configure",
    [
        "configure(cli)",
        "helpers.configure(target=cli)",
        'configure({"parser": cli})',
        'settings = {"parser": cli}\nconfigure(settings)',
        'add = cli.add_argument\nadd("--token")',
        "def configure(*parsers): pass\nconfigure(cli)",
        "cli.customize()",
    ],
)
def test_inspect_rejects_argument_configuration_outside_the_inspected_source(
    configure: str,
) -> None:
    """Fail when parser flow escapes the supported helper and method patterns."""
    source = "import argparse\ncli = argparse.ArgumentParser()\n" + configure
    with pytest.raises(inspection_subject.CommandError, match="cannot inspect CLI"):
        _inspection_discovery(source)


@pytest.mark.parametrize("consumed", [False, True])
def test_inspect_rejects_ambiguous_public_interfaces(*, consumed: bool) -> None:
    """Require an unambiguous root instead of merging distinct command interfaces."""
    source = """
import argparse
one = argparse.ArgumentParser()
one.add_argument('--one')
two = argparse.ArgumentParser()
two.add_argument('--two')
"""
    if consumed:
        source += "\ndef main():\n    one.parse_args()\n    two.parse_args()\n"
    with pytest.raises(
        inspection_subject.CommandError,
        match="multiple public parsers",
    ):
        _inspection_discovery(source)


def test_inspect_reports_statically_declared_arguments_in_order() -> None:
    """Resolve literal branches and preserve the order of finite literal loops."""
    report = _inspection_discovery("""
import argparse
cli = argparse.ArgumentParser()
if True:
    cli.add_argument('--enabled')
else:
    cli.add_argument('--disabled')
for name in ('--one', '--two'):
    cli.add_argument(name, default=compute_default())
for name in []:
    cli.add_argument(name)
""")
    _expect(
        report
        == {
            "arguments": [
                {"names": ["--enabled"]},
                {"names": ["--one"], "default": {"unknown": "compute_default()"}},
                {"names": ["--two"], "default": {"unknown": "compute_default()"}},
            ],
            "commands": {},
        },
        report,
    )


def test_inspect_includes_interfaces_declared_in_the_main_guard() -> None:
    """Treat the standard main guard as active when describing an executable CLI."""
    report = _inspection_discovery("""
import argparse
if __name__ == '__main__':
    cli = argparse.ArgumentParser()
    cli.add_argument('--public')
    cli.parse_args()
""")
    _expect(
        report
        == {
            "arguments": [{"names": ["--public"]}],
            "commands": {},
        },
        report,
    )


@pytest.mark.parametrize(
    "expression",
    [
        "(process(path) for path in cli.parse_args().files)",
        "[process(path) for path in cli.parse_args().files]",
        "{process(path) for path in cli.parse_args().files}",
        "{path: process(path) for path in cli.parse_args().files}",
    ],
)
def test_inspect_recognizes_arguments_used_to_generate_collections(
    expression: str,
) -> None:
    """Recognize a parser consumed while evaluating the first comprehension iterable."""
    report = _inspection_discovery(
        """
import argparse
def main():
    cli = argparse.ArgumentParser()
    cli.add_argument('files', nargs='+')
    results = EXPRESSION
""".replace("EXPRESSION", expression),
    )
    _expect(
        report
        == {
            "arguments": [{"names": ["files"], "nargs": "+"}],
            "commands": {},
        },
        report,
    )


@pytest.mark.parametrize(
    "expression",
    [
        "(cli.parse_args() for item in items)",
        "(item for item in items if cli.parse_args())",
        "(item for item in items for path in cli.parse_args().files)",
    ],
)
def test_inspect_rejects_conditionally_used_command_interfaces(
    expression: str,
) -> None:
    """Reject parser consumption that depends on comprehension iterations or filters."""
    source = """
import argparse
def main():
    cli = argparse.ArgumentParser()
    cli.add_argument('files', nargs='+')
    results = EXPRESSION
""".replace("EXPRESSION", expression)
    with pytest.raises(
        inspection_subject.CommandError,
        match="unresolved control flow",
    ):
        _inspection_discovery(source)


@pytest.mark.parametrize(
    "loop",
    [
        "for path in cli.parse_args().files:\n        process(path)",
        "while cli.parse_args().files:\n        break",
    ],
)
def test_inspect_recognizes_arguments_used_to_control_loops(loop: str) -> None:
    """Loop iterables and initial while conditions execute before their bodies."""
    report = _inspection_discovery(
        """
import argparse
def main():
    cli = argparse.ArgumentParser()
    cli.add_argument('files', nargs='+')
    LOOP
""".replace("LOOP", loop),
    )
    _expect(
        report
        == {
            "arguments": [{"names": ["files"], "nargs": "+"}],
            "commands": {},
        },
        report,
    )


def test_inspect_reports_argument_groups_exclusivity_and_defaults() -> None:
    """Expose group membership, exclusivity, and defaults in the correct command."""
    report = _inspection_discovery("""
import argparse
cli = argparse.ArgumentParser()
run = cli.add_subparsers().add_parser('run')
options = run.add_argument_group('Credentials', 'Choose an identity')
identity = options.add_mutually_exclusive_group(required=True)
identity.add_argument('--token')
identity.add_argument('--password')
run.set_defaults(token='anonymous', password=compute_default())
""")
    command = report["commands"]["run"]
    _expect(
        command
        == {
            "arguments": [{"names": ["--token"]}, {"names": ["--password"]}],
            "commands": {},
            "groups": [
                {
                    "arguments": [["--token"], ["--password"]],
                    "exclusive": False,
                    "title": "Credentials",
                    "description": "Choose an identity",
                },
                {
                    "arguments": [["--token"], ["--password"]],
                    "exclusive": True,
                    "required": True,
                },
            ],
            "defaults": {
                "token": "anonymous",
                "password": {"unknown": "compute_default()"},
            },
        },
        command,
    )


def test_inspect_ignores_unreachable_configuration() -> None:
    """Discover the consumed interface without executing uncalled configuration."""
    report = _inspection_discovery("""
import argparse
cli = argparse.ArgumentParser()
def unused():
    external.configure(cli)
    cli.add_argument('--unused')
def main():
    cli.add_argument('--public')
    try:
        cli.parse_args()
    except ValueError:
        cli.error('invalid input')
""")
    _expect(
        report
        == {
            "arguments": [{"names": ["--public"]}],
            "commands": {},
        },
        report,
    )


@pytest.mark.parametrize(
    "constructor",
    [
        "argparse.ArgumentParser(**options)",
        "argparse.ArgumentParser().add_subparsers(**options)",
        "argparse.ArgumentParser().add_subparsers().add_parser('run', **options)",
    ],
)
def test_inspect_rejects_unresolved_interface_settings(constructor: str) -> None:
    """Unresolved parser settings may hide inherited arguments or command aliases."""
    with pytest.raises(inspection_subject.CommandError, match="literal mapping"):
        _inspection_discovery("import argparse\ncli = " + constructor)


def test_inspect_preserves_aliases_declared_in_static_settings() -> None:
    """Expand literal parser settings while retaining subcommand aliases."""
    report = _inspection_discovery("""
import argparse
cli = argparse.ArgumentParser(**{'parents': []})
run = cli.add_subparsers().add_parser('run', **{'aliases': ['r']})
run.add_argument('--token')
""")
    _expect(
        report["commands"]
        == {
            "run": {"arguments": [{"names": ["--token"]}], "commands": {}},
            "r": {"arguments": [{"names": ["--token"]}], "commands": {}},
        },
        report,
    )


def test_inspect_rejects_custom_subcommand_parsers() -> None:
    """Custom parser classes can introduce declarations outside the inspected tree."""
    with pytest.raises(
        inspection_subject.CommandError,
        match="custom subcommand parser",
    ):
        _inspection_discovery("""
import argparse
from helpers import CustomParser
cli = argparse.ArgumentParser()
cli.add_subparsers(parser_class=CustomParser).add_parser('run')
""")


@pytest.mark.parametrize(
    ("source", "reason"),
    [
        ('other.add_argument("--external")', "cannot resolve the argument parser"),
        ('other.add_parser("external")', "cannot resolve the subcommand parser"),
        ("other.parse_args()", "cannot resolve the public argparse parser"),
        (
            "cli = argparse.ArgumentParser()\ncli.add_argument(**options)",
            "argument names must be literal strings",
        ),
        (
            "cli = argparse.ArgumentParser(parents=[shared])",
            "argparse parents= inheritance is not supported",
        ),
        (
            (
                "cli = argparse.ArgumentParser()\n"
                'cli.add_subparsers().add_parser("run", parents=[shared])'
            ),
            "argparse parents= inheritance is not supported",
        ),
        (
            "cli = argparse.ArgumentParser()\ncli.add_argument(*names)",
            "argument names must be literal strings",
        ),
        (
            "argparse.ArgumentParser().add_subparsers().add_parser(name)",
            "subcommand name must be a literal string",
        ),
        (
            (
                "argparse.ArgumentParser().add_subparsers()"
                '.add_parser("list", aliases=names)'
            ),
            "subcommand aliases must be literal strings",
        ),
    ],
)
def test_inspect_rejects_unresolved_interfaces_with_source_context(
    source: str, reason: str
) -> None:
    """Fail discovery with source context instead of a partial successful report."""
    with pytest.raises(
        inspection_subject.CommandError,
        match=re.escape(reason),
    ) as error:
        inspection_subject.ArgumentDiscovery(
            "import argparse\n" + source,
            "example/main.py",
        ).report()
    _expect(
        re.search(r"example/main.py:\d+:\d+:", str(error.value)) is not None,
        "missing source location",
    )
    _expect("\n  " in str(error.value), "missing offending expression")


@pytest.mark.parametrize("constructor", ["argparse.ArgumentParser()", "Parser()"])
def test_inspect_rejects_unsupported_class_based_interfaces(
    constructor: str,
) -> None:
    """Do not report a class-based CLI as an empty successful interface."""
    source = f"""
import argparse
from argparse import ArgumentParser as Parser
class CLI:
    def run(self):
        cli = {constructor}
        cli.add_argument('--token', required=True)
        cli.parse_args()
def main():
    CLI().run()
"""
    with pytest.raises(inspection_subject.CommandError, match="class-based argparse"):
        _inspection_discovery(source)


def test_inspect_keeps_command_arguments_in_their_declared_scope() -> None:
    """Keep same-named helpers attached to their own subcommands."""
    source = """
import argparse
def configure_one(cli):
    def configure(target):
        target.add_argument('--one')
    configure(cli)
def configure_two(cli):
    def configure(target):
        target.add_argument('--two')
    configure(cli)
def parser():
    cli = argparse.ArgumentParser()
    commands = cli.add_subparsers()
    configure_one(commands.add_parser('one'))
    configure_two(commands.add_parser('two'))
    return cli
def main():
    parser().parse_args()
"""
    report = _inspection_discovery(source)
    _expect(
        report
        == {
            "arguments": [],
            "commands": {
                "one": {"arguments": [{"names": ["--one"]}], "commands": {}},
                "two": {"arguments": [{"names": ["--two"]}], "commands": {}},
            },
        },
        report,
    )


def test_inspect_rejects_ambiguous_argument_definitions() -> None:
    """Retain an explicit failure when helper definitions remain ambiguous."""
    with pytest.raises(inspection_subject.CommandError, match="ambiguous helper"):
        _inspection_discovery("""
import argparse
def configure(cli):
    cli.add_argument('--first')
def configure(cli):
    cli.add_argument('--second')
def main():
    cli = argparse.ArgumentParser()
    configure(cli)
    cli.parse_args()
""")


def test_inspect_includes_arguments_declared_by_nested_helpers() -> None:
    """Follow parser parameters read by a nested helper without global fallback."""
    report = _inspection_discovery("""
import argparse
def configure(cli):
    def add():
        cli.add_argument('--closed-over')
    add()
def main():
    cli = argparse.ArgumentParser()
    configure(cli)
    cli.parse_args()
""")
    _expect(
        report == {"arguments": [{"names": ["--closed-over"]}], "commands": {}},
        report,
    )


def test_inspect_ignores_unrelated_imports_when_resolving_the_interface() -> None:
    """Keep an unrelated factory alias from replacing the CLI constructor."""
    report = _inspection_discovery("""
def parser():
    from argparse import ArgumentParser as Factory
    return Factory()
def unrelated():
    from argparse import Namespace as Factory
    return Factory()
def main():
    cli = parser()
    cli.add_argument('--public')
    cli.parse_args()
""")
    _expect(
        report == {"arguments": [{"names": ["--public"]}], "commands": {}},
        report,
    )


@pytest.mark.parametrize(
    "source",
    [
        """
from argparse import ArgumentParser
def main(ArgumentParser):
    cli = ArgumentParser()
    cli.add_argument('--unknown')
    cli.parse_args()
""",
        """
import argparse
argparse = external_module
cli = argparse.ArgumentParser()
cli.add_argument('--unknown')
cli.parse_args()
""",
        """
from argparse import ArgumentParser
ArgumentParser = external_factory
cli = ArgumentParser()
cli.add_argument('--unknown')
cli.parse_args()
""",
    ],
)
def test_inspect_rejects_shadowed_constructor_aliases(source: str) -> None:
    """Do not treat reassigned names or parameters as their shadowed imports."""
    with pytest.raises(inspection_subject.CommandError, match="cannot resolve"):
        _inspection_discovery(source)


def test_inspect_rejects_arguments_from_replaced_definitions() -> None:
    """An opaque callable must not inherit the interface of a replaced helper."""
    with pytest.raises(inspection_subject.CommandError, match="external helper"):
        _inspection_discovery("""
import argparse
def configure(cli):
    cli.add_argument('--hidden')
configure = external_helper
def main():
    cli = argparse.ArgumentParser()
    configure(cli)
    cli.parse_args()
""")


@pytest.mark.parametrize("module", ["argparse", "click"])
def test_inspect_rejects_unresolved_cli_star_imports(module: str) -> None:
    """Keep unresolved wildcard interfaces from becoming an empty successful CLI."""
    with pytest.raises(inspection_subject.CommandError, match="no supported parser"):
        _inspection_discovery(f"from {module} import *\ndef main(): pass\n")


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_inspect_preserves_unicode_arguments_across_line_endings(
    newline: str,
) -> None:
    """Match AST byte columns to LibCST names across Unicode and line endings."""
    source = """import argparse
def main():
    label = 'é\u2028'; cli = argparse.ArgumentParser()
    cli.add_argument('--résumé', default=label)
    cli.parse_args()
""".replace("\n", newline)
    report = _inspection_discovery(source)
    _expect(
        report
        == {
            "arguments": [
                {"names": ["--résumé"], "default": {"unknown": "label"}},
            ],
            "commands": {},
        },
        report,
    )


def test_inspect_preserves_declared_source_encoding() -> None:
    """Decode a source encoding cookie before matching metadata byte offsets."""
    source = """# coding: latin-1
import argparse
label = 'é'; cli = argparse.ArgumentParser()
cli.add_argument('--résumé', default='é')
""".encode("latin-1")
    report = inspection_subject.ArgumentDiscovery(source, "encoded.py").report()
    _expect(
        report
        == {
            "arguments": [{"names": ["--résumé"], "default": "é"}],
            "commands": {},
        },
        report,
    )


def test_inspect_reports_the_same_interface_for_command_aliases() -> None:
    """Expose aliases with the same interface as their canonical command."""
    report = _inspection_discovery("""
import argparse
cli = argparse.ArgumentParser()
commands = cli.add_subparsers()
listing = commands.add_parser(name='list', aliases=['ls'])
listing.add_argument('--all', action='store_true')
listing.add_subparsers().add_parser('files', aliases=['f'])
""")
    _expect(set(report["commands"]) == {"list", "ls"}, "missing alias")
    listing = report["commands"]["list"]
    _expect(listing == report["commands"]["ls"], "alias interface differs")
    _expect(
        listing["arguments"]
        == [
            {"names": ["--all"], "action": "store_true"},
        ],
        "lost alias arguments",
    )
    _expect(
        set(listing["commands"]) == {"files", "f"},
        "missing nested alias",
    )


@pytest.mark.parametrize(
    "method",
    ["parse_intermixed_args", "parse_known_intermixed_args"],
)
def test_inspect_excludes_internal_interfaces_when_arguments_are_intermixed(
    method: str,
) -> None:
    """Identify public roots for both intermixed argparse entry points."""
    report = _inspection_discovery(f"""
import argparse
def internal():
    cli = argparse.ArgumentParser()
    cli.add_argument('--private')
    return cli
def parser():
    cli = argparse.ArgumentParser()
    cli.add_argument('--public')
    return cli
def main():
    parser().{method}()
""")
    _expect(
        [row["names"] for row in report["arguments"]] == [["--public"]],
        "internal arguments leaked into the public interface",
    )


def test_inspect_preserves_required_arguments_declared_by_helpers() -> None:
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
    _expect(
        report["arguments"] == [{"names": ["--required"], "required": True}],
        "factory or keyword binding was lost",
    )


@pytest.mark.parametrize("entrypoint", ["parser().parse_args()", "parse(parser())"])
def test_inspect_excludes_internal_commands_and_arguments(
    entrypoint: str,
) -> None:
    """Select the parser consumed by main while retaining its configured helpers."""
    source = """
import argparse
class Internal:
    def parser(self):
        cli = argparse.ArgumentParser()
        cli.add_argument("--class-internal")
        return cli
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
    _expect(
        [row["names"] for row in report["arguments"]] == [["--shared"], ["--public"]],
        "internal parser arguments leaked into the public CLI",
    )
    _expect(list(report["commands"]) == ["repo"], "internal command leaked")


def test_inspect_reports_sorted_test_names_with_class_context(
    tmp_path: Path,
) -> None:
    """Retain sorted test identifiers and class context."""
    _inspection_repository(tmp_path)
    _write_fixture_source(
        tmp_path / "example/test_main.py",
        """
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
""",
    )
    rows = inspection_subject.inspect_repository(tmp_path)["example"]["tests"]
    _expect(
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
    package = root / "example"
    package.mkdir(parents=True)
    _write_fixture_source(package / "default.nix", '{ meta.description = "Example"; }')
    _write_fixture_source(
        package / "main.py",
        """
import argparse
raise RuntimeError("inspection must not execute this")
def parser():
    cli = argparse.ArgumentParser()
    cli.add_argument("--output", default="result")
    return cli
""",
    )
    _write_fixture_source(
        package / "test_main.py",
        """
raise RuntimeError("inspection must not collect or run tests")
def test_default_output(): pass
""",
    )
    host = root / "laptop"
    host.mkdir(parents=True)
    (host / "configuration.nix").touch()
    library = root / "library"
    library.mkdir()
    _write_fixture_source(library / "default.nix", "{}")


@pytest.mark.parametrize("directory", ["prm", "tmp", "experiments"])
def test_resource_discovery_requires_tracking(tmp_path: Path, directory: str) -> None:
    """Ignore local experiments and recognize any explicitly selected package name."""
    _repository(tmp_path)
    _fixture_add(tmp_path, "example", "python")
    before = inspection_subject.inspect_repository(tmp_path)
    storage = tmp_path / directory
    storage.mkdir()
    (storage / "default.nix").write_text("{}")
    (storage / "main.py").write_text("def main(): pass\n")
    _expect(
        inspection_subject.inspect_repository(tmp_path) == before,
        "local experiment entered inventory",
    )
    _git(tmp_path, "add", "--force", "--", directory)
    _expect(
        directory in inspection_subject.inspect_repository(tmp_path),
        "tracked package was excluded by name",
    )


def test_inspect_is_read_only(tmp_path: Path) -> None:
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
    _expect(before == after, "inspection wrote to the repository")
    _expect(list(report) == ["example", "laptop", "library"], report)
    _expect(report["example"]["type"] == "python", report)
    _expect(
        set(report["example"]) == {"type", "description", "cli", "tests"},
        report,
    )
    _expect("packages" not in report and "hosts" not in report, report)
    _expect(
        report["laptop"] == {"type": "host"},
        "wrong hosts",
    )
    _expect(
        report["library"] == {"type": "nix"},
        "empty optional fields leaked into the package inventory",
    )
    _expect(report["example"]["tests"] == ["test_default_output"], report)
    _expect(report["example"]["description"] == "Example", report)


@pytest.mark.parametrize("has_parser", [False, True])
def test_inspect_reports_library_metadata_and_tests_without_a_cli(
    tmp_path: Path, *, has_parser: bool
) -> None:
    """Retain metadata and tests for a declared non-executable Python library."""
    _inspection_repository(tmp_path)
    package = tmp_path / "example"
    _write_fixture_source(
        package / "default.nix",
        """
{ inputs, pkgs, ... }:
let python = pkgs.python3;
in inputs.self.lib.mkPythonPackage {
    inherit pkgs;
    executable = false;
    meta.description = "Python library";
    src = ./.;
}
""",
    )
    if not has_parser:
        _write_fixture_source(
            package / "main.py",
            "raise RuntimeError('do not import')\n",
        )
    report = inspection_subject.inspect_repository(tmp_path)["example"]
    _expect(
        report
        == {
            "type": "python",
            "description": "Python library",
            "tests": ["test_default_output"],
        },
        report,
    )


def test_inspect_uses_package_executable_settings_without_dependency_metadata(
    tmp_path: Path,
) -> None:
    """Read executable metadata from the outer package rather than dependencies."""
    _inspection_repository(tmp_path)
    _write_fixture_source(
        tmp_path / "example/default.nix",
        """
{ inputs, pkgs, ... }:
let dependency = { executable = false; };
in inputs.self.lib.mkPythonPackage {
    inherit pkgs;
    executable = true;
    src = ./.;
}
""",
    )
    report = inspection_subject.inspect_repository(tmp_path)["example"]
    _expect(
        report["cli"]
        == {
            "arguments": [{"names": ["--output"], "default": "result"}],
        },
        report,
    )


def test_inspect_cli_failure_has_context_and_no_partial_json(
    tmp_path: Path,
) -> None:
    """Surface discovery errors on stderr with a failing status and no JSON output."""
    _inspection_repository(tmp_path)
    source = tmp_path / "example/main.py"
    _write_fixture_source(source, "import argparse\nother.add_argument('--missing')\n")
    completed = _run(tmp_path, "inspect", "--repository", str(tmp_path), code=1)
    _expect(completed.stdout == "", completed)
    _expect(f"{source}:2:1: cannot inspect CLI:" in completed.stderr, completed)
    _expect("cannot resolve the argument parser" in completed.stderr, completed)
    _expect("other.add_argument('--missing')" in completed.stderr, completed)
    _expect("Traceback" not in completed.stderr, completed)


def test_inspect_preserves_package_and_host_in_one_directory(
    tmp_path: Path,
) -> None:
    """Retain both resource roles when their marker files share a directory."""
    _inspection_repository(tmp_path)
    (tmp_path / "example/configuration.nix").touch()
    resource = inspection_subject.inspect_repository(tmp_path)["example"]
    _expect(resource["type"] == ["python", "host"], resource)


def test_inspect_compacts_commands_without_losing_declared_values(
    tmp_path: Path,
) -> None:
    """Keep empty commands, aliases, and literal defaults when pruning containers."""
    _inspection_repository(tmp_path)
    _write_fixture_source(
        tmp_path / "example/main.py",
        """
import argparse
cli = argparse.ArgumentParser()
commands = cli.add_subparsers()
commands.add_parser('empty', aliases=['e'])
run = commands.add_parser('run')
run.add_argument('--values', default=[])
run.add_argument('--mapping', default={})
run.add_argument('--optional', default=None)
run.add_argument('--enabled', default=False)
run.add_argument('--count', default=0)
run.add_argument('--name', default='')
run.set_defaults(internal=None)
""",
    )
    report = inspection_subject.inspect_repository(tmp_path)["example"]["cli"]
    _expect(
        report
        == {
            "commands": {
                "empty": {},
                "e": {},
                "run": {
                    "arguments": [
                        {"names": ["--values"], "default": []},
                        {"names": ["--mapping"], "default": {}},
                        {"names": ["--optional"], "default": None},
                        {"names": ["--enabled"], "default": False},
                        {"names": ["--count"], "default": 0},
                        {"names": ["--name"], "default": ""},
                    ],
                    "defaults": {"internal": None},
                },
            },
        },
        report,
    )


def test_inspect_omits_docstrings_and_test_implementation(tmp_path: Path) -> None:
    """Return stable identifiers without test bodies or source docstrings."""
    _inspection_repository(tmp_path)
    source = tmp_path / "example/main.py"
    _write_fixture_source(source, '"""Source description."""\n' + source.read_text())
    tests = tmp_path / "example/test_main.py"
    template = (
        "def test_default_output():\n"
        '    """DOCUMENTATION"""\n'
        '    assert render() == "EXPECTED"\n'
    )
    _write_fixture_source(tests, template)
    before = inspection_subject.inspect_repository(tmp_path)
    _write_fixture_source(
        tests,
        template.replace("DOCUMENTATION", "Changed").replace("EXPECTED", "second"),
    )
    after = inspection_subject.inspect_repository(tmp_path)
    _expect(before == after, "test implementation or docstrings leaked into inventory")
    _expect(before["example"]["description"] == "Example", before)
    _expect(before["example"]["tests"] == ["test_default_output"], before)


@pytest.mark.parametrize("location", ["outside", "root", "nested"])
def test_inspect_reports_the_same_repository_through_equivalent_paths(
    tmp_path: Path,
    location: str,
) -> None:
    """Return the same JSON inventory through supported paths inside and outside Git."""
    _inspection_repository(tmp_path)
    expected = inspection_subject.inspect_repository(tmp_path)
    _expect(
        "test_default_output" in expected["example"]["tests"],
        "inventory lost test identifier",
    )
    if location != "outside":
        _git(tmp_path, "init", "--quiet")
        _git(tmp_path, "add", ".")
    cwd = tmp_path / "example" if location == "nested" else tmp_path
    paths = [["--repository", "."], ["--repository", str(tmp_path)]]
    if location != "outside":
        paths.append([])
    for arguments in paths:
        completed = subprocess.run(  # noqa: S603 - Test harness selects the executable and fixture arguments.
            [os.environ["PACKAGE_E2E_EXECUTABLE"], "inspect", *arguments],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        _expect(
            json.loads(completed.stdout) == expected,
            "path resolution changed the CLI inventory",
        )


def test_inspect_rejects_invalid_repositories_and_source_with_context(
    tmp_path: Path,
) -> None:
    """Report invalid repositories and malformed source with useful errors."""
    with pytest.raises(inspection_subject.CommandError, match=r"flake\.nix"):
        inspection_subject.inspect_repository(tmp_path)
    _inspection_repository(tmp_path)
    _write_fixture_source(tmp_path / "example" / "main.py", "def invalid(")
    with pytest.raises(
        inspection_subject.CommandError,
        match="could not parse Python source",
    ):
        inspection_subject.inspect_repository(tmp_path)


def test_inspect_reports_no_resources_for_an_empty_repository(
    tmp_path: Path,
) -> None:
    """Return an empty resource map for an empty repository."""
    (tmp_path / "flake.nix").touch()
    _expect(
        inspection_subject.inspect_repository(tmp_path) == {},
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
    completed = subprocess.run(  # noqa: S603 - Test harness selects the executable and fixture arguments.
        [
            os.environ["PACKAGE_E2E_EXECUTABLE"],
            "inspect",
            "--repository",
            target,
        ],
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
    (source / "new").mkdir(parents=True)
    (source / "new/configuration.nix").touch()
    inspection_subject.git(source, ["add", "."])
    inspection_subject.git(source, ["commit", "--quiet", "-m", "add host"])


def test_inspect_remote_is_fresh_and_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inspect fresh remote contents without creating persistent home state."""
    home, source, remote = _inspection_remote_fixture(tmp_path, monkeypatch)
    before = _inspection_tree_snapshot(home)
    _expect(
        _inspection_inspect_target(remote)
        == inspection_subject.inspect_repository(source),
        "wrong remote inventory",
    )
    _inspection_advance_remote(source)
    _expect(
        _inspection_inspect_target(remote)
        == inspection_subject.inspect_repository(source),
        "remote inspection was stale",
    )
    _expect(
        _inspection_tree_snapshot(home) == before,
        "remote inspection changed HOME",
    )


@pytest.mark.parametrize("malformed", [False, True])
def test_inspect_remote_cleans_temporary_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    malformed: bool,
) -> None:
    """Remove temporary clones after successful inspection and parser errors."""
    home, source, remote = _inspection_remote_fixture(tmp_path, monkeypatch)
    if malformed:
        _write_fixture_source(source / "example/main.py", "def invalid(")
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
            _expect(
                inspection_subject.inspect_repository(checkout)
                == inspection_subject.inspect_repository(source),
                "wrong temporary checkout contents",
            )
    _expect(not checkout.exists(), "temporary checkout survived inspection")
    _expect(not list(home.iterdir()), "temporary inspection changed HOME")


def test_inspect_includes_uncommitted_changes_only_for_local_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep local dirty content visible only when explicitly inspecting its path."""
    _home, _source, remote = _inspection_remote_fixture(tmp_path, monkeypatch)
    checkout = tmp_path / "checkout"
    _git(tmp_path, "clone", "--", remote, str(checkout))
    module = checkout / "example/main.py"
    module.write_text(module.read_text().replace('default="result"', 'default="local"'))
    before = _inspection_tree_snapshot(tmp_path)
    local_report = _inspection_inspect_target(str(checkout))
    _expect(
        local_report["example"]["cli"]["arguments"][0]["default"] == "local",
        local_report,
    )
    remote_report = _inspection_inspect_target(remote)
    _expect(
        remote_report["example"]["cli"]["arguments"][0]["default"] == "result",
        remote_report,
    )
    _expect(
        _inspection_tree_snapshot(tmp_path) == before,
        "inspection changed source or Git state",
    )


def test_inspect_remote_accepts_native_file_urls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Let Git handle transports without imposing hosted repository conventions."""
    home, source, _remote = _inspection_remote_fixture(tmp_path, monkeypatch)
    _expect(
        _inspection_inspect_target(source.as_uri())
        == inspection_subject.inspect_repository(source),
        "native file URL was rejected",
    )
    _expect(not list(home.iterdir()), "file URL inspection changed HOME")


@pytest.mark.parametrize("remote_kind", ["missing", "unsupported", "invalid"])
def test_inspect_remote_preserves_native_clone_errors(
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
    expected = subprocess.run(  # noqa: S603 - Fixed Git executable with test-controlled arguments.
        ["git", "clone", "--", remote, str(native_checkout)],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    actual = subprocess.run(  # noqa: S603 - Test harness selects the executable and fixture arguments.
        [
            os.environ["PACKAGE_E2E_EXECUTABLE"],
            "inspect",
            "--repository",
            remote,
        ],
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
    _expect(
        (actual.returncode, actual.stdout, actual_error)
        == (
            expected.returncode,
            expected.stdout,
            expected.stderr.replace(str(native_checkout), "<CHECKOUT>"),
        ),
        f"native clone error changed: {expected.stderr!r}, {actual.stderr!r}",
    )
    _expect(not list(scratch.iterdir()), "failed clone left temporary files")
    _expect(not list(home.iterdir()), "failed clone changed HOME")


def _inspection_diff_repository(root: Path) -> dict[str, Any]:
    """Commit inspectable sources, including binary data and archive exclusions."""
    _inspection_repository(root)
    (root / "asset").write_bytes(b"\x00\xff\n")
    (root / ".gitattributes").write_text("example export-ignore\n")
    _git(root, "init", "--quiet")
    _git(root, "add", ".")
    _fixture_git(root, "commit", "--quiet", "-m", "initial")
    return cast("dict[str, Any]", inspection_subject.inspect_repository(root))


def _inspection_diff_description(root: Path, description: str) -> None:
    """Change a public field without changing resource identity."""
    _write_fixture_source(
        root / "example/default.nix",
        '{ meta.description = "' + description + '"; }',
    )


def _inspection_apply_diff(
    before: dict[str, Any],
    difference: str,
    *,
    diff_format: str = "jd",
) -> dict[str, Any]:
    """Use jd itself to verify that an emitted patch reconstructs the inspection."""
    with TemporaryDirectory(prefix="afairesi-jd-patch-") as directory:
        patch = Path(directory) / "change.jd"
        patch.write_text(difference)
        completed = subprocess.run(  # noqa: S603 - Fixed jd executable; patch is a temporary fixture file.
            ["jd", "-f", diff_format, "-p", str(patch)],  # noqa: S607
            input=json.dumps(before),
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    return cast("dict[str, Any]", json.loads(completed.stdout))


@pytest.mark.parametrize(
    ("arguments", "left", "right", "code"),
    [
        ((), "index", "working", 0),
        (("--cached",), "head", "index", 0),
        (("--staged",), "head", "index", 0),
        (("HEAD",), "head", "working", 0),
        (("--cached", "initial"), "initial", "index", 0),
        (("initial", "HEAD"), "initial", "head", 0),
        (("initial..HEAD",), "initial", "head", 0),
        (("initial...HEAD",), "initial", "head", 0),
        (("--exit-code",), "index", "working", 1),
        (("-R",), "working", "index", 0),
    ],
)
def test_inspect_diff_uses_git_comparisons_and_emits_jd_patches(
    tmp_path: Path,
    arguments: tuple[str, ...],
    left: str,
    right: str,
    code: int,
) -> None:
    """Keep staged, working, and historical inventories distinct and read-only."""
    reports = {"initial": _inspection_diff_repository(tmp_path)}
    _git(tmp_path, "tag", "initial")
    _inspection_diff_description(tmp_path, "Committed")
    _git(tmp_path, "add", ".")
    _fixture_git(tmp_path, "commit", "--quiet", "-m", "change description")
    reports["head"] = inspection_subject.inspect_repository(tmp_path)
    _inspection_diff_description(tmp_path, "Staged")
    _git(tmp_path, "add", ".")
    reports["index"] = inspection_subject.inspect_repository(tmp_path)
    _inspection_diff_description(tmp_path, "Working")
    reports["working"] = inspection_subject.inspect_repository(tmp_path)
    before = _inspection_tree_snapshot(tmp_path)
    completed = _run(tmp_path / "example", "inspect", "diff", *arguments, code=code)
    _expect(
        _inspection_apply_diff(reports[left], completed.stdout) == reports[right],
        completed,
    )
    _expect(not completed.stderr, completed)
    _expect(
        _inspection_tree_snapshot(tmp_path) == before,
        "diff changed Git or sources",
    )


def test_inspect_diff_ignores_implementation_edits_and_untracked_resources(
    tmp_path: Path,
) -> None:
    """Emit nothing for changes outside the tracked interface contract."""
    _inspection_diff_repository(tmp_path)
    source = tmp_path / "example/main.py"
    _write_fixture_source(source, source.read_text() + "\ndef internal(): return 42\n")
    tests = tmp_path / "example/test_main.py"
    _write_fixture_source(
        tests,
        tests.read_text().replace("pass", "raise RuntimeError('changed')"),
    )
    (tmp_path / "untracked").mkdir()
    (tmp_path / "untracked/configuration.nix").touch()
    before = _inspection_tree_snapshot(tmp_path)
    completed = _run(tmp_path, "inspect", "diff", "--exit-code")
    _expect(not completed.stdout and not completed.stderr, completed)
    _expect(_inspection_tree_snapshot(tmp_path) == before, "empty diff changed state")


def test_inspect_diff_covers_structural_and_nested_cli_changes(
    tmp_path: Path,
) -> None:
    """Round-trip resource changes, nested arguments, and lists with jd context."""
    before = _inspection_diff_repository(tmp_path)
    source = tmp_path / "example/main.py"
    _write_fixture_source(
        source,
        source.read_text().replace(
            "    return cli",
            "    cli.add_argument('--verbose', action='store_true')\n"
            "    command = cli.add_subparsers().add_parser('run')\n"
            "    command.add_argument('paths', nargs='+')\n"
            "    return cli",
        ),
    )
    _write_fixture_source(tmp_path / "example/test_main.py", "def test_new(): pass\n")
    (tmp_path / "laptop/configuration.nix").unlink()
    (tmp_path / "new-host").mkdir()
    (tmp_path / "new-host/configuration.nix").touch()
    _git(tmp_path, "add", ".")
    after = inspection_subject.inspect_repository(tmp_path)
    completed = _run(tmp_path, "inspect", "diff", "--cached")
    _expect(_inspection_apply_diff(before, completed.stdout) == after, completed)


def test_inspect_diff_pipes_through_jd_translation(tmp_path: Path) -> None:
    """Translate native diff stdin to JSON Patch for JSON viewers and patching."""
    before = _inspection_diff_repository(tmp_path)
    _inspection_diff_description(tmp_path, "Changed")
    difference = _run(tmp_path, "inspect", "diff").stdout
    translated = subprocess.run(
        ["jd", "-t", "jd2patch"],  # noqa: S607
        input=difference,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    _expect(isinstance(json.loads(translated.stdout), list), translated)
    _expect(
        _inspection_apply_diff(before, translated.stdout, diff_format="patch")
        == inspection_subject.inspect_repository(tmp_path),
        translated,
    )
    _expect("afairesi-inspect-diff-" not in difference, difference)
    _expect(
        _run(tmp_path, "inspect", "diff").stdout == difference,
        "temporary snapshot paths made identical diffs unstable",
    )


def test_inspect_diff_three_dot_uses_the_merge_base(tmp_path: Path) -> None:
    """Exclude the other branch's changes and ignore malformed working sources."""
    base = _inspection_diff_repository(tmp_path)
    _git(tmp_path, "tag", "base")
    _git(tmp_path, "checkout", "--quiet", "-b", "topic")
    _inspection_diff_description(tmp_path, "Topic")
    _git(tmp_path, "add", ".")
    _fixture_git(tmp_path, "commit", "--quiet", "-m", "topic interface")
    topic = inspection_subject.inspect_repository(tmp_path)
    _git(tmp_path, "checkout", "--quiet", "-b", "other", "base")
    (tmp_path / "other-host").mkdir()
    (tmp_path / "other-host/configuration.nix").touch()
    _git(tmp_path, "add", ".")
    _fixture_git(tmp_path, "commit", "--quiet", "-m", "other interface")
    other = inspection_subject.inspect_repository(tmp_path)
    _write_fixture_source(tmp_path / "example/main.py", "def invalid(")
    before = _inspection_tree_snapshot(tmp_path)
    for comparison, report in (("other...topic", base), ("other..topic", other)):
        completed = _run(tmp_path, "inspect", "diff", comparison)
        _expect(_inspection_apply_diff(report, completed.stdout) == topic, completed)
    _expect(_inspection_tree_snapshot(tmp_path) == before, "range diff changed state")


def test_inspect_diff_compares_initial_staging_against_an_empty_repository(
    tmp_path: Path,
) -> None:
    """Compare the first staged interface against an empty inventory."""
    _git(tmp_path, "init", "--quiet")
    _expect(not _run(tmp_path, "inspect", "diff", "--cached").stdout, "nonempty diff")
    _inspection_repository(tmp_path)
    _git(tmp_path, "add", ".")
    staged = inspection_subject.inspect_repository(tmp_path)
    _inspection_diff_description(tmp_path, "Unstaged")
    before = _inspection_tree_snapshot(tmp_path)
    completed = _run(tmp_path, "inspect", "diff", "--cached", "--exit-code", code=1)
    _expect(_inspection_apply_diff({}, completed.stdout) == staged, completed)
    _expect(_inspection_tree_snapshot(tmp_path) == before, "initial diff changed state")


@pytest.mark.parametrize(
    ("arguments", "failure", "code"),
    [
        (("missing-revision",), "Needed a single revision", 128),
        (("HEAD", "HEAD", "HEAD"), "at most two revisions", 2),
        (("--cached", "HEAD", "HEAD"), "at most one revision", 2),
        (("--cached", "HEAD..HEAD"), "not a range", 2),
        ((), "could not parse Python source", 1),
    ],
)
def test_inspect_diff_errors_leave_no_partial_output_or_temporary_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: tuple[str, ...],
    failure: str,
    code: int,
) -> None:
    """Reject invalid comparisons atomically and clean failed snapshots."""
    root = tmp_path / "repository"
    root.mkdir()
    _inspection_diff_repository(root)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setenv("TMPDIR", str(scratch))
    if not arguments:
        _write_fixture_source(root / "example/main.py", "def invalid(")
    before = _inspection_tree_snapshot(root)
    completed = _run(root, "inspect", "diff", *arguments, code=code)
    _expect(not completed.stdout, completed)
    _expect(failure in completed.stderr, completed)
    _expect(not list(scratch.iterdir()), "failed diff left temporary sources")
    _expect(_inspection_tree_snapshot(root) == before, "failed diff changed state")


def test_inspect_diff_supports_help_and_repository_selection_outside_git(
    tmp_path: Path,
) -> None:
    """Describe diffs outside Git and distinguish repository paths from commands."""
    help_output = _run(tmp_path, "inspect", "diff", "--help").stdout
    _expect("--cached" in help_output and "jd-compatible" in help_output, help_output)
    _expect("--color | less -R" in help_output, help_output)
    _expect("| less" in help_output, help_output)
    _expect("| jd -t jd2patch | jless" in help_output, help_output)
    _expect(not list(tmp_path.iterdir()), "help wrote files")
    root = tmp_path / "diff"
    root.mkdir()
    report = _inspection_diff_repository(root)
    completed = _run(tmp_path, "inspect", "--repository", "diff")
    _expect(json.loads(completed.stdout) == report, completed)
    _inspection_diff_description(root, "Changed")
    completed = _run(tmp_path, "inspect", "--repository", str(root), "diff")
    _expect(
        _inspection_apply_diff(report, completed.stdout)
        == inspection_subject.inspect_repository(root),
        completed,
    )


def test_inspect_diff_rejects_unmerged_index_without_changes(
    tmp_path: Path,
) -> None:
    """Reject conflict stages instead of emitting an incomplete interface diff."""
    _inspection_diff_repository(tmp_path)
    blob = _git(tmp_path, "rev-parse", "HEAD:example/main.py").strip()
    subprocess.run(
        ["git", "update-index", "--index-info"],  # noqa: S607
        cwd=tmp_path,
        input=(
            "0 "
            + "0" * len(blob)
            + "\texample/main.py\n"
            + f"100644 {blob} 1\texample/main.py\n"
            + f"100644 {blob} 2\texample/main.py\n"
            + f"100644 {blob} 3\texample/main.py\n"
        ),
        text=True,
        check=True,
        timeout=10,
    )
    before = _inspection_tree_snapshot(tmp_path)
    completed = _run(tmp_path, "inspect", "diff", code=1)
    _expect(not completed.stdout and "unmerged index" in completed.stderr, completed)
    _expect(
        _inspection_tree_snapshot(tmp_path) == before,
        "conflict diff changed state",
    )


def test_inspect_diff_preserves_linked_worktrees(tmp_path: Path) -> None:
    """Read the selected worktree's index while preserving shared Git metadata."""
    root, linked = tmp_path / "repository", tmp_path / "linked"
    root.mkdir()
    report = _inspection_diff_repository(root)
    _git(root, "worktree", "add", "--quiet", "--detach", str(linked), "HEAD")
    _inspection_diff_description(linked, "Linked")
    before = _inspection_tree_snapshot(tmp_path)
    completed = _run(linked / "example", "inspect", "diff")
    _expect(
        _inspection_apply_diff(report, completed.stdout)
        == inspection_subject.inspect_repository(linked),
        completed,
    )
    _expect(
        _inspection_tree_snapshot(tmp_path) == before,
        "diff changed worktree state",
    )


def test_inspect_reports_public_commands_and_test_campaigns() -> None:
    """Inventory the flat public CLI and its nested test campaigns."""
    subject = import_module("afairesi.main")
    report = subject.ArgumentDiscovery(
        Path(__file__).with_name("main.py").read_bytes(),
    ).report()
    commands = report["commands"]
    _expect(set(commands) == {"inspect", "test"}, commands)
    _expect(set(commands["inspect"]["commands"]) == {"diff"}, commands["inspect"])
    campaigns = commands["test"]["commands"]
    _expect(
        list(campaigns) == ["coverage", "fuzz", "mutation", "performance", "property"],
        campaigns,
    )
    for name, campaign in campaigns.items():
        arguments = [row["names"] for row in campaign["arguments"]]
        _expect(arguments.count(["target"]) == 1, campaign)
        for option in ("--timeout", "-k", "-m"):
            _expect(arguments.count([option]) == (name != "coverage"), campaign)
    _expect(not report["arguments"], report["arguments"])


@pytest.mark.parametrize("kind", ["python", "nix", "lean4"])
def test_source_validation_ignores_local_packages_and_optional_tests(
    tmp_path: Path,
    kind: str,
) -> None:
    """Keep unselected sources out of validation, inspection, and test workspaces."""
    root = tmp_path / "project"
    root.mkdir()
    _repository(root)
    _fixture_add(root, "example", kind)
    extension = {"python": "py", "nix": "nix", "lean4": "lean"}[kind]
    tests = f"example/test_main.{extension}"
    (root / tests).write_bytes(b"def broken(:")
    local = root / "experiments"
    local.mkdir()
    (local / "main.py").write_bytes(b"def broken(:")
    _check_flake_source(root)
    before = _snapshot(root)
    _check_flake_source(root)
    _expect(_snapshot(root) == before, "validation admitted or changed local sources")
    subject = import_module("afairesi.main")
    report = subject.inspect_repository(root)
    _expect(set(report) == {"example"} and "tests" not in report["example"], report)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subject._copy_test_sources(root, workspace)  # noqa: SLF001
    _expect(not (workspace / "experiments").exists(), "copied a local package")
    _expect(not (workspace / tests).exists(), "copied local tests")


def test_campaign_includes_tracked_assets_and_unstaged_edits(
    tmp_path: Path,
) -> None:
    """Use Git membership and working contents without assigning meaning to names."""
    root = tmp_path / "project"
    root.mkdir()
    _repository(root)
    _fixture_add(root, "example", "python")
    assets = ["assets/input.bin", "example/data/value.bin", "prm/file", "tmp/file"]
    for relative in assets:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"staged")
        _git(root, "add", "--force", "--", relative)
        path.write_bytes(b"working")
    (root / "example/local.bin").write_bytes(b"local")
    before = _snapshot(root)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subject = import_module("afairesi.main")
    subject._copy_test_sources(root, workspace)  # noqa: SLF001
    for relative in assets:
        _expect((workspace / relative).read_bytes() == b"working", relative)
    _expect(not (workspace / "example/local.bin").exists(), "local asset was copied")
    _expect(_snapshot(root) == before, "copy changed repository state")


@pytest.mark.parametrize("command", ["property", "fuzz"])
def test_campaign_artifacts_do_not_replace_selected_sources(
    tmp_path: Path,
    command: str,
) -> None:
    """Keep runner files separate from arbitrary tracked project assets."""
    root = tmp_path / "consumer"
    assets = (
        "bin/example",
        ".afairesi-python/marker",
        "run-tests.py",
        "fuzz_support.py",
        "package-entry.py",
        "tests.log",
        "environment.log",
        "active-test-pgid",
        "profiles",
        "hypothesis",
        "source/value",
    )
    tests = (
        "from pathlib import Path\n"
        "from hypothesis import given, strategies as st\n"
        "@given(st.integers())\n"
        "def test_fuzz_assets(value):\n"
        f"    for relative in {assets!r}:\n"
        "        assert Path(relative).read_text() == 'selected'\n"
    )
    environment = _campaign_prepare_runner_flake(root, tests)
    for relative in assets:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("selected")
    _git(root, "add", "--", *assets)
    before = _snapshot(root)
    result = _run_campaign(root, environment, command)
    _expect(result.returncode == 0 and "1 passed" in result.stdout, result)
    _expect(_snapshot(root) == before, "campaign changed project sources")

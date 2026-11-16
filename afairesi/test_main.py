# Copyright (c) 2026- Paschalis Bizopoulos
"""Check Afairesi's public contracts with explicit regressions and generated cases."""

from __future__ import annotations

import ast
import json
import os
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

import coverage
import pytest

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


@pytest.mark.parametrize("location", ["outside", "root", "nested"])
def test_cli_help_lists_current_commands_and_rejects_invalid_requests(
    location: str,
) -> None:
    """Expose consistent commands and reject invalid requests before creating state."""
    with TemporaryDirectory(prefix="afairesi-cli-") as directory:
        root = Path(directory)
        if location != "outside":
            _repository(root)
        cwd = root
        if location == "nested":
            cwd = root / "prm/nested"
            cwd.mkdir(parents=True)
        before = _inspection_tree_snapshot(root)
        help_output = _run(cwd, "--help").stdout
        _expect("usage:" in help_output, help_output)
        command_names = [
            "inspect",
            "inspect-diff",
        ]
        for command in command_names:
            option = _run(cwd, command, "--help").stdout
            _expect(
                f"usage: afairesi {command}" in option,
                command,
            )
        environment = dict(os.environ)
        environment["PATH"] = (
            str(Path(os.environ["PACKAGE_E2E_EXECUTABLE"]).parent)
            + os.pathsep
            + environment["PATH"]
        )
        _expect(
            _run(cwd, "--help", executable="afairesi", environment=environment).stdout
            == help_output,
            "standalone command unavailable through PATH",
        )
        rejected = _run(cwd, "unknown-command", code=2)
        _expect("invalid choice" in rejected.stderr, rejected)
        positions = [help_output.index(f"    {name} ") for name in command_names]
        _expect(
            positions == sorted(positions),
            "help commands must be alphabetical",
        )
        _expect(_run(cwd).stdout == help_output, "bare afairesi must show help")
        _expect(_inspection_tree_snapshot(root) == before, "CLI help changed state")


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


def _prepare_coverage_flake(
    root: Path,
) -> dict[str, str]:
    """Build coverage-producing checks with offline Nix inputs."""
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
        "def unused():\n"
        "    return 'uncovered'\n"
    )
    tests = (
        "import os, subprocess, pytest\n"
        "from z_last import main as subject\n"
        "from hypothesis import given, example, settings, strategies as st\n"
        "@given(st.just(1))\n"
        "@example(0)\n"
        "def test_explicit(value):\n"
        "    assert value in {0, 1}\n"
        "@given(st.just(1))\n"
        "def test_generated_only(value):\n"
        "    assert value == 1\n"
        "    assert settings.default.max_examples == 100\n"
        "    assert settings.default.derandomize\n"
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
    _check_flake_source(root)
    _write_fixture_source(
        root / "flake.nix",
        "{ outputs = _: let nixLibrary = import ./prm/builders/main.nix "
        "{ self.outPath = ./prm; }; lib = nixLibrary.lib; "
        f"system = {json.dumps(system)}; "
        "mkCheck = name: attrs: script: builtins.derivation { "
        "inherit system name; inherit (attrs) src PACKAGE_E2E_EXECUTABLE; "
        f"builder = {json.dumps(bash)}; "
        f"PATH = {json.dumps(sys.prefix + '/bin:' + environment['PATH'])}; "
        f"PYTHONPATH = {json.dumps(os.pathsep.join(sys.path))}; "
        'args = [ "-e" (builtins.toFile "check-builder" script) ]; }; '
        "pkgs = { stdenv.system = system; runCommand = mkCheck; lib = { "
        "optionalAttrs = condition: attrs: if condition then attrs else {}; "
        "concatMap = f: xs: builtins.concatLists (map f xs); "
        'getExe = p: "${p}/bin/${p.cliName}"; }; }; '
        "packages = { " + packages + " }; checks = { " + checks + " }; "
        "in { checks.${system} = checks; }; }\n",
    )
    _git(root, "add", ".")
    return environment


def test_flake_checks_measure_subprocesses_without_changing_sources() -> None:
    """Build real checks, measure CLI lines, and keep reports outside the checkout."""
    with TemporaryDirectory(prefix="afairesi-coverage-") as directory:
        root = Path(directory) / "source with spaces"
        environment = _prepare_coverage_flake(root)
        before = _snapshot(root)
        expression = (
            f"let f = builtins.getFlake {json.dumps('git+' + root.as_uri())}; "
            'in f.checks.${builtins.currentSystem}."z-last"'
        )
        _run(
            root,
            "flake",
            "check",
            "--no-update-lock-file",
            "-L",
            executable="nix",
            environment=environment,
        )
        result = _run(
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
        report = Path(result.stdout.strip())
        _expect((report / "html/index.html").is_file(), report)
        data = json.loads((report / "coverage.json").read_text())
        _expect(data["totals"]["missing_lines"] > 0, data)
        files = data["files"]
        _expect(
            len(files) == 1 and next(iter(files)).endswith("/main.py"),
            files,
        )
        measured = next(iter(files.values()))
        _expect(set(measured["executed_lines"]) == {1, 2, 3, 4, 6, 7}, measured)
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


@pytest.mark.parametrize("generated", [False, True])
def test_flake_checks_fail_for_test_failures(
    tmp_path: Path,
    *,
    generated: bool,
) -> None:
    """Fail the Nix check for ordinary assertions and generated counterexamples."""
    root = tmp_path / "source"
    environment = _prepare_coverage_flake(root)
    tests = root / "z-last/test_main.py"
    failure = (
        "@given(st.just(0))\ndef test_failure(value):\n    assert value != 0\n"
        if generated
        else "def test_failure():\n    assert False\n"
    )
    _write_fixture_source(tests, tests.read_text() + failure)
    before = _snapshot(root)
    result = _run(
        root,
        "flake",
        "check",
        "--no-update-lock-file",
        "-L",
        code=1,
        executable="nix",
        environment=environment,
    )
    _expect(
        "test_failure" in result.stderr and "AssertionError" in result.stderr, result
    )
    _expect(_snapshot(root) == before, "failed check changed repository state")


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
    completed = _run(tmp_path, "inspect", str(tmp_path))
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
    completed = _run(tmp_path, "inspect", str(tmp_path), code=1)
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
    completed = _run(tmp_path, "inspect", str(tmp_path), code=1)
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
    paths = [["."], [str(tmp_path)]]
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
        (("--from", "HEAD"), "head", "working", 0),
        (("--to", "HEAD"), "index", "head", 0),
        (("--cached", "--from", "initial"), "initial", "index", 0),
        (("--from", "initial", "--to", "HEAD"), "initial", "head", 0),
        (
            ("--from", "initial", "--to", "HEAD", "--merge-base"),
            "initial",
            "head",
            0,
        ),
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
    completed = _run(tmp_path / "example", "inspect-diff", *arguments, code=code)
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
    completed = _run(tmp_path, "inspect-diff", "--exit-code")
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
    completed = _run(tmp_path, "inspect-diff", "--cached")
    _expect(_inspection_apply_diff(before, completed.stdout) == after, completed)


def test_inspect_diff_pipes_through_jd_translation(tmp_path: Path) -> None:
    """Translate native diff stdin to JSON Patch for JSON viewers and patching."""
    before = _inspection_diff_repository(tmp_path)
    _inspection_diff_description(tmp_path, "Changed")
    difference = _run(tmp_path, "inspect-diff").stdout
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
        _run(tmp_path, "inspect-diff").stdout == difference,
        "temporary snapshot paths made identical diffs unstable",
    )


def test_inspect_diff_uses_the_merge_base(tmp_path: Path) -> None:
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
    for arguments, report in ((("--merge-base",), base), ((), other)):
        completed = _run(
            tmp_path, "inspect-diff", "--from", "other", "--to", "topic", *arguments
        )
        _expect(_inspection_apply_diff(report, completed.stdout) == topic, completed)
    _expect(_inspection_tree_snapshot(tmp_path) == before, "range diff changed state")


def test_inspect_diff_compares_initial_staging_against_an_empty_repository(
    tmp_path: Path,
) -> None:
    """Compare the first staged interface against an empty inventory."""
    _git(tmp_path, "init", "--quiet")
    _expect(not _run(tmp_path, "inspect-diff", "--cached").stdout, "nonempty diff")
    _inspection_repository(tmp_path)
    _git(tmp_path, "add", ".")
    staged = inspection_subject.inspect_repository(tmp_path)
    _inspection_diff_description(tmp_path, "Unstaged")
    before = _inspection_tree_snapshot(tmp_path)
    completed = _run(tmp_path, "inspect-diff", "--cached", "--exit-code", code=1)
    _expect(_inspection_apply_diff({}, completed.stdout) == staged, completed)
    _expect(_inspection_tree_snapshot(tmp_path) == before, "initial diff changed state")


@pytest.mark.parametrize(
    ("arguments", "failure", "code"),
    [
        (("--from", "missing-revision"), "Needed a single revision", 128),
        (("--to", "missing-revision"), "Needed a single revision", 128),
        ((".", "HEAD", "HEAD"), "unrecognized arguments", 2),
        (("--cached", "--to", "HEAD"), "cannot be combined with --to", 2),
        (("--merge-base",), "requires --from and --to", 2),
        (("--merge-base", "--from", "HEAD"), "requires --from and --to", 2),
        (("--merge-base", "--to", "HEAD"), "requires --from and --to", 2),
        (
            ("--cached", "--from", "HEAD", "--to", "HEAD", "--merge-base"),
            "cannot be combined with --to",
            2,
        ),
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
    completed = _run(root, "inspect-diff", *arguments, code=code)
    _expect(not completed.stdout, completed)
    _expect(failure in completed.stderr, completed)
    _expect(not list(scratch.iterdir()), "failed diff left temporary sources")
    _expect(_inspection_tree_snapshot(root) == before, "failed diff changed state")


@pytest.mark.parametrize("name", ["diff", "HEAD", "repository with spaces"])
def test_inspect_diff_supports_help_and_repository_selection_outside_git(
    tmp_path: Path,
    name: str,
) -> None:
    """Describe diffs outside Git and distinguish repository paths from commands."""
    help_output = _run(tmp_path, "inspect-diff", "--help").stdout
    _expect("--cached" in help_output and "jd-compatible" in help_output, help_output)
    _expect("--color | less -R" in help_output, help_output)
    _expect("| less" in help_output, help_output)
    _expect("| jd -t jd2patch | jless" in help_output, help_output)
    _expect(not list(tmp_path.iterdir()), "help wrote files")
    root = tmp_path / name
    root.mkdir()
    report = _inspection_diff_repository(root)
    completed = _run(tmp_path, "inspect", name)
    _expect(json.loads(completed.stdout) == report, completed)
    _inspection_diff_description(root, "Changed")
    for target in (name, str(root)):
        completed = _run(tmp_path, "inspect-diff", target)
        _expect(
            _inspection_apply_diff(report, completed.stdout)
            == inspection_subject.inspect_repository(root),
            completed,
        )
        completed = _run(
            tmp_path, "inspect-diff", "--from", "HEAD", "--to", "HEAD", target
        )
        _expect(not completed.stdout, completed)


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
    completed = _run(tmp_path, "inspect-diff", code=1)
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
    completed = _run(linked / "example", "inspect-diff")
    _expect(
        _inspection_apply_diff(report, completed.stdout)
        == inspection_subject.inspect_repository(linked),
        completed,
    )
    _expect(
        _inspection_tree_snapshot(tmp_path) == before,
        "diff changed worktree state",
    )


def test_inspect_reports_public_commands() -> None:
    """Inventory every public command without nested command groups."""
    subject = import_module("afairesi.main")
    report = subject.ArgumentDiscovery(
        Path(__file__).with_name("main.py").read_bytes(),
    ).report()
    commands = report["commands"]
    _expect(
        set(commands)
        == {
            "inspect",
            "inspect-diff",
        },
        commands,
    )
    for name, command in commands.items():
        _expect(not command["commands"], command)
        arguments = [row["names"] for row in command["arguments"]]
        _expect(arguments.count(["repository"]) == 1, command)
        _expect(["--repository"] not in arguments, command)
        for option in ("--from", "--to", "--merge-base"):
            _expect(arguments.count([option]) == (name == "inspect-diff"), command)
    _expect(not report["arguments"], report["arguments"])


@pytest.mark.parametrize("kind", ["python", "nix", "lean4"])
def test_source_validation_ignores_local_packages_and_optional_tests(
    tmp_path: Path,
    kind: str,
) -> None:
    """Keep unselected sources out of validation and inspection."""
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
    before = _snapshot(root)
    _check_flake_source(root)
    _expect(_snapshot(root) == before, "validation admitted or changed local sources")
    subject = import_module("afairesi.main")
    report = subject.inspect_repository(root)
    _expect(set(report) == {"example"} and "tests" not in report["example"], report)

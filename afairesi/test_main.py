# Copyright (c) 2026- Paschalis Bizopoulos
"""Check Afairesi's CLI and source validation."""

import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TYPE_CHECKING, cast

import pytest

import afairesi as inspection_subject
import nix_syntax

if TYPE_CHECKING:
    from collections.abc import Iterator


def _run(
    root: Path,
    *arguments: str,
    code: int = 0,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(  # noqa: S603 - Test harness selects the executable and fixture arguments.
        [os.environ["PACKAGE_E2E_EXECUTABLE"], *arguments],
        cwd=root,
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
        raise AssertionError(msg)
    return result


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(  # noqa: S603 - Fixed Git executable with test-controlled arguments.
        ["git", *arguments],  # noqa: S607
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    ).stdout


def _commit(root: Path, message: str) -> None:
    """Commit staged fixture files without relying on the user's Git config."""
    _git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "-c",
        "maintenance.auto=false",
        "commit",
        "--quiet",
        "-m",
        message,
    )


def _fixture_add(
    root: Path,
    name: str,
    kind: str,
) -> None:
    """Write the minimal tracked source needed by a test."""
    sources = {
        "bash": {"main.sh": "echo hello\n"},
        "host": {"configuration.nix": "{ ... }: {}\n"},
        "python": {"main.py": "def main():\n    pass\n"},
        "html": {"index.html": "<!doctype html><html><body></body></html>\n"},
        "latex": {
            "ms.tex": (
                "\\documentclass{article}\n\\begin{document}\nHello.\n\\end{document}\n"
            )
        },
        "lean4": {"main.lean": "def main : IO Unit := pure ()\n"},
        "generic": {},
    }[kind]
    if kind != "host":
        builder = {
            "bash": "inputs.afairesi.lib.mkBashPackage",
            "python": "inputs.self.lib.mkPythonPackage",
            "html": "inputs.self.lib.mkHtmlPackage",
            "latex": "inputs.self.lib.mkLatexPackage",
            "lean4": "inputs.self.lib.mkLean4Package",
            "generic": "pkgs.writeTextFile",
        }[kind]
        settings = (
            '  name = baseNameOf ./.;\n  text = "";\n'
            if kind == "generic"
            else "  inherit pkgs;\n  src = ./.;\n"
        )
        if kind in {"python", "lean4"}:
            settings += "  executable = true;\n"
        sources["default.nix"] = (
            f"{{ inputs, pkgs, ... }}:\n{builder} {{\n{settings}}}\n"
        )
    package = root / name
    package.mkdir()
    for filename, source in sources.items():
        (package / filename).write_text(source)
    _git(root, "add", "--force", "--", name)


@pytest.mark.parametrize("exported", [False, True], ids=["git", "export"])
def test_source_validation_skips_nested_assets_without_running_package_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, exported: bool
) -> None:
    """Validate resource files without executing code or traversing nested assets."""
    repository = _repository(tmp_path)
    _fixture_add(repository, "example", "python")
    (repository / "example/main.py").write_text(
        "raise RuntimeError('must not execute')\n"
    )
    assets = repository / "example/assets"
    assets.mkdir()
    (assets / "default.nix").write_text("invalid nested declaration")
    _git(repository, "add", "--", "example")
    local = repository / "experiments"
    local.mkdir()
    (local / "main.py").write_text("invalid untracked source")
    if exported:
        shutil.rmtree(repository / ".git")
    original = os.scandir

    def guarded(path: str | os.PathLike[str]) -> Iterator[os.DirEntry[str]]:
        if Path(path).is_relative_to(assets) or (
            not exported and Path(path).is_relative_to(local)
        ):
            message = f"traversed excluded directory {path}"
            raise AssertionError(message)
        return original(path)

    monkeypatch.setattr(os, "scandir", guarded)
    sources = inspection_subject.source_paths(repository)
    _expect(Path("example/default.nix") in sources, sources)
    _expect(Path("example/assets/default.nix") not in sources, sources)
    inspection_subject.validate_flake_source(repository)


def _expect(condition: bool, detail: object) -> None:  # noqa: FBT001
    """Report an invariant failure with its observed state."""
    if not condition:
        raise AssertionError(detail)


def _repository(root: Path) -> Path:
    """Index a minimal flake in an existing temporary directory."""
    _git(root, "init", "--quiet")
    (root / "flake.nix").write_text("{}\n")
    (root / "flake.lock").touch()
    _git(root, "add", ".")
    return root


@pytest.fixture(name="root")
def repository_root_fixture(tmp_path: Path) -> Path:
    """Index a flake alongside the test's other temporary files."""
    root = tmp_path / "repo"
    root.mkdir()
    return _repository(root)


def _snapshot(root: Path) -> dict[Path, tuple[int, bytes]]:
    """Observe contents and modes, including Git metadata and symbolic links."""
    files = {}
    for path in root.rglob("*"):
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            contents = os.fsencode(path.readlink())
        elif stat.S_ISREG(mode):
            contents = path.read_bytes()
        else:
            contents = b""
        files[path.relative_to(root)] = (mode, contents)
    return files


def test_afairesi_prints_the_behavioral_specification_before_implementation(
    root: Path,
) -> None:
    """Publish high-level test names as soon as the tests have been written."""
    _fixture_add(root, "inbox", "python")
    (root / "inbox/main.py").unlink()
    tests = root / "inbox/test_main.py"
    tests.write_text(
        "raise RuntimeError('inspection must not import this suite')\n"
        "def test_delivers_messages(): ...\n"
    )
    _git(root, "add", "--", "inbox/test_main.py")
    report = json.loads(_run(root).stdout)
    _expect(report == {"inbox": ["test_delivers_messages"]}, report)


def test_detailed_behavior_names_extend_the_specification_in_stable_order(
    root: Path,
) -> None:
    """List each declaration once, independently of assertion text or case data."""
    _fixture_add(root, "inbox", "python")
    tests = root / "inbox/test_main.py"
    tests.write_text(
        "import pytest\n"
        "@pytest.mark.parametrize('message', ['abc', 'ABC'])\n"
        "def test_preserves_message_case(message): assert deliver(message) == message\n"
        "def test_delivers_messages(): assert deliver('hello') == 'hello'\n"
    )
    _git(root, "add", "--", "inbox/test_main.py")
    first = _run(root).stdout
    _expect(
        json.loads(first)
        == {"inbox": ["test_delivers_messages", "test_preserves_message_case"]},
        first,
    )
    tests.write_text(
        "def test_delivers_messages(): ...\ndef test_preserves_message_case(): ...\n"
    )
    _expect(_run(root).stdout == first, first)


def test_each_package_lists_a_shared_behavior_name_once(root: Path) -> None:
    """Keep equal identifiers distinct across packages and unique within a suite."""
    for name in ("mail_box", "mail-box"):
        _fixture_add(root, name, "python")
        tests = root / name / "test_main.py"
        tests.write_text("def test_delivers(): ...\ndef test_delivers(): ...\n")
        _git(root, "add", "--", f"{name}/test_main.py")
    report = json.loads(_run(root).stdout)
    _expect(list(report) == ["mail-box", "mail_box"], report)
    _expect(
        report == {"mail-box": ["test_delivers"], "mail_box": ["test_delivers"]}, report
    )
    with pytest.raises(inspection_subject.CommandError, match="module names collide"):
        inspection_subject.validate_flake_source(root)


def test_cli_shows_help_and_rejects_unknown_arguments_outside_a_repository(
    tmp_path: Path,
) -> None:
    """Expose the CLI and reject invalid requests outside a repository."""
    before = _snapshot(tmp_path)
    help_output = _run(tmp_path, "--help").stdout
    _expect("[PATH_OR_REMOTE]" in help_output, help_output)
    _expect("  diff " in help_output, help_output)
    option = _run(tmp_path, "diff", "--help").stdout
    _expect(option.splitlines()[0] == "usage: afairesi diff [-h]", option)
    for arguments in (
        ("--unknown",),
        (".", "extra"),
        ("diff", "."),
        ("diff", "--unknown"),
    ):
        rejected = _run(tmp_path, *arguments, code=2)
        _expect("unrecognized arguments" in rejected.stderr, rejected)
    _expect(_snapshot(tmp_path) == before, "CLI help changed state")


@pytest.mark.parametrize(
    ("kind", "constructor"),
    [
        ("bash", "inputs.afairesi.lib.mkBashPackage"),
        ("html", "inputs.self.lib.mkHtmlPackage"),
        ("latex", "inputs.afairesi.lib.mkLatexPackage"),
        ("lean4", "(inputs.self).lib.mkLean4Package"),
        ("python", "let build = inputs.self.lib.mkPythonPackage; in (build)"),
    ],
)
def test_declared_package_kind_determines_the_required_entrypoint(
    tmp_path: Path,
    kind: str,
    constructor: str,
) -> None:
    """Keep the declared build kind when supporting files resemble entrypoints."""
    entrypoints = {
        "bash": "main.sh",
        "html": "index.html",
        "latex": "ms.tex",
        "lean4": "main.lean",
        "python": "main.py",
    }
    root = _repository(tmp_path)
    _fixture_add(root, "example", kind)
    package = root / "example"
    executable = "executable = true;" if kind in {"lean4", "python"} else ""
    (package / "default.nix").write_text(
        "{ inputs, pkgs, ... }: let dependency = inputs.self.lib.mkHtmlPackage {}; in "
        f"{constructor} {{ inherit pkgs; src = ./.; {executable} }}",
    )
    if kind != "python":
        (package / "test_main.py").write_text("def test_unrelated(): pass")
    for filename in (*entrypoints.values(), "main.nix"):
        (package / filename).touch()
    _git(root, "add", "--", "example")
    report = json.loads(_run(root).stdout)
    _expect(report == {"example": []}, report)
    inspection_subject.validate_flake_source(root)
    (package / entrypoints[kind]).unlink()
    with pytest.raises(
        inspection_subject.CommandError, match=re.escape(entrypoints[kind])
    ):
        inspection_subject.validate_flake_source(root)


def test_source_validation_rejects_a_tracked_python_package_initializer(
    root: Path,
) -> None:
    """Reject a selected initializer without changing source or installing over main."""
    _fixture_add(root, "example", "python")
    initializer = root / "example/__init__.py"
    initializer.write_text('"""Personal initialization."""\n')
    _git(root, "add", "example/__init__.py")
    before = _snapshot(root)
    with pytest.raises(
        inspection_subject.CommandError, match=r"__init__\.py: reserved"
    ):
        inspection_subject.validate_flake_source(root)
    _expect(_snapshot(root) == before, "rejection changed sources")


def test_source_validation_accepts_builders_in_a_with_scope(root: Path) -> None:
    """Validate a package without rewriting its Nix scope."""
    _fixture_add(root, "example", "python")
    definition = root / "example/default.nix"
    definition.write_text(
        "{ inputs, pkgs, ... }: with inputs.self.lib; "
        "mkPythonPackage { inherit pkgs; src = ./.; executable = true; }\n"
    )
    _git(root, "add", ".")
    before = _snapshot(root)
    inspection_subject.validate_flake_source(root)
    _expect(_snapshot(root) == before, "validation changed repository")


@pytest.mark.parametrize(
    "relative",
    ["flake.nix", "example/default.nix", "example/main.nix", "example/test_main.nix"],
)
def test_source_validation_identifies_the_file_with_invalid_nix_syntax(
    root: Path, relative: str
) -> None:
    """Report syntax errors in tracked flake, package, source, and test files."""
    _fixture_add(root, "example", "generic")
    (root / relative).write_text("{ inputs, pkgs, ... }: (\n")
    _git(root, "add", "--", relative)
    before = _snapshot(root)
    with pytest.raises(
        nix_syntax.NixSyntaxError, match=rf"{re.escape(relative)}:\d+:\d+: parse error"
    ):
        inspection_subject.validate_flake_source(root)
    _expect(_snapshot(root) == before, "syntax validation changed repository")


def test_packages_with_custom_builders_stay_generic_regardless_of_source_files(
    root: Path,
) -> None:
    """Discover Nix suites for native and opaque builders with mixed source files."""
    builders = {
        "class": "pkgs.python3Packages.buildPythonPackage",
        "dash-name": "pkgs.rustPlatform.buildRustPackage",
        "dash_name": 'pkgs.haskellPackages.callCabal2nix "example" ./.',
        "custom": "import ./builder.nix",
    }
    for name, builder in builders.items():
        package = root / name
        package.mkdir()
        (package / "default.nix").write_text(
            f"{{ pkgs, ... }}: {builder} {{}}\n",
        )
        (package / "test_main.nix").write_text(
            "{ testBuilds = { expr = 1; expected = 1; }; }"
        )
        (package / "__init__.py").write_text("VALUE = 42\n")
        (package / "main.py").write_text("not valid Python!\n")
        (package / "test_main.py").write_text("not valid Python tests!\n")
        for marker in ("main.lean", "ms.tex", "index.html"):
            (package / marker).write_text("Supporting source\n")
    _git(root, "add", ".")
    inspection_subject.validate_flake_source(root)
    report = json.loads(_run(root).stdout)
    _expect(report == {name: ["testBuilds"] for name in builders}, report)


def test_python_names_match_the_functions_and_methods_collected_by_pytest(
    tmp_path: Path,
) -> None:
    """Use pytest's collection as the oracle for ordinary and unittest test names."""
    _inspection_repository(tmp_path)
    path = tmp_path / "example/test_main.py"
    path.write_text("""
import unittest as unit
from unittest import IsolatedAsyncioTestCase as AsyncCase
from unittest import TestCase
from unittest import TestCase as Case
def testCamelCase(): pass
def test(): pass
def helper():
    def test_nested(): pass
class TestArguments:
    def testCamelCase(self): pass
    async def test_missing_input(self): pass
class Arguments(Case):
    def test_valid_input(self): pass
class Derived(Arguments):
    def test_other_input(self): pass
class Helper:
    def test_not_collected(self): pass
class Reports(TestCase):
    def testCamelCase(self): pass
class Cases:
    def test_result(self): pass
class Intermediate(Cases): pass
class TestInherited(Intermediate): pass
class TestOverride(Cases):
    test_result = None
    def test_visible(self): pass
class Qualified(unit.TestCase):
    def test_qualified(self): pass
class AsyncBase(AsyncCase): pass
class AsyncReports(AsyncBase):
    async def test_result(self): pass
""")
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
    declared = json.loads(_run(tmp_path, str(tmp_path)).stdout)["example"]
    _expect(declared == collected, (declared, collected))


def test_specification_preserves_package_names_and_omits_standalone_hosts(
    root: Path,
) -> None:
    """Keep each package's exact spelling in a mixed repository."""
    packages = [
        ("python", "Mixed_Name-v2"),
        ("python", "double--dash"),
        ("python", "_Private"),
        ("html", "Web.Site+v2"),
        ("latex", "Report.v2"),
        ("lean4", "Proof_Library-v2"),
        ("generic", "Package Name"),
        ("generic", "δοκιμή"),
    ]
    for kind, name in packages:
        _fixture_add(root, name, kind)
    _fixture_add(root, "workLaptop", "host")
    inspection_subject.validate_flake_source(root)
    report = json.loads(_run(root).stdout)
    _expect(report == {name: [] for _, name in packages}, report)


def _inspection_repository(root: Path) -> None:
    (root / "flake.nix").touch()
    package = root / "example"
    package.mkdir(parents=True)
    (package / "default.nix").write_text(
        "{ inputs, pkgs, ... }: inputs.self.lib.mkPythonPackage { "
        "inherit pkgs; src = ./.; executable = true; "
        'meta.description = "Example"; }',
    )
    (package / "main.py").write_text(
        """
raise RuntimeError("inspection must not execute this")
def main(): pass
""",
    )
    (package / "test_main.py").write_text(
        """
raise RuntimeError("inspection must not collect or run tests")
def test_default_output(): pass
""",
    )
    library = root / "library"
    library.mkdir()
    (library / "default.nix").write_text("{}")


def test_package_names_follow_their_tracked_declarations(
    root: Path,
) -> None:
    """Publish a tracked package and carry its test names through a rename."""
    package = root / "draft"
    package.mkdir()
    (package / "default.nix").write_text("{}")
    (package / "test_main.nix").write_text(
        "{ testDelivers = { expr = 1; expected = 1; }; }"
    )
    _git(root, "add", "--", "draft/test_main.nix")
    _expect(json.loads(_run(root).stdout) == {}, "untracked declaration was listed")
    _git(root, "add", "--", "draft/default.nix")
    _expect(json.loads(_run(root).stdout) == {"draft": ["testDelivers"]}, package)
    _git(root, "mv", "--", "draft", "inbox")
    _expect(json.loads(_run(root).stdout) == {"inbox": ["testDelivers"]}, root)


def test_library_tests_are_listed_even_when_main_requires_arguments(
    tmp_path: Path,
) -> None:
    """Only executables require a main function callable without arguments."""
    _inspection_repository(tmp_path)
    (tmp_path / "flake.lock").touch()
    package = tmp_path / "example"
    definition = package / "default.nix"
    definition.write_text(
        """
{ inputs, pkgs, ... }:
let options = {
    inherit pkgs;
    executable = false;
    src = ./.;
}; in inputs.self.lib.mkPythonPackage options
""",
    )
    source = package / "main.py"
    source.write_text(source.read_text().replace("def main():", "def main(values):"))
    inspection_subject.validate_flake_source(tmp_path)
    report = inspection_subject.inspect_repository(tmp_path)["example"]
    _expect(report == ["test_default_output"], report)
    definition.write_text(
        definition.read_text().replace("executable = false", "executable = true")
    )
    with pytest.raises(
        inspection_subject.CommandError,
        match="main must accept a call without arguments",
    ):
        inspection_subject.validate_flake_source(tmp_path)


@pytest.mark.parametrize("in_git", [False, True], ids=["outside", "git"])
def test_afairesi_prints_package_test_names_by_default_or_by_path(
    tmp_path: Path,
    *,
    in_git: bool,
) -> None:
    """Print the same package specification from Git and exported directories."""
    _inspection_repository(tmp_path)
    expected = {"example": ["test_default_output"], "library": []}
    nested = tmp_path / "example"
    if in_git:
        _git(tmp_path, "init", "--quiet")
        _git(tmp_path, "add", ".")
        calls = [
            (tmp_path, []),
            (nested, []),
            (nested, ["."]),
            (nested, [f"../{tmp_path.name}"]),
        ]
    else:
        completed = _run(tmp_path, code=1)
        _expect(not completed.stdout, completed)
        _expect("not inside a Git repository" in completed.stderr, completed)
        calls = [(tmp_path, ["."]), (nested, [str(tmp_path)])]
    before = _snapshot(tmp_path)
    for cwd, arguments in calls:
        completed = _run(cwd, *arguments)
        _expect(
            json.loads(completed.stdout) == expected,
            "path resolution changed the package specification",
        )
    _expect(_snapshot(tmp_path) == before, "inspection wrote to the repository")


def test_specification_reports_a_missing_flake_or_malformed_test_suite(
    tmp_path: Path,
) -> None:
    """Report invalid repositories and malformed source with useful errors."""
    with pytest.raises(inspection_subject.CommandError, match=r"flake\.nix"):
        inspection_subject.inspect_repository(tmp_path)
    _inspection_repository(tmp_path)
    (tmp_path / "example" / "test_main.py").write_text("def invalid(")
    with pytest.raises(
        inspection_subject.CommandError,
        match="could not parse Python source",
    ):
        inspection_subject.inspect_repository(tmp_path)


@pytest.fixture
def clone_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Isolate Git clones and verify that each inspection cleans up."""
    home = tmp_path / "home"
    scratch = tmp_path / "temporary"
    scratch.mkdir()
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TMPDIR", str(scratch))
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")
    yield tmp_path
    _expect(not list(scratch.iterdir()), "remote inspection left temporary files")
    _expect(not list(home.iterdir()), "remote inspection changed HOME")


@pytest.fixture
def remote_repository(
    clone_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, str]:
    """Serve the fixture commit through a local Git URL rewrite."""
    source = clone_workspace / "source"
    source.mkdir()
    _committed_repository(source)
    remote = "https://example.test/team/repository.git"
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{source.as_uri()}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", remote)
    return source, remote


def test_remote_test_names_reflect_new_commits_on_each_run(
    tmp_path: Path,
    remote_repository: tuple[Path, str],
) -> None:
    """Inspect fresh remote contents without creating persistent home state."""
    source, remote = remote_repository
    _expect(
        json.loads(_run(tmp_path, remote).stdout)
        == inspection_subject.inspect_repository(source),
        "wrong remote test names",
    )
    _write_test(source, "remote_behavior")
    _git(source, "add", "example/test_main.py")
    _commit(source, "rename test")
    _expect(
        json.loads(_run(tmp_path, remote).stdout)
        == inspection_subject.inspect_repository(source),
        "remote inspection was stale",
    )


def test_afairesi_reports_invalid_remote_tests_without_emitting_partial_json(
    tmp_path: Path,
    remote_repository: tuple[Path, str],
) -> None:
    """Report parse failures from a cloned repository without emitting partial JSON."""
    source, remote = remote_repository
    (source / "example/test_main.py").write_text("def invalid(")
    _git(source, "add", "example/test_main.py")
    _commit(source, "malformed source")
    completed = _run(tmp_path, remote, code=1)
    _expect(
        not completed.stdout and "could not parse Python source" in completed.stderr,
        completed,
    )


def test_local_paths_include_uncommitted_changes_while_git_file_urls_show_commits(
    tmp_path: Path,
    remote_repository: tuple[Path, str],
) -> None:
    """Read the same dirty repository by filesystem path and by native Git URL."""
    source, _remote = remote_repository
    _write_test(source, "local_behavior")
    before = _snapshot(tmp_path)
    local_report = json.loads(_run(tmp_path, str(source)).stdout)
    _expect(local_report["example"] == ["test_local_behavior"], local_report)
    remote_report = json.loads(_run(tmp_path, source.as_uri()).stdout)
    _expect(remote_report["example"] == ["test_default_output"], remote_report)
    _expect(
        _snapshot(tmp_path) == before,
        "inspection changed source or Git state",
    )


def test_afairesi_forwards_the_error_and_exit_status_from_a_failed_remote_clone(
    clone_workspace: Path,
) -> None:
    """Compare failed remote inspection with Git's exit status and diagnostics."""
    remote = (clone_workspace / "missing.git").as_uri()
    scratch = Path(os.environ["TMPDIR"])
    native_checkout = clone_workspace / "native-checkout"
    expected = subprocess.run(  # noqa: S603 - Fixed Git executable with test-controlled arguments.
        ["git", "clone", "--", remote, str(native_checkout)],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    actual = _run(clone_workspace, remote, code=expected.returncode)
    actual_error = re.sub(
        re.escape(str(scratch)) + r"/afairesi-inspect-[^/]+/repository",
        "<CHECKOUT>",
        actual.stderr,
    )
    _expect(
        (actual.stdout, actual_error)
        == (
            expected.stdout,
            expected.stderr.replace(str(native_checkout), "<CHECKOUT>"),
        ),
        f"native clone error changed: {expected.stderr!r}, {actual.stderr!r}",
    )


def _committed_repository(root: Path) -> None:
    """Create a committed repository with inspectable sources."""
    _inspection_repository(root)
    _git(root, "init", "--quiet")
    _git(root, "add", ".")
    _commit(root, "initial")


def _write_test(root: Path, behavior: str) -> None:
    """Give the fixture's sole Python test a new name."""
    (root / "example/test_main.py").write_text(f"def test_{behavior}(): pass\n")


def _apply_diff(
    before: dict[str, list[str]],
    difference: str,
) -> dict[str, list[str]]:
    """Use jd itself to verify that an emitted patch reconstructs the inspection."""
    with NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".jd") as patch:
        patch.write(difference)
        patch.flush()
        completed = subprocess.run(  # noqa: S603 - Fixed jd executable; patch is a temporary fixture file.
            ["jd", "-p", patch.name],  # noqa: S607
            input=json.dumps(before),
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    return cast("dict[str, list[str]]", json.loads(completed.stdout))


def test_diff_emits_a_jd_patch_from_index_to_working_tree(tmp_path: Path) -> None:
    """Use the staged test names as the baseline even from a nested directory."""
    _committed_repository(tmp_path)
    _write_test(tmp_path, "Staged")
    _git(tmp_path, "add", ".")
    index = inspection_subject.inspect_repository(tmp_path)
    (tmp_path / "example/test_main.py").write_text(
        "def test_empty_input(): pass\ndef test_unicode_input(): pass\n"
    )
    working = inspection_subject.inspect_repository(tmp_path)
    before = _snapshot(tmp_path)
    completed = _run(tmp_path / "example", "diff")
    _expect(_apply_diff(index, completed.stdout) == working, completed)
    _expect(not completed.stderr, completed)
    _expect("afairesi-diff-" not in completed.stdout, completed)
    _expect(_run(tmp_path, "diff").stdout == completed.stdout, "diff output changed")
    _expect(
        _snapshot(tmp_path) == before,
        "diff changed Git or sources",
    )


def test_diff_ignores_implementation_edits_and_untracked_resources(
    tmp_path: Path,
) -> None:
    """Only changes to the package-to-test-name mapping produce output."""
    _committed_repository(tmp_path)
    source = tmp_path / "example/main.py"
    source.write_text("def unfinished(")
    declaration = tmp_path / "example/default.nix"
    declaration.write_text(declaration.read_text().replace("Example", "Renamed CLI"))
    tests = tmp_path / "example/test_main.py"
    tests.write_text(
        'def test_default_output():\n    """Updated notes."""\n'
        '    raise RuntimeError("changed")\n',
    )
    (tmp_path / "untracked").mkdir()
    (tmp_path / "untracked/default.nix").write_text("{}")
    before = _snapshot(tmp_path)
    completed = _run(tmp_path, "diff")
    _expect(not completed.stdout and not completed.stderr, completed)
    _expect(_snapshot(tmp_path) == before, "empty diff changed state")


def test_diff_follows_tracked_symlink_targets_of_test_suites(tmp_path: Path) -> None:
    """Keep deeply nested symlink targets in both snapshots."""
    _committed_repository(tmp_path)
    source = tmp_path / "example/test_main.py"
    target = tmp_path / "assets/deep/entry"
    target.parent.mkdir(parents=True)
    source.rename(target)
    source.symlink_to("../assets/deep/entry")
    _git(tmp_path, "add", ".")
    before = inspection_subject.inspect_repository(tmp_path)
    target.write_text(target.read_text().replace("default_output", "requested_output"))
    after = inspection_subject.inspect_repository(tmp_path)
    _expect(before != after, after)
    _expect(_apply_diff(before, _run(tmp_path, "diff").stdout) == after, after)


def test_removing_a_package_drops_its_entry_even_when_it_shares_a_host(
    root: Path,
) -> None:
    """Drop deleted declarations and keep diagnosing them in layout validation."""
    _fixture_add(root, "shared", "host")
    (root / "shared/default.nix").write_text("{}")
    _git(root, "add", "--", "shared/default.nix")
    before = json.loads(_run(root).stdout)
    _expect(before == {"shared": []}, before)
    (root / "shared/default.nix").unlink()
    after = json.loads(_run(root).stdout)
    _expect(after == {}, after)
    _expect(_apply_diff(before, _run(root, "diff").stdout) == after, after)
    with pytest.raises(
        inspection_subject.CommandError,
        match=r"shared/default\.nix: missing required tracked regular file",
    ):
        inspection_subject.validate_flake_source(root)


def test_diff_uses_the_index_before_the_first_commit(tmp_path: Path) -> None:
    """Compare against the index even when the repository has no HEAD."""
    _git(tmp_path, "init", "--quiet")
    _expect(not _run(tmp_path, "diff").stdout, "nonempty diff")
    _inspection_repository(tmp_path)
    _git(tmp_path, "add", ".")
    staged = inspection_subject.inspect_repository(tmp_path)
    _write_test(tmp_path, "Unstaged")
    before = _snapshot(tmp_path)
    completed = _run(tmp_path, "diff")
    _expect(
        _apply_diff(staged, completed.stdout)
        == inspection_subject.inspect_repository(tmp_path),
        completed,
    )
    _expect(_snapshot(tmp_path) == before, "initial diff changed state")


def test_diff_errors_leave_no_partial_output_or_temporary_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clean failed snapshots when a tracked source cannot be inspected."""
    root = tmp_path / "repository"
    root.mkdir()
    _committed_repository(root)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setenv("TMPDIR", str(scratch))
    (root / "example/test_main.py").write_text("def invalid(")
    before = _snapshot(root)
    completed = _run(root, "diff", code=1)
    _expect(not completed.stdout, completed)
    _expect("could not parse Python source" in completed.stderr, completed)
    _expect(not list(scratch.iterdir()), "failed diff left temporary sources")
    _expect(_snapshot(root) == before, "failed diff changed state")


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("diff", ["./diff"]),
        ("diff", ["--", "diff"]),
        ("repository with spaces", ["repository with spaces"]),
    ],
)
def test_cli_accepts_repository_names_that_need_disambiguation_or_contain_spaces(
    tmp_path: Path,
    name: str,
    arguments: list[str],
) -> None:
    """Accept explicit paths to diff and ordinary directory names as given."""
    root = tmp_path / name
    root.mkdir()
    (root / "flake.nix").touch()
    completed = _run(tmp_path, *arguments)
    _expect(json.loads(completed.stdout) == {}, completed)


def test_diff_reports_unresolved_merge_conflicts_and_leaves_the_index_untouched(
    tmp_path: Path,
) -> None:
    """Report the conflicting index before producing a test-name diff."""
    _committed_repository(tmp_path)
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
    before = _snapshot(tmp_path)
    completed = _run(tmp_path, "diff", code=1)
    _expect(not completed.stdout and "unmerged index" in completed.stderr, completed)
    _expect(
        _snapshot(tmp_path) == before,
        "conflict diff changed state",
    )


def test_diff_reads_the_selected_linked_worktree_without_modifying_it(
    tmp_path: Path,
) -> None:
    """Read the selected worktree's index while preserving shared Git metadata."""
    root, linked = tmp_path / "repository", tmp_path / "linked"
    root.mkdir()
    _committed_repository(root)
    report = inspection_subject.inspect_repository(root)
    _git(root, "worktree", "add", "--quiet", "--detach", str(linked), "HEAD")
    _write_test(linked, "Linked")
    before = _snapshot(tmp_path)
    completed = _run(linked / "example", "diff")
    _expect(
        _apply_diff(report, completed.stdout)
        == inspection_subject.inspect_repository(linked),
        completed,
    )
    _expect(
        _snapshot(tmp_path) == before,
        "diff changed worktree state",
    )


def test_source_validation_ignores_untracked_packages_and_test_files(
    root: Path,
) -> None:
    """Skip malformed local declarations and suites until they are tracked."""
    _fixture_add(root, "example", "python")
    _fixture_add(root, "library", "generic")
    (root / "example/test_main.py").write_text("def broken(:")
    (root / "library/test_main.nix").write_text("{ invalid")
    local = root / "experiments"
    local.mkdir()
    (local / "default.nix").write_text("{ invalid")
    before = _snapshot(root)
    inspection_subject.validate_flake_source(root)
    report = inspection_subject.inspect_repository(root)
    _expect(report == {"example": [], "library": []}, report)
    _expect(
        _snapshot(root) == before, "reading untracked sources changed the repository"
    )


@pytest.mark.parametrize(
    ("kind", "suite", "source", "expected"),
    [
        (
            "bash",
            "test_main.bats",
            """exit 99
cat <<'TEXT'
@test "not a declaration" {
TEXT
@test "preserves spaces" { true; }
check_status() { # @test
  true
}
helper() { true; }
""",
            ["check_status", "preserves spaces"],
        ),
        (
            "generic",
            "test_main.nix",
            """{ ... }: let
              selection = [ "preservesSpaces" ];
              suite = {
                tests = selection;
                preservesSpaces = { expr = throw "never evaluate"; expected = true; };
                testExcluded = { expr = false; expected = true; };
              };
            in (suite)""",
            ["preservesSpaces"],
        ),
        (
            "lean4",
            "test_main.lean",
            """namespace Résultats
section
/- theorem ignored : True := trivial -/
theorem preservesCafé : True := trivial
example : True := trivial
end
end Résultats
""",
            ["Résultats.preservesCafé"],
        ),
    ],
)
def test_named_cases_appear_in_the_package_specification_and_its_diff(
    root: Path, kind: str, suite: str, source: str, expected: list[str]
) -> None:
    """Follow a declared behavior through inspection, a rename, and a jd patch."""
    _fixture_add(root, "example", kind)
    path = root / "example" / suite
    path.write_text(source)
    untracked = json.loads(_run(root).stdout)
    _expect(untracked == {"example": []}, untracked)
    _git(root, "add", "--", "example")
    before = json.loads(_run(root).stdout)
    _expect(before == {"example": expected}, before)
    path.write_text(source.replace("preserves", "retains"))
    after = {"example": [name.replace("preserves", "retains") for name in expected]}
    _expect(json.loads(_run(root).stdout) == after, after)
    _expect(_apply_diff(before, _run(root, "diff").stdout) == after, after)


def test_cargo_lists_tests_from_enabled_targets_and_their_module_trees(
    root: Path,
) -> None:
    """List declared unit and integration tests without building the crate."""
    _fixture_add(root, "example", "generic")
    package = root / "example"
    sources = {
        "Cargo.toml": """[package]
name = "example"
edition = "2024"
autotests = false
[[test]]
name = "behavior"
path = "cases/output.rs"
[[bin]]
name = "tool"
test = false
""",
        "src/lib.rs": (
            '#[cfg(test)]\nmod tests;\nconst TEXT: &str = "#[test] fn fake() {}";\n'
        ),
        "src/tests.rs": (
            '#[path = "shared.rs"] mod shared;\n#[test]\nfn accepts_input() {}\n'
        ),
        "src/shared.rs": "mod nested { #[test] fn preserves_spaces() {} }\n",
        "src/bin/tool.rs": "#[test] fn disabled() {}\n",
        "cases/output.rs": "#[test] /* a comment */ #[ignore] fn prints_output() {}\n",
        "tests/unused.rs": "#[test] fn ignored_target() {}\n",
        "src/orphan.rs": "#[test] fn unreachable() {}\n",
        "build.rs": 'fn main() { panic!("do not build"); }\n',
    }
    for filename, source in sources.items():
        path = package / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    _git(root, "add", "--", "example")
    (package / "src/bin/untracked.rs").write_text("#[test] fn untracked() {}\n")
    before = json.loads(_run(root).stdout)
    _expect(
        before
        == {
            "example": [
                "prints_output",
                "tests::accepts_input",
                "tests::shared::nested::preserves_spaces",
            ]
        },
        before,
    )
    suite = package / "src/shared.rs"
    suite.write_text(suite.read_text().replace("preserves", "retains"))
    after = json.loads(_run(root).stdout)
    _expect(
        before != after and _apply_diff(before, _run(root, "diff").stdout) == after,
        after,
    )

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
from typing import TYPE_CHECKING, Any, cast

import pytest

import afairesi as inspection_subject
import nix_syntax

if TYPE_CHECKING:
    import ast
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
            "{ inputs, pkgs, ... }:\n"
            + builder
            + " {\n"
            + settings
            + '  meta.description = "Fixture package.";\n}\n'
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
        "from pathlib import Path\noutput = Path.home() / 'reports'\n"
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


def test_inventory_lists_a_renamed_package_and_its_tests_under_the_new_name(
    tmp_path: Path,
) -> None:
    """Report the renamed package and its tests from the Git index."""
    root = _repository(tmp_path)
    _fixture_add(root, "example", "generic")
    (root / "example/test_main.nix").write_text('throw "must not evaluate"')
    _git(root, "add", "--", "example/test_main.nix")
    _git(root, "mv", "--", "example", "renamed")
    report = json.loads(_run(root).stdout)
    _expect(set(report) == {"renamed"}, report)
    _expect(report["renamed"]["tests"] == ["test_main.nix"], report)


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
        ("python", "let lib = inputs.self.lib; in lib.mkPythonPackage"),
        (
            "python",
            (
                "let build = inputs.self.lib.mkPythonPackage; "
                "in { fallback ? build }: (build)"
            ),
        ),
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
    for filename in (*entrypoints.values(), "main.nix"):
        (package / filename).touch()
    _git(root, "add", "--", "example")
    report = json.loads(_run(root).stdout)
    _expect(report["example"]["type"] == kind, report)
    inspection_subject.validate_flake_source(root)
    (package / entrypoints[kind]).unlink()
    with pytest.raises(
        inspection_subject.CommandError, match=re.escape(entrypoints[kind])
    ):
        inspection_subject.validate_flake_source(root)


@pytest.mark.parametrize("kind", ["bash", "html", "latex", "lean4", "generic"])
def test_package_kind_determines_which_tests_are_listed(root: Path, kind: str) -> None:
    """Supporting Python files never replace another package kind's test report."""
    _fixture_add(root, "example", kind)
    package = root / "example"
    (package / "test_main.py").write_text("not valid python!")
    (package / "test_main.sh").write_text("exit 1\n")
    (package / "test_main.lean").write_text("example : 1 = 1 := rfl\n")
    (package / "test_main.nix").write_text("{}\n")
    _git(root, "add", "--", "example")
    report = json.loads(_run(root).stdout)["example"]
    expected = {
        "bash": ["test_main.sh"],
        "lean4": ["test_main.lean"],
        "generic": ["test_main.nix"],
    }.get(kind, [])
    _expect(report.get("tests", []) == expected, report)


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


@pytest.mark.parametrize(
    "expression",
    [
        (
            "let executable = true; args = { inherit pkgs executable; src = ./.; }; "
            "in inputs.self.lib.mkPythonPackage args"
        ),
        (
            "with inputs.self.lib; "
            "mkPythonPackage { inherit pkgs; src = ./.; executable = true; }"
        ),
    ],
)
def test_source_validation_accepts_indirect_afairesi_builders(
    root: Path,
    expression: str,
) -> None:
    """Accept Nix expressions without rewriting builders or evaluating source."""
    _fixture_add(root, "example", "python")
    definition = root / "example/default.nix"
    definition.write_text("{ inputs, pkgs, ... }: " + expression + "\n")
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


@pytest.mark.parametrize(
    "builder",
    [
        "pkgs.python3Packages.buildPythonPackage",
        "pkgs.rustPlatform.buildRustPackage",
        'pkgs.haskellPackages.callCabal2nix "example" ./.',
        "import ./builder.nix",
    ],
)
def test_packages_with_custom_builders_stay_generic_regardless_of_source_files(
    root: Path,
    builder: str,
) -> None:
    """Keep native and opaque builders generic regardless of supporting sources."""
    for name in ("class", "dash-name", "dash_name"):
        _fixture_add(root, name, "generic")
        package = root / name
        (package / "default.nix").write_text(
            f"{{ inputs, pkgs, ... }}: {builder} {{ "
            'meta.description = "Generic package"; }\n',
        )
        (package / "__init__.py").write_text("VALUE = 42\n")
        (package / "main.py").write_text("not valid Python!\n")
        (package / "test_main.py").write_text("not valid Python tests!\n")
        for marker in ("main.lean", "ms.tex", "index.html"):
            (package / marker).write_text("Supporting source\n")
    _git(root, "add", ".")
    before = _snapshot(root)
    inspection_subject.validate_flake_source(root)
    report = json.loads(_run(root).stdout)
    _expect(
        report
        == {
            name: {"type": "generic", "description": "Generic package"}
            for name in ("class", "dash-name", "dash_name")
        },
        report,
    )
    _expect(_snapshot(root) == before, "generic packages were rewritten")


def test_python_test_inventory_matches_the_functions_and_methods_collected_by_pytest(
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
    declared = inspection_subject.inspect_repository(tmp_path)["example"]["tests"]
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
        ("generic", "Package Name"),
        ("generic", "δοκιμή"),
        ("host", "workLaptop"),
    ],
)
def test_inventory_preserves_the_spelling_of_package_and_host_names(
    root: Path, kind: str, name: str
) -> None:
    """Validate and inspect names without a shared style rule."""
    _fixture_add(root, name, kind)
    before = _snapshot(root)
    inspection_subject.validate_flake_source(root)
    report = json.loads(_run(root).stdout)
    _expect(report[name]["type"] == kind, report)
    _expect(_snapshot(root) == before, "validation changed resource spelling")


@pytest.mark.parametrize("description", [None, "package"])
def test_package_metadata_excludes_dependency_descriptions(
    tmp_path: Path,
    description: str | None,
) -> None:
    """Read only the returned package's description, including curried builders."""
    metadata = "" if description is None else f'meta.description = "{description}";'
    source = (
        '{ pkgs }: let dependency = { meta.description = "dependency"; }; '
        f'in pkgs.runCommand "example" {{ {metadata} }} "touch $out"'
    )
    (tmp_path / "flake.nix").touch()
    package = tmp_path / "example"
    package.mkdir()
    (package / "default.nix").write_text(source)
    resource = json.loads(_run(tmp_path, str(tmp_path)).stdout)["example"]
    expected = {"type": "generic"}
    if description is not None:
        expected["description"] = description
    _expect(resource == expected, resource)


def _inspection_discovery(source: str) -> dict[str, Any]:
    return inspection_subject.ArgumentDiscovery(source).report()


@pytest.mark.parametrize(
    "source",
    [
        "raise RuntimeError('do not execute')\ndef main(): pass",
        "import sys\ndef main(): return sys.version",
        "from .argparse import *\ndef main(): pass",
        """
import argparse
def main():
    argparse.ArgumentParser().parse_args()
""",
    ],
)
def test_argument_free_executables_have_an_empty_argument_list(
    tmp_path: Path, source: str
) -> None:
    """Argument-free scripts need no parser and remain statically inspectable."""
    _inspection_repository(tmp_path)
    (tmp_path / "example/main.py").write_text(source)
    completed = _run(tmp_path, str(tmp_path))
    resource = json.loads(completed.stdout)["example"]
    _expect(resource["arguments"] == [] and "commands" not in resource, resource)


@pytest.mark.parametrize(
    "source",
    [
        "import argparse",
        "from argparse import *\ndef main(): pass",
        "import argparse\ndef unused(): return argparse.ArgumentParser()",
        """\
import argparse
def main():
    argparse.ArgumentParser().parse_args()
from replacement import main
""",
        """
import argparse
import sys
def main():
    preview = argparse.ArgumentParser()
    preview.add_argument('--preview')
    return sys.argv[1:]
""",
        "import click\n@click.command()\ndef main(): pass",
        "import typer\napp = typer.Typer()",
        "import sys\ndef main(): return sys.argv[1:]",
        "import sys as system\ndef main(): return system.argv[1:]",
        "from sys import argv as args\ndef main(): return args[1:]",
    ],
)
def test_afairesi_reports_an_error_for_python_clis_it_cannot_identify(
    tmp_path: Path, source: str
) -> None:
    """Fail inspection when a Python package's interface cannot be discovered."""
    _inspection_repository(tmp_path)
    path = tmp_path / "example/main.py"
    path.write_text(source)
    completed = _run(tmp_path, str(tmp_path), code=1)
    _expect(completed.stdout == "", completed)
    _expect(
        f"{path}: cannot inspect CLI: no supported parser found" in completed.stderr,
        completed,
    )


def test_cli_arguments_are_listed_under_the_command_that_accepts_them() -> None:
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


def test_cli_reports_preserve_nested_and_argument_free_commands() -> None:
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
cli.parse_args()
""")
    _expect(
        report
        == {
            "arguments": [
                {"names": ["--verbose"], "action": "store_true"},
            ],
            "commands": {
                "version": {},
                "remote": {
                    "arguments": [
                        {"names": ["--verbose"], "action": "count"},
                    ],
                    "commands": {
                        "list": {},
                        "add": {"arguments": [{"names": ["url"]}]},
                    },
                },
            },
        },
        "command tree lost scope or empty commands",
    )


def test_unresolved_argument_metadata_is_marked_as_unknown() -> None:
    """Keep unresolved metadata when argument names and scope are known."""
    report = _inspection_discovery("""
from argparse import ArgumentParser as Parser
cli = Parser()
cli.add_argument("--output", default=compute_default(), **options)
cli.parse_args()
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
def test_cli_discovery_rejects_arguments_whose_presence_depends_on_runtime_values(
    declaration: str,
) -> None:
    """Reject uncertain argument membership, including helper and alias dependencies."""
    source = (
        "import argparse\ncli = argparse.ArgumentParser()\n"
        f"{declaration}\ncli.parse_args()"
    )
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
def test_cli_discovery_fails_when_argument_configuration_cannot_be_resolved(
    configure: str,
) -> None:
    """Fail when parser flow escapes the supported helper and method patterns."""
    source = (
        "import argparse\ncli = argparse.ArgumentParser()\n"
        f"{configure}\ncli.parse_args()"
    )
    with pytest.raises(inspection_subject.CommandError, match="cannot inspect CLI"):
        _inspection_discovery(source)


def test_cli_discovery_rejects_executables_with_multiple_public_parsers() -> None:
    """Require an unambiguous root instead of merging distinct command interfaces."""
    source = """
import argparse
one = argparse.ArgumentParser()
one.add_argument('--one')
two = argparse.ArgumentParser()
two.add_argument('--two')
def main():
    one.parse_args()
    two.parse_args()
"""
    with pytest.raises(
        inspection_subject.CommandError,
        match="multiple public parsers",
    ):
        _inspection_discovery(source)


def test_statically_declared_cli_arguments_appear_in_order() -> None:
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
cli.parse_args()
""")
    _expect(
        report
        == {
            "arguments": [
                {"names": ["--enabled"]},
                {"names": ["--one"], "default": {"unknown": "compute_default()"}},
                {"names": ["--two"], "default": {"unknown": "compute_default()"}},
            ],
        },
        report,
    )


def test_cli_reports_include_arguments_declared_in_the_python_main_guard() -> None:
    """Treat the standard main guard as active when describing an executable CLI."""
    report = _inspection_discovery("""
import argparse
if __name__ == '__main__':
    cli = argparse.ArgumentParser()
    cli.add_argument('--public')
    cli.parse_args()
""")
    _expect(report == {"arguments": [{"names": ["--public"]}]}, report)


@pytest.mark.parametrize(
    "expression",
    [
        "(process(path) for path in cli.parse_args().files)",
        "[process(path) for path in cli.parse_args().files]",
        "{process(path) for path in cli.parse_args().files}",
        "{path: process(path) for path in cli.parse_args().files}",
    ],
)
def test_cli_reports_include_arguments_read_as_input_to_a_comprehension(
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
    _expect(report == {"arguments": [{"names": ["files"], "nargs": "+"}]}, report)


@pytest.mark.parametrize(
    "expression",
    [
        "(cli.parse_args() for item in items)",
        "(item for item in items if cli.parse_args())",
        "(item for item in items for path in cli.parse_args().files)",
    ],
)
def test_cli_discovery_rejects_conditional_argument_parsing_inside_comprehensions(
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
def test_cli_reports_include_arguments_read_before_a_loop_body_runs(loop: str) -> None:
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
    _expect(report == {"arguments": [{"names": ["files"], "nargs": "+"}]}, report)


def test_cli_reports_preserve_argument_groups_exclusivity_and_defaults() -> None:
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
cli.parse_args()
""")
    command = report["commands"]["run"]
    _expect(
        command
        == {
            "arguments": [{"names": ["--token"]}, {"names": ["--password"]}],
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


@pytest.mark.parametrize(
    ("entrypoint", "method"),
    [
        ("def main(): start()", "parse_args"),
        ("start()", "parse_intermixed_args"),
        ("run = start\nmain = run", "parse_known_intermixed_args"),
        (
            "def main(): internal.parse_args()\nclass main: pass\nstart()\nmain()",
            "parse_args",
        ),
        ("def main(): start()\nmain()\ndel main", "parse_args"),
    ],
)
def test_cli_reports_omit_arguments_unreachable_from_the_entrypoint(
    entrypoint: str, method: str
) -> None:
    """Follow the executable entry point and ignore an uncalled parser factory."""
    source = f"""
import argparse
cli = argparse.ArgumentParser()
internal = argparse.ArgumentParser()
internal.add_argument('--internal')
def run():
    private = argparse.ArgumentParser()
    other.add_argument('--unused')
    return private
def start():
    cli.add_argument('--public')
    try:
        cli.{method}()
    except ValueError:
        cli.error('invalid input')
"""
    report = _inspection_discovery(source + entrypoint)
    _expect(report == {"arguments": [{"names": ["--public"]}]}, report)


@pytest.mark.parametrize(
    ("source", "reason"),
    [
        (
            "cli = argparse.ArgumentParser()\nother.add_parser('external')",
            "cannot resolve the subcommand parser",
        ),
        ("cli = other", "cannot resolve the public argparse parser"),
        ("cli = argparse.ArgumentParser(**options)", "literal mapping"),
        (
            "cli = argparse.ArgumentParser()\ncli.add_subparsers(**options)",
            "literal mapping",
        ),
        (
            (
                "cli = argparse.ArgumentParser()\n"
                "cli.add_subparsers().add_parser('run', **options)"
            ),
            "literal mapping",
        ),
        (
            (
                "from helpers import CustomParser\ncli = argparse.ArgumentParser()\n"
                "cli.add_subparsers(parser_class=CustomParser).add_parser('run')"
            ),
            "custom subcommand parser",
        ),
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
            "cli = argparse.ArgumentParser()\ncli.add_subparsers().add_parser(name)",
            "subcommand name must be a literal string",
        ),
        (
            (
                "cli = argparse.ArgumentParser()\ncli.add_subparsers()"
                '.add_parser("list", aliases=names)'
            ),
            "subcommand aliases must be literal strings",
        ),
    ],
)
def test_cli_discovery_errors_identify_the_unresolved_expression_and_its_location(
    source: str, reason: str
) -> None:
    """Fail discovery with source context instead of a partial successful report."""
    with pytest.raises(
        inspection_subject.CommandError,
        match=re.escape(reason),
    ) as error:
        inspection_subject.ArgumentDiscovery(
            f"import argparse\n{source}\ncli.parse_args()",
            "example/main.py",
        ).report()
    _expect(
        re.search(r"example/main.py:\d+:\d+:", str(error.value)) is not None,
        "missing source location",
    )
    _expect("\n  " in str(error.value), "missing offending expression")


@pytest.mark.parametrize("constructor", ["argparse.ArgumentParser()", "Parser()"])
def test_cli_discovery_rejects_argparse_interfaces_defined_in_class_methods(
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


def test_subcommand_options_stay_separate_when_helpers_share_a_name() -> None:
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
            "commands": {
                "one": {"arguments": [{"names": ["--one"]}]},
                "two": {"arguments": [{"names": ["--two"]}]},
            },
        },
        report,
    )


def test_cli_discovery_rejects_conflicting_definitions_of_an_argument_helper() -> None:
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


def test_cli_reports_include_arguments_declared_by_nested_helpers() -> None:
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
        report == {"arguments": [{"names": ["--closed-over"]}]},
        report,
    )


def test_unrelated_imports_do_not_change_the_reported_cli() -> None:
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
        report == {"arguments": [{"names": ["--public"]}]},
        report,
    )


def test_cli_discovery_finds_public_arguments_despite_unrelated_recursive_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inspect recursive helpers without enumerating every route through a cycle."""
    calls = "    recurse(value)\n" * 12
    discovery = inspection_subject.ArgumentDiscovery(
        "import argparse\ndef recurse(value):\n" + calls + "    return value\n"
        "def main():\n"
        "    cli = argparse.ArgumentParser()\n"
        "    recurse('unrelated')\n"
        "    cli.add_argument('--public')\n"
        "    cli.parse_args()\n",
    )
    original = discovery._bindings  # noqa: SLF001 - Bound graph traversal work without timing-dependent assertions.
    visits = 0
    limit = 2000

    def bindings(node: ast.Name) -> list[ast.expr]:
        nonlocal visits
        visits += 1
        _expect(visits <= limit, "repeated recursive binding traversal")
        return original(node)

    monkeypatch.setattr(discovery, "_bindings", bindings)
    report = discovery.report()
    _expect(
        report == {"arguments": [{"names": ["--public"]}]},
        report,
    )


def test_parsers_returned_by_recursive_helpers_keep_their_public_arguments() -> None:
    """Keep parser-bearing cycles reachable while excluding unrelated cycles."""
    report = _inspection_discovery("""
import argparse
def forward(value):
    forward(value)
    return value
def main():
    cli = forward(argparse.ArgumentParser())
    cli.add_argument('--public')
    cli.parse_args()
""")
    _expect(
        report == {"arguments": [{"names": ["--public"]}]},
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
def test_cli_discovery_rejects_parsers_whose_argparse_import_has_been_shadowed(
    source: str,
) -> None:
    """Do not treat reassigned names or parameters as their shadowed imports."""
    with pytest.raises(inspection_subject.CommandError, match="cannot resolve"):
        _inspection_discovery(source)


def test_cli_discovery_rejects_external_replacements_for_argument_helpers() -> None:
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


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_cli_reports_preserve_unicode_arguments_across_line_endings(
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
        },
        report,
    )


def test_cli_reports_read_option_names_and_defaults_in_the_declared_python_encoding(
    root: Path,
) -> None:
    """Respect the Python encoding declaration in validation and CLI discovery."""
    _fixture_add(root, "example", "python")
    (root / "example/main.py").write_bytes(
        """# coding: latin-1
import argparse
label = 'é'; cli = argparse.ArgumentParser()
cli.add_argument('--résumé', default='é')
cli.parse_args()
""".encode("latin-1")
    )
    inspection_subject.validate_flake_source(root)
    resource = json.loads(_run(root).stdout)["example"]
    _expect(
        resource["arguments"] == [{"names": ["--résumé"], "default": "é"}], resource
    )


def test_command_aliases_expose_the_same_interface_as_their_canonical_command() -> None:
    """Expose aliases with the same interface as their canonical command."""
    report = _inspection_discovery("""
import argparse
cli = argparse.ArgumentParser(**{'parents': []})
commands = cli.add_subparsers()
listing = commands.add_parser(name='list', **{'aliases': ['ls']})
listing.add_argument('--all', action='store_true')
listing.add_subparsers().add_parser('files', aliases=['f'])
cli.parse_args()
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


def test_cli_reports_preserve_required_arguments_declared_by_helpers() -> None:
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
    cli.parse_args()
""")
    _expect(
        report["arguments"] == [{"names": ["--required"], "required": True}],
        "factory or keyword binding was lost",
    )


@pytest.mark.parametrize("entrypoint", ["parser().parse_args()", "parse(parser())"])
def test_cli_reports_exclude_internal_commands_and_arguments(
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
import argparse
raise RuntimeError("inspection must not execute this")
def parser():
    cli = argparse.ArgumentParser()
    cli.add_argument("--output", default="result")
    return cli
def main():
    parser().parse_args()
""",
    )
    (package / "test_main.py").write_text(
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
    (library / "default.nix").write_text("{}")


def test_a_package_enters_the_inventory_when_git_tracks_its_declaration(
    root: Path,
) -> None:
    """Discover a package when its declaration joins its tracked supporting files."""
    storage = root / "tmp"
    storage.mkdir()
    (storage / "default.nix").write_text("{}")
    (storage / "main.py").write_text("def main(): pass\n")
    _git(root, "add", "--force", "--", "tmp/main.py")
    before = inspection_subject.inspect_repository(root)
    _expect(before == {}, before)
    _git(root, "add", "--force", "--", "tmp/default.nix")
    after = inspection_subject.inspect_repository(root)
    _expect(after == {"tmp": {"type": "generic"}}, after)


def test_inventory_lists_hosts_and_packages_without_running_or_modifying_source(
    tmp_path: Path,
) -> None:
    """Inspect source containing failing statements without executing or writing."""
    _inspection_repository(tmp_path)
    before = _snapshot(tmp_path)
    report = inspection_subject.inspect_repository(tmp_path)
    _expect(_snapshot(tmp_path) == before, "inspection wrote to the repository")
    _expect(list(report) == ["example", "laptop", "library"], report)
    _expect(report["example"]["type"] == "python", report)
    _expect(
        set(report["example"]) == {"type", "description", "arguments", "tests"},
        report,
    )
    _expect(
        report["laptop"] == {"type": "host"},
        "wrong hosts",
    )
    _expect(
        report["library"] == {"type": "generic"},
        "empty optional fields leaked into the package inventory",
    )
    _expect(report["example"]["tests"] == ["test_default_output"], report)
    _expect(report["example"]["description"] == "Example", report)


def test_library_inventory_includes_metadata_and_tests_without_a_cli(
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
let python = pkgs.python3;
in inputs.self.lib.mkPythonPackage {
    inherit pkgs;
    executable = false;
    meta.description = "Python library";
    src = ./.;
}
""",
    )
    source = package / "main.py"
    source.write_text(source.read_text().replace("def main():", "def main(values):"))
    inspection_subject.validate_flake_source(tmp_path)
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
    definition.write_text(
        definition.read_text().replace("executable = false", "executable = true")
    )
    with pytest.raises(
        inspection_subject.CommandError,
        match="main must accept a call without arguments",
    ):
        inspection_subject.validate_flake_source(tmp_path)


def test_the_package_executable_setting_determines_whether_to_list_its_cli(
    tmp_path: Path,
) -> None:
    """Read executable metadata from the outer package rather than dependencies."""
    _inspection_repository(tmp_path)
    (tmp_path / "example/default.nix").write_text(
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
        report["arguments"] == [{"names": ["--output"], "default": "result"}],
        report,
    )


def test_afairesi_reports_cli_errors_on_stderr_without_emitting_partial_json(
    tmp_path: Path,
) -> None:
    """Surface discovery errors on stderr with a failing status and no JSON output."""
    _inspection_repository(tmp_path)
    source = tmp_path / "example/main.py"
    source.write_text("import argparse\nother.add_argument('--missing')\n")
    completed = _run(tmp_path, str(tmp_path), code=1)
    _expect(completed.stdout == "", completed)
    _expect(f"{source}:2:1: cannot inspect CLI:" in completed.stderr, completed)
    _expect("cannot resolve the argument parser" in completed.stderr, completed)
    _expect("other.add_argument('--missing')" in completed.stderr, completed)
    _expect("Traceback" not in completed.stderr, completed)


def test_cli_reports_preserve_empty_commands_and_falsy_argument_defaults(
    tmp_path: Path,
) -> None:
    """Keep empty commands, aliases, and literal defaults when pruning containers."""
    _inspection_repository(tmp_path)
    (tmp_path / "example/main.py").write_text(
        """
import argparse
cli = argparse.ArgumentParser()
commands = cli.add_subparsers()
commands.add_parser('empty', aliases=['e'])
cli.set_defaults(scope='root')
run = commands.add_parser('run')
run.add_argument('--values', default=[])
run.add_argument('--mapping', default={})
run.add_argument('--optional', default=None)
run.add_argument('--enabled', default=False)
run.add_argument('--count', default=0)
run.add_argument('--name', default='')
run.set_defaults(internal=None)
cli.parse_args()
""",
    )
    report = inspection_subject.inspect_repository(tmp_path)["example"]
    _expect(
        report
        == {
            "type": "python",
            "description": "Example",
            "tests": ["test_default_output"],
            "defaults": {"scope": "root"},
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


@pytest.mark.parametrize("in_git", [False, True], ids=["outside", "git"])
def test_afairesi_prints_the_current_repository_inventory_by_default_or_by_path(
    tmp_path: Path,
    *,
    in_git: bool,
) -> None:
    """Return the same JSON inventory through supported paths inside and outside Git."""
    _inspection_repository(tmp_path)
    expected = inspection_subject.inspect_repository(tmp_path)
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
    for cwd, arguments in calls:
        completed = _run(cwd, *arguments)
        _expect(
            json.loads(completed.stdout) == expected,
            "path resolution changed the CLI inventory",
        )


def test_inventory_reports_a_missing_flake_or_malformed_python_source(
    tmp_path: Path,
) -> None:
    """Report invalid repositories and malformed source with useful errors."""
    with pytest.raises(inspection_subject.CommandError, match=r"flake\.nix"):
        inspection_subject.inspect_repository(tmp_path)
    _inspection_repository(tmp_path)
    (tmp_path / "example" / "main.py").write_text("def invalid(")
    with pytest.raises(
        inspection_subject.CommandError,
        match="could not parse Python source",
    ):
        inspection_subject.inspect_repository(tmp_path)


@pytest.fixture
def remote_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[Path, str]]:
    """Serve a local Git remote and check that inspections clean up after themselves."""
    home, source = tmp_path / "home", tmp_path / "source"
    scratch = tmp_path / "temporary"
    scratch.mkdir()
    home.mkdir()
    source.mkdir()
    _committed_repository(source)
    remote = "https://example.test/team/repository.git"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TMPDIR", str(scratch))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{source.as_uri()}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", remote)
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")
    yield source, remote
    _expect(not list(scratch.iterdir()), "remote inspection left temporary files")
    _expect(not list(home.iterdir()), "remote inspection changed HOME")


def test_remote_inventory_reflects_new_commits_on_each_run(
    tmp_path: Path,
    remote_repository: tuple[Path, str],
) -> None:
    """Inspect fresh remote contents without creating persistent home state."""
    source, remote = remote_repository
    _expect(
        json.loads(_run(tmp_path, remote).stdout)
        == inspection_subject.inspect_repository(source),
        "wrong remote inventory",
    )
    (source / "new").mkdir()
    (source / "new/configuration.nix").touch()
    _git(source, "add", "new")
    _fixture_git(source, "commit", "--quiet", "-m", "add host")
    _expect(
        json.loads(_run(tmp_path, remote).stdout)
        == inspection_subject.inspect_repository(source),
        "remote inspection was stale",
    )


def test_afairesi_reports_invalid_remote_python_source_without_emitting_partial_json(
    tmp_path: Path,
    remote_repository: tuple[Path, str],
) -> None:
    """Report parse failures from a cloned repository without emitting partial JSON."""
    source, remote = remote_repository
    (source / "example/main.py").write_text("def invalid(")
    _git(source, "add", "example/main.py")
    _fixture_git(source, "commit", "--quiet", "-m", "malformed source")
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
    module = source / "example/main.py"
    module.write_text(module.read_text().replace('default="result"', 'default="local"'))
    before = _snapshot(tmp_path)
    local_report = json.loads(_run(tmp_path, str(source)).stdout)
    _expect(
        local_report["example"]["arguments"][0]["default"] == "local",
        local_report,
    )
    remote_report = json.loads(_run(tmp_path, source.as_uri()).stdout)
    _expect(
        remote_report["example"]["arguments"][0]["default"] == "result",
        remote_report,
    )
    _expect(
        _snapshot(tmp_path) == before,
        "inspection changed source or Git state",
    )


@pytest.mark.usefixtures("remote_repository")
def test_afairesi_forwards_the_error_and_exit_status_from_a_failed_remote_clone(
    tmp_path: Path,
) -> None:
    """Compare failed remote inspection with Git's exit status and diagnostics."""
    remote = (tmp_path / "missing.git").as_uri()
    scratch = Path(os.environ["TMPDIR"])
    native_checkout = tmp_path / "native-checkout"
    expected = subprocess.run(  # noqa: S603 - Fixed Git executable with test-controlled arguments.
        ["git", "clone", "--", remote, str(native_checkout)],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    actual = _run(tmp_path, remote, code=expected.returncode)
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
    _fixture_git(root, "commit", "--quiet", "-m", "initial")


def _diff_description(root: Path, description: str) -> None:
    """Change a public field without changing resource identity."""
    (root / "example/default.nix").write_text(
        '{ meta.description = "' + description + '"; }',
    )


def _apply_diff(
    before: dict[str, Any],
    difference: str,
) -> dict[str, Any]:
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
    return cast("dict[str, Any]", json.loads(completed.stdout))


def test_diff_emits_a_jd_patch_from_index_to_working_tree(tmp_path: Path) -> None:
    """Use the staged interface as the baseline even from a nested directory."""
    _committed_repository(tmp_path)
    _diff_description(tmp_path, "Staged")
    _git(tmp_path, "add", ".")
    index = inspection_subject.inspect_repository(tmp_path)
    _diff_description(tmp_path, "Working")
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
    """Emit nothing for changes outside the tracked interface contract."""
    _committed_repository(tmp_path)
    source = tmp_path / "example/main.py"
    source.write_text(
        '"""Implementation notes."""\n'
        + source.read_text()
        + "\ndef internal(): return 42\n"
    )
    tests = tmp_path / "example/test_main.py"
    tests.write_text(
        'def test_default_output():\n    """Updated notes."""\n'
        '    raise RuntimeError("changed")\n',
    )
    (tmp_path / "untracked").mkdir()
    (tmp_path / "untracked/configuration.nix").touch()
    before = _snapshot(tmp_path)
    completed = _run(tmp_path, "diff")
    _expect(not completed.stdout and not completed.stderr, completed)
    _expect(_snapshot(tmp_path) == before, "empty diff changed state")


def test_diff_analyzes_each_distinct_entrypoint_once_across_snapshots_and_runs(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Analyze identical entrypoints once and invalidate by their actual bytes."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    _inspection_repository(root)
    _git(root, "add", ".")
    analyze = inspection_subject.ArgumentDiscovery.report
    calls = []

    def count(discovery: inspection_subject.ArgumentDiscovery) -> dict[str, Any]:
        calls.append(discovery.filename)
        return analyze(discovery)

    monkeypatch.setattr(inspection_subject.ArgumentDiscovery, "report", count)
    monkeypatch.chdir(root)
    monkeypatch.setattr(sys, "argv", ["afairesi", "diff"])
    inspection_subject.main()
    inspection_subject.main()
    _expect(len(calls) == 1, calls)
    calls.clear()
    source = root / "example/main.py"
    source.write_text(source.read_text().replace("--output", "--user"))
    inspection_subject.main()
    _expect(len(calls) == 1, calls)


def test_diff_includes_tracked_targets_of_symlinked_entrypoints(tmp_path: Path) -> None:
    """Keep deeply nested symlink targets in both snapshots."""
    _committed_repository(tmp_path)
    source = tmp_path / "example/main.py"
    target = tmp_path / "assets/deep/entry"
    target.parent.mkdir(parents=True)
    source.rename(target)
    source.symlink_to("../assets/deep/entry")
    _git(tmp_path, "add", ".")
    before = inspection_subject.inspect_repository(tmp_path)
    target.write_text(target.read_text().replace("--output", "--user"))
    after = inspection_subject.inspect_repository(tmp_path)
    _expect(before != after, after)
    _expect(_apply_diff(before, _run(tmp_path, "diff").stdout) == after, after)


def test_diff_reconstructs_changes_to_hosts_tests_and_subcommand_arguments(
    tmp_path: Path,
) -> None:
    """Round-trip resource changes, nested arguments, and lists with jd context."""
    _committed_repository(tmp_path)
    before = inspection_subject.inspect_repository(tmp_path)
    source = tmp_path / "example/main.py"
    source.write_text(
        source.read_text().replace(
            "    return cli",
            "    cli.add_argument('--verbose', action='store_true')\n"
            "    command = cli.add_subparsers().add_parser('run')\n"
            "    command.add_argument('paths', nargs='+')\n"
            "    return cli",
        ),
    )
    (tmp_path / "example/test_main.py").write_text("def test_new(): pass\n")
    (tmp_path / "laptop/configuration.nix").unlink()
    after = inspection_subject.inspect_repository(tmp_path)
    completed = _run(tmp_path, "diff")
    _expect(_apply_diff(before, completed.stdout) == after, completed)


def test_removing_a_package_drops_its_references_and_preserves_a_colocated_host(
    root: Path,
) -> None:
    """Remove packages and their dependency edges while retaining a colocated host."""
    _fixture_add(root, "app", "python")
    _fixture_add(root, "shared", "host")
    (root / "shared/default.nix").write_text("{}")
    _fixture_add(root, "consumer", "generic")
    (root / "consumer/default.nix").write_text("{ sources = [ ../app ../shared ]; }")
    _git(root, "add", "--", "consumer", "shared")
    before = json.loads(_run(root).stdout)
    for name in ("app", "shared"):
        (root / name / "default.nix").unlink()
    after = json.loads(_run(root).stdout)
    _expect(
        after
        == {
            "consumer": {"type": "generic", "dependencies": {"shared": ["source"]}},
            "shared": {"type": "host"},
        },
        after,
    )
    _expect(_apply_diff(before, _run(root, "diff").stdout) == after, after)
    with pytest.raises(
        inspection_subject.CommandError, match="missing required tracked regular file"
    ) as rejected:
        inspection_subject.validate_flake_source(root)
    for name in ("app", "shared"):
        _expect(
            f"{name}/default.nix: missing required tracked regular file"
            in str(rejected.value),
            rejected.value,
        )


def test_diff_uses_the_index_before_the_first_commit(tmp_path: Path) -> None:
    """Compare against the index even when the repository has no HEAD."""
    _git(tmp_path, "init", "--quiet")
    _expect(not _run(tmp_path, "diff").stdout, "nonempty diff")
    _inspection_repository(tmp_path)
    _git(tmp_path, "add", ".")
    staged = inspection_subject.inspect_repository(tmp_path)
    _diff_description(tmp_path, "Unstaged")
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
    (root / "example/main.py").write_text("def invalid(")
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
    """Reject conflict stages instead of emitting an incomplete interface diff."""
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
    _diff_description(linked, "Linked")
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


def test_afairesi_lists_its_repository_argument_and_argument_free_diff() -> None:
    """Expose the repository argument only on the top-level inspection command."""
    report = inspection_subject.ArgumentDiscovery(
        Path(__file__).with_name("main.py").read_bytes(),
    ).report()
    _expect(report["commands"] == {"diff": {}}, report["commands"])
    _expect(
        [row["names"] for row in report["arguments"]] == [["repository"]],
        report["arguments"],
    )


@pytest.mark.parametrize("kind", ["python", "generic", "lean4"])
def test_source_validation_ignores_untracked_packages_and_test_files(
    tmp_path: Path,
    kind: str,
) -> None:
    """Keep unselected sources out of validation and inspection."""
    root = tmp_path / "project"
    root.mkdir()
    _repository(root)
    _fixture_add(root, "example", kind)
    extension = {"python": "py", "generic": "nix", "lean4": "lean"}[kind]
    tests = f"example/test_main.{extension}"
    (root / tests).write_bytes(b"def broken(:")
    local = root / "experiments"
    local.mkdir()
    (local / "default.nix").write_text("{ invalid")
    (local / "main.py").write_bytes(b"def broken(:")
    before = _snapshot(root)
    inspection_subject.validate_flake_source(root)
    _expect(_snapshot(root) == before, "validation admitted or changed local sources")
    report = inspection_subject.inspect_repository(root)
    _expect(set(report) == {"example"} and "tests" not in report["example"], report)


def test_inventory_groups_package_and_host_dependencies_by_role(
    tmp_path: Path,
) -> None:
    """Combine a package and host's local dependency roles without evaluating Nix."""
    root = _repository(tmp_path)
    for name in ("consumer", "library", "other", "tool", "helper"):
        directory = root / name
        directory.mkdir()
        (directory / "default.nix").write_text("{}: {}")
        (directory / "main.nix").write_text("{}")
    source = """{ inputs, pkgs, ... }:
      let inherit (pkgs.stdenv.hostPlatform) system;
          local = inputs.self.packages.${system};
          deps = [ local.library local.library ];
      in pkgs.runCommand "consumer" {
        propagatedBuildInputs = deps /* base */ ++
          ( /* extra */ with inputs.self.packages.${system}; [ other ]);
        nativeCheckInputs = [ inputs.self /* local */ .packages.${system}.library ];
        hook = local: { checkInputs = [ local.other ]; };
        buildInputs = [ inputs.external.packages.${system}.other ];
        text = "inputs.self.packages.fake.consumer";
        tool = local.tool;
        src = ../other/main.nix;
        root = ../.;
      } "throw must-not-evaluate"
    """
    (root / "consumer/default.nix").write_text(source)
    (root / "consumer/configuration.nix").write_text("""
      { inputs, system, ... }: let local = inputs.self.packages.${system}; in {
        environment.systemPackages = [ local.tool ];
        settings = [ local.library local.helper local.consumer ];
      }
    """)
    _git(root, "add", ".")
    report = inspection_subject.inspect_repository(root)
    _expect(report["consumer"]["type"] == ["generic", "host"], report)
    _expect(
        report["consumer"]["dependencies"]
        == {
            "helper": ["reference"],
            "library": ["runtime", "test"],
            "other": ["runtime", "source"],
            "tool": ["runtime"],
        },
        report,
    )
    _expect("dependencies" not in report["library"], report)
    (root / "consumer/default.nix").write_text("\n# moved declaration\n" + source)
    _expect(
        inspection_subject.inspect_repository(root) == report,
        "line numbers leaked into the interface",
    )


def test_cli_discovery_ignores_nested_aliases_unrelated_to_arguments() -> None:
    """Traverse shared containers once while finding actual parser arguments."""
    source = "import argparse\nvalue0 = []\n"
    for index in range(1, 24):
        source += f"value{index} = [value{index - 1}, value{index - 1}]\n"
    source += "consume(value23)\nparser = argparse.ArgumentParser()\n"
    source += "parser.add_argument('--name')\nparser.parse_args()\n"
    report = _inspection_discovery(source)
    _expect(report["arguments"] == [{"names": ["--name"]}], report)

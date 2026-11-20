# Copyright (c) 2026- Paschalis Bizopoulos
"""Check repository inspection, diffs, and source validation."""

import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import afairesi as inspection_subject
import nix_syntax

if TYPE_CHECKING:
    from collections.abc import Iterator


_ENTRYPOINTS = {
    "bash": ("main.sh", "echo hello\n"),
    "html": ("index.html", "<!doctype html><html><body></body></html>\n"),
    "latex": (
        "ms.tex",
        "\\documentclass{article}\n\\begin{document}\nHello.\n\\end{document}\n",
    ),
    "lean4": ("main.lean", "def main : IO Unit := pure ()\n"),
    "python": ("main.py", "def main():\n    pass\n"),
}


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(  # noqa: S603 - Fixed Git executable with test-controlled arguments.
        ["git", *arguments],  # noqa: S607
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    ).stdout


def _fixture_add(
    root: Path,
    name: str,
    kind: str,
) -> None:
    """Write the minimal tracked source needed by a test."""
    sources = {}
    if kind in _ENTRYPOINTS:
        filename, content = _ENTRYPOINTS[kind]
        sources[filename] = content
    if kind == "host":
        sources["configuration.nix"] = "{ ... }: {}\n"
    else:
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


def test_inspection_lists_sorted_test_names_before_implementation(
    root: Path,
) -> None:
    """List declarations without importing the suite or requiring an implementation."""
    _fixture_add(root, "inbox", "python")
    (root / "inbox/main.py").unlink()
    (root / "inbox/test_main.py").write_text(
        "import pytest\n"
        "raise RuntimeError('inspection must not import this suite')\n"
        "@pytest.mark.parametrize('message', ['abc', 'ABC'])\n"
        "def test_preserves_message_case(message): assert deliver(message) == message\n"
        "def test_delivers_messages(): assert deliver('hello') == 'hello'\n"
    )
    _git(root, "add", "--", "inbox/test_main.py")
    report = inspection_subject.inspect_repository(root)
    _expect(
        report == {"inbox": ["test_delivers_messages", "test_preserves_message_case"]},
        report,
    )


def test_each_package_lists_a_shared_behavior_name_once(root: Path) -> None:
    """Keep equal identifiers distinct across packages and unique within a suite."""
    for name in ("mail_box", "mail-box"):
        _fixture_add(root, name, "python")
        tests = root / name / "test_main.py"
        tests.write_text("def test_delivers(): ...\ndef test_delivers(): ...\n")
        _git(root, "add", "--", f"{name}/test_main.py")
    report = inspection_subject.inspect_repository(root)
    _expect(list(report) == ["mail-box", "mail_box"], report)
    _expect(
        report == {"mail-box": ["test_delivers"], "mail_box": ["test_delivers"]}, report
    )
    with pytest.raises(ValueError, match="module names collide"):
        inspection_subject.validate_flake_source(root)


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
    root: Path,
    kind: str,
    constructor: str,
) -> None:
    """Keep the declared build kind when supporting files resemble entrypoints."""
    package = root / "example"
    package.mkdir()
    executable = "executable = true;" if kind in {"lean4", "python"} else ""
    (package / "default.nix").write_text(
        "{ inputs, pkgs, ... }: let dependency = inputs.self.lib.mkHtmlPackage {}; in "
        f"{constructor} {{ inherit pkgs; src = ./.; {executable} }}",
    )
    if kind != "python":
        (package / "test_main.py").write_text("def test_unrelated(): pass")
    for filename, content in _ENTRYPOINTS.values():
        (package / filename).write_text(content)
    (package / "main.nix").touch()
    _git(root, "add", "--", "example")
    report = inspection_subject.inspect_repository(root)
    _expect(report == {"example": []}, report)
    inspection_subject.validate_flake_source(root)
    entrypoint, _ = _ENTRYPOINTS[kind]
    (package / entrypoint).unlink()
    with pytest.raises(ValueError, match=re.escape(entrypoint)):
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
    with pytest.raises(ValueError, match=r"__init__\.py: reserved"):
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
    with pytest.raises(
        nix_syntax.NixSyntaxError, match=rf"{re.escape(relative)}:\d+:\d+: parse error"
    ):
        inspection_subject.validate_flake_source(root)


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
    report = inspection_subject.inspect_repository(root)
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
    declared = inspection_subject.inspect_repository(tmp_path)["example"]
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
    report = inspection_subject.inspect_repository(root)
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
    (package / "main.py").write_text("def main(): pass\n")
    _write_test(root, "default_output")


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
    _expect(
        inspection_subject.inspect_repository(root) == {},
        "untracked declaration was listed",
    )
    _git(root, "add", "--", "draft/default.nix")
    _expect(
        inspection_subject.inspect_repository(root) == {"draft": ["testDelivers"]},
        package,
    )
    _git(root, "mv", "--", "draft", "inbox")
    _expect(
        inspection_subject.inspect_repository(root) == {"inbox": ["testDelivers"]}, root
    )


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
        ValueError,
        match="main must accept a call without arguments",
    ):
        inspection_subject.validate_flake_source(tmp_path)


def test_specification_reports_a_missing_flake_or_malformed_test_suite(
    tmp_path: Path,
) -> None:
    """Report invalid repositories and malformed source with useful errors."""
    with pytest.raises(ValueError, match=r"flake\.nix"):
        inspection_subject.inspect_repository(tmp_path)
    _inspection_repository(tmp_path)
    (tmp_path / "example" / "test_main.py").write_text("def invalid(")
    with pytest.raises(
        ValueError,
        match="could not parse Python source",
    ):
        inspection_subject.inspect_repository(tmp_path)


def _indexed_repository(root: Path) -> None:
    """Stage the sources used as the diff baseline."""
    _inspection_repository(root)
    _git(root, "init", "--quiet")
    _git(root, "add", ".")


def _write_test(root: Path, behavior: str) -> None:
    """Give the fixture's sole Python test a new name."""
    (root / "example/test_main.py").write_text(f"def test_{behavior}(): pass\n")


def test_diff_ignores_implementation_edits_and_untracked_resources(
    tmp_path: Path,
) -> None:
    """Only changes to the package-to-test-name mapping produce output."""
    _inspection_repository(tmp_path)
    _git(tmp_path, "init", "--quiet")
    _expect(not inspection_subject.diff_repository(tmp_path), "untracked files in diff")
    _git(tmp_path, "add", ".")
    source = tmp_path / "example/main.py"
    source.write_text("def unfinished(")
    declaration = tmp_path / "example/default.nix"
    declaration.write_text(
        declaration.read_text().replace("Example", "Updated description")
    )
    tests = tmp_path / "example/test_main.py"
    tests.write_text(
        'def test_default_output():\n    """Updated notes."""\n'
        '    raise RuntimeError("changed")\n',
    )
    (tmp_path / "untracked").mkdir()
    (tmp_path / "untracked/default.nix").write_text("{}")
    before = _snapshot(tmp_path)
    difference = inspection_subject.diff_repository(tmp_path)
    _expect(not difference, difference)
    _expect(_snapshot(tmp_path) == before, "empty diff changed state")


def test_diff_follows_tracked_symlink_targets_of_test_suites(tmp_path: Path) -> None:
    """Keep deeply nested symlink targets in both snapshots."""
    _indexed_repository(tmp_path)
    source = tmp_path / "example/test_main.py"
    target = tmp_path / "assets/deep/entry"
    target.parent.mkdir(parents=True)
    source.rename(target)
    source.symlink_to("../assets/deep/entry")
    _git(tmp_path, "add", ".")
    target.write_text("def test_unicode_input(): ...\ndef test_empty_input(): ...\n")
    changes = inspection_subject.diff_repository(tmp_path)
    _expect(
        changes
        == [
            {
                "package": "example",
                "status": "modified",
                "added": ["test_empty_input", "test_unicode_input"],
                "removed": ["test_default_output"],
            }
        ],
        changes,
    )


def test_removing_a_package_drops_its_entry_even_when_it_shares_a_host(
    root: Path,
) -> None:
    """Drop deleted declarations and keep diagnosing them in layout validation."""
    _fixture_add(root, "shared", "host")
    (root / "shared/default.nix").write_text("{}")
    _git(root, "add", "--", "shared/default.nix")
    (root / "shared/default.nix").unlink()
    changes = inspection_subject.diff_repository(root)
    _expect(
        changes
        == [{"package": "shared", "status": "removed", "added": [], "removed": []}],
        changes,
    )
    with pytest.raises(
        ValueError,
        match=r"shared/default\.nix: missing required tracked regular file",
    ):
        inspection_subject.validate_flake_source(root)


def test_diff_errors_clean_up_temporary_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clean failed snapshots when a tracked source cannot be inspected."""
    root = tmp_path / "repository"
    root.mkdir()
    _indexed_repository(root)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    (root / "example/test_main.py").write_text("def invalid(")
    before = _snapshot(root)
    with pytest.raises(ValueError, match="could not parse Python source"):
        inspection_subject.diff_repository(root)
    _expect(not list(scratch.iterdir()), "failed diff left temporary sources")
    _expect(_snapshot(root) == before, "failed diff changed state")


def test_diff_reports_unresolved_merge_conflicts_and_leaves_the_index_untouched(
    tmp_path: Path,
) -> None:
    """Report the conflicting index before producing a test-name diff."""
    _indexed_repository(tmp_path)
    blob = _git(tmp_path, "rev-parse", ":example/main.py").strip()
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
    with pytest.raises(ValueError, match="unmerged index"):
        inspection_subject.diff_repository(tmp_path)
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
    _indexed_repository(root)
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
        "initial",
    )
    _git(root, "worktree", "add", "--quiet", "--detach", str(linked), "HEAD")
    _write_test(linked, "staged")
    _git(linked, "add", "example/test_main.py")
    _write_test(linked, "working")
    before = _snapshot(tmp_path)
    changes = inspection_subject.diff_repository(linked)
    _expect(
        changes
        == [
            {
                "package": "example",
                "status": "modified",
                "added": ["test_working"],
                "removed": ["test_staged"],
            }
        ],
        changes,
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


def test_inspection_extracts_named_cases_from_tracked_suites(root: Path) -> None:
    """Read Bats, Nix, and Lean declarations once their suites are staged."""
    expected = {}
    for kind, suite, source, names in [
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
    ]:
        _fixture_add(root, kind, kind)
        (root / kind / suite).write_text(source)
        expected[kind] = names
    untracked = inspection_subject.inspect_repository(root)
    _expect(untracked == {kind: [] for kind in expected}, untracked)
    _git(root, "add", "--", *expected)
    report = inspection_subject.inspect_repository(root)
    _expect(report == expected, report)


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
    before = inspection_subject.inspect_repository(root)
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
    changes = inspection_subject.diff_repository(root)
    _expect(
        changes
        == [
            {
                "package": "example",
                "status": "modified",
                "added": ["tests::shared::nested::retains_spaces"],
                "removed": ["tests::shared::nested::preserves_spaces"],
            }
        ],
        changes,
    )

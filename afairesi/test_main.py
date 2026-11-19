# Copyright (c) 2026- Paschalis Bizopoulos
"""Check Afairesi's CLI and source validation."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Any, cast

import pytest

import afairesi as inspection_subject

if TYPE_CHECKING:
    import ast
    from collections.abc import Iterator


def _run(
    root: Path,
    *arguments: str,
    code: int = 0,
    executable: str | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(  # noqa: S603 - Test harness selects the executable and fixture arguments.
        [
            executable or os.environ["PACKAGE_E2E_EXECUTABLE"],
            *arguments,
        ],
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
        raise AssertionError(
            msg,
        )
    return result


def _check_flake_source(
    root: Path, *, code: int = 0
) -> subprocess.CompletedProcess[str]:
    """Exercise the source-only validator used by the structure check."""
    return _run(
        root,
        "-c",
        "from afairesi import check_source_main; check_source_main()",
        str(root),
        code=code,
        executable=sys.executable,
    )


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
def test_source_validation_respects_excluded_trees(
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
    for name in (".gitignore", "flake.nix", "flake.lock", "LICENSE"):
        (root / name).write_text("", encoding="utf-8")
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


def test_inspect_uses_tracked_names_after_git_move(tmp_path: Path) -> None:
    """Report the renamed package and its tests from the Git index."""
    root = _repository(tmp_path)
    _fixture_add(root, "example", "generic")
    (root / "example/test_main.nix").write_text('throw "must not evaluate"')
    _git(root, "add", "--", "example/test_main.nix")
    _git(root, "mv", "--", "example", "renamed")
    report = json.loads(_run(root).stdout)
    _expect(set(report) == {"renamed"}, report)
    _expect(report["renamed"]["tests"] == ["test_main.nix"], report)


@pytest.mark.parametrize("filename", ["main.nix", "test_main.nix"])
def test_nix_optional_sources_are_syntax_checked(root: Path, filename: str) -> None:
    """Reject malformed optional Nix files during source validation."""
    _fixture_add(root, "example", "generic")
    (root / "example" / filename).write_text("{ invalid = ; }\n")
    _git(root, "add", "--", f"example/{filename}")
    result = _check_flake_source(root, code=1)
    _expect(f"example/{filename}:" in result.stderr, result)
    _expect("parse error" in result.stderr, result)


def test_cli_help_lists_current_commands_and_rejects_invalid_requests(
    tmp_path: Path,
) -> None:
    """Expose the CLI and reject invalid requests outside a repository."""
    before = _snapshot(tmp_path)
    help_output = _run(tmp_path, "--help").stdout
    _expect("usage:" in help_output, help_output)
    _expect("[PATH_OR_REMOTE]" in help_output, help_output)
    _expect("  diff " in help_output, help_output)
    option = _run(tmp_path, "diff", "--help").stdout
    _expect("usage: afairesi diff" in option, option)
    _expect("--cached" in option and "jd-compatible" in option, option)
    _expect("--color | less -R" in option, option)
    _expect("| jd -t jd2patch | jless" in option, option)
    for arguments in (("--unknown",), (".", "extra"), ("--cached",)):
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
def test_declared_package_kind_allows_supporting_entrypoint_names(
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
    _check_flake_source(root)
    (package / entrypoints[kind]).unlink()
    rejected = _check_flake_source(root, code=1)
    _expect(entrypoints[kind] in rejected.stderr, rejected)


@pytest.mark.parametrize("kind", ["bash", "html", "latex", "lean4", "generic"])
def test_inspect_selects_tests_by_package_kind(root: Path, kind: str) -> None:
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


def test_python_initializer_collision_is_rejected_before_build(root: Path) -> None:
    """Reject a selected initializer without changing source or installing over main."""
    _fixture_add(root, "example", "python")
    initializer = root / "example/__init__.py"
    initializer.write_text('"""Personal initialization."""\n')
    _git(root, "add", "example/__init__.py")
    before = _snapshot(root)
    result = _check_flake_source(root, code=1)
    _expect("__init__.py: reserved" in result.stderr, result)
    _expect(_snapshot(root) == before, "rejection changed sources")


@pytest.mark.parametrize(
    "expression",
    [
        "pkgs.python3.pkgs.buildPythonPackage { src = ./.; }",
        (
            "let executable = true; args = { inherit pkgs executable; src = ./.; }; "
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
    root: Path,
    expression: str,
) -> None:
    """Accept Nix expressions without rewriting builders or evaluating source."""
    _fixture_add(root, "example", "python")
    definition = root / "example/default.nix"
    definition.write_text("{ inputs, pkgs, ... }: " + expression + "\n")
    _git(root, "add", ".")
    before = _snapshot(root)
    _check_flake_source(root)
    _expect(_snapshot(root) == before, "validation changed repository")


@pytest.mark.parametrize("relative", ["flake.nix", "example/default.nix"])
def test_source_validation_rejects_malformed_nix(root: Path, relative: str) -> None:
    """Separate expression flexibility from Nix syntax errors."""
    _fixture_add(root, "example", "python")
    (root / relative).write_text("{ inputs, pkgs, ... }: (\n")
    before = _snapshot(root)
    result = _check_flake_source(root, code=1)
    _expect("parse error" in result.stderr, result)
    _expect(relative in result.stderr, result)
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
def test_generic_builders_do_not_inherit_language_conventions(
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
    _check_flake_source(root)
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


@pytest.mark.parametrize(
    "source",
    [
        "def testCamelCase(): pass\ndef test(): pass\n",
        "class TestBehavior:\n    def testCamelCase(self): pass\n",
        (
            "from unittest import TestCase\n"
            "class Reports(TestCase):\n    def testCamelCase(self): pass\n"
        ),
        (
            "class Cases:\n    def test_result(self): pass\n"
            "class Intermediate(Cases): pass\n"
            "class TestBehavior(Intermediate): pass\n"
        ),
        (
            "class Cases:\n    def test_result(self): pass\n"
            "class TestBehavior(Cases):\n"
            "    test_result = None\n"
            "    def test_visible(self): pass\n"
        ),
        (
            "import unittest as unit\n"
            "class Reports(unit.TestCase):\n    def test_result(self): pass\n"
        ),
        (
            "from unittest import IsolatedAsyncioTestCase as Case\n"
            "class Base(Case): pass\n"
            "class Reports(Base):\n    async def test_result(self): pass\n"
        ),
    ],
)
def test_static_test_names_match_native_collection(tmp_path: Path, source: str) -> None:
    """Use pytest's collection as the oracle for ordinary and unittest test names."""
    _inspection_repository(tmp_path)
    path = tmp_path / "example/test_main.py"
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
def test_resource_names_have_no_spelling_convention(
    root: Path, kind: str, name: str
) -> None:
    """Validate and inspect names without a shared style rule."""
    _fixture_add(root, name, kind)
    before = _snapshot(root)
    _check_flake_source(root)
    report = json.loads(_run(root).stdout)
    _expect(report[name]["type"] == kind, report)
    _expect(_snapshot(root) == before, "validation changed resource spelling")


def test_web_formatting_preserves_literal_line_breaks(root: Path) -> None:
    """Automatic formatting preserves JavaScript strings and HTML preformatted text."""
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


def test_formatting_transforms_tracked_sources_without_staging(root: Path) -> None:
    """Remove ordinary comments while preserving lint findings and Git selection."""
    _fixture_add(root, "site", "html")
    (root / "flake.nix").write_text("{}\n")
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
def test_formatting_ignores_repository_tool_configuration(
    root: Path, *, tracked: bool
) -> None:
    """Selected sources receive the same policy with tracked or local tool settings."""
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


def test_formatting_normalizes_metadata_and_supporting_sources(root: Path) -> None:
    """Normalize selected metadata and supporting files consistently at every depth."""
    _fixture_add(root, "site", "html")
    metadata = {
        ".gitignore": "*\n\n!*/\n",
        "LICENSE": "first\n\nsecond\n",
        "flake.lock": "{\n\n}\n",
    }
    for name in ("prm", "tmp"):
        _fixture_add(root, name, "generic")
        (root / name / "main.nix").write_text("{z=2;a=1;}\n")
    for name, source in metadata.items():
        (root / name).write_text(source)
    supporting = ("site/assets", "site/fixtures", "site/prm", "site/tmp")
    supporting_source = "# Explanation\nvalue=  1\n"
    for name in supporting:
        directory = root / name
        directory.mkdir(parents=True)
        (directory / "main.py").write_text(supporting_source)
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


@pytest.mark.parametrize("description", [None, "package"])
def test_inspect_reports_package_descriptions_without_dependency_metadata(
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
    _inspection_repository(tmp_path)
    (tmp_path / "example/main.py").write_text(source)
    completed = _run(tmp_path, str(tmp_path))
    resource = json.loads(completed.stdout)["example"]
    _expect(resource["arguments"] == [] and "commands" not in resource, resource)


@pytest.mark.parametrize(
    "source",
    [
        "import argparse",
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
def test_inspect_requires_supported_cli(tmp_path: Path, source: str) -> None:
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


def test_inspect_marks_unresolved_argument_metadata_as_unknown() -> None:
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
def test_inspect_rejects_runtime_dependent_structure(declaration: str) -> None:
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
def test_inspect_rejects_argument_configuration_outside_the_inspected_source(
    configure: str,
) -> None:
    """Fail when parser flow escapes the supported helper and method patterns."""
    source = (
        "import argparse\ncli = argparse.ArgumentParser()\n"
        f"{configure}\ncli.parse_args()"
    )
    with pytest.raises(inspection_subject.CommandError, match="cannot inspect CLI"):
        _inspection_discovery(source)


def test_inspect_rejects_ambiguous_public_interfaces() -> None:
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


def test_inspect_includes_interfaces_declared_in_the_main_guard() -> None:
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
    _expect(report == {"arguments": [{"names": ["files"], "nargs": "+"}]}, report)


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
    _expect(report == {"arguments": [{"names": ["files"], "nargs": "+"}]}, report)


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
def test_inspect_ignores_unreachable_configuration(
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


def test_inspect_preserves_aliases_declared_in_static_settings() -> None:
    """Expand literal parser settings while retaining subcommand aliases."""
    report = _inspection_discovery("""
import argparse
cli = argparse.ArgumentParser(**{'parents': []})
run = cli.add_subparsers().add_parser('run', **{'aliases': ['r']})
run.add_argument('--token')
cli.parse_args()
""")
    _expect(
        report["commands"]
        == {
            "run": {"arguments": [{"names": ["--token"]}]},
            "r": {"arguments": [{"names": ["--token"]}]},
        },
        report,
    )


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
def test_inspect_rejects_unresolved_interfaces_with_source_context(
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
            "commands": {
                "one": {"arguments": [{"names": ["--one"]}]},
                "two": {"arguments": [{"names": ["--two"]}]},
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
        report == {"arguments": [{"names": ["--closed-over"]}]},
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
        report == {"arguments": [{"names": ["--public"]}]},
        report,
    )


def test_inspect_bounds_resolution_of_recursive_non_parser_helpers(
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


def test_inspect_preserves_parsers_returned_through_recursive_helpers() -> None:
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
        },
        report,
    )


def test_inspect_preserves_declared_source_encoding(root: Path) -> None:
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
    _check_flake_source(root)
    resource = json.loads(_run(root).stdout)["example"]
    _expect(
        resource["arguments"] == [{"names": ["--résumé"], "default": "é"}], resource
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
    cli.parse_args()
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
    (tmp_path / "example/test_main.py").write_text(
        """
raise RuntimeError('must not execute')
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


@pytest.mark.parametrize("directory", ["prm", "tmp", "experiments"])
def test_resource_discovery_requires_tracking(tmp_path: Path, directory: str) -> None:
    """Discover a package when its declaration joins its tracked supporting files."""
    _repository(tmp_path)
    _fixture_add(tmp_path, "example", "python")
    before = inspection_subject.inspect_repository(tmp_path)
    storage = tmp_path / directory
    storage.mkdir()
    (storage / "default.nix").write_text("{}")
    (storage / "main.py").write_text("def main(): pass\n")
    (storage / "ms.tex").touch()
    _git(
        tmp_path, "add", "--force", "--", f"{directory}/main.py", f"{directory}/ms.tex"
    )
    snapshot = _snapshot(tmp_path)
    _check_flake_source(tmp_path)
    _expect(
        inspection_subject.inspect_repository(tmp_path) == before,
        "local experiment entered inventory",
    )
    _expect(_snapshot(tmp_path) == snapshot, "untracked declaration changed state")
    _git(tmp_path, "add", "--force", "--", f"{directory}/default.nix")
    _expect(
        directory in inspection_subject.inspect_repository(tmp_path),
        "tracked package was excluded by name",
    )


def test_inspect_is_read_only(tmp_path: Path) -> None:
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


def test_inspect_reports_library_metadata_and_tests_without_a_cli(
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
    _check_flake_source(tmp_path)
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
    rejected = _check_flake_source(tmp_path, code=1)
    _expect("main must accept a call without arguments" in rejected.stderr, rejected)


def test_inspect_uses_package_executable_settings_without_dependency_metadata(
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


def test_inspect_cli_failure_has_context_and_no_partial_json(
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


def test_inspect_compacts_commands_without_losing_declared_values(
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
    if location == "outside":
        completed = _run(cwd, code=1)
        _expect(not completed.stdout, completed)
        _expect("not inside a Git repository" in completed.stderr, completed)
    else:
        paths.append([])
    if location == "nested":
        paths.append([f"../{tmp_path.name}"])
    for arguments in paths:
        completed = _run(cwd, *arguments)
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
    (tmp_path / "example" / "main.py").write_text("def invalid(")
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
    _inspection_repository(source)
    _git(source, "init", "--quiet")
    _git(source, "add", ".")
    _fixture_git(source, "commit", "--quiet", "-m", "initial")
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


def test_inspect_remote_is_fresh_and_read_only(
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


def test_inspect_remote_reports_malformed_source(
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


def test_inspect_includes_uncommitted_changes_only_for_local_paths(
    tmp_path: Path,
    remote_repository: tuple[Path, str],
) -> None:
    """Keep local dirty content visible only when explicitly inspecting its path."""
    _source, remote = remote_repository
    checkout = tmp_path / "checkout"
    _git(tmp_path, "clone", "--", remote, str(checkout))
    module = checkout / "example/main.py"
    module.write_text(module.read_text().replace('default="result"', 'default="local"'))
    before = _snapshot(tmp_path)
    local_report = json.loads(_run(tmp_path, str(checkout)).stdout)
    _expect(
        local_report["example"]["arguments"][0]["default"] == "local",
        local_report,
    )
    remote_report = json.loads(_run(tmp_path, remote).stdout)
    _expect(
        remote_report["example"]["arguments"][0]["default"] == "result",
        remote_report,
    )
    _expect(
        _snapshot(tmp_path) == before,
        "inspection changed source or Git state",
    )


def test_inspect_remote_accepts_native_file_urls(
    tmp_path: Path,
    remote_repository: tuple[Path, str],
) -> None:
    """Let Git handle transports without imposing hosted repository conventions."""
    source, _remote = remote_repository
    _expect(
        json.loads(_run(tmp_path, source.as_uri()).stdout)
        == inspection_subject.inspect_repository(source),
        "native file URL was rejected",
    )


@pytest.mark.parametrize("remote_kind", ["missing", "unsupported", "invalid"])
@pytest.mark.usefixtures("remote_repository")
def test_inspect_remote_preserves_native_clone_errors(
    tmp_path: Path,
    remote_kind: str,
) -> None:
    """Compare failed remote inspection with Git's exit status and diagnostics."""
    remote = {
        "missing": (tmp_path / "missing.git").as_uri(),
        "unsupported": "unsupported://example.test/team/repository",
        "invalid": "https://",
    }[remote_kind]
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


def _diff_repository(root: Path) -> dict[str, Any]:
    """Commit inspectable sources, including binary data and archive exclusions."""
    _inspection_repository(root)
    (root / "asset").write_bytes(b"\x00\xff\n")
    (root / ".gitattributes").write_text("example export-ignore\n")
    _git(root, "init", "--quiet")
    _git(root, "add", ".")
    _fixture_git(root, "commit", "--quiet", "-m", "initial")
    return inspection_subject.inspect_repository(root)


def _diff_description(root: Path, description: str) -> None:
    """Change a public field without changing resource identity."""
    (root / "example/default.nix").write_text(
        '{ meta.description = "' + description + '"; }',
    )


def _apply_diff(
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
        (("--exit-code",), "index", "working", 1),
        (("-R",), "working", "index", 0),
    ],
)
def test_diff_uses_git_comparisons_and_emits_jd_patches(
    tmp_path: Path,
    arguments: tuple[str, ...],
    left: str,
    right: str,
    code: int,
) -> None:
    """Keep staged, working, and historical inventories distinct and read-only."""
    reports = {"initial": _diff_repository(tmp_path)}
    _git(tmp_path, "tag", "initial")
    _diff_description(tmp_path, "Committed")
    _git(tmp_path, "add", ".")
    _fixture_git(tmp_path, "commit", "--quiet", "-m", "change description")
    reports["head"] = inspection_subject.inspect_repository(tmp_path)
    _diff_description(tmp_path, "Staged")
    _git(tmp_path, "add", ".")
    reports["index"] = inspection_subject.inspect_repository(tmp_path)
    _diff_description(tmp_path, "Working")
    reports["working"] = inspection_subject.inspect_repository(tmp_path)
    before = _snapshot(tmp_path)
    completed = _run(tmp_path / "example", "diff", *arguments, code=code)
    _expect(
        _apply_diff(reports[left], completed.stdout) == reports[right],
        completed,
    )
    _expect(not completed.stderr, completed)
    _expect(
        _snapshot(tmp_path) == before,
        "diff changed Git or sources",
    )


def test_diff_ignores_implementation_edits_and_untracked_resources(
    tmp_path: Path,
) -> None:
    """Emit nothing for changes outside the tracked interface contract."""
    _diff_repository(tmp_path)
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
    completed = _run(tmp_path, "diff", "--exit-code")
    _expect(not completed.stdout and not completed.stderr, completed)
    _expect(_snapshot(tmp_path) == before, "empty diff changed state")


def test_diff_covers_structural_and_nested_cli_changes(
    tmp_path: Path,
) -> None:
    """Round-trip resource changes, nested arguments, and lists with jd context."""
    before = _diff_repository(tmp_path)
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
    (tmp_path / "new-host").mkdir()
    (tmp_path / "new-host/configuration.nix").touch()
    _git(tmp_path, "add", ".")
    after = inspection_subject.inspect_repository(tmp_path)
    completed = _run(tmp_path, "diff", "--cached")
    _expect(_apply_diff(before, completed.stdout) == after, completed)


def test_unstaged_package_removal_matches_working_tree_diff(root: Path) -> None:
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
    rejected = _check_flake_source(root, code=1)
    for name in ("app", "shared"):
        _expect(
            f"{name}/default.nix: missing required tracked regular file"
            in rejected.stderr,
            rejected,
        )


def test_diff_pipes_through_jd_translation(tmp_path: Path) -> None:
    """Translate native diff stdin to JSON Patch for JSON viewers and patching."""
    before = _diff_repository(tmp_path)
    _diff_description(tmp_path, "Changed")
    difference = _run(tmp_path, "diff").stdout
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
        _apply_diff(before, translated.stdout, diff_format="patch")
        == inspection_subject.inspect_repository(tmp_path),
        translated,
    )
    _expect("afairesi-diff-" not in difference, difference)
    _expect(
        _run(tmp_path, "diff").stdout == difference,
        "temporary snapshot paths made identical diffs unstable",
    )


def test_diff_uses_the_merge_base(tmp_path: Path) -> None:
    """Exclude the other branch's changes and ignore malformed working sources."""
    base = _diff_repository(tmp_path)
    _git(tmp_path, "tag", "base")
    _git(tmp_path, "checkout", "--quiet", "-b", "topic")
    _diff_description(tmp_path, "Topic")
    _git(tmp_path, "add", ".")
    _fixture_git(tmp_path, "commit", "--quiet", "-m", "topic interface")
    topic = inspection_subject.inspect_repository(tmp_path)
    _git(tmp_path, "checkout", "--quiet", "-b", "other", "base")
    (tmp_path / "other-host").mkdir()
    (tmp_path / "other-host/configuration.nix").touch()
    _git(tmp_path, "add", ".")
    _fixture_git(tmp_path, "commit", "--quiet", "-m", "other interface")
    other = inspection_subject.inspect_repository(tmp_path)
    (tmp_path / "example/main.py").write_text("def invalid(")
    before = _snapshot(tmp_path)
    for arguments, report in ((("--merge-base",), base), ((), other)):
        completed = _run(
            tmp_path, "diff", "--from", "other", "--to", "topic", *arguments
        )
        _expect(_apply_diff(report, completed.stdout) == topic, completed)
    _expect(_snapshot(tmp_path) == before, "range diff changed state")


def test_diff_compares_initial_staging_against_an_empty_repository(
    tmp_path: Path,
) -> None:
    """Compare the first staged interface against an empty inventory."""
    _git(tmp_path, "init", "--quiet")
    _expect(not _run(tmp_path, "diff", "--cached").stdout, "nonempty diff")
    _inspection_repository(tmp_path)
    _git(tmp_path, "add", ".")
    staged = inspection_subject.inspect_repository(tmp_path)
    _diff_description(tmp_path, "Unstaged")
    before = _snapshot(tmp_path)
    completed = _run(tmp_path, "diff", "--cached", "--exit-code", code=1)
    _expect(_apply_diff({}, completed.stdout) == staged, completed)
    _expect(_snapshot(tmp_path) == before, "initial diff changed state")


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
def test_diff_errors_leave_no_partial_output_or_temporary_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: tuple[str, ...],
    failure: str,
    code: int,
) -> None:
    """Reject invalid comparisons atomically and clean failed snapshots."""
    root = tmp_path / "repository"
    root.mkdir()
    _diff_repository(root)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setenv("TMPDIR", str(scratch))
    if not arguments:
        (root / "example/main.py").write_text("def invalid(")
    before = _snapshot(root)
    completed = _run(root, "diff", *arguments, code=code)
    _expect(not completed.stdout, completed)
    _expect(failure in completed.stderr, completed)
    _expect(not list(scratch.iterdir()), "failed diff left temporary sources")
    _expect(_snapshot(root) == before, "failed diff changed state")


@pytest.mark.parametrize("name", ["diff", "HEAD", "repository with spaces"])
def test_cli_selects_repositories_outside_git(
    tmp_path: Path,
    name: str,
) -> None:
    """Distinguish repository paths from commands and revision names."""
    root = tmp_path / name
    root.mkdir()
    report = _diff_repository(root)
    paths = [(f"./{name}",), ("--", name), (str(root),)]
    if name != "diff":
        paths.append((name,))
    for arguments in paths:
        completed = _run(tmp_path, *arguments)
        _expect(json.loads(completed.stdout) == report, completed)
    _diff_description(root, "Changed")
    for target in (name, str(root)):
        completed = _run(tmp_path, "diff", target)
        _expect(
            _apply_diff(report, completed.stdout)
            == inspection_subject.inspect_repository(root),
            completed,
        )
        completed = _run(tmp_path, "diff", "--from", "HEAD", "--to", "HEAD", target)
        _expect(not completed.stdout, completed)


def test_diff_rejects_unmerged_index_without_changes(
    tmp_path: Path,
) -> None:
    """Reject conflict stages instead of emitting an incomplete interface diff."""
    _diff_repository(tmp_path)
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


def test_diff_preserves_linked_worktrees(tmp_path: Path) -> None:
    """Read the selected worktree's index while preserving shared Git metadata."""
    root, linked = tmp_path / "repository", tmp_path / "linked"
    root.mkdir()
    report = _diff_repository(root)
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


def test_inspect_reports_public_commands() -> None:
    """Inventory every public command without nested command groups."""
    report = inspection_subject.ArgumentDiscovery(
        Path(__file__).with_name("main.py").read_bytes(),
    ).report()
    commands = report["commands"]
    _expect(set(commands) == {"diff"}, commands)
    command = commands["diff"]
    _expect("commands" not in command, command)
    arguments = [row["names"] for row in command["arguments"]]
    _expect(arguments.count(["repository"]) == 1, command)
    for option in ("--from", "--to", "--merge-base"):
        _expect(arguments.count([option]) == 1, command)
    _expect(
        [row["names"] for row in report["arguments"]] == [["repository"]],
        report["arguments"],
    )


@pytest.mark.parametrize("kind", ["python", "generic", "lean4"])
def test_source_validation_ignores_local_packages_and_optional_tests(
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
    (local / "main.py").write_bytes(b"def broken(:")
    before = _snapshot(root)
    _check_flake_source(root)
    _expect(_snapshot(root) == before, "validation admitted or changed local sources")
    report = inspection_subject.inspect_repository(root)
    _expect(set(report) == {"example"} and "tests" not in report["example"], report)


def test_inspection_reports_declared_local_dependency_edges(tmp_path: Path) -> None:
    """Combine a package and host's local dependency roles without evaluating Nix."""
    root = _repository(tmp_path)
    for name in ("consumer", "library", "other", "tool", "helper"):
        directory = root / name
        directory.mkdir()
        (directory / "default.nix").write_text("{}: {}")
        (directory / "main.nix").write_text("{}")
    source = """{ inputs, pkgs, ... }:
      let local = inputs.self.packages.${pkgs.stdenv.system};
          deps = [ local.library local.library ];
      in pkgs.runCommand "consumer" {
        propagatedBuildInputs = deps ++
          (with inputs.self.packages.${pkgs.stdenv.system}; [ other ]);
        nativeCheckInputs = [ inputs.self.packages.${pkgs.stdenv.system}.library ];
        hook = local: { checkInputs = [ local.other ]; };
        buildInputs = [ inputs.external.packages.${pkgs.stdenv.system}.other ];
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


def test_inspection_handles_shared_non_parser_aliases() -> None:
    """Traverse shared containers once while finding actual parser arguments."""
    source = "import argparse\nvalue0 = []\n"
    for index in range(1, 24):
        source += f"value{index} = [value{index - 1}, value{index - 1}]\n"
    source += "consume(value23)\nparser = argparse.ArgumentParser()\n"
    source += "parser.add_argument('--name')\nparser.parse_args()\n"
    report = _inspection_discovery(source)
    _expect(report["arguments"] == [{"names": ["--name"]}], report)

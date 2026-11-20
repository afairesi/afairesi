# Copyright (c) 2026- Paschalis Bizopoulos
"""Extract behavioral specifications from Afairesi repositories."""

# ruff: noqa: INP001 - mkPythonPackage installs this module as __init__.py.

import argparse
import ast
import contextlib
import io
import json
import keyword
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import nix_syntax
from afairesi.rust import cargo_names
from afairesi.specification import bats_names, lean_names, nix_names

if TYPE_CHECKING:
    from collections.abc import Iterator

    from tree_sitter import Node
BUILDER_KINDS = {
    "mkBashPackage": "bash",
    "mkHtmlPackage": "html",
    "mkLatexPackage": "latex",
    "mkLean4Package": "lean4",
    "mkPythonPackage": "python",
}
PACKAGE_MAIN_FILES = {
    "bash": "main.sh",
    "generic": "main.nix",
    "html": "index.html",
    "latex": "ms.tex",
    "lean4": "main.lean",
    "python": "main.py",
}
ROOT_FILES = ("flake.lock", "flake.nix")


class CommandError(RuntimeError):
    """A user-facing command failure."""


@dataclass(frozen=True)
class Package:
    """A repository package and its parsed declaration."""

    root: Path
    kind: str
    definition: nix_syntax.Document


def git(
    root: Path,
    arguments: list[str],
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run Git in a selected repository, forwarding diagnostics on failure."""
    completed = subprocess.run(  # noqa: S603 - Git arguments are passed without a shell.
        ["git", "-C", str(root), *arguments],  # noqa: S607
        capture_output=True,
        check=False,
        text=True,
    )
    if check and completed.returncode:
        sys.stdout.write(completed.stdout)
        sys.stderr.write(completed.stderr)
        raise SystemExit(completed.returncode)
    return completed


def repository_root() -> Path:
    """Discover the current Git worktree root."""
    completed = git(
        Path(),
        ["rev-parse", "--path-format=absolute", "--show-toplevel"],
        check=False,
    )
    if completed.returncode != 0:
        msg = "not inside a Git repository"
        raise CommandError(msg)
    return Path(completed.stdout.strip())


def resolve_repository_path(path: Path) -> Path:
    """Resolve explicit paths from the Git root, or cwd outside a repository."""
    if path.is_absolute():
        return path.resolve()
    try:
        root = repository_root()
    except CommandError:
        root = Path.cwd()
    return (root / path).resolve()


def read_regular(path: Path) -> bytes | None:
    """Read an optional regular file, rejecting other filesystem entries."""
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(mode):
        msg = f"{path}: must be a regular file"
        raise CommandError(msg)
    return path.read_bytes()


def source_paths(root: Path) -> set[Path]:
    """Select top-level package files and indexed Rust sources for module discovery."""
    if (root / ".git").exists():
        entries = git(root, ["ls-files", "-z"]).stdout
        return {
            Path(item)
            for item in entries.split("\0")
            if item and (item.count("/") <= 1 or item.endswith(".rs"))
        }
    paths: set[Path] = set()
    for directory, directories, filenames in root.walk():
        if directory != root:
            directories.clear()
        relative = directory.relative_to(root)
        paths.update(relative / name for name in filenames)
    return paths


def _builder_field(document: nix_syntax.Document, field: str) -> Node | None:
    """Resolve the function or argument of the returned builder call."""
    body = nix_syntax.call_package_body(document)
    if body is None or body.type != "apply_expression":
        return None
    return nix_syntax.resolve_expression(document, body.child_by_field_name(field))


def _package_kind(document: nix_syntax.Document) -> str:
    """Select conventions only from recognizable builders without evaluating Nix."""
    function = _builder_field(document, "function")
    if function is not None and function.type == "select_expression":
        attrs = function.child_by_field_name("attrpath")
        if attrs is not None and (parts := nix_syntax.static_attrpath(document, attrs)):
            return BUILDER_KINDS.get(parts[-1], "generic")
    return "generic"


def _resource_definitions(root: Path, paths: set[Path], filename: str) -> list[Path]:
    """Select declarations directly under top-level resource directories."""
    definitions = []
    for path in paths:
        if path.name != filename or len(path.parent.parts) != 1:
            continue
        definition = root / path
        if not definition.parent.is_symlink() and definition.parent.is_dir():
            definitions.append(definition)
    return sorted(definitions)


def detect_packages(root: Path, paths: set[Path]) -> tuple[list[Package], list[Path]]:
    """Read package declarations and identify tracked declarations missing on disk."""
    result = []
    missing = []
    for path in _resource_definitions(root, paths, "default.nix"):
        source = read_regular(path)
        relative = path.relative_to(root)
        if source is None:
            missing.append(relative)
            continue
        document = nix_syntax.parse(source, str(relative))
        result.append(Package(path.parent, _package_kind(document), document))
    return result, missing


def validate_python_package_names(packages: list[Package]) -> None:
    """Reject invalid or conflicting installed Python module names."""
    modules: dict[str, str] = {}
    for package in packages:
        if package.kind != "python":
            continue
        name = package.root.name
        module = name.replace("-", "_")
        if not module.isidentifier() or keyword.iskeyword(module):
            msg = (
                f"{name}: installed Python module must be "
                "a valid non-keyword identifier"
            )
            raise CommandError(msg)
        if module in modules:
            msg = (
                f"{name} and {modules[module]}: "
                f"installed Python module names collide: {module}"
            )
            raise CommandError(msg)
        modules[module] = name


def _structure_issues(
    root: Path, packages: list[Package], paths: set[Path]
) -> list[str]:
    """Check tracked resource files for layout and Python source errors."""
    issues: list[str] = []
    conventional = {Path(item) for item in ROOT_FILES}
    for config in _resource_definitions(root, paths, "configuration.nix"):
        if config.is_file():
            relative = config.relative_to(root)
            conventional.update(
                (relative, relative.with_name("hardware-configuration.nix"))
            )
    for package in packages:
        issues.extend(_python_source_issues(package, paths))
        directory = package.root.relative_to(root)
        main = directory / PACKAGE_MAIN_FILES[package.kind]
        conventional.add(main)
        if suite := NAMED_TEST_FILES.get(package.kind):
            conventional.add(directory / suite[0])
        if package.kind != "generic" and (
            main not in paths or not (root / main).is_file()
        ):
            issues.append(f"{main}: missing required tracked regular file")
    for relative in sorted(conventional & paths):
        path = root / relative
        if path.is_symlink():
            issues.append(
                f"{relative}: expected regular file or directory, found symbolic link",
            )
    return issues


def _python_class_mro(
    classes: dict[str, ast.ClassDef],
    name: str,
    seen: set[str],
) -> list[str]:
    """Compute Python's class precedence without executing base declarations."""
    if name in seen:
        msg = f"cyclic class inheritance: {name}"
        raise CommandError(msg)
    if name not in classes:
        return [name]
    bases = [ast.unparse(base) for base in classes[name].bases]
    sequences = [_python_class_mro(classes, base, seen | {name}) for base in bases] + [
        bases.copy(),
    ]
    result = [name]
    while sequences := [sequence for sequence in sequences if sequence]:
        candidate = next(
            (
                sequence[0]
                for sequence in sequences
                if all(sequence[0] not in other[1:] for other in sequences)
            ),
            None,
        )
        if candidate is None:
            msg = f"inconsistent class inheritance: {name}"
            raise CommandError(msg)
        result.append(candidate)
        for sequence in sequences:
            if sequence[0] == candidate:
                sequence.pop(0)
    return result


def _python_test_names(module: ast.Module) -> set[str]:
    """Collect pytest identifiers, including methods inherited from local bases."""
    classes = {
        node.name: node for node in module.body if isinstance(node, ast.ClassDef)
    }
    case_classes = _unittest_classes(module)
    names = {
        node.name
        for node in module.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test")
    }
    for name in classes:
        if not (name.startswith("Test") or name in case_classes):
            continue
        methods: set[str] = set()
        for ancestor in reversed(_python_class_mro(classes, name, set())):
            if ancestor not in classes:
                continue
            for statement in classes[ancestor].body:
                if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if statement.name.startswith("test"):
                        methods.add(statement.name)
                elif isinstance(statement, (ast.Assign, ast.AnnAssign)):
                    targets = (
                        statement.targets
                        if isinstance(statement, ast.Assign)
                        else [statement.target]
                    )
                    methods.difference_update(
                        target.id for target in targets if isinstance(target, ast.Name)
                    )
        names.update(f"{name}::{method}" for method in methods)
    return names


def _python_source_issues(package: Package, paths: set[Path]) -> list[str]:
    """Check source contracts without importing or executing package code."""
    if package.kind != "python":
        return []
    issues = []
    name = package.root.name
    if Path(name) / "__init__.py" in paths:
        issues.append(
            f"{name}/__init__.py: reserved for the installed main.py; "
            "move initialization into main.py"
        )
    source = read_regular(package.root / "main.py")
    if source is not None:
        module = ast.parse(source, filename=str(package.root / "main.py"))
        if _python_test_names(module):
            issues.append(
                f"{name}/main.py: move test definitions to test_main.py",
            )
        entrypoint = (
            None if _python_library(package.definition) else _python_entrypoint(module)
        )
        prefix = f"{name}/main.py: "
        if isinstance(entrypoint, ast.AsyncFunctionDef):
            issues.append(
                prefix + "main must be synchronous; the executable calls main()",
            )
        elif isinstance(entrypoint, (ast.FunctionDef, ast.Lambda)) and (
            len(entrypoint.args.posonlyargs) + len(entrypoint.args.args)
            > len(entrypoint.args.defaults)
            or any(default is None for default in entrypoint.args.kw_defaults)
        ):
            issues.append(prefix + "main must accept a call without arguments")
        elif isinstance(
            entrypoint, (ast.Constant, ast.List, ast.Tuple, ast.Set, ast.Dict)
        ):
            issues.append(prefix + "main must be callable; the executable calls main()")
    tests = package.root / "test_main.py"
    if tests.relative_to(package.root.parent) in paths and tests.is_file():
        _source(tests)
    return issues


def _python_entrypoint(
    module: ast.Module,
) -> ast.stmt | ast.expr | None:
    """Find the last direct binding or deletion of the installed main name."""
    for node in reversed(module.body):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node.name == "main"
        ):
            return node
        if isinstance(node, ast.Delete) and any(
            isinstance(target, ast.Name) and target.id == "main"
            for target in node.targets
        ):
            return node
        if isinstance(node, (ast.Import, ast.ImportFrom)) and any(
            (alias.asname or alias.name) == "main" for alias in node.names
        ):
            return None
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(
                isinstance(target, ast.Name) and target.id == "main"
                for target in targets
            ):
                return node.value
    return None


def validate_flake_source(root: Path) -> None:
    """Validate a Git-filtered flake source without requiring Git metadata."""
    paths = source_paths(root)
    packages, missing = detect_packages(root, paths)
    validate_python_package_names(packages)
    issues = _structure_issues(root, packages, paths)
    issues.extend(f"{path}: missing required tracked regular file" for path in missing)
    flake = read_regular(root / "flake.nix")
    if flake is not None:
        nix_syntax.parse(flake, "flake.nix")
    for required in ROOT_FILES:
        if not (root / required).is_file():
            issues.append(f"{required}: missing required regular file")
    if issues:
        formatted_issues = "\n".join(f"  - {issue}" for issue in issues)
        msg = f"repository layout validation failed:\n{formatted_issues}"
        raise CommandError(msg)
    for package in packages:
        if package.kind == "generic":
            for filename in (
                PACKAGE_MAIN_FILES["generic"],
                NAMED_TEST_FILES["generic"][0],
            ):
                source = package.root / filename
                if source.relative_to(root) in paths and source.is_file():
                    nix_syntax.parse(source.read_bytes(), str(source))


def _unittest_classes(module: ast.Module) -> set[str]:
    """Recognize unittest subclasses regardless of their class names."""
    bases: set[str] = set()
    case_types = {"TestCase", "IsolatedAsyncioTestCase"}
    for node in module.body:
        if isinstance(node, ast.Import):
            bases.update(
                (alias.asname or alias.name) + "." + case
                for alias in node.names
                if alias.name == "unittest"
                for case in case_types
            )
        elif isinstance(node, ast.ImportFrom) and node.module == "unittest":
            bases.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name in case_types
            )
    classes = [node for node in module.body if isinstance(node, ast.ClassDef)]
    found: set[str] = set()
    while additions := {
        node.name
        for node in classes
        if node.name not in found
        and any(ast.unparse(base) in bases | found for base in node.bases)
    }:
        found.update(additions)
    return found


@contextlib.contextmanager
def inspection_remote_repository(remote: str) -> Iterator[Path]:
    """Inspect a native Git clone without changing existing repositories."""
    with tempfile.TemporaryDirectory(prefix="afairesi-inspect-") as temporary:
        checkout = Path(temporary) / "repository"
        clone = subprocess.run(  # noqa: S603 - Git receives the remote after the option delimiter.
            ["git", "clone", "--", remote, str(checkout)],  # noqa: S607
            check=False,
        )
        if clone.returncode:
            raise SystemExit(clone.returncode)
        yield checkout


def _source(path: Path) -> ast.Module:
    """Parse source without importing it or executing its declarations."""
    try:
        return ast.parse(path.read_bytes(), filename=str(path))
    except (OSError, SyntaxError, UnicodeError) as error:
        message = f"{path}: could not parse Python source: {error}"
        raise CommandError(message) from error


def _python_library(document: nix_syntax.Document) -> bool:
    """Read the executable setting from the Python builder's arguments."""
    argument = _builder_field(document, "argument")
    if argument is None or argument.type not in {
        "attrset_expression",
        "rec_attrset_expression",
    }:
        return False
    bindings = nix_syntax.binding_set(argument)
    return [
        document.text(value)
        for binding in (bindings.named_children if bindings is not None else [])
        if (attrpath := binding.child_by_field_name("attrpath")) is not None
        and nix_syntax.static_attrpath(document, attrpath) == ("executable",)
        and (value := binding.child_by_field_name("expression")) is not None
    ] == ["false"]


def _python_suite_names(path: Path) -> set[str]:
    """Read the pytest identifiers declared in a suite."""
    return _python_test_names(_source(path))


NAMED_TEST_FILES = {
    "bash": ("test_main.bats", bats_names),
    "generic": ("test_main.nix", nix_names),
    "lean4": ("test_main.lean", lean_names),
    "python": ("test_main.py", _python_suite_names),
}


def inspect_repository(root: Path) -> dict[str, list[str]]:
    """Map each declared package to its sorted, statically named tests."""
    if not (root / "flake.nix").is_file():
        message = f"{root}: expected an Afairesi repository with flake.nix"
        raise CommandError(message)
    paths = source_paths(root)
    packages, _ = detect_packages(root, paths)
    report: dict[str, list[str]] = {}
    tracked_rust = None
    for package in packages:
        name = package.root.name
        names: set[str] = set()
        if suite_info := NAMED_TEST_FILES.get(package.kind):
            filename, extract = suite_info
            suite = package.root / filename
            if suite.relative_to(root) in paths and suite.is_file():
                names.update(extract(suite))
        if (
            package.kind == "generic"
            and Path(name) / "Cargo.toml" in paths
            and (package.root / "Cargo.toml").is_file()
        ):
            if tracked_rust is None and (root / ".git").exists():
                tracked_rust = {
                    root / source for source in paths if source.suffix == ".rs"
                }
            names.update(cargo_names(package.root, tracked_rust))
        report[name] = sorted(names)
    return report


def diff_inputs(root: Path, listing: str) -> list[tuple[str, str, Path]]:
    """Select inspection inputs, retaining possible targets of source symlinks."""
    files = []
    for entry in listing.split("\0"):
        if not entry:
            continue
        metadata, name = entry.split("\t", 1)
        mode, oid, stage = metadata.split()
        if stage != "0":
            message = "cannot inspect an unmerged index; resolve conflicts first"
            raise CommandError(message)
        if mode != "160000":
            files.append((mode, oid, Path(name)))
    manifests = {
        path.parent
        for _, _, path in files
        if path.name == "Cargo.toml" and len(path.parent.parts) == 1
    }
    filenames = {
        "default.nix",
        "Cargo.toml",
        *(entry[0] for entry in NAMED_TEST_FILES.values()),
    }
    inputs = [
        (mode, oid, path)
        for mode, oid, path in files
        if path == Path("flake.nix")
        or (len(path.parent.parts) == 1 and path.name in filenames)
        or (path.suffix == ".rs" and Path(path.parts[0]) in manifests)
    ]
    if any(mode == "120000" or (root / path).is_symlink() for mode, _, path in inputs):
        return files
    return inputs


def _inspection_git_report(
    root: Path,
    files: list[tuple[str, str, Path]],
    *,
    working_tree: bool = False,
) -> dict[str, list[str]]:
    """Inspect tracked sources in a temporary directory without checkout filters."""
    with tempfile.TemporaryDirectory(prefix="afairesi-diff-") as directory:
        snapshot = Path(directory)
        if working_tree:
            for _mode, _oid, relative in files:
                source = root / relative
                if any((root / parent).is_symlink() for parent in relative.parents):
                    continue
                if source.is_file() or source.is_symlink():
                    target = snapshot / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target, follow_symlinks=False)
        elif files:
            completed = subprocess.run(  # noqa: S603 - Fixed Git command; object IDs are passed on stdin.
                ["git", "-C", str(root), "cat-file", "--batch"],  # noqa: S607
                input="".join(f"{oid}\n" for _mode, oid, _path in files).encode(),
                capture_output=True,
                check=False,
            )
            if completed.returncode:
                sys.stderr.write(completed.stderr.decode(errors="replace"))
                raise SystemExit(completed.returncode)
            content = io.BytesIO(completed.stdout)
            for mode, _oid, relative in files:
                _object, separator, size = content.readline().partition(b" blob ")
                if not separator:
                    message = f"{relative}: could not read Git blob"
                    raise CommandError(message)
                source_bytes = content.read(int(size))
                content.read(1)
                target = snapshot / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                if mode == "120000":
                    target.symlink_to(os.fsdecode(source_bytes))
                else:
                    target.write_bytes(source_bytes)
        return (
            inspect_repository(snapshot) if (snapshot / "flake.nix").is_file() else {}
        )


def _dispatch_diff(arguments: list[str]) -> None:
    """Render changes in package test names between the index and the working tree."""
    _diff_parser().parse_args(arguments)
    root = repository_root()
    files = diff_inputs(root, git(root, ["ls-files", "--stage", "-z"]).stdout)
    before = json.dumps(_inspection_git_report(root, files))
    after = json.dumps(_inspection_git_report(root, files, working_tree=True))
    if before == after:
        return
    with tempfile.TemporaryDirectory(prefix="afairesi-diff-") as directory:
        baseline = Path(directory) / "before.json"
        baseline.write_text(before, encoding="utf-8")
        completed = subprocess.run(  # noqa: S603 - The filename is fixed.
            ["jd", baseline.name],  # noqa: S607
            cwd=directory,
            input=after,
            capture_output=True,
            check=False,
            text=True,
        )
    if completed.returncode not in {0, 1}:
        sys.stderr.write(completed.stderr)
        raise SystemExit(completed.returncode)
    sys.stdout.write(completed.stdout)


def _dispatch_inspection(arguments: list[str]) -> None:
    """Inspect local or remote sources without executing package code."""
    value = parser().parse_args(arguments).repository
    repository: contextlib.AbstractContextManager[Path]
    if value is not None and (
        "://" in value or (":" in value.split("/", 1)[0] and not Path(value).exists())
    ):
        repository = inspection_remote_repository(value)
    else:
        repository = contextlib.nullcontext(
            resolve_repository_path(Path(value))
            if value is not None
            else repository_root(),
        )
    with repository as root:
        report = inspect_repository(root)
    sys.stdout.write(json.dumps(report, indent=2) + "\n")


def parser() -> argparse.ArgumentParser:
    """Construct the parser for the package test-name specification."""
    result = argparse.ArgumentParser(
        prog="afairesi",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Print a JSON object mapping each package to its sorted test names. "
            "Read Python tests (test_main.py), Bats cases (test_main.bats), Nix "
            "runTests cases (test_main.nix), Lean theorems (test_main.lean), and "
            "Rust #[test] functions in Cargo targets without executing source. "
            "Packages with no named tests map to []."
        ),
        epilog=(
            "Commands:\n"
            "  diff    diff test names in jd format\n"
            "Use './diff' or '-- diff' to inspect a repository named diff."
        ),
    )
    result.add_argument(
        "repository",
        nargs="?",
        metavar="PATH_OR_REMOTE",
        help=(
            "repository directory or Git URL (default: current repository); "
            "relative paths use the Git root, or the current directory outside Git"
        ),
    )
    return result


def _diff_parser() -> argparse.ArgumentParser:
    """Construct the parser for the diff subcommand."""
    return argparse.ArgumentParser(
        prog="afairesi diff",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Print a jd-compatible diff of package test names. "
            "Compare the current repository's index with tracked working-tree sources. "
            "Untracked files and implementation-only changes are omitted. "
            "Inspect source without executing it or changing Git state."
        ),
        epilog=(
            "Examples:\n"
            "  afairesi diff | jd -t jd2patch | jless\n"
            "  afairesi diff > change.jd\n"
            "  jd -p change.jd before.json\n"
            "\nFor patching, before.json must contain the baseline inspection."
        ),
    )


def main() -> None:
    """Inspect by default, reserving the first argument 'diff' for comparisons."""
    arguments = sys.argv[1:]
    comparing = arguments[:1] == ["diff"]
    try:
        if comparing:
            _dispatch_diff(arguments[1:])
        else:
            _dispatch_inspection(arguments)
    except KeyboardInterrupt:
        sys.exit(130)
    except CommandError as error:
        program = "afairesi diff" if comparing else "afairesi"
        sys.stderr.write(f"{program}: {error}\n")
        raise SystemExit(1) from error
    except (OSError, SyntaxError, ValueError) as error:
        sys.stderr.write(f"error: {error}\n")
        raise SystemExit(1) from error

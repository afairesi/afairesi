#!/usr/bin/env python3
# Copyright (c) 2026- Paschalis Bizopoulos
"""Inspect repositories in the afairesi computing environment."""

from __future__ import annotations

import argparse
import ast
import contextlib
import io
import json
import keyword
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import nix_syntax

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import Never

    from tree_sitter import Node
KIND_MARKERS = {
    "html": "index.html",
    "latex": "ms.tex",
    "lean4": "main.lean",
    "nix": "main.nix",
    "python": "main.py",
}
PACKAGE_BUILDERS = {
    "html": "mkHtmlPackage",
    "latex": "mkLatexPackage",
    "python": "mkPythonPackage",
    "lean4": "mkLean4Package",
}
ROOT_FILES = {
    "flake.lock",
    "flake.nix",
}
RESOURCE_PATH_COMPONENTS = 1


class CommandError(RuntimeError):
    """A user-facing command failure."""


@dataclass(frozen=True)
class Package:
    """A repository package with its kind and source directory."""

    name: str
    kind: str
    root: Path


def run(
    arguments: list[str],
    cwd: Path | None = None,
    *,
    check: bool = True,
    capture_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a process with captured output or inherited terminal streams."""
    completed = subprocess.run(  # noqa: S603 - Callers construct argv; commands run without a shell.
        arguments,
        cwd=cwd,
        capture_output=capture_output,
        check=False,
        text=True,
    )
    if check and completed.returncode != 0:
        if completed.stdout:
            sys.stdout.write(completed.stdout)
        if completed.stderr:
            sys.stderr.write(completed.stderr)
        raise SystemExit(completed.returncode)
    return completed


def git(
    root: Path,
    arguments: list[str],
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run Git in a selected repository."""
    return run(
        ["git", "-C", str(root), *arguments],
        check=check,
    )


def repository_root(path: Path = Path()) -> Path:
    """Discover the current Git worktree root."""
    completed = git(
        path,
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


def read_regular(path: Path) -> str | None:
    """Read an optional regular file, rejecting other filesystem entries."""
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(mode):
        msg = f"{path}: must be a regular file"
        raise CommandError(msg)
    return path.read_text(encoding="utf-8")


def _tracked_paths(root: Path) -> set[Path]:
    """Return paths represented in the index."""
    completed = git(root, ["ls-files", "-z"])
    return {Path(item) for item in completed.stdout.split("\0") if item}


def source_paths(root: Path) -> set[Path]:
    """Select tracked working files, or files in an exported source tree."""
    if (root / ".git").exists():
        return _tracked_paths(root)
    paths: set[Path] = set()
    for directory, directories, filenames in root.walk():
        directories[:] = [name for name in directories if name != ".git"]
        paths.update(
            (directory / name).relative_to(root) for name in filenames if name != ".git"
        )
    return paths


def _package_kind(name: str, markers: set[str], definition: str | None) -> str:
    """Use recognizable builders or source markers without evaluating Nix."""
    if definition:
        with contextlib.suppress(nix_syntax.NixSyntaxError):
            document = nix_syntax.parse(definition)
            body = _package_body(document)
            function = body.child_by_field_name("function")
            if body.type == "apply_expression" and function is not None:
                constructor = _resource_constructor(document, function)
                for kind, builder in PACKAGE_BUILDERS.items():
                    if constructor == builder:
                        return kind
    matches = [kind for kind, marker in KIND_MARKERS.items() if marker in markers]
    if len(matches) > 1:
        if definition is not None:
            return "nix"
        msg = f"{name}: has ambiguous project markers: {', '.join(matches)}"
        raise CommandError(msg)
    return matches[0] if matches else "nix"


def resource_directories(root: Path, paths: set[Path]) -> list[Path]:
    """List top-level source directories without traversing local files."""
    directories = {
        root / path.parts[0]
        for path in paths
        if len(path.parts) > 1 and path.parts[0] != ".git"
    }
    return sorted(
        path for path in directories if path.is_dir() and not path.is_symlink()
    )


def detect_hosts(root: Path, paths: set[Path]) -> list[Path]:
    """Detect hosts under the same directory rules as packages."""
    return [
        path
        for path in resource_directories(root, paths)
        if Path(path.name) / "configuration.nix" in paths
        and (path / "configuration.nix").is_file()
    ]


def detect_packages(root: Path, paths: set[Path] | None = None) -> list[Package]:
    """Detect declared packages, falling back to unambiguous entrypoint files."""
    result: list[Package] = []
    paths = source_paths(root) if paths is None else paths
    for package_root in resource_directories(root, paths):
        markers = {
            marker
            for marker in KIND_MARKERS.values()
            if Path(package_root.name) / marker in paths
            and (package_root / marker).is_file()
        }
        definition = Path(package_root.name) / "default.nix"
        if not markers and definition not in paths:
            continue
        result.append(
            Package(
                package_root.name,
                _package_kind(
                    package_root.name,
                    markers,
                    read_regular(root / definition) if definition in paths else None,
                ),
                package_root,
            ),
        )
    validate_python_package_names(result)
    return result


def validate_name(name: str) -> None:
    """Require one directory name without imposing a spelling convention."""
    path = Path(name)
    if (
        path.is_absolute()
        or len(path.parts) != RESOURCE_PATH_COMPONENTS
        or name != path.name
        or name in {"..", ".git"}
        or "\0" in name
    ):
        msg = f"resource path must be a root directory name: {name}"
        raise CommandError(msg)


def validate_python_package_names(packages: list[Package]) -> None:
    """Reject invalid or conflicting installed Python module names."""
    modules: dict[str, str] = {}
    for package in packages:
        if package.kind != "python":
            continue
        module = package.name.replace("-", "_")
        if not module.isidentifier() or keyword.iskeyword(module):
            msg = (
                f"{package.name}: installed Python module must be "
                "a valid non-keyword identifier"
            )
            raise CommandError(msg)
        if module in modules:
            msg = (
                f"{package.name} and {modules[module]}: "
                f"installed Python module names collide: {module}"
            )
            raise CommandError(msg)
        modules[module] = package.name


def package_files(package: Package) -> set[Path]:
    """Return conventional source files for a package kind."""
    relative = Path() / package.name
    kind_files = {
        "python": {"main.py", "test_main.py"},
        "html": {"index.html"},
        "latex": {"ms.tex"},
        "lean4": {"main.lean", "test_main.lean"},
        "nix": {"main.nix", "test_main.nix"},
    }[package.kind]
    return {relative / "default.nix", *(relative / item for item in kind_files)}


def required_package_files(package: Package) -> set[Path]:
    """Return regular files required for a package kind."""
    optional = {
        "html": set(),
        "latex": set(),
        "lean4": {"test_main.lean"},
        "nix": {"main.nix", "test_main.nix"},
        "python": {"test_main.py"},
    }[package.kind]
    return {path for path in package_files(package) if path.name not in optional}


def _conventional_paths(
    root: Path, packages: list[Package], paths: set[Path]
) -> set[Path]:
    """Select tracked conventional files for source validation."""
    conventional = {Path(item) for item in ROOT_FILES} & paths
    for host in detect_hosts(root, paths):
        validate_name(host.name)
        conventional.update(
            {
                Path(host.name) / "configuration.nix",
                Path(host.name) / "hardware-configuration.nix",
            }
            & paths,
        )
    for package in packages:
        conventional.update(package_files(package) & paths)
    return conventional


def inspect_structure(
    root: Path, paths: set[Path] | None = None
) -> tuple[list[Package], list[str]]:
    """Validate recognized tracked resources, permitting arbitrary supporting files."""
    paths = source_paths(root) if paths is None else paths
    packages = detect_packages(root, paths)
    issues: list[str] = []
    for package in packages:
        validate_name(package.name)
        issues.extend(_python_source_issues(package, paths))
        issues.extend(
            f"{relative}: missing required tracked regular file"
            for relative in sorted(required_package_files(package))
            if relative not in paths or not (root / relative).is_file()
        )
    for relative in sorted(_conventional_paths(root, packages, paths)):
        path = root / relative
        if path.is_symlink():
            issues.append(
                f"{relative}: expected regular file or directory, found symbolic link",
            )
    return packages, issues


def has_python_tests(path: Path) -> bool:
    """Detect static Python test definitions without executing the source."""
    try:
        module = ast.parse(path.read_bytes(), filename=str(path))
        return bool(python_test_definitions(module))
    except (OSError, SyntaxError, UnicodeError) as error:
        msg = f"{path}: Python source could not be parsed: {error}"
        raise CommandError(
            msg,
        ) from error


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


def python_test_definitions(
    module: ast.Module,
) -> list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    """Find static test definitions, including methods inherited from local bases."""
    classes = {
        node.name: node for node in module.body if isinstance(node, ast.ClassDef)
    }
    case_classes = _unittest_classes(module)

    def methods(name: str) -> dict[str, ast.stmt]:
        result: dict[str, ast.stmt] = {}
        for ancestor in reversed(_python_class_mro(classes, name, set())):
            if ancestor not in classes:
                continue
            for statement in classes[ancestor].body:
                if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    result[statement.name] = statement
                elif isinstance(statement, (ast.Assign, ast.AnnAssign)):
                    targets = (
                        statement.targets
                        if isinstance(statement, ast.Assign)
                        else [statement.target]
                    )
                    for target in targets:
                        if isinstance(target, ast.Name):
                            result[target.id] = statement
        return result

    result: list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]] = []
    for node in module.body:
        owner = node.name if isinstance(node, ast.ClassDef) else ""
        definitions = (
            methods(owner).values()
            if isinstance(node, ast.ClassDef)
            and (owner.startswith("Test") or owner in case_classes)
            else [node]
        )
        result.extend(
            (owner, definition)
            for definition in definitions
            if isinstance(definition, (ast.FunctionDef, ast.AsyncFunctionDef))
            and definition.name.startswith("test")
        )
    return result


def _python_source_issues(package: Package, paths: set[Path]) -> list[str]:
    """Check source contracts without importing or executing package code."""
    if package.kind != "python":
        return []
    issues = []
    if Path(package.name) / "__init__.py" in paths:
        issues.append(
            f"{package.name}/__init__.py: reserved for the installed main.py; "
            "move initialization into main.py"
        )
    source = read_regular(package.root / "main.py")
    if source is not None:
        module = ast.parse(source, filename=str(package.root / "main.py"))
        if python_test_definitions(module):
            issues.append(
                f"{package.name}/main.py: move test definitions to test_main.py",
            )
        binding = _module_main_binding(module)
        entrypoint = (
            binding.value
            if isinstance(binding, (ast.Assign, ast.AnnAssign))
            else binding
        )
        prefix = f"{package.name}/main.py: "
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
        elif isinstance(binding, (ast.Assign, ast.AnnAssign)) and isinstance(
            binding.value,
            (ast.Constant, ast.List, ast.Tuple, ast.Set, ast.Dict),
        ):
            issues.append(prefix + "main must be callable; the executable calls main()")
    tests = package.root / "test_main.py"
    if tests.relative_to(package.root.parent) in paths and tests.is_file():
        has_python_tests(tests)
    return issues


def package_description(package: Package) -> str | None:
    """Extract declared package metadata where supported."""
    default = read_regular(package.root / "default.nix")
    return source_package_description(default) if default is not None else None


def source_package_description(source: str) -> str | None:
    """Extract a literal Nix package description without evaluation."""
    with contextlib.suppress(nix_syntax.NixSyntaxError):
        return _meta_description(source)
    return None


def _attrset_expression(
    document: nix_syntax.Document,
    expression: Node,
    path: tuple[str, ...],
) -> list[Node]:
    """Find direct static bindings beneath an attribute-set expression."""
    if expression.type not in {"attrset_expression", "rec_attrset_expression"}:
        return []
    binding_set = nix_syntax.binding_set(expression)
    return [
        value
        for binding in ([] if binding_set is None else binding_set.named_children)
        if binding.type == "binding"
        and (attrpath := binding.child_by_field_name("attrpath")) is not None
        and nix_syntax.static_attrpath(document, attrpath) == path
        and (value := binding.child_by_field_name("expression")) is not None
    ]


def _meta_description(source: str) -> str | None:
    """Read the returned package's description without searching dependencies."""
    document = nix_syntax.parse(source)
    container = _package_body(document)
    while container.type == "apply_expression":
        argument = container.child_by_field_name("argument")
        if argument is not None and argument.type in {
            "attrset_expression",
            "rec_attrset_expression",
        }:
            container = argument
            break
        function = container.child_by_field_name("function")
        if function is None:
            return None
        container = function
    matches = _attrset_expression(document, container, ("meta", "description"))
    for metadata in _attrset_expression(document, container, ("meta",)):
        matches.extend(_attrset_expression(document, metadata, ("description",)))
    return nix_syntax.string_value(document, matches[0]) if len(matches) == 1 else None


def _module_main_binding(module: ast.Module) -> ast.stmt | None:
    """Find the last explicit module-level binding used by the executable."""
    for node in reversed(module.body):
        if (
            (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "main"
            )
            or (
                isinstance(node, (ast.Import, ast.ImportFrom))
                and any((alias.asname or alias.name) == "main" for alias in node.names)
            )
            or (
                isinstance(node, (ast.Assign, ast.AnnAssign))
                and node.value is not None
                and any(
                    isinstance(target, ast.Name) and target.id == "main"
                    for target in (
                        node.targets if isinstance(node, ast.Assign) else [node.target]
                    )
                )
            )
        ):
            return node
    return None


def _package_body(document: nix_syntax.Document) -> Node:
    """Find the outer package expression beneath functions and let bindings."""
    body: Node = document.root
    while body.type in {
        "function_expression",
        "let_expression",
        "parenthesized_expression",
    }:
        child = body.child_by_field_name(
            "expression" if body.type == "parenthesized_expression" else "body",
        )
        if child is None:
            msg = "Package definition has no body"
            raise CommandError(msg)
        body = child
    return body


def _resource_constructor(
    document: nix_syntax.Document,
    expression: Node,
    visited: frozenset[int] = frozenset(),
) -> str | None:
    """Resolve static constructor references and lexical let aliases."""
    if expression.start_byte in visited:
        return None
    visited = visited | {expression.start_byte}
    if expression.type == "parenthesized_expression":
        inner = expression.child_by_field_name("expression")
        return (
            None if inner is None else _resource_constructor(document, inner, visited)
        )
    if expression.type == "select_expression":
        attrpath = expression.child_by_field_name("attrpath")
        path = (
            None if attrpath is None else nix_syntax.static_attrpath(document, attrpath)
        )
        return None if path is None else path[-1]
    if expression.type != "variable_expression":
        return None
    return _resource_variable_constructor(document, expression, visited)


def _resource_variable_constructor(
    document: nix_syntax.Document,
    expression: Node,
    visited: frozenset[int],
) -> str | None:
    """Resolve aliases without crossing a binding that shadows their names."""
    name = document.text(expression)
    scope = expression.parent
    while scope is not None:
        if scope.type == "function_expression":
            parameters = [
                child
                for field in ("universal", "formals")
                if (child := scope.child_by_field_name(field)) is not None
            ]
            if any(
                node.type == "identifier" and document.text(node) == name
                for parameter in parameters
                for node in nix_syntax.walk(parameter)
            ):
                return None
        if scope.type in {"let_expression", "rec_attrset_expression"}:
            bindings = nix_syntax.binding_set(scope)
            for binding in [] if bindings is None else bindings.named_children:
                path = binding.child_by_field_name("attrpath")
                value = binding.child_by_field_name("expression")
                if (
                    path is not None
                    and nix_syntax.static_attrpath(document, path) == (name,)
                    and value is not None
                ):
                    return _resource_constructor(document, value, visited)
        scope = scope.parent
    return name if name == "derivation" else None


def _validate_flake_syntax(root: Path) -> None:
    """Parse the flake without restricting its resource definitions."""
    source = read_regular(root / "flake.nix")
    if source is not None and source.strip():
        nix_syntax.parse(source, "flake.nix")


def validate_flake_source(root: Path) -> None:
    """Validate a Git-filtered flake source without requiring Git metadata."""
    paths = source_paths(root)
    packages, issues = inspect_structure(root, paths)
    _validate_flake_syntax(root)
    for required in ("flake.lock", "flake.nix"):
        if not (root / required).is_file():
            issues.append(f"{required}: missing required regular file")
    if issues:
        formatted_issues = "\n".join(f"  - {issue}" for issue in issues)
        msg = f"repository layout validation failed:\n{formatted_issues}"
        raise CommandError(msg)
    for package in packages:
        for relative in sorted(package_files(package) & paths):
            if relative.name == "default.nix" or package.kind == "nix":
                source = root / relative
                if source.is_file():
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
        run(["git", "clone", "--", remote, str(checkout)], capture_output=False)
        yield checkout


def _source(path: Path) -> ast.Module:
    """Parse source without importing it or executing its declarations."""
    try:
        return ast.parse(path.read_bytes(), filename=str(path))
    except (OSError, SyntaxError, UnicodeError) as error:
        message = f"{path}: could not parse Python source: {error}"
        raise CommandError(message) from error


class _PythonNames:
    """Resolve lexical Python names using LibCST without importing the source."""

    def __init__(self, source: str | bytes, filename: str) -> None:
        """Index qualified references and definitions at their UTF-8 byte offsets."""
        import libcst as cst  # noqa: PLC0415 - Load metadata only for CLI inspection.
        from libcst import helpers, metadata  # noqa: PLC0415 - Keep CLI startup lightweight.

        try:
            syntax = cst.parse_module(source)
            self.module = ast.parse(syntax.code, filename=filename)
        except (cst.ParserSyntaxError, SyntaxError, UnicodeError) as error:
            message = f"{filename}: could not parse Python source: {error}"
            raise CommandError(message) from error
        encoded = syntax.code.encode("utf-8")
        self.line_offsets = [
            0,
            *(match.end() for match in re.finditer(rb"\r\n?|\n", encoded)),
        ]
        wrapper = metadata.MetadataWrapper(syntax, unsafe_skip_copy=True)
        qualified = wrapper.resolve(metadata.QualifiedNameProvider)
        spans = wrapper.resolve(metadata.ByteSpanPositionProvider)
        scopes = wrapper.resolve(metadata.ScopeProvider)
        self.references: dict[int, set[str]] = {}
        self.imported_references: dict[int, set[str]] = {}
        self.definitions: dict[int, str] = {}
        for node, span in spans.items():
            if not isinstance(node, (cst.Name, cst.FunctionDef, cst.ClassDef)):
                continue
            raw = qualified.get(node, set())
            qualified_names = raw() if callable(raw) else raw
            names = {name.name for name in qualified_names}
            if isinstance(node, cst.Name):
                sources = {name.source for name in qualified_names}
                if sources == {metadata.QualifiedNameSource.LOCAL}:
                    self.references[span.start] = names
                elif sources == {metadata.QualifiedNameSource.IMPORT}:
                    self.imported_references[span.start] = names
            elif len(names) == 1:
                self.definitions[span.start] = next(iter(names))
        self.imports = {
            name.name.split(".", 1)[0]
            for scope in set(scopes.values())
            if scope is not None
            for assignment in scope.assignments
            for name in assignment.get_qualified_names_for(assignment.name)
            if name.source == metadata.QualifiedNameSource.IMPORT
        }
        self.imports.update(
            (helpers.get_full_name_for_node(node.module) or "").split(".", 1)[0]
            for node in spans
            if isinstance(node, cst.ImportFrom)
            and node.module is not None
            and isinstance(node.names, cst.ImportStar)
        )

    def _offset(
        self,
        node: ast.expr
        | ast.arg
        | ast.FunctionDef
        | ast.AsyncFunctionDef
        | ast.ClassDef,
    ) -> int:
        return self.line_offsets[node.lineno - 1] + node.col_offset

    def names(self, node: ast.expr | ast.arg, *, imported: bool = False) -> set[str]:
        """Return canonical names, honoring imports, closures, and shadowing."""
        if isinstance(node, (ast.Name, ast.arg)):
            references = self.imported_references if imported else self.references
            return references.get(self._offset(node), set())
        if isinstance(node, ast.Attribute):
            return {
                f"{name}.{node.attr}"
                for name in self.names(node.value, imported=imported)
            }
        return set()

    def definition(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
    ) -> str:
        """Return the qualified name of a function or class declaration."""
        return self.definitions[self._offset(node)]


def _value(node: ast.expr) -> object:
    """Describe literals exactly and unresolved expressions explicitly."""
    try:
        value = ast.literal_eval(node)
        json.dumps(value)
    except (ValueError, TypeError):
        if isinstance(node, ast.Name) and node.id in {"str", "int", "float", "bool"}:
            return node.id
        return {"unknown": ast.unparse(node)}
    else:
        return value


def _command_node(tree: dict[str, Any], path: tuple[str, ...]) -> dict[str, Any]:
    """Find or create the command node at a resolved path."""
    node = tree
    for name in path:
        node = node["commands"].setdefault(name, {"arguments": [], "commands": {}})
    return node


class _LoopLiteral(ast.NodeTransformer):
    """Substitute a simple loop variable without executing its body."""

    def __init__(self, name: str, value: object) -> None:
        """Remember the literal binding for one loop iteration."""
        self.name = name
        self.value = value

    def visit_Name(self, node: ast.Name) -> ast.expr:
        """Replace reads while retaining their source locations."""
        if node.id == self.name and isinstance(node.ctx, ast.Load):
            return ast.copy_location(
                ast.parse(repr(self.value), mode="eval").body,
                node,
            )
        return node


class ArgumentDiscovery:
    """Follow static parser bindings, factory returns, and configuration helpers."""

    def __init__(self, source: str | bytes, filename: str = "<source>") -> None:
        """Index parser assignments and helper call arguments."""
        self.filename = filename
        self.names = _PythonNames(source, filename)
        self.has_argparse = "argparse" in self.names.imports
        self.argument_free = not self.names.imports.intersection(
            {"argparse", "click", "typer", "fire", "docopt", "getopt"},
        ) and not any(
            self._qualified(node) == "sys.argv"
            for node in ast.walk(self.names.module)
            if isinstance(node, (ast.Name, ast.Attribute))
        )
        self.bindings: dict[str, list[ast.expr]] = {}
        self.returns: dict[str, list[ast.expr]] = {}
        self.functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
        self.ambiguous_functions: set[str] = set()
        self.classes: dict[str, ast.ClassDef] = {}
        self.calls: list[tuple[str, ast.Call]] = []
        self.controls: dict[int, ast.expr] = {}
        self._collect(self.names.module, "")
        scopes = self._public_scopes()
        if "main" not in self.functions:
            scopes = self._public_scopes(
                scopes
                | {
                    scope
                    for scope, call in self.calls
                    if self._qualified(call.func) == "argparse.ArgumentParser"
                },
            )
        self.calls = [(scope, call) for scope, call in self.calls if scope in scopes]
        for _scope, call in self.calls:
            if (key := self._function(call.func)) is not None:
                function = self.functions[key]
                parameters = function.args.posonlyargs + function.args.args
                for parameter, argument in zip(parameters, call.args, strict=False):
                    self._bind(parameter, argument)
                for keyword in call.keywords:
                    if keyword.arg is not None:
                        for parameter in [*parameters, *function.args.kwonlyargs]:
                            if parameter.arg == keyword.arg:
                                self._bind(parameter, keyword.value)

    def _bind(
        self,
        target: ast.Name | ast.arg,
        value: ast.expr,
    ) -> None:
        for name in self.names.names(target):
            self.bindings.setdefault(name, []).append(value)

    def _bindings(self, node: ast.Name) -> list[ast.expr]:
        return [
            binding
            for name in self.names.names(node)
            for binding in self.bindings.get(name, [])
        ]

    def _function(self, node: ast.expr) -> str | None:
        if isinstance(node, ast.Name) and not self._bindings(node):
            names = self.names.names(node) & self.functions.keys()
            if len(names) == 1:
                return next(iter(names))
        return None

    def _error(self, node: ast.expr, reason: str) -> Never:
        message = (
            f"{self.filename}:{node.lineno}:{node.col_offset + 1}: "
            f"cannot inspect CLI: {reason}\n  {ast.unparse(node)}"
        )
        raise CommandError(message)

    def _collect(
        self,
        node: ast.AST,
        scope: str,
        control: ast.expr | None = None,
    ) -> None:
        if control is not None:
            self.controls[id(node)] = control
        if self._collect_branches(node, scope, control):
            return
        if isinstance(
            node,
            (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp),
        ):
            self._collect_comprehension(node, scope, control)
            return
        if self._collect_runtime_loop(node, scope, control):
            return
        if control is None and isinstance(node, ast.Match):
            control = node.subject
        if isinstance(node, ast.ClassDef):
            self.classes[self.names.definition(node)] = node
            return
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scope = self.names.definition(node)
            self.ambiguous_functions.update(self.functions.keys() & {scope})
            self.functions[scope] = node
        self._index_binding(node, scope)
        for child in ast.iter_child_nodes(node):
            self._collect(child, scope, control)

    def _index_binding(self, node: ast.AST, scope: str) -> None:
        """Record simple assignments, factory returns, and calls."""
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self._bind(target, node.value)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.value
        ):
            self._bind(node.target, node.value)
        elif isinstance(node, ast.Return) and node.value:
            self.returns.setdefault(scope, []).append(node.value)
        elif isinstance(node, ast.Call):
            self.calls.append((scope, node))

    def _collect_branches(
        self,
        node: ast.AST,
        scope: str,
        control: ast.expr | None,
    ) -> bool:
        """Select literal branches and retain uncertain exception handlers."""
        if isinstance(node, ast.If):
            self._collect(node.test, scope, control)
            try:
                condition = self._branch_value(node.test)
            except (ValueError, TypeError):
                for child in [*node.body, *node.orelse]:
                    self._collect(child, scope, control or node.test)
            else:
                for child in node.body if condition else node.orelse:
                    self._collect(child, scope, control)
            return True
        if isinstance(node, (ast.Try, ast.TryStar)):
            for child in [*node.body, *node.orelse, *node.finalbody]:
                self._collect(child, scope, control)
            for handler in node.handlers:
                self._collect(
                    handler,
                    scope,
                    control or ast.Constant(value="exception handler"),
                )
            return True
        return False

    @staticmethod
    def _branch_value(node: ast.expr) -> object:
        """Interpret literals and the standard executable-module guard."""
        if (
            isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Name)
            and node.left.id == "__name__"
            and len(node.ops) == 1
            and isinstance(node.ops[0], (ast.Eq, ast.NotEq))
            and _value(node.comparators[0]) == "__main__"
        ):
            return isinstance(node.ops[0], ast.Eq)
        return ast.literal_eval(node)

    def _collect_runtime_loop(
        self,
        node: ast.AST,
        scope: str,
        control: ast.expr | None,
    ) -> bool:
        """Collect eager loop inputs before marking iteration-dependent statements."""
        if isinstance(node, ast.For) and self._collect_literal_loop(
            node,
            scope,
            control,
        ):
            return True
        if isinstance(node, (ast.For, ast.AsyncFor)):
            self._collect(node.iter, scope, control)
            self._collect(node.target, scope, control or node.iter)
            for statement in [*node.body, *node.orelse]:
                self._collect(statement, scope, control or node.iter)
            return True
        if isinstance(node, ast.While):
            self._collect(node.test, scope, control)
            try:
                condition = self._branch_value(node.test)
            except (ValueError, TypeError):
                condition = True
            for statement in node.body if condition else []:
                self._collect(statement, scope, control or node.test)
            for statement in node.orelse:
                self._collect(
                    statement,
                    scope,
                    (control or node.test) if condition else control,
                )
            return True
        return False

    def _collect_comprehension(
        self,
        node: ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp,
        scope: str,
        control: ast.expr | None,
    ) -> None:
        """Collect eager iterables separately from conditional comprehension parts."""
        first, *remaining = node.generators
        self._collect(first.iter, scope, control)
        uncertain = control or node
        for child in [first.target, *first.ifs, *remaining]:
            self._collect(child, scope, uncertain)
        values = (
            [node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]
        )
        for value in values:
            self._collect(value, scope, uncertain)

    def _collect_literal_loop(
        self,
        node: ast.For,
        scope: str,
        control: ast.expr | None,
    ) -> bool:
        """Expand finite literal loops whose variable is never rebound."""
        if not isinstance(node.target, ast.Name):
            return False
        try:
            values = ast.literal_eval(node.iter)
        except (ValueError, TypeError):
            return False
        if not isinstance(values, (list, tuple, str)) or any(
            isinstance(
                child,
                (
                    ast.Break,
                    ast.Continue,
                    ast.FunctionDef,
                    ast.AsyncFunctionDef,
                    ast.ClassDef,
                ),
            )
            or (
                isinstance(child, ast.Name)
                and child.id == node.target.id
                and isinstance(child.ctx, (ast.Store, ast.Del))
            )
            for statement in node.body
            for child in ast.walk(statement)
        ):
            return False
        for value in values:
            for statement in node.body:
                expanded = _LoopLiteral(node.target.id, value).visit(
                    deepcopy(statement),
                )
                self._collect(expanded, scope, control)
        for statement in node.orelse:
            self._collect(statement, scope, control)
        return True

    def _qualified(self, node: ast.expr) -> str:
        names = self.names.names(node, imported=True)
        return next(iter(names)) if len(names) == 1 else ""

    def _sources(self, node: ast.expr) -> list[ast.expr]:
        """Follow assignments, helper returns, and method receivers."""
        if isinstance(node, ast.Name):
            return self._bindings(node)
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute):
                return [node.func.value]
            if (key := self._function(node.func)) is not None:
                return self.returns.get(key, [])
        return []

    def _conditions(
        self,
        node: ast.expr,
        seen: frozenset[int] = frozenset(),
    ) -> set[ast.expr]:
        """Follow control dependencies through parser aliases and helper parameters."""
        key = id(node)
        if key in seen:
            return set()
        seen = seen | {key}
        result = {self.controls[id(node)]} if id(node) in self.controls else set()
        for value in self._sources(node):
            result.update(self._conditions(value, seen))
        return result

    def _check_structure(self, call: ast.Call, roots: set[str]) -> None:
        """Reject runtime-dependent construction only for the selected CLI."""
        paths = self._resolve(call)
        if not paths and isinstance(call.func, ast.Attribute):
            paths = self._resolve(call.func.value)
        if any(not roots or path[0] in roots for path in paths) and self._conditions(
            call,
        ):
            self._error(call, "CLI construction depends on unresolved control flow")

    def _parser_keywords(self, call: ast.Call) -> dict[str, ast.expr]:
        """Expand literal parser settings that can affect command structure."""
        result = {}
        for option in call.keywords:
            if option.arg is not None:
                result[option.arg] = option.value
                continue
            if not isinstance(option.value, ast.Dict) or any(
                key is None or not isinstance(_value(key), str)
                for key in option.value.keys
            ):
                self._error(call, "parser keyword expansion must be a literal mapping")
            for key, value in zip(
                option.value.keys,
                option.value.values,
                strict=True,
            ):
                if key is not None:
                    result[cast("str", _value(key))] = value
        return result

    def _resolve(
        self,
        node: ast.expr,
        seen: frozenset[int] = frozenset(),
    ) -> set[tuple[str, ...]]:
        key = id(node)
        if key in seen:
            return set()
        seen = seen | {key}
        if isinstance(node, ast.Name):
            return set().union(
                *(self._resolve(value, seen) for value in self._bindings(node)),
            )
        if not isinstance(node, ast.Call):
            return set()
        return self._resolve_call(node, seen)

    def _resolve_call(
        self,
        node: ast.Call,
        seen: frozenset[int],
    ) -> set[tuple[str, ...]]:
        if self._qualified(node.func) == "argparse.ArgumentParser":
            return {(f"{node.lineno}:{node.col_offset}:{id(node)}",)}
        if (key := self._function(node.func)) is not None:
            return set().union(
                *(self._resolve(value, seen) for value in self.returns.get(key, [])),
            )
        if isinstance(node.func, ast.Attribute):
            parents = self._resolve(node.func.value, seen)
            if node.func.attr in {
                "add_subparsers",
                "add_argument_group",
                "add_mutually_exclusive_group",
            }:
                return parents
            if node.func.attr == "add_parser":
                if not parents:
                    return set()
                keywords = self._parser_keywords(node)
                name_node = node.args[0] if node.args else keywords.get("name")
                name = _value(name_node) if name_node is not None else None
                aliases = _value(keywords["aliases"]) if "aliases" in keywords else []
                if not isinstance(name, str):
                    self._error(node, "subcommand name must be a literal string")
                if not isinstance(aliases, (list, tuple)) or not all(
                    isinstance(alias, str) for alias in aliases
                ):
                    self._error(node, "subcommand aliases must be literal strings")
                return {
                    (*parent, label) for parent in parents for label in [name, *aliases]
                }
        return set()

    def _public_scopes(self, initial: set[str] | None = None) -> set[str]:
        """Find functions reachable from the public entry point."""
        scopes = initial or {"", "main"}
        while True:
            reachable = scopes | {
                key
                for scope, call in self.calls
                if scope in scopes and (key := self._function(call.func)) is not None
            }
            if reachable == scopes:
                break
            scopes = reachable
        return scopes

    def _public_roots(self) -> set[str]:
        """Select consumed parsers, rejecting unsupported public construction."""
        roots: set[str] = set()
        scopes = self._public_scopes()
        for scope, call in self.calls:
            if scope not in scopes:
                continue
            if isinstance(call.func, ast.Name):
                if self._function(call.func) in self.ambiguous_functions:
                    self._error(
                        call,
                        f"ambiguous helper {call.func.id!r}: multiple definitions",
                    )
                classes = self.names.names(call.func) & self.classes.keys()
                definition = (
                    self.classes[next(iter(classes))] if len(classes) == 1 else None
                )
                if definition is not None and any(
                    (
                        isinstance(node, (ast.Name, ast.Attribute))
                        and self._qualified(node) == "argparse.ArgumentParser"
                    )
                    or (
                        isinstance(node, ast.Attribute)
                        and node.attr in {"add_argument", "add_parser"}
                    )
                    for node in ast.walk(definition)
                ):
                    self._error(
                        call,
                        "class-based argparse construction is not supported",
                    )
            if not isinstance(call.func, ast.Attribute) or call.func.attr not in {
                "parse_args",
                "parse_known_args",
                "parse_intermixed_args",
                "parse_known_intermixed_args",
            }:
                continue
            paths = self._resolve(call.func.value)
            if not paths and self.has_argparse:
                self._error(call, "cannot resolve the public argparse parser")
            self._check_structure(call, set())
            roots.update(path[0] for path in paths)
            if len(roots) > 1:
                self._error(call, "multiple public parsers cannot form one CLI")
        return roots

    def _check_parents(self, call: ast.Call, roots: set[str]) -> None:
        """Reject inherited interfaces until their argument scopes can be resolved."""
        contexts = self._resolve(call)
        if not any(not roots or context[0] in roots for context in contexts):
            return
        self._check_structure(call, roots)
        options = self._parser_keywords(call)
        if "parents" in options and _value(options["parents"]) not in ([], ()):
            self._error(call, "argparse parents= inheritance is not supported")
        if (
            "parser_class" in options
            and self._qualified(options["parser_class"]) != "argparse.ArgumentParser"
        ):
            self._error(call, "custom subcommand parser classes cannot be inspected")

    def _check_helpers(self, roots: set[str]) -> None:
        """Do not let opaque helpers or parser methods silently change the interface."""
        methods = {
            "add_argument",
            "add_parser",
            "add_subparsers",
            "add_argument_group",
            "add_mutually_exclusive_group",
            "set_defaults",
            "get_default",
            "parse_args",
            "parse_known_args",
            "parse_intermixed_args",
            "parse_known_intermixed_args",
            "error",
            "exit",
            "print_help",
            "print_usage",
            "format_help",
            "format_usage",
        }
        for _scope, call in self.calls:
            if isinstance(call.func, ast.Name):
                self._parser_input(call.func, roots)
            receiver = (
                self._resolve(call.func.value)
                if isinstance(call.func, ast.Attribute)
                else set()
            )
            arguments = [*call.args, *(keyword.value for keyword in call.keywords)]
            passed = {
                index
                for index, argument in enumerate(arguments)
                if self._parser_input(argument, roots)
            }
            if any(path[0] in roots for path in receiver):
                if isinstance(call.func, ast.Attribute) and call.func.attr in methods:
                    continue
                self._error(call, "unsupported parser method may change the CLI")
            if not passed:
                continue
            if (key := self._function(call.func)) is not None:
                self._check_helper_binding(self.functions[key], call, passed)
                continue
            self._error(call, "external helper receiving a parser cannot be inspected")

    def _check_helper_binding(
        self,
        function: ast.FunctionDef | ast.AsyncFunctionDef,
        call: ast.Call,
        passed: set[int],
    ) -> None:
        """Require forwarded parsers to bind to explicit helper parameters."""
        parameters = function.args.posonlyargs + function.args.args
        for index in passed:
            if index < len(call.args):
                if isinstance(call.args[index], ast.Starred) or index >= len(
                    parameters,
                ):
                    self._error(
                        call,
                        "cannot bind parser argument to helper parameter",
                    )
            elif call.keywords[index - len(call.args)].arg not in {
                parameter.arg for parameter in [*parameters, *function.args.kwonlyargs]
            }:
                self._error(
                    call,
                    "cannot bind parser argument to helper parameter",
                )

    def _parser_input(
        self,
        node: ast.expr,
        roots: set[str],
        seen: frozenset[int] = frozenset(),
    ) -> bool:
        """Find parser inputs wrapped in literal containers or aliases."""
        key = id(node)
        if key in seen:
            return False
        seen = seen | {key}
        if any(path[0] in roots for path in self._resolve(node)):
            return True
        if isinstance(node, ast.Name):
            return any(
                self._parser_input(value, roots, seen) for value in self._bindings(node)
            )
        if (
            isinstance(node, ast.Attribute)
            and node.attr.startswith("add_")
            and self._parser_input(node.value, roots, seen)
        ):
            self._error(node, "indirect parser mutation cannot be inspected")
        if isinstance(node, (ast.Dict, ast.List, ast.Tuple, ast.Set, ast.Starred)):
            return any(
                self._parser_input(child, roots, seen)
                for child in ast.iter_child_nodes(node)
                if isinstance(child, ast.expr)
            )
        return False

    def _groups(
        self,
        node: ast.expr,
        seen: frozenset[int] = frozenset(),
    ) -> set[int]:
        """Find group declarations through the same bindings used for parser scopes."""
        key = id(node)
        if key in seen:
            return set()
        seen = seen | {key}
        groups: set[int] = set()
        for value in self._sources(node):
            groups.update(self._groups(value, seen))
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"add_argument_group", "add_mutually_exclusive_group"}
        ):
            groups.add(id(node))
        return groups

    def _group_members(
        self,
        call: ast.Call,
        contexts: set[tuple[str, ...]],
        roots: set[str],
    ) -> dict[tuple[str, ...], list[list[object]]]:
        """List group members by their declared argument names."""
        members: dict[tuple[str, ...], list[list[object]]] = {
            context: [] for context in contexts
        }
        for _argument_scope, argument in self.calls:
            if (
                not isinstance(argument.func, ast.Attribute)
                or argument.func.attr != "add_argument"
            ):
                continue
            if id(call) not in self._groups(argument.func.value):
                continue
            for path in self._resolve(argument.func.value):
                if path[0] in roots and path[1:] in members:
                    members[path[1:]].append(
                        [_value(name) for name in argument.args],
                    )
        return members

    def _report_metadata(self, result: dict[str, Any], roots: set[str]) -> None:
        """Describe parser defaults and group constraints without evaluating values."""
        for _scope, call in self.calls:
            if not isinstance(call.func, ast.Attribute) or call.func.attr not in {
                "set_defaults",
                "add_argument_group",
                "add_mutually_exclusive_group",
            }:
                continue
            paths = self._resolve(call.func.value)
            contexts = {path[1:] for path in paths if path[0] in roots}
            if not contexts:
                continue
            options = {
                keyword.arg or "**kwargs": _value(keyword.value)
                for keyword in call.keywords
            }
            if call.func.attr == "set_defaults":
                if self._conditions(call):
                    options = {name: {"unknown": ast.unparse(call)} for name in options}
                for context in sorted(contexts):
                    _command_node(result, context).setdefault("defaults", {}).update(
                        options,
                    )
                continue
            self._check_structure(call, roots)
            for name, value in zip(("title", "description"), call.args, strict=False):
                options[name] = _value(value)
            members = self._group_members(call, contexts, roots)
            for context in sorted(contexts):
                _command_node(result, context).setdefault("groups", []).append(
                    {
                        "arguments": members[context],
                        "exclusive": call.func.attr == "add_mutually_exclusive_group",
                        **options,
                    },
                )

    def _report_argument(
        self,
        result: dict[str, Any],
        call: ast.Call,
        receiver: ast.expr,
        roots: set[str],
    ) -> None:
        """Attach a declaration only when its parser and argument names are known."""
        contexts = self._resolve(receiver)
        if not contexts:
            if self.has_argparse:
                self._error(call, "cannot resolve the argument parser")
            return
        public_contexts = {
            context[1:] for context in contexts if not roots or context[0] in roots
        }
        if not public_contexts:
            return
        self._check_structure(call, roots)
        names = [_value(argument) for argument in call.args]
        if not names or any(not isinstance(name, str) for name in names):
            self._error(call, "argument names must be literal strings")
        options = {
            keyword.arg or "**kwargs": _value(keyword.value)
            for keyword in call.keywords
        }
        for context in sorted(public_contexts):
            _command_node(result, context)["arguments"].append(
                {"names": names, **options},
            )

    def _inspection_roots(self) -> set[str]:
        """Require a single root, falling back to unconsumed parser factories."""
        roots = self._public_roots()
        if not roots:
            for _scope, call in self.calls:
                if self._qualified(call.func) == "argparse.ArgumentParser":
                    roots.update(path[0] for path in self._resolve(call))
                    if len(roots) > 1:
                        self._error(call, "multiple public parsers cannot form one CLI")
        return roots

    def report(self) -> dict[str, Any]:
        """Return a command tree, failing when its interface cannot be resolved."""
        roots = self._inspection_roots()
        self._check_helpers(roots)
        result: dict[str, Any] = {
            "arguments": [],
            "commands": {},
        }
        for _scope, call in self.calls:
            if self._qualified(call.func) == "argparse.ArgumentParser":
                self._check_parents(call, roots)
            if not isinstance(call.func, ast.Attribute):
                continue
            if call.func.attr == "add_subparsers":
                self._check_parents(call, roots)
            if call.func.attr == "add_parser":
                contexts = self._resolve(call)
                if not contexts and self.has_argparse:
                    self._error(call, "cannot resolve the subcommand parser")
                self._check_parents(call, roots)
                paths = {path[1:] for path in contexts if not roots or path[0] in roots}
                for path in sorted(paths):
                    _command_node(result, path)
            if call.func.attr == "add_argument":
                self._report_argument(result, call, call.func.value, roots)
        if not self.argument_free and not any(
            self._qualified(call.func) == "argparse.ArgumentParser"
            for _scope, call in self.calls
        ):
            message = f"{self.filename}: cannot inspect CLI: no supported parser found"
            raise CommandError(message)
        self._report_metadata(result, roots)
        return result


def _python_library(package: Package) -> bool:
    """Recognize an explicitly non-executable package without evaluating Nix."""
    source = read_regular(package.root / "default.nix")
    if source is None:
        return False
    document = nix_syntax.parse(source)
    body = _package_body(document)
    argument = body.child_by_field_name("argument")
    values = _attrset_expression(
        document,
        body if argument is None else argument,
        ("executable",),
    )
    return len(values) == 1 and document.text(values[0]) == "false"


def _compact_cli(command: dict[str, Any]) -> dict[str, Any]:
    """Omit empty command containers while preserving declared argument values."""
    result = {
        key: value
        for key, value in command.items()
        if key not in {"arguments", "commands"} or value
    }
    if "commands" in result:
        result["commands"] = {
            name: _compact_cli(child) for name, child in result["commands"].items()
        }
    return result


def inspect_repository(root: Path) -> dict[str, Any]:
    """Describe root resources by name, with their interfaces and test identifiers."""
    if not (root / "flake.nix").is_file():
        message = f"{root}: expected an Afairesi repository with flake.nix"
        raise CommandError(message)
    paths = source_paths(root)
    host_names = {host.name for host in detect_hosts(root, paths)}
    resources: dict[str, Any] = {name: {"type": "host"} for name in host_names}
    for package in detect_packages(root, paths):
        cli = None
        if package.kind == "python":
            path = package.root / "main.py"
            if _python_library(package):
                _source(path)
            else:
                cli = _compact_cli(
                    ArgumentDiscovery(path.read_bytes(), str(path)).report(),
                )
        tests = package.root / "test_main.py"
        test_module = (
            _source(tests)
            if package.kind == "python"
            and tests.relative_to(root) in paths
            and tests.is_file()
            else None
        )
        description = package_description(package)
        identifiers = (
            sorted(
                f"{owner}::{definition.name}" if owner else definition.name
                for owner, definition in python_test_definitions(test_module)
            )
            if test_module is not None
            else ["test_main.lean"]
            if package.kind == "lean4"
            and Path(package.name) / "test_main.lean" in paths
            and (package.root / "test_main.lean").is_file()
            else ["test_main.nix"]
            if package.kind == "nix"
            and Path(package.name) / "test_main.nix" in paths
            and (package.root / "test_main.nix").is_file()
            else []
        )
        resource: dict[str, Any] = {
            "type": [package.kind, "host"]
            if package.name in host_names
            else package.kind,
        }
        if description:
            resource["description"] = description
        if cli is not None:
            resource["cli"] = cli
        if identifiers:
            resource["tests"] = identifiers
        resources[package.name] = resource
    return dict(sorted(resources.items()))


def _inspection_git_files(
    root: Path,
    revision: str | None,
) -> list[tuple[str, str, Path]]:
    """List raw blobs from a tree or the index without refreshing Git state."""
    if revision is None:
        listing = git(root, ["ls-files", "--stage", "-z"]).stdout
    else:
        tree = git(
            root,
            ["rev-parse", "--verify", "--end-of-options", f"{revision}^{{tree}}"],
        ).stdout.strip()
        listing = git(root, ["ls-tree", "-r", "-z", "--full-tree", tree]).stdout
    files = []
    for entry in listing.split("\0"):
        if not entry:
            continue
        metadata, name = entry.split("\t", 1)
        mode, middle, last = metadata.split()
        if revision is None and last != "0":
            message = "cannot inspect an unmerged index; resolve conflicts first"
            raise CommandError(message)
        if mode != "160000":
            files.append((mode, middle if revision is None else last, Path(name)))
    return files


def _inspection_git_report(
    root: Path,
    revision: str | None = None,
    *,
    working_tree: bool = False,
) -> dict[str, Any]:
    """Inspect tracked sources in a temporary directory without checkout filters."""
    files = _inspection_git_files(root, revision)
    with tempfile.TemporaryDirectory(prefix="afairesi-inspect-diff-") as directory:
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


def _inspection_diff_revisions(
    options: argparse.Namespace,
    cli: argparse.ArgumentParser,
    root: Path,
) -> tuple[str | None, str | None]:
    """Resolve named comparison endpoints and an optional common ancestor."""
    left, right = options.from_revision, options.to_revision
    if options.cached and right is not None:
        cli.error("inspect-diff --cached cannot be combined with --to")
    if options.merge_base:
        if left is None or right is None:
            cli.error("inspect-diff --merge-base requires --from and --to")
        commits = [
            git(
                root,
                ["rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"],
            ).stdout.strip()
            for ref in (left, right)
        ]
        left = git(root, ["merge-base", *commits]).stdout.strip()
    return left, right


def _dispatch_inspect_diff(
    options: argparse.Namespace,
    cli: argparse.ArgumentParser,
    root: Path,
) -> None:
    """Render structural inspection changes with jd's native diff implementation."""
    root = repository_root(root)
    left_revision, right_revision = _inspection_diff_revisions(options, cli, root)
    if options.cached:
        head = git(root, ["rev-parse", "--verify", "HEAD"], check=False)
        before = (
            _inspection_git_report(root, left_revision or "HEAD")
            if left_revision is not None or head.returncode == 0
            else {}
        )
        after = _inspection_git_report(root)
    else:
        before = _inspection_git_report(root, left_revision)
        after = (
            _inspection_git_report(root, right_revision)
            if right_revision is not None
            else _inspection_git_report(root, working_tree=True)
        )
    if options.reverse:
        before, after = after, before
    with tempfile.TemporaryDirectory(prefix="afairesi-inspect-diff-") as directory:
        left, right = (Path(directory) / name for name in ("before.json", "after.json"))
        left.write_text(json.dumps(before), encoding="utf-8")
        right.write_text(json.dumps(after), encoding="utf-8")
        completed = run(
            ["jd", *(["-color"] if options.color else []), left.name, right.name],
            cwd=Path(directory),
            check=False,
        )
    if completed.returncode not in {0, 1}:
        sys.stderr.write(completed.stderr)
        raise SystemExit(completed.returncode)
    sys.stdout.write(completed.stdout)
    if options.exit_code and completed.returncode:
        raise SystemExit(1)


def _dispatch_inspect(
    options: argparse.Namespace,
    cli: argparse.ArgumentParser,
) -> None:
    """Inspect local or remote sources without executing package code."""
    try:
        value = options.repository
        remote = value is not None and (
            "://" in value
            or (":" in value.split("/", 1)[0] and not Path(value).exists())
        )
        repository = (
            inspection_remote_repository(value)
            if remote
            else contextlib.nullcontext(
                resolve_repository_path(Path(value))
                if value is not None
                else repository_root(),
            )
        )
        with repository as root:
            if options.command == "inspect-diff":
                _dispatch_inspect_diff(options, cli, root)
                return
            report = inspect_repository(root)
    except CommandError as error:
        cli.exit(1, f"{cli.prog}: {error}\n")
    sys.stdout.write(json.dumps(report, indent=2) + "\n")


def _configure_inspection_target(inspection: argparse.ArgumentParser) -> None:
    """Declare an optional repository path or remote for inspection commands."""
    inspection.add_argument(
        "repository",
        nargs="?",
        metavar="PATH_OR_REMOTE",
        help=(
            "repository directory or Git URL (default: current repository); "
            "relative paths use the Git root, or the current directory outside Git"
        ),
    )


def _configure_inspect_diff_parser(difference: argparse.ArgumentParser) -> None:
    """Configure named revisions and structural diff output."""
    _configure_inspection_target(difference)
    difference.add_argument(
        "--from",
        dest="from_revision",
        metavar="REV",
        help="baseline revision (default: index, or HEAD with --cached)",
    )
    difference.add_argument(
        "--to",
        dest="to_revision",
        metavar="REV",
        help="target revision (default: working tree); incompatible with --cached",
    )
    difference.add_argument(
        "--merge-base",
        action="store_true",
        help="compare the common ancestor of --from and --to with --to; requires both",
    )
    difference.add_argument(
        "--cached",
        "--staged",
        dest="cached",
        action="store_true",
        help="compare HEAD (or --from REV) with index; before first commit use {}",
    )
    difference.add_argument(
        "--color",
        action="store_true",
        help="colorize the diff with ANSI escapes, including when piped",
    )
    difference.add_argument(
        "--exit-code",
        action="store_true",
        help="exit 1 for interface changes, 0 for no changes (default: exit 0)",
    )
    difference.add_argument(
        "-R",
        "--reverse",
        action="store_true",
        help="reverse the comparison",
    )


def parser() -> argparse.ArgumentParser:
    """Construct the inspection command-line parser."""
    result = argparse.ArgumentParser(
        prog="afairesi",
        description=("Inspect repositories in the afairesi computing environment."),
    )
    commands = result.add_subparsers(
        dest="command", title="commands", metavar="COMMAND"
    )
    inspection = commands.add_parser(
        "inspect",
        help="inspect repository interfaces and test identifiers",
        description=(
            "Print JSON describing hosts, packages, CLI arguments, "
            "and test identifiers. "
            "Resource type is the package kind or 'host'. "
            "Empty optional fields are omitted; for Python packages, cli: {} "
            "denotes an executable without arguments. "
            "Fail if CLI declarations cannot be resolved reliably. "
            "Inspect source without executing it."
        ),
    )
    _configure_inspection_target(inspection)
    difference = commands.add_parser(
        "inspect-diff",
        help="diff inspected interfaces in jd format",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Print a jd-compatible structural diff of repository inspections. "
            "Compare the index with tracked working-tree sources by default. "
            "Select revisions with --from and --to; use --merge-base to compare "
            "their common ancestor with --to. "
            "Untracked files and implementation-only changes are omitted. "
            "Inspect source without executing it or changing Git state."
        ),
        epilog=(
            "Examples:\n"
            "  afairesi inspect-diff ../project --from HEAD~1 --to HEAD\n"
            "  afairesi inspect-diff --cached | less\n"
            "  afairesi inspect-diff --cached --color | less -R\n"
            "  afairesi inspect-diff --cached | jd -t jd2patch | jless\n"
            "  afairesi inspect-diff --cached > change.jd\n"
            "  jd -p change.jd before.json\n"
            "\nFor patching, before.json must contain the baseline inspection."
        ),
    )
    _configure_inspect_diff_parser(difference)

    return result


def check_source_main() -> None:
    """Validate an exported flake source without changing it."""
    cli = argparse.ArgumentParser(description="Validate the flake source structure.")
    cli.add_argument(
        "source",
        type=Path,
        help="path to the exported flake source",
    )
    options = cli.parse_args()
    try:
        validate_flake_source(options.source.resolve())
    except (CommandError, OSError, SyntaxError, ValueError) as error:
        sys.stderr.write((f"error: {error}") + "\n")
        raise SystemExit(1) from error


def _dispatch_command(
    options: argparse.Namespace,
    cli: argparse.ArgumentParser,
) -> None:
    """Run the selected inspection command."""
    if options.command is None:
        cli.print_help()
    elif options.command in {"inspect", "inspect-diff"}:
        _dispatch_inspect(options, cli)


def main() -> None:
    """Dispatch inspection commands."""
    cli = parser()
    options = cli.parse_args()
    try:
        _dispatch_command(options, cli)
    except KeyboardInterrupt:
        sys.exit(130)
    except (
        CommandError,
        OSError,
        SyntaxError,
        ValueError,
    ) as error:
        sys.stderr.write((f"error: {error}") + "\n")
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()

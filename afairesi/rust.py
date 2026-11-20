# Copyright (c) 2026- Paschalis Bizopoulos
"""Read Rust test functions from Cargo targets and their declared modules."""

# ruff: noqa: INP001 - mkPythonPackage installs this file inside the package.

import tomllib
from os.path import normpath
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tree_sitter_language_pack import get_parser

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from tree_sitter import Node


def _targets(
    root: Path, manifest: dict[str, Any], includes: Callable[[Path], bool]
) -> Iterator[Path]:
    """Resolve conventional Cargo test harnesses and explicit target overrides."""
    package = manifest.get("package", {})
    if not package:
        return
    library = manifest.get("lib", {})
    if (
        ("lib" in manifest or package.get("autolib", True))
        and library.get("test", True)
        and library.get("harness", True)
    ):
        yield root / library.get("path", "src/lib.rs")
    for kind, folder in (
        ("bin", "src/bin"),
        ("test", "tests"),
        ("example", "examples"),
        ("bench", "benches"),
    ):
        directory = root / folder
        candidates = {
            path.stem: path for path in directory.glob("*.rs") if includes(path)
        }
        candidates.update(
            {
                path.parent.name: path
                for path in directory.glob("*/main.rs")
                if includes(path)
            }
        )
        if kind == "bin" and includes(root / "src/main.rs"):
            candidates[package["name"]] = root / "src/main.rs"
        declared = manifest.get(kind, [])
        for target in declared:
            name = target["name"]
            default = candidates.pop(name, directory / (name + ".rs"))
            if target.get("test", kind in {"bin", "test"}) and target.get(
                "harness", True
            ):
                yield root / target["path"] if "path" in target else default
        modern = package.get("edition", "2015") != "2015"
        if kind in {"bin", "test"} and package.get(
            f"auto{kind}s", modern or not declared
        ):
            yield from candidates.values()


def _field(node: Node, name: str) -> str:
    value = node.child_by_field_name(name)
    return (value.text or b"").decode() if value is not None else ""


def _attributes(item: Node) -> dict[str, Node]:
    """Index the immediately preceding outer attributes, ignoring comments."""
    attributes = {}
    previous = item.prev_named_sibling
    while previous is not None and previous.type in {
        "attribute_item",
        "line_comment",
        "block_comment",
    }:
        if (
            previous.type == "attribute_item"
            and (attr := previous.named_child(0))
            and (key := attr.named_child(0))
        ):
            attributes[(key.text or b"").decode()] = attr
        previous = previous.prev_named_sibling
    return attributes


class _Crate:
    """Follow module declarations, keeping source paths and Rust names distinct."""

    def __init__(self, root: Path, tracked: set[Path] | None) -> None:
        self.root = root.resolve()
        self.tracked = tracked
        self.parser = get_parser("rust")
        self.names: set[str] = set()

    def includes(self, path: Path) -> bool:
        return (
            path.suffix == ".rs"
            and (self.tracked is None or path in self.tracked)
            and path.is_file()
            and path.resolve().is_relative_to(self.root)
        )

    def read(
        self, path: Path, directory: Path, prefix: str, ancestors: frozenset[Path]
    ) -> None:
        path = Path(normpath(path))
        if not self.includes(path) or path.resolve() in ancestors:
            return
        tree = self.parser.parse(path.read_bytes())
        self.visit(
            tree.root_node, directory, path.parent, prefix, ancestors | {path.resolve()}
        )

    def visit(
        self,
        parent: Node,
        directory: Path,
        attribute_dir: Path,
        prefix: str,
        ancestors: frozenset[Path],
    ) -> None:
        for item in parent.named_children:
            if item.type == "function_item":
                marker = _attributes(item).get("test")
                if marker is not None and marker.named_child_count == 1:
                    self.names.add(prefix + _field(item, "name"))
            elif item.type == "mod_item":
                name = _field(item, "name")
                qualifier = prefix + name + "::"
                override = _attributes(item).get("path")
                location = directory / name.removeprefix("r#")
                if override is not None:
                    value = override.child_by_field_name("value")
                    if value is None or value.type != "string_literal":
                        continue
                    location = attribute_dir / _field(override, "value")[1:-1]
                if body := item.child_by_field_name("body"):
                    self.visit(body, location, location, qualifier, ancestors)
                elif override is not None:
                    self.read(location, location.parent, qualifier, ancestors)
                else:
                    target = location.with_name(location.name + ".rs")
                    if not self.includes(target):
                        target = location / "mod.rs"
                    self.read(target, location, qualifier, ancestors)


def cargo_names(root: Path, tracked: set[Path] | None) -> set[str]:
    """List static #[test] functions reachable from this package's test harnesses."""
    path = root / "Cargo.toml"
    try:
        manifest = tomllib.loads(path.read_text(encoding="utf-8"))
        crate = _Crate(root, tracked)
        for target in _targets(root, manifest, crate.includes):
            crate.read(target, target.parent, "", frozenset())
    except (KeyError, TypeError, AttributeError, tomllib.TOMLDecodeError) as error:
        msg = f"{path}: invalid Cargo manifest: {error}"
        raise ValueError(msg) from error
    return crate.names

#!/usr/bin/env python3
# Copyright (c) 2026- Paschalis Bizopoulos
"""Canonicalize ordering and nesting in Nix expressions."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING, cast

import nix_syntax

if TYPE_CHECKING:
    from tree_sitter import Node


@dataclass(frozen=True)
class Binding:  # noqa: D101
    path: tuple[str, ...] | None
    value: str | None
    raw: str
    nested: tuple[Binding, ...] | None = None


def _replace_children(document: nix_syntax.Document, node: Node) -> str:
    source = document.source[node.start_byte : node.end_byte]
    edits = []
    for child in node.named_children:
        rendered = render_node(document, child).encode()
        original = document.source[child.start_byte : child.end_byte]
        if rendered != original:
            edits.append(
                (
                    child.start_byte - node.start_byte,
                    child.end_byte - node.start_byte,
                    rendered,
                ),
            )
    return cast("bytes", nix_syntax.apply_edits(source, edits)).decode()


def _binding(document: nix_syntax.Document, node: Node) -> Binding:
    rendered = _replace_children(document, node)
    if node.type != "binding":
        return Binding(None, None, rendered)
    attrpath = node.child_by_field_name("attrpath")
    expression = node.child_by_field_name("expression")
    if attrpath is None or expression is None:
        return Binding(None, None, rendered)
    path = nix_syntax.static_attrpath(document, attrpath)
    value = render_node(document, expression)
    nested = _nested_bindings(document, expression)
    return Binding(path, value, rendered, nested)


def _nested_bindings(
    document: nix_syntax.Document,
    expression: Node,
) -> tuple[Binding, ...] | None:
    if expression.type != "attrset_expression" or document.text(
        expression,
    ).lstrip().startswith("rec"):
        return None
    binding_set = next(
        (child for child in expression.named_children if child.type == "binding_set"),
        None,
    )
    if binding_set is None:
        return ()
    expanded: list[Binding] = []
    for child in binding_set.named_children:
        attrs = child.child_by_field_name("attrs")
        if (
            child.type == "inherit"
            and attrs is not None
            and all(attr.type == "identifier" for attr in attrs.named_children)
            and not any(node.type == "comment" for node in nix_syntax.walk(child))
        ):
            expanded.extend(
                Binding((document.text(attr),), document.text(attr), "")
                for attr in attrs.named_children
            )
        else:
            expanded.append(_binding(document, child))
    bindings = tuple(expanded)
    return bindings if all(binding.path is not None for binding in bindings) else None


def _paths_unambiguous(paths: list[tuple[str, ...]]) -> bool:
    ordered = sorted(paths)
    return all(right[: len(left)] != left for left, right in pairwise(ordered))


def _normalize(bindings: list[Binding]) -> list[Binding]:
    static = [binding for binding in bindings if binding.path]
    opaque = [binding for binding in bindings if not binding.path]
    groups: dict[str, list[Binding]] = {}
    for binding in static:
        if not (binding.path is not None):
            raise AssertionError
        groups.setdefault(binding.path[0], []).append(binding)
    result = opaque
    for root in sorted(groups):
        group = groups[root]
        paths = [binding.path for binding in group if binding.path is not None]
        compatible = (
            len(group) == 1
            and (
                len(paths[0]) > 1
                or (group[0].nested is not None and bool(group[0].nested))
            )
        ) or (
            len(group) > 1
            and all(len(path) > 1 for path in paths)
            and _paths_unambiguous(paths)
        )
        if not compatible:
            result.extend(sorted(group, key=lambda item: item.path or ()))
            continue
        nested: list[Binding] = []
        for binding in group:
            if not (binding.path is not None):
                raise AssertionError
            if len(binding.path) > 1:
                nested.append(
                    Binding(
                        binding.path[1:],
                        binding.value,
                        binding.raw,
                        binding.nested,
                    ),
                )
            elif binding.nested is not None:
                nested.extend(binding.nested)
        normalized = _normalize(nested)
        if len(normalized) == 1 and normalized[0].path:
            child = normalized[0]
            child_path = cast("tuple[str, ...]", child.path)
            result.append(
                Binding((root, *child_path), child.value, child.raw, child.nested),
            )
        else:
            result.append(Binding((root,), _render_set(normalized), ""))
    return result


def _render_binding(binding: Binding) -> str:
    if binding.path is None or binding.value is None:
        return binding.raw.strip()
    keys = ".".join(_render_key(key) for key in binding.path)
    return f"{keys} = {binding.value.strip()};"


def _render_key(key: str) -> str:
    if (
        key
        and key.isascii()
        and key
        not in {"assert", "else", "if", "in", "inherit", "let", "rec", "then", "with"}
        and (key[0].isalpha() or key[0] == "_")
        and all(character.isalnum() or character in "_-'" for character in key)
    ):
        return key
    return cast("str", nix_syntax.quote_string(key))


def _render_set(bindings: list[Binding]) -> str:
    return "{ " + " ".join(_render_binding(binding) for binding in bindings) + " }"


def render_node(document: nix_syntax.Document, node: Node) -> str:
    """Render a node after recursively applying canonical ordering."""
    if node.type == "binding_set":
        bindings = [_binding(document, child) for child in node.named_children]
        return " ".join(_render_binding(binding) for binding in _normalize(bindings))
    if node.type == "formals":
        formals = [child for child in node.named_children if child.type == "formal"]
        rendered = sorted(
            (_replace_children(document, child) for child in formals),
            key=lambda text: text.split("?", 1)[0].strip(),
        )
        if any(child.type == "ellipses" for child in node.named_children):
            rendered.append("...")
        return "{ " + ", ".join(rendered) + " }"
    if node.type == "list_expression":
        elements = [render_node(document, child) for child in node.named_children]
        contains_string = any("string" in child.type for child in node.named_children)
        if not contains_string:
            elements.sort()
        return "[ " + " ".join(elements) + " ]"
    return (
        _replace_children(document, node)
        if node.named_child_count
        else document.text(node)
    )


def format_text(source: str, path: str = "<expression>") -> str:
    """Sort a Nix expression and apply deterministic formatting."""
    document = nix_syntax.parse(source, path)
    transformed = render_node(document, document.root)
    nix_syntax.parse(transformed, path)
    return transformed


def format_file(path: Path) -> bool:
    """Format one file and report whether processing succeeded."""
    try:
        source = path.read_text(encoding="utf-8")
        formatted = format_text(source, str(path))
        nix_syntax.write_if_changed(path, formatted)
    except (OSError, ValueError) as error:
        print(f"error: {path}: {error}", file=sys.stderr)  # noqa: T201
        return False
    return True


def main() -> None:
    """Sort and format expressions in selected Nix files in place."""
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog="Example: nix_alphabetize flake.nix packages/*/default.nix",
    )
    parser.add_argument(
        "files",
        nargs="+",
        metavar="FILE",
        help="Nix files to rewrite in place",
    )
    if not all(format_file(Path(argument)) for argument in parser.parse_args().files):
        raise SystemExit(1)


if __name__ == "__main__":
    main()

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
class Binding:
    """Rendered Nix text for an assignment value or a whole binding with no path."""

    path: tuple[str, ...]
    value: str
    nested: tuple[Binding, ...] = ()


def _replace_children(document: nix_syntax.Document, node: Node) -> str:
    parts = []
    start = node.start_byte
    for child in node.named_children:
        parts.append(document.source[start : child.start_byte].decode())
        parts.append(render_node(document, child))
        start = child.end_byte
    parts.append(document.source[start : node.end_byte].decode())
    return "".join(parts)


def _binding(document: nix_syntax.Document, node: Node) -> Binding:
    attrpath = node.child_by_field_name("attrpath")
    expression = node.child_by_field_name("expression")
    path = None if attrpath is None else nix_syntax.static_attrpath(document, attrpath)
    if path is None or expression is None:
        return Binding((), _replace_children(document, node))
    nested = _nested_bindings(document, expression)
    if nested is None:
        return Binding(path, render_node(document, expression))
    return Binding(path, _render_set(_normalize(list(nested))), nested)


def _nested_bindings(
    document: nix_syntax.Document,
    expression: Node,
) -> tuple[Binding, ...] | None:
    if expression.type != "attrset_expression":
        return None
    binding_set = nix_syntax.binding_set(expression)
    if binding_set is None:
        return ()
    for child in binding_set.named_children:
        if child.type == "binding":
            attrpath = child.child_by_field_name("attrpath")
            if (
                attrpath is None
                or nix_syntax.static_attrpath(document, attrpath) is None
            ):
                return None
        elif (
            child.type != "inherit"
            or (attrs := child.child_by_field_name("attrs")) is None
            or not all(attr.type == "identifier" for attr in attrs.named_children)
            or any(node.type == "comment" for node in nix_syntax.walk(child))
        ):
            return None
    expanded: list[Binding] = []
    for child in binding_set.named_children:
        attrs = child.child_by_field_name("attrs")
        if child.type == "inherit" and attrs is not None:
            expanded.extend(
                Binding((document.text(attr),), document.text(attr))
                for attr in attrs.named_children
            )
        else:
            expanded.append(_binding(document, child))
    return tuple(expanded)


def _paths_unambiguous(paths: list[tuple[str, ...]]) -> bool:
    ordered = sorted(paths)
    return all(right[: len(left)] != left for left, right in pairwise(ordered))


def _normalize(bindings: list[Binding]) -> list[Binding]:
    groups: dict[str, list[Binding]] = {}
    result = []
    for binding in bindings:
        if binding.path:
            groups.setdefault(binding.path[0], []).append(binding)
        else:
            result.append(binding)
    for root in sorted(groups):
        group = groups[root]
        paths = [binding.path for binding in group]
        if len(group) == 1:
            compatible = len(paths[0]) > 1 or bool(group[0].nested)
        else:
            compatible = all(len(path) > 1 for path in paths) and _paths_unambiguous(
                paths
            )
        if not compatible:
            result.extend(sorted(group, key=lambda item: item.path))
            continue
        nested: list[Binding] = []
        for binding in group:
            if len(binding.path) > 1:
                nested.append(Binding(binding.path[1:], binding.value, binding.nested))
            else:
                nested.extend(binding.nested)
        normalized = _normalize(nested)
        if len(normalized) == 1 and normalized[0].path:
            child = normalized[0]
            result.append(Binding((root, *child.path), child.value, child.nested))
        else:
            result.append(Binding((root,), _render_set(normalized)))
    return result


def _render_binding(binding: Binding) -> str:
    if not binding.path:
        return binding.value.strip()
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
    inherited = []
    assignments = []
    for binding in bindings:
        if (
            len(binding.path) == 1
            and binding.value == binding.path[0]
            and _render_key(binding.path[0]) == binding.path[0]
        ):
            inherited.append(binding.path[0])
        else:
            assignments.append(_render_binding(binding))
    if inherited:
        assignments.insert(0, "inherit " + " ".join(inherited) + ";")
    return "{ " + " ".join(assignments) + " }"


def render_node(document: nix_syntax.Document, node: Node) -> str:
    """Render a node after recursively applying canonical ordering."""
    if node.type in {"binding_set", "formals"} and any(
        child.type == "comment" for child in nix_syntax.walk(node)
    ):
        return _replace_children(document, node)
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
    return (
        _replace_children(document, node)
        if node.named_child_count
        else document.text(node)
    )


def format_text(source: str, path: str = "<expression>") -> str:
    """Sort a Nix expression and apply deterministic formatting."""
    document = nix_syntax.parse(source, path)
    transformed = render_node(document, document.tree.root_node)
    nix_syntax.parse(transformed, path)
    return transformed


def format_file(path: Path) -> bool:
    """Format one file and report whether processing succeeded."""
    try:
        source = path.read_text(encoding="utf-8")
        formatted = format_text(source, str(path))
        nix_syntax.write_if_changed(path, formatted)
    except (OSError, ValueError) as error:
        sys.stderr.write((f"error: {path}: {error}") + "\n")
        return False
    return True


def main() -> None:
    """Sort and format expressions in selected Nix files in place."""
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog="Example: alphabetize_nix flake.nix */default.nix",
    )
    parser.add_argument(
        "files",
        nargs="+",
        metavar="FILE",
        help="Nix files to rewrite in place",
    )
    succeeded = True
    for argument in parser.parse_args().files:
        succeeded &= format_file(Path(argument))
    if not succeeded:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

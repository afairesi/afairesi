# Copyright (c) 2026- Paschalis Bizopoulos
"""Read declared behavioral names from Bats, Nix and Lean test suites."""

# ruff: noqa: INP001 - mkPythonPackage installs this file inside the package.

import json
import re
import subprocess
from typing import TYPE_CHECKING

from tree_sitter_language_pack import get_parser

import nix_syntax

if TYPE_CHECKING:
    from pathlib import Path

    from tree_sitter import Node


def bats_names(path: Path) -> set[str]:
    """Read top-level Bats declarations without sourcing the shell program."""
    source = path.read_bytes()
    parsed = subprocess.run(
        ["shfmt", "-ln", "bats", "--to-json"],  # noqa: S607
        input=source,
        capture_output=True,
        check=False,
    )
    if parsed.returncode:
        message = f"{path}: {parsed.stderr.decode().strip()}"
        raise ValueError(message)
    names = set()
    lines = source.splitlines()
    for statement in json.loads(parsed.stdout).get("Stmts", []):
        command = statement.get("Cmd", {})
        if command.get("Type") == "TestDecl":
            title = command["Description"]
            text = source[title["Pos"]["Offset"] : title["End"]["Offset"]].decode()
            if text.startswith(("'", '"')) and text.endswith(text[0]):
                text = text[1:-1]
            names.add(text)
        elif command.get("Type") == "FuncDecl":
            line = lines[command["Pos"]["Line"] - 1]
            if re.search(rb"\{[ \t]+#[ \t]*@test[ \t]*$", line):
                names.add(command["Name"]["Value"])
    return names


def nix_names(path: Path) -> set[str]:
    """Read named cases from the returned Nix suite and its optional selection."""
    document = nix_syntax.parse(path.read_bytes(), str(path))
    suite = nix_syntax.call_package_body(document)
    if suite is None or suite.type not in {
        "attrset_expression",
        "rec_attrset_expression",
    }:
        return set()
    bindings = nix_syntax.binding_set(suite)
    names = set()
    selected = set()
    for binding in bindings.named_children if bindings else []:
        attrpath = binding.child_by_field_name("attrpath")
        if attrpath is None:
            continue
        name = next(iter(nix_syntax.attrpath_parts(document, attrpath)), None)
        if name == "tests":
            value = nix_syntax.resolve_expression(
                document, binding.child_by_field_name("expression")
            )
            if value is None or value.type != "list_expression":
                return set()
            for item in value.children_by_field_name("element"):
                literal = nix_syntax.string_value(document, item)
                if literal is None:
                    return set()
                selected.add(literal)
        elif name is not None:
            names.add(name)
    return (
        names & selected
        if selected
        else {name for name in names if name.startswith("test")}
    )


def lean_names(path: Path) -> set[str]:
    """Read theorem declarations under their Lean namespaces in the test module."""
    source = path.read_bytes()

    def parser_name(match: re.Match[str]) -> str:
        keyword, name = match.groups()
        return keyword + "".join(
            char if char.isascii() else "x" * len(char.encode()) for char in name
        )

    parser_source = re.sub(
        r"(\b(?:(?:theorem|namespace)\s+|(?:section|end)[ \t]+))([\w.']+)",
        parser_name,
        source.decode(),
    )
    tree = get_parser("lean").parse(parser_source.encode())
    names = set()

    def visit(node: Node, prefix: str) -> None:
        name = node.child_by_field_name("name")
        if node.type == "theorem" and name is not None:
            identifier = source[name.start_byte : name.end_byte].decode()
            names.add(
                identifier.removeprefix("_root_.")
                if identifier.startswith("_root_.")
                else prefix + identifier
            )
        elif node.type in {"module", "namespace", "section", "declaration"}:
            if node.type == "namespace" and name is not None:
                prefix += source[name.start_byte : name.end_byte].decode() + "."
            for child in node.named_children:
                visit(child, prefix)

    visit(tree.root_node, "")
    return names

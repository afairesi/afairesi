# Copyright (c) 2026- Paschalis Bizopoulos
"""Exercise repository rewriting against a locally evaluated fixture flake."""

from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def test_cli_removes_defaults_and_preserves_overrides(tmp_path: Path) -> None:
    """Resolve option defaults with Nix, rewrite their source, and converge."""
    root = tmp_path / "repository-${unbound}"
    root.mkdir()
    dependency = root / "prm/nixpkgs"
    dependency.mkdir(parents=True)
    (dependency / "flake.nix").write_text(
        """{
      outputs = _: { lib.attrByPath = path: fallback: attrs:
        let follow = path: value:
          if path == [] then value else
          if builtins.hasAttr (builtins.head path) value
          then follow (builtins.tail path) value.${builtins.head path}
          else fallback;
        in follow path attrs;
      };
    }""",
        encoding="utf-8",
    )
    (root / "flake.nix").write_text(
        """{
      inputs.nixpkgs.url = "path:./prm/nixpkgs";
      outputs = { self, nixpkgs }: {
        nixosConfigurations.demo.options = {
          services.demo.enable = {
            default = false;
            definitionsWithLocations = [{ file = "${self}/configuration.nix"; }];
          };
          services.demo.text = {
            default = "literal \\${name} \\q";
            definitionsWithLocations = [{ file = "${self}/configuration.nix"; }];
          };
          services.demo.multiline = {
            default = "one\\ntwo\\n";
            definitionsWithLocations = [{ file = "${self}/configuration.nix"; }];
          };
          services.demo.compound = {
            default = { values = [ null true 7 2.5 ]; };
            definitionsWithLocations = [{ file = "${self}/configuration.nix"; }];
          };
          services.other.enable = {
            default = false;
            definitionsWithLocations = [{ file = "${self}/configuration.nix"; }];
          };
        };
      };
    }""",
        encoding="utf-8",
    )
    configuration = root / "configuration.nix"
    configuration.write_text(
        "{ services.demo.enable = false; services.other.enable = true; "
        'services.demo.text = "literal \\${name} \\q"; '
        "services.demo.multiline = ''\n  one\n  two\n''; "
        "services.demo.compound = { values = [ null true 7 2.5 ]; }; "
        'description = "keep  spacing"; empty = {}; }',
        encoding="utf-8",
    )
    scratch = root / "tmp/invalid.nix"
    scratch.parent.mkdir()
    scratch.write_text("{ broken = ; }", encoding="utf-8")
    environment = dict(os.environ)
    environment["NIX_REMOTE"] = f"local?root={tmp_path / 'store'}"
    environment["NIX_CONFIG"] = (
        "experimental-features = nix-command flakes\nbuild-users-group =\n"
    )
    command = [os.environ["PACKAGE_E2E_EXECUTABLE"], str(root)]
    result = subprocess.run(  # noqa: S603
        command,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    output = configuration.read_text()
    if (
        "services.demo" in output
        or "services.other.enable = true;" not in output
        or 'description = "keep  spacing";' not in output
        or "empty = {};" not in output
    ):
        raise AssertionError(output)
    result = subprocess.run(  # noqa: S603
        command,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if (
        result.returncode
        or configuration.read_text() != output
        or scratch.read_text() != "{ broken = ; }"
    ):
        raise AssertionError(result.stderr)

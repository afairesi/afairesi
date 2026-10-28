# Copyright (c) 2026- Paschalis Bizopoulos
"""Exercise repository rewriting against a locally evaluated fixture flake."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from remove_nix_defaults import main as remove_nix_defaults


def test_cli_removes_pinned_package_defaults(tmp_path: Path) -> None:
    """Respect pinned defaults, overrides, recursive references, and call scopes."""
    root = tmp_path / "repository"
    dependency = root / "prm/afairesi"
    dependency.mkdir(parents=True)
    (dependency / "flake.nix").write_text(
        """{ outputs = _: { lib.mkPythonPackage = arguments@{
          pkgs, src, executable, ... }:
          pkgs.python3.pkgs.buildPythonPackage (
            { version = "7.2.0"; propagatedBuildInputs = [];
              customFlag = executable; retryCount = arguments.specialOption or 3;
              settings.mode = "automatic";
              config.mode = "automatic";
              doCheck = true; strictDeps = true;
            } // arguments
          ); }; }""",
        encoding="utf-8",
    )
    (root / "flake.nix").write_text(
        """{ inputs.afairesi.url = "path:./prm/afairesi";
          outputs = { afairesi, ... }: { inherit (afairesi) lib; }; }""",
        encoding="utf-8",
    )
    matching = root / "default.nix"
    matching.write_text(
        """{ inputs, pkgs, ... }: let dependency = pkgs.buildPythonPackage {
          version = "7.2.0"; propagatedBuildInputs = [];
        }; in (inputs.afairesi or inputs.self).lib.mkPythonPackage {
          inherit pkgs; executable = true; src = ./.;
          version = "7.2.0"; propagatedBuildInputs = [];
          customFlag = true; retryCount = 3; settings.mode = "automatic";
          config.mode = "automatic";
          doCheck = true; strictDeps = true;
          meta.description = "keep metadata";
          passthru = { version = "7.2.0"; propagatedBuildInputs = []; };
        }""",
        encoding="utf-8",
    )
    overrides = root / "overrides.nix"
    overrides.write_text(
        """{ inputs, pkgs, ... }: (inputs.afairesi or inputs.self).lib.mkPythonPackage {
          inherit pkgs; executable = true; src = ./.;
          version = "0.0.0"; propagatedBuildInputs = [ "dependency" ];
          customFlag = false; retryCount = 4; settings.mode = "manual";
          config.mode = "manual";
          doCheck = 1; strictDeps = false;
        }""",
        encoding="utf-8",
    )
    recursive = root / "recursive.nix"
    recursive.write_text(
        """{ inputs, pkgs, ... }:
        (inputs.afairesi or inputs.self).lib.mkPythonPackage rec {
          inherit pkgs; executable = true; src = ./.;
          version = "7.2.0"; passthru.version = version;
        }""",
        encoding="utf-8",
    )
    unrelated = root / "unrelated.nix"
    unrelated.write_text(
        '{ pkgs, ... }: pkgs.buildPythonPackage { version = "7.2.0"; }',
        encoding="utf-8",
    )
    self_package = root / "self.nix"
    self_package.write_text(
        """{ inputs, pkgs, ... }: (inputs.afairesi or inputs.self).lib.mkPythonPackage {
          inherit pkgs; src = ./.; executable = false; version = "7.2.0";
          propagatedBuildInputs = [];
          customFlag = false;
        }""",
        encoding="utf-8",
    )
    preserved = {path: path.read_text() for path in (overrides, recursive, unrelated)}
    for name, constructor in (
        ("external", "inputs.afairesi.lib.mkPythonPackage"),
        ("self", "inputs.self.lib.mkPythonPackage"),
    ):
        path = root / f"noncanonical-{name}.nix"
        path.write_text(
            f"{{ inputs, pkgs, ... }}: {constructor} {{\n"
            '  inherit pkgs; src = ./.; executable = false; version = "7.2.0";\n'
            "}\n",
            encoding="utf-8",
        )
        preserved[path] = path.read_text()
    contextual = root / "contextual.nix"
    contextual.write_text(
        """{ inputs, pkgs, ... }: (inputs.afairesi or inputs.self).lib.mkPythonPackage {
          inherit pkgs; src = ./.; executable = true;
          specialOption = 9; retryCount = 9;
        }""",
        encoding="utf-8",
    )
    environment = dict(os.environ)
    environment["NIX_REMOTE"] = f"local?root={tmp_path / 'store'}"
    environment["NIX_CONFIG"] = (
        "experimental-features = nix-command flakes\nbuild-users-group =\n"
    )
    command = [os.environ["PACKAGE_E2E_EXECUTABLE"], str(root)]
    previous = None
    retained_scopes = 2
    for _ in range(2):
        result = subprocess.run(  # noqa: S603 - Test harness executable with a fixture repository path.
            command,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        if result.returncode:
            raise AssertionError(result.stderr)
        output = matching.read_text()
        if (
            output.count('version = "7.2.0";') != retained_scopes
            or output.count("propagatedBuildInputs = [];") != retained_scopes
            or 'meta.description = "keep metadata";' not in output
            or "version" in self_package.read_text()
            or "propagatedBuildInputs" in self_package.read_text()
            or "customFlag" in output
            or "retryCount" in output
            or "settings" in output
            or "config" in output
            or "doCheck" in output
            or "strictDeps" in output
            or "customFlag" in self_package.read_text()
            or "executable = false;" not in self_package.read_text()
            or "retryCount" in contextual.read_text()
            or "specialOption = 9;" not in contextual.read_text()
            or any(path.read_text() != content for path, content in preserved.items())
        ):
            raise AssertionError(output)
        if previous is not None and previous != output:
            raise AssertionError(output)
        previous = output


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
    (dependency / "default.nix").write_text(
        "_: (import ./flake.nix).outputs {}",
        encoding="utf-8",
    )
    treefmt = root / "prm/treefmt"
    treefmt.mkdir()
    (treefmt / "flake.nix").write_text(
        """{ outputs = _: { lib.evalModule = _: _: {
          options.formatter.foo.default = false;
          options.formatter.bar.default = false;
        }; }; }""",
        encoding="utf-8",
    )
    (root / "flake.nix").write_text(
        """{
      inputs.nixpkgs.url = "path:./prm/nixpkgs";
      inputs.treefmt-nix.url = "path:./prm/treefmt";
      outputs = { self, nixpkgs, ... }: {
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
    formatter = root / "formatter.nix"
    formatter.write_text(
        """{ inputs, pkgs, ... }: let sibling = { formatter.foo = false; };
          in inputs.treefmt-nix.lib.evalModule pkgs {
            formatter.foo = false; formatter.bar = true;
          }""",
        encoding="utf-8",
    )
    environment = dict(os.environ)
    environment["NIX_REMOTE"] = f"local?root={tmp_path / 'store'}"
    environment["NIX_CONFIG"] = (
        "experimental-features = nix-command flakes\nbuild-users-group =\n"
    )
    command = [os.environ["PACKAGE_E2E_EXECUTABLE"], str(root)]
    result = subprocess.run(  # noqa: S603 - Test harness executable with a fixture repository path.
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
        or formatter.read_text().count("formatter.foo = false;") != 1
        or "formatter.bar = true;" not in formatter.read_text()
    ):
        raise AssertionError(output)
    result = subprocess.run(  # noqa: S603 - Test harness executable with a fixture repository path.
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


def _nix_environment(tmp_path: Path) -> dict[str, str]:
    environment = dict(os.environ)
    environment["NIX_REMOTE"] = f"local?root={tmp_path / 'store'}"
    environment["NIX_CONFIG"] = (
        "experimental-features = nix-command flakes\nbuild-users-group =\n"
    )
    return environment


@pytest.mark.parametrize(
    "source",
    ["{ broken = ; }", "{ flag = false; flag = true; }", "{ flag = missing; }"],
)
def test_cli_preserves_native_parse_errors(tmp_path: Path, source: str) -> None:
    """Nix validates inputs before any defaults can hide a native error."""
    root = tmp_path / "repository"
    root.mkdir()
    (root / "flake.nix").write_text("{ outputs = _: {}; }")
    path = root / "configuration.nix"
    path.write_text(source)
    environment = _nix_environment(tmp_path)
    native = subprocess.run(  # noqa: S603 - Fixed Nix executable with fixture paths or expressions.
        ["nix-instantiate", "--parse", "--", str(path)],  # noqa: S607
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    actual = subprocess.run(  # noqa: S603 - Test harness selects the executable and fixture arguments.
        [os.environ["PACKAGE_E2E_EXECUTABLE"], str(root)],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if (
        not native.returncode
        or actual.returncode != native.returncode
        or actual.stderr != native.stderr
        or path.read_text() != source
    ):
        raise AssertionError((native, actual))


def test_process_repository_parses_option_like_relative_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Treat a leading dash in a repository path as a filename, not an option."""
    monkeypatch.chdir(tmp_path)
    for name, value in _nix_environment(tmp_path).items():
        if name.startswith("NIX_"):
            monkeypatch.setenv(name, value)
    root = Path("-repository")
    root.mkdir()
    (root / "configuration.nix").write_text("{ broken = ; }")
    with pytest.raises(subprocess.CalledProcessError) as error:
        remove_nix_defaults.process_repository(root)
    if "syntax error" not in error.value.stderr:
        raise AssertionError(error.value.stderr)


def _treefmt_fixture(root: Path) -> None:
    nixpkgs = root / "prm/nixpkgs"
    nixpkgs.mkdir(parents=True)
    (nixpkgs / "flake.nix").write_text(
        """{ outputs = _: { lib.attrByPath = path: fallback: attrs:
          if path == [] then attrs else
          let key = builtins.head path; rest = builtins.tail path;
          in if builtins.hasAttr key attrs
          then ((import ./flake.nix).outputs {}).lib.attrByPath
            rest fallback attrs.${key}
          else fallback;
        }; }""",
    )
    (nixpkgs / "default.nix").write_text("_: (import ./flake.nix).outputs {}")
    treefmt = root / "prm/treefmt"
    treefmt.mkdir()
    (treefmt / "flake.nix").write_text(
        """{ outputs = _: { lib.evalModule = _: module: {
          options.flag.default = false;
          options.count.default = 0;
          options.nested.flag.default = false;
          config = { flag = false; count = 0; } // module // {
            nested = { flag = false; } // (module.nested or {});
          };
        }; }; }""",
    )
    (root / "flake.nix").write_text(
        """{ inputs.nixpkgs.url = "path:./prm/nixpkgs";
          inputs.treefmt-nix.url = "path:./prm/treefmt";
          outputs = _: {}; }""",
    )


def test_cli_preserves_nondefaults_and_shared_host_overrides(tmp_path: Path) -> None:
    """Remove a file's value only when every consuming host agrees on its default."""
    root = tmp_path / "repository"
    _treefmt_fixture(root)
    flake = root / "flake.nix"
    flake.write_text(
        flake.read_text().replace(
            "outputs = _: {};",
            """outputs = { self, nixpkgs, ... }: {
              nixosConfigurations = {
                enabled.options.flag = {
                  default = false;
                  definitionsWithLocations = [{ file = "${self}/enabled.nix"; }];
                };
                disabled.options.flag = {
                  default = false;
                  definitionsWithLocations = [{ file = ./disabled.nix; }];
                };
                first.options.flag = {
                  default = false;
                  definitionsWithLocations = [{ file = "${self}/shared.nix"; }];
                };
                second.options.flag = {
                  default = true;
                  definitionsWithLocations = [{ file = "${self}/shared.nix"; }];
                };
                external.options.flag = {
                  default = false;
                  definitionsWithLocations = [{ file = "${nixpkgs}/unused.nix"; }];
                };
              };
            };""",
        ),
    )
    for name, value in (
        ("enabled", "true"),
        ("disabled", "false"),
        ("shared", "false"),
        ("unused", "false"),
    ):
        (root / f"{name}.nix").write_text(f"{{ flag = {value}; }}")
    for _ in range(2):
        subprocess.run(  # noqa: S603 - Test harness selects the executable and fixture arguments.
            [os.environ["PACKAGE_E2E_EXECUTABLE"], str(root)],
            env=_nix_environment(tmp_path),
            capture_output=True,
            check=True,
            timeout=30,
        )
        if (root / "enabled.nix").read_text() != "{ flag = true; }":
            msg = "non-default value removed"
            raise AssertionError(msg)
        if (root / "shared.nix").read_text() != "{ flag = false; }":
            msg = "override needed by another host removed"
            raise AssertionError(msg)
        if (root / "unused.nix").read_text() != "{ flag = false; }":
            msg = "another input's source location matched a local file"
            raise AssertionError(msg)
        if "flag" in (root / "disabled.nix").read_text():
            msg = "matching default retained"
            raise AssertionError(msg)


def test_cli_preserves_values_merged_from_multiple_modules(tmp_path: Path) -> None:
    """An explicit list matching the default still contributes to a merged option."""
    root = tmp_path / "repository"
    _treefmt_fixture(root)
    flake = root / "flake.nix"
    flake.write_text(
        flake.read_text().replace(
            "outputs = _: {};",
            """outputs = { self, ... }: {
              nixosConfigurations.demo.options.values = {
                default = [1];
                definitionsWithLocations = [
                  { file = "${self}/first.nix"; }
                  { file = "${self}/second.nix"; }
                ];
              };
            };""",
        ),
    )
    (root / "first.nix").write_text("{ values = [1]; }")
    (root / "second.nix").write_text("{ values = [2]; }")
    environment = _nix_environment(tmp_path)
    subprocess.run(  # noqa: S603 - Test harness selects the executable and fixture arguments.
        [os.environ["PACKAGE_E2E_EXECUTABLE"], str(root)],
        env=environment,
        capture_output=True,
        check=True,
        timeout=30,
    )
    expression = (
        f"((import {root / 'first.nix'}).values or []) ++ "
        f"((import {root / 'second.nix'}).values or [])"
    )
    result = subprocess.run(  # noqa: S603 - Fixed Nix executable with test-authored expressions.
        ["nix", "eval", "--impure", "--json", "--expr", expression],  # noqa: S607
        env=environment,
        capture_output=True,
        check=True,
        timeout=30,
    )
    if json.loads(result.stdout) != [1, 2]:
        raise AssertionError(result.stdout)


@pytest.mark.parametrize(
    ("assignments", "retained"),
    [
        ("flag = false; count = 0;", []),
        ("flag = 0; count = false;", ["flag = 0;", "count = false;"]),
        ("nested = { flag = false; };", []),
        (
            "nested = { flag = false; # keep nested comment\n };",
            ["# keep nested comment"],
        ),
        (
            "nested = rec { flag = false; copy = flag; };",
            ["flag = false;", "copy = flag;"],
        ),
    ],
)
def test_cli_preserves_native_treefmt_values(
    tmp_path: Path,
    assignments: str,
    retained: list[str],
) -> None:
    """Only matching literal types are removed, with comments and scope intact."""
    root = tmp_path / "repository"
    _treefmt_fixture(root)
    path = root / "formatter.nix"
    path.write_text(
        "{ inputs, pkgs, ... }: inputs.treefmt-nix.lib.evalModule pkgs {\n"
        "# keep leading comment\n"
        f"{assignments}\n"
        "untouched /* keep binding comment */ = {};\n"
        "# keep trailing comment\n}",
    )
    environment = _nix_environment(tmp_path)
    expression = (
        f"let root = {json.dumps(str(root))}; flake = builtins.getFlake root; "
        'in ((import (root + "/formatter.nix")) '
        "{ inputs = flake.inputs; pkgs = {}; }).config"
    )
    command = ["nix", "eval", "--impure", "--json", "--expr", expression]
    expected = subprocess.run(  # noqa: S603 - Fixed Nix executable with test-authored expressions.
        command,
        env=environment,
        capture_output=True,
        check=True,
        timeout=30,
    ).stdout
    for _ in range(2):
        subprocess.run(  # noqa: S603 - Test harness selects the executable and fixture arguments.
            [os.environ["PACKAGE_E2E_EXECUTABLE"], str(root)],
            env=environment,
            capture_output=True,
            check=True,
            timeout=30,
        )
        actual = subprocess.run(  # noqa: S603 - Fixed Nix executable with test-authored expressions.
            command,
            env=environment,
            capture_output=True,
            check=True,
            timeout=30,
        ).stdout
        source = path.read_text()
        if actual != expected or any(text not in source for text in retained):
            raise AssertionError((expected, actual, source))
        if not retained and any(key in source for key in ("flag =", "count =")):
            raise AssertionError(source)
        for comment in (
            "# keep leading comment",
            "# keep trailing comment",
            "/* keep binding comment */",
        ):
            if comment not in source:
                raise AssertionError(source)


@pytest.mark.parametrize(
    "value",
    ['throw "native evaluation failure"', "assert false; {}"],
)
def test_cli_preserves_native_evaluation_errors(tmp_path: Path, value: str) -> None:
    """Evaluator failures retain their native status and final diagnostic."""
    root = tmp_path / "repository"
    root.mkdir()
    (root / "flake.nix").write_text(
        f"{{ outputs = _: {{ nixosConfigurations = {value}; }}; }}",
    )
    path = root / "configuration.nix"
    source = "{ flag = false; }"
    path.write_text(source)
    environment = _nix_environment(tmp_path)
    native = subprocess.run(  # noqa: S603 - Fixed Nix executable with test-authored expressions.
        [  # noqa: S607
            "nix",
            "eval",
            "--impure",
            "--json",
            "--expr",
            f"(builtins.getFlake {json.dumps(str(root))}).nixosConfigurations",
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    actual = subprocess.run(  # noqa: S603 - Test harness selects the executable and fixture arguments.
        [os.environ["PACKAGE_E2E_EXECUTABLE"], str(root)],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if (
        not native.returncode
        or actual.returncode != native.returncode
        or actual.stderr.rsplit("error: ", 1)[-1]
        != native.stderr.rsplit("error: ", 1)[-1]
        or "nix evaluation failed:" in actual.stderr
        or path.read_text() != source
    ):
        raise AssertionError((native, actual))

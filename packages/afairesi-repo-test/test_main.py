# Copyright (c) 2026- Paschalis Bizopoulos
"""Check isolated test campaigns and their command-line contracts."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from contextlib import contextmanager
from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

import coverage
import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import Any
import afairesi


def _run(
    root: Path,
    *arguments: str,
    code: int = 0,
    executable: str | None = None,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(  # noqa: S603
        [executable or os.environ["PACKAGE_E2E_EXECUTABLE"], *arguments],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode != code:
        raise AssertionError(result.stdout + result.stderr)
    return result


def _repair_flake(
    root: Path,
    *arguments: str,
    code: int = 0,
) -> subprocess.CompletedProcess[str]:
    """Exercise the private flake repair entry point used by the formatter."""
    return _run(
        root,
        "-c",
        "from afairesi import formatter_main; formatter_main()",
        *arguments,
        code=code,
        executable=sys.executable,
    )


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(  # noqa: S603
        ["git", *arguments],  # noqa: S607
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    ).stdout


def _make_source_package(root: Path, name: str, source: str) -> Path:
    """Create a canonical package fixture without executing its source."""
    package = root / "packages" / name
    package.mkdir(parents=True)
    (root / "flake.nix").touch()
    (package / "default.nix").touch()
    (package / "main.py").touch()
    (package / "test_main.py").write_text(source)
    return package


def _make_runner_target(
    root: Path,
    source: str,
    tests: str,
    *,
    name: str = "example",
) -> Path:
    """Create a minimal canonical package for an isolated test."""
    package = root / "packages" / name
    package.mkdir(parents=True)
    (root / "flake.nix").write_text("{}", encoding="utf-8")
    (package / "default.nix").write_text("{}", encoding="utf-8")
    (package / "main.py").write_text(source, encoding="utf-8")
    (package / "test_main.py").write_text(tests, encoding="utf-8")
    return package


def _prepare_runner_flake(
    root: Path,
    tests: str,
    source: str = "def main():\n    print('ready')\n",
    *,
    name: str = "example",
) -> dict[str, str]:
    """Provide an offline flake backed by this check's real Python environment."""
    package = _make_runner_target(root, source, tests, name=name)
    (package / "main.py").write_text(source, encoding="utf-8")
    builders = root / "prm/builders"
    shutil.copytree(Path(afairesi.__file__).with_name("prm"), builders)
    shutil.copyfile(afairesi.__file__, builders.parent / "main.py")
    environment = dict(os.environ)
    store = root.parent / "nix"
    environment["NIX_REMOTE"] = (
        f"local?store={store / 'store'}&state={store / 'state'}&log={store / 'log'}"
    )
    environment["NIX_CONFIG"] = (
        "experimental-features = nix-command flakes\nbuild-users-group =\n"
    )
    system = subprocess.run(
        ["nix", "eval", "--impure", "--raw", "--expr", "builtins.currentSystem"],  # noqa: S607
        env=environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout
    (root / "flake.nix").write_text(
        "{ outputs = _: let lib = import ./prm/builders/builders.nix; "
        "pkgs = { lib = { concatMap = f: xs: builtins.concatLists (map f xs); "
        'makeBinPath = _: ""; }; writeText = builtins.toFile; }; '
        f"packageDrv.python.withPackages = _ : {json.dumps(sys.prefix)}; "
        "in { "
        f"packages.{json.dumps(system)} = builtins.listToAttrs (map (name: {{ "
        'name = "${name}-test-environment"; '
        "value = (lib.mkTestEnvironment { inherit pkgs packageDrv; }).manifest; "
        "}) (builtins.attrNames (builtins.readDir ./packages))); }; }",
        encoding="utf-8",
    )
    for args in (["init", "--quiet"], ["add", "."]):
        subprocess.run(  # noqa: S603
            ["git", "-C", str(root), *args],  # noqa: S607
            check=True,
            capture_output=True,
            timeout=10,
        )
    return environment


def _run_runner_cli(
    root: Path,
    environment: dict[str, str],
    command: str,
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    """Exercise test runners through the public standalone command."""
    environment = dict(environment)
    executable_directory = os.path.dirname(os.environ["PACKAGE_E2E_EXECUTABLE"])  # noqa: PTH120
    environment["PATH"] = executable_directory + os.pathsep + environment["PATH"]
    return subprocess.run(  # noqa: S603
        ["afairesi-repo-test", command, str(root), *arguments],  # noqa: S607
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def _prepare_coverage_flake(
    root: Path,
    *,
    fail_build: bool = False,
) -> dict[str, str]:
    """Build generated checks and coverage variants with offline Nix inputs."""
    root.mkdir(parents=True)
    _git(root, "init", "--quiet")
    for filename in (".gitignore", "flake.nix", "flake.lock", "LICENSE"):
        (root / filename).write_text(
            "{}" if filename.endswith((".nix", ".lock")) else "",
        )
    (root / "flake.lock").write_text(
        json.dumps({"nodes": {"root": {}}, "root": "root", "version": 7}),
    )
    _git(root, "add", ".")
    environment = dict(os.environ)
    store = root.parent / "nix"
    environment["NIX_REMOTE"] = (
        f"local?store={store / 'store'}&state={store / 'state'}&log={store / 'log'}"
    )
    environment["NIX_CONFIG"] = (
        "experimental-features = nix-command flakes\n"
        "build-users-group =\n"
        "sandbox = false\n"
        "sandbox-build-dir = /coverage-build\n"
        "eval-cache = false\n"
    )
    system = subprocess.run(
        ["nix", "eval", "--impure", "--raw", "--expr", "builtins.currentSystem"],  # noqa: S607
        env=environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout
    site_packages = (
        f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
    )
    coverage_root = Path(coverage.__file__).resolve().parents[4]
    bash = shutil.which("bash")
    if bash is None:
        message = "coverage fixtures require bash"
        raise AssertionError(message)
    packages = []
    checks = []
    reports = []
    shutil.copytree(Path(afairesi.__file__).with_name("prm"), root / "prm/builders")
    shutil.copyfile(afairesi.__file__, root / "prm/main.py")
    for name in ("example", "z-last"):
        _manage(root, "add", f"packages/{name}", "python")
        package = root / "packages" / name
        source = (
            "import os\n"
            "def main():\n"
            "    if os.getenv('AFAIRESI_COVERAGE_CHOICE') == 'alternate':\n"
            "        print('alternate')\n"
            "    else:\n"
            "        print('ready')\n"
        )
        tests = (
            "import os, subprocess, pytest\n"
            "from hypothesis import given, example, strategies as st\n"
            "@given(st.just(1))\n"
            "@example(0)\n"
            "def test_explicit(value):\n"
            "    assert value == 0\n"
            "@given(st.just(1))\n"
            "def test_generated_only(value):\n"
            "    assert value == 1\n"
            "@pytest.mark.skip(reason='optional browser')\n"
            "def test_optional_browser():\n"
            "    assert False\n"
            "@pytest.mark.parametrize('message', ['ready', 'alternate'])\n"
            "def test_cli(message, monkeypatch):\n"
            "    monkeypatch.setenv('AFAIRESI_COVERAGE_CHOICE', message)\n"
            "    result = "
            "subprocess.run([os.environ['PACKAGE_E2E_EXECUTABLE']],\n"
            "        capture_output=True, text=True)\n"
            "    assert result.stdout == message + '\\n'\n"
            "    assert result.stderr == ''\n"
        )
        if name == "example" and fail_build:
            tests = "def test_failure(): assert False\n"
        (package / "main.py").write_text(source)
        (package / "test_main.py").write_text(tests)
        installed = root / "prm/installed" / name
        module_name = name.replace("-", "_")
        module = installed / site_packages / module_name
        module.mkdir(parents=True)
        (module / "__init__.py").write_text(source)
        executable = installed / "bin" / name
        executable.parent.mkdir()
        executable.write_text(
            f"#!{sys.executable}\nimport sys\nfrom pathlib import Path\n"
            "sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "
            f"{site_packages!r}))\n"
            f"from {module_name} import main\nmain()\n",
        )
        executable.chmod(0o755)
        packages.append(
            f"{json.dumps(name)} = {{ "
            f"src = builtins.path {{ path = ./packages/{name}; "
            f'name = "{name}-src"; }}; '
            f"outPath = builtins.path {{ path = ./prm/installed/{name}; "
            f'name = "{name}-installed"; }}; '
            f"pname = {json.dumps(module_name)}; cliName = {json.dumps(name)}; "
            f"meta.mainProgram = {json.dumps(name)}; "
            "propagatedBuildInputs = []; python = { "
            f"sitePackages = {json.dumps(site_packages)}; "
            f"pkgs.coverage = {json.dumps(str(coverage_root))}; "
            f"withPackages = _: {json.dumps(sys.prefix)}; }}; }};",
        )
        expression = (
            f"import ./checks/{name}/default.nix {{ inherit pkgs; "
            "inputs.self = { inherit lib; packages.${system} = packages; }; }"
        )
        checks.append(f"{json.dumps(name)} = {expression};")
        reports.append(
            f"{json.dumps(name + '-coverage')} = lib.mkCoverage {{ "
            f"packageName = {json.dumps(name)}; "
            f"packageDrv = packages.{json.dumps(name)}; "
            f"check = checks.{json.dumps(name)}; }};",
        )
    _repair_flake(root)
    (root / "flake.nix").write_text(
        "{ outputs = _: let lib = import ./prm/builders/builders.nix; "
        f"system = {json.dumps(system)}; "
        "mkCheck = name: attrs: script: let build = current: (builtins.derivation { "
        "inherit system; inherit (current) name src PACKAGE_E2E_EXECUTABLE; "
        f"builder = {json.dumps(bash)}; "
        f"PATH = {json.dumps(sys.prefix + '/bin:' + environment['PATH'])}; "
        f"PYTHONPATH = {json.dumps(os.pathsep.join(sys.path))}; "
        'args = [ "-e" (builtins.toFile "check-builder" '
        '("export -n src PACKAGE_E2E_EXECUTABLE\\n" + current.buildCommand)) ]; '
        "}) // { overrideAttrs = f: build (current // f current); }; "
        "in build (attrs // { inherit name; buildCommand = script; }); "
        "pkgs = { stdenv.system = system; runCommand = mkCheck; lib = { "
        "optionalAttrs = condition: attrs: if condition then attrs else {}; "
        "concatMap = f: xs: builtins.concatLists (map f xs); "
        'getExe = p: "${p}/bin/${p.cliName}"; }; }; '
        "packages = { "
        + " ".join(packages)
        + " }; checks = { "
        + " ".join(checks)
        + " }; "
        "in { packages.${system} = packages // { " + " ".join(reports) + " }; "
        "checks.${system} = checks; }; }\n",
    )
    _git(root, "add", ".")
    return environment


def _expect(condition: bool, detail: object) -> None:  # noqa: FBT001
    """Report an invariant failure with its observed state."""
    if not condition:
        raise AssertionError(detail)


def _repository(root: Path, *, object_format: str = "sha1") -> Path:
    """Index a minimal flake in an existing temporary directory."""
    _git(root, "init", "--quiet", f"--object-format={object_format}")
    for name in (".gitignore", "flake.nix", "flake.lock", "LICENSE"):
        (root / name).write_text("", encoding="utf-8")
    _git(root, "add", ".")
    return root


@contextmanager
def _fresh_repository(*, object_format: str = "sha1") -> Iterator[Path]:
    """Give every generated example its own repository and cleanup."""
    with TemporaryDirectory(prefix="afairesi-contract-") as directory:
        yield _repository(Path(directory), object_format=object_format)


def _snapshot(root: Path, *, exclude: tuple[str, ...] = ()) -> tuple[object, ...]:
    """Observe paths, contents, modes, staged blobs, refs, and local Git settings."""
    files = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if relative.parts[0] in (".git", *exclude):
            continue
        mode = path.lstat().st_mode
        value = (
            str(path.readlink()).encode()
            if stat.S_ISLNK(mode)
            else path.read_bytes()
            if stat.S_ISREG(mode)
            else b""
        )
        files[relative.as_posix()] = (stat.S_IFMT(mode), stat.S_IMODE(mode), value)
    return (
        files,
        _git(root, "ls-files", "--stage", "-z"),
        _git(root, "for-each-ref", "--format=%(refname) %(objectname)"),
        _git(root, "config", "--local", "--list"),
    )


def _runner_package(root: Path, name: str, tests: str, source: str = "") -> None:
    """Add another offline runnable package without replacing the fixture flake."""
    flake = (root / "flake.nix").read_text()
    _make_runner_target(root, source, tests, name=name)
    (root / "flake.nix").write_text(flake)
    _git(root, "add", ".")


def _campaign(
    root: Path,
    environment: dict[str, str],
    command: str,
    *arguments: str,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run an omitted target and preserve original sources, settings, and refs."""
    before = _snapshot(root, exclude=("tmp",))
    result = subprocess.run(  # noqa: S603
        [os.environ["PACKAGE_E2E_EXECUTABLE"], command, *arguments],
        cwd=cwd or root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    _expect(
        _snapshot(root, exclude=("tmp",)) == before,
        "campaign changed original sources or Git state",
    )
    return result


def _manage(
    root: Path,
    *arguments: str,
    code: int = 0,
) -> subprocess.CompletedProcess[str]:
    """Set up repository fixtures through the management executable."""
    return _run(root, "repo", *arguments, code=code, executable="afairesi")


def test_campaign_targets_respect_repository_defaults_and_explicit_packages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Route campaigns independently of their expensive test engines."""
    subject = import_module("packages.afairesi-repo-test.main")
    root = _repository(tmp_path)
    package = _make_source_package(root, "alpha", "def test_alpha(): pass\n")
    nested = package / "prm/nested"
    nested.mkdir(parents=True)
    observed: list[Path] = []

    def run(target: Path, *_arguments: object) -> bool:
        observed.append(target)
        return True

    for runner in (
        "_run_coverage",
        "_run_test_package",
        "_run_test_repository",
    ):
        monkeypatch.setattr(subject, runner, run)
    for cwd in (package, nested):
        monkeypatch.chdir(cwd)
        observed.clear()
        monkeypatch.setattr(sys, "argv", ["afairesi-repo-test"])
        with pytest.raises(SystemExit) as combined:
            subject.main()
        _expect(combined.value.code == 0 and observed == [root] * 3, observed)
        for path in (
            ("coverage",),
            ("hypothesis",),
            ("mutation",),
        ):
            targets = (
                ((), root),
                ((".",), root),
                (("packages/alpha",), package),
                ((str(package),), package),
            )
            for arguments, expected in targets:
                monkeypatch.setattr(sys, "argv", ["afairesi", *path, *arguments])
                with pytest.raises(SystemExit) as completed:
                    subject.main()
                _expect(completed.value.code == 0, path)
                _expect(observed[-1] == expected, (path, arguments, observed))


def test_cli_contracts_validate_help_interfaces_budgets_and_targets() -> None:  # noqa: C901, PLR0912
    """Expose consistent commands and reject invalid requests before creating state."""
    with TemporaryDirectory(prefix="afairesi-cli-") as directory:
        root = Path(directory)
        commands = ((), ("coverage",), ("hypothesis",), ("mutation",))
        for path in commands:
            option = _run(root, *path, "--help").stdout
            _expect(
                "usage:" in option,
                path,
            )
        environment = dict(os.environ)
        environment["PATH"] = (
            str(Path(os.environ["PACKAGE_E2E_EXECUTABLE"]).parent)
            + os.pathsep
            + environment["PATH"]
        )
        _expect(
            _run(
                root,
                "--help",
                executable="afairesi-repo-test",
                environment=environment,
            ).stdout
            == _run(root, "--help").stdout,
            "standalone command unavailable through PATH",
        )
        _expect(
            "not inside a Git repository" in _run(root, code=1).stderr,
            "bare test must require a repository",
        )
        for command in ("coverage", "hypothesis", "mutation"):
            _run(root, command, code=1)
        for command in ("hypothesis", "mutation"):
            for timeout in ("nan", "inf", "-inf", "0", "-1"):
                _run(root, command, str(root), f"--timeout={timeout}", code=2)
        _run(root, "hypothesis", str(root), "--max-examples", "0", code=2)
        for arguments in (
            ("--max-mutations", "0"),
            ("--max-mutations", "-1"),
            ("--lines", "0"),
            ("--lines", "3:2"),
            ("--lines", "bad"),
            ("--operator", "["),
        ):
            _run(root, "mutation", str(root), *arguments, code=2)
        _expect(not any(root.iterdir()), "invalid CLI request created state")
    for layout in ("empty", "nonpython", "untested", "single_untested"):
        with _fresh_repository() as root:
            target = root
            if layout == "nonpython":
                _manage(root, "add", "packages/web", "html")
            elif layout in {"untested", "single_untested"}:
                target = _make_runner_target(root, "", "")
                (target / "test_main.py").unlink()
                if layout == "untested":
                    target = root
            before = _snapshot(root)
            for command in ("hypothesis", "mutation"):
                result = _run(
                    root,
                    command,
                    str(target),
                    code=0 if layout == "untested" else 1,
                )
                if layout == "untested":
                    _expect(
                        "Skipping example: no test_main.py" in result.stdout
                        and "0 passed, 0 failed, 1 skipped" in result.stdout,
                        result,
                    )
                elif layout != "single_untested":
                    _expect("no Python packages found" in result.stderr, result)
            if layout == "empty":
                _expect(
                    "no Python packages found" in _run(root, "coverage", code=1).stderr,
                    "coverage",
                )
            _expect(_snapshot(root) == before, "nonrunnable target created state")


@pytest.mark.parametrize("failure", [None, "coverage", "hypothesis", "mutation"])
def test_combined_campaigns_continue_after_failures_and_report_one_exit_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str | None,
) -> None:
    """Execute every campaign in order even when an earlier campaign fails."""
    subject = import_module("packages.afairesi-repo-test.main")
    _repository(tmp_path)
    monkeypatch.chdir(tmp_path)
    observed: list[str] = []

    def run(command: str) -> bool:
        observed.append(command)
        if command == failure and command != "mutation":
            message = "campaign failed"
            raise subject.CommandError(message)
        return command != failure

    def coverage_run(_target: Path) -> bool:
        return run("coverage")

    def runner(
        _target: Path,
        command: str,
        *_arguments: object,
    ) -> bool:
        return run(command)

    monkeypatch.setattr(subject, "_run_coverage", coverage_run)
    monkeypatch.setattr(subject, "_run_test_repository", runner)
    monkeypatch.setattr(sys, "argv", ["afairesi-repo-test", "--help"])
    with pytest.raises(SystemExit) as help_status:
        subject.main()
    _expect(help_status.value.code == 0 and not observed, observed)
    capsys.readouterr()
    monkeypatch.setattr(sys, "argv", ["afairesi-repo-test"])
    with pytest.raises(SystemExit) as completed:
        subject.main()
    _expect(completed.value.code == (0 if failure is None else 1), completed)
    _expect(observed == ["coverage", "hypothesis", "mutation"], observed)
    output = capsys.readouterr()
    _expect("Campaign summary:" in output.out, output)
    for command in observed:
        status = "failed" if command == failure else "passed"
        _expect(f"  {command}: {status}\n" in output.out, output)


def test_coverage_checks_measure_subprocesses_without_changing_sources() -> None:
    """Build real checks, measure CLI lines, and keep reports outside the checkout."""
    with TemporaryDirectory(prefix="afairesi-coverage-") as directory:
        root = Path(directory) / "source with spaces"
        environment = _prepare_coverage_flake(root)
        before = _snapshot(root)
        expression = (
            f"let f = builtins.getFlake {json.dumps('git+' + root.as_uri())}; "
            "in f.checks.${builtins.currentSystem}.example"
        )
        plain = _run(
            root,
            "build",
            "--no-link",
            "--print-out-paths",
            "--impure",
            "--expr",
            expression,
            executable="nix",
            environment=environment,
        )
        _expect(
            not any(
                path.name.startswith(".coverage")
                or path.name in {"html", "coverage.json"}
                for path in Path(plain.stdout.strip()).iterdir()
            ),
            "ordinary checks produced coverage artifacts",
        )
        result = _run_runner_cli(root, environment, "coverage")
        _expect(not result.returncode, result)
        reports = [
            line
            for line in result.stdout.splitlines()
            if line.endswith("/html/index.html")
        ]
        expected_reports = 2
        _expect(len(reports) == expected_reports, result)
        for line in reports:
            report = Path(line.split(": ", 1)[1]).parents[1]
            files = json.loads((report / "coverage.json").read_text())["files"]
            _expect(
                len(files) == 1 and next(iter(files)).endswith("/main.py"),
                files,
            )
            _expect(
                set(next(iter(files.values()))["executed_lines"]) == {1, 2, 3, 4, 6},
                files,
            )
            measured = next(iter(files.values()))
            _expect(
                {tuple(branch) for branch in measured["executed_branches"]}
                == {(3, 4), (3, 6)}
                and measured["contexts"]["4"] == ["test_main.py::test_cli[alternate]"]
                and measured["contexts"]["6"] == ["test_main.py::test_cli[ready]"],
                measured,
            )
            audit = json.loads((report / "tests.json").read_text())
            rows = {row["nodeid"]: row for row in audit["tests"]}
            expected_counts = {"collected_cases": 5, "functions": 4, "skipped": 2}
            _expect(
                audit["package"] in {"example", "z-last"}
                and all(
                    audit["summary"][key] == value
                    for key, value in expected_counts.items()
                )
                and audit["summary"]["unexecuted_properties"]
                == ["test_main.py::test_generated_only"]
                and rows["test_main.py::test_generated_only"]["body_calls"] == 0
                and rows["test_main.py::test_explicit"]["body_calls"] == 1
                and rows["test_main.py::test_optional_browser"]["skip_reason"]
                == "optional browser"
                and all(row["duration"] >= 0 for row in audit["tests"]),
                audit,
            )
        _expect(
            _snapshot(root) == before and not (root / "tmp").exists(),
            "coverage changed repository state",
        )


def test_coverage_continues_after_failed_nix_builds() -> None:
    """Report each failure and still build later runnable packages."""
    with TemporaryDirectory(prefix="afairesi-coverage-failure-") as directory:
        root = Path(directory) / "source"
        environment = _prepare_coverage_flake(root, fail_build=True)
        untested = _make_runner_target(root, "", "", name="untested")
        (untested / "test_main.py").unlink()
        (root / "flake.nix").write_text(_git(root, "show", ":flake.nix"))
        before = _snapshot(root)
        result = _run_runner_cli(root, environment, "coverage")
        _expect(
            result.returncode == 1 and "1 passed, 1 failed, 1 skipped" in result.stdout,
            result,
        )
        _expect(
            "z-last:" in result.stdout
            and "/html/index.html" in result.stdout
            and "afairesi-repo-test coverage: example:" in result.stderr,
            result,
        )
        _expect(
            _snapshot(root) == before,
            "failed coverage build changed repository state",
        )


@pytest.mark.parametrize("missing", ["check", "html", "tests"])
def test_coverage_rejects_missing_checks_and_report_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    missing: str,
) -> None:
    """Validate coverage output guards without rebuilding a successful package."""
    subject = import_module("packages.afairesi-repo-test.main")
    root = tmp_path / "source"
    root.mkdir()
    _repository(root)
    package = _make_source_package(root, "example", "def test_ready(): pass\n")
    check = root / "checks/example/default.nix"
    check.parent.mkdir(parents=True)
    if missing != "check":
        check.write_text("{}")
    report = tmp_path / "report"
    report.mkdir()
    if missing == "tests":
        (report / "html").mkdir()
        (report / "html/index.html").touch()
    original_run = subprocess.run
    builds: list[list[str]] = []

    def run(
        arguments: list[str],
        **keywords: Any,  # noqa: ANN401 - forward subprocess's keyword arguments
    ) -> subprocess.CompletedProcess[str]:
        if arguments[:2] == ["nix", "build"]:
            builds.append(arguments)
            return subprocess.CompletedProcess(arguments, 0, str(report) + "\n")
        return original_run(arguments, **keywords)

    monkeypatch.setattr(subject.subprocess, "run", run)
    before = _snapshot(root)
    message = {
        "check": f"missing {check}",
        "html": "no HTML report",
        "tests": "no test report",
    }[missing]
    monkeypatch.setattr(sys, "argv", ["afairesi-repo-test", "coverage", str(package)])
    with pytest.raises(SystemExit) as completed:
        subject.main()
    _expect(completed.value.code == 1 and message in capsys.readouterr().err, completed)
    _expect(len(builds) == (0 if missing == "check" else 1), builds)
    _expect(_snapshot(root) == before, "coverage validation modified source state")


def test_coverage_skips_untested_packages_and_continues_after_invalid_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Validate package eligibility and continuation separately from Nix builds."""
    subject = import_module("packages.afairesi-repo-test.main")
    _repository(tmp_path)
    _make_source_package(tmp_path, "alpha", "def invalid(")
    untested = _make_source_package(tmp_path, "untested", "")
    (untested / "test_main.py").unlink()
    ready = _make_source_package(tmp_path, "z-last", "def test_ready(): pass\n")
    built: list[Path] = []
    monkeypatch.setattr(subject, "_build_package_coverage", built.append)
    before = _snapshot(tmp_path)
    monkeypatch.setattr(sys, "argv", ["afairesi-repo-test", "coverage", str(tmp_path)])
    with pytest.raises(SystemExit) as completed:
        subject.main()
    _expect(completed.value.code == 1, "invalid source must fail coverage")
    output = capsys.readouterr()
    _expect(built == [ready], built)
    _expect(
        "1 passed, 1 failed, 1 skipped" in output.out
        and "afairesi-repo-test coverage: alpha:" in output.err,
        output,
    )
    _expect(_snapshot(tmp_path) == before, "coverage eligibility modified sources")


@pytest.mark.parametrize("failure", ["counterexample", "timeout"])
def test_hypothesis_campaigns_continue_after_failures(failure: str) -> None:
    """Retain counterexamples and terminate stalled descendants before continuing."""
    with TemporaryDirectory(prefix="afairesi-failed-campaign-") as directory:
        root = Path(directory) / "source with spaces"
        environment = _prepare_runner_flake(
            root,
            "def test_ready(): pass\n",
            name="z-last",
        )
        bad_tests = (
            (
                "from hypothesis import given, strategies as st\n"
                "@given(st.integers())\n"
                "def test_failure(value):\n"
                "    assert value != 0\n"
            )
            if failure == "counterexample"
            else (
                "import subprocess, sys, time\n"
                "from pathlib import Path\n"
                "def test_stalled():\n"
                "    child = subprocess.Popen([sys.executable, '-c', 'import "
                "time; time.sleep(30)'])\n"
                "    Path('child.pid').write_text(str(child.pid))\n"
                "    time.sleep(30)\n"
            )
        )
        _runner_package(root, "alpha", bad_tests)
        _runner_package(root, "untested", "")
        (root / "packages/untested/test_main.py").unlink()
        result = _campaign(
            root,
            environment,
            "hypothesis",
            "--max-examples",
            "1",
            "--timeout",
            "20" if failure == "counterexample" else "10",
        )
        _expect(
            result.returncode == 1 and "1 passed, 1 failed, 1 skipped" in result.stdout,
            result,
        )
        (failed,) = (root / "tmp").glob("python-hypothesis-alpha-*")
        diagnostic = (
            "Falsifying example" if failure == "counterexample" else "timed out"
        )
        _expect(
            diagnostic
            in result.stdout + result.stderr + (failed / "tests.log").read_text(),
            result,
        )
        if failure == "timeout":
            pid = int((failed / "child.pid").read_text())
            status = Path(f"/proc/{pid}/status")
            _expect(
                not status.exists() or "State:\tZ" in status.read_text(),
                "timed-out suite left a running descendant",
            )


@settings(deadline=None)
@given(max_examples=st.integers(min_value=1, max_value=5))
@example(max_examples=5)
def test_hypothesis_campaigns_generate_cases_in_isolated_sources(
    max_examples: int,
) -> None:
    """Count generated cases and isolate copied Git commands and runtime state."""
    with TemporaryDirectory(prefix="afairesi-campaign-") as directory:
        root = Path(directory) / "source with spaces"
        source = (
            "import json, os\nfrom pathlib import Path\n"
            "def main():\n"
            "    assert not (Path.home() / 'sentinel').exists()\n"
            "    Path('cli-environment.json').write_text(json.dumps("
            "{name: os.environ[name] for name in "
            "('HOME', 'XDG_CACHE_HOME', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME', "
            "'XDG_STATE_HOME', 'XDG_RUNTIME_DIR', 'TMPDIR')}))\n"
            "    print('ready')\n"
        )
        tests = (
            "import os, subprocess, sys\n"
            "from pathlib import Path\n"
            "from hypothesis import given, example, strategies as st\n"
            "@given(st.integers())\n"
            "@example(0)\n"
            "def test_property(value):\n"
            "    with Path('examples').open('a') as output:\n"
            "        output.write(str(value) + '\\n')\n"
            "def test_cli():\n"
            "    repository = subprocess.run(['git', 'rev-parse', "
            "'--show-toplevel'], capture_output=True, text=True)\n"
            "    assert repository.returncode != 0\n"
            "    result = subprocess.run(['git', 'example'], "
            "capture_output=True, text=True)\n"
            "    assert result.returncode == 0 and result.stdout == "
            "'ready\\n'\n"
            "    import json\n"
            "    directories = json.loads(Path('cli-environment.json').read_text())\n"
            "    for name, directory in directories.items():\n"
            "        assert directory == os.environ[name]\n"
            "        assert Path(directory).is_relative_to(Path.cwd())\n"
            "    assert not (Path.home() / 'sentinel').exists()\n"
            "    child = subprocess.run([sys.executable, '-c', "
            '"import importlib; '
            "print(importlib.import_module('packages.git-example.main').__file__)\"], "
            "cwd=Path.home(), capture_output=True, text=True)\n"
            "    assert child.returncode == 0, child.stderr\n"
            "    assert Path(child.stdout.strip()).is_relative_to("
            "Path.cwd() / 'packages')\n"
        )
        environment = _prepare_runner_flake(root, tests, source, name="git-example")
        caller_home = Path(directory) / "caller home"
        caller_home.mkdir()
        sentinel = caller_home / "sentinel"
        sentinel.write_text("preserve")
        environment.update(
            AFAIRESI_TEST_REPORT=str(sentinel),
            AFAIRESI_TEST_PACKAGE="unrelated-package",
            AFAIRESI_MUTATION_REPORT="1",
        )
        for variable in (
            "HOME",
            "XDG_CACHE_HOME",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "XDG_STATE_HOME",
            "XDG_RUNTIME_DIR",
            "TMPDIR",
        ):
            environment[variable] = str(caller_home)
        before = _snapshot(root, exclude=("tmp",))
        result = _run_runner_cli(
            root,
            environment,
            "hypothesis",
            "--max-examples",
            str(max_examples),
        )
        _expect(
            not result.returncode and "1 passed, 0 failed, 0 skipped" in result.stdout,
            result,
        )
        _expect(
            _snapshot(root, exclude=("tmp",)) == before,
            "explicit campaign modified source",
        )
        workspaces = list((root / "tmp").glob("python-hypothesis-git-example-*"))
        _expect(len(workspaces) == 1, workspaces)
        expected_case_count = 2
        for workspace in workspaces:
            _expect(
                len((workspace / "examples").read_text().splitlines())
                == max_examples + 1,
                workspace,
            )
            _expect(
                f"{max_examples} passing examples"
                in (workspace / "tests.log").read_text(),
                workspace,
            )
            report = json.loads((workspace / "tests.json").read_text())
            rows = {row["nodeid"]: row for row in report["tests"]}
            _expect(
                report["package"] == "git-example"
                and report["summary"]["selected_cases"] == expected_case_count
                and rows["test_main.py::test_property"]["body_calls"]
                == max_examples + 1
                and rows["test_main.py::test_cli"]["body_calls"] == 1
                and not report["summary"]["unexecuted_properties"]
                and not list(workspace.glob("test-runtime-*")),
                report,
            )
        _expect(
            sentinel.read_text() == "preserve",
            caller_home,
        )


def test_mutation_campaigns_reject_invalid_baselines_and_continue() -> None:
    """Check fixed baseline failures once, independently of generated values."""
    with TemporaryDirectory(prefix="afairesi-baselines-") as directory:
        root = Path(directory) / "source with spaces"
        environment = _prepare_runner_flake(
            root,
            "def test_ready(): pass\n",
            name="git-example",
        )
        baselines = {
            "alpha": "def test_failure():\n    assert False\n",
            "beta": "",
            "gamma": "raise ImportError('missing dependency')\n",
            "delta": (
                "import pytest\n@pytest.mark.skip(reason='disabled')\n"
                "def test_skipped():\n    assert False\n"
            ),
        }
        for name, baseline in baselines.items():
            _runner_package(root, name, baseline)
        _runner_package(root, "untested", "")
        (root / "packages/untested/test_main.py").unlink()
        result = _campaign(
            root,
            environment,
            "mutation",
            "--timeout",
            "10",
            "--max-mutations",
            "1",
        )
        _expect(
            result.returncode == 1
            and "1 passed, 4 failed, 1 skipped" in result.stdout
            and "baseline.log" in result.stderr,
            result,
        )
        for name in baselines:
            (failed,) = (root / "tmp").glob(f"python-mutation-{name}-*")
            _expect(
                (failed / "baseline.log").is_file()
                and not (failed / "summary.json").exists(),
                failed,
            )


def test_mutation_campaigns_report_empty_plans() -> None:
    """Accept an empty mutation plan when explicit baseline examples pass."""
    with TemporaryDirectory(prefix="afairesi-empty-mutations-") as directory:
        root = Path(directory) / "source"
        tests = (
            "from packages.example import main\n"
            "from hypothesis import given, example, strategies as st\n"
            "@given(st.just(1))\n"
            "@example(0)\n"
            "def test_explicit(value):\n"
            "    assert value == 0 and main is not None\n"
        )
        environment = _prepare_runner_flake(root, tests, "")
        result = _campaign(root, environment, "mutation")
        (workspace,) = (root / "tmp").glob("python-mutation-example-*")
        _expect(
            not result.returncode
            and json.loads((workspace / "summary.json").read_text()) == {},
            result,
        )


@settings(deadline=None)
@given(value=st.integers(min_value=1, max_value=9))
@example(value=1)
def test_mutation_campaigns_report_outcomes_and_replay_plans(
    value: int,
) -> None:
    """Report killed/surviving mutations and replay plans across generated values."""
    with TemporaryDirectory(prefix="afairesi-mutations-") as directory:
        root = Path(directory) / "source with spaces"
        source = (
            "def value():\n"
            f"    return {value}\n"
            "\n"
            "def unused():\n"
            "    return 2\n"
            "\n"
            "def main():\n"
            "    print(value())\n"
        )
        tests = (
            "import subprocess\nfrom pathlib import Path\n"
            "def test_cli():\n"
            "    marker = Path.home() / 'baseline-marker'\n"
            "    assert not marker.exists()\n"
            "    marker.write_text('created')\n"
            "    result = subprocess.run(['git', 'example'], "
            "capture_output=True, text=True)\n"
            "    assert result.returncode == 0 "
            f"and result.stdout == {str(value) + chr(10)!r}\n"
            "def test_cli_process_success():\n"
            "    result = subprocess.run(['git', 'example'], "
            "capture_output=True, text=True)\n"
            "    assert result.returncode == 0\n"
        )
        environment = _prepare_runner_flake(root, tests, source, name="git-example")
        result = _campaign(
            root,
            environment,
            "mutation",
            "--timeout",
            "10",
            cwd=root / "packages/git-example",
        )
        _expect(not result.returncode, result)
        (workspace,) = (root / "tmp").glob("python-mutation-git-example-*")
        summary = json.loads((workspace / "summary.json").read_text())
        _expect(
            summary.get("killed", 0) > 0
            and summary.get("survived", 0) > 0
            and (workspace / "report.html").stat().st_size > 0,
            summary,
        )
        target_line = 2
        focused = _campaign(
            root,
            environment,
            "mutation",
            "--timeout",
            "10",
            "-k",
            "test_cli and not process_success",
            "--lines",
            str(target_line),
            "--operator",
            "NumberReplacer",
            "--max-mutations",
            "1",
            cwd=root / "packages/git-example",
        )
        _expect(not focused.returncode, focused)
        focused_workspace = next(
            path
            for path in (root / "tmp").glob("python-mutation-git-example-*")
            if path != workspace
        )
        plan_path = focused_workspace / "mutation-plan.json"
        plan = json.loads(plan_path.read_text())
        defects = json.loads((focused_workspace / "mutation-results.json").read_text())
        _expect(
            len(plan["mutations"]) == len(defects["mutations"]) == 1
            and defects["mutations"][0]["status"] == "killed"
            and defects["mutations"][0]["mutations"][0]["start_pos"][0] == target_line
            and defects["kills_by_test"]
            == {
                "test_main.py::test_cli": [plan["mutations"][0]["id"]],
            },
            defects,
        )
        existing_workspaces = set((root / "tmp").iterdir())
        weaker = _campaign(
            root,
            environment,
            "mutation",
            "--timeout",
            "10",
            "-k",
            "process_success",
            "--mutation-plan",
            str(plan_path),
            cwd=root / "packages/git-example",
        )
        _expect(not weaker.returncode, weaker)
        (weaker_workspace,) = set((root / "tmp").iterdir()) - existing_workspaces
        weaker_defects = json.loads(
            (weaker_workspace / "mutation-results.json").read_text(),
        )
        _expect(
            len(weaker_defects["mutations"]) == 1
            and weaker_defects["mutations"][0]["id"] == defects["mutations"][0]["id"]
            and weaker_defects["mutations"][0]["status"] == "survived"
            and not weaker_defects["kills_by_test"],
            weaker_defects,
        )
        package_source = root / "packages/git-example/main.py"
        package_source.write_text(
            source.replace(f"return {value}\n", f"return {value + 10}\n"),
        )
        mismatch = _campaign(
            root,
            environment,
            "mutation",
            "--timeout",
            "10",
            "-k",
            "process_success",
            "--mutation-plan",
            str(plan_path),
            cwd=root / "packages/git-example",
        )
        _expect(
            mismatch.returncode == 1 and "does not match" in mismatch.stderr,
            mismatch,
        )


def test_static_cli_declares_commands_and_campaign_filters() -> None:
    """Preserve campaign filter arguments in the public parser."""
    subject = import_module("packages.afairesi-repo-test.main")
    for campaign in ("hypothesis", "mutation"):
        options = subject.parser().parse_args(
            [campaign, "-k", "keyword", "-m", "marker"],
        )
        _expect(
            options.keywords == "keyword" and options.markers == "marker",
            "Campaign filter arguments must retain their runtime behavior",
        )


def test_campaign_home_target_requires_native_git_traversal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Reject home scope instead of silently traversing registered submodules."""
    subject = import_module("packages.afairesi-repo-test.main")
    _git(tmp_path, "init", "--quiet")
    (tmp_path / ".gitmodules").touch()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["afairesi-repo-test"])
    with pytest.raises(SystemExit) as completed:
        subject.main()
    _expect(completed.value.code == 1, completed)
    _expect("git submodule foreach" in capsys.readouterr().err, completed)

#!/usr/bin/env python3
# Copyright (c) 2026- Paschalis Bizopoulos
"""Run coverage, property tests, and mutation tests for Afairesi packages."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import afairesi
from afairesi import (
    CommandError,
    has_python_tests,
    home_submodules,
    repository_root,
    repository_type,
    validate_name,
)


def _command_target(target: Path | None) -> Path:
    """Honor an explicit path or select the current Afairesi repository."""
    if target is not None:
        return target.resolve()
    root = repository_root()
    repository_type(root)
    return root


def _test_target_root(package: Path) -> Path:
    """Validate the canonical target and return its flake root."""
    validate_name(package.name)
    root = package.parent.parent
    if (
        package.parent.name != "packages"
        or not (root / "flake.nix").is_file()
        or not all(
            (package / name).is_file()
            for name in ("default.nix", "main.py", "test_main.py")
        )
    ):
        message = (
            "expected a canonical packages/NAME with default.nix, main.py "
            "and test_main.py inside a flake"
        )
        raise CommandError(message)
    return root


def _copy_test_sources(root: Path, workspace: Path) -> None:
    """Copy package sources and supporting assets without tmp or metadata."""
    ignored = shutil.ignore_patterns(
        "tmp",
        ".git",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "*.pyc",
    )
    for package in sorted((root / "packages").iterdir()):
        if package.is_dir() and not package.is_symlink():
            shutil.copytree(
                package,
                workspace / "packages" / package.name,
                ignore=ignored,
            )
    if (root / "prm").is_dir():
        shutil.copytree(root / "prm", workspace / "prm", ignore=ignored)


def _stop_test_group(pid: int) -> None:
    """Terminate a subprocess session, including its descendants."""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pid, signal.SIGKILL)


def _run_test_command(
    command: list[str],
    workspace: Path,
    log: Path,
    *,
    timeout: float | None = None,
) -> None:
    """Capture command output and clean up subprocess groups on interruption."""
    with (
        log.open("w", encoding="utf-8") as output,
        subprocess.Popen(  # noqa: S603
            command,
            cwd=workspace,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        ) as process,
    ):
        try:
            code = process.wait(timeout=timeout)
        except (KeyboardInterrupt, subprocess.TimeoutExpired):
            active = workspace / "active-test-pgid"
            if active.exists():
                _stop_test_group(int(active.read_text(encoding="utf-8")))
            _stop_test_group(process.pid)
            process.wait()
            raise
    if code:
        message = f"command failed ({code}); see {log}"
        raise CommandError(message)


@dataclass(frozen=True)
class TestEnvironment:
    """The interpreter and external tools resolved from a target package."""

    python: str
    path: str


def _build_test_environment(root: Path, name: str, workspace: Path) -> TestEnvironment:
    """Build the target's declared test environment through its flake output."""
    log = workspace / "environment.log"
    _run_test_command(
        [
            "nix",
            "build",
            "--no-update-lock-file",
            "--no-link",
            "--print-out-paths",
            _flake_installable(root, f"{name}-test-environment"),
        ],
        workspace,
        log,
    )
    paths = [
        line
        for line in log.read_text(encoding="utf-8").splitlines()
        if Path(line).is_absolute() and Path(line).is_file()
    ]
    if len(paths) != 1:
        message = f"could not resolve target environment; see {log}"
        raise CommandError(message)
    environment = json.loads(Path(paths[0]).read_text(encoding="utf-8"))
    return TestEnvironment(str(environment["python"]), str(environment["path"]))


@dataclass(frozen=True)
class TestSelection:
    """Select pytest cases and a reproducible subset of source mutations."""

    keywords: str = ""
    markers: str = ""
    lines: tuple[tuple[int, int], ...] = ()
    operators: tuple[str, ...] = ()
    max_mutations: int | None = None
    mutation_plan: Path | None = None

    def pytest_arguments(self) -> list[str]:
        """Render selectors without passing them through a shell."""
        arguments = []
        for option, value in (("-k", self.keywords), ("-m", self.markers)):
            if value:
                arguments.extend([option, value])
        return arguments


def _test_report_source() -> str:
    """Read the pytest plugin shared by coverage and on-demand campaigns."""
    return (Path(afairesi.__file__).with_name("prm") / "test_report.py").read_text(
        encoding="utf-8",
    )


def _prepare_package_tests(
    workspace: Path,
    name: str,
    environment: TestEnvironment,
    max_examples: int | None = None,
    *,
    selection: TestSelection | None = None,
) -> list[str]:
    """Run pytest and package executables against the same isolated source copy."""
    selection = selection or TestSelection()
    launcher = workspace / "bin" / name
    launcher.parent.mkdir()
    launcher.write_text(
        "#!/bin/sh\nexec "
        + shlex.join([environment.python, str(workspace / "package-entry.py")])
        + ' "$@"\n',
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    (workspace / "package-entry.py").write_text(
        "import importlib, sys\n"
        f"sys.argv[0] = {str(launcher)!r}\n"
        f"importlib.import_module({'packages.' + name + '.main'!r}).main()\n",
        encoding="utf-8",
    )
    profile = (
        "    from hypothesis import Phase, settings\n"
        '    settings.register_profile("coverage", phases=[Phase.explicit])\n'
        '    settings.load_profile("coverage")\n'
        if max_examples is None
        else "    from hypothesis import settings\n"
        f'    settings.register_profile("ondemand", max_examples={max_examples},'
        " deadline=None)\n"
        '    settings.load_profile("ondemand")\n'
    )
    (workspace / "_afairesi_test_report.py").write_text(
        _test_report_source(),
        encoding="utf-8",
    )
    pytest_arguments = [
        "-p",
        "no:cacheprovider",
        "-p",
        "_afairesi_test_report",
        "-p",
        "_hypothesis_pytestplugin",
        *selection.pytest_arguments(),
    ]
    if max_examples is not None:
        pytest_arguments.extend(
            ["--hypothesis-show-statistics"],
        )
    pytest_arguments.extend(
        ["--import-mode=importlib", "-q", f"packages/{name}/test_main.py"],
    )
    bootstrap = workspace / "run-tests.py"
    bootstrap.write_text(
        "import os, sys\n"
        "from pathlib import Path\n"
        "from tempfile import TemporaryDirectory\n"
        "os.dup2(1, 2)\n"
        f"os.environ['PACKAGE_E2E_EXECUTABLE'] = {str(launcher)!r}\n"
        f"tools = {str(launcher.parent) + os.pathsep + environment.path!r}\n"
        "os.environ['PATH'] = tools + os.pathsep + os.environ.get('PATH', '')\n"
        "os.environ['PYTHONDONTWRITEBYTECODE'] = '1'\n"
        f"os.environ['GIT_CEILING_DIRECTORIES'] = {str(workspace.parent)!r}\n"
        "os.environ.pop('PYTHONPATH', None)\n"
        "os.environ.pop('PYTEST_ADDOPTS', None)\n"
        "os.environ['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'\n"
        "os.environ.pop('PYTEST_PLUGINS', None)\n"
        "os.environ.pop('AFAIRESI_TEST_REPORT_OWNER', None)\n"
        "os.environ.pop('AFAIRESI_TEST_CONTEXT', None)\n"
        f"os.environ['AFAIRESI_TEST_PACKAGE'] = {name!r}\n"
        f"os.environ['AFAIRESI_TEST_REPORT'] = {str(workspace / 'tests.json')!r}\n"
        "os.environ['AFAIRESI_MUTATION_REPORT'] = "
        f"{str(int(max_examples is None))!r}\n"
        "os.environ['HYPOTHESIS_STORAGE_DIRECTORY'] = "
        f"{str(workspace / 'hypothesis')!r}\n"
        "runtime = TemporaryDirectory(prefix='test-runtime-', "
        f"dir={str(workspace)!r})\n"
        "directories = {'HOME': 'home', 'XDG_CACHE_HOME': 'cache',\n"
        "    'XDG_CONFIG_HOME': 'config', 'XDG_DATA_HOME': 'data',\n"
        "    'XDG_STATE_HOME': 'state', 'XDG_RUNTIME_DIR': 'run', 'TMPDIR': 'tmp'}\n"
        "for variable, name in directories.items():\n"
        "    directory = Path(runtime.name) / name\n"
        "    directory.mkdir(mode=0o700)\n"
        "    os.environ[variable] = str(directory)\n"
        "pid = Path('active-test-pgid')\n"
        "pid.write_text(str(os.getpgrp()))\n"
        "try:\n" + profile + "    import pytest\n"
        f"    sys.exit(pytest.main({pytest_arguments!r}))\n"
        "finally:\n"
        "    pid.unlink(missing_ok=True)\n"
        "    runtime.cleanup()\n",
        encoding="utf-8",
    )
    return [environment.python, "-B", str(bootstrap)]


def _mutation_id(mutations: list[dict[str, Any]]) -> str:
    """Identify a source mutation independently of Cosmic Ray's random job ID."""
    encoded = json.dumps(mutations, sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _select_mutations(workspace: Path, name: str, selection: TestSelection) -> None:
    """Filter the initialized session and retain a plan reusable with another suite."""
    from attrs import asdict  # noqa: PLC0415 - only mutation campaigns need the engine
    from cosmic_ray.work_db import WorkDB, use_db  # noqa: PLC0415
    from cosmic_ray.work_item import WorkerOutcome, WorkResult  # noqa: PLC0415

    source_hash = hashlib.sha256(
        (workspace / "packages" / name / "main.py").read_bytes(),
    ).hexdigest()
    replay_ids: set[str] | None = None
    if selection.mutation_plan is not None:
        try:
            plan = json.loads(selection.mutation_plan.read_text(encoding="utf-8"))
            if (
                plan["schema"] != "afairesi.mutation-plan"
                or plan["schema_version"] != 1
                or plan["package"] != name
                or plan["source_sha256"] != source_hash
            ):
                message = "mutation plan does not match the package source"
                raise CommandError(message)
            replay_ids = {entry["id"] for entry in plan["mutations"]}
        except (OSError, ValueError, KeyError, TypeError) as error:
            message = f"invalid mutation plan: {selection.mutation_plan}: {error}"
            raise CommandError(message) from error
    operators = [re.compile(pattern) for pattern in selection.operators]
    with use_db(workspace / "session.sqlite", WorkDB.Mode.open) as database:
        candidates = []
        for item in database.work_items:
            mutations = [asdict(mutation) for mutation in item.mutations]
            for mutation in mutations:
                mutation["module_path"] = str(mutation["module_path"])
            identity = _mutation_id(mutations)
            matches = all(
                (
                    not selection.lines
                    or any(
                        start <= mutation.start_pos[0] <= end
                        for start, end in selection.lines
                    )
                )
                and (
                    not operators
                    or any(
                        pattern.search(mutation.operator_name) for pattern in operators
                    )
                )
                for mutation in item.mutations
            )
            if matches and (replay_ids is None or identity in replay_ids):
                candidates.append((identity, item, mutations))
        candidates.sort(key=lambda candidate: candidate[0])
        if replay_ids is not None and replay_ids != {row[0] for row in candidates}:
            message = (
                "mutation plan contains mutations unavailable "
                "with these filters or engine"
            )
            raise CommandError(message)
        if selection.max_mutations is not None:
            candidates = candidates[: selection.max_mutations]
        selected_jobs = {item.job_id for _, item, _ in candidates}
        if database.num_work_items and not selected_jobs:
            message = "no mutations matched the selection"
            raise CommandError(message)
        database.set_multiple_results(
            [
                item.job_id
                for item in database.work_items
                if item.job_id not in selected_jobs
            ],
            WorkResult(worker_outcome=WorkerOutcome.SKIPPED, output="not selected"),
        )
        manifest = {
            "schema": "afairesi.mutation-plan",
            "schema_version": 1,
            "package": name,
            "source_sha256": source_hash,
            "mutations": [
                {"id": identity, "job_id": item.job_id, "mutations": mutations}
                for identity, item, mutations in candidates
            ],
        }
        (workspace / "mutation-plan.json").write_text(
            json.dumps(manifest, indent=2) + "\n",
            encoding="utf-8",
        )
        sys.stdout.write(
            f"Selected {len(selected_jobs)} of {database.num_work_items} mutations.\n",
        )


def _mutation_status(result: dict[str, Any] | None) -> str:
    """Keep filtered jobs, timeouts, and engine errors out of assertion kills."""
    if result is None:
        return "pending"
    if result["worker_outcome"] == "skipped":
        return "skipped"
    if result["worker_outcome"] != "normal" or result["test_outcome"] == "incompetent":
        return "error"
    if result["output"] == "timeout":
        return "timeout"
    return str(result["test_outcome"])


def _mutation_attribution(result: dict[str, Any] | None) -> dict[str, Any]:
    """Read the pytest report captured in an individual worker's output."""
    if result is not None:
        prefix = "AFAIRESI_TEST_REPORT "
        for line in reversed((result["output"] or "").splitlines()):
            if line.startswith(prefix):
                return cast("dict[str, Any]", json.loads(line.removeprefix(prefix)))
    return {}


def _summarize_mutations(workspace: Path) -> bool:
    """Report engine outcomes without treating survivors as command failures."""
    counts: Counter[str] = Counter()
    survivors: list[str] = []
    mutations = []
    kills: dict[str, list[str]] = {}
    for line in (workspace / "results.jsonl").read_text(encoding="utf-8").splitlines():
        item, result = json.loads(line)
        status = _mutation_status(result)
        counts[status] += 1
        attribution = _mutation_attribution(result)
        identity = _mutation_id(item["mutations"])
        failed_tests = attribution.get("failed_tests", [])
        if status == "killed":
            for nodeid in failed_tests:
                kills.setdefault(nodeid, []).append(identity)
        if status != "skipped":
            mutations.append(
                {
                    "id": identity,
                    "status": status,
                    "mutations": item["mutations"],
                    "failed_tests": failed_tests,
                    "collection_errors": attribution.get("collection_errors", []),
                    "diff": result["diff"] if result is not None else None,
                },
            )
        if status == "survived":
            survivors.append(f"Survived {item['job_id']}:\n{result['diff']}")
    summary = dict(counts)
    (workspace / "summary.json").write_text(
        json.dumps(summary, indent=2) + "\n",
        encoding="utf-8",
    )
    (workspace / "mutation-results.json").write_text(
        json.dumps(
            {
                "schema": "afairesi.mutations",
                "schema_version": 1,
                "summary": summary,
                "mutations": sorted(mutations, key=lambda mutation: mutation["id"]),
                "kills_by_test": {
                    nodeid: sorted(ids) for nodeid, ids in sorted(kills.items())
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    if not counts:
        sys.stdout.write("No mutations generated.\n")
    else:
        statuses = ("killed", "survived", "timeout", "error", "pending", "skipped")
        sys.stdout.write(", ".join(f"{key}: {counts[key]}" for key in statuses) + "\n")
    if survivors:
        sys.stdout.write("\n".join(survivors) + "\n")
    return not (counts["error"] or counts["pending"])


def _run_mutation_campaign(
    workspace: Path,
    name: str,
    environment: TestEnvironment,
    timeout: float,
    selection: TestSelection,
) -> bool:
    """Baseline, mutate, and report one copied package."""
    command = _prepare_package_tests(
        workspace,
        name,
        environment,
        selection=selection,
    )
    sys.stdout.write("Running baseline tests...\n")
    sys.stdout.flush()
    _run_test_command(command, workspace, workspace / "baseline.log", timeout=timeout)
    shutil.copyfile(workspace / "tests.json", workspace / "baseline-tests.json")
    baseline = json.loads((workspace / "baseline-tests.json").read_text())
    if not baseline["summary"].get("passed", 0):
        message = "selected baseline has no passing tests; see baseline-tests.json"
        raise CommandError(message)
    config = workspace / "cosmic-ray.toml"
    config.write_text(
        "[cosmic-ray]\n"
        f"module-path = {json.dumps('packages/' + name + '/main.py')}\n"
        f"timeout = {timeout}\n"
        "excluded-modules = []\n"
        f"test-command = {json.dumps(shlex.join(command))}\n"
        '[cosmic-ray.distributor]\nname = "local"\n',
        encoding="utf-8",
    )
    engine = ["cosmic-ray"]
    session = str(workspace / "session.sqlite")
    _run_test_command(
        [*engine, "init", str(config), session],
        workspace,
        workspace / "init.log",
    )
    _select_mutations(workspace, name, selection)
    sys.stdout.write("Running mutations...\n")
    sys.stdout.flush()
    _run_test_command(
        [*engine, "exec", str(config), session],
        workspace,
        workspace / "engine.log",
    )
    _run_test_command(
        [*engine, "dump", session],
        workspace,
        workspace / "results.jsonl",
    )
    _run_test_command(
        ["cr-html", session],
        workspace,
        workspace / "report.html",
    )
    return _summarize_mutations(workspace)


def _run_test_package(
    package: Path,
    command: str,
    timeout: float | None,
    max_examples: int | None,
    selection: TestSelection,
) -> bool:
    """Run one isolated package and retain its logs and reports."""
    root = _test_target_root(package)
    tmp = root / "tmp"
    tmp.mkdir(exist_ok=True)
    workspace = Path(
        tempfile.mkdtemp(prefix=f"python-{command}-{package.name}-", dir=tmp),
    )
    label = (
        "Mutation workspace and reports"
        if command == "mutation"
        else "Hypothesis workspace and logs"
    )
    sys.stdout.write(f"{label}: {workspace}\n")
    sys.stdout.flush()
    _copy_test_sources(root, workspace)
    environment = _build_test_environment(root, package.name, workspace)
    if command == "mutation":
        if timeout is None:
            message = "mutation tests require a timeout"
            raise CommandError(message)
        return _run_mutation_campaign(
            workspace,
            package.name,
            environment,
            timeout,
            selection,
        )
    arguments = _prepare_package_tests(
        workspace,
        package.name,
        environment,
        max_examples,
        selection=selection,
    )
    log = workspace / "tests.log"
    try:
        _run_test_command(arguments, workspace, log, timeout=timeout)
    finally:
        if log.exists():
            sys.stdout.write(log.read_text(encoding="utf-8"))
    return True


def _run_test_repository(
    root: Path,
    command: str,
    timeout: float | None,
    max_examples: int | None,
    selection: TestSelection,
) -> bool:
    """Run each Python package, continuing after failures and summarizing results."""
    directory = root / "packages"
    packages = (
        sorted(
            path
            for path in directory.iterdir()
            if path.is_dir() and not path.is_symlink() and (path / "main.py").is_file()
        )
        if directory.is_dir()
        else []
    )
    if not packages:
        message = f"no Python packages found under {directory}"
        raise CommandError(message)
    outcomes: dict[str, str] = {}
    for package in packages:
        if not (package / "test_main.py").is_file():
            outcomes[package.name] = "skipped"
            sys.stdout.write(f"Skipping {package.name}: no test_main.py\n")
            continue
        sys.stdout.write(f"Running {package.name}...\n")
        sys.stdout.flush()
        try:
            outcomes[package.name] = (
                "passed"
                if _run_test_package(package, command, timeout, max_examples, selection)
                else "failed"
            )
        except (CommandError, OSError, subprocess.TimeoutExpired) as error:
            outcomes[package.name] = "failed"
            sys.stderr.write(f"afairesi-tests {command}: {package.name}: {error}\n")
    sys.stdout.write("\nRepository summary:\n")
    for package_name, status in outcomes.items():
        sys.stdout.write(f"  {package_name}: {status}\n")
    sys.stdout.write(
        ", ".join(
            f"{sum(value == status for value in outcomes.values())} {status}"
            for status in ("passed", "failed", "skipped")
        )
        + "\n",
    )
    return "failed" not in outcomes.values()


def _dispatch_test_runner(
    options: argparse.Namespace,
    cli: argparse.ArgumentParser,
) -> bool:
    """Validate budgets and run an explicitly requested test campaign."""
    max_examples = getattr(options, "max_examples", None)
    if max_examples is not None and max_examples <= 0:
        cli.error("--max-examples must be positive")
    if options.timeout is not None and (
        not math.isfinite(options.timeout) or options.timeout <= 0
    ):
        cli.error("--timeout must be positive and finite")
    mutation_limit = getattr(options, "max_mutations", None)
    if mutation_limit is not None and mutation_limit <= 0:
        cli.error("--max-mutations must be positive")
    operators = getattr(options, "operators", ())
    try:
        for pattern in operators:
            re.compile(pattern)
    except re.error as error:
        cli.error(f"invalid --operator expression: {error}")
    selection = TestSelection(
        keywords=options.keywords,
        markers=options.markers,
        lines=tuple(getattr(options, "lines", ())),
        operators=tuple(operators),
        max_mutations=mutation_limit,
        mutation_plan=getattr(options, "mutation_plan", None),
    )
    target = _command_target(options.target)
    runner = (
        _run_test_repository if (target / "flake.nix").is_file() else _run_test_package
    )
    return runner(
        target,
        options.test_command,
        options.timeout,
        max_examples,
        selection,
    )


def _flake_installable(root: Path, attribute: str) -> str:
    """Select a named output while retaining Git's tracked-source filtering."""
    return f"git+{root.as_uri()}#{attribute}"


def _build_package_coverage(package: Path) -> None:
    """Build an instrumented variant of the test check and print its report path."""
    root = _test_target_root(package)
    check = root / "checks" / package.name / "default.nix"
    if not check.is_file():
        message = f"missing {check}; run nix fmt to generate checks"
        raise CommandError(message)
    sys.stdout.write(f"Building coverage for {package.name}...\n")
    sys.stdout.flush()
    completed = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "nix",
            "build",
            "--no-link",
            "--no-update-lock-file",
            "--print-out-paths",
            _flake_installable(root, f"{package.name}-coverage"),
        ],
        stdout=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode:
        message = f"coverage check failed for {package.name}"
        raise CommandError(message)
    outputs = completed.stdout.splitlines()
    if len(outputs) != 1:
        message = f"expected one coverage output for {package.name}"
        raise CommandError(message)
    report = Path(outputs[0]) / "html" / "index.html"
    if not report.is_file():
        message = (
            f"coverage check produced no HTML report for {package.name}; "
            "run nix fmt to update the test checks"
        )
        raise CommandError(message)
    sys.stdout.write(f"{package.name}: {report}\n")
    test_report = Path(outputs[0]) / "tests.json"
    if not test_report.is_file():
        message = f"coverage check produced no test report for {package.name}"
        raise CommandError(message)
    summary = json.loads(test_report.read_text(encoding="utf-8"))["summary"]
    sys.stdout.write(
        f"{package.name}: {summary['selected_cases']} cases, "
        f"{summary.get('skipped', 0)} skipped; {test_report}\n",
    )
    if summary["unexecuted_properties"]:
        sys.stdout.write(
            f"{package.name}: properties with no body executions: "
            + ", ".join(summary["unexecuted_properties"])
            + "\n",
        )


def _run_coverage(target: Path) -> bool:
    """Build package checks independently, preserving caching and reporting failures."""
    repository = (target / "flake.nix").is_file()
    if repository:
        directory = target / "packages"
        packages = (
            sorted(
                path
                for path in directory.iterdir()
                if path.is_dir()
                and not path.is_symlink()
                and (path / "main.py").is_file()
            )
            if directory.is_dir()
            else []
        )
        if not packages:
            message = f"no Python packages found under {directory}"
            raise CommandError(message)
    else:
        _test_target_root(target)
        packages = [target]
    outcomes: dict[str, str] = {}
    for package in packages:
        try:
            if repository and (
                not (package / "test_main.py").is_file()
                or not has_python_tests(package / "test_main.py")
            ):
                outcomes[package.name] = "skipped"
                sys.stdout.write(f"Skipping {package.name}: no Python tests\n")
                continue
            _build_package_coverage(package)
            outcomes[package.name] = "passed"
        except (CommandError, OSError) as error:
            outcomes[package.name] = "failed"
            sys.stderr.write(f"afairesi-tests coverage: {package.name}: {error}\n")
    if repository:
        sys.stdout.write("\nRepository summary:\n")
        for name, status in outcomes.items():
            sys.stdout.write(f"  {name}: {status}\n")
        sys.stdout.write(
            ", ".join(
                f"{sum(value == status for value in outcomes.values())} {status}"
                for status in ("passed", "failed", "skipped")
            )
            + "\n",
        )
    return "failed" not in outcomes.values()


def _mutation_lines(value: str) -> tuple[int, int]:
    """Validate inclusive source line ranges before a campaign creates state."""
    if not re.fullmatch(r"[0-9]+(?::[0-9]+)?", value):
        message = "expected a positive line number or START:END"
        raise argparse.ArgumentTypeError(message)
    first, _, last = value.partition(":")
    start, end = int(first), int(last or first)
    if not 0 < start <= end:
        message = "expected 0 < START <= END"
        raise argparse.ArgumentTypeError(message)
    return start, end


def parser() -> argparse.ArgumentParser:
    """Construct the test campaign command-line parser."""
    result = argparse.ArgumentParser(
        prog="afairesi-tests",
        description=(
            "Run coverage, property tests, and mutation tests for Afairesi "
            "packages. Without a subcommand, run all three campaigns in the "
            "current flake or the registered submodules of the current home repository."
        ),
    )
    result.set_defaults(test_command=None, target=None)
    commands = result.add_subparsers(dest="test_command", metavar="COMMAND")
    coverage = commands.add_parser(
        "coverage",
        help="measure test coverage and print HTML report paths",
        description="Measure coverage from explicit test examples.",
        epilog=(
            "Repository targets skip packages without tests and summarize results. "
            "HTML reports are stored in the Nix store. "
            "Use nix fmt to create or update checks."
        ),
    )
    coverage.add_argument(
        "target",
        type=Path,
        nargs="?",
        default=None,
        metavar="PATH",
        help="package, flake, or home (default: current repository and its submodules)",
    )
    hypothesis = commands.add_parser(
        "hypothesis",
        help="run generated property tests in isolated package copies",
        description="Run generated property tests in isolated package copies.",
        epilog=(
            "Repository targets run Python packages sequentially, skip packages "
            "without test_main.py, and summarize results. Logs and reports "
            "are retained under the flake's tmp/ directory."
        ),
    )
    hypothesis.add_argument(
        "target",
        type=Path,
        nargs="?",
        default=None,
        metavar="PATH",
        help="package, flake, or home (default: current repository and its submodules)",
    )
    hypothesis.add_argument(
        "--timeout",
        type=float,
        help=(
            "seconds per test-suite invocation, excluding environment build "
            "(default: unlimited)"
        ),
    )
    hypothesis.add_argument(
        "--max-examples",
        type=int,
        default=100,
        help="successful generated examples per property (default: 100)",
    )
    mutation = commands.add_parser(
        "mutation",
        help="run mutation tests in isolated package copies",
        description="Run mutation tests in isolated package copies.",
        epilog=(
            "Repository targets run Python packages sequentially, skip packages "
            "without test_main.py, and summarize results. Logs and reports "
            "are retained under the flake's tmp/ directory."
        ),
    )
    mutation.add_argument(
        "target",
        type=Path,
        nargs="?",
        default=None,
        metavar="PATH",
        help="package, flake, or home (default: current repository and its submodules)",
    )
    mutation.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        help=(
            "seconds per test-suite invocation, excluding environment build "
            "(default: 300)"
        ),
    )
    hypothesis.add_argument(
        "-k",
        dest="keywords",
        default="",
        help="run pytest cases matching this keyword expression",
    )
    hypothesis.add_argument(
        "-m",
        dest="markers",
        default="",
        help="run pytest cases matching this marker expression",
    )
    mutation.add_argument(
        "-k",
        dest="keywords",
        default="",
        help="run pytest cases matching this keyword expression",
    )
    mutation.add_argument(
        "-m",
        dest="markers",
        default="",
        help="run pytest cases matching this marker expression",
    )
    mutation.add_argument(
        "--lines",
        type=_mutation_lines,
        action="append",
        default=[],
        metavar="START:END",
        help=(
            "select mutations starting in this inclusive "
            "main.py line range (repeatable)"
        ),
    )
    mutation.add_argument(
        "--operator",
        dest="operators",
        action="append",
        default=[],
        metavar="REGEX",
        help="select mutation operators matching a regular expression (repeatable)",
    )
    mutation.add_argument(
        "--max-mutations",
        type=int,
        metavar="N",
        help="maximum mutations to test",
    )
    mutation.add_argument(
        "--mutation-plan",
        type=Path,
        metavar="PATH",
        help=(
            "reuse a mutation-plan.json against the same "
            "package source and different tests"
        ),
    )
    return result


def _run_test_targets(
    campaign: argparse.Namespace,
    cli: argparse.ArgumentParser,
    target: Path,
    targets: list[Path],
) -> bool:
    """Run one campaign across selected repositories and collect every failure."""
    command = campaign.test_command
    results: dict[Path, bool] = {}
    for selected in targets:
        selected_options = argparse.Namespace(**vars(campaign))
        selected_options.target = selected
        if selected != target:
            sys.stdout.write(f"\nRunning {command} for {selected}...\n")
            sys.stdout.flush()
        try:
            results[selected] = (
                _run_coverage(selected)
                if command == "coverage"
                else _dispatch_test_runner(selected_options, cli)
            )
        except (CommandError, OSError, subprocess.TimeoutExpired) as error:
            results[selected] = False
            sys.stderr.write(f"afairesi-tests {command}: {selected}: {error}\n")
        except KeyboardInterrupt:
            sys.stderr.write(f"afairesi-tests {command}: interrupted\n")
            sys.exit(130)
    if targets != [target]:
        sys.stdout.write(f"\nSubmodule summary ({command}):\n")
        for selected, success in results.items():
            sys.stdout.write(
                f"  {selected.relative_to(target)}: "
                f"{'passed' if success else 'failed'}\n",
            )
    return all(results.values())


def _dispatch_test_command(
    options: argparse.Namespace,
    cli: argparse.ArgumentParser,
) -> bool:
    """Run selected campaigns, continuing after failures and returning one status."""
    combined = options.test_command is None
    target = _command_target(options.target)
    targets = _test_targets(target)
    if combined:
        campaigns = [
            cli.parse_args([command, str(target)])
            for command in ("coverage", "hypothesis", "mutation")
        ]
    else:
        campaigns = [options]
    outcomes: dict[str, bool] = {}
    for campaign in campaigns:
        command = campaign.test_command
        if combined:
            sys.stdout.write(f"\nRunning {command} campaign...\n")
            sys.stdout.flush()
        outcomes[command] = _run_test_targets(campaign, cli, target, targets)
    if combined:
        sys.stdout.write("\nCampaign summary:\n")
        for command, success in outcomes.items():
            sys.stdout.write(f"  {command}: {'passed' if success else 'failed'}\n")
    sys.exit(0 if all(outcomes.values()) else 1)


def _test_targets(target: Path) -> list[Path]:
    """Expand a home target into its registered submodules without changing them."""
    if repository_type(target, "git") != "home":
        return [target]
    targets = []
    for repository in home_submodules(target, require_url=False):
        checkout = (target / repository["path"]).resolve()
        if not checkout.is_relative_to(target) or checkout == target:
            message = (
                f"submodule path escapes the home repository: {repository['path']}"
            )
            raise CommandError(message)
        targets.append(checkout)
    if not targets:
        message = f"no submodules found under {target}"
        raise CommandError(message)
    return targets


def main() -> None:
    """Dispatch the selected test campaigns."""
    cli = parser()
    options = cli.parse_args()
    try:
        _dispatch_test_command(options, cli)
    except (CommandError, OSError, subprocess.TimeoutExpired) as error:
        print(f"error: {error}", file=sys.stderr)  # noqa: T201
        raise SystemExit(1) from error
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()

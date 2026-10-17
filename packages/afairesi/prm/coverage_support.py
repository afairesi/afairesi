# Copyright (c) 2026- Paschalis Bizopoulos
"""Support subprocess coverage and map installed modules to source files."""

import os
from pathlib import Path

import coverage


def start_subprocess_coverage() -> None:
    """Start coverage in child interpreters and inherit the active test context."""
    tracer = coverage.Coverage.current() or coverage.process_startup()
    if tracer is not None:
        tracer.switch_context(os.environ.get("AFAIRESI_TEST_CONTEXT", ""))


def remap_coverage_paths(installed_path: str) -> None:
    """Combine installed-module coverage with the corresponding source file."""
    data = coverage.CoverageData()
    data.read()
    mapped = coverage.CoverageData(basename=".coverage-mapped")
    mapped.update(
        data,
        map_path=lambda path: (
            os.environ["src"] + "/main.py"  # noqa: SIM112 - Nix build environment.
            if path == installed_path
            else path
        ),
    )
    mapped.write()
    Path(mapped.data_filename()).replace(data.data_filename())

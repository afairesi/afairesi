# Copyright (c) 2026- Paschalis Bizopoulos
"""Carry pytest test contexts into covered package subprocesses."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import coverage
import pytest

if TYPE_CHECKING:
    from collections.abc import Generator


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item: pytest.Item) -> Generator[None, object, object]:
    """Attribute test setup, execution, teardown, and children to their test."""
    previous = os.environ.get("AFAIRESI_TEST_CONTEXT", "")
    os.environ["AFAIRESI_TEST_CONTEXT"] = item.nodeid
    tracer = coverage.Coverage.current()
    if tracer is not None:
        tracer.switch_context(item.nodeid)
    try:
        return (yield)
    finally:
        os.environ["AFAIRESI_TEST_CONTEXT"] = previous
        if tracer is not None:
            tracer.switch_context(previous)

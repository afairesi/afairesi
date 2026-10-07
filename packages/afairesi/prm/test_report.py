import functools
import json
import os
import sys
from collections import Counter
from pathlib import Path
import pytest
from hypothesis import Phase, is_hypothesis_test, settings

tests = {}
collection_errors = []
owner = os.environ.setdefault("AFAIRESI_TEST_REPORT_OWNER", str(os.getpid()))


def node_id(item):
    path, separator, name = item.nodeid.partition("::")
    return Path(path).name + separator + name


def record(item):
    key = node_id(item)
    if key not in tests:
        function = getattr(item, "obj", None)
        property_test = is_hypothesis_test(function)
        instance = getattr(function, "__self__", None)
        profile = getattr(
            function,
            "_hypothesis_internal_use_settings",
            getattr(instance, "settings", settings.default),
        )
        examples = getattr(function, "hypothesis_explicit_examples", ())
        handle = getattr(function, "hypothesis", None)
        measured = not property_test or hasattr(handle, "inner_test")
        tests[key] = {
            "nodeid": key,
            "function": key.split("[", 1)[0],
            "outcome": "not_run",
            "duration": 0.0,
            "body_calls": 0 if measured else None,
            "is_property": property_test,
            "explicit_examples": len(examples),
            "generation_enabled": property_test and Phase.generate in profile.phases,
        }
    return tests[key]


def pytest_collection_finish(session):
    for item in session.items:
        record(item)


def pytest_deselected(items):
    for item in items:
        record(item)["outcome"] = "deselected"


def pytest_collectreport(report):
    if report.failed:
        collection_errors.append(str(report.longrepr))


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item, nextitem):
    previous = os.environ.get("AFAIRESI_TEST_CONTEXT", "")
    context = node_id(item) if owner == str(os.getpid()) else previous
    os.environ["AFAIRESI_TEST_CONTEXT"] = context
    try:
        import coverage

        tracer = coverage.Coverage.current()
    except ImportError:
        tracer = None
    if tracer is not None:
        tracer.switch_context(context)
    try:
        return (yield)
    finally:
        os.environ["AFAIRESI_TEST_CONTEXT"] = previous
        if tracer is not None:
            tracer.switch_context(previous)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item):
    row = record(item)
    handle = getattr(getattr(item, "obj", None), "hypothesis", None)
    original = getattr(handle, "inner_test", None)
    if original is None:
        if not row["is_property"]:
            row["body_calls"] += 1
    else:

        @functools.wraps(original)
        def counted(*args, **kwargs):
            row["body_calls"] += 1
            return original(*args, **kwargs)

        handle.inner_test = counted
    try:
        return (yield)
    finally:
        if original is not None:
            handle.inner_test = original


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    report = yield
    row = record(item)
    row["duration"] += report.duration
    if report.failed or row["outcome"] != "failed":
        if report.failed or report.skipped or report.when == "call":
            row["outcome"] = report.outcome
    if report.skipped:
        reason = report.longrepr
        reason = str(reason[-1] if isinstance(reason, tuple) else reason)
        row["skip_reason"] = reason.removeprefix("Skipped: ")
    if hasattr(report, "wasxfail"):
        row["xfail_reason"] = report.wasxfail
    statistics = getattr(item, "hypothesis_statistics", None)
    if statistics:
        row["hypothesis_statistics"] = statistics
    return report


def pytest_sessionfinish(session, exitstatus):
    if owner != str(os.getpid()):
        return
    rows = sorted(tests.values(), key=lambda row: row["nodeid"])
    selected = [row for row in rows if row["outcome"] != "deselected"]
    unexecuted = [
        row["nodeid"]
        for row in selected
        if row["is_property"] and row["body_calls"] == 0
    ]
    summary = dict(Counter(row["outcome"] for row in rows))
    summary.update(
        {
            "collected_cases": len(rows),
            "selected_cases": len(selected),
            "functions": len({row["function"] for row in rows}),
            "properties": sum(row["is_property"] for row in selected),
            "unexecuted_properties": unexecuted,
            "unmeasured_properties": [
                row["nodeid"] for row in selected if row["body_calls"] is None
            ],
            "body_calls": sum(row["body_calls"] or 0 for row in selected),
            "duration": sum(row["duration"] for row in selected),
        }
    )
    document = {
        "schema": "afairesi.tests",
        "schema_version": 1,
        "package": os.environ.get("AFAIRESI_TEST_PACKAGE"),
        "exit_code": int(exitstatus),
        "summary": summary,
        "tests": rows,
        "collection_errors": collection_errors,
    }
    report = Path(os.environ["AFAIRESI_TEST_REPORT"])
    report.write_text(json.dumps(document, indent=2) + "\n")
    if os.environ.get("AFAIRESI_MUTATION_REPORT") == "1":
        attribution = {
            "failed_tests": [
                row["nodeid"] for row in rows if row["outcome"] == "failed"
            ],
            "collection_errors": collection_errors,
        }
        sys.stdout.write("\nAFAIRESI_TEST_REPORT " + json.dumps(attribution) + "\n")

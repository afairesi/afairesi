# Copyright (c) 2026 VALAB/ITI
# ruff: noqa: S603, S607
"""Verify browser transport, directory scopes, and semantic package diffs."""

import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
from http import HTTPStatus
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import urlencode

import pytest
from canonical import (
    canonical_root,
    command_catalog,
    overview_data,
    resource_data,
    source_package_cli,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient

from packages.canonical_browser import main as app

TEST_MODIFIED_CHANGE = "modified"
TEST_PARSER_ERROR = 2
TEST_REPOSITORY_FIELDS = 3
TEST_ACTION_FAILURE = 7


def commit_sources(root: Path) -> None:
    """Record an isolated baseline without changing the user's Git configuration."""
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "baseline",
        ],
        check=True,
    )


def require_output(
    condition: bool,  # noqa: FBT001 - assertion helper
    message: str = "Unexpected output snapshot or report",
) -> None:
    """Fail a behavioral check with a readable explanation."""
    if not condition:
        raise AssertionError(message)


class TestOutputSnapshots(unittest.TestCase):
    """Exercise saved output across browser launches and explicit refreshes."""

    def setUp(self) -> None:
        """Create an isolated package with runtime output."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "flake.nix").write_text("{}\n")
        self.output = self.root / "packages/example/tmp"
        self.output.mkdir(parents=True)
        (self.output.parent / "main.py").write_text('"""Example."""\n')

    def capture(
        self,
        snapshots: app.OutputSnapshots,
        *,
        refresh: bool = False,
    ) -> app.OutputComparison:
        """Wait for one capture as the report page would."""
        data = app.gui_data(self.root)
        snapshots.observe(data, refresh=refresh)
        comparison = snapshots.comparisons[self.output]
        if comparison.future is None:
            msg = "Output capture was not scheduled"
            raise AssertionError(msg)
        comparison.future.result(timeout=20)
        return comparison

    def test_capture_preserves_symlinks_without_reading_external_targets(self) -> None:
        """Compare symlink destinations without exposing external file contents."""
        secret = self.root / "private.txt"
        secret.write_text("PRIVATE-CONTENT-MUST-NOT-APPEAR\n")
        link = self.output / "link"
        link.symlink_to(secret)
        with app.OutputSnapshots() as snapshots:
            first = self.capture(snapshots)
            require_output(first.error == "", first.error)
            if first.current is None:
                msg = "Expected a complete initial capture"
                raise AssertionError(msg)
            require_output((first.current / "output/link").is_symlink())
        link.unlink()
        link.symlink_to(self.root / "missing.txt")
        with app.OutputSnapshots() as snapshots:
            comparison = self.capture(snapshots)
            require_output(comparison.error == "", comparison.error)
            report = re.sub(rb"<[^>]+>", b"", app.output_diff_page(comparison))
            require_output(b"private.txt" in report)
            require_output(b"missing.txt" in report)
            require_output(b"PRIVATE-CONTENT-MUST-NOT-APPEAR" not in report)

    def test_comparison_failures_and_timeouts_are_visible_and_refresh_can_retry(
        self,
    ) -> None:
        """Show tool failures and timeouts while retaining captures for retry."""
        with app.OutputSnapshots() as snapshots:
            self.capture(snapshots)
        tools = self.root / "tools"
        tools.mkdir()
        executable = tools / "diffoscope"
        executable.write_text(
            f"#!{sys.executable}\nimport sys\n"
            "sys.stderr.write('<failed>')\nsys.exit(2)\n",
        )
        executable.chmod(0o700)
        with app.OutputSnapshots() as snapshots:
            with patch.dict(os.environ, {"PATH": f"{tools}:{os.environ['PATH']}"}):
                comparison = self.capture(snapshots)
                require_output("exit 2" in comparison.error)
                require_output(b"&lt;failed&gt;" in app.output_diff_page(comparison))
                previous = comparison.previous
                executable.write_text(
                    f"#!{sys.executable}\nimport time\ntime.sleep(10)\n",
                )
                with patch.object(app, "OUTPUT_DIFF_TIMEOUT", 0.05):
                    self.capture(snapshots, refresh=True)
                require_output("exceeded" in comparison.error)
                require_output(comparison.previous == previous)
            self.capture(snapshots, refresh=True)
            require_output(comparison.error == "", comparison.error)
            require_output(b"No output changes" in app.output_diff_page(comparison))

    def test_failed_copy_preserves_history_and_other_browsers_cannot_rotate_it(
        self,
    ) -> None:
        """Failed captures and concurrent browsers preserve the last complete output."""
        (self.output / "value.txt").write_text("before\n")
        with app.OutputSnapshots() as first, app.OutputSnapshots() as second:
            comparison = self.capture(first)
            latest = (comparison.store / "latest").read_text()
            concurrent = self.capture(second)
            require_output("another browser" in concurrent.error)
            require_output((comparison.store / "latest").read_text() == latest)
            with patch.object(shutil, "copytree", side_effect=OSError("copy failed")):
                self.capture(first, refresh=True)
            require_output("copy failed" in comparison.error)
            require_output((comparison.store / "latest").read_text() == latest)
            require_output(len(list(comparison.store.glob("capture-*"))) == 1)
            first.close()
            self.capture(second, refresh=True)
            require_output(concurrent.error == "", concurrent.error)
            require_output(not concurrent.changed)

    def test_launches_compare_output_and_refresh_keeps_previous_capture(self) -> None:
        """HTML reports compare two launches, retaining the same baseline on refresh."""
        value = self.output / "value.json"
        value.write_text('{"value": "before"}\n')
        removed = self.output / "removed.txt"
        removed.write_text("old output\n")
        archive = self.output / "archive.zip"
        with zipfile.ZipFile(archive, "w") as stream:
            stream.writestr("nested.txt", "old archive content\n")
        with app.OutputSnapshots() as snapshots:
            first = self.capture(snapshots)
            require_output(first.error == "", first.error)
            require_output(b"Initial capture saved" in app.output_diff_page(first))
            baseline = first.current
        value.write_text('{"value": "after"}\n')
        removed.unlink()
        (self.output / "added.txt").write_text("new output\n")
        with zipfile.ZipFile(archive, "w") as stream:
            stream.writestr("nested.txt", "new archive content\n")
        with app.OutputSnapshots() as snapshots:
            second = self.capture(snapshots)
            require_output(second.error == "", second.error)
            require_output(second.previous == baseline)
            require_output(second.changed)
            for text in (
                b"before",
                b"after",
                b"removed.txt",
                b"added.txt",
                b"nested.txt",
                b"Previous capture:",
                b"Current capture:",
            ):
                require_output(
                    text in re.sub(b"<[^>]+>", b"", app.output_diff_page(second)),
                )
            current = second.current
            value.write_text('{"value": "refreshed"}\n')
            self.capture(snapshots)
            require_output(second.current == current)
            self.capture(snapshots, refresh=True)
            require_output(second.previous == baseline)
            require_output(second.current != current)
            require_output(second.error == "", second.error)
            require_output(
                b"refreshed" in re.sub(b"<[^>]+>", b"", app.output_diff_page(second)),
            )
            require_output(
                set(second.store.glob("capture-*"))
                == {second.previous, second.current},
            )
            latest = second.current
        with app.OutputSnapshots() as snapshots:
            third = self.capture(snapshots)
            require_output(third.previous == latest)
            require_output(third.error == "", third.error)
            require_output(not third.changed)
            require_output(b"No output changes" in app.output_diff_page(third))

    def test_metadata_is_ignored_and_missing_output_reports_removals(self) -> None:
        """Ignore metadata changes and report the removal of a whole output tree."""
        value = self.output / "value.txt"
        value.write_text("retained contents\n")
        with app.OutputSnapshots() as snapshots:
            self.capture(snapshots)
        value.chmod(0o700)
        os.utime(value, (1, 1))
        with app.OutputSnapshots() as snapshots:
            comparison = self.capture(snapshots)
            require_output(comparison.error == "", comparison.error)
            require_output(not comparison.changed)
            value.unlink()
            self.output.rmdir()
            self.capture(snapshots, refresh=True)
            require_output(comparison.error == "", comparison.error)
            require_output(comparison.changed)
            require_output(b"retained" in app.output_diff_page(comparison))

    def test_output_tree_embeds_changes_and_reports_individual_entries(self) -> None:
        """Keep nested output changes inline and scope reports to their entry."""
        nested = self.output / "nested"
        nested.mkdir()
        changed = nested / "value.txt"
        changed.write_text("before\n")
        (self.output / "unchanged.txt").write_text("same\n")
        removed = self.output / "removed.txt"
        removed.write_text("removed\n")
        with app.OutputSnapshots() as snapshots:
            self.capture(snapshots)
        changed.write_text("after\n")
        removed.unlink()
        (self.output / "added.txt").write_text("added\n")
        (self.output / "binary").write_bytes(b"\0binary")
        (self.output / "external").symlink_to(self.root)
        with app.OutputSnapshots() as snapshots:
            comparison = self.capture(snapshots)
            data = app.gui_data(self.root)
            snapshots.observe(data)
            package = next(node for node in data["nodes"] if node["kind"] == "package")
            tree = next(
                child
                for child in package["tree"]["children"]
                if child["title"] == "tmp/"
            )
            require_output(tree["expandable"] and bool(tree["output_diff"]))
            entries = {child["title"]: child for child in tree["children"]}
            require_output(entries["removed.txt"]["change"] == "removed")
            require_output(entries["added.txt"]["change"] == "added")
            require_output(entries["unchanged.txt"]["output_diff"] is None)
            require_output(entries["binary"]["text_diff"] is None)
            require_output(not entries["external"]["children"])
            value = entries["nested/"]["children"][0]
            require_output(
                "-before" in value["text_diff"] and "+after" in value["text_diff"],
            )
            report = snapshots.entry_report(comparison, "nested/value.txt")
            if report.future is None:
                self.fail("Entry report was not scheduled")
            report.future.result(timeout=20)
            require_output(report.error == "", report.error)
            content = re.sub(b"<[^>]+>", b"", app.output_diff_page(report))
            require_output(b"before" in content and b"after" in content)
            require_output(b"unchanged.txt" not in content)
            with pytest.raises(ValueError, match="Invalid output entry"):
                snapshots.entry_report(comparison, "../timestamp")
            with pytest.raises(ValueError, match="escapes capture"):
                snapshots.entry_report(comparison, "external/flake.nix")

    def test_report_requests_reuse_captures_while_the_server_stays_responsive(
        self,
    ) -> None:
        """Reuse captures for reports and keep browsing during a comparison."""
        value = self.output / "value.txt"
        value.write_text("before\n")
        with app.OutputSnapshots() as snapshots:
            self.capture(snapshots)
        value.write_text("after\n")
        started = threading.Event()
        release = threading.Event()
        original = app.compare_output

        def delayed(previous: Path, current: Path) -> bool:
            started.set()
            if not release.wait(10):
                msg = "Comparison was not released"
                raise ValueError(msg)
            return original(previous, current)

        with (
            app.OutputSnapshots() as snapshots,
            patch.object(app, "OutputSnapshots", return_value=snapshots),
            patch.object(app, "compare_output", side_effect=delayed) as compare,
            TestClient(
                app.gui_app(self.root),
                base_url="http://127.0.0.1:8765",
            ) as client,
        ):
            try:
                require_output(started.wait(10))
                response = client.request("GET", "/api/overview")
                data = json.loads(response.content)
                package = next(
                    node for node in data["nodes"] if node["kind"] == "package"
                )
                route = package["output_diff"]
                comparison = snapshots.comparisons[self.output]
                for _ in range(2):
                    response = client.request("GET", route)
                    require_output(response.status_code == HTTPStatus.OK)
                    require_output(b"updates automatically" in response.content)
                response = client.request(
                    "GET",
                    "/output-diff?" + urlencode({"path": str(self.root)}),
                )
                require_output(response.status_code == HTTPStatus.NOT_FOUND)
                release.set()
                if comparison.future is not None:
                    comparison.future.result(timeout=20)
                response = client.request("GET", route)
                report = re.sub(rb"<[^>]+>", b"", response.content)
                require_output(b"before" in report)
                require_output(b"after" in report)
                compare.assert_called_once()
            finally:
                release.set()


class TestBoundary(unittest.TestCase):
    """Verify the source contract through complete browser snapshots."""

    def test_browser_cli_is_statically_discoverable(self) -> None:
        """The browser's own interface must remain visible to Canonical inspection."""
        entries = source_package_cli(Path(app.__file__).read_bytes(), "main.py")
        if not all(
            any(entry.text.startswith(name) for entry in entries)
            for name in ("directory", "--no-open", "--port")
        ):
            msg = "Canonical inspection must discover every browser CLI parameter"
            raise AssertionError(msg)

    def test_empty_home_and_host_only_flake_have_no_fabricated_packages(self) -> None:
        """Only backend resource identities may appear in the browser graph."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".gitignore").write_text("/*\n!/.gitignore\n!/.gitmodules\n")
            child = root / "forge.example"
            child.mkdir()
            if canonical_root(child) != root:
                msg = "Empty home layouts must share the backend's root recognition"
                raise AssertionError(msg)
            home = app.gui_data(root)
            (root / ".gitignore").unlink()
            (root / "flake.nix").write_text("{}\n")
            source = root / "hosts/laptop/configuration.nix"
            source.parent.mkdir(parents=True)
            source.write_text("{}\n")
            flake = app.gui_data(root)
        if [node["id"] for node in home["nodes"]] != [".:repository"]:
            msg = "An empty home must not fabricate a removed packages directory"
            raise AssertionError(msg)
        if any(node["kind"] == "package" for node in flake["nodes"]):
            msg = "Host-only flakes must not fabricate package resources"
            raise AssertionError(msg)

    def test_host_dependency_removals_and_deleted_hosts_and_checks_are_visible(
        self,
    ) -> None:
        """Historical source comparisons must cover every Canonical resource kind."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "flake.nix").write_text("{}\n")
            host = root / "hosts/laptop/configuration.nix"
            host.parent.mkdir(parents=True)
            host.write_text(
                "{ inputs, system, ... }: { environment.systemPackages = [ "
                "inputs.self.packages.${system}.tool ]; }\n",
            )
            check = root / "checks/laptopVmWithDisko/default.nix"
            check.parent.mkdir(parents=True)
            check.write_text("{}\n")
            commit_sources(root)
            host.write_text("{}\n")
            changed = app.gui_data(root)
            host.unlink()
            check.unlink()
            removed = app.gui_data(root)
        edge = next(edge for edge in changed["edges"] if edge["kind"] == "runtime")
        if (
            edge["change"] != "removed"
            or edge["declaration"]["path"] != "hosts/laptop/configuration.nix"
        ):
            msg = "Host dependency removals must retain their declaration metadata"
            raise AssertionError(msg)
        host_record = next(node for node in changed["nodes"] if node["kind"] == "host")
        if "- runtime: packages/tool" not in json.dumps(host_record["tree"]):
            msg = "Host dependency removals must appear in the source details"
            raise AssertionError(msg)
        records = {node["id"]: node for node in removed["nodes"]}
        for identifier in (".:hosts/laptop", ".:checks/laptopVmWithDisko"):
            if (
                not records[identifier]["removed"]
                or records[identifier]["tree"]["change"] != "removed"
            ):
                msg = (
                    "Deleted hosts and checks must remain visible as removed resources"
                )
                raise AssertionError(msg)
        host_tree = records[".:hosts/laptop"]["tree"]
        source = next(
            child
            for child in host_tree["children"]
            if child["title"] == "configuration.nix"
        )
        if not any(child["title"] == "Dependencies" for child in source["children"]):
            msg = "Removed host dependencies must retain their original source filename"
            raise AssertionError(msg)

    def test_multiline_help_changes_remain_under_the_source_file(self) -> None:
        """Documentation containing summary labels must retain every paragraph."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "flake.nix").write_text("{}\n")
            source = root / "packages/example/main.py"
            source.parent.mkdir(parents=True)
            before = (
                "First paragraph.\n\nDependencies:\n"
                "  Documentation, not declarations.\nOriginal last paragraph."
            )
            source.write_text(
                f'"""{before}"""\nraise RuntimeError("must not execute")\n',
            )
            commit_sources(root)
            after = before.replace("Original", "Changed")
            source.write_text(
                f'"""{after}"""\nraise RuntimeError("must not execute")\n',
            )
            snapshot = app.gui_data(root)
        package = next(node for node in snapshot["nodes"] if node["kind"] == "package")
        file = next(
            child
            for child in package["tree"]["children"]
            if child["title"] == "main.py"
        )
        documentation = [
            (child["title"], child["change"])
            for child in file["children"]
            if child["change"]
        ]
        if documentation != [("- " + before, "removed"), ("+ " + after, "added")]:
            msg = "Multiline documentation must be compared as a complete source fact"
            raise AssertionError(msg)
        if any(child["title"] == "Dependencies" for child in file["children"]):
            msg = "Documentation labels must not become declaration groups"
            raise AssertionError(msg)

    def test_prm_suppression_edits_and_deleted_sources_keep_changes(self) -> None:
        """Current and historical asset inventories must use the same source rules."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "flake.nix").write_text("{}\n")
            package = root / "packages/example"
            package.mkdir(parents=True)
            (package / "default.nix").write_text("{}\n")
            asset = package / "prm/nested/script.js"
            asset.parent.mkdir(parents=True)
            asset.write_text("/* eslint-disable no-alert */\n")
            commit_sources(root)
            asset.write_text(
                "/* eslint-disable no-alert */\n/* eslint-disable no-console */\n",
            )
            changed = app.gui_data(root)
            asset.unlink()
            removed = app.gui_data(root)
        for snapshot, transition, change in (
            (changed, "1 → 2", "modified"),
            (removed, "1 → 0", "removed"),
        ):
            package = next(
                node for node in snapshot["nodes"] if node["kind"] == "package"
            )
            source_node = next(
                child
                for child in package["tree"]["children"]
                if child["title"] == "prm/nested/script.js"
            )
            if not any(
                child["title"] == f"eslint-disable (global): {transition}"
                and child["change"] == change
                for child in source_node["children"]
            ):
                msg = (
                    "Tracked asset suppression changes must retain their source parents"
                )
                raise AssertionError(msg)
        if source_node["lines"] is not None:
            msg = "A removed source must not claim current line counts"
            raise AssertionError(msg)

    def test_schema_versions_are_checked_and_added_fields_are_allowed(self) -> None:
        """Reject incompatible documents before rendering, without mutating inputs."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "flake.nix").write_text("{}\n")
            current = overview_data(root)
            previous = overview_data(root, revision="HEAD")
            for version in (None, 0, 2, "1", True):
                with (
                    self.subTest(version=version),
                    patch.object(
                        app,
                        "overview_data",
                        return_value={**current, "schema_version": version},
                    ),
                    pytest.raises(
                        ValueError,
                        match="Unsupported Canonical overview schema",
                    ),
                ):
                    app.browser_snapshot(root)
            with (
                patch.object(
                    app,
                    "overview_data",
                    side_effect=[current, {**previous, "schema": "other.overview"}],
                ),
                pytest.raises(
                    ValueError,
                    match="Unsupported Canonical overview schema",
                ),
            ):
                app.browser_snapshot(root)
            current["future_field"] = {"optional": True}
            original = json.dumps(current)
            with patch.object(app, "overview_data", side_effect=[current, previous]):
                snapshot = app.browser_snapshot(root)
            if (
                snapshot["future_field"] != {"optional": True}
                or json.dumps(current) != original
            ):
                msg = "Optional fields must survive without modifying backend snapshots"
                raise AssertionError(msg)


class TestGui(unittest.TestCase):
    """Verify the read-only GUI transport and shared semantic model."""

    def test_canonical_commands_and_package_tests_preserve_cli_arguments(self) -> None:
        """Run every catalog entry and test action through Canonical without a shell."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "packages/example"
            package.mkdir(parents=True)
            executable = root / "canonical"
            executable.write_text(
                f"#!{sys.executable}\n"
                "import json, os, sys, time\n"
                "value = {'argv': sys.argv[1:], 'cwd': os.getcwd()}\n"
                "print(json.dumps(value), flush=True)\n"
                "if '--wait' in sys.argv: time.sleep(60)\n"
                "sys.exit(7 if '--fail' in sys.argv else 0)\n",
            )
            executable.chmod(0o700)
            data: dict[str, Any] = {
                "root": str(root),
                "nodes": [
                    {
                        "kind": "package",
                        "directory": str(root),
                        "path": "packages/example",
                        "name": "example",
                    },
                ],
            }
            commands = app.CanonicalActions()
            actions = app.PackageActions()
            self.addCleanup(commands.close)
            self.addCleanup(actions.close)
            commands.observe(data)
            actions.observe(data)
            with patch.dict(
                os.environ,
                {"PATH": str(root) + os.pathsep + os.environ["PATH"]},
            ):
                for entry in data["commands"]:
                    command = entry["command"]
                    commands.start(
                        str(root),
                        command,
                        "'two words' '$(touch injected)'",
                    )
                    commands.jobs[str(root)].process.wait(timeout=10)
                    state = commands.status(str(root))
                    invocation = json.loads(state["output"])
                    require_output(state["state"] == "passed")
                    require_output(invocation["cwd"] == str(root))
                    require_output(
                        invocation["argv"]
                        == [
                            *command.split(),
                            "two words",
                            "$(touch injected)",
                        ],
                    )
                    if command.startswith("test "):
                        actions.start(str(package), command, "--timeout 12")
                        actions.jobs[str(package)].process.wait(timeout=10)
                        invocation = json.loads(actions.status(str(package))["output"])
                        require_output(invocation["cwd"] == str(package))
                        require_output(
                            invocation["argv"] == [*command.split(), "--timeout", "12"],
                        )
                require_output(not (root / "injected").exists())
                commands.start(str(root), "test mutation", "--fail")
                commands.jobs[str(root)].process.wait(timeout=10)
                require_output(
                    commands.status(str(root))["exit_code"] == TEST_ACTION_FAILURE,
                )
                commands.start(str(root), "test hypothesis", "--wait")
                with pytest.raises(ValueError, match="already running"):
                    commands.start(str(root), "overview")
                commands.start(str(root), "stop")
                require_output(commands.status(str(root))["state"] == "stopped")
            with pytest.raises(ValueError, match="Unknown directory"):
                commands.start(str(package), "overview")
            with pytest.raises(ValueError, match="Unknown Canonical command"):
                commands.start(str(root), "not-a-command")
            with pytest.raises(ValueError, match="No closing quotation"):
                commands.start(str(root), "overview", "'")

    def test_command_transport_rejects_cross_site_requests_and_unknown_directories(
        self,
    ) -> None:
        """Protect command execution and publish the same catalog over HTTP."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with TestClient(
                app.gui_app(root),
                base_url="http://127.0.0.1:8765",
            ) as client:
                response = client.request("GET", "/api/overview")
                data = json.loads(response.content)
                require_output(data["commands"] == command_catalog())
                body = json.dumps({"directory": str(root), "action": "overview"})
                response = client.request(
                    "POST",
                    "/api/command",
                    content=body,
                    headers={
                        "Content-Type": "application/json",
                        "Origin": "https://example.com",
                    },
                )
                require_output(response.status_code == HTTPStatus.FORBIDDEN)
                for invalid_body in (
                    "{",
                    "null",
                    "[]",
                    json.dumps({"directory": str(root), "args": 42}),
                    json.dumps({"directory": str(root), "unexpected": "value"}),
                    " " * (app.ACTION_BODY_LIMIT + 1),
                ):
                    response = client.post(
                        "/api/command",
                        content=invalid_body,
                        headers={"Content-Type": "application/json"},
                    )
                    require_output(response.status_code == HTTPStatus.BAD_REQUEST)
                response = client.request(
                    "POST",
                    "/api/command",
                    content=json.dumps(
                        {"directory": str(root / "unknown"), "action": "overview"},
                    ),
                    headers={"Content-Type": "application/json"},
                )
                require_output(response.status_code == HTTPStatus.BAD_REQUEST)
                require_output(
                    "Unknown directory" in json.loads(response.content)["error"],
                )
                response = client.request(
                    "GET",
                    "/api/command?" + urlencode({"directory": str(root)}),
                )
                require_output(json.loads(response.content)["state"] == "idle")

    def test_directory_scope_excludes_sibling_resources(self) -> None:
        """Intermediate directories and package directories show only their contents."""
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            (home / ".gitmodules").write_text(
                '[submodule "github.com/example/one"]\n'
                "path = github.com/example/one\n"
                '[submodule "other.example/two"]\n'
                "path = other.example/two\n",
            )
            for relative in ("github.com/example/one", "other.example/two"):
                repository = home / relative
                repository.mkdir(parents=True)
                (repository / "flake.nix").write_text("{}\n")
                for name in ("first", "second"):
                    package = repository / "packages" / name
                    package.mkdir(parents=True)
                    (package / "default.nix").write_text("{}\n")
                    (package / "main.py").write_text('"""Example."""\n')
            scoped = home / "github.com"
            viewer = app.RepositoryBrowser(scoped)
            viewer.refresh()
            snapshot = viewer.snapshot
            if snapshot["root"] != str(scoped) or snapshot["parent"] != str(home):
                msg = "The current directory and its parent must define navigation"
                raise AssertionError(msg)
            repositories = {node["repository"] for node in snapshot["nodes"]}
            if repositories != {"example/one"}:
                msg = "A directory scope must exclude sibling repositories"
                raise AssertionError(msg)
            package = scoped / "example/one/packages/first"
            snapshot = app.gui_data(package)
            names = [
                node["name"] for node in snapshot["nodes"] if node["kind"] == "package"
            ]
            if names != ["first"]:
                msg = "Starting inside a package must exclude other packages"
                raise AssertionError(msg)

    def test_directory_scopes_reuse_data_until_refresh(self) -> None:
        """Changing scopes must preserve cached declarations until explicit refresh."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "flake.nix").write_text("{}\n")
            package = root / "packages/example"
            package.mkdir(parents=True)
            source = package / "main.py"
            source.write_text('"""Before."""\n')
            browser = app.RepositoryBrowser(root)
            browser.load()
            before = browser.snapshot
            source.write_text('"""After."""\n')
            browser.cwd = package
            browser.load()
            if "Before." not in json.dumps(browser.snapshot):
                msg = "Child navigation must reuse the cached snapshot"
                raise AssertionError(msg)
            browser.cwd = root
            browser.load()
            if browser.snapshot != before:
                msg = "Parent navigation must reuse the cached snapshot"
                raise AssertionError(msg)
            browser.refresh()
            if "After." not in json.dumps(browser.snapshot):
                msg = "Refresh must load new declarations"
                raise AssertionError(msg)

    def test_gui_package_includes_offline_layout_engine(self) -> None:
        """The installed app must include the same engine served by source tests."""
        executable = os.environ.get("PACKAGE_E2E_EXECUTABLE")
        if not executable:
            self.skipTest("Nix package executable not supplied")
        version = f"python{sys.version_info.major}.{sys.version_info.minor}"
        for name in ("g6",):
            engine = (
                Path(executable).parent.parent
                / "lib"
                / version
                / f"site-packages/canonical_browser/prm/{name}.js"
            )
            if (
                engine.read_bytes()
                != Path(
                    os.environ[f"CANONICAL_BROWSER_{name.upper()}"],
                ).read_bytes()
            ):
                msg = "The packaged graph engine must match the pinned Nix dependency"
                raise AssertionError(msg)

    def test_gui_preserves_shared_resource_details_and_changes(self) -> None:
        """Both representations use the same resources and structured changes."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "flake.nix").write_text("{}\n")
            for relative, source in (
                ("packages/sample/default.nix", "{}\n"),
                ("packages/sample/test_main.py", "def test_old(): pass\n"),
                ("hosts/laptop/configuration.nix", "{}\n"),
                ("checks/sample/default.nix", "{}\n"),
            ):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(source)
            commit_sources(root)
            (root / "packages/sample/test_main.py").write_text("def test_new(): pass\n")
            snapshot = app.gui_data(root)
            details = resource_data(root / "packages/sample")
        records = {record["id"]: record for record in snapshot["nodes"]}
        package = records[".:packages/sample"]
        require_output(package["details"] == details)
        require_output("tree" not in snapshot)
        source = next(
            node
            for node in package["tree"]["children"]
            if node["title"] == "test_main.py"
        )
        tests = next(node for node in source["children"] if node["field"] == "tests")
        require_output(
            [(node["title"], node["change"]) for node in tests["children"]]
            == [("- test old", "removed"), ("+ test new", "added")],
        )

    def test_gui_serves_assets_and_live_data_without_exposing_checkout(self) -> None:
        """Assets ship with the package; traversal and writes cannot reach files."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with TestClient(
                app.gui_app(root),
                base_url="http://127.0.0.1:8765",
            ) as client:
                for route in (
                    "/",
                    "/script.js",
                    "/style.css",
                    "/g6.js",
                    "/icons.js",
                ):
                    response = client.request("GET", route)
                    if response.status_code != HTTPStatus.OK or not response.content:
                        msg = f"Missing packaged GUI asset: {route}"
                        raise AssertionError(msg)
                response = client.get("/api/overview")
                require_output(response.status_code == HTTPStatus.OK)
                require_output(response.json()["root"] == str(root))
                for route in ("/../main.py", "/.git/config", "/main.py"):
                    response = client.request("GET", route)
                    if response.status_code != HTTPStatus.NOT_FOUND:
                        msg = "The GUI must not serve checkout files"
                        raise AssertionError(msg)
                response = client.request("POST", "/api/overview", content=b"change")
                if response.status_code != HTTPStatus.METHOD_NOT_ALLOWED:
                    msg = "GUI requests must not write repository data"
                    raise AssertionError(msg)
                with patch.object(
                    app,
                    "gui_data",
                    side_effect=ValueError("Invalid repository"),
                ):
                    response = client.request("GET", "/api/overview")
                    if (
                        response.status_code != HTTPStatus.INTERNAL_SERVER_ERROR
                        or json.loads(response.content)
                        != {"error": "Invalid repository"}
                    ):
                        msg = "Repository errors must return readable JSON"
                        raise AssertionError(msg)

    def test_home_is_the_upper_navigation_boundary(self) -> None:
        """Home scopes have no parent, including a home with a flake."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(Path, "home", return_value=root):
                if app.directory_snapshot(root)["parent"] is not None:
                    msg = "Directory home must have no parent"
                    raise AssertionError(msg)
                (root / "flake.nix").write_text("{}\n")
                if app.gui_data(root)["parent"] is not None:
                    msg = "Flake home must have no parent"
                    raise AssertionError(msg)

    def test_output_browser_serves_files_inside_tmp_only(self) -> None:
        """List output, escape filenames, and reject traversal and symlink escapes."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "packages/example/tmp"
            nested = output / "nested"
            nested.mkdir(parents=True)
            name = "report & <one>.txt"
            (output / name).write_text("Generated output")
            source = output.parent / "main.py"
            source.write_text("Source must stay private")
            (output / "escaped.py").symlink_to(source)
            with TestClient(
                app.gui_app(root),
                base_url="http://127.0.0.1:8765",
            ) as client:
                response = client.request(
                    "GET",
                    "/output?" + urlencode({"path": str(output)}),
                )
                listing = response.content.decode()
                if (
                    response.status_code != HTTPStatus.OK
                    or "report &amp; &lt;one&gt;.txt" not in listing
                    or "escaped.py" in listing
                ):
                    msg = "Output listings must escape names and omit escaped links"
                    raise AssertionError(msg)
                response = client.request(
                    "GET",
                    "/output?" + urlencode({"path": str(output / name)}),
                )
                if (
                    response.status_code != HTTPStatus.OK
                    or response.content != b"Generated output"
                ):
                    msg = "Output files must open directly in the web browser"
                    raise AssertionError(msg)
                route = "/output?" + urlencode({"path": str(output / name)})
                response = client.head(route)
                require_output(response.status_code == HTTPStatus.OK)
                require_output(response.content == b"")
                require_output(
                    int(response.headers["Content-Length"]) == len(b"Generated output"),
                )
                response = client.get(route, headers={"Range": "bytes=0-8"})
                require_output(response.status_code == HTTPStatus.PARTIAL_CONTENT)
                require_output(response.content == b"Generated")
                response = client.request(
                    "GET",
                    "/output?" + urlencode({"path": str(nested)}),
                )
                if (
                    response.status_code != HTTPStatus.OK
                    or "../" not in response.content.decode()
                ):
                    msg = "Nested output listings must link to their parent"
                    raise AssertionError(msg)
                for path in (
                    source,
                    output / ".." / "main.py",
                    output / "escaped.py",
                ):
                    response = client.request(
                        "GET",
                        "/output?" + urlencode({"path": str(path)}),
                    )
                    if response.status_code != HTTPStatus.NOT_FOUND:
                        msg = "Output browsing must not expose source files"
                        raise AssertionError(msg)

    def test_package_actions_run_checks_arguments_failures_and_stop(self) -> None:
        """Forward run arguments, execute checks and stop commands on close."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "packages/example"
            package.mkdir(parents=True)
            executable = root / "nix"
            executable.write_text(
                f"#!{sys.executable}\n"
                "import json, os, sys, time\n"
                "value = {'argv': sys.argv[1:], 'cwd': os.getcwd()}\n"
                "print(json.dumps(value), flush=True)\n"
                "if '--wait' in sys.argv: time.sleep(60)\n"
                "sys.exit(7 if '--fail' in sys.argv else 0)\n",
            )
            executable.chmod(0o700)
            data: dict[str, Any] = {
                "nodes": [
                    {
                        "kind": "package",
                        "directory": str(root),
                        "path": "packages/example",
                        "name": "example",
                        "package_type": "html",
                    },
                ],
            }
            actions = app.PackageActions()
            self.addCleanup(actions.close)
            with patch.dict(
                os.environ,
                {"PATH": str(root) + os.pathsep + os.environ["PATH"]},
            ):
                actions.observe(data)
                require_output(not data["nodes"][0]["actions"]["check"])
                with pytest.raises(ValueError, match="no declared check"):
                    actions.start(str(package), "check")
                check = root / "checks/example"
                check.mkdir(parents=True)
                (check / "default.nix").write_text("{}\n")
                actions.observe(data)
                actions.start(str(package), "check")
                actions.jobs[str(package)].process.wait(timeout=10)
                state = actions.status(str(package))
                command = json.loads(state["output"])
                require_output(state["state"] == "passed")
                require_output(
                    command["argv"][:3] == ["build", "--no-link", "--print-build-logs"],
                )
                require_output(
                    "#checks." in command["argv"][3]
                    and command["argv"][3].endswith('."example"'),
                )
                actions.start(
                    str(package),
                    "run",
                    "'two words' '$(touch injected)' --fail",
                )
                actions.jobs[str(package)].process.wait(timeout=10)
                state = actions.status(str(package))
                command = json.loads(state["output"])
                require_output(
                    state["state"] == "failed"
                    and state["exit_code"] == TEST_ACTION_FAILURE,
                )
                require_output(command["cwd"] == str(package))
                require_output(
                    command["argv"][:3] == ["run", f"{root}#example", "--"],
                )
                require_output(
                    command["argv"][-3:]
                    == ["two words", "$(touch injected)", "--fail"],
                )
                require_output(not (package / "injected").exists())
                data["nodes"][0]["package_type"] = "python"
                actions.observe(data)
                actions.start(str(package), "run", "--wait")
                with pytest.raises(ValueError, match="already running"):
                    actions.start(str(package), "run")
                actions.start(str(package), "stop")
                require_output(actions.status(str(package))["state"] == "stopped")
                actions.start(str(package), "run", "--wait")
                job = actions.jobs[str(package)]
                actions.close()
                require_output(job.process.poll() is not None)
            with pytest.raises(ValueError, match="Unknown package"):
                actions.start(str(root), "run")

    def test_package_sources_count_lines_and_exclude_runtime_output(self) -> None:
        """Count blank lines and unterminated last lines, including prm sources."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "flake.nix").write_text("{}\n")
            package = root / "packages/example"
            package.mkdir(parents=True)
            (package / "default.nix").write_bytes(b"{}\n")
            (package / "main.py").write_bytes(b'"""Help."""\r\n\r\nvalue = 1')
            (package / "test_main.py").write_bytes(b"")
            resources = package / "prm/web"
            resources.mkdir(parents=True)
            (resources / "script.js").write_bytes(
                b"// eslint-disable-next-line\n\nrun();\n",
            )
            (resources / "picture.png").write_bytes(b"binary\n")
            (resources / "linked.py").symlink_to(package / "main.py")
            (package / "tmp").mkdir()
            (package / "tmp/generated.py").write_bytes(b"runtime\n")
            snapshot = app.gui_data(root)
            record = next(
                node for node in snapshot["nodes"] if node["kind"] == "package"
            )
            sources = {
                child["title"]: child["lines"]
                for child in record["tree"]["children"]
                if child["source_file"]
            }
            if sources != {
                "default.nix": 1,
                "main.py": 3,
                "prm/web/script.js": 3,
                "test_main.py": 0,
            }:
                msg = "Source counts must exclude output, binary assets, and links"
                raise AssertionError(msg)
            if record["source_metrics"] != {
                "lines": {
                    "default.nix": 1,
                    "main.py": 3,
                    "test_main.py": 0,
                },
                "suppressions": {},
            }:
                msg = "Metrics must exclude prm sources and zero suppression counts"
                raise AssertionError(msg)
            if record["details"] != resource_data(package):
                msg = "Source line counts must match current file contents"
                raise AssertionError(msg)
            host = root / "hosts/example"
            host.mkdir(parents=True)
            (host / "configuration.nix").write_text("{}\n")
            host_record = next(
                node for node in app.gui_data(root)["nodes"] if node["kind"] == "host"
            )
            if host_record["source_metrics"]["lines"] != {"configuration.nix": 1}:
                msg = "Host source files must contribute current metrics"
                raise AssertionError(msg)

    def test_removed_dependencies_share_connections_and_change_colors(self) -> None:
        """Retain removed providers and declaration metadata in both views."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "flake.nix").write_text("{}\n")
            package = root / "packages/consumer"
            package.mkdir(parents=True)
            source = package / "default.nix"
            source.write_text(
                "{ inputs, system, ... }: { buildInputs = [ "
                "inputs.self.packages.${system}.old ]; }\n",
            )
            commit_sources(root)
            source.write_text("{}\n")
            snapshot = app.gui_data(root)
        records = {record["id"]: record for record in snapshot["nodes"]}
        if not records[".:packages/old"]["removed"]:
            msg = "Removed dependency providers must remain in the shared snapshot"
            raise AssertionError(msg)
        connections = records[".:packages/consumer"]["tree"]["children"][-1]
        if connections["children"][0]["change"] != "removed":
            msg = "Connections must retain the same change colors as GUI arrows"
            raise AssertionError(msg)
        edge = next(edge for edge in snapshot["edges"] if edge["kind"] == "build")
        if (
            edge["change"] != "removed"
            or edge["declaration"]["path"] != "packages/consumer/default.nix"
        ):
            msg = "Removed arrows must preserve their source declarations"
            raise AssertionError(msg)

    def test_runtime_os_and_persistence_are_observations(self) -> None:
        """Detect running modules and report uncertainty about disk-backed roots."""
        with tempfile.TemporaryDirectory() as directory:
            system = Path(directory)
            units = system / "etc/systemd/system"
            units.mkdir(parents=True)
            mounts = system / "mounts"
            mounts.write_text("tmpfs / tmpfs rw 0 0\n/dev/home /home ext4 rw 0 0\n")
            (units / "preservation.target").write_text("[Unit]\n")
            (units / "persist-files.service").write_text(
                "ExecStart=/nix/store/example-impermanence-mount-file\n",
            )
            with patch.object(
                platform,
                "freedesktop_os_release",
                return_value={"ID": "nixos", "PRETTY_NAME": "NixOS test"},
            ):
                record = app.machine_resource(system, mounts)
                if (
                    record["icon"] != "nixos"
                    or record["details"][0] != "OS: NixOS test"
                ):
                    msg = (
                        "The running OS must be read independently of host declarations"
                    )
                    raise AssertionError(msg)
                for expected in (
                    "Root: ephemeral (tmpfs)",
                    "Preservation: detected in running system",
                    "Impermanence: detected in running system",
                    "Filesystem /home: ext4",
                ):
                    if expected not in record["details"]:
                        raise AssertionError(expected)
                (units / "preservation.target").unlink()
                (units / "persist-files.service").unlink()
                mounts.write_text("/dev/root / btrfs rw 0 0\n")
                details = app.machine_resource(system, mounts)["details"]
                if "Root: btrfs; reset on reboot not established" not in details:
                    msg = "Disk-backed roots may still be reset on reboot"
                    raise AssertionError(msg)
                if "Preservation: not detected" not in details:
                    msg = "Missing evidence must be reported as not detected"
                    raise AssertionError(msg)

    def test_storage_counts_disk_blocks_and_links_existing_output(self) -> None:
        """Count prm allocation once per inode and preserve the runtime link."""
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / "packages/example"
            resources = package / "prm"
            resources.mkdir(parents=True)
            original = resources / "asset.bin"
            original.write_bytes(b"asset" * 1024)
            (resources / "hardlink.bin").hardlink_to(original)
            external = Path(temporary) / "external.bin"
            external.write_bytes(b"outside" * 8192)
            link = resources / "link.bin"
            link.symlink_to(external)
            expected = sum(
                path.lstat().st_blocks * 512 for path in (resources, original, link)
            )
            if app.directory_disk_size(resources) != expected:
                msg = "Disk usage must deduplicate hardlinks and ignore symlink targets"
                raise AssertionError(msg)
            output = package / "tmp"
            output.mkdir()
            storage = app.package_storage(package)
            if [node.title for node in storage] != [
                f"prm/: {app.format_bytes(expected)}",
                "tmp/",
            ] or storage[-1].directory != output.resolve():
                msg = "Packages must show allocated prm size and existing tmp link"
                raise AssertionError(msg)
            output.rmdir()
            if any(node.directory for node in app.package_storage(package)):
                msg = "An absent output directory must not have a link"
                raise AssertionError(msg)

    def test_web_browser_launch_and_port_validation(self) -> None:
        """Startup opens a loopback browser with the requested port."""
        with (
            patch.object(sys.stdin, "isatty", return_value=False),
            patch.object(app, "open_gui") as launch,
        ):
            app.main(["--no-open", "--port", "0"])
        launch.assert_called_once_with(
            Path.cwd().resolve(),
            port=0,
            open_browser=False,
        )
        with pytest.raises(SystemExit) as error:
            app.main(["--port", "-1"])
        if error.value.code != TEST_PARSER_ERROR:
            msg = "Invalid ports must be rejected by the parser"
            raise AssertionError(msg)


class TestCli(unittest.TestCase):
    """Verify web-only startup."""

    def test_default_launch_opens_web_browser(self) -> None:
        """The browser starts from cwd without a GUI mode flag."""
        with patch.object(app, "open_gui") as launch:
            app.main([])
        launch.assert_called_once_with(
            Path.cwd().resolve(),
            port=8765,
            open_browser=True,
        )

    def test_launch_address_accepts_connections_before_opening_browser(self) -> None:
        """An ephemeral listening address must work while the app starts."""
        addresses = []

        def open_browser(address: str) -> None:
            port = int(address.rsplit(":", 1)[1])
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                addresses.append(address)

        with (
            patch.object(app, "gui_app", return_value=FastAPI()),
            patch("uvicorn.Server.run"),
            patch("webbrowser.open", side_effect=open_browser),
        ):
            app.open_gui(Path.cwd(), port=0, open_browser=True)
        require_output(len(addresses) == 1)

    def test_removed_terminal_flag_is_rejected(self) -> None:
        """Legacy renderer selection cannot select a removed implementation."""
        with pytest.raises(SystemExit) as error:
            app.main(["--gui"])
        if error.value.code != TEST_PARSER_ERROR:
            msg = "Removed mode flags must be rejected"
            raise AssertionError(msg)


class TestRepositoryData(unittest.TestCase):
    """Exercise file-organized details through the browser's production data API."""

    def setUp(self) -> None:
        """Create a disposable flake with a conventional Python package."""
        storage = tempfile.TemporaryDirectory()
        self.addCleanup(storage.cleanup)
        self.root = Path(storage.name)
        (self.root / "flake.nix").write_text("{}\n")
        self.package = self.root / "packages/sample"
        self.package.mkdir(parents=True)
        (self.package / "default.nix").write_text(
            '{ pkgs, ... }: { meta.description = "Sample."; }\n',
        )
        self.source = self.package / "main.py"
        self.source.write_text(
            '"""Example."""\nimport argparse\ndef parser():\n'
            "    p = argparse.ArgumentParser()\n"
            '    p.add_argument("--value", help="Value")\n    return p\n',
        )
        (self.package / "test_main.py").write_text("def test_one(): pass\n")

    def record(self) -> dict[str, Any]:
        """Read the package through the same graph pipeline as HTTP requests."""
        return next(
            node
            for node in app.gui_data(self.root)["nodes"]
            if node["id"] == ".:packages/sample"
        )

    def files(self) -> dict[str, Any]:
        """Select the file rows without interpreting their display labels."""
        return {
            node["title"]: node
            for node in self.record()["tree"]["children"]
            if node["source_file"]
        }

    def test_cli_parameters_belong_to_nested_commands(self) -> None:
        """Command paths place changed parameters under their declared commands."""
        before = (
            "import argparse\ndef parser():\n    p = argparse.ArgumentParser()\n"
            '    p.add_argument("--root")\n    commands = p.add_subparsers()\n'
            '    run = commands.add_parser("run")\n'
            '    run.add_argument("--old")\n    return p\n'
        )
        self.source.write_text(before)
        commit_sources(self.root)
        self.source.write_text(before.replace('"--old"', '"--new"'))
        cli = next(
            node
            for node in self.files()["main.py"]["children"]
            if node["field"] == "cli"
        )
        run = next(node for node in cli["children"] if node["title"] == "run")
        require_output(
            [node["change"] for node in run["children"]] == ["removed", "added"],
        )
        require_output(
            any(
                node["title"].startswith("--root") and node["change"] is None
                for node in cli["children"]
            ),
        )

    def test_dependencies_appear_in_shared_overview_and_inline_changes(self) -> None:
        """Dependency rows and graph relationships reflect the same declarations."""
        default = self.package / "default.nix"
        default.write_text(
            "{ inputs, ... }: { buildInputs = [ "
            "inputs.self.packages.x86_64-linux.first ]; }\n",
        )
        commit_sources(self.root)
        default.write_text(
            "{ inputs, ... }: { buildInputs = [ "
            "inputs.self.packages.x86_64-linux.second ]; }\n",
        )
        group = next(
            node
            for node in self.files()["default.nix"]["children"]
            if node["field"] == "dependencies"
        )
        require_output(
            [(node["title"], node["change"]) for node in group["children"]]
            == [
                ("- build: packages/first", "removed"),
                ("+ build: packages/second", "added"),
            ],
        )

    def test_display_labels_do_not_control_file_placement(self) -> None:
        """Keep documentation independent of display labels."""
        text = "Suppressions\nLines: 999\nArguments\nHelp: arbitrary text"
        self.source.write_text(repr(text) + "\n")
        documentation = self.files()["main.py"]["children"][0]
        require_output(documentation["field"] == "help")
        require_output(documentation["value"] == text)
        require_output(self.files()["main.py"]["lines"] == 1)

    def test_empty_groups_are_hidden_but_diagnostics_and_removals_remain(self) -> None:
        """Empty facts have no placeholder rows, while diagnostics remain visible."""
        self.source.write_text('"""Library."""\n')
        (self.package / "test_main.py").write_text("")
        require_output(
            not any(
                node["field"] == "tests"
                for node in self.files()["test_main.py"]["children"]
            ),
        )
        (self.package / "test_main.py").write_text("def broken(\n")
        tests = self.files()["test_main.py"]["children"][0]
        require_output(tests["warning"] and tests["children"][0]["warning"])
        require_output(self.record()["tree"]["warning"])

    def test_high_level_diff_compares_summaries_not_source_code(self) -> None:
        """Body-only edits do not introduce semantic changes."""
        commit_sources(self.root)
        self.source.write_text(self.source.read_text() + "value = 42\n")
        record = self.record()

        def changed(node: dict[str, Any]) -> bool:
            return bool(node["change"]) or any(
                changed(child) for child in node["children"]
            )

        require_output(not changed(record["tree"]))

    def test_http_navigation_reuses_source_snapshot_until_refresh(self) -> None:
        """Navigation scopes one source capture; refresh observes source edits."""
        self.source.write_text('"""Before."""\n')
        with (
            patch.object(
                app,
                "browser_snapshot",
                wraps=app.browser_snapshot,
            ) as collect,
            TestClient(
                app.gui_app(self.root),
                base_url="http://127.0.0.1:8765",
            ) as client,
        ):
            self.source.write_text('"""After."""\n')
            for query in ({}, {"directory": str(self.package)}, {}):
                response = client.get("/api/overview", params=query)
                require_output(response.status_code == HTTPStatus.OK)
                package = next(
                    node
                    for node in response.json()["nodes"]
                    if node["kind"] == "package"
                )
                require_output(package["details"]["help"] == "Before.")
            collect.assert_called_once_with(self.root)
            refreshed = client.get("/api/overview", params={"refresh": "1"})
            package = next(
                node for node in refreshed.json()["nodes"] if node["kind"] == "package"
            )
            require_output(package["details"]["help"] == "After.")
            require_output(
                [invocation.args for invocation in collect.call_args_list]
                == [(self.root,), (self.root,)],
            )

    def test_metadata_changes_have_explicit_fields_and_values(self) -> None:
        """Changed metadata carries raw values independently of its display title."""
        commit_sources(self.root)
        (self.package / "default.nix").write_text(
            '{ ... }: { meta.description = "Updated."; }\n',
        )
        rows = [
            node
            for node in self.record()["tree"]["children"]
            if node["field"] == "description"
        ]
        require_output(
            [(node["value"], node["change"]) for node in rows]
            == [("Sample.", "removed"), ("Updated.", "added")],
        )

    def test_package_data_uses_canonical_test_discovery(self) -> None:
        """Browser test rows come from the shared static discovery contract."""
        (self.package / "test_main.py").write_text(
            "import unittest as unit\nclass Checks(unit.TestCase):\n"
            "    def test_nested_case(self): pass\ndef test_free_case(): pass\n",
        )
        record = self.record()
        require_output(record["details"] == resource_data(self.package))
        require_output(
            record["details"]["tests"] == ["test nested case", "test free case"],
        )

    def test_package_details_belong_to_their_source_files(self) -> None:
        """Keep documentation, interfaces and tests under their actual source files."""
        files = self.files()
        main = files["main.py"]
        require_output(main["lines"] == len(self.source.read_text().splitlines()))
        require_output([node["field"] for node in main["children"]] == ["help", "cli"])
        require_output(main["children"][0]["value"] == "Example.")
        require_output(files["test_main.py"]["children"][0]["field"] == "tests")
        require_output(self.record()["details"] == resource_data(self.package))

    def test_removed_source_retains_declarations_without_current_line_counts(
        self,
    ) -> None:
        """Historical declarations remain visible without inventing current metrics."""
        commit_sources(self.root)
        self.source.unlink()
        main = self.files()["main.py"]
        require_output(main["lines"] is None)
        require_output(main["children"][0]["change"] == "removed")

    def test_resolved_cli_diagnostic_does_not_warn_in_inline_history(self) -> None:
        """A corrected interface clears warnings even when historical parsing failed."""
        valid = self.source.read_text()
        self.source.write_text("def main(): pass\n")
        commit_sources(self.root)
        self.source.write_text(valid)
        require_output(not self.record()["tree"]["warning"])

    def test_source_assets_keep_per_file_suppressions(self) -> None:
        """Tracked source assets retain metrics outside aggregate root-source counts."""
        resources = self.package / "prm"
        resources.mkdir()
        contents = "// eslint-disable-next-line\nrun();\n"
        (resources / "script.js").write_text(contents)
        record = self.record()
        require_output("prm/script.js" not in record["source_metrics"]["lines"])
        script = self.files()["prm/script.js"]
        require_output(script["lines"] == len(contents.splitlines()))
        require_output(script["children"][0]["field"] == "suppressions")

    def test_suppression_diff_changes_counts_without_repeating_labels(self) -> None:
        """Suppression changes are one structured row per source, kind and scope."""
        self.source.write_text(self.source.read_text() + "# noqa\n")
        commit_sources(self.root)
        self.source.write_text(
            self.source.read_text() + "# noqa: F401\nvalue = '# noqa'\n",
        )
        rows = [
            node
            for node in self.files()["main.py"]["children"]
            if node["field"] == "suppressions"
        ]
        require_output(
            [(node["title"], node["change"]) for node in rows]
            == [("noqa (local): 1 → 2", "modified")],
        )

    def test_unavailable_cli_summary_warns_on_collapsed_package(self) -> None:
        """Parser diagnostics propagate to source and package rows."""
        self.source.write_text("def main(): pass\n")
        record = self.record()
        require_output(record["tree"]["warning"])
        require_output(self.files()["main.py"]["warning"])
        cli = next(
            node
            for node in self.files()["main.py"]["children"]
            if node["field"] == "cli"
        )
        require_output(cli["children"][0]["warning"])

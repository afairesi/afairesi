# Copyright (c) 2026 VALAB/ITI
# ruff: noqa: S603, S607
"""Verify package browsing, summary diffs, and terminal navigation."""

import curses
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from perigrafo import CliEntry

from packages.perigrafo_browser import main as app

TEST_MODIFIED_STYLE = 33
TEST_EXPECTED_BUILDS = 2
TEST_PAGE_HEIGHT = 2
TEST_EXPECTED_VISIBLE = 2
TEST_PARSER_ERROR = 2
TEST_REPOSITORY_FIELDS = 2


class TestBrowserData(unittest.TestCase):
    """Verify the browser resource model and semantic changes."""

    def test_browser_preserves_packages_and_hosts_and_hides_checks(self) -> None:
        """Show packages, hosts, and semantic changes while hiding checks."""
        root = Path("/workspace")
        nodes = [
            app.TreeNode(
                "packages/sample",
                [
                    app.TreeNode(
                        "Tests",
                        [
                            app.TreeNode("+ test new", style=32),
                            app.TreeNode("- test old", style=31),
                        ],
                    ),
                ],
            ),
        ]
        with (
            patch.object(
                app,
                "overview_data",
                return_value={
                    "nodes": [
                        {
                            "id": ".:repository",
                            "kind": "repository",
                            "repository": ".",
                            "path": ".",
                            "profile": "flake",
                        },
                        {
                            "id": ".:packages/sample",
                            "kind": "package",
                            "repository": ".",
                            "path": "packages/sample",
                            "name": "sample",
                            "package_type": "python",
                        },
                        {
                            "id": ".:hosts/laptop",
                            "kind": "host",
                            "repository": ".",
                            "path": "hosts/laptop",
                            "name": "laptop",
                        },
                        {
                            "id": ".:checks/sample",
                            "kind": "check",
                            "repository": ".",
                            "path": "checks/sample",
                            "name": "sample",
                        },
                    ],
                    "edges": [
                        {
                            "source": ".:packages/sample",
                            "target": ".:checks/sample",
                            "kind": "checked-by",
                        },
                    ],
                },
            ),
            patch.object(app.Viewer, "package_entries", return_value=nodes),
        ):
            snapshot = app.browser_data(root)
        records = {record["id"]: record for record in snapshot["nodes"]}
        if (
            any(record["kind"] == "check" for record in records.values())
            or snapshot["edges"]
        ):
            msg = "Check resources and their connections must stay hidden"
            raise AssertionError(msg)
        if "checks/" in json.dumps(snapshot["tree"]):
            msg = "Checks must not appear in the terminal tree"
            raise AssertionError(msg)
        repository = snapshot["tree"][0]
        package = repository["children"][0]
        if package != records[".:packages/sample"]["tree"]:
            msg = "Terminal and GUI package details must be identical"
            raise AssertionError(msg)
        if (
            records[".:hosts/laptop"]["icon"] != "nixos"
            or len(repository["children"]) != TEST_REPOSITORY_FIELDS
        ):
            msg = "Hosts must appear alongside packages"
            raise AssertionError(msg)
        file = next(
            child for child in package["children"] if child["title"] == "test_main.py"
        )
        leaves = file["children"][0]["children"]
        if [node["change"] for node in leaves] != ["added", "removed"]:
            msg = "Nested changes must retain their addition and removal status"
            raise AssertionError(msg)

    def test_directory_paths_start_collapsed(self) -> None:
        """Directory expansion is manual regardless of the number of packages."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "flake.nix").write_text("{}\n")
            for name in ("first", "second"):
                package = root / "packages" / name
                package.mkdir(parents=True)
                (package / "default.nix").write_text("{}\n")
                (package / "main.py").write_text('"""Example."""\n')
                viewer = app.Viewer(root)
                viewer.refresh_overview()
                directory = viewer.overview[0]
                if directory.expanded or any(
                    child.expanded for child in directory.children or []
                ):
                    msg = "Directories and packages must start collapsed"
                    raise AssertionError(msg)
                viewer.overview_rows(80)
                viewer.navigate("l", 20, viewer.rows(80))
                if not directory.expanded:
                    msg = "Directory expansion must remain available manually"
                    raise AssertionError(msg)

    def test_directory_scope_matches_snapshot_and_terminal(self) -> None:
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
            viewer = app.Viewer(scoped)
            viewer.refresh_overview()
            snapshot = viewer.snapshot
            if snapshot["root"] != str(scoped) or snapshot["parent"] != str(home):
                msg = "The current directory and its parent must define navigation"
                raise AssertionError(msg)
            repositories = {node["repository"] for node in snapshot["nodes"]}
            if repositories != {"example/one"}:
                msg = "A directory scope must exclude sibling repositories"
                raise AssertionError(msg)
            if snapshot["tree"] != [
                app.serialize_node(node) for node in viewer.overview
            ]:
                msg = "The snapshot and TUI must share the scoped hierarchy"
                raise AssertionError(msg)
            package = scoped / "example/one/packages/first"
            snapshot = app.browser_data(package)
            names = [
                node["name"] for node in snapshot["nodes"] if node["kind"] == "package"
            ]
            if names != ["first"]:
                msg = "Starting inside a package must exclude other packages"
                raise AssertionError(msg)

    def test_home_tree_nests_user_under_machine(self) -> None:
        """Home containment and collapsed machine details match both views."""
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            (home / "repository").mkdir()
            with patch.object(Path, "home", return_value=home):
                viewer = app.Viewer(home)
                viewer.refresh_overview()
                machine = viewer.overview[0]
                user, details = machine.children or []
                if (
                    machine.title != f"Machine: {platform.node()}"
                    or not machine.expanded
                    or user.title != f"User: {home.name} ({home})"
                    or user.directory != home
                    or user.expanded
                    or (user.children or [])[0].title != "repository"
                    or details.expanded
                ):
                    msg = "Home must nest its visible user below the machine"
                    raise AssertionError(msg)
                if viewer.snapshot["tree"] != [
                    app.serialize_node(node) for node in viewer.overview
                ]:
                    msg = "Snapshot and terminal must share the home hierarchy"
                    raise AssertionError(msg)
                (home / "flake.nix").write_text("{}\n")
                viewer.refresh_overview()
                machine = viewer.overview[0]
                user = (machine.children or [])[0]
                if user.directory != home:
                    msg = "Unexpected tree containment or expansion"
                    raise AssertionError(msg)
                if user.title != f"User: {home.name} ({home})":
                    msg = "Unexpected tree containment or expansion"
                    raise AssertionError(msg)

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
            snapshot = app.browser_data(root)
            record = next(
                node for node in snapshot["nodes"] if node["kind"] == "package"
            )
            sources = {
                child["title"]: next(
                    item["title"]
                    for item in child["children"]
                    if item["title"].startswith("Lines:")
                )
                for child in record["tree"]["children"]
                if child["source_file"]
            }
            if sources != {
                "default.nix": "Lines: 1",
                "main.py": "Lines: 3",
                "prm/web/script.js": "Lines: 3",
                "test_main.py": "Lines: 0",
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
            if snapshot["tree"][0]["children"][0] != record["tree"]:
                msg = "Source line counts must be shared by the GUI and TUI"
                raise AssertionError(msg)
            host = root / "hosts/example"
            host.mkdir(parents=True)
            (host / "configuration.nix").write_text("{}\n")
            host_record = next(
                node
                for node in app.browser_data(root)["nodes"]
                if node["kind"] == "host"
            )
            if host_record["source_metrics"]["lines"] != {"configuration.nix": 1}:
                msg = "Host source files must contribute current metrics"
                raise AssertionError(msg)

    def test_removed_dependencies_share_connections_and_change_colors(self) -> None:
        """Both renderers retain removed providers and their relationships."""
        tree = [
            app.TreeNode(
                "packages/consumer",
                [
                    app.TreeNode(
                        "Dependencies",
                        [
                            app.TreeNode("- build: packages/old", style=31),
                        ],
                    ),
                ],
            ),
        ]
        with patch.object(
            app,
            "overview_data",
            return_value={
                "nodes": [
                    {
                        "id": ".:repository",
                        "kind": "repository",
                        "repository": ".",
                        "path": ".",
                    },
                    {
                        "id": ".:packages/consumer",
                        "kind": "package",
                        "repository": ".",
                        "path": "packages/consumer",
                    },
                ],
                "edges": [],
            },
        ):
            snapshot, nodes = app.browser_snapshot(Path("/workspace"), tree)
        records = {record["id"]: record for record in snapshot["nodes"]}
        if not records[".:packages/old"]["removed"]:
            msg = "Removed dependency providers must remain in the shared snapshot"
            raise AssertionError(msg)
        connections = records[".:packages/consumer"]["tree"]["children"][-1]
        if connections["children"][0]["change"] != "removed":
            msg = "Connections must retain the same change colors as GUI arrows"
            raise AssertionError(msg)
        if snapshot["edges"][0]["change"] != "removed" or not nodes:
            msg = "Both the tree and graphical relationship must preserve removal"
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

    def test_terminal_directory_navigation_and_parent(self) -> None:
        """Enter and Backspace navigate directories independently of tree expansion."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            child = root / "child"
            child.mkdir()
            viewer = app.Viewer(root)
            viewer.refresh_overview()
            viewer.navigate("\n", 20, viewer.rows(80))
            if viewer.cwd != child:
                msg = "Enter must open the selected directory"
                raise AssertionError(msg)
            viewer.navigate(curses.KEY_BACKSPACE, 20, viewer.rows(80))
            if viewer.cwd != root:
                msg = "Parent navigation must work even in an empty directory"
                raise AssertionError(msg)
            with (
                patch.object(Path, "home", return_value=root),
                patch.object(viewer, "load_overview") as load,
            ):
                viewer.navigate(curses.KEY_BACKSPACE, 20, viewer.rows(80))
                load.assert_not_called()
                if (
                    viewer.cwd != root
                    or app.directory_snapshot(root)[0]["parent"] is not None
                ):
                    msg = "Home must be the upper navigation boundary"
                    raise AssertionError(msg)
                (root / "flake.nix").write_text("{}\n")
                if app.browser_data(root)["parent"] is not None:
                    msg = "Canonical home snapshots must also stop parent navigation"
                    raise AssertionError(msg)


class TestCli(unittest.TestCase):
    """Verify direct browser startup and the terminal requirement."""

    def test_nonterminal_executable_explains_text_alternative(self) -> None:
        """Piped execution fails clearly instead of entering a prompt loop."""
        executable = os.environ.get("PACKAGE_E2E_EXECUTABLE")
        if not executable:
            self.skipTest("Nix package executable not supplied")
        result = subprocess.run(
            [executable],
            input="",
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 1 or "perigrafo overview" not in result.stderr:
            msg = "Expected a terminal error with the text-output alternative"
            raise AssertionError(msg)
        if result.stdout:
            msg = "Nonterminal execution must not display a chat prompt"
            raise AssertionError(msg)

    def test_terminal_opens_browser_directly(self) -> None:
        """Startup opens the viewer without creating an agent or history."""
        with (
            patch.object(sys.stdin, "isatty", return_value=True),
            patch.object(sys.stdout, "isatty", return_value=True),
            patch.object(app.Viewer, "view") as view,
        ):
            app.main([])
        view.assert_called_once_with()


class TestViewer(unittest.TestCase):  # noqa: D101
    def test_current_package_view_uses_canonical_overview(self) -> None:  # noqa: D102
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            viewer = app.Viewer(root)
            with patch.object(
                app,
                "package_overview",
                return_value="Name: shared",
            ) as read:
                entry = viewer.package_entry(root, "sample", diff=False)
            read.assert_called_once_with(root / "packages/sample")
            if (
                entry is None
                or not entry.children
                or entry.children[0].title != "Name: shared"
            ):
                msg = "Package view did not display the canonical overview"
                raise AssertionError(msg)

    def test_dependencies_appear_in_shared_overview_and_inline_changes(self) -> None:
        """Show dependency declarations and edits using the shared summary."""
        before = app.Viewer.package_summary(
            "consumer",
            {
                "default.nix": (
                    "{inputs, system, ...}: { buildInputs = ["
                    "inputs.self.packages.${system}.core]; }"
                ),
            },
        )
        after = app.Viewer.package_summary(
            "consumer",
            {
                "default.nix": (
                    "{inputs, system, ...}: { buildInputs = ["
                    "inputs.self.packages.${system}.engine]; }"
                ),
            },
        )
        dependencies = next(
            node
            for node in app.Viewer.summary_tree(after)
            if node.title == "Dependencies"
        )
        if [node.title for node in dependencies.children or []] != [
            "build: packages/engine",
        ]:
            raise AssertionError(dependencies)
        for render in (app.Viewer.merged_summary_tree, app.Viewer.summary_changes):
            changes = next(
                node for node in render(before, after) if node.title == "Dependencies"
            )
            if {node.title for node in changes.children or []} != {
                "- build: packages/core",
                "+ build: packages/engine",
            }:
                raise AssertionError(changes)

    def test_diff_key_uses_cached_views_and_refreshes_only_on_request(self) -> None:
        """D switches cached trees, and r explicitly rebuilds the current snapshot."""
        viewer = app.Viewer()
        screen = MagicMock()
        screen.getmaxyx.return_value = (12, 80)
        screen.get_wch.side_effect = ["D", "r", "L", "D", "q"]
        with (
            patch.object(
                viewer,
                "package_entries",
                return_value=[
                    app.TreeNode(
                        "packages/example",
                        [
                            app.TreeNode("unchanged"),
                            app.TreeNode("+ changed", style=32),
                        ],
                    ),
                ],
            ) as build,
            patch.object(curses, "has_colors", return_value=False),
            patch.object(curses, "curs_set"),
            patch.object(
                app,
                "browser_snapshot",
                side_effect=lambda _root, tree: ({}, tree),
            ),
            patch.object(
                app,
                "scope_snapshot",
                side_effect=lambda data, _directory: (data, viewer.full_overview),
            ),
        ):
            viewer.ensure_overview()
            viewer.screen(screen)
        if [item.kwargs for item in build.call_args_list] != [
            {"diff": False},
            {"diff": False},
        ] or viewer.mode != "high-level":
            msg = "Only startup and r may recalculate the overview"
            raise AssertionError(msg)
        if any("high-level" in item.args[2] for item in screen.addnstr.call_args_list):
            msg = "The footer must omit the view name"
            raise AssertionError(msg)
        if [node.title for node in viewer.full_overview[0].children or []] != [
            "unchanged",
            "+ changed",
        ] or [node.title for node in viewer.diff_overview[0].children or []] != [
            "+ changed",
        ]:
            msg = "Filtering must preserve the complete cached overview"
            raise AssertionError(msg)

    def test_diff_only_omits_unchanged_fields_and_keeps_command_ancestors(self) -> None:
        """Filtering removes unchanged details while retaining nested change context."""
        before = (
            "Name: sample\nDescription: Before\nArguments:\n"
            "  --same\nTests:\n  test same"
        )
        after = before.replace("Before", "After")
        nodes = app.Viewer.merged_summary_tree(before, after)
        nodes.append(
            app.TreeNode(
                "command",
                [app.TreeNode("--same"), app.TreeNode("+ --new", style=32)],
            ),
        )
        changes = app.Viewer.changed_nodes(nodes)
        if [node.title for node in changes] != [
            "- Description: Before",
            "+ Description: After",
            "command",
        ] or [node.title for node in changes[-1].children or []] != ["+ --new"]:
            msg = "Diff-only trees must contain changes and their command ancestors"
            raise AssertionError(msg)

    def test_directory_navigation_reuses_source_snapshot_until_refresh(self) -> None:
        """Parent navigation rescopes declarations; Refresh reads new evidence."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "flake.nix").write_text("{}\n")
            package = root / "packages/example"
            package.mkdir(parents=True)
            (package / "default.nix").write_text("{}\n")
            (package / "main.py").write_text('"""Before."""\n')
            viewer = app.Viewer(package)
            with patch.object(
                app,
                "browser_snapshot",
                wraps=app.browser_snapshot,
            ) as build:
                viewer.ensure_overview()
                (package / "main.py").write_text('"""After."""\n')
                viewer.navigate(curses.KEY_BACKSPACE, 20, viewer.rows(80))
                viewer.navigate(curses.KEY_BACKSPACE, 20, viewer.rows(80))
                if viewer.cwd != root or build.call_count != 1:
                    msg = "Navigating within a repository must reuse its declarations"
                    raise AssertionError(msg)
                serialized = json.dumps(viewer.snapshot)
                if "Before." not in serialized or "After." in serialized:
                    msg = "Navigation must retain the snapshot until explicit Refresh"
                    raise AssertionError(msg)
                viewer.refresh_overview()
                if build.call_count != TEST_EXPECTED_BUILDS:
                    msg = "Refresh must reread declarations"
                    raise AssertionError(msg)
                if "After." not in json.dumps(viewer.snapshot):
                    msg = "Refresh must display the new declarations"
                    raise AssertionError(msg)

    def test_empty_groups_are_hidden_but_diagnostics_and_removals_remain(self) -> None:
        """Placeholder declarations are neither entries nor semantic changes."""
        empty = (
            "Name: sample\nArguments:\n  (not applicable)\n"
            "Dependencies:\n  (not declared)\nTests:\n  (none)\n"
            "Suppressions:\n  (none)\n"
        )
        for tree in (
            app.Viewer.summary_tree(empty, cli=[CliEntry((), "(not applicable)")]),
            app.Viewer.merged_summary_tree(empty, empty),
        ):
            if [node.title for node in tree] != ["Name: sample"]:
                msg = "Empty declaration groups must be omitted in both views"
                raise AssertionError(msg)
        previous = empty.replace("Tests:\n  (none)", "Tests:\n  test existing")
        changed = app.Viewer.merged_summary_tree(previous, empty)
        tests = next(node for node in changed if node.title == "Tests")
        if [node.title for node in tests.children or []] != ["- test existing"]:
            msg = "Removing the last test must remain visible in the diff"
            raise AssertionError(msg)
        diagnostic = empty.replace(
            "Tests:\n  (none)",
            "Tests:\n  (unavailable: syntax error)",
        )
        tests = next(
            node
            for node in app.Viewer.summary_tree(diagnostic)
            if node.title == "Tests"
        )
        if not app.Viewer.has_warning(tests):
            msg = "Unavailable analysis must remain visible as a diagnostic"
            raise AssertionError(msg)

    def test_g_and_capital_g_move_cursor_to_viewport_edges(self) -> None:  # noqa: D102
        for mode in ("high-level", "high-level diff"):
            viewer = app.Viewer()
            viewer.mode = mode
            viewer.overview = [app.TreeNode(str(index)) for index in range(5)]
            rows = viewer.rows(80)
            viewer.navigate("G", TEST_PAGE_HEIGHT, rows)
            if (
                viewer.top != len(rows) - TEST_PAGE_HEIGHT
                or viewer.selected != rows[-1].owner
            ):
                msg = f"G must place the cursor on the bottom row in {mode} mode"
                raise AssertionError(msg)
            viewer.navigate("g", TEST_PAGE_HEIGHT, viewer.rows(80))
            if viewer.top != 0 or viewer.selected != 0:
                msg = f"g must place the cursor on the top row in {mode} mode"
                raise AssertionError(msg)

    def test_high_level_diff_compares_summaries_not_source_code(self) -> None:  # noqa: D102, PLR0915
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(
                ["git", "-C", str(root), "config", "user.email", "test@example.com"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(root), "config", "user.name", "Test"],
                check=True,
            )
            package = root / "packages/sample"
            package.mkdir(parents=True)
            (package / "default.nix").write_text(
                '{ meta.description = "Before"; }\n',
                encoding="utf-8",
            )
            (package / "main.py").write_text(
                '"""Same help."""\n'
                "import argparse\n"
                "parser = argparse.ArgumentParser()\n"
                'parser.add_argument("--old", help="Old option")\n'
                "value = 1\n",
                encoding="utf-8",
            )
            (package / "test_main.py").write_text(
                "def test_old(): pass\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "-C", str(root), "add", "packages"], check=True)
            subprocess.run(
                ["git", "-C", str(root), "commit", "-qm", "baseline"],
                check=True,
            )
            (package / "default.nix").write_text(
                '{ meta.description = "After"; }\n',
                encoding="utf-8",
            )
            (package / "main.py").write_text(
                '"""Same help."""\n'
                "import argparse\n"
                "parser = argparse.ArgumentParser()\n"
                'parser.add_argument("--new", help="New option")\n'
                "value = 2\n",
                encoding="utf-8",
            )
            (package / "test_main.py").write_text(
                "def test_new(): pass\n",
                encoding="utf-8",
            )
            added = root / "packages/added"
            added.mkdir()
            (added / "main.py").write_text('"""New package."""\n', encoding="utf-8")
            removed = root / "packages/removed"
            removed.mkdir()
            (removed / "default.nix").write_text(
                '{ meta.description = "Gone"; }\n',
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "-C", str(root), "add", "packages/removed"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(root), "commit", "-qm", "add removed"],
                check=True,
            )
            shutil.rmtree(removed)
            viewer = app.Viewer(root)
            entries = {node.title: node for node in viewer.package_entries(diff=True)}
            sample_diff = entries["packages/sample"]
            changed_fields = {node.title for node in sample_diff.children or []}
            if (
                "- Description: Before" not in changed_fields
                or "+ Description: After" not in changed_fields
            ):
                msg = "Diff must include changed summary metadata"
                raise AssertionError(msg)
            tests = next(
                node for node in sample_diff.children or [] if node.title == "Tests"
            )
            if {node.title for node in tests.children or []} != {
                "- test old",
                "+ test new",
            }:
                msg = "Diff must show removed and added test names"
                raise AssertionError(msg)
            arguments = next(
                node for node in sample_diff.children or [] if node.title == "Arguments"
            )
            if {node.title for node in arguments.children or []} != {
                "- --old  optional; help='Old option'",
                "+ --new  optional; help='New option'",
            }:
                msg = "Argument diff must show removed and added CLI options"
                raise AssertionError(msg)
            viewer.mode = "high-level diff"
            viewer.overview = [sample_diff]
            viewer.selected = 0
            viewer.navigate("l", 20, viewer.rows(80))
            visible = "\n".join(row.text for row in viewer.rows(80))
            if "test old" in visible or "test new" in visible:
                msg = "Test-name diff children must be collapsed under Tests"
                raise AssertionError(msg)
            if "--old" in visible or "--new" in visible:
                msg = "Argument diff entries must be collapsed under Arguments"
                raise AssertionError(msg)
            arguments_row = next(
                row
                for row in viewer.rows(80)
                if row.text.rstrip().endswith("Arguments")
            )
            viewer.selected = arguments_row.owner
            viewer.navigate("l", 20, viewer.rows(80))
            visible = "\n".join(row.text for row in viewer.rows(80))
            if "--old" not in visible or "--new" not in visible:
                msg = "Expanding Arguments in a diff must show changed options"
                raise AssertionError(msg)
            tests_row = next(
                row for row in viewer.rows(80) if row.text.rstrip().endswith("Tests")
            )
            viewer.selected = tests_row.owner
            viewer.navigate("l", 20, viewer.rows(80))
            visible = "\n".join(row.text for row in viewer.rows(80))
            if "test old" not in visible or "test new" not in visible:
                msg = "Expanding Tests in a diff must show changed test names"
                raise AssertionError(msg)
            if "packages/added" not in entries or "packages/removed" not in entries:
                msg = "High-level diff must include added and removed packages"
                raise AssertionError(msg)
            if any("value =" in node.title for node in sample_diff.children or []):
                msg = "High-level diff must omit source-code changes"
                raise AssertionError(msg)

    def test_high_level_diff_omits_unchanged_summaries_and_colors_changes(self) -> None:  # noqa: D102
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(
                ["git", "-C", str(root), "config", "user.email", "test@example.com"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(root), "config", "user.name", "Test"],
                check=True,
            )
            package = root / "packages/same"
            package.mkdir(parents=True)
            (package / "main.py").write_text(
                '"""Help."""\nvalue = 1\n',
                encoding="utf-8",
            )
            (package / "test_main.py").write_text(
                "def test_one(): pass\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "-C", str(root), "add", "packages"], check=True)
            subprocess.run(
                ["git", "-C", str(root), "commit", "-qm", "baseline"],
                check=True,
            )
            (package / "main.py").write_text(
                '"""Help."""\nvalue = 2\n',
                encoding="utf-8",
            )
            viewer = app.Viewer(root)
            overview = viewer.package_entries()
            if not overview or overview[0].title != "packages/same":
                msg = "The regular high-level view must retain unchanged summaries"
                raise AssertionError(msg)
            if viewer.package_entries(diff=True):
                msg = "Source-only changes must not appear in a high-level diff"
                raise AssertionError(msg)
            viewer.mode = "high-level diff"
            viewer.overview = [
                app.TreeNode(
                    "packages/change",
                    [
                        app.TreeNode("- old", style=31),
                        app.TreeNode("+ new", style=32),
                    ],
                    expanded=True,
                ),
            ]
            rows = viewer.rows(80)
            if viewer.styles(rows[1]) != [31] or viewer.styles(rows[2]) != [32]:
                msg = "Removed and added summary lines must be red and green"
                raise AssertionError(msg)

    def test_home_high_level_preserves_repository_directory_structure(self) -> None:  # noqa: D102
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / ".gitmodules").write_text(
                '[submodule "github.com/example/project"]\n'
                "\tpath = github.com/example/project\n",
                encoding="utf-8",
            )
            package = root / "github.com/example/project/packages/sample"
            package.mkdir(parents=True)
            (package / "main.py").write_text('"""Sample help."""\n', encoding="utf-8")
            viewer = app.Viewer(root)
            entries = viewer.package_entries()
            if [node.title for node in entries] != ["github.com"]:
                msg = "Home overview must start with the repository path parent"
                raise AssertionError(msg)
            domain = entries[0].children or []
            organization = domain[0].children or []
            repository = organization[0].children or []
            if repository[0].title != "packages/sample":
                msg = "Home overview must preserve repository and package hierarchy"
                raise AssertionError(msg)

    def test_nested_cli_diff_keeps_changes_under_their_commands(self) -> None:
        """Show changed parameters and removed commands at their original paths."""
        before = [
            CliEntry(("test",), "command", command=True),
            CliEntry(("test", "coverage"), "command", command=True),
            CliEntry(("test", "coverage"), "--jobs  default=2"),
            CliEntry(("retired",), "command", command=True),
        ]
        after = [*before[:2], CliEntry(("test", "coverage"), "--jobs  default=4")]
        tree = app.Viewer.cli_tree(after, previous=before)
        test = next(node for node in tree if node.title == "test")
        coverage = (test.children or [])[0]
        if [(node.title, node.style) for node in coverage.children or []] != [
            ("- --jobs  default=2", 31),
            ("+ --jobs  default=4", 32),
        ]:
            msg = "Changed CLI parameters must retain their path and colors"
            raise AssertionError(msg)
        retired = next(node for node in tree if node.title == "- retired")
        if (retired.title, retired.style) != ("- retired", 31):
            msg = "Removed commands must retain removal styling"
            raise AssertionError(msg)

    def test_overview_child_collapse_and_cursor_follow(self) -> None:  # noqa: D102
        viewer = app.Viewer()
        viewer.mode = "high-level"
        leaf = app.TreeNode("leaf")
        branch = app.TreeNode("branch", [leaf])
        viewer.overview = [app.TreeNode("root", [branch])]
        viewer.height = 2
        viewer.navigate("l", 2, viewer.rows(80))
        viewer.navigate("j", 2, viewer.rows(80))
        viewer.navigate("l", 2, viewer.rows(80))
        viewer.navigate("j", 2, viewer.rows(80))
        visible = viewer.rows(80)
        selected_row = next(
            index for index, row in enumerate(visible) if row.owner == viewer.selected
        )
        if not viewer.top <= selected_row < viewer.top + viewer.height:
            msg = "Moving down must scroll the selected tree row into view"
            raise AssertionError(msg)
        viewer.navigate("h", 2, visible)
        visible = viewer.rows(80)
        if (
            viewer.overview_visible[viewer.selected].title != "branch"
            or len(visible) != TEST_EXPECTED_VISIBLE
        ):
            msg = "h on a nested child must collapse its parent and select that parent"
            raise AssertionError(msg)

    def test_overview_is_cached_until_refreshed(self) -> None:  # noqa: D102
        viewer = app.Viewer()
        with (
            patch.object(
                viewer,
                "package_entries",
                side_effect=[[app.TreeNode("first")], [app.TreeNode("second")]],
            ) as build,
            patch.object(
                app,
                "browser_snapshot",
                side_effect=lambda _root, tree: ({}, tree),
            ),
            patch.object(
                app,
                "scope_snapshot",
                side_effect=lambda data, _directory: (data, viewer.full_overview),
            ),
        ):
            viewer.ensure_overview()
            viewer.ensure_overview()
            if build.call_count != 1 or viewer.overview[0].title != "first":
                msg = "The startup overview must be built only once"
                raise AssertionError(msg)
            viewer.selected = viewer.top = 3
            viewer.refresh_overview()
            if (
                build.call_count != TEST_EXPECTED_BUILDS
                or viewer.overview[0].title != "second"
            ):
                msg = "Refreshing must rebuild the overview"
                raise AssertionError(msg)
            if viewer.selected != 0 or viewer.top != 0:
                msg = "Refreshing must reset navigation to the start"
                raise AssertionError(msg)

    def test_overview_nests_commands_and_search_reveals_parameters(self) -> None:
        """Keep root options visible and nested command parameters collapsible."""
        source = (
            "import argparse\n"
            "p = argparse.ArgumentParser()\n"
            "p.add_argument('--verbose')\n"
            "commands = p.add_subparsers()\n"
            "test = commands.add_parser('test')\n"
            "children = test.add_subparsers()\n"
            "coverage = children.add_parser('coverage')\n"
            "coverage.add_argument('--jobs', type=int, default=2)\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "packages/sample"
            package.mkdir(parents=True)
            (package / "main.py").write_text(source, encoding="utf-8")
            viewer = app.Viewer(root)
            viewer.mode = "high-level"
            viewer.overview = viewer.package_entries()
            entry = viewer.overview[0]
            arguments = next(
                node for node in entry.children or [] if node.title == "Arguments"
            )
            children = arguments.children or []
            if [node.title for node in children] != ["--verbose  optional", "test"]:
                msg = "Arguments must contain root options and command nodes"
                raise AssertionError(msg)
            test = children[1]
            coverage = (test.children or [])[0]
            if coverage.title != "coverage" or [
                node.title for node in coverage.children or []
            ] != ["--jobs  optional; default=2; type=int"]:
                msg = "Nested command must own its parameters"
                raise AssertionError(msg)
            entry.expanded = arguments.expanded = True
            visible = "\n".join(row.text for row in viewer.rows(100))
            if "--jobs" in visible or "test: command" in visible:
                msg = "Command details must stay collapsed without redundant labels"
                raise AssertionError(msg)
            viewer.pattern = "--jobs"
            viewer.search(1)
            visible = "\n".join(row.text for row in viewer.rows(100))
            if "--jobs" not in visible or not test.expanded or not coverage.expanded:
                msg = "Search must open every ancestor of a CLI parameter"
                raise AssertionError(msg)

    def test_package_details_belong_to_their_source_files(self) -> None:
        """Keep arguments, tests, documentation, and suppression diffs under sources."""
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary)
            (package / "main.py").write_text('"""Example."""\n# noqa\n')
            (package / "prm").mkdir()
            (package / "prm/script.js").write_text(
                "// eslint-disable-next-line\nrun();\n",
            )
            arguments = app.TreeNode("Arguments", [app.TreeNode("+ --help", style=32)])
            tests = app.TreeNode("Tests", [app.TreeNode("- test old", style=31)])
            tree = app.TreeNode(
                "packages/example",
                [
                    app.TreeNode("Language: python"),
                    app.TreeNode("Help: Example."),
                    arguments,
                    tests,
                    app.TreeNode(
                        "Suppressions",
                        [
                            app.TreeNode(
                                "main.py",
                                [app.TreeNode("noqa (global): 1 → 2", style=33)],
                            ),
                            app.TreeNode(
                                "test_main.py",
                                [app.TreeNode("type: ignore (local): 1 → 0", style=31)],
                            ),
                        ],
                    ),
                ],
            )
            sources = app.package_sources(package)
            metrics = app.source_metrics(sources)
            app.package_file_tree(package, tree, sources)
            if metrics["suppressions"].get("noqa (global)", 0) != 0:
                msg = "Current metrics must ignore suppression diff baselines"
                raise AssertionError(msg)
            files = {
                node.title: node for node in tree.children or [] if node.source_file
            }
            if set(files) != {"main.py", "test_main.py", "prm/script.js"}:
                msg = "Package details must be rooted at source files"
                raise AssertionError(msg)
            main = {node.title: node for node in files["main.py"].children or []}
            if main["Arguments"] is not arguments or "Example." not in main:
                msg = "Arguments and documentation must belong to main.py"
                raise AssertionError(msg)
            if [node.title for node in files["main.py"].children or []][:4] != [
                "Example.",
                "Lines: 2",
                "noqa (local): 1",
                "noqa (global): 1 → 2",
            ]:
                msg = "Documentation, lines, and flat Python counts must stay ordered"
                raise AssertionError(msg)
            if main["noqa (global): 1 → 2"].style != TEST_MODIFIED_STYLE:
                msg = "Flat suppression rows must preserve semantic diff styling"
                raise AssertionError(msg)
            test = {node.title: node for node in files["test_main.py"].children or []}
            if test["Tests"] is not tests or "Lines: 0" in test:
                msg = "Removed files must retain test diffs without claiming zero lines"
                raise AssertionError(msg)
            script = {
                node.title: node for node in files["prm/script.js"].children or []
            }
            if "eslint-disable (local): 1" not in script or "Suppressions" in script:
                msg = "Source assets must have direct suppression rows"
                raise AssertionError(msg)
            changed = app.Viewer.changed_nodes([tree])[0]
            if {node.title for node in changed.children or []} != {
                "main.py",
                "test_main.py",
            }:
                msg = "Diff filtering must preserve source ancestors of changed details"
                raise AssertionError(msg)
            if any(
                "Lines:" in str(app.serialize_node(node))
                for node in changed.children or []
            ):
                msg = "Informational line counts must not create semantic changes"
                raise AssertionError(msg)

    def test_package_summary_fields_and_test_name_children(self) -> None:  # noqa: D102
        files = {
            "default.nix": '{ meta.description = "Useful package"; }\n',
            "main.py": (
                '"""Package help."""\n'
                "import argparse\n"
                "parser = argparse.ArgumentParser()\n"
                'parser.add_argument("--input", help="Input path")\n'
            ),
            "test_main.py": "def test_alpha(): pass\ndef test_beta(): pass\n",
        }
        summary = app.Viewer.package_summary("sample", files)
        if not all(
            value in summary
            for value in (
                "Name: sample",
                "Description: Useful package",
                "Help: Package help.",
                "--input  optional; help='Input path'",
                "  test alpha",
                "  test beta",
            )
        ):
            msg = "Package summary must show metadata and test names"
            raise AssertionError(msg)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "packages/sample"
            package.mkdir(parents=True)
            for filename, content in files.items():
                (package / filename).write_text(content, encoding="utf-8")
            viewer = app.Viewer(root)
            viewer.mode = "high-level"
            viewer.overview = viewer.package_entries()
            if len(viewer.rows(80)) != 1:
                msg = "Package children must be collapsed by default"
                raise AssertionError(msg)
            viewer.navigate("l", 20, viewer.rows(80))
            visible = "\n".join(row.text for row in viewer.rows(80))
            if "Tests" not in visible or "test_alpha" in visible:
                msg = "Expanding a package must reveal a collapsed Tests group"
                raise AssertionError(msg)
            arguments_row = next(
                row
                for row in viewer.rows(80)
                if row.text.rstrip().endswith("Arguments")
            )
            viewer.selected = arguments_row.owner
            viewer.navigate("l", 20, viewer.rows(80))
            visible = "\n".join(row.text for row in viewer.rows(80))
            if "--input  optional; help='Input path'" not in visible:
                msg = "Expanding Arguments must show declared options"
                raise AssertionError(msg)
            viewer.navigate("h", 20, viewer.rows(80))
            visible = "\n".join(row.text for row in viewer.rows(80))
            if "--input  optional; help='Input path'" in visible:
                msg = "Collapsing Arguments must hide argument entries"
                raise AssertionError(msg)
            tests_row = next(
                row for row in viewer.rows(80) if row.text.rstrip().endswith("Tests")
            )
            viewer.selected = tests_row.owner
            viewer.navigate("l", 20, viewer.rows(80))
            visible = "\n".join(row.text for row in viewer.rows(80))
            if "test alpha" not in visible or "test beta" not in visible:
                msg = "Expanding Tests must reveal each test name"
                raise AssertionError(msg)
            viewer.navigate("h", 20, viewer.rows(80))
            visible = "\n".join(row.text for row in viewer.rows(80))
            if "test alpha" in visible or "test beta" in visible:
                msg = "Collapsing Tests must hide its test-name children"
                raise AssertionError(msg)
            viewer.selected = 0
            viewer.navigate("h", 20, viewer.rows(80))
            if len(viewer.rows(80)) != 1:
                msg = "Collapsing a package must hide all package children"
                raise AssertionError(msg)

    def test_package_summary_uses_canonical_cli_parser(self) -> None:  # noqa: D102
        source = (
            "import argparse\n"
            "parser = argparse.ArgumentParser()\n"
            "commands = parser.add_subparsers()\n"
            "build = commands.add_parser('build')\n"
            "build.add_argument('--jobs', type=int, default=2, help='Worker count')\n"
        )
        arguments = app.Viewer.summary_group(
            app.Viewer.package_summary("sample", {"main.py": source}),
            "Arguments",
        )
        if arguments != [
            "build: command",
            "build: --jobs  optional; default=2; type=int; help='Worker count'",
        ]:
            msg = f"Unexpected canonical CLI summary: {arguments!r}"
            raise AssertionError(msg)

    def test_package_summary_uses_canonical_test_discovery(self) -> None:  # noqa: D102
        files = {
            "test_main.py": (
                "def test_top_level(): pass\n"
                "def helper_test(): pass\n"
                "class TestCases:\n"
                "    def test_method_case(self): pass\n"
                "    def helper(self): pass\n"
                "class Helpers:\n"
                "    def test_not_a_case(self): pass\n"
            ),
        }
        summary = app.Viewer.package_summary("sample", files)
        tests = app.Viewer.summary_group(summary, "Tests")
        if tests != ["test top level", "test method case"]:
            msg = f"Unexpected canonical test names: {tests!r}"
            raise AssertionError(msg)

    def test_paging_at_last_parent_does_not_jump_backwards(self) -> None:  # noqa: D102
        viewer = app.Viewer()
        viewer.overview = [app.TreeNode(str(index)) for index in range(30)]
        for _ in viewer.overview:
            viewer.navigate("j", 10, viewer.rows(80))
        top = viewer.top
        viewer.navigate(" ", 10, viewer.rows(80))
        if viewer.top < top:
            msg = "Forward paging at the final parent must not scroll backwards"
            raise AssertionError(msg)

    def test_resolved_cli_diagnostic_does_not_warn_in_inline_history(self) -> None:
        """Keep removed diagnostics visible without marking a fixed package."""
        before = "def main():\n    pass\n"
        after = (
            "import argparse\ndef parser():\n"
            "    return argparse.ArgumentParser()\n"
            "def main():\n    parser().parse_args()\n"
        )
        tree = app.Viewer.merged_summary_tree(
            app.Viewer.package_summary("sample", {"main.py": before}),
            app.Viewer.package_summary("sample", {"main.py": after}),
            previous_cli=app.source_cli_overview(before),
            current_cli=app.source_cli_overview(after),
        )
        entry = app.TreeNode("packages/sample", tree)
        if app.Viewer.has_warning(entry):
            msg = "Historical diagnostics must not keep a resolved warning active"
            raise AssertionError(msg)
        arguments = next(node for node in tree if node.title == "Arguments")
        if not any(
            node.title.startswith("- (unavailable:")
            for node in arguments.children or []
        ):
            msg = "Resolved diagnostics must remain visible as removals"
            raise AssertionError(msg)
        current = app.TreeNode(
            "packages/sample",
            app.Viewer.cli_tree(
                app.source_cli_overview(before),
                previous=app.source_cli_overview(after),
            ),
        )
        if not app.Viewer.has_warning(current):
            msg = "New diagnostics must still mark the package as unavailable"
            raise AssertionError(msg)

    def test_suppression_counts_appear_in_overview_and_diff(self) -> None:
        """Keep suppression counts visible in the shared package summary."""
        before = app.Viewer.package_summary(
            "sample",
            {"index.html": "<main></main>\n"},
        )
        after = app.Viewer.package_summary(
            "sample",
            {
                "index.html": (
                    "<!-- html-validate-disable -->\n"
                    "<!-- html-validate-disable-next -->\n"
                ),
            },
        )
        tree = app.Viewer.summary_tree(after)
        suppressions = next(node for node in tree if node.title == "Suppressions")
        files = {node.title: node for node in suppressions.children or []}
        if list(files) != ["index.html"] or {
            node.title for node in files["index.html"].children or []
        } != {
            "html-validate-disable (global): 1",
            "html-validate-disable (local): 1",
        }:
            msg = "Overview must distinguish global and local HTML directives"
            raise AssertionError(msg)
        diff = app.Viewer.merged_summary_tree(before, after)
        changes = next(node for node in diff if node.title == "Suppressions")
        if not any(
            child.title == "html-validate-disable (global): 0 → 1"
            for node in changes.children or []
            if node.title == "index.html"
            for child in node.children or []
        ):
            msg = "Summary diff must show new suppression counts"
            raise AssertionError(msg)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            package = root / "packages/sample"
            package.mkdir(parents=True)
            html = package / "index.html"
            html.write_text("<main></main>\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "packages"], check=True)
            html.write_text("<!-- html-validate-disable -->\n", encoding="utf-8")
            viewer = app.Viewer(root)
            entry = viewer.package_entry(root, "sample", diff=True)
            actual = (
                next(
                    node
                    for node in entry.children or []
                    if node.title == "Suppressions"
                )
                if entry
                else None
            )
            if actual is None or not any(
                child.title == "html-validate-disable (global): 0 → 1"
                for node in actual.children or []
                if node.title == "index.html"
                for child in node.children or []
            ):
                msg = "HTML diff must compare the previous file contents"
                raise AssertionError(msg)

    def test_suppression_diff_changes_counts_without_repeating_labels(self) -> None:
        """Include bare and tagged type ignores, excluding string contents."""
        before = app.Viewer.package_summary(
            "sample",
            {
                "main.py": "value = 1  # type: ignore[assignment]\n# noqa\n",
                "test_main.py": "# type: ignore\n",
            },
        )
        after = app.Viewer.package_summary(
            "sample",
            {
                "main.py": (
                    "value = 1  # type: ignore[assignment]\n"
                    "other = 2  # type: ignore\n"
                    "# noqa\n"
                    'text = "# type: ignore"\n'
                ),
                "test_main.py": "",
            },
        )
        tree = app.Viewer.merged_summary_tree(before, after)
        group = next(node for node in tree if node.title == "Suppressions")
        files = {node.title: node for node in group.children or []}
        if list(files) != ["main.py", "test_main.py"] or any(
            node.expanded for node in files.values()
        ):
            msg = "Suppression filenames must be separate, collapsed groups"
            raise AssertionError(msg)
        if [node.title for node in files["main.py"].children or []] != [
            "noqa (local): 1",
            "type: ignore (local): 1 → 2",
        ] or [node.title for node in files["test_main.py"].children or []] != [
            "type: ignore (local): 1 → 0",
        ]:
            msg = "Each filename must contain its own suppression count transitions"
            raise AssertionError(msg)
        changes = app.Viewer.changed_nodes([group])[0]
        changed_files = {node.title: node for node in changes.children or []}
        if [node.title for node in changed_files["main.py"].children or []] != [
            "type: ignore (local): 1 → 2",
        ] or "test_main.py" not in changed_files:
            msg = "Diff-only views must keep file parents and omit unchanged counts"
            raise AssertionError(msg)
        viewer = app.Viewer()
        viewer.overview = [changes]
        viewer.navigate("l", 20, viewer.rows(80))
        if any("type: ignore" in row.text for row in viewer.rows(80)):
            msg = "Suppression counts must stay hidden until their file is expanded"
            raise AssertionError(msg)
        viewer.navigate("j", 20, viewer.rows(80))
        viewer.navigate("l", 20, viewer.rows(80))
        if not any(
            "type: ignore (local): 1 → 2" in row.text for row in viewer.rows(80)
        ):
            msg = "Expanding a file must reveal its count changes"
            raise AssertionError(msg)

    def test_unavailable_cli_summary_warns_on_collapsed_package(self) -> None:
        """Expose parser diagnostics without opening the package tree."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "packages/sample"
            package.mkdir(parents=True)
            (package / "main.py").write_text(
                '"""Example."""\ndef main():\n    pass\n',
                encoding="utf-8",
            )
            viewer = app.Viewer(root)
            entry = viewer.package_entry(root, "sample", diff=False)
            if entry is None:
                msg = "Expected a package entry"
                raise AssertionError(msg)
            viewer.mode = "high-level"
            viewer.overview = [entry]
            rows = viewer.rows(80)
            if not rows[0].text.endswith("packages/sample [!]"):
                msg = "Collapsed package must show a warning marker"
                raise AssertionError(msg)
            if viewer.styles(rows[0]) != [33, 7]:
                msg = "Package warning must be visible in the overview"
                raise AssertionError(msg)
            entry.expanded = True
            arguments = next(
                node for node in entry.children or [] if node.title == "Arguments"
            )
            arguments.expanded = True
            visible = "\n".join(row.text for row in viewer.rows(80))
            if "(unavailable: unsupported CLI interface" not in visible:
                msg = "Expanding Arguments must reveal the original diagnostic"
                raise AssertionError(msg)
            (package / "main.py").write_text(
                "import argparse\nparser = argparse.ArgumentParser()\n",
                encoding="utf-8",
            )
            fixed = viewer.package_entry(root, "sample", diff=False)
            if fixed is None or viewer.has_warning(fixed):
                msg = "Warning must clear when the CLI summary becomes available"
                raise AssertionError(msg)

    def test_unicode_and_tiny_terminals(self) -> None:  # noqa: D102
        viewer = app.Viewer()
        viewer.overview = [app.TreeNode("界e\u0301界界")]
        for width in (1, 2, 4, 20):
            for _start, content in viewer.wrap("界e\u0301界界", width):
                cells = sum(
                    0
                    if unicodedata.combining(char)
                    else 2
                    if unicodedata.east_asian_width(char) in {"W", "F"}
                    else 1
                    for char in content
                )
                if cells > width:
                    msg = "Wide characters and indentation must fit the terminal"
                    raise AssertionError(msg)

    def test_wrapping_and_control_characters(self) -> None:  # noqa: D102
        viewer = app.Viewer()
        viewer.overview = [app.TreeNode("\x1b[31m" + "x" * 100)]
        width = 20
        rows = viewer.rows(width)
        if any(len(row.text) > width or "\x1b" in row.text for row in rows):
            msg = "Output must wrap and must not execute terminal escape sequences"
            raise AssertionError(msg)

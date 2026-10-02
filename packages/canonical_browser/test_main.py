# Copyright (c) 2026 VALAB/ITI
# ruff: noqa: S603, S607
"""Verify browser transport, directory scopes, and semantic package diffs."""

import http.client
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http import HTTPStatus
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

import pytest
from git_canonical import CliEntry, source_cli_overview

from packages.canonical_browser import main as app

TEST_MODIFIED_CHANGE = "modified"
TEST_PARSER_ERROR = 2
TEST_REPOSITORY_FIELDS = 3


class TestGui(unittest.TestCase):
    """Verify the read-only GUI transport and shared semantic model."""

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
            with app.gui_server(root) as server:
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                connection = http.client.HTTPConnection(
                    "127.0.0.1",
                    server.server_port,
                    timeout=5,
                )
                try:
                    connection.request(
                        "GET",
                        "/output?" + urlencode({"path": str(output)}),
                    )
                    response = connection.getresponse()
                    listing = response.read().decode()
                    if (
                        response.status != HTTPStatus.OK
                        or "report &amp; &lt;one&gt;.txt" not in listing
                        or "escaped.py" in listing
                    ):
                        msg = "Output listings must escape names and omit escaped links"
                        raise AssertionError(msg)
                    connection.request(
                        "GET",
                        "/output?" + urlencode({"path": str(output / name)}),
                    )
                    response = connection.getresponse()
                    if (
                        response.status != HTTPStatus.OK
                        or response.read() != b"Generated output"
                    ):
                        msg = "Output files must open directly in the web browser"
                        raise AssertionError(msg)
                    connection.request(
                        "GET",
                        "/output?" + urlencode({"path": str(nested)}),
                    )
                    response = connection.getresponse()
                    if (
                        response.status != HTTPStatus.OK
                        or "../" not in response.read().decode()
                    ):
                        msg = "Nested output listings must link to their parent"
                        raise AssertionError(msg)
                    for path in (
                        source,
                        output / ".." / "main.py",
                        output / "escaped.py",
                    ):
                        connection.request(
                            "GET",
                            "/output?" + urlencode({"path": str(path)}),
                        )
                        response = connection.getresponse()
                        response.read()
                        if response.status != HTTPStatus.NOT_FOUND:
                            msg = "Output browsing must not expose source files"
                            raise AssertionError(msg)
                finally:
                    connection.close()
                    server.shutdown()
                    thread.join(timeout=5)

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

    def test_home_is_the_upper_navigation_boundary(self) -> None:
        """Home scopes have no parent, including a home with a flake."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(Path, "home", return_value=root):
                if app.directory_snapshot(root)[0]["parent"] is not None:
                    msg = "Directory home must have no parent"
                    raise AssertionError(msg)
                (root / "flake.nix").write_text("{}\n")
                if app.gui_data(root)["parent"] is not None:
                    msg = "Flake home must have no parent"
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

    def test_removed_dependencies_share_connections_and_change_colors(self) -> None:
        """Both renderers retain removed providers and their relationships."""
        tree = [
            app.TreeNode(
                "packages/consumer",
                [
                    app.TreeNode(
                        "Dependencies",
                        [
                            app.TreeNode("- build: packages/old", change="removed"),
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

    def test_gui_serves_assets_and_live_data_without_exposing_checkout(self) -> None:
        """Assets ship with the package; traversal and writes cannot reach files."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with app.gui_server(root) as server:
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                connection = http.client.HTTPConnection(
                    "127.0.0.1",
                    server.server_port,
                    timeout=5,
                )
                try:
                    for route in (
                        "/",
                        "/script.js",
                        "/style.css",
                        "/g6.js",
                    ):
                        connection.request("GET", route)
                        response = connection.getresponse()
                        if response.status != HTTPStatus.OK or not response.read():
                            msg = f"Missing packaged GUI asset: {route}"
                            raise AssertionError(msg)
                    with patch.object(
                        app,
                        "gui_data",
                        side_effect=[{"root": "first"}, {"root": "second"}],
                    ):
                        for expected in ("first", "second"):
                            connection.request("GET", "/api/overview")
                            response = connection.getresponse()
                            if (
                                response.status != HTTPStatus.OK
                                or json.loads(response.read())["root"] != expected
                            ):
                                msg = "Overview requests must read fresh data"
                                raise AssertionError(msg)
                    for route in ("/../main.py", "/.git/config", "/main.py"):
                        connection.request("GET", route)
                        response = connection.getresponse()
                        if response.status != HTTPStatus.NOT_FOUND:
                            msg = "The GUI must not serve checkout files"
                            raise AssertionError(msg)
                        response.read()
                    connection.request("POST", "/api/overview", b"change")
                    response = connection.getresponse()
                    if response.status != HTTPStatus.NOT_IMPLEMENTED:
                        msg = "GUI requests must not write repository data"
                        raise AssertionError(msg)
                    response.read()
                    with patch.object(
                        app,
                        "gui_data",
                        side_effect=ValueError("Invalid repository"),
                    ):
                        connection.request("GET", "/api/overview")
                        response = connection.getresponse()
                        if (
                            response.status != HTTPStatus.INTERNAL_SERVER_ERROR
                            or json.loads(response.read())
                            != {"error": "Invalid repository"}
                        ):
                            msg = "Repository errors must return readable JSON"
                            raise AssertionError(msg)
                finally:
                    connection.close()
                    server.shutdown()
                    thread.join(timeout=5)

    def test_gui_preserves_shared_resource_details_and_changes(self) -> None:
        """Both representations use the same machine, resources, and change colors."""
        root = Path("/workspace")
        nodes = [
            app.TreeNode(
                "packages/sample",
                [
                    app.TreeNode(
                        "Tests",
                        [
                            app.TreeNode("+ test new", change="added"),
                            app.TreeNode("- test old", change="removed"),
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
            patch.object(app.RepositoryBrowser, "package_entries", return_value=nodes),
        ):
            snapshot = app.gui_data(root)
        records = {record["id"]: record for record in snapshot["nodes"]}
        repository = snapshot["tree"][0]
        package = repository["children"][0]
        if package != records[".:packages/sample"]["tree"]:
            msg = "Package details must match their resource record"
            raise AssertionError(msg)
        if (
            records[".:hosts/laptop"]["icon"] != "nixos"
            or len(repository["children"]) != TEST_REPOSITORY_FIELDS
        ):
            msg = "Hosts and checks must appear alongside packages"
            raise AssertionError(msg)
        file = next(
            child for child in package["children"] if child["title"] == "test_main.py"
        )
        leaves = file["children"][0]["children"]
        if [node["change"] for node in leaves] != ["added", "removed"]:
            msg = "Nested changes must retain their addition and removal status"
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

    def test_removed_terminal_flag_is_rejected(self) -> None:
        """Legacy renderer selection cannot select a removed implementation."""
        with pytest.raises(SystemExit) as error:
            app.main(["--gui"])
        if error.value.code != TEST_PARSER_ERROR:
            msg = "Removed mode flags must be rejected"
            raise AssertionError(msg)


class TestRepositoryData(unittest.TestCase):  # noqa: D101
    def test_package_details_belong_to_their_source_files(self) -> None:
        """Keep arguments, tests, documentation, and suppression diffs under sources."""
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary)
            (package / "main.py").write_text('"""Example."""\n# noqa\n')
            (package / "prm").mkdir()
            (package / "prm/script.js").write_text(
                "// eslint-disable-next-line\nrun();\n",
            )
            arguments = app.TreeNode(
                "Arguments",
                [app.TreeNode("+ --help", change="added")],
            )
            tests = app.TreeNode(
                "Tests",
                [app.TreeNode("- test old", change="removed")],
            )
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
                                [
                                    app.TreeNode(
                                        "noqa (global): 1 → 2",
                                        change="modified",
                                    ),
                                ],
                            ),
                            app.TreeNode(
                                "test_main.py",
                                [
                                    app.TreeNode(
                                        "type: ignore (local): 1 → 0",
                                        change="removed",
                                    ),
                                ],
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
            if main["noqa (global): 1 → 2"].change != TEST_MODIFIED_CHANGE:
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
            changed = app.RepositoryBrowser.changed_nodes([tree])[0]
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

    def test_empty_groups_are_hidden_but_diagnostics_and_removals_remain(self) -> None:
        """Placeholder declarations are neither entries nor semantic changes."""
        empty = (
            "Name: sample\nArguments:\n  (not applicable)\n"
            "Dependencies:\n  (not declared)\nTests:\n  (none)\n"
            "Suppressions:\n  (none)\n"
        )
        for tree in (
            app.RepositoryBrowser.summary_tree(
                empty,
                cli=[CliEntry((), "(not applicable)")],
            ),
            app.RepositoryBrowser.merged_summary_tree(empty, empty),
        ):
            if [node.title for node in tree] != ["Name: sample"]:
                msg = "Empty declaration groups must be omitted in both views"
                raise AssertionError(msg)
        previous = empty.replace("Tests:\n  (none)", "Tests:\n  test existing")
        changed = app.RepositoryBrowser.merged_summary_tree(previous, empty)
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
            for node in app.RepositoryBrowser.summary_tree(diagnostic)
            if node.title == "Tests"
        )
        if not app.RepositoryBrowser.has_warning(tests):
            msg = "Unavailable analysis must remain visible as a diagnostic"
            raise AssertionError(msg)

    def test_diff_only_omits_unchanged_fields_and_keeps_command_ancestors(self) -> None:
        """Filtering removes unchanged details while retaining nested change context."""
        before = (
            "Name: sample\nDescription: Before\nArguments:\n"
            "  --same\nTests:\n  test same"
        )
        after = before.replace("Before", "After")
        nodes = app.RepositoryBrowser.merged_summary_tree(before, after)
        nodes.append(
            app.TreeNode(
                "command",
                [app.TreeNode("--same"), app.TreeNode("+ --new", change="added")],
            ),
        )
        changes = app.RepositoryBrowser.changed_nodes(nodes)
        if [node.title for node in changes] != [
            "- Description: Before",
            "+ Description: After",
            "command",
        ] or [node.title for node in changes[-1].children or []] != ["+ --new"]:
            msg = "Diff-only trees must contain changes and their command ancestors"
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
        summary = app.RepositoryBrowser.package_summary("sample", files)
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

    def test_current_package_view_uses_canonical_overview(self) -> None:  # noqa: D102
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            viewer = app.RepositoryBrowser(root)
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

    def test_suppression_diff_changes_counts_without_repeating_labels(self) -> None:
        """Include bare and tagged type ignores, excluding string contents."""
        before = app.RepositoryBrowser.package_summary(
            "sample",
            {
                "main.py": "value = 1  # type: ignore[assignment]\n# noqa\n",
                "test_main.py": "# type: ignore\n",
            },
        )
        after = app.RepositoryBrowser.package_summary(
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
        tree = app.RepositoryBrowser.merged_summary_tree(before, after)
        group = next(node for node in tree if node.title == "Suppressions")
        files = {node.title: node for node in group.children or []}
        if list(files) != ["main.py", "test_main.py"]:
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
        changes = app.RepositoryBrowser.changed_nodes([group])[0]
        changed_files = {node.title: node for node in changes.children or []}
        if [node.title for node in changed_files["main.py"].children or []] != [
            "type: ignore (local): 1 → 2",
        ] or "test_main.py" not in changed_files:
            msg = "Diff-only views must keep file parents and omit unchanged counts"
            raise AssertionError(msg)

    def test_suppression_counts_appear_in_overview_and_diff(self) -> None:
        """Keep suppression counts visible in the shared package summary."""
        before = app.RepositoryBrowser.package_summary(
            "sample",
            {"index.html": "<main></main>\n"},
        )
        after = app.RepositoryBrowser.package_summary(
            "sample",
            {
                "index.html": (
                    "<!-- html-validate-disable -->\n"
                    "<!-- html-validate-disable-next -->\n"
                ),
            },
        )
        tree = app.RepositoryBrowser.summary_tree(after)
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
        diff = app.RepositoryBrowser.merged_summary_tree(before, after)
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
            viewer = app.RepositoryBrowser(root)
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

    def test_dependencies_appear_in_shared_overview_and_inline_changes(self) -> None:
        """Show dependency declarations and edits using the shared summary."""
        before = app.RepositoryBrowser.package_summary(
            "consumer",
            {
                "default.nix": (
                    "{inputs, system, ...}: { buildInputs = ["
                    "inputs.self.packages.${system}.core]; }"
                ),
            },
        )
        after = app.RepositoryBrowser.package_summary(
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
            for node in app.RepositoryBrowser.summary_tree(after)
            if node.title == "Dependencies"
        )
        if [node.title for node in dependencies.children or []] != [
            "build: packages/engine",
        ]:
            raise AssertionError(dependencies)
        for render in (app.RepositoryBrowser.merged_summary_tree,):
            changes = next(
                node for node in render(before, after) if node.title == "Dependencies"
            )
            if {node.title for node in changes.children or []} != {
                "- build: packages/core",
                "+ build: packages/engine",
            }:
                raise AssertionError(changes)

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
            viewer = app.RepositoryBrowser(root)
            entry = viewer.package_entry(root, "sample", diff=False)
            if entry is None:
                msg = "Expected a package entry"
                raise AssertionError(msg)
            if not viewer.has_warning(entry):
                msg_0 = "Unavailable CLI must retain its diagnostic"
                raise AssertionError(msg_0)
            (package / "main.py").write_text(
                "import argparse\nparser = argparse.ArgumentParser()\n",
                encoding="utf-8",
            )
            fixed = viewer.package_entry(root, "sample", diff=False)
            if fixed is None or viewer.has_warning(fixed):
                msg = "Warning must clear when the CLI summary becomes available"
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
        summary = app.RepositoryBrowser.package_summary("sample", files)
        tests = app.RepositoryBrowser.summary_group(summary, "Tests")
        if tests != ["test top level", "test method case"]:
            msg = f"Unexpected canonical test names: {tests!r}"
            raise AssertionError(msg)

    def test_resolved_cli_diagnostic_does_not_warn_in_inline_history(self) -> None:
        """Keep removed diagnostics visible without marking a fixed package."""
        before = "def main():\n    pass\n"
        after = (
            "import argparse\ndef parser():\n"
            "    return argparse.ArgumentParser()\n"
            "def main():\n    parser().parse_args()\n"
        )
        tree = app.RepositoryBrowser.merged_summary_tree(
            app.RepositoryBrowser.package_summary("sample", {"main.py": before}),
            app.RepositoryBrowser.package_summary("sample", {"main.py": after}),
            previous_cli=source_cli_overview(before),
            current_cli=source_cli_overview(after),
        )
        entry = app.TreeNode("packages/sample", tree)
        if app.RepositoryBrowser.has_warning(entry):
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
            app.RepositoryBrowser.cli_tree(
                source_cli_overview(before),
                previous=source_cli_overview(after),
            ),
        )
        if not app.RepositoryBrowser.has_warning(current):
            msg = "New diagnostics must still mark the package as unavailable"
            raise AssertionError(msg)

    def test_package_summary_uses_canonical_cli_parser(self) -> None:  # noqa: D102
        source = (
            "import argparse\n"
            "parser = argparse.ArgumentParser()\n"
            "commands = parser.add_subparsers()\n"
            "build = commands.add_parser('build')\n"
            "build.add_argument('--jobs', type=int, default=2, help='Worker count')\n"
        )
        arguments = app.RepositoryBrowser.summary_group(
            app.RepositoryBrowser.package_summary("sample", {"main.py": source}),
            "Arguments",
        )
        if arguments != [
            "build: command",
            "build: --jobs  optional; default=2; type=int; help='Worker count'",
        ]:
            msg = f"Unexpected canonical CLI summary: {arguments!r}"
            raise AssertionError(msg)

    def test_cli_parameters_belong_to_nested_commands(self) -> None:
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
            viewer = app.RepositoryBrowser(root)
            entry = viewer.package_entries()[0]
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

    def test_nested_cli_diff_keeps_changes_under_their_commands(self) -> None:
        """Show changed parameters and removed commands at their original paths."""
        before = [
            CliEntry(("test",), "command", command=True),
            CliEntry(("test", "coverage"), "command", command=True),
            CliEntry(("test", "coverage"), "--jobs  default=2"),
            CliEntry(("retired",), "command", command=True),
        ]
        after = [*before[:2], CliEntry(("test", "coverage"), "--jobs  default=4")]
        tree = app.RepositoryBrowser.cli_tree(after, previous=before)
        test = next(node for node in tree if node.title == "test")
        coverage = (test.children or [])[0]
        if [(node.title, node.change) for node in coverage.children or []] != [
            ("- --jobs  default=2", "removed"),
            ("+ --jobs  default=4", "added"),
        ]:
            msg = "Changed CLI parameters must retain their path and colors"
            raise AssertionError(msg)
        retired = next(node for node in tree if node.title == "- retired")
        if (retired.title, retired.change) != ("- retired", "removed"):
            msg = "Removed commands must retain removal styling"
            raise AssertionError(msg)

    def test_high_level_diff_compares_summaries_not_source_code(self) -> None:  # noqa: D102
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
            viewer = app.RepositoryBrowser(root)
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
            if "packages/added" not in entries or "packages/removed" not in entries:
                msg = "High-level diff must include added and removed packages"
                raise AssertionError(msg)
            if any("value =" in node.title for node in sample_diff.children or []):
                msg = "High-level diff must omit source-code changes"
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
            viewer = app.RepositoryBrowser(root)
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
            viewer = app.RepositoryBrowser(root)
            overview = viewer.package_entries()
            if not overview or overview[0].title != "packages/same":
                msg = "The regular high-level view must retain unchanged summaries"
                raise AssertionError(msg)
            if viewer.package_entries(diff=True):
                msg = "Source-only changes must not appear in a high-level diff"
                raise AssertionError(msg)

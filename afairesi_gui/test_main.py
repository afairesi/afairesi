# Copyright (c) 2026- Paschalis Bizopoulos
"""Exercise read-only inspection, interface diffs, HTTP boundaries, and SSH."""

from __future__ import annotations

import http.client
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from typing import TYPE_CHECKING

import pytest

import afairesi_gui.main as subject

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

REMOTE_REVISION = "0123456789abcdef0123456789abcdef01234567"


def expect(condition: bool, detail: object) -> None:  # noqa: FBT001
    """Report a violated behavior without relying on optimized-away assertions."""
    if not condition:
        raise AssertionError(detail)


def wait_for(predicate: Callable[[], bool]) -> None:
    """Wait briefly for an asynchronous process or protocol event."""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    message = "Timed out waiting for the expected event"
    raise AssertionError(message)


def git(root: Path, *arguments: str) -> str:
    """Run Git against isolated test repositories."""
    return subprocess.run(  # noqa: S603 - Fixed Git executable with test-owned argument vectors.
        ["git", "-C", str(root), *arguments],  # noqa: S607
        text=True,
        capture_output=True,
        check=True,
        timeout=10,
    ).stdout.strip()


def initialize(root: Path) -> None:
    """Create a repository without depending on user commit configuration."""
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "--quiet")
    git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "--allow-empty",
        "--quiet",
        "-m",
        "initial",
    )


def register(home: Path, child: Path) -> None:
    """Register a checkout by its actual Git index entry and .gitmodules path."""
    relative = str(child.relative_to(home))
    git(home, "config", "--file", ".gitmodules", f"submodule.{relative}.path", relative)
    git(
        home,
        "config",
        "--file",
        ".gitmodules",
        f"submodule.{relative}.url",
        "https://example.invalid/repo.git",
    )
    git(home, "add", "--force", ".gitmodules")
    git(
        home,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{git(child, 'rev-parse', 'HEAD')},{relative}",
    )


@pytest.fixture
def workspace(tmp_path: Path) -> subject.Workspace:
    """Provide one registered package repository and an unrelated checkout."""
    initialize(tmp_path)
    child = tmp_path / "projects" / "example"
    initialize(child)
    (child / "flake.nix").write_text("{}")
    package = child / "hello"
    package.mkdir()
    (package / "default.nix").write_text(
        "{ inputs, pkgs, ... }: inputs.self.lib.mkPythonPackage { "
        "inherit pkgs; executable = true; src = ./.; "
        'meta.description = "Hello"; }'
    )
    (package / "main.py").write_text(
        "import argparse\ndef main():\n    parser = argparse.ArgumentParser()\n"
        '    parser.add_argument("--name", default="world")\n'
        "    parser.parse_args()\n"
    )
    (package / "test_main.py").write_text("def test_hello():\n    pass\n")
    git(child, "add", "--force", ".")
    register(tmp_path, child)
    initialize(tmp_path / "unregistered")
    return subject.Workspace(tmp_path)


def test_discovery_is_exactly_one_level_of_registered_gitlinks(
    workspace: subject.Workspace,
) -> None:
    """Exclude unregistered checkouts, nested submodules, and declaration-only paths."""
    home = workspace.home
    child = home / "projects/example"
    nested = child / "nested"
    initialize(nested)
    register(child, nested)
    git(
        home,
        "config",
        "--file",
        ".gitmodules",
        "submodule.unregistered.path",
        "unregistered",
    )
    entries = workspace.repositories()
    expect([entry["id"] for entry in entries] == ["projects/example"], entries)
    expect(entries[0]["initialized"], entries)
    for path in ("unregistered", "projects/example/nested", "../outside", "/outside"):
        with pytest.raises(ValueError, match="direct registered"):
            workspace.repository(path)


def test_deinitialized_submodule_remains_visible(workspace: subject.Workspace) -> None:
    """Keep registered repositories in the catalog when their checkout is absent."""
    home = workspace.home
    child = home / "projects/example"
    child.rename(home / "parked")
    entries = workspace.repositories()
    expect(len(entries) == 1 and not entries[0]["initialized"], entries)
    with pytest.raises(ValueError, match="not initialized"):
        workspace.repository("projects/example")


def test_inspection_reads_interfaces_without_executing_or_modifying_source(
    workspace: subject.Workspace,
) -> None:
    """Read CLI/test data without importing sources or changing repository state."""
    source = workspace.home / "projects/example/hello/main.py"
    source.write_text(source.read_text() + '\nraise RuntimeError("must not execute")\n')
    before = {
        path: path.read_bytes()
        for path in (
            workspace.home / ".gitmodules",
            workspace.home / ".git/index",
            source,
        )
    }
    report = workspace.inspect("projects/example")
    expect(
        all(path.read_bytes() == content for path, content in before.items()),
        "workspace read changed tracked state",
    )
    expect(report["resources"]["hello"]["tests"] == ["test_hello"], report)
    expect(
        report["resources"]["hello"]["cli"]["arguments"][0]["names"] == ["--name"],
        report,
    )
    expect(report["dirty"], report)
    source.write_text("invalid python !")
    report = workspace.inspect("projects/example")
    expect(bool(report["inspection_error"]) and not report["resources"], report)


def test_interface_diff_separates_staged_and_unstaged_changes(
    workspace: subject.Workspace,
) -> None:
    """Use the actual CLI for both comparisons without changing the checkout."""
    root = workspace.home / "projects/example"
    git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "--quiet",
        "-m",
        "baseline",
    )
    source = root / "hello/main.py"
    source.write_text(source.read_text().replace('default="world"', 'default="staged"'))
    git(root, "add", "--force", "hello/main.py")
    source.write_text(
        source.read_text().replace('default="staged"', 'default="working"')
    )
    index = (root / ".git/index").read_bytes()
    content = source.read_bytes()
    changes = workspace.interface_changes("projects/example")
    for name, before, after in (
        ("working", "staged", "working"),
        ("staged", "world", "staged"),
    ):
        patch = changes[name]["patch"]
        for operation, value in (("remove", before), ("add", after)):
            expect(
                {
                    "op": operation,
                    "path": "/hello/cli/arguments/0/default",
                    "value": value,
                }
                in patch,
                patch,
            )
    expect((root / ".git/index").read_bytes() == index, "diff changed the index")
    expect(source.read_bytes() == content, "diff changed working sources")


@pytest.mark.parametrize("malformed", [False, True])
def test_interface_diff_ignores_implementation_changes_and_reports_parse_errors(
    workspace: subject.Workspace, *, malformed: bool
) -> None:
    """Ignore implementation-only edits and expose inspection errors per comparison."""
    source = workspace.home / "projects/example/hello/main.py"
    source.write_text(
        source.read_text()
        + ("\ninvalid python !\n" if malformed else "\ndef internal(): return 42\n")
    )
    changes = workspace.interface_changes("projects/example")
    working = changes["working"]
    expect(
        bool(working.get("error")) if malformed else working["patch"] == [],
        changes,
    )
    expect(bool(changes["staged"]["patch"]), changes)


def test_interface_diff_cache_tracks_working_index_and_head_changes(
    workspace: subject.Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Share concurrent inspections and invalidate each actual comparison input."""
    calls = []
    comparisons = 2
    original: Callable[..., str] = subject.command

    def tracked(
        arguments: list[str], root: Path, *, input_text: str | None = None
    ) -> str:
        if arguments[:2] == ["afairesi", "diff"]:
            calls.append(arguments)
        return original(arguments, root, input_text=input_text)

    monkeypatch.setattr(subject, "command", tracked)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(workspace.interface_changes, ["projects/example"] * 2))
    expect(len(calls) == comparisons and results[0] == results[1], calls)
    results[0]["working"]["patch"].append({"op": "remove", "path": "/hello"})
    expect(
        workspace.interface_changes("projects/example")["working"]["patch"] == [],
        results,
    )
    expect(len(calls) == comparisons, calls)
    calls.clear()
    root = workspace.home / "projects/example"
    source = root / "hello/main.py"
    before = source.stat()
    source.write_text(source.read_text().replace('default="world"', 'default="earth"'))
    os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
    result = workspace.interface_changes("projects/example")
    expect(
        {
            "op": "add",
            "path": "/hello/cli/arguments/0/default",
            "value": "earth",
        }
        in result["working"]["patch"]
        and len(calls) == comparisons,
        result,
    )
    calls.clear()
    git(root, "add", "--force", "hello/main.py")
    result = workspace.interface_changes("projects/example")
    expect(result["working"]["patch"] == [] and len(calls) == comparisons, result)
    calls.clear()
    git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "--quiet",
        "-m",
        "interfaces",
    )
    result = workspace.interface_changes("projects/example")
    expect(
        all(item["patch"] == [] for item in result.values()),
        result,
    )
    expect(len(calls) == comparisons, calls)


def test_diff_versions_preserve_native_array_edits_and_deleted_resources(
    tmp_path: Path,
) -> None:
    """Reconstruct both baselines from native jd patches, including shifted arrays."""
    from pathlib import Path  # noqa: PLC0415 - Runtime path for the browser module.

    before = {
        "runner": {
            "type": "python",
            "description": "Before",
            "tests": ["test_removed", "test_kept"],
            "cli": {
                "arguments": [{"names": ["--old"]}, {"names": ["--port"], "default": 1}]
            },
            "dependencies": [{"resource": "removed", "kind": "runtime"}],
        },
        "removed": {"type": "python"},
        "special/~": {"type": "python", "description": "before"},
    }
    staged = json.loads(json.dumps(before))
    staged["runner"]["description"] = "Staged"
    staged["runner"]["cli"]["arguments"][1]["default"] = 2
    staged["runner"]["tests"] = ["test_kept", "test_staged"]
    staged["new"] = {"type": "nix"}
    working = json.loads(json.dumps(staged))
    working["runner"]["description"] = "Working"
    working["runner"]["cli"]["arguments"] = [
        {"names": ["--port"], "default": 3},
        {"names": ["--new"]},
    ]
    working["runner"]["tests"] = ["test_working", "test_kept"]
    working["runner"]["dependencies"] = [{"resource": "new", "kind": "build"}]
    working["special/~"]["description"] = "after"
    del working["removed"]
    paths = [tmp_path / name for name in ("before.json", "index.json", "working.json")]
    for path, value in zip(paths, (before, staged, working), strict=True):
        path.write_text(json.dumps(value))
    interfaces = {}
    for name, left, right in (
        ("staged", paths[0], paths[1]),
        ("working", paths[1], paths[2]),
    ):
        result = subprocess.run(  # noqa: S603 - Native diff over test-owned snapshots.
            ["jd", str(left), str(right)],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        expect(result.returncode == 1, result.stderr)
        interfaces[name] = {
            "patch": json.loads(
                subject.command(
                    ["jd", "-t", "jd2patch"], tmp_path, input_text=result.stdout
                )
            ),
        }
    interfaces["working"]["resources"] = working
    module = Path(subject.__file__).with_name("differences.js").as_uri()
    script = f"""
        import {{
            repositoryVersions, differences, versionEntries, equal, reversePatch,
            changeKind
        }} from {json.dumps(module)};
        import {{ readFileSync }} from 'node:fs';
        const payload = JSON.parse(readFileSync(0, 'utf8'));
        const view = repositoryVersions(payload);
        const tests = versionEntries(view, root => root.runner.tests, name => name);
        const argumentsList = versionEntries(view,
            root => root.runner.cli.arguments, argument => argument.names[0]);
        let staleRejected = false;
        try {{ reversePatch({{a: 2}}, [{{op: 'add', path: '/a', value: 1}}]); }}
        catch {{ staleRejected = true; }}
        const special = reversePatch({{}}, [{{
            op:'remove',path:'/__proto__',value:{{polluted:true}}
        }}]);
        process.stdout.write(JSON.stringify({{
            view,
            tests: tests.map(entry => ({{
                key: entry.key, kind: changeKind(entry.changes)
            }})),
            argumentsList: argumentsList.map(entry => ({{
                key: entry.key, kind: changeKind(entry.changes)
            }})),
            changes: differences(view, root => root.runner.description),
            unchanged: differences(view, root => root.runner.type),
            orderIndependent: equal({{a:1,b:2}},{{b:2,a:1}}),
            staleRejected,
            safe: Object.hasOwn(special, '__proto__') && !({{}}).polluted,
        }}));
    """
    completed = subprocess.run(  # noqa: S603 - Exercise the shipped browser module with native patches.
        ["node", "--input-type=module", "-e", script],  # noqa: S607
        input=json.dumps(interfaces),
        text=True,
        capture_output=True,
        check=True,
        timeout=10,
    )
    result = json.loads(completed.stdout)
    expect(
        result["view"] == {"head": before, "index": staged, "working": working}, result
    )
    expect(
        result["changes"]
        == [
            {"before": "Before", "after": "Staged", "stage": "staged"},
            {"before": "Staged", "after": "Working", "stage": "unstaged"},
        ],
        result,
    )
    expect(result["unchanged"] == [], result)
    expect(
        {entry["key"]: entry["kind"] for entry in result["tests"]}
        == {
            "test_working": "add",
            "test_kept": "",
            "test_staged": "remove",
            "test_removed": "remove",
        },
        result,
    )
    expect(
        {entry["key"]: entry["kind"] for entry in result["argumentsList"]}
        == {"--port": "modify", "--new": "add", "--old": "remove"},
        result,
    )
    expect(
        result["orderIndependent"] and result["staleRejected"] and result["safe"],
        result,
    )


@pytest.fixture
def server(workspace: subject.Workspace) -> Iterator[ThreadingHTTPServer]:
    """Run the actual HTTP router against the temporary home."""
    with ThreadingHTTPServer(("127.0.0.1", 0), subject.handler(workspace)) as httpd:
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            yield httpd
        finally:
            httpd.shutdown()
            thread.join(timeout=5)


def request(
    server: ThreadingHTTPServer,
    method: str,
    path: str,
    headers: dict[str, str] | None = None,
) -> tuple[int, bytes]:
    """Send an HTTP request to the test server."""
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
    try:
        connection.request(
            method, path, body="{}" if method == "POST" else None, headers=headers or {}
        )
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def test_http_serves_only_assets_and_rejects_foreign_origins(
    server: ThreadingHTTPServer,
) -> None:
    """Serve inspection only and keep repository data within the local origin."""
    status, body = request(server, "GET", "/api/workspace")
    expect(status == HTTPStatus.OK and len(json.loads(body)["repositories"]) == 1, body)
    status, body = request(server, "GET", "/api/diff?repository=projects/example")
    interfaces = json.loads(body)
    expect(
        status == HTTPStatus.OK
        and interfaces["working"]["patch"] == []
        and "hello" in interfaces["working"]["resources"]
        and bool(interfaces["staged"]["patch"]),
        interfaces,
    )
    status, body = request(server, "GET", "/")
    expect(status == HTTPStatus.OK and b"afairesi" in body, status)
    expect(
        request(server, "GET", "/.git/config")[0] == HTTPStatus.NOT_FOUND,
        "served Git files",
    )
    expect(
        request(server, "GET", "/api/workspace", {"Host": "attacker.invalid"})[0]
        == HTTPStatus.FORBIDDEN,
        "accepted foreign host",
    )
    expect(
        request(
            server,
            "GET",
            "/api/workspace",
            {"Origin": "https://attacker.invalid"},
        )[0]
        == HTTPStatus.FORBIDDEN,
        "accepted foreign origin",
    )
    expect(
        request(server, "POST", "/api/workspace", {"Content-Type": "application/json"})[
            0
        ]
        == HTTPStatus.METHOD_NOT_ALLOWED,
        "accepted mutation request",
    )
    expect(
        request(server, "GET", "/api/unknown")[0] == HTTPStatus.NOT_FOUND,
        "accepted unknown route",
    )


def test_ssh_arguments_preserve_remote_paths_and_loopback_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pass remote paths literally and retain the HTTP server's origin checks."""
    import argparse  # noqa: PLC0415 - Only used to supply launcher options.
    import shlex  # noqa: PLC0415 - Decode the command passed to the remote shell.
    from pathlib import Path  # noqa: PLC0415 - Runtime paths in launcher options.

    options = argparse.Namespace(
        ssh="user@host-alias",
        home=Path("/remote/home with 'quotes' $(touch unwanted)"),
        port=8766,
        remote_revision=REMOTE_REVISION.upper(),
    )
    arguments, url = subject.ssh_arguments(options)
    expect(arguments[:4] == ["ssh", "-T", "-S", "none"], arguments)
    expect("ExitOnForwardFailure=yes" in arguments, arguments)
    expect(
        arguments[arguments.index("-L") + 1] == "127.0.0.1:8766:127.0.0.1:8766",
        arguments,
    )
    expect(arguments[-3:-1] == ["--", options.ssh], arguments)
    expect(url == "http://127.0.0.1:8766", url)
    expect(
        shlex.split(arguments[-1])
        == [
            "env",
            "AFAIRESI_GUI_SSH_SESSION=1",
            "nix",
            "--extra-experimental-features",
            "nix-command flakes",
            "run",
            f"github:afairesi/afairesi/{REMOTE_REVISION}#afairesi_gui",
            "--",
            "--no-open",
            "--port",
            "8766",
            "--home",
            str(options.home),
        ],
        arguments,
    )
    options.home = None
    options.port = 0
    options.remote_revision = None
    monkeypatch.setattr(subject, "SOURCE_REVISION", REMOTE_REVISION)
    arguments, url = subject.ssh_arguments(options)
    remote = shlex.split(arguments[-1])
    expect("--home" not in remote, remote)
    expect(f"github:afairesi/afairesi/{REMOTE_REVISION}#afairesi_gui" in remote, remote)
    port = remote[remote.index("--port") + 1]
    expect(int(port) > 0 and url == f"http://127.0.0.1:{port}", arguments)


@pytest.mark.parametrize("revision", [None, "main", "0123456", "x" * 40])
def test_ssh_rejects_unpinned_development_builds(
    monkeypatch: pytest.MonkeyPatch, revision: str | None
) -> None:
    """Never silently substitute the latest release for a development checkout."""
    import argparse  # noqa: PLC0415 - Launcher options for this contract test.

    monkeypatch.setattr(subject, "SOURCE_REVISION", "")
    with pytest.raises(ValueError, match="--remote-revision"):
        subject.ssh_arguments(argparse.Namespace(remote_revision=revision))


@pytest.mark.parametrize("exit_code", [0, 255])
def test_ssh_startup_failure_never_opens_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exit_code: int
) -> None:
    """Do not open a dashboard when SSH fails or no server announces readiness."""
    import argparse  # noqa: PLC0415 - Launcher options for this contract test.

    executable = tmp_path / "ssh"
    executable.write_text(f"#!{sys.executable}\nraise SystemExit({exit_code})\n")
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    opened: list[str] = []
    monkeypatch.setattr(subject.webbrowser, "open", opened.append)
    with pytest.raises(ValueError, match="Remote workspace exited"):
        subject.remote_workspace(
            argparse.Namespace(
                ssh="test-host",
                home=None,
                port=0,
                no_open=False,
                remote_revision=REMOTE_REVISION,
            )
        )
    expect(opened == [], opened)


def check_remote_workspace(url: str, home: Path) -> None:
    """Check remote inspection and reject local and foreign mutation requests."""
    port = int(url.rsplit(":", 1)[-1])
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("GET", "/api/workspace")
        response = connection.getresponse()
        result = json.loads(response.read())
        expect(response.status == HTTPStatus.OK, result)
        expect(result["home"] == str(home), result)
        expect(
            [repo["id"] for repo in result["repositories"]] == ["projects/example"],
            result,
        )
        connection.request("GET", "/api/repository?repository=projects/example")
        response = connection.getresponse()
        result = json.loads(response.read())
        expect(
            response.status == HTTPStatus.OK and "hello" in result["resources"], result
        )
        connection.request(
            "POST",
            "/api/workspace",
            body="{}",
            headers={"Content-Type": "application/json", "Origin": url},
        )
        response = connection.getresponse()
        expect(response.status == HTTPStatus.METHOD_NOT_ALLOWED, response.read())
        connection.request(
            "POST",
            "/api/workspace",
            body="{}",
            headers={
                "Content-Type": "application/json",
                "Origin": "https://foreign.invalid",
            },
        )
        response = connection.getresponse()
        expect(response.status == HTTPStatus.FORBIDDEN, response.read())
    finally:
        connection.close()


@pytest.mark.parametrize("explicit_home", [False, True])
def test_ssh_launcher_uses_remote_home_and_stops_its_server(
    workspace: subject.Workspace,
    monkeypatch: pytest.MonkeyPatch,
    *,
    explicit_home: bool,
) -> None:
    """Run Nix and the real server through simulated SSH without installing the GUI."""
    import signal  # noqa: PLC0415 - Interrupt only the test-owned launcher.
    from pathlib import Path  # noqa: PLC0415 - Locate the shipped entry point.

    scratch = workspace.home / "ssh-test"
    scratch.mkdir()
    executable = scratch / "ssh"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import os, shlex, sys\nfrom pathlib import Path\n"
        "Path(os.environ['SSH_TEST_PID']).write_text(str(os.getpid()))\n"
        "command = shlex.split(sys.argv[-1])\n"
        "assert command[:2] == ['env', 'AFAIRESI_GUI_SSH_SESSION=1']\n"
        "os.environ['AFAIRESI_GUI_SSH_SESSION'] = '1'\n"
        "os.environ['HOME'] = os.environ['SSH_TEST_HOME']\n"
        f"source = command.index('github:afairesi/afairesi/{REMOTE_REVISION}"
        "#afairesi_gui')\n"
        "command[source] = 'path:' + os.environ['SSH_TEST_FLAKE'] + '#afairesi_gui'\n"
        "os.execvp(command[2], command[2:])\n"
    )
    executable.chmod(0o755)
    flake = scratch / "remote-flake"
    flake.mkdir()
    (flake / "flake.nix").write_text("""{
      outputs = { self }: {
        apps = builtins.listToAttrs (map (system: {
          name = system;
          value.afairesi_gui = { type = "app"; program = "${./launch}"; };
        }) [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ]);
      };
    }""")
    entry = flake / "launch"
    entry.write_text(
        f"#!{sys.executable}\nimport afairesi_gui.main as app\napp.main()\n"
    )
    entry.chmod(0o755)
    for name, value in {
        "PATH": f"{scratch}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(scratch / "not-a-local-repository"),
        "SSH_TEST_HOME": str(workspace.home if not explicit_home else scratch),
        "SSH_TEST_PID": str(scratch / "server.pid"),
        "SSH_TEST_FLAKE": str(flake),
        "NIX_REMOTE": f"local?store={scratch / 'store'}"
        f"&state={scratch / 'state'}&log={scratch / 'log'}",
        "NIX_CONFIG": "experimental-features =\nbuild-users-group =\n"
        "sandbox = false\nsubstituters =\n",
    }.items():
        monkeypatch.setenv(name, value)
    launcher = [
        sys.executable,
        str(Path(subject.__file__)),
        "--ssh",
        "test-host",
        "--remote-revision",
        REMOTE_REVISION,
        "--no-open",
        "--port",
        "0",
    ]
    if explicit_home:
        launcher.extend(["--home", str(workspace.home)])
    with (
        (scratch / "stdout").open("w") as output,
        subprocess.Popen(  # noqa: S603 - Test-owned launcher and fake SSH executable.
            launcher,
            stdout=output,
            stderr=subprocess.PIPE,
            text=True,
        ) as process,
    ):
        try:
            wait_for(
                lambda: (
                    "Afairesi workspace (test-host):"
                    in (scratch / "stdout").read_text()
                    or process.poll() is not None
                )
            )
            expect(process.poll() is None, (scratch / "stdout").read_text())
            url = (scratch / "stdout").read_text().strip().split()[-1]
            check_remote_workspace(url, workspace.home)
        finally:
            process.send_signal(signal.SIGINT)
            _stdout, errors = process.communicate(timeout=10)
        expect(process.returncode == 0, errors)
    pid = int((scratch / "server.pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)

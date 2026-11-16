# Copyright (c) 2026- Paschalis Bizopoulos
"""Exercise workspace boundaries, HTTP requests, processes, and Codex events."""

from __future__ import annotations

import base64
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
from typing import TYPE_CHECKING, Any

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


def test_inspection_uses_afairesi_report_and_never_executes_source(
    workspace: subject.Workspace,
) -> None:
    """Return actual CLI/test data without importing the inspected package."""
    source = workspace.home / "projects/example/hello/main.py"
    source.write_text(source.read_text() + '\nraise RuntimeError("must not execute")\n')
    report = workspace.inspect("projects/example")
    expect(report["resources"]["hello"]["tests"] == ["test_hello"], report)
    expect(
        report["resources"]["hello"]["cli"]["arguments"][0]["names"] == ["--name"],
        report,
    )
    expect(report["dirty"] and not report["pin_changed"], report)
    source.write_text("invalid python !")
    report = workspace.inspect("projects/example")
    expect(bool(report["inspection_error"]) and not report["resources"], report)


def test_changes_separates_staged_and_unstaged_interface_diffs(
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
    changes = workspace.changes("projects/example")
    for name, before, after in (
        ("working", "staged", "working"),
        ("staged", "world", "staged"),
    ):
        difference = changes["interfaces"][name]["diff"]
        expect(f'- "{before}"' in difference, difference)
        expect(f'+ "{after}"' in difference, difference)
        expect("\x1b" not in difference and '@ ["hello",' in difference, difference)
        expect("diff --git" in changes[name], changes)
    expect((root / ".git/index").read_bytes() == index, "diff changed the index")
    expect(source.read_bytes() == content, "diff changed working sources")


@pytest.mark.parametrize("malformed", [False, True])
def test_changes_keeps_file_diffs_when_interfaces_are_unchanged_or_invalid(
    workspace: subject.Workspace, *, malformed: bool
) -> None:
    """Keep raw changes visible when an interface diff is empty or fails."""
    source = workspace.home / "projects/example/hello/main.py"
    source.write_text(
        source.read_text()
        + ("\ninvalid python !\n" if malformed else "\ndef internal(): return 42\n")
    )
    changes = workspace.changes("projects/example")
    working = changes["interfaces"]["working"]
    expect(
        bool(working.get("error"))
        if malformed
        else working["diff"] == "" and working["patch"] == [],
        changes,
    )
    expect(bool(changes["interfaces"]["staged"]["diff"]), changes)
    expect("diff --git" in changes["working"], changes)


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
    results[0]["working"]["diff"] = "modified by caller"
    expect(
        workspace.interface_changes("projects/example")["working"]["diff"] == "",
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
        '+ "earth"' in result["working"]["diff"] and len(calls) == comparisons, result
    )
    calls.clear()
    git(root, "add", "--force", "hello/main.py")
    result = workspace.interface_changes("projects/example")
    expect(result["working"]["diff"] == "" and len(calls) == comparisons, result)
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
        all(item["diff"] == "" and item["patch"] == [] for item in result.values()),
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
            "diff": result.stdout,
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
            view, tests, argumentsList,
            changes: differences(view, root => root.runner.description),
            unchanged: differences(view, root => root.runner.type),
            transient: changeKind(tests.find(t => t.key === 'test_staged').changes),
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
    expect(result["transient"] == "remove", result)
    expect(
        {entry["key"]: entry["current"] for entry in result["tests"]}
        == {
            "test_working": True,
            "test_kept": True,
            "test_staged": False,
            "test_removed": False,
        },
        result,
    )
    expect(
        {entry["key"]: entry["current"] for entry in result["argumentsList"]}
        == {"--port": True, "--new": True, "--old": False},
        result,
    )
    expect(
        result["orderIndependent"] and result["staleRejected"] and result["safe"],
        result,
    )


def test_action_arguments_are_vectors_and_do_not_use_a_shell(
    workspace: subject.Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep quoted run arguments literal and reject actions before spawning."""
    application = subject.Application(workspace.home)
    captured = []

    def actions(_repository: str, _resource: str) -> dict[str, list[str]]:
        return {"run": ["nix", "run", '.#"hello"']}

    def start(*arguments: object) -> dict[str, Any]:
        captured.append(arguments)
        return {"id": "job"}

    monkeypatch.setattr(application.workspace, "actions", actions)
    monkeypatch.setattr(application.jobs, "start", start)
    application.post(
        "/api/jobs",
        {
            "repository": "projects/example",
            "resource": "hello",
            "action": "run",
            "arguments": "--name 'two words' '$(touch unwanted)'",
        },
    )
    expect(
        captured[0][-1]
        == [
            "nix",
            "run",
            '.#"hello"',
            "--",
            "--name",
            "two words",
            "$(touch unwanted)",
        ],
        captured,
    )
    application.post(
        "/api/jobs",
        {
            "repository": "projects/example",
            "resource": "hello",
            "action": "run",
            "arguments": ["--name", "", "two words", "0", "$(touch unwanted)"],
        },
    )
    argv = captured[-1][-1]
    expect(
        isinstance(argv, list)
        and argv[-5:] == ["--name", "", "two words", "0", "$(touch unwanted)"],
        captured,
    )
    with pytest.raises(TypeError, match="array of strings"):
        application.post(
            "/api/jobs",
            {
                "repository": "projects/example",
                "resource": "hello",
                "action": "run",
                "arguments": [0],
            },
        )
    with pytest.raises(ValueError, match="unavailable"):
        application.post(
            "/api/jobs",
            {"repository": "projects/example", "resource": "hello", "action": "clean"},
        )
    with pytest.raises(ValueError, match="direct registered"):
        application.post(
            "/api/jobs",
            {"repository": "unregistered", "resource": "hello", "action": "run"},
        )
    expect([item[3] for item in captured] == ["run", "run"], captured)


def test_job_streams_before_exit_and_can_be_cancelled(tmp_path: Path) -> None:
    """Observe live stdout and terminate a running process without a shell."""
    jobs = subject.Jobs()
    try:
        job = jobs.start(
            tmp_path,
            "repo",
            "resource",
            "run",
            [sys.executable, "-u", "-c", "import time; print('ready'); time.sleep(30)"],
        )
        wait_for(lambda: "ready" in jobs.snapshot("repo")[0]["output"])
        expect(jobs.snapshot("repo")[0]["status"] == "running", jobs.snapshot("repo"))
        jobs.cancel(job["id"])
        wait_for(lambda: jobs.snapshot("repo")[0]["exit_code"] is not None)
        expect(jobs.snapshot("repo")[0]["status"] == "cancelled", jobs.snapshot("repo"))
        jobs.start(
            tmp_path,
            "other",
            "resource",
            "test",
            [sys.executable, "-c", "raise SystemExit(7)"],
        )
        wait_for(lambda: jobs.snapshot("other")[0]["status"] == "failed")
        expected_failure = 7
        expect(
            jobs.snapshot("other")[0]["exit_code"] == expected_failure,
            jobs.snapshot("other"),
        )
        expect(len(jobs.snapshot("repo")) == 1, jobs.snapshot("repo"))
    finally:
        jobs.close()


@pytest.fixture
def server(workspace: subject.Workspace) -> Iterator[ThreadingHTTPServer]:
    """Run the actual HTTP router against the temporary home."""
    application = subject.Application(workspace.home)
    with ThreadingHTTPServer(("127.0.0.1", 0), subject.handler(application)) as httpd:
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            yield httpd
        finally:
            httpd.shutdown()
            thread.join(timeout=5)
            application.close()


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
    """Keep home files and mutation APIs outside arbitrary browser origins."""
    status, body = request(server, "GET", "/api/workspace")
    expect(status == HTTPStatus.OK and len(json.loads(body)["repositories"]) == 1, body)
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
            "POST",
            "/api/terminal/start",
            {"Content-Type": "application/json", "Origin": "https://attacker.invalid"},
        )[0]
        == HTTPStatus.FORBIDDEN,
        "accepted foreign origin",
    )
    expect(
        request(server, "POST", "/api/terminal/start", {"Content-Type": "text/plain"})[
            0
        ]
        == HTTPStatus.BAD_REQUEST,
        "accepted form-compatible body",
    )


@pytest.fixture
def native_codex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run an actual PTY with a local fixture instead of a paid model or account."""
    executable = tmp_path / "native-codex"
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.setenv("TERMINAL_TEST_LOG", str(tmp_path / "terminal-calls.jsonl"))
    executable.write_text(
        f"#!{sys.executable}\n"
        r"""
import json, os, signal, sys, termios, tty
from pathlib import Path
with Path(os.environ['TERMINAL_TEST_LOG']).open('a') as log:
    log.write(json.dumps(sys.argv[1:]) + '\n')
if sys.argv[1:3] == ['login', 'status']:
    raise SystemExit(int(os.environ.get('TERMINAL_TEST_SIGNED_OUT', '0')))
if sys.argv[1:3] == ['login', '--device-auth']:
    print('https://example.invalid/device TEST-CODE', flush=True)
    raise SystemExit(int(os.environ.get('TERMINAL_TEST_LOGIN_FAILURE', '0')))
tty.setraw(0)
def size(*args):
    print('SIZE=' + str(termios.tcgetwinsize(0)), flush=True)
signal.signal(signal.SIGWINCH, size)
print('READY ' + json.dumps({'cwd': os.getcwd(), 'tty': os.isatty(0),
    'controlling': os.tcgetpgrp(0) == os.getpgrp()}), flush=True)
size()
while True:
    data = os.read(0, 4096)
    if b'EXIT' in data:
        print('FINISHED', flush=True)
        break
    print('BYTES=' + data.hex(), flush=True)
"""
    )
    executable.chmod(0o755)
    return executable


def terminal_output(terminal: subject.Terminal) -> str:
    """Decode the wire format without normalizing escapes or UTF-8."""
    return base64.b64decode(terminal.snapshot()["output"]).decode()


def test_native_terminal_preserves_keys_resize_and_session(
    tmp_path: Path, native_codex: Path
) -> None:
    """Native commands, Unicode, paste, and refresh reach the same controlling PTY."""
    terminal = subject.Terminal(tmp_path, str(native_codex))
    try:
        start = terminal.start(100, 20)
        wait_for(lambda: "READY" in terminal_output(terminal))
        output = terminal_output(terminal)
        expect('"tty": true' in output and '"controlling": true' in output, output)
        expect(str(tmp_path) in output and "SIZE=(20, 100)" in output, output)
        session = start["session"]
        expect(terminal.start(80, 15)["session"] == session, "duplicate process")
        data = "/model\r@main.py\x1b[A\x1b[200~γειά\nworld\x1b[201~".encode()
        terminal.write(base64.b64encode(data).decode(), session)
        wait_for(lambda: data.hex() in terminal_output(terminal))
        terminal.resize(120, 25, session)
        wait_for(lambda: "SIZE=(25, 120)" in terminal_output(terminal))
        snapshot = terminal.snapshot()
        expect(snapshot["reset"] and "READY" in terminal_output(terminal), snapshot)
        expect(terminal.snapshot(snapshot["offset"], session)["output"] == "", snapshot)
        terminal.write(base64.b64encode(b"EXIT").decode(), session)
        wait_for(lambda: not terminal.snapshot()["running"])
        expect("FINISHED" in terminal_output(terminal), terminal.snapshot())
        expect(terminal.snapshot()["exit_code"] == 0, terminal.snapshot())
        restarted = terminal.start(100, 20)
        expect(restarted["session"] != session, restarted)
        with pytest.raises(ValueError, match="session ended"):
            terminal.write(base64.b64encode(b"stale input").decode(), session)
    finally:
        terminal.close()
    expect(
        terminal.process is not None and terminal.process.poll() is not None,
        "left a native process behind",
    )
    expect(
        terminal.reader is not None and not terminal.reader.is_alive(),
        "left the PTY reader behind",
    )


@pytest.mark.parametrize("signed_out", [False, True])
def test_remote_terminal_uses_native_device_sign_in(
    tmp_path: Path,
    native_codex: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    signed_out: bool,
) -> None:
    """Use existing remote login or complete device auth before launching the TUI."""
    monkeypatch.setenv("TERMINAL_TEST_SIGNED_OUT", str(int(signed_out)))
    terminal = subject.Terminal(tmp_path, str(native_codex), remote=True)
    try:
        terminal.start(100, 20)
        wait_for(lambda: "READY" in terminal_output(terminal))
        calls = [
            json.loads(line)
            for line in (tmp_path / "terminal-calls.jsonl").read_text().splitlines()
        ]
        expect(calls[0] == ["login", "status"], calls)
        expect((["login", "--device-auth"] in calls) == signed_out, calls)
        expect("--no-daemon" in calls[-1] and "app-server" not in calls[-1], calls)
    finally:
        terminal.close()


def test_remote_terminal_stops_after_failed_login(
    tmp_path: Path, native_codex: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep failed device login visible without starting the TUI."""
    monkeypatch.setenv("TERMINAL_TEST_SIGNED_OUT", "1")
    failure_code = 7
    monkeypatch.setenv("TERMINAL_TEST_LOGIN_FAILURE", str(failure_code))
    terminal = subject.Terminal(tmp_path, str(native_codex), remote=True)
    try:
        terminal.start(100, 20)
        wait_for(lambda: not terminal.snapshot()["running"])
        expect(terminal.snapshot()["exit_code"] == failure_code, terminal.snapshot())
        expect("TEST-CODE" in terminal_output(terminal), terminal.snapshot())
        expect("READY" not in terminal_output(terminal), terminal.snapshot())
    finally:
        terminal.close()


def test_terminal_http_bounds_and_foreign_origins(
    workspace: subject.Workspace, native_codex: Path
) -> None:
    """Protect terminal mutation endpoints and reject malformed sizes and data."""
    application = subject.Application(workspace.home, str(native_codex))
    with ThreadingHTTPServer(("127.0.0.1", 0), subject.handler(application)) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for path in ["start", "input", "resize"]:
                code, _body = request(
                    server,
                    "POST",
                    f"/api/terminal/{path}",
                    {
                        "Content-Type": "application/json",
                        "Origin": "https://foreign.invalid",
                    },
                )
                expect(code == HTTPStatus.FORBIDDEN, path)
            code, _body = request(
                server,
                "POST",
                "/api/terminal/start",
                {"Content-Type": "application/json"},
            )
            expect(code == HTTPStatus.BAD_REQUEST, "accepted missing dimensions")
            for columns, rows in [(0, 20), (100, 0), (501, 20), (100, 201)]:
                with pytest.raises(ValueError, match="dimensions"):
                    application.terminal.start(columns, rows)
            with pytest.raises(ValueError, match="base64"):
                application.terminal.write("not base64!", "")
        finally:
            server.shutdown()
            thread.join(timeout=5)
            application.close()


def test_source_does_not_modify_home_registration(workspace: subject.Workspace) -> None:
    """Reading the workspace preserves the registry, index, and working sources."""
    root = workspace.home
    before = {
        path: path.read_bytes()
        for path in (
            root / ".gitmodules",
            root / ".git/index",
            root / "projects/example/hello/main.py",
        )
    }
    workspace.repositories()
    workspace.inspect("projects/example")
    expect(
        all(path.read_bytes() == content for path, content in before.items()),
        "workspace read changed tracked state",
    )


def test_argument_controls_round_trip_through_argparse() -> None:
    """Preserve omission, empty strings, zero, repeated flags, and choices."""
    import argparse  # noqa: PLC0415 - Only used by the CLI control contract test.
    from pathlib import Path  # noqa: PLC0415 - Runtime path needed for the browser module.

    arguments = [
        {"names": ["--name"], "default": "fallback"},
        {"names": ["--port"], "type": "int"},
        {"names": ["--dry-run"], "action": "store_true"},
        {"names": ["--quiet"], "action": "store_false"},
        {"names": ["-v"], "action": "count"},
        {"names": ["--color"], "choices": ["red", "blue"]},
        {"names": ["--tag"], "action": "append"},
        {"names": ["--cache"], "action": {"unknown": "argparse.BooleanOptionalAction"}},
        {"names": ["--limit"], "type": "int", "nargs": "?", "const": 5},
        {"names": ["path"]},
    ]
    values = {
        str(index): {"enabled": True, "text": text}
        for index, text in enumerate(
            [
                "",
                "0",
                "",
                "",
                "2",
                "blue",
                "two words\n$(touch unwanted)",
                "false",
                "",
                "a directory",
            ]
        )
    }
    values["8"]["constant"] = True
    command = {"arguments": arguments}
    cases = [
        {"command": command, "values": values},
        {"command": {"arguments": arguments[:1]}, "values": {}},
        {
            "command": {"arguments": [arguments[1]]},
            "values": {"0": {"enabled": True, "text": "1.5"}},
        },
        {
            "command": {"arguments": [arguments[5]]},
            "values": {"0": {"enabled": True, "text": "green"}},
        },
        {
            "command": {
                "arguments": arguments[:2],
                "groups": [{"exclusive": True, "arguments": [["--name"], ["--port"]]}],
            },
            "values": values,
        },
    ]
    module = Path(subject.__file__).with_name("arguments.js").as_uri()
    script = f"""
        import {{ commandArguments }} from {json.dumps(module)};
        import {{ readFileSync }} from 'node:fs';
        const cases = JSON.parse(readFileSync(0, 'utf8'));
        process.stdout.write(JSON.stringify(cases.map(item => {{
            try {{ return {{ argv: commandArguments(item.command, item.values) }}; }}
            catch (error) {{ return {{ error: error.message }}; }}
        }})));
    """
    result = subprocess.run(  # noqa: S603 - Test-authored JavaScript and metadata, no shell.
        ["node", "--input-type=module", "-e", script],  # noqa: S607
        input=json.dumps(cases),
        text=True,
        capture_output=True,
        check=True,
        timeout=10,
    )
    results = json.loads(result.stdout)
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default="fallback")
    parser.add_argument("--port", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--quiet", action="store_false")
    parser.add_argument("-v", action="count")
    parser.add_argument("--color", choices=["red", "blue"])
    parser.add_argument("--tag", action="append")
    parser.add_argument("--cache", action=argparse.BooleanOptionalAction)
    parser.add_argument("--limit", type=int, nargs="?", const=5)
    parser.add_argument("path")
    parsed = vars(parser.parse_args(results[0]["argv"]))
    expect(
        parsed
        == {
            "name": "",
            "port": 0,
            "dry_run": True,
            "quiet": False,
            "v": 2,
            "color": "blue",
            "tag": ["two words", "$(touch unwanted)"],
            "cache": False,
            "limit": 5,
            "path": "a directory",
        },
        parsed,
    )
    expect(results[1]["argv"] == [], results)
    expect(all("error" in result for result in results[2:]), results)


def test_all_resource_actions_share_one_nix_evaluation(
    workspace: subject.Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolve every initial resource box against real offline flake outputs."""
    root = workspace.home / "projects/example"
    library = root / "library"
    library.mkdir()
    (library / "default.nix").write_text('{ pkgs }: pkgs.runCommand "library" {} ""')
    (library / "main.nix").write_text("{}")
    (root / "flake.nix").write_text("""{
      outputs = { self }: {
        packages.${builtins.currentSystem} = {
          hello.meta.mainProgram = "hello";
          library = {};
        };
        checks.${builtins.currentSystem}.hello = {};
      };
    }""")
    git(root, "add", "--force", ".")
    scratch = workspace.home / "nix-test"
    monkeypatch.setenv(
        "NIX_REMOTE",
        f"local?store={scratch / 'store'}"
        f"&state={scratch / 'state'}&log={scratch / 'log'}",
    )
    monkeypatch.setenv(
        "NIX_CONFIG",
        "experimental-features = nix-command flakes\n"
        "build-users-group =\nsandbox = false\n"
        "eval-cache = false\nsubstituters =\n",
    )
    calls: list[list[str]] = []
    original: Callable[[list[str], Path], str] = subject.command

    def record(argv: list[str], cwd: Path) -> str:
        calls.append(argv)
        return original(argv, cwd)

    monkeypatch.setattr(subject, "command", record)
    application = subject.Application(workspace.home)
    result = application.get("/api/actions", {"repository": ["projects/example"]})
    if not isinstance(result, dict):
        raise TypeError(result)
    expect(set(result) == {"hello", "library"}, result)
    expect(set(result["hello"]) == {"build", "run", "test"}, result)
    expect(set(result["library"]) == {"build"}, result)
    evaluations = [
        argv for argv in calls if argv[:2] == ["nix", "eval"] and "--json" in argv
    ]
    expect(len(evaluations) == 1, calls)
    expect(
        not any(argv[:2] in (["nix", "build"], ["nix", "run"]) for argv in calls), calls
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
        codex="/remote/codex with spaces",
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
            "--codex",
            options.codex,
            "--home",
            str(options.home),
        ],
        arguments,
    )
    options.home = None
    options.port = 0
    options.codex = None
    options.remote_revision = None
    monkeypatch.setattr(subject, "SOURCE_REVISION", REMOTE_REVISION)
    arguments, url = subject.ssh_arguments(options)
    remote = shlex.split(arguments[-1])
    expect("--home" not in remote, remote)
    expect("--codex" not in remote, remote)
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
                codex=None,
                no_open=False,
                remote_revision=REMOTE_REVISION,
            )
        )
    expect(opened == [], opened)


def check_remote_workspace(url: str, home: Path) -> None:
    """Check remote discovery, child Nix commands, and HTTP origin enforcement."""
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
        connection.request("GET", "/api/actions?repository=projects/example")
        response = connection.getresponse()
        result = json.loads(response.read())
        expect(response.status == HTTPStatus.OK and result == {"hello": {}}, result)
        connection.request(
            "POST",
            "/api/terminal/start",
            body='{"columns":100,"rows":20}',
            headers={"Content-Type": "application/json", "Origin": url},
        )
        response = connection.getresponse()
        expect(response.status != HTTPStatus.FORBIDDEN, response.read())
        connection.request(
            "POST",
            "/api/terminal/start",
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

    (workspace.home / "projects/example/flake.nix").write_text(
        "{ outputs = { self }: {}; }"
    )
    scratch = workspace.home / "ssh-test"
    scratch.mkdir()
    executable = scratch / "ssh"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, os, shlex, sys\nfrom pathlib import Path\n"
        "Path(os.environ['SSH_TEST_LOG']).write_text(json.dumps(sys.argv[1:]))\n"
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
        "SSH_TEST_LOG": str(scratch / "arguments.json"),
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
        "--codex",
        "/unavailable-codex",
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

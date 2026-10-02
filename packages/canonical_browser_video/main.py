#!/usr/bin/env python3
# Copyright (c) 2026- Paschalis Bizopoulos
# ruff: noqa: S603, S607
"""Generate a repeatable MP4 demonstration of the Canonical web browser."""

import argparse
import os
import subprocess
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

TIMEOUT = 180
WIDTH, HEIGHT = 1280, 800


def demo_repository(workspace: Path) -> None:
    """Create a small repository with a visible interface change."""
    package = workspace / "packages/example"
    package.mkdir(parents=True)
    (workspace / "flake.nix").write_text("{}\n")
    (package / "default.nix").write_text(
        '{ meta.description = "Example command line package"; }\n',
    )
    source = (
        '"""Print a message from the command line."""\n'
        "import argparse\n"
        "parser = argparse.ArgumentParser()\n"
        'parser.add_argument("--message", help="Message to print")\n'
    )
    (package / "main.py").write_text(source)
    (package / "test_main.py").write_text("def test_message_argument(): pass\n")
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(["git", "add", "."], cwd=workspace, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Demo",
            "-c",
            "user.email=demo@example.invalid",
            "commit",
            "-qm",
            "Baseline",
        ],
        cwd=workspace,
        check=True,
    )
    (package / "main.py").write_text(source.replace("--message", "--text"))


def repository_root() -> Path:
    """Find this canonical checkout from any working directory."""
    candidates = list(Path.cwd().resolve().parents)
    candidates.insert(0, Path.cwd().resolve())
    home = Path.home()
    configured = subprocess.run(
        [
            "git",
            "-C",
            str(home),
            "config",
            "--file",
            ".gitmodules",
            "--get-regexp",
            r"^submodule\..*\.path$",
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    candidates.extend(
        home / line.split(maxsplit=1)[1]
        for line in configured.stdout.splitlines()
        if len(line.split(maxsplit=1)) == 2  # noqa: PLR2004
    )
    for candidate in candidates:
        root = candidate.resolve()
        if (root / "flake.nix").is_file() and (
            root / "packages/canonical_browser_video/default.nix"
        ).is_file():
            return root
    message = "Cannot locate the canonical checkout containing canonical_browser_video"
    raise RuntimeError(message)


def record(workspace: Path, recordings: Path) -> Path:
    """Drive and record the actual GUI in Chromium."""
    with subprocess.Popen(
        ["canonical_browser", str(workspace), "--no-open", "--port", "0"],
        stdout=subprocess.PIPE,
        text=True,
    ) as server:
        try:
            if server.stdout is None:
                msg = "Browser did not provide its listening address"
                raise RuntimeError(msg)
            address = (
                server.stdout.readline().strip().removeprefix("Canonical browser: ")
            )
            if not address.startswith("http://127.0.0.1:"):
                msg = f"Unexpected browser address: {address}"
                raise RuntimeError(msg)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    executable_path=os.environ["CANONICAL_BROWSER_VIDEO_CHROMIUM"],
                )
                context = browser.new_context(
                    viewport={"width": WIDTH, "height": HEIGHT},
                    record_video_dir=str(recordings),
                    record_video_size={"width": WIDTH, "height": HEIGHT},
                )
                page = context.new_page()
                page.goto(address)
                page.wait_for_function(
                    "document.querySelector('#canvas').dataset.layout === 'ready'",
                )
                page.wait_for_timeout(1500)
                page.locator(".resource-block > summary").first.click()
                page.wait_for_timeout(1500)
                page.locator("#search").fill("--text")
                page.wait_for_timeout(1500)
                page.locator("#search").fill("")
                page.locator("#changes").click()
                page.wait_for_timeout(1500)
                page.locator("#changes").click()
                page.locator("#fit").click()
                page.wait_for_timeout(1500)
                video = page.video
                context.close()
                browser.close()
                if video is None:
                    msg = "Chromium did not record a video"
                    raise RuntimeError(msg)
                return Path(video.path())
        finally:
            server.terminate()
            server.wait(timeout=5)


def generate() -> Path:
    """Record the web browser and save an MP4 in this package's tmp directory."""
    output = (
        repository_root() / "packages/canonical_browser_video/tmp/canonical_browser.mp4"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="canonical-browser-video-") as temporary:
        workspace = Path(temporary) / "workspace"
        demo_repository(workspace)
        video = record(workspace, Path(temporary) / "recordings")
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(video),
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(output),
            ],
            check=True,
            timeout=TIMEOUT,
        )
    return output


def main(argv: list[str] | None = None) -> None:
    """Generate a demonstration of the web browser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    try:
        output = generate()
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"canonical_browser_video: {exc}\n")
    print(f"Created {output}")  # noqa: T201


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Capture the shipped remote browser UI with isolated example API responses.

This serves the repository's real browser assets. The two example sessions and
their transcript are fixtures; no live DOXA, LORE or hub store is opened.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "rust/doxa-remote/assets"
SHOTS = ROOT / "assets/shots"
SESSIONS = {
    "sessions": [
        {"id": "workstation~codex-1", "title": "gpt-6-sol@main/doxa", "engine": "codex", "model": "gpt-6-sol"},
        {"id": "workstation~claude-2", "title": "sonnet@docs/lore", "engine": "claude", "model": "claude-sonnet"},
    ]
}
TURNS = [
    {
        "prompt": "Check the remote control flow and summarize what is ready.",
        "text": "The host connector has registered two example sessions with the private hub. A second DOXA instance can open their recent turns in native tabs, send prompts, and answer pending input. The browser uses the same scoped hub commands.",
        "tools": [{"name": "read_file", "result": "Read the remote setup guide"}],
    },
    {
        "prompt": "What should I verify before using it on another device?",
        "text": "Keep the hub behind private Tailscale Serve, allow only your login, and confirm that the session host is connected. Remote model and permission changes still happen on the host.",
        "tools": [],
    },
]


def handler_for(scene: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: object) -> None:
            pass

        def reply(self, data: bytes, content_type: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def json(self, value: object) -> None:
            self.reply(json.dumps(value).encode(), "application/json")

        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            asset = {"/": ("index.html", "text/html"), "/remote.js": ("remote.js", "text/javascript"),
                     "/remote.css": ("remote.css", "text/css"), "/remote-sw.js": ("remote-sw.js", "text/javascript")}.get(path)
            if asset:
                name, content_type = asset
                self.reply((ASSETS / name).read_bytes(), content_type)
            elif path == "/api/sessions":
                self.json(SESSIONS)
            elif path == "/api/push/config":
                self.json({"enabled": False})
            elif path.startswith("/api/commands/"):
                pending = ([{"id": "review-1", "kind": "permission", "title": "Allow reading project files?"}]
                           if scene == "review" else [])
                self.json({"status": "accepted", "result": {"turns": TURNS, "pending_inputs": pending,
                                                           "dropped_turns": 0, "next_seq": 1}})
            elif path.endswith("/events"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                try:
                    self.wfile.write(b'data: {"type":"hello","engine":"codex","model":"gpt-6-sol"}\n\n')
                    self.wfile.flush()
                    time.sleep(60)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            else:
                self.send_error(404)

        def do_POST(self) -> None:
            size = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(min(size, 128_000))
            if urlsplit(self.path).path.endswith("/transcript"):
                self.json({"command_id": "example-transcript"})
            else:
                self.send_error(404)

    return Handler


def capture(scene: str, chrome: str, cache: Path) -> Path:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(scene))
    server.daemon_threads = True
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    target = SHOTS / f"rust-remote-browser-{scene}.png"
    try:
        with tempfile.TemporaryDirectory(prefix="remote-gallery-", dir=cache) as profile:
            chrome_process = subprocess.Popen([
                chrome, "--headless", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
                "--disable-background-networking", "--no-first-run", "--no-default-browser-check",
                "--hide-scrollbars", "--force-device-scale-factor=2", "--window-size=1534,867",
                "--remote-debugging-port=0", "--remote-allow-origins=*", f"--user-data-dir={profile}",
                f"http://127.0.0.1:{server.server_port}/",
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                port_file = Path(profile) / "DevToolsActivePort"
                deadline = time.monotonic() + 10
                while not port_file.exists():
                    if chrome_process.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError("Chrome DevTools did not start")
                    time.sleep(0.05)
                port = port_file.read_text().splitlines()[0]
                subprocess.run(["node", str(ROOT / "scripts/capture_remote_cdp.mjs"), port,
                                str(target)], check=True, timeout=20)
            finally:
                chrome_process.terminate()
                try:
                    chrome_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    chrome_process.kill()
                    chrome_process.wait()
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
    if not target.is_file() or target.stat().st_size < 10_000:
        raise RuntimeError(f"browser capture missing or empty: {target}")
    return target


def main() -> None:
    chrome = shutil.which("google-chrome-stable") or shutil.which("google-chrome")
    if not chrome:
        raise SystemExit("Google Chrome is required to capture the remote gallery")
    cache = Path.home() / ".cache/doxa"
    cache.mkdir(parents=True, exist_ok=True)
    SHOTS.mkdir(parents=True, exist_ok=True)
    for scene in ("conversation", "review"):
        print(capture(scene, chrome, cache))


if __name__ == "__main__":
    main()

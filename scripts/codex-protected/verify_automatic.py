#!/usr/bin/env python3
"""Credential-free compiled-provider fault proof using a loopback Responses fixture."""
import argparse
import hashlib
import http.server
import json
import os
from pathlib import Path
import selectors
import shlex
import signal
import subprocess
import tempfile
import threading
import time


class Rpc:
    def __init__(self, command, environment, cwd):
        self.process = subprocess.Popen(command, cwd=cwd, env=environment, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        self.pending = bytearray()
        self.events = []
        self.sequence = 0

    def send(self, value):
        self.process.stdin.write(json.dumps(value).encode() + b"\n")
        self.process.stdin.flush()

    def receive(self, deadline):
        while b"\n" not in self.pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self.selector.select(remaining):
                raise AssertionError("compiled provider fixture deadline expired")
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise AssertionError("compiled provider fixture ended unexpectedly")
            self.pending.extend(chunk)
            if len(self.pending) > 8 * 1024 * 1024:
                raise AssertionError("compiled provider fixture frame exceeded its bound")
        line, _, rest = self.pending.partition(b"\n")
        self.pending = bytearray(rest)
        return json.loads(line)

    def request(self, method, params):
        self.sequence += 1
        self.send({"id": self.sequence, "method": method, "params": params})
        deadline = time.monotonic() + 25
        while True:
            frame = self.receive(deadline)
            if frame.get("id") == self.sequence:
                assert "error" not in frame, f"fixture RPC {method} refused: {frame.get('error')}"
                return frame.get("result", {})
            self.events.append(frame)

    def turn(self, thread, text):
        result = self.request("turn/start", {"threadId": thread, "input": [{"type": "text", "text": text, "text_elements": []}]})
        turn = result["turn"]["id"]
        deadline = time.monotonic() + 25
        while True:
            frame = self.receive(deadline)
            self.events.append(frame)
            if frame.get("method") == "turn/completed" and frame.get("params", {}).get("turn", {}).get("id") == turn:
                return frame["params"]["turn"]["status"]

    def close(self):
        self.selector.close()
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.process.wait(timeout=5)


def hook_overrides(mode):
    commands = {
        "carrier-missing": "/doxa-fixture/nonexistent-native-carrier",
        "timeout": "/bin/sleep 5",
        "invalid": "/usr/bin/printf '{'",
        "empty": "/usr/bin/true",
        "plain": "/usr/bin/printf 'review complete'",
        "stopped": "/usr/bin/printf " + shlex.quote('{"continue":false,"suppressOutput":true}'),
        "async": "/usr/bin/printf " + shlex.quote('{"continue":true,"suppressOutput":true}'),
        "duplicate": "/usr/bin/printf " + shlex.quote('{"continue":true,"suppressOutput":true}'),
        "allow": "/usr/bin/printf " + shlex.quote('{"continue":true,"suppressOutput":true}'),
    }
    if mode == "missing":
        return []
    command = commands[mode]
    asynchronous = mode == "async"
    timeout = 1
    normalized = {"event_name": "pre_compact", "matcher": "^(auto|manual)$", "hooks": [
        {"type": "command", "command": command, "timeout": timeout, "async": asynchronous}]}
    digest = "sha256:" + hashlib.sha256(json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    group = '{matcher="^(auto|manual)$",hooks=[{type="command",command=' + json.dumps(command) + ',timeout=1,async=' + str(asynchronous).lower() + '}]}'
    count = 2 if mode == "duplicate" else 1
    state = ",".join(json.dumps(f"/<session-flags>/config.toml:pre_compact:{index}:0") + '={enabled=true,trusted_hash=' + json.dumps(digest) + '}' for index in range(count))
    return ["-c", "hooks={PreCompact=[" + ",".join([group] * count) + "],state={" + state + "}}"]


def scenario(binary, scratch, mode, *, default_window=False):
    with tempfile.TemporaryDirectory(prefix="codex-auto-", dir=scratch) as directory:
        root = Path(directory)
        codex_home = root / "codex-home"
        codex_home.mkdir(mode=0o700)
        requests = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_POST(self):
                size = int(self.headers.get("Content-Length", "0"))
                assert size <= 8 * 1024 * 1024
                body = self.rfile.read(size)
                requests.append((self.path, json.loads(body)))
                count = len(requests)
                text = "DOXA retained first fixture message" if count == 1 else "DOXA fixture summary" if count == 2 else "DOXA final fixture reply"
                tokens = (245000 if default_window else 1000) if count == 1 else 10
                response = "fixture-" + str(count)
                events = [{"type": "response.created", "response": {"id": response}},
                    {"type": "response.output_item.done", "item": {"type": "message", "role": "assistant", "id": "message-" + str(count), "content": [{"type": "output_text", "text": text}]}},
                    {"type": "response.completed", "response": {"id": response, "usage": {"input_tokens": tokens, "input_tokens_details": None, "output_tokens": 0, "output_tokens_details": None, "total_tokens": tokens}}}]
                payload = "".join("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n" for event in events).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        compact_config = (['model_context_window=272000'] if default_window else
            ['model_context_window=100000', 'model_auto_compact_token_limit=100'])
        config = '\n'.join(['model="gpt-5.5"', 'model_provider="doxa_fixture"',
            *compact_config,
            'model_post_turn_compact_threshold_percent=0', '[features]', 'codex_hooks=true', 'token_budget=false',
            '[model_providers.doxa_fixture]', 'name="DOXA loopback fixture"',
            f'base_url="http://127.0.0.1:{server.server_port}/v1"', 'wire_api="responses"',
            'requires_openai_auth=false', 'request_max_retries=0', 'stream_max_retries=0', 'supports_websockets=false'])
        (codex_home / "config.toml").write_text(config + "\n")
        (codex_home / "config.toml").chmod(0o600)
        environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(root),
            "CODEX_HOME": str(codex_home), "XDG_STATE_HOME": str(root / "state"), "TMPDIR": str(root), "RUST_LOG": "off"}
        rpc = Rpc([str(binary), "--listen", "stdio://"] + hook_overrides(mode), environment, root)
        try:
            initialized = rpc.request("initialize", {"clientInfo": {"name": "doxa_fault_fixture", "version": "1"}, "capabilities": {"experimentalApi": True}})
            assert initialized["userAgent"].startswith("doxa_codex_rs/0.156.1 (doxa-precompact-fail-closed-v1; ")
            rpc.send({"method": "initialized"})
            inventory = rpc.request("hooks/list", {"cwds": [str(root)]})
            hooks = [hook for entry in inventory["data"] for hook in entry["hooks"] if hook["eventName"] == "preCompact" and hook["source"] == "sessionFlags"]
            assert len(hooks) == (0 if mode == "missing" else 2 if mode == "duplicate" else 1)
            assert all(hook["enabled"] and hook["trustStatus"] == "trusted" for hook in hooks)
            result = rpc.request("thread/start", {"cwd": str(root), "model": "gpt-5.5", "modelProvider": "doxa_fixture", "approvalPolicy": "never", "sandbox": "read-only"})
            thread = result["thread"]["id"]
            first = rpc.turn(thread, "First synthetic fixture turn. Preserve this history.")
            assert first == "completed", first
            assert len(requests) == 1
            read = rpc.request("thread/read", {"threadId": thread, "includeTurns": False})
            source = Path(read["thread"]["path"])
            before = source.read_bytes()
            second = rpc.turn(thread, "Second synthetic fixture turn triggers automatic compaction.")
            rpc.request("thread/read", {"threadId": thread, "includeTurns": False})
            after = source.read_bytes()
            records = [json.loads(line) for line in after.splitlines()]
            replacements = [record for record in records if record.get("type") == "compacted"]
            completed = [event for event in rpc.events if event.get("method") == "hook/completed"]
            if mode == "allow":
                assert second == "completed", second
                assert len(requests) == 3, len(requests)
                assert len(replacements) == 1, replacements
            else:
                assert second != "completed", second
                assert len(requests) == 1, "compaction inference reached the loopback provider despite denied review"
                assert not replacements, "automatic history replacement was persisted"
                assert after.startswith(before), "owned rollout history was rewritten"
                assert b"DOXA retained first fixture message" in after
                assert not any(event.get("method") == "item/completed" and event.get("params", {}).get("item", {}).get("type") == "contextCompaction" for event in rpc.events)
            return {"mode": mode, "window": "default" if default_window else "lowered", "first_status": first, "second_status": second, "http_requests": len(requests),
                "history_replacements": len(replacements), "hook_statuses": [event["params"]["run"]["status"] for event in completed], "paid_requests": 0}
        finally:
            rpc.close()
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", type=Path, required=True)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--mode", choices=["missing", "carrier-missing", "timeout", "invalid", "empty", "plain", "stopped", "async", "duplicate", "allow"])
    parser.add_argument("--default-window", action="store_true", help="exercise the unoverridden automatic threshold with a 272k context fixture")
    options = parser.parse_args()
    options.scratch.mkdir(parents=True, exist_ok=True, mode=0o700)
    modes = [options.mode] if options.mode else ["missing", "carrier-missing", "timeout", "invalid", "empty", "plain", "stopped", "async", "duplicate", "allow"]
    for mode in modes:
        print(json.dumps(scenario(options.server, options.scratch, mode, default_window=options.default_window), sort_keys=True), flush=True)


if __name__ == "__main__":
    main()

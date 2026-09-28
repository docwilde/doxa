#!/usr/bin/env python3
"""Credential-free proof of the compiled code-mode helper and native tool events.

The loopback Responses peer requests one real read of an unpredictable owned
workspace file. Only after the matching custom-tool output arrives does it emit
the final answer. Report metadata only; discard requests, replies and rollouts.
The standalone control uses a trusted explicit-allow hook, not a LORE reviewer.
Optional --daemon verifies the same read through DOXA's production hook gate.
"""
import argparse
import collections
import contextlib
import hashlib
import http.server
import json
import os
from pathlib import Path
import secrets
import selectors
import shlex
import signal
import socket
import subprocess
import tempfile
import threading
import time

AGENT_PREFIX = "doxa_codex_rs/0.156.1 (doxa-precompact-fail-closed-v1; "
MAX_BYTES = 8 * 1024 * 1024
DEADLINE = 45
CALL_ID = "owned-code-mode-read"


def output_text(row):
    output = row.get("output")
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        return "".join(item.get("text", "") for item in output
                       if isinstance(item, dict) and isinstance(item.get("text"), str))
    return ""


def visible_tools(body):
    names = set()
    for item in body.get("tools", []):
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("name"), str):
            names.add(item["name"])
        for tool in item.get("tools", []):
            if isinstance(tool, dict) and isinstance(tool.get("name"), str):
                names.add(tool["name"])
    return names


def terminate(process):
    # Keep the leader unreaped until descendants have received SIGKILL.
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=5)
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream:
            stream.close()


class Frames:
    def __init__(self, stream):
        self.stream = stream
        self.selector = selectors.DefaultSelector()
        self.selector.register(stream, selectors.EVENT_READ)
        self.pending = bytearray()
        self.total = 0

    def read(self, deadline):
        while b"\n" not in self.pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self.selector.select(remaining):
                raise TimeoutError("owned proof frame deadline")
            chunk = os.read(self.stream.fileno(), 65536) if hasattr(self.stream, "fileno") else b""
            if not chunk:
                raise RuntimeError("owned proof transport ended")
            self.pending.extend(chunk)
            self.total += len(chunk)
            if self.total > 4 * MAX_BYTES:
                raise RuntimeError("owned proof total output exceeded bound")
            if len(self.pending) > MAX_BYTES:
                raise RuntimeError("owned proof frame exceeded bound")
        line, _, rest = self.pending.partition(b"\n")
        self.pending = bytearray(rest)
        frame = json.loads(line)
        if not isinstance(frame, dict):
            raise RuntimeError("owned proof invalid envelope")
        return frame

    def close(self):
        self.selector.close()


class Rpc:
    def __init__(self, command, environment, cwd):
        self.process = subprocess.Popen(command, cwd=cwd, env=environment,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            start_new_session=True)
        self.frames = Frames(self.process.stdout)
        self.events = []
        self.sequence = 0

    def send(self, value):
        self.process.stdin.write(json.dumps(value).encode() + b"\n")
        self.process.stdin.flush()

    def request(self, method, params):
        self.sequence += 1
        self.send({"id": self.sequence, "method": method, "params": params})
        deadline = time.monotonic() + DEADLINE
        while True:
            frame = self.frames.read(deadline)
            if frame.get("id") == self.sequence:
                if "error" in frame:
                    raise RuntimeError("owned proof RPC refused")
                return frame.get("result", {})
            self.events.append(frame)
            if len(self.events) > 1024:
                raise RuntimeError("owned proof notification count exceeded bound")

    def turn(self, thread):
        result = self.request("turn/start", {"threadId": thread, "input": [
            {"type": "text", "text": "Read the owned fixture file using code mode.", "text_elements": []}]})
        turn = result["turn"]["id"]
        deadline = time.monotonic() + DEADLINE
        while True:
            frame = self.frames.read(deadline)
            self.events.append(frame)
            if len(self.events) > 1024:
                raise RuntimeError("owned proof notification count exceeded bound")
            if (frame.get("method") == "turn/completed"
                    and frame.get("params", {}).get("threadId") == thread
                    and frame["params"].get("turn", {}).get("id") == turn):
                return frame["params"]["turn"]["status"]

    def close(self):
        self.frames.close()
        terminate(self.process)


class ModelPeer:
    def __init__(self, workspace, token):
        self.token = token
        self.requests = 0
        self.read_verified = False
        self.code_mode_only = False
        self.output_seen = False
        self.failed = False
        self.output_error_tags = []
        command = {"cmd": "cat fixture.txt", "workdir": str(workspace), "login": False,
                   "yield_time_ms": 1000, "max_output_tokens": 128}
        self.code = "text((await tools.exec_command(" + json.dumps(command) + ")).output);"
        peer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                self.connection.settimeout(5)
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= MAX_BYTES or self.path != "/v1/responses":
                        raise ValueError("invalid local request framing")
                    body = json.loads(self.rfile.read(size))
                    events = peer.response(body)
                except (ValueError, TypeError, KeyError, OSError):
                    peer.failed = True
                    self.send_error(400, "owned fixture request refused")
                    return
                payload = "".join("event: " + event["type"] + "\ndata: " +
                                  json.dumps(event) + "\n\n" for event in events).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)

    def response(self, body):
        self.requests += 1
        if self.requests > 2 or not isinstance(body, dict):
            raise ValueError("extra inference request")
        response = "owned-response-" + str(self.requests)
        if self.requests == 1:
            tools = visible_tools(body)
            self.code_mode_only = "exec" in tools and not tools.intersection({"exec_command", "shell"})
            item = {"type": "custom_tool_call", "call_id": CALL_ID, "name": "exec", "input": self.code}
        else:
            outputs = [row for row in body.get("input", []) if isinstance(row, dict)
                       and row.get("type") == "custom_tool_call_output" and row.get("call_id") == CALL_ID]
            self.output_seen = len(outputs) == 1
            text = output_text(outputs[0]) if self.output_seen else ""
            self.read_verified = self.token in text and outputs[0].get("success") is not False
            self.output_error_tags = [tag for tag in ("failed", "unavailable", "host", "permission")
                                      if tag in text.lower()]
            item = {"type": "message", "role": "assistant", "id": "owned-answer",
                    "content": [{"type": "output_text", "text": self.token if self.read_verified
                                 else "owned fixture tool result refused"}]}
        usage = {"input_tokens": 50, "input_tokens_details": None, "output_tokens": 5,
                 "output_tokens_details": None, "total_tokens": 55}
        return [{"type": "response.created", "response": {"id": response}},
                {"type": "response.output_item.done", "item": item},
                {"type": "response.completed", "response": {"id": response, "usage": usage}}]

    def __enter__(self):
        self.worker.start()
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=5)


def allow_hook():
    # Explicit loopback control approval only; this is never a reviewer claim.
    command = "/usr/bin/printf " + shlex.quote('{"continue":true,"suppressOutput":true}')
    normalized = {"event_name": "pre_compact", "matcher": "^(auto|manual)$", "hooks": [
        {"type": "command", "command": command, "timeout": 1, "async": False}]}
    digest = "sha256:" + hashlib.sha256(json.dumps(normalized, sort_keys=True,
                                               separators=(",", ":")).encode()).hexdigest()
    group = '{matcher="^(auto|manual)$",hooks=[{type="command",command=' + json.dumps(command) + ',timeout=1,async=false}]}'
    state = json.dumps("/<session-flags>/config.toml:pre_compact:0:0") + '={enabled=true,trusted_hash=' + json.dumps(digest) + '}'
    return ["-c", "hooks={PreCompact=[" + group + "],state={" + state + "}}"]


def environment(root):
    return {"PATH": "/usr/bin:/bin", "HOME": str(root), "CODEX_HOME": str(root / "codex"),
            "XDG_STATE_HOME": str(root / "state"), "TMPDIR": str(root), "LANG": "C.UTF-8",
            "DO_NOT_TRACK": "1", "RUST_LOG": "off"}


def configure(root, peer):
    home = root / "codex"
    home.mkdir(mode=0o700)
    config = '\n'.join(['model="gpt-6-sol"', 'model_provider="doxa_fixture"',
        'model_auto_compact_token_limit=100000000', 'model_post_turn_compact_threshold_percent=0',
        '[features]', 'codex_hooks=true', 'token_budget=false', 'code_mode_only=true',
        '[model_providers.doxa_fixture]', 'name="Owned code mode proof"',
        f'base_url="http://127.0.0.1:{peer.server.server_port}/v1"', 'wire_api="responses"',
        'requires_openai_auth=false', 'request_max_retries=0', 'stream_max_retries=0', 'supports_websockets=false'])
    (home / "config.toml").write_text(config + "\n")
    (home / "config.toml").chmod(0o600)


def provider_turn(server, root, peer, token):
    rpc = Rpc([str(server), "--listen", "stdio://", *allow_hook()], environment(root), root / "workspace")
    try:
        initialized = rpc.request("initialize", {"clientInfo": {"name": "doxa_owned_code_mode", "version": "1"},
                                                "capabilities": {"experimentalApi": True}})
        assert initialized.get("userAgent", "").startswith(AGENT_PREFIX)
        rpc.send({"method": "initialized"})
        config = rpc.request("config/read", {"cwd": str(root / "workspace"), "includeLayers": False})
        assert config["config"]["features"]["token_budget"] is False
        inventory = rpc.request("hooks/list", {"cwds": [str(root / "workspace")]})
        hooks = [hook for row in inventory.get("data", []) for hook in row.get("hooks", [])
                 if hook.get("eventName") == "preCompact" and hook.get("source") == "sessionFlags"]
        assert len(hooks) == 1 and hooks[0]["trustStatus"] == "trusted" and hooks[0]["enabled"]
        assert hooks[0]["async"] is False and hooks[0]["handlerType"] == "command"
        result = rpc.request("thread/start", {"cwd": str(root / "workspace"), "model": "gpt-6-sol",
            "modelProvider": "doxa_fixture", "approvalPolicy": "never", "sandbox": "read-only"})
        status = rpc.turn(result["thread"]["id"])
        completed = [row["params"]["item"] for row in rpc.events if row.get("method") == "item/completed"]
        commands = [item for item in completed if item.get("type") == "commandExecution"]
        replies = [item.get("text", "") for item in completed if item.get("type") == "agentMessage"]
        return {"transport": "compiled-provider", "turn_status": status,
                "raw_item_types": sorted({item.get("type", "") for item in completed}),
                "command_items": len(commands), "command_read_verified": any(
                    item.get("exitCode") == 0 and item.get("aggregatedOutput", "").strip() == token for item in commands),
                "final_token_matches": "".join(replies).strip() == token, "trusted_hook_verified": True}
    finally:
        rpc.close()


def daemon_turn(daemon, launcher, lore, root, peer, token):
    env = environment(root)
    env.update({"DOXA_HOME": str(root / "doxa"), "LORE_ROOT": str(root / "lore"),
        "LORE_PROJECTS_DIR": str(root / "projects"), "LORE_CODEX_SESSIONS_DIR": str(root / "codex/sessions"),
        "LORE_SKILLS_DIR": str(root / "skills"), "DOXA_LORE_RS": str(lore), "DOXA_LORE": "1",
        "LORE_DISABLE_SYNC": "1", "LORE_DISABLE_REVIEW": "1", "DOXA_AGENT_PEER_SEND": "0"})
    process = subprocess.Popen([str(daemon), "--runtime-dir", str(root / "runtime"), "--cwd", str(root / "workspace"),
        "--session-id", "code-mode-proof", "--engine", "codex", "--codex-bin", str(launcher),
        "--model", "gpt-6-sol", "--sandbox", "read-only", "--linger", "10"], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    frames = stream = None
    try:
        deadline = time.monotonic() + DEADLINE
        registry = root / "runtime/registry/code-mode-proof.json"
        while not registry.exists():
            if process.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError("owned native daemon startup failed")
            time.sleep(.02)
        stream = socket.socket(socket.AF_UNIX)
        stream.connect(json.loads(registry.read_text())["daemon_socket"])
        frames = Frames(stream)
        hello = frames.read(deadline)
        assert hello.get("engine") == "codex" and hello.get("lore_enabled") is True
        stream.sendall(json.dumps({"type": "attach", "cursor": None}).encode() + b"\n")
        stream.sendall(json.dumps({"type": "prompt", "id": 1, "text": "Read the owned fixture file using code mode."}).encode() + b"\n")
        kinds = collections.Counter()
        text = ""
        tool_read = False
        deadline = time.monotonic() + DEADLINE
        for _ in range(1024):
            frame = frames.read(deadline)
            event = frame.get("event", {})
            kind = event.get("type")
            if kind:
                kinds[kind] += 1
            if kind == "text_delta":
                text += event.get("data", {}).get("text", "")
            if kind == "tool_result_detail":
                tool_read |= token in json.dumps(event.get("data", {}))
            if kind == "turn_done":
                return {"transport": "native-daemon", "turn_status": "failed" if event["data"].get("is_error") else "completed",
                    "events": dict(kinds), "command_read_verified": tool_read,
                    "final_token_matches": text.strip() == token, "trusted_hook_verified": True,
                    "successful_lore_review": False}
        raise RuntimeError("owned native event count exceeded bound")
    finally:
        if frames:
            frames.close()
        if stream:
            stream.close()
        terminate(process)


def scenario(server, scratch, daemon=None, launcher=None, lore=None):
    report = {"paid_requests": 0, "model": "gpt-6-sol", "submitted_turns": 1,
              "source_or_reply_text_retained": False, "credential_files_created": False,
              "sibling_helper_present": server.with_name("codex-code-mode-host").is_file()}
    with tempfile.TemporaryDirectory(prefix="cm-", dir=scratch) as directory:
        root = Path(directory)
        root.chmod(0o700)
        workspace = root / "workspace"
        workspace.mkdir(mode=0o700)
        token = secrets.token_hex(16)
        fixture = workspace / "fixture.txt"
        fixture.write_text(token + "\n")
        fixture.chmod(0o600)
        before = fixture.read_bytes()
        with ModelPeer(workspace, token) as peer:
            configure(root, peer)
            try:
                if daemon:
                    report.update(daemon_turn(daemon, launcher, lore, root, peer, token))
                else:
                    report.update(provider_turn(server, root, peer, token))
            except Exception as error:
                report.update({"result": type(error).__name__})
            report.update({"http_requests": peer.requests, "code_mode_only_visible": peer.code_mode_only,
                "matching_tool_output_seen": peer.output_seen, "helper_read_verified": peer.read_verified,
                "tool_output_error_tags": peer.output_error_tags, "fixture_unchanged": fixture.read_bytes() == before})
            passed = (not peer.failed and peer.requests == 2 and peer.code_mode_only and peer.read_verified
                      and report.get("command_read_verified") and report.get("final_token_matches")
                      and report.get("turn_status") == "completed" and report["fixture_unchanged"])
            report["result"] = "passed" if passed else report.get("result", "failed")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", type=Path, required=True, help="actual compiled protected app server with sibling helper")
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--daemon", type=Path, help="also verify normalization through the actual native daemon")
    parser.add_argument("--launcher", type=Path, help="explicit protected native dispatcher for --daemon")
    parser.add_argument("--lore", type=Path, help="actual native LORE carrier for --daemon")
    args = parser.parse_args()
    if not args.server.is_absolute() or not args.server.is_file():
        parser.error("--server must be an absolute compiled binary")
    if args.daemon and (not args.launcher or not args.lore):
        parser.error("--daemon requires --launcher and --lore")
    if not args.scratch.is_absolute() or args.scratch.is_symlink():
        parser.error("--scratch must be an absolute owned disk directory")
    args.scratch.mkdir(parents=True, exist_ok=True, mode=0o700)
    metadata = args.scratch.stat()
    if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
        parser.error("--scratch must be private and owned")
    reports = [scenario(args.server, args.scratch)]
    if args.daemon:
        reports.append(scenario(args.server, args.scratch, args.daemon, args.launcher, args.lore))
    for report in reports:
        print(json.dumps(report, sort_keys=True), flush=True)
    return 0 if all(report["result"] == "passed" for report in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())

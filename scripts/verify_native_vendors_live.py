#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Private live checks launched by the guarded Rust verify_native_live example.

Not a CI test. Emits metadata only, never raw frames, credentials or model
reasoning. Two short turns per available provider; each has a 90-second deadline.
"""
import collections
import datetime
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time


KEYS = [os.environ[n] for n in ("DEEPSEEK_API_KEY", "ZAI_API_KEY") if os.environ.get(n)]
TURN_TIMEOUT = 90


def emit(value):
    text = json.dumps(value, ensure_ascii=True, sort_keys=True)
    for key in sorted(KEYS, key=len, reverse=True):
        text = text.replace(key, "[REDACTED]")
    print(text, flush=True)


class Wire:
    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(.2)
        self.sock.connect(path)
        self.buffer = b""
        self.counter = 0

    def receive(self, deadline):
        while b"\n" not in self.buffer:
            if time.monotonic() >= deadline:
                raise TimeoutError("bounded native operation timed out")
            try:
                part = self.sock.recv(65536)
            except socket.timeout:
                continue
            if not part:
                raise RuntimeError("native daemon disconnected")
            self.buffer += part
            if len(self.buffer) > 8 * 1024 * 1024:
                raise RuntimeError("native frame exceeds verifier limit")
        line, self.buffer = self.buffer.split(b"\n", 1)
        return json.loads(line)

    def send(self, value):
        self.sock.sendall((json.dumps(value) + "\n").encode())

    def call(self, method, params=None, timeout=20):
        self.counter += 1
        self.send({"type": "call", "id": self.counter, "method": method, "params": params or {}})
        deadline = time.monotonic() + timeout
        while True:
            frame = self.receive(deadline)
            if frame.get("id") == self.counter:
                return frame

    def turn(self, text):
        self.counter += 1
        self.send({"type": "prompt", "id": self.counter, "text": text})
        deadline = time.monotonic() + TURN_TIMEOUT
        counts = collections.Counter()
        rendered = ""
        while True:
            frame = self.receive(deadline)
            event = frame.get("event", {})
            kind = event.get("type")
            if kind:
                counts[kind] += 1
            if kind == "text_delta":
                rendered += event.get("data", {}).get("text", "")
            if kind == "needs_input":
                raise RuntimeError("unexpected native approval request")
            if kind == "turn_done":
                return {"events": dict(counts), "done": event.get("data", {})}, rendered
            if frame.get("id") == self.counter and frame.get("ok") is False:
                raise RuntimeError("native prompt rejected")


def terminate_group(process):
    """Reap the daemon and stop descendants even if their parent exited first."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    # A surviving child may ignore SIGTERM after the daemon has already exited.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=5)


def verify(provider, variable):
    if not os.environ.get(variable):
        return {"provider": provider, "result": "missing_credential", "submitted_turns": 0}
    daemon = os.environ.get("DOXA_NATIVE_DAEMON")
    if not daemon or not Path(daemon).is_file():
        return {"provider": provider, "result": "missing_native_daemon", "submitted_turns": 0}
    parent = Path(os.environ.get("TMPDIR", str(Path.home() / ".cache/doxa-live")))
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    result = {"provider": provider, "submitted_turns": 0, "protocol": "native daemon / Chat Completions SSE",
              "started_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "cost_basis": "provider reported prompt/completion tokens; no billed cost inferred"}
    with tempfile.TemporaryDirectory(prefix="vendors-live-", dir=parent) as directory:
        home = Path(directory)
        home.chmod(0o700)
        workspace = home / "workspace"
        workspace.mkdir(mode=0o700)
        token = secrets.token_hex(8)
        (workspace / "fixture.txt").write_text(token + "\n")
        lore = home / "lore"
        lore.mkdir(mode=0o700)
        (lore / "USER.md").write_text("# Isolated synthetic verification\n")
        env = {"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
               "TMPDIR": str(parent), "DOXA_HOME": str(home / "doxa"), "CODEX_HOME": str(home / "codex"),
               "LORE_ROOT": str(lore), "LORE_PROJECTS_DIR": str(home / "projects"),
               "DOXA_LORE": "0", "DOXA_AGENT_PEER_SEND": "0", "DOXA_VENDOR_TOOLS": "workspace-read",
               "DOXA_LORE_RS": os.environ.get("DOXA_LORE_RS", "/home/docwilde/.local/bin/lore-rs"),
               variable: os.environ[variable]}
        process = subprocess.Popen([daemon, "--runtime-dir", str(home / "runtime"), "--cwd", str(workspace),
            "--session-id", "live-vendor", "--engine", provider, "--effort", "low",
            "--linger", "10"], env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True)
        wire = None
        try:
            registry = home / "runtime/registry/live-vendor.json"
            deadline = time.monotonic() + 15
            while not registry.exists():
                if process.poll() is not None:
                    raise RuntimeError("native daemon startup failed")
                if time.monotonic() >= deadline:
                    raise TimeoutError("native daemon startup timed out")
                time.sleep(.02)
            wire = Wire(json.loads(registry.read_text())["daemon_socket"])
            hello = wire.receive(time.monotonic() + 10)
            result["initial_config"] = {n: hello.get(n) for n in ("model", "effort", "lore_enabled", "billing")}
            wire.send({"type": "attach", "cursor": None})
            catalog = wire.call("list_models")
            result["catalog"] = catalog
            if not catalog.get("ok") or "Provider account catalog" not in catalog.get("note", ""):
                result["result"] = "provider_catalog_unavailable_no_paid_turns"
                return result
            preferred = "deepseek-flash" if provider == "deepseek" else "glm-5.3-flash"
            if preferred not in catalog.get("models", []):
                result["result"] = "preferred_cheap_model_unavailable_no_paid_turns"
                return result
            model = wire.call("set_model", {"model": preferred})
            effort = wire.call("set_effort", {"effort": "low"})
            result["config_controls"] = [model, effort]
            if not model.get("ok") or not effort.get("ok"):
                result["result"] = "native_configuration_rejected_no_paid_turns"
                return result
            result["submitted_turns"] += 1
            first, text = wire.turn("Use workspace_read exactly once to read fixture.txt. Reply with only its content. "
                "Make no other tool calls. Keep reasoning under 50 words and final answer under 20 words.")
            first["synthetic_file_content_matches"] = text.strip() == token
            result["turns"] = [first]
            if first["done"].get("is_error") or not first["synthetic_file_content_matches"]:
                result["result"] = "native_turn_failed_or_tool_read_unverified"
                return result
            next_effort = "none" if provider == "deepseek" else "low"
            result["next_turn_config"] = wire.call("set_effort", {"effort": next_effort})
            if not result["next_turn_config"].get("ok"):
                result["result"] = "next_turn_configuration_failed"
                return result
            result["submitted_turns"] += 1
            second, text = wire.turn("Without tools, repeat the token from your previous answer. "
                "Reply only with the token. Keep reasoning under 30 words.")
            second["previous_turn_recalled"] = text.strip() == token
            result["turns"].append(second)
            paths = list(home.rglob("live-vendor.messages.json"))
            roles = []
            if len(paths) == 1:
                roles = [message.get("role") for message in json.loads(paths[0].read_text()).get("messages", [])]
            result["committed_history_roles"] = roles
            result["final_status"] = wire.call("status")
            passed = not second["done"].get("is_error") and second["previous_turn_recalled"] and roles == ["user", "assistant"] * 2
            result["result"] = "passed" if passed else "native_history_or_second_turn_failed"
            # Workspace reads have no tool callback today; matching a random
            # token proves the file was read, but does not count HTTP requests.
            result["chat_request_count"] = None
            result["workspace_read_count"] = "at_least_one_proved_by_random_token"
        except (TimeoutError, RuntimeError, OSError, ValueError) as error:
            result["result"] = type(error).__name__
            # Exception bodies and raw stderr can contain private output.
        finally:
            if wire:
                try:
                    wire.call("interrupt", timeout=2)
                    wire.call("stop", timeout=2)
                except (TimeoutError, RuntimeError, OSError, ValueError):
                    pass
                wire.sock.close()
            terminate_group(process)
            process.stderr.close()
    return result


if __name__ == "__main__":
    # The Rust launcher is the documented entry point: it guards saved keys
    # and resolves them before this child creates disposable private homes.
    if sys.argv[1:] != ["--live"]:
        emit({"result": "explicit_live_opt_in_required", "submitted_turns": 0})
        sys.exit(2)
    failed = False
    for provider, variable in (("deepseek", "DEEPSEEK_API_KEY"), ("glm", "ZAI_API_KEY")):
        outcome = verify(provider, variable)
        emit(outcome)
        failed |= outcome["result"] not in ("passed", "missing_credential")
    sys.exit(int(failed))

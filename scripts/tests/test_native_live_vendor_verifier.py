# SPDX-License-Identifier: AGPL-3.0-only
"""Credential-free verifier integration; actual native daemon + loopback SSE.

Run with stdlib unittest and DOXA_NATIVE_DAEMON built with local-test-server.
Only account catalog discovery is adapted: its production URL is fixed, so a
synthetic flat reply prevents fixture credentials reaching a real endpoint.
"""
import contextlib
import http.server
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "verify_native_vendors_live.py"
SPEC = importlib.util.spec_from_file_location("native_live_verifier", SOURCE)
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)
BINARY = os.environ.get("DOXA_NATIVE_DAEMON")
ORIGINAL_POPEN = subprocess.Popen


def process_exited_or_zombie(pid):
    stat = Path(f"/proc/{pid}/stat")
    if stat.exists():
        return stat.read_text().split(") ", 1)[1][0] == "Z"
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                           capture_output=True, text=True, timeout=2)
    return not state.stdout.strip() or state.stdout.lstrip().startswith("Z")


@contextlib.contextmanager
def vendor_server(hang=False, resumed_reasoning=None, first_reasoning="Finish the synthetic token answer."):
    requests = []
    release = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(request)
            if hang:
                release.wait(10)
                return
            messages = request["messages"]
            model = request["model"]
            if len(requests) == 1:
                delta = {"reasoning_content": "Read the synthetic file.", "tool_calls": [{
                    "index": 0, "id": "fixture-read", "type": "function", "function": {
                        "name": "workspace_read", "arguments": '{"path":"fixture.txt"}'}}]}
                finish = "tool_calls"
            elif len(requests) == 2:
                if model.startswith("deepseek") and request["thinking"]["type"] == "enabled" \
                        and messages[-2].get("reasoning_content") != "Read the synthetic file.":
                    # Realistic DeepSeek failure rather than a fixture which
                    # accepts a continuation the documented service rejects.
                    self.send_error(400, "missing reasoning_content in tool continuation")
                    return
                result = json.loads(messages[-1]["content"])
                assert result["path"] == "fixture.txt"
                delta = {"content": result["content"].strip(), "reasoning_content": "Finish the synthetic token answer."}
                finish = "stop"
            else:
                if model.startswith("deepseek") and request["thinking"]["type"] == "enabled":
                    assistants = [message for message in messages if message["role"] == "assistant"]
                    expected = first_reasoning if len(requests) == 3 else (
                        resumed_reasoning or "Recall the previous token.")
                    if not assistants or assistants[-1].get("reasoning_content") != expected:
                        self.send_error(400, "prior assistant reasoning missing after new user input")
                        return
                answer = next(message["content"] for message in reversed(messages)
                              if message["role"] == "assistant")
                delta = {"reasoning_content": "Recall the previous token.", "content": answer}
                finish = "stop"
            if request["thinking"]["type"] == "disabled":
                delta.pop("reasoning_content", None)
            frame = {"model": model, "choices": [{"delta": delta, "finish_reason": finish}],
                     "usage": {"prompt_tokens": 3, "completion_tokens": 2}}
            body = ("data: " + json.dumps(frame) + "\n\ndata: [DONE]\n\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class CatalogAdapter(verifier.Wire):
    """Real native socket for all operations except account catalog discovery."""
    def call(self, method, params=None, timeout=20):
        if method != "list_models":
            reply = super().call(method, params, timeout)
            assert "result" not in reply, "native protocol replies flatten host fields"
            return reply
        self.counter += 1
        return {"type": "reply", "id": self.counter, "ok": True,
                "models": ["deepseek-flash", "glm-5.3-flash"],
                "capabilities": [{"model": "deepseek-flash", "efforts": ["low", "high", "max", "none"]},
                                 {"model": "glm-5.3-flash", "efforts": ["low", "high", "max"]}],
                "note": "Provider account catalog · changes apply next turn"}


class NoneThenLowWire(CatalogAdapter):
    switched = False

    def call(self, method, params=None, timeout=20):
        if method == "set_effort" and not self.switched:
            self.switched = True
            params = {"effort": "none"}
        return super().call(method, params, timeout)


@unittest.skipUnless(BINARY, "requires local-test-server DOXA_NATIVE_DAEMON")
class NativeLiveVerifierTests(unittest.TestCase):
    @contextlib.contextmanager
    def resumed(self, home):
        args, kwargs = self.last_launch
        process = ORIGINAL_POPEN([*args, "--resume", "true"], **kwargs)
        wire = None
        try:
            registry = home / "runtime/registry/live-vendor.json"
            deadline = time.monotonic() + 10
            while not registry.exists():
                self.assertIsNone(process.poll(), "native resume startup failed")
                self.assertLess(time.monotonic(), deadline)
                time.sleep(.01)
            wire = verifier.Wire(json.loads(registry.read_text())["daemon_socket"])
            wire.receive(time.monotonic() + 10)
            wire.send({"type": "attach", "cursor": None})
            yield wire
        finally:
            if wire:
                with contextlib.suppress(TimeoutError, RuntimeError, OSError, ValueError):
                    wire.call("stop", timeout=2)
                wire.sock.close()
            verifier.terminate_group(process)
            process.stderr.close()

    def fixture(self, provider, endpoint, root, binary=None, retained=False, wire_class=CatalogAdapter):
        variable = "DEEPSEEK_API_KEY" if provider == "deepseek" else "ZAI_API_KEY"
        environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "TMPDIR": str(root),
                       "DOXA_NATIVE_DAEMON": binary or BINARY,
                       "DOXA_LORE_RS": os.environ.get("DOXA_LORE_RS", "/home/docwilde/.local/bin/lore-rs"),
                       variable: "isolated-native-live-fixture"}
        processes = []

        def launch(args, **kwargs):
            self.assertNotIn("--lore-python", args)
            self.assertTrue(kwargs["start_new_session"])
            self.assertEqual(kwargs["env"]["DOXA_LORE"], "0")
            self.assertEqual(kwargs["env"]["DOXA_VENDOR_TOOLS"], "workspace-read")
            process = ORIGINAL_POPEN([*args, "--vendor-endpoint", endpoint], **kwargs)
            processes.append(process)
            self.last_launch = ([*args, "--vendor-endpoint", endpoint], kwargs)
            return process

        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, environment, clear=True))
            stack.enter_context(patch.object(verifier, "Wire", wire_class))
            stack.enter_context(patch.object(verifier.subprocess, "Popen", launch))
            if retained:
                home = root / "retained"
                home.mkdir(mode=0o700)
                stack.enter_context(patch.object(verifier.tempfile, "TemporaryDirectory",
                    lambda **_kwargs: contextlib.nullcontext(str(home))))
            outcome = verifier.verify(provider, variable)
        self.assertIn(len(processes), (1, 2))
        if outcome.get("resume", {}).get("started"):
            self.assertEqual(len(processes), 2)
        self.assertTrue(all(process.poll() is not None for process in processes))
        return outcome

    def test_native_startup_controls_sse_tool_and_committed_history(self):
        parent = Path(os.environ.get("TMPDIR", str(Path.home() / ".cache/doxa-fixture-tests")))
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        for provider in ("deepseek", "glm"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory(dir=parent) as directory, \
                    vendor_server() as (endpoint, requests):
                result = self.fixture(provider, endpoint, Path(directory))
                self.assertEqual(result["result"], "passed", result)
                self.assertEqual(result["submitted_turns"], 2)
                self.assertTrue(result["resume"]["started"])
                self.assertEqual(result["resume"]["config"]["model"], requests[0]["model"])
                self.assertEqual(result["resume"]["config"]["effort"], "low")
                self.assertEqual(len(requests), 3)
                self.assertEqual(result["committed_history_roles"], ["user", "assistant"] * 2)
                self.assertEqual(result["config_controls"][0]["model"], requests[0]["model"])
                self.assertEqual(result["config_controls"][1]["effort"], "low")
                for turn in result["turns"]:
                    self.assertGreater(turn["events"].get("text_delta", 0), 0)
                    self.assertGreater(turn["events"].get("reasoning_delta", 0), 0)
                    self.assertFalse(turn["done"]["is_error"])
                    self.assertTrue(turn["done"]["usage_complete"])
                    self.assertTrue(turn["done"]["model_consistent"])
                    self.assertTrue(turn["nonce_evidence"]["exact_match"])
                    self.assertEqual(turn["nonce_evidence"]["nonce_occurrences"], 1)
                    self.assertEqual(turn["reported_usage"]["prompt_tokens"],
                                     turn["done"]["prompt_tokens"])
                    self.assertEqual(turn["reported_usage"]["completion_tokens"],
                                     turn["done"]["completion_tokens"])
                self.assertEqual(result["turns"][0]["done"]["prompt_tokens"], 6)
                self.assertEqual(result["turns"][0]["done"]["completion_tokens"], 4)
                self.assertEqual(requests[0]["reasoning_effort"], "low")
                self.assertEqual(requests[1]["reasoning_effort"], "low")
                self.assertEqual(requests[2]["thinking"], {"type": "enabled"})
                self.assertEqual(requests[2]["reasoning_effort"], "low")
                self.assertTrue(all(request["stream"] for request in requests))
                if provider == "deepseek":
                    self.assertEqual(requests[1]["messages"][-2]["reasoning_content"],
                                     "Read the synthetic file.")
                self.assertEqual(result["final_status"]["status"]["effort"],
                                 "low")

    def test_deepseek_private_reasoning_survives_native_resume_without_public_leak(self):
        parent = Path(os.environ.get("TMPDIR", str(Path.home() / ".cache/doxa-fixture-tests")))
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(dir=parent) as directory, \
                vendor_server(resumed_reasoning="*** Recall the previous token.") as (endpoint, requests):
            root = Path(directory)
            result = self.fixture("deepseek", endpoint, root, retained=True)
            self.assertEqual(result["result"], "passed", result)
            home = root / "retained"
            paths = list(home.rglob("live-vendor.messages.json"))
            self.assertEqual(len(paths), 1)
            private = paths[0]
            envelope = json.loads(private.read_text())
            self.assertEqual(envelope["messages"][1]["reasoning_content"], "Finish the synthetic token answer.")
            self.assertEqual(envelope["messages"][3]["reasoning_content"], "Recall the previous token.")
            self.assertEqual(private.stat().st_mode & 0o7777, 0o600)
            # Exercise constructor redaction after reading previously persisted
            # reasoning. The synthetic fixture key never leaves loopback.
            envelope["messages"][3]["reasoning_content"] = "isolated-native-live-fixture Recall the previous token."
            private.write_text(json.dumps(envelope))
            with self.resumed(home) as wire:
                done, text = wire.turn("Without tools, repeat the previous token; reply only with the token.")
                self.assertFalse(done["done"]["is_error"], done)
                self.assertEqual(text.strip(), (home / "workspace/fixture.txt").read_text().strip())
                self.assertEqual(len(requests), 4)
                self.assertEqual(requests[3]["messages"][-2]["reasoning_content"], "*** Recall the previous token.")
                self.assertNotIn("isolated-native-live-fixture", json.dumps(requests[3]))
                self.assertNotIn("isolated-native-live-fixture", private.read_text())
                public = next(home.rglob("live-vendor.jsonl")).read_text()
                self.assertNotIn("reasoning_content", public)
                self.assertNotIn("Finish the synthetic token answer.", public)
                self.assertNotIn("Recall the previous token.", public)

    def test_legacy_reasoning_refusal_preserves_history_and_names_recovery(self):
        parent = Path(os.environ.get("TMPDIR", str(Path.home() / ".cache/doxa-fixture-tests")))
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(dir=parent) as directory, vendor_server() as (endpoint, requests):
            root = Path(directory)
            result = self.fixture("deepseek", endpoint, root, retained=True)
            self.assertEqual(result["result"], "passed", result)
            home = root / "retained"
            private = next(home.rglob("live-vendor.messages.json"))
            envelope = json.loads(private.read_text())
            for message in envelope["messages"]:
                message.pop("reasoning_content", None)
            private.write_text(json.dumps(envelope))
            original = private.read_bytes()
            transcript = next(home.rglob("live-vendor.jsonl"))
            public = transcript.read_bytes()
            with self.resumed(home) as wire:
                done, _text = wire.turn("repeat the previous token")
                self.assertTrue(done["done"]["is_error"], done)
                self.assertIn("start a new session or restart with --effort none", done["done"]["error"])
                self.assertEqual(len(requests), 3, "legacy refusal must not send a fourth HTTP request")
                self.assertEqual(private.read_bytes(), original)
                self.assertEqual(transcript.read_bytes(), public)

    def test_none_then_low_replays_known_empty_reasoning(self):
        parent = Path(os.environ.get("TMPDIR", str(Path.home() / ".cache/doxa-fixture-tests")))
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(dir=parent) as directory, \
                vendor_server(first_reasoning="") as (endpoint, requests):
            result = self.fixture("deepseek", endpoint, Path(directory), wire_class=NoneThenLowWire)
            self.assertEqual(result["result"], "passed", result)
            self.assertEqual(result["config_controls"][1]["effort"], "none")
            self.assertEqual(requests[0]["thinking"], {"type": "disabled"})
            self.assertEqual(requests[1]["thinking"], {"type": "disabled"})
            self.assertEqual(requests[2]["thinking"], {"type": "enabled"})
            self.assertEqual(requests[2]["reasoning_effort"], "low")
            self.assertEqual(requests[2]["messages"][-2]["reasoning_content"], "")

    def test_timeout_kills_native_process_group_and_term_resistant_child(self):
        parent = Path(os.environ.get("TMPDIR", str(Path.home() / ".cache/doxa-fixture-tests")))
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(dir=parent) as directory, vendor_server(hang=True) as (endpoint, requests):
            root = Path(directory)
            pidfile = root / "descendant.pid"
            ready = root / "descendant.ready"
            wrapper = root / "native-with-child"
            # No credential text in the wrapper. This child intentionally
            # survives SIGTERM to prove cleanup escalates for descendants.
            child_code = ("import signal,time;from pathlib import Path;"
                "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                f"Path({str(ready)!r}).write_text('ready');time.sleep(60)")
            wrapper.write_text("#!/usr/bin/python3\nimport os,subprocess,sys\n"
                f"child=subprocess.Popen([sys.executable,'-c',{child_code!r}])\n"
                f"open({str(pidfile)!r},'w').write(str(child.pid))\n"
                f"import time\nfor _ in range(500):\n"
                f" if os.path.exists({str(ready)!r}): break\n time.sleep(.01)\n"
                f"else: sys.exit(2)\n"
                f"os.execv({BINARY!r},[{BINARY!r}]+sys.argv[1:])\n")
            wrapper.chmod(0o700)
            with patch.object(verifier, "TURN_TIMEOUT", .3):
                result = self.fixture("deepseek", endpoint, root, str(wrapper))
            self.assertEqual(result["result"], "TimeoutError", result)
            self.assertEqual(result["submitted_turns"], 1)
            self.assertEqual(len(requests), 1)
            pid = int(pidfile.read_text())
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if process_exited_or_zombie(pid):
                    break
                time.sleep(.01)
            else:
                self.fail("term-resistant native descendant is still running")

    def test_resume_startup_failure_sends_no_second_turn_and_reaps_child(self):
        parent = Path(os.environ.get("TMPDIR", str(Path.home() / ".cache/doxa-fixture-tests")))
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(dir=parent) as directory, vendor_server() as (endpoint, requests):
            root = Path(directory)
            pidfile = root / "resume-child.pid"
            ready = root / "resume-child.ready"
            wrapper = root / "resume-failure"
            child_code = ("import signal,time;from pathlib import Path;"
                "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                f"Path({str(ready)!r}).write_text('ready');time.sleep(60)")
            wrapper.write_text("#!/usr/bin/python3\nimport os,subprocess,sys\n"
                "if '--resume' in sys.argv:\n"
                f" child=subprocess.Popen([sys.executable,'-c',{child_code!r}])\n"
                f" open({str(pidfile)!r},'w').write(str(child.pid))\n"
                f" import time\n for _ in range(500):\n"
                f"  if os.path.exists({str(ready)!r}): break\n  time.sleep(.01)\n"
                f" else: sys.exit(2)\n"
                " sys.exit(1)\n"
                f"os.execv({BINARY!r},[{BINARY!r}]+sys.argv[1:])\n")
            wrapper.chmod(0o700)
            result = self.fixture("deepseek", endpoint, root, str(wrapper))
            self.assertEqual(result["result"], "RuntimeError", result)
            self.assertEqual(result["submitted_turns"], 1)
            self.assertEqual(len(requests), 2, "failed resume must not submit the history turn")
            pid = int(pidfile.read_text())
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if process_exited_or_zombie(pid):
                    break
                time.sleep(.01)
            else:
                self.fail("failed resume left a provider descendant running")


if __name__ == "__main__":
    unittest.main()

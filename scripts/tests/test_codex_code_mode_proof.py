"""Bounds and evidence rejection for the credential-free compiled-provider proof."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("code_mode_proof", Path(__file__).resolve().parents[1] /
                                            "codex-protected/verify_code_mode.py")
proof = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proof)


class ProofTests(unittest.TestCase):
    def test_native_read_requires_same_successful_completed_command_identity(self):
        for wrong in ("orphan", "failed", "different-tool", "different-id", "wrong-detail", None):
            with self.subTest(wrong=wrong):
                evidence = proof.NativeToolEvidence()
                evidence.observe({"type": "tool_call", "data": {"id": "read-1", "name": "command_execution"}}, "token")
                detail = "wrong" if wrong == "wrong-detail" else "token\n"
                evidence.observe({"type": "tool_result_detail", "data": {"id": "read-1", "text": detail}}, "token")
                if wrong != "orphan":
                    evidence.observe({"type": "tool_result", "data": {
                        "id": "read-2" if wrong == "different-id" else "read-1",
                        "name": "mcp_tool_call" if wrong == "different-tool" else "command_execution",
                        "is_error": wrong == "failed"}}, "token")
                self.assertEqual(evidence.verified, wrong is None)

    def test_first_request_cannot_contain_token_and_answer_is_returned_output_only(self):
        token = "a1" * 16
        with tempfile.TemporaryDirectory() as directory:
            peer = proof.ModelPeer(Path(directory), token)
            try:
                with self.assertRaisesRegex(ValueError, "already visible"):
                    peer.response({"input": [{"text": token}]})
                self.assertFalse(peer.first_request_token_absent)
            finally:
                peer.server.server_close()
            peer = proof.ModelPeer(Path(directory), token)
            try:
                peer.response({"tools": [{"name": "exec"}]})
                self.assertTrue(peer.first_request_token_absent)
                events = peer.response({"input": [{"type": "custom_tool_call_output",
                    "call_id": proof.CALL_ID, "output": "actual read: " + token + "\n"}]})
                self.assertEqual(events[1]["item"]["content"][0]["text"], token)
            finally:
                peer.server.server_close()

    def test_responses_lite_inventory_requires_developer_additional_tools(self):
        inventory = {"type": "additional_tools", "role": "developer", "tools": [
            {"type": "namespace", "name": "functions", "tools": [
                {"type": "custom", "name": "exec"}]}]}
        self.assertEqual(proof.visible_tools({"input": [inventory]}), {"functions", "exec"})
        inventory["role"] = "user"
        self.assertEqual(proof.visible_tools({"input": [inventory]}), set())
        inventory["role"] = "developer"
        inventory["type"] = "message"
        self.assertEqual(proof.visible_tools({"input": [inventory]}), set())

    def test_model_peer_requires_one_matching_successful_tool_output(self):
        with tempfile.TemporaryDirectory() as directory:
            peer = proof.ModelPeer(Path(directory), "owned-unpredictable-token")
            try:
                events = peer.response({"tools": [{"type": "custom", "name": "exec"}]})
                self.assertTrue(peer.code_mode_only)
                call = events[1]["item"]
                self.assertEqual(call["type"], "custom_tool_call")
                self.assertIn("cat fixture.txt", call["input"])
                self.assertNotIn(peer.token, call["input"], "the read cannot learn its token from the generated call")
                peer.response({"input": [{"type": "custom_tool_call_output", "call_id": "wrong-call",
                                         "output": peer.token}]})
                self.assertFalse(peer.output_seen)
                self.assertFalse(peer.read_verified)
                with self.assertRaises(ValueError):
                    peer.response({})
            finally:
                peer.server.server_close()

    def test_failed_or_duplicate_tool_outputs_cannot_be_success_evidence(self):
        for failure in ("failed", "duplicate", "missing"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                peer = proof.ModelPeer(Path(directory), "owned-unpredictable-token")
                try:
                    peer.response({"tools": [{"name": "exec"}, {"name": "exec_command"}]})
                    self.assertFalse(peer.code_mode_only)
                    output = {"type": "custom_tool_call_output", "call_id": proof.CALL_ID,
                              "output": [{"type": "input_text", "text": peer.token}]}
                    rows = [output]
                    if failure == "failed":
                        output["success"] = False
                    elif failure == "duplicate":
                        rows.append(output)
                    else:
                        rows = []
                    peer.response({"input": rows})
                    self.assertFalse(peer.read_verified)
                finally:
                    peer.server.server_close()

    def test_private_environment_does_not_forward_authentication_or_tool_overrides(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "owned-fixture", "CODEX_HOME": "/operator/home",
                                     "DOXA_CODEX_APPSERVER": "0", "CODEX_CODE_MODE_HOST": "/unsafe/helper"}):
            environment = proof.environment(Path("/owned/proof"))
        self.assertEqual(environment["CODEX_HOME"], "/owned/proof/codex")
        self.assertEqual(environment["PATH"], "/usr/bin:/bin")
        self.assertNotIn("OPENAI_API_KEY", environment)
        self.assertNotIn("DOXA_CODEX_APPSERVER", environment)
        self.assertNotIn("CODEX_CODE_MODE_HOST", environment)

    def test_invalid_frame_refuses_and_deadline_remains_bounded(self):
        process = subprocess.Popen(["/usr/bin/python3", "-c", "print('[]',flush=True)"], stdout=subprocess.PIPE,
                                   start_new_session=True)
        frames = proof.Frames(process.stdout)
        try:
            with self.assertRaisesRegex(RuntimeError, "invalid envelope"):
                frames.read(time.monotonic() + 1)
        finally:
            frames.close()
            proof.terminate(process)
        process = subprocess.Popen(["/usr/bin/python3", "-c", "import time;time.sleep(5)"], stdout=subprocess.PIPE,
                                   start_new_session=True)
        frames = proof.Frames(process.stdout)
        try:
            started = time.monotonic()
            with self.assertRaises(TimeoutError):
                frames.read(started + .05)
            self.assertLess(time.monotonic() - started, 1)
        finally:
            frames.close()
            proof.terminate(process)

    def test_initial_census_failure_reaps_process_and_closes_pipes_and_pidfds(self):
        popen = subprocess.Popen
        snapshot = proof.process_snapshot
        spawned = []
        censuses = 0
        def spawn(*args, **kwargs):
            process = popen(*args, **kwargs)
            spawned.append(process)
            return process
        def census():
            nonlocal censuses
            censuses += 1
            if censuses == 2:
                raise RuntimeError("synthetic census failure")
            return snapshot()
        before = len(list(Path("/proc/self/fd").iterdir()))
        with patch.object(proof.subprocess, "Popen", side_effect=spawn), patch.object(proof, "process_snapshot", side_effect=census):
            with self.assertRaisesRegex(RuntimeError, "synthetic census failure"):
                proof.owned_process(["/usr/bin/python3", "-c", "import time;time.sleep(30)"],
                                    stdout=subprocess.PIPE, start_new_session=True)
        self.assertIsNotNone(spawned[0].returncode)
        self.assertTrue(spawned[0].stdout.closed)
        self.assertEqual(before, len(list(Path("/proc/self/fd").iterdir())))

    def test_cleanup_tracks_owned_descendants_with_separate_groups_and_sessions(self):
        import signal
        script = """import os,time
children=[]
for detach in ['group','session']:
    child=os.fork()
    if child==0:
        if detach=='group': os.setpgid(0,0)
        else: os.setsid()
        time.sleep(30)
        os._exit(0)
    children.append(child)
print(' '.join(map(str,children)),flush=True)
time.sleep(30)
"""
        unrelated = subprocess.Popen(["/usr/bin/python3", "-c", "import time;time.sleep(30)"],
                                     start_new_session=True)
        process = proof.owned_process(["/usr/bin/python3", "-c", script],
                                      stdout=subprocess.PIPE, start_new_session=True)
        children = []
        starts = {}
        try:
            children = [int(pid) for pid in process.stdout.readline().split()]
            process.proof_tree.collect()
            starts = {pid: proof.process_snapshot()[pid][0] for pid in children}
            # Orphan the already remembered children before cleanup. Their SID/
            # PGID and current ancestry cannot be relied on after leader exit.
            process.kill()
            process.wait(timeout=2)
            started = time.monotonic()
            proof.terminate(process)
            self.assertLess(time.monotonic() - started, 3)
            rows = proof.process_snapshot()
            for pid, start in starts.items():
                self.assertFalse(pid in rows and rows[pid][0] == start and rows[pid][2] != "Z")
            self.assertIsNone(unrelated.poll(), "cleanup must not signal another owned test process")
        finally:
            if process.poll() is None:
                proof.terminate(process)
            for pid, start in starts.items():
                with proof.contextlib.suppress(ProcessLookupError):
                    descriptor = os.pidfd_open(pid)
                    try:
                        row = proof.process_row(pid)
                        if row and row[0] == start:
                            signal.pidfd_send_signal(descriptor, signal.SIGKILL)
                    finally:
                        os.close(descriptor)
            unrelated.kill()
            unrelated.wait(timeout=2)

    def test_explicit_hook_control_keeps_trusted_synchronous_review_contract(self):
        import tomllib
        flag, value = proof.allow_hook()
        self.assertEqual(flag, "-c")
        hooks = tomllib.loads(value)["hooks"]
        self.assertEqual(len(hooks["PreCompact"]), 1)
        handler = hooks["PreCompact"][0]["hooks"][0]
        self.assertIs(handler["async"], False)
        self.assertIn('"continue":true', handler["command"])
        self.assertEqual(len(hooks["state"]), 1)
        state = next(iter(hooks["state"].values()))
        self.assertTrue(state["enabled"])
        self.assertTrue(state["trusted_hash"].startswith("sha256:"))


if __name__ == "__main__":
    unittest.main()

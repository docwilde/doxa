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

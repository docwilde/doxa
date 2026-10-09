# SPDX-License-Identifier: AGPL-3.0-only
"""Source-only macOS lifecycle checks; never contact a provider."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("macos_lifecycle",
    SCRIPTS / "verify_macos_authenticated_lifecycle.py")
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)
from verify_native_claude_live import stop_and_wait


def valid_evidence():
    return {"result": "passed", "submitted_turns": 2, "launch_attached": True,
        "first_stop_exited": True, "resume_attached": True,
        "final_stop_exited": True,
        "first": {"ok": True, "text_deltas": 1},
        "second": {"ok": True, "text_deltas": 2},
        "credential": "private-fixture-secret", "model": "account-model"}


class GateTests(unittest.TestCase):
    def forbidden(self):
        raise AssertionError("provider runner was called")

    def test_opt_in_platform_and_protected_codex_gates_do_not_launch(self):
        for provider, live, platform, reason in [
            ("claude", False, "darwin", "explicit_live_opt_in_required"),
            ("claude", True, "linux", "macos_host_required"),
            ("codex", True, "darwin", "protected_codex_linux_only"),
            ("private-fixture-secret", True, "darwin", "unsupported_provider"),
        ]:
            with self.subTest(provider=provider, reason=reason):
                result = verifier.receipt(provider, live=live, platform=platform,
                    claude_runner=self.forbidden)
                self.assertEqual(result["status"], "unknown")
                self.assertEqual(result["reason"], reason)
                self.assertTrue(all(value == "unknown" for value in result["checks"].values()))
                self.assertNotIn("private-fixture-secret", json.dumps(result))

    def test_pass_requires_two_eventful_turns_resume_and_both_exits(self):
        evidence = valid_evidence()
        result = verifier.receipt("claude", live=True, platform="darwin",
            claude_runner=lambda: evidence)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(set(result), {"provider", "status", "checks", "submitted_turns"})
        self.assertEqual(set(result["checks"].values()), {"pass"})
        self.assertNotIn("private-fixture-secret", json.dumps(result))
        for field in ("launch_attached", "first_stop_exited", "resume_attached",
                      "final_stop_exited"):
            changed = valid_evidence()
            changed[field] = False
            with self.subTest(field=field):
                self.assertEqual(verifier.receipt("claude", live=True,
                    platform="darwin", claude_runner=lambda: changed)["status"], "unknown")
        changed = valid_evidence()
        changed["second"]["text_deltas"] = 0
        self.assertEqual(verifier.receipt("claude", live=True,
            platform="darwin", claude_runner=lambda: changed)["checks"]["event_delivery"],
            "unknown")

    def test_malformed_or_exception_evidence_cannot_leak(self):
        def failed():
            raise RuntimeError("private-fixture-secret")
        receipt = verifier.receipt("claude", live=True, platform="darwin",
            claude_runner=failed)
        self.assertEqual(receipt["reason"], "verifier_exception")
        self.assertNotIn("private-fixture-secret", json.dumps(receipt))
        for value in (None, {"result": {"private": "private-fixture-secret"},
                            "submitted_turns": True}, valid_evidence() | {"submitted_turns": 3}):
            receipt = verifier.receipt("claude", live=True, platform="darwin",
                claude_runner=lambda: value)
            self.assertEqual(receipt["status"], "unknown")
            self.assertNotIn("private-fixture-secret", json.dumps(receipt))

    def test_linux_command_refuses_live_without_provider_invocation(self):
        if sys.platform == "darwin":
            self.skipTest("never run the opt-in live command from macOS CI")
        command = subprocess.run([sys.executable, str(SCRIPTS /
            "verify_macos_authenticated_lifecycle.py"), "--live"],
            capture_output=True, text=True, timeout=5,
            env={**os.environ, "DEEPSEEK_API_KEY": "private-fixture-secret"})
        self.assertNotEqual(command.returncode, 0)
        self.assertEqual(json.loads(command.stdout)["reason"], "macos_host_required")
        self.assertNotIn("private-fixture-secret", command.stdout + command.stderr)

    def test_import_does_not_touch_provider_modules_before_opt_in(self):
        command = subprocess.run([sys.executable, "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); "
            "import verify_macos_authenticated_lifecycle; "
            "assert 'verify_native_claude_live' not in sys.modules; "
            "assert 'verify_native_vendors_live' not in sys.modules", str(SCRIPTS)],
            capture_output=True, text=True, timeout=5)
        self.assertEqual(command.returncode, 0, command.stderr)


class StopEvidenceTests(unittest.TestCase):
    class Wire:
        def __init__(self, ok):
            self.ok = ok
        def call(self, method, timeout):
            assert method == "stop" and timeout == 5
            return {"ok": self.ok}

    class Process:
        def __init__(self, times_out=False):
            self.times_out = times_out
            self.waited = False
        def wait(self, timeout):
            self.waited = True
            if self.times_out:
                raise subprocess.TimeoutExpired("fixture", timeout)
            return 0

    def test_stop_requires_acknowledgement_and_native_exit(self):
        process = self.Process()
        stop_and_wait(self.Wire(True), process)
        self.assertTrue(process.waited)
        process = self.Process()
        with self.assertRaises(RuntimeError):
            stop_and_wait(self.Wire(False), process)
        self.assertFalse(process.waited)
        with self.assertRaises(TimeoutError):
            stop_and_wait(self.Wire(True), self.Process(times_out=True))


if __name__ == "__main__":
    unittest.main()

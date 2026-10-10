# SPDX-License-Identifier: AGPL-3.0-only
"""Credential-free checks that the live verifier cannot certify a failed flow."""
import collections
import contextlib
import io
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import verify_claude_auto_permissions as verifier


def event(kind, **data):
    return {"event": {"type": kind, "data": data}}


class Marker:
    def __init__(self, text="nonce"):
        self.text = text

    def is_file(self):
        return self.text is not None

    def read_text(self):
        return self.text


class FakeWire:
    def __init__(self, frames, mode=None):
        self.counter = 0
        self.frames = collections.deque(frames)
        self.mode = mode or {"ok": True, "mode": "auto"}
        self.sent = []

    def send(self, value):
        self.sent.append(value)

    def call(self, method, params=None):
        self.sent.append({"method": method, "params": params})
        return self.mode

    def receive_event(self, _deadline):
        return self.frames.popleft()


class AutoVerifierTests(unittest.TestCase):
    def first(self, *, resolved="pending", marker="nonce", extra=(), mode=None):
        wire = FakeWire([
            event("needs_input", id="pending", tool_name="Bash"),
            event("needs_input_resolved", id=resolved),
            *extra,
            event("text_delta", text="nonce"),
            event("turn_done", is_error=False),
        ], mode)
        result = verifier.check_turn(wire, "fixture command", Marker(marker),
                                     "nonce", switch_pending=True)
        self.assertFalse(any(item.get("method") == "approve" for item in wire.sent))
        return result

    def second(self):
        wire = FakeWire([event("text_delta", text="nonce"),
                         event("turn_done", is_error=False)])
        return verifier.check_turn(wire, "fixture command", Marker(), "nonce",
                                   switch_pending=False)

    def test_success_requires_exact_card_and_real_marker_for_both_turns(self):
        self.assertTrue(verifier.passed(self.first(), self.second()))
        self.assertFalse(verifier.passed(self.first(resolved="unrelated"), self.second()))
        self.assertFalse(verifier.passed(self.first(marker=None), self.second()))
        self.assertFalse(verifier.passed(self.first(marker="wrong"), self.second()))

    def test_fresh_provider_refusal_remains_unanswered_and_fails(self):
        first = self.first(extra=[event("needs_input", id="fresh", tool_name="Bash",
                                       decision_reason_code="org_ask_ceiling")])
        self.assertEqual(first["auto_approval_requests"], 1)
        self.assertEqual(first["provider_reason_code"], "org_ask_ceiling")
        self.assertFalse(verifier.passed(first, self.second()))

    def test_mode_mismatch_cannot_pass(self):
        first = self.first(mode={"ok": True, "mode": "default"})
        self.assertEqual(first["reason"], "mode_switch_refused")
        self.assertFalse(verifier.passed(first, self.second()))

    def test_malformed_or_incomplete_success_evidence_cannot_pass(self):
        for field in self.first():
            result = self.first()
            del result[field]
            self.assertFalse(verifier.passed(result, self.second()), field)
        result = self.first()
        result["auto_approval_requests"] = False
        self.assertFalse(verifier.passed(result, self.second()))
        wire = FakeWire([event("text_delta", text="nonce"), event("turn_done")])
        second = verifier.check_turn(wire, "fixture", Marker(), "nonce", switch_pending=False)
        self.assertFalse(verifier.passed(self.first(), second))

    def test_rpc_preserves_resolution_event_before_reply(self):
        wire = verifier.EventWire.__new__(verifier.EventWire)
        wire.counter = 0
        wire.events = collections.deque()
        resolved = event("needs_input_resolved", id="pending")
        reply = {"id": 1, "ok": True, "mode": "auto"}
        with patch.object(verifier.Wire, "send"), \
                patch.object(verifier.Wire, "receive", side_effect=[resolved, reply]):
            self.assertEqual(wire.call("set_permission_mode", {"mode": "auto"}), reply)
            self.assertEqual(wire.receive_event(time.monotonic() + 1), resolved)

    def test_no_live_opt_in_never_launches(self):
        with patch.object(verifier, "run") as run, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(verifier.main([]), 1)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()

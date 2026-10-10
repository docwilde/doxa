# SPDX-License-Identifier: AGPL-3.0-only
"""Credential-free checks for the bounded live Codex permission oracle."""
import collections
import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import verify_codex_auto_permissions as verifier

COMMAND = "python3 /private/marker.py baseline nonce"
REFUSAL = "Finish the current response and queued prompts, then change permissions for the next turn"


def event(kind, **data):
    return {"event": {"type": kind, "data": data}}


class Marker:
    def exists(self):
        return False

    def is_file(self):
        return True

    def read_text(self):
        return "nonce"


class Wire:
    def __init__(self, frames, preserved=True):
        self.counter = 0
        self.frames = collections.deque(frames)
        self.sent = []
        self.preserved = preserved

    def send(self, value):
        self.sent.append(value)

    def call(self, method, params=None):
        self.sent.append({"method": method, "params": params})
        return {
            "set_permission_mode": {"ok": False, "error": REFUSAL},
            "status": {"status": {"permission_mode": "on-request"}},
            "get_state": {"pending_inputs_complete": True,
                          "pending_inputs": [{"id": "pending"}] if self.preserved else []},
            "answer_needs_input": {"ok": True},
        }[method]

    def receive_event(self, _deadline):
        return self.frames.popleft()


def approval(command=COMMAND):
    return event("needs_input", id="pending", tool_name="command_execution",
                 input_summary=json.dumps({"command": command}))


def success():
    return {"command_approvals": 0, "turn_completed": True, "turn_succeeded": True,
            "marker_verified": True, "exact_reply": True}


class CodexVerifierTests(unittest.TestCase):
    def test_baseline_approves_only_exact_disposable_command(self):
        for command in (COMMAND + " && id", COMMAND + " > /private/other", COMMAND + " $(id)"):
            wire = Wire([approval(command)])
            result = verifier.turn(wire, COMMAND, Marker(), "nonce", baseline=True)
            self.assertEqual(result["reason"], "unexpected_approval_left_unanswered")
            self.assertFalse(any(item.get("method") == "answer_needs_input" for item in wire.sent))
        self.assertTrue(verifier.command_matches(json.dumps({"command": COMMAND}), COMMAND))
        self.assertTrue(verifier.command_matches(json.dumps({"command": "bash -lc '" + COMMAND + "'"}), COMMAND))

    def test_auto_approval_is_never_answered(self):
        wire = Wire([approval()])
        result = verifier.turn(wire, COMMAND, Marker(), "nonce")
        self.assertEqual(result["command_approvals"], 1)
        self.assertFalse(any("method" in item for item in wire.sent))

    def test_baseline_can_continue_only_after_verified_pending_refusal(self):
        frames = [approval(), event("needs_input_resolved", id="pending"),
                  event("text_delta", text="nonce"), event("turn_done", is_error=False)]
        wire = Wire(frames)
        result = verifier.turn(wire, COMMAND, Marker(), "nonce", baseline=True)
        self.assertTrue(verifier.successful(result))
        approvals = [item for item in wire.sent if item.get("method") == "answer_needs_input"]
        self.assertEqual(approvals, [{"method": "answer_needs_input", "params":
                                    {"id": "pending", "answer": {"decision": "allow"}}}])
        wire = Wire([approval()], preserved=False)
        result = verifier.turn(wire, COMMAND, Marker(), "nonce", baseline=True)
        self.assertEqual(result["reason"], "unverified_active_switch_result")
        self.assertFalse(any(item.get("method") == "answer_needs_input" for item in wire.sent))

    def test_partial_result_requires_observed_refusal_and_real_execution(self):
        baseline = {**success(), "command_approvals": 1, "active_switch_rejected": True,
                    "pending_preserved_after_refusal": True, "baseline_approved_once": True,
                    "pending_card_resolved": True}
        receipt = {"baseline": baseline, "first_auto": success(), "second_auto": success(),
                   "same_provider_thread": True, "auto_mode_persisted": True,
                   "idle_switch_verified": True, "stop_exited": True}
        self.assertEqual(verifier.summarize(copy.deepcopy(receipt))["status"], "partial")
        for name in ("pending_card_resolved", "active_switch_rejected", "baseline_approved_once"):
            missing = copy.deepcopy(receipt)
            del missing["baseline"][name]
            self.assertEqual(verifier.summarize(missing)["status"], "unknown", name)
        for name in ("same_provider_thread", "auto_mode_persisted", "stop_exited"):
            missing = copy.deepcopy(receipt)
            del missing[name]
            self.assertEqual(verifier.summarize(missing)["status"], "unknown", name)
        for name in success():
            missing = copy.deepcopy(receipt)
            del missing["first_auto"][name]
            self.assertEqual(verifier.summarize(missing)["status"], "unknown", name)

    def test_pass_requires_pending_switch_and_never_accepts_manual_auto_turn(self):
        baseline = {**success(), "command_approvals": 1,
                    "active_switch_verified": True, "pending_card_resolved": True}
        receipt = {"baseline": baseline, "first_auto": success(), "second_auto": success(),
                   "same_provider_thread": True, "auto_mode_persisted": True,
                   "idle_switch_verified": True, "stop_exited": True}
        self.assertEqual(verifier.summarize(copy.deepcopy(receipt))["status"], "passed")
        receipt["first_auto"]["command_approvals"] = 1
        self.assertEqual(verifier.summarize(receipt)["status"], "unknown")

    def test_no_live_opt_in_does_not_launch(self):
        with patch.object(verifier, "run") as run, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(verifier.main([]), 1)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()

"""Start identity checks must run before the SDK engine is constructed."""

import importlib.util
import asyncio
import json
import pathlib
import sys
import types
import unittest
from unittest import mock


SIDECAR = pathlib.Path(__file__).resolve().parents[1] / "claude_sidecar.py"
spec = importlib.util.spec_from_file_location("claude_sidecar", SIDECAR)
sidecar = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sidecar)


class IdentityTests(unittest.TestCase):
    def test_fresh_id_and_resume(self):
        self.assertEqual(sidecar.validate_identity("abc-123", None), ("abc-123", None))
        self.assertEqual(sidecar.validate_identity(None, "abc-123"), ("abc-123", "abc-123"))
        self.assertEqual(sidecar.validate_identity("abc-123", "abc-123"),
                         ("abc-123", "abc-123"))

    def test_rejects_unsafe_and_mismatched_ids(self):
        for session_id, resume in [
            ("../escape", None), (None, "../escape"),
            ("abc", "different"), ("", None), (None, ""),
            (12, None), (None, 12),
        ]:
            with self.subTest(session_id=session_id, resume=resume):
                with self.assertRaises(ValueError):
                    sidecar.validate_identity(session_id, resume)

    def test_failed_start_can_be_retried(self):
        class FakeEngine:
            attempts = 0

            def __init__(self, **_options):
                pass

            async def start(self):
                FakeEngine.attempts += 1
                if FakeEngine.attempts == 1:
                    raise RuntimeError("temporary start failure")
                return types.SimpleNamespace(type="started", data={})

            async def finalize(self):
                return types.SimpleNamespace(type="finalized", data={})

            async def peer_events(self):
                if False:
                    yield None

        frames = [
            {"type": "request", "id": 1, "method": "start",
             "params": {"cwd": str(SIDECAR.parent), "session_id": "retry"}},
            {"type": "request", "id": 2, "method": "start",
             "params": {"cwd": str(SIDECAR.parent), "session_id": "retry"}},
            {"type": "request", "id": 3, "method": "finalize", "params": {}},
        ]
        replies = []

        async def read_frame(_reader, _limit):
            if not frames:
                return b""
            return json.dumps(frames.pop(0)).encode() + b"\n"

        engine_module = types.ModuleType("doxa.engine")
        engine_module.SessionEngine = FakeEngine
        with mock.patch.dict(sys.modules, {"doxa.engine": engine_module}), \
             mock.patch.object(sidecar.asyncio, "to_thread", read_frame), \
             mock.patch.object(sidecar, "emit", replies.append):
            asyncio.run(sidecar.run())
        self.assertEqual(FakeEngine.attempts, 2)
        self.assertEqual([reply.get("ok") for reply in replies if reply.get("type") == "reply"],
                         [False, True, True])


if __name__ == "__main__":
    unittest.main()

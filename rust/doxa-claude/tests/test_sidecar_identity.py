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
    def test_emit_writes_complete_frame_after_partial_write(self):
        parts = []

        def partial_write(_fd, data):
            chunk = bytes(data[:3])
            parts.append(chunk)
            return len(chunk)

        with mock.patch.object(sidecar.os, "write", partial_write):
            sidecar.emit({"type": "reply", "ok": True})
        self.assertEqual(json.loads(b"".join(parts)), {"type": "reply", "ok": True})
        self.assertTrue(b"".join(parts).endswith(b"\n"))

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
            finalize_calls = 0

            def __init__(self, **_options):
                pass

            async def start(self):
                FakeEngine.attempts += 1
                if FakeEngine.attempts == 1:
                    raise RuntimeError("temporary start failure")
                return types.SimpleNamespace(type="started", data={})

            async def finalize(self):
                FakeEngine.finalize_calls += 1
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
        self.assertEqual(FakeEngine.finalize_calls, 1)
        self.assertEqual([reply.get("ok") for reply in replies if reply.get("type") == "reply"],
                         [False, True, True])

    def test_stdin_eof_cancels_turn_and_finalizes_once(self):
        class FakeEngine:
            instance = None

            def __init__(self, **_options):
                self.finalize_calls = 0
                self.turn_cancelled = False
                FakeEngine.instance = self

            async def start(self):
                return types.SimpleNamespace(type="started", data={})

            async def send(self, _prompt):
                try:
                    await asyncio.sleep(3600)
                    yield None
                except asyncio.CancelledError:
                    self.turn_cancelled = True
                    raise

            async def peer_events(self):
                await asyncio.sleep(3600)
                yield None

            async def finalize(self):
                self.finalize_calls += 1
                return types.SimpleNamespace(type="finalized", data={})

        frames = [
            {"type": "request", "id": 1, "method": "start",
             "params": {"cwd": str(SIDECAR.parent), "session_id": "eof"}},
            {"type": "request", "id": 2, "method": "prompt", "params": {"text": "work"}},
        ]

        async def read_frame(_reader, _limit):
            if not frames:
                await asyncio.sleep(0.01)
                return b""
            return json.dumps(frames.pop(0)).encode() + b"\n"

        engine_module = types.ModuleType("doxa.engine")
        engine_module.SessionEngine = FakeEngine
        with mock.patch.dict(sys.modules, {"doxa.engine": engine_module}), \
             mock.patch.object(sidecar.asyncio, "to_thread", read_frame), \
             mock.patch.object(sidecar, "emit"):
            asyncio.run(sidecar.run())
        self.assertTrue(FakeEngine.instance.turn_cancelled)
        self.assertEqual(FakeEngine.instance.finalize_calls, 1)

    def test_stdin_eof_bounds_stalled_finalize(self):
        class FakeEngine:
            cancelled = False

            def __init__(self, **_options):
                pass

            async def start(self):
                return types.SimpleNamespace(type="started", data={})

            async def peer_events(self):
                if False:
                    yield None

            async def finalize(self):
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    FakeEngine.cancelled = True
                    raise

        frames = [{"type": "request", "id": 1, "method": "start",
                   "params": {"cwd": str(SIDECAR.parent), "session_id": "eof"}}]

        async def read_frame(_reader, _limit):
            return json.dumps(frames.pop(0)).encode() + b"\n" if frames else b""

        engine_module = types.ModuleType("doxa.engine")
        engine_module.SessionEngine = FakeEngine
        with mock.patch.dict(sys.modules, {"doxa.engine": engine_module}), \
             mock.patch.object(sidecar.asyncio, "to_thread", read_frame), \
             mock.patch.object(sidecar, "emit"), \
             mock.patch.object(sidecar, "EOF_FINALIZE_TIMEOUT", 0.01):
            asyncio.run(sidecar.run())
        self.assertTrue(FakeEngine.cancelled)

    def test_stdin_eof_skips_finalize_while_turn_ignores_cancellation(self):
        class FakeEngine:
            instance = None

            def __init__(self, **_options):
                self.cancellations = 0
                self.finalize_calls = 0
                FakeEngine.instance = self

            async def start(self):
                return types.SimpleNamespace(type="started", data={})

            async def send(self, _prompt):
                while True:
                    try:
                        await asyncio.sleep(3600)
                    except asyncio.CancelledError:
                        self.cancellations += 1
                        if self.cancellations > 1:
                            raise
                    yield types.SimpleNamespace(type="ignored", data={})

            async def peer_events(self):
                if False:
                    yield None

            async def finalize(self):
                self.finalize_calls += 1
                return types.SimpleNamespace(type="finalized", data={})

        frames = [
            {"type": "request", "id": 1, "method": "start",
             "params": {"cwd": str(SIDECAR.parent), "session_id": "eof"}},
            {"type": "request", "id": 2, "method": "prompt", "params": {"text": "work"}},
        ]

        async def read_frame(_reader, _limit):
            if not frames:
                await asyncio.sleep(0.01)
                return b""
            return json.dumps(frames.pop(0)).encode() + b"\n"

        engine_module = types.ModuleType("doxa.engine")
        engine_module.SessionEngine = FakeEngine
        with mock.patch.dict(sys.modules, {"doxa.engine": engine_module}), \
             mock.patch.object(sidecar.asyncio, "to_thread", read_frame), \
             mock.patch.object(sidecar, "emit"), \
             mock.patch.object(sidecar, "TASK_CANCEL_TIMEOUT", 0.01):
            asyncio.run(sidecar.run())
        self.assertGreaterEqual(FakeEngine.instance.cancellations, 2)
        self.assertEqual(FakeEngine.instance.finalize_calls, 0)


if __name__ == "__main__":
    unittest.main()

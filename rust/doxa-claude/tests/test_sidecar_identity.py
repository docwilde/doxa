"""Start identity checks must run before the SDK engine is constructed."""

import importlib.util
import asyncio
import json
import pathlib
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock


SIDECAR = pathlib.Path(__file__).resolve().parents[1] / "claude_sidecar.py"
spec = importlib.util.spec_from_file_location("claude_sidecar", SIDECAR)
sidecar = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sidecar)


class IdentityTests(unittest.TestCase):
    def test_startup_error_codes_use_sdk_types_and_never_echo_private_details(self):
        from claude_agent_sdk import CLIConnectionError, CLINotFoundError, CLIJSONDecodeError, ProcessError
        private = "sk-private prompt account traceback"
        errors = [
            (CLINotFoundError(private), "startup_cli_missing"),
            (PermissionError(private), "startup_permission_denied"),
            (TimeoutError(private), "startup_timeout"),
            (Exception("Control request timeout: initialize"), "startup_timeout"),
            (CLIJSONDecodeError(private, ValueError(private)), "startup_cli_protocol"),
            (CLIConnectionError(private), "startup_cli_connection"),
            (ProcessError(private, exit_code=1, stderr=private), "startup_cli_process"),
            (TypeError(private), "startup_options_invalid"),
            (RuntimeError(private), "startup_failed"),
            (ExceptionGroup(private, [RuntimeError(private), TimeoutError(private)]), "startup_timeout"),
        ]
        for error, expected in errors:
            with self.subTest(expected=expected):
                self.assertEqual(sidecar.startup_error_code(error), expected)
                self.assertNotIn(private, sidecar.startup_error_code(error))

    def test_startup_reply_exposes_fixed_failure_reason_and_remains_retryable(self):
        from claude_agent_sdk import ProcessError
        class FailedEngine:
            def __init__(self, peer_presence=True, **_options):
                pass
            async def start(self):
                raise ProcessError("sk-private prompt",exit_code=1,stderr="private account")
        engine_module=types.ModuleType("doxa.engine");engine_module.SessionEngine=FailedEngine
        requests=iter([{"type":"request","id":1,"method":"start",
            "params":{"cwd":str(SIDECAR.parent),"session_id":"safe-failure"}}])
        replies=[]
        async def read_frame(_reader,_limit):
            try: return json.dumps(next(requests)).encode()+b"\n"
            except StopIteration: return b""
        with mock.patch.dict(sys.modules,{"doxa.engine":engine_module}), \
             mock.patch.object(sidecar.asyncio,"to_thread",read_frame), \
             mock.patch.object(sidecar,"emit",replies.append):
            asyncio.run(sidecar.run())
        reply=next(frame for frame in replies if frame["type"]=="reply")
        self.assertEqual(reply,{"type":"reply","id":1,"ok":False,"error":"startup_cli_process"})
        self.assertNotIn("private",json.dumps(replies))

    def test_account_snapshot_uses_only_connected_display_fields(self):
        self.assertEqual(sidecar.account_snapshot({
            "email": " sdk@example.test ", "organization": "SDK org",
            "subscriptionType": "pro", "apiProvider": "firstParty",
            "accessToken": "must never cross bridge", "organizationName": "cached org",
        }), {"email": "sdk@example.test", "organization": "SDK org",
             "subscriptionType": "pro", "apiProvider": "firstParty"})
        self.assertIsNone(sidecar.account_snapshot({"email": "bad\nvalue",
                                                  "organization": "x" * 257}))
        self.assertIsNone(sidecar.account_snapshot(None))

    def test_session_engine_options_requires_explicit_peer_ownership(self):
        class OlderEngine:
            def __init__(self, cwd, session_id=None, resume=None, model=None):
                pass

        class CurrentEngine:
            def __init__(self, cwd, detail_events=False, peer_presence=True):
                pass

        options = {"cwd": "/project", "session_id": "session"}
        with self.assertRaises(sidecar.PeerPresenceUnsupported):
            sidecar.session_engine_options(OlderEngine, options)
        self.assertEqual(sidecar.session_engine_options(CurrentEngine, options),
                         {**options, "detail_events": True, "peer_presence": False})
        self.assertEqual(options, {"cwd": "/project", "session_id": "session"})

    def test_outdated_engine_refuses_start_before_constructor_or_registry(self):
        class OlderEngine:
            def __init__(self, **options):
                raise AssertionError("unsupported engine must not be constructed")

        engine_module = types.ModuleType("doxa.engine")
        engine_module.SessionEngine = OlderEngine
        requests = iter([{"type": "request", "id": 1, "method": "start",
                          "params": {"cwd": str(SIDECAR.parent), "session_id": "old"}}])
        replies = []

        async def read_frame(_reader, _limit):
            try:
                return json.dumps(next(requests)).encode() + b"\n"
            except StopIteration:
                return b""

        with mock.patch.dict(sys.modules, {"doxa.engine": engine_module}), \
             mock.patch.object(sidecar.asyncio, "to_thread", read_frame), \
             mock.patch.object(sidecar, "emit", replies.append):
            asyncio.run(sidecar.run())
        reply = next(frame for frame in replies if frame["type"] == "reply")
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"], "peer_presence_unsupported_update_python_and_restart")

    def test_compact_review_deadline_blocks_delayed_worker(self):
        class SlowEngine:
            async def review_before_compact(self):
                await asyncio.sleep(0.05)
                return True

        self.assertFalse(asyncio.run(sidecar.reviewed_compact_ready(
            SlowEngine(), "/compact", timeout=0.001)))

    def test_compact_review_requires_success_and_exact_command(self):
        class Engine:
            def __init__(self, result):
                self.result = result
                self.calls = 0

            async def review_before_compact(self):
                self.calls += 1
                return self.result

        good = Engine(True)
        self.assertTrue(asyncio.run(sidecar.reviewed_compact_ready(good, "/compact", timeout=1)))
        self.assertEqual(good.calls, 1)
        bad = Engine(False)
        self.assertFalse(asyncio.run(sidecar.reviewed_compact_ready(bad, "/compact", timeout=1)))
        self.assertFalse(asyncio.run(sidecar.reviewed_compact_ready(good, "/compact now", timeout=1)))
        self.assertEqual(good.calls, 1)

    def test_billing_snapshot_uses_matching_local_tier_and_marks_stale_quota(self):
        from doxa import identity

        stale = identity.Usage(
            session=identity.UsageLimit("session", 9, "normal", ""),
            weekly=identity.UsageLimit("weekly_all", 48, "normal", ""),
            scoped=None, scope_label="",
            fetched_at=datetime.now(timezone.utc) - timedelta(hours=7),
        )
        for precise, expected in [("default_claude_max_5x", "max 5x"),
                                  ("default_claude_max_20x", "max 20x")]:
            with self.subTest(precise=precise), \
                 mock.patch.object(identity, "local_account", return_value={
                     "emailAddress": "person@example.com",
                     "organizationRateLimitTier": precise,
                 }), \
                 mock.patch.object(identity, "usage", return_value=stale):
                self.assertEqual(sidecar.billing_snapshot({
                    "email": "PERSON@example.com", "subscriptionType": "Claude Max",
                }), {"mode": "subscription", "type": expected,
                     "quota": "5h:9% week:48%~",
                     "quota_limits": {"five_hour": {"percent":9,"stale":True,"source":"claude_cli_cache"},
                                      "seven_day": {"percent":48,"stale":True,"source":"claude_cli_cache"}},
                     "quota_source":"claude_cli_cache","quota_stale":True})

    def test_billing_snapshot_rejects_foreign_local_tier_and_quota(self):
        from doxa import identity

        with mock.patch.object(identity, "local_account", return_value={
            "emailAddress": "other@example.com",
            "organizationRateLimitTier": "default_claude_max_20x",
        }), mock.patch.object(identity, "usage") as usage:
            self.assertEqual(sidecar.billing_snapshot({
                "email": "person@example.com", "subscriptionType": "Claude Max",
            }), {"mode": "subscription", "type": "max", "quota": None,
                "quota_limits":{},"quota_source":"claude_cli_cache","quota_stale":False})
            usage.assert_not_called()

    def test_billing_snapshot_requires_sdk_subscription(self):
        from doxa import identity

        with mock.patch.object(identity, "local_account") as local, \
             mock.patch.object(identity, "usage") as usage:
            self.assertIsNone(sidecar.billing_snapshot({
                "email": "person@example.com", "apiProvider": "firstParty",
            }))
            self.assertIsNone(sidecar.billing_snapshot(None))
            local.assert_not_called()
            usage.assert_not_called()

    def test_catalog_probe_does_not_block_model_or_control_replies(self):
        from doxa import claude_catalog, providers

        class FakeEngine:
            def __init__(self, peer_presence=True, **_options):
                pass

            async def start(self):
                return types.SimpleNamespace(type="started", data={})

            async def set_model(self, model):
                return model

            async def finalize(self):
                return types.SimpleNamespace(type="finalized", data={})

            async def peer_events(self):
                if False:
                    yield None

        class FakeProvider:
            async def list_models(self):
                return [types.SimpleNamespace(id="verified", source="cache")]

            def catalog_note(self, _models):
                return "verified cache"

        engine_module = types.ModuleType("doxa.engine")
        engine_module.SessionEngine = FakeEngine
        frames = [
            ("start", {"cwd": str(SIDECAR.parent), "session_id": "catalog"}),
            ("list_models", {}),
            ("set_model", {"model": "verified"}),
            ("list_models", {}),
            ("finalize", {}),
        ]
        requests = iter({"type": "request", "id": i, "method": method, "params": params}
                        for i, (method, params) in enumerate(frames, 1))
        replies = []
        gate = asyncio.Event()

        async def read_frame(_reader, _limit):
            try:
                frame = next(requests)
            except StopIteration:
                return b""
            if frame["id"] == 4:
                gate.set()
            await asyncio.sleep(0)
            return json.dumps(frame).encode() + b"\n"

        async def slow_probe():
            await gate.wait()
            return "refreshed"

        probe = mock.AsyncMock(side_effect=slow_probe)

        with mock.patch.dict(sys.modules, {"doxa.engine": engine_module}), \
             mock.patch.object(sidecar.asyncio, "to_thread", read_frame), \
             mock.patch.object(sidecar, "emit", replies.append), \
             mock.patch.object(claude_catalog, "attempt_cli_catalog_refresh", probe) as refresh, \
             mock.patch.object(providers, "model_provider", return_value=FakeProvider()), \
             mock.patch.object(providers.ClaudeProvider, "startup_catalog_checked"):
            asyncio.run(sidecar.run())

        self.assertEqual(refresh.call_count, 1)
        by_id = {frame["id"]: frame for frame in replies if frame.get("type") == "reply"}
        self.assertEqual(by_id[2]["result"], {
            "models": [], "loading": True,
            "note": "Claude model catalog is loading; refreshes automatically"})
        self.assertEqual(by_id[3]["result"]["model"], "verified")
        self.assertEqual(by_id[4]["result"]["models"], ["verified"])

    def test_list_models_uses_one_startup_probe_and_hides_static_fallback(self):
        from doxa import claude_catalog, providers

        class FakeEngine:
            def __init__(self, peer_presence=True, **_options):
                pass

            async def start(self):
                return types.SimpleNamespace(type="started", data={})

            async def finalize(self):
                return types.SimpleNamespace(type="finalized", data={})

            async def peer_events(self):
                if False:
                    yield None

        class FakeProvider:
            def __init__(self, rows):
                self.rows = rows

            async def list_models(self):
                return self.rows

            def catalog_note(self, _models):
                return "Claude CLI verified cache"

        engine_module = types.ModuleType("doxa.engine")
        engine_module.SessionEngine = FakeEngine
        for rows, expected, note in [
            ([types.SimpleNamespace(id="account-model", source="cache"),
              types.SimpleNamespace(id="guessed-alias", source="fallback")],
             ["account-model"], "Claude CLI verified cache"),
            ([types.SimpleNamespace(id="guessed-alias", source="fallback")],
             [], "No verified Claude model catalog available"),
        ]:
            with self.subTest(expected=expected):
                requests = iter({"type": "request", "id": i, "method": method, "params": params}
                                for i, (method, params) in enumerate([
                                    ("start", {"cwd": str(SIDECAR.parent), "session_id": "catalog"}),
                                    ("list_models", {}),
                                    ("list_models", {}),
                                    ("finalize", {}),
                                ], 1))
                replies = []

                async def read_frame(_reader, _limit):
                    try:
                        await asyncio.sleep(0)
                        return json.dumps(next(requests)).encode() + b"\n"
                    except StopIteration:
                        return b""

                probe = mock.AsyncMock(return_value="refreshed")

                with mock.patch.dict(sys.modules, {"doxa.engine": engine_module}), \
                     mock.patch.object(sidecar.asyncio, "to_thread", read_frame), \
                     mock.patch.object(sidecar, "emit", replies.append), \
                     mock.patch.object(claude_catalog, "attempt_cli_catalog_refresh", probe) as refresh, \
                     mock.patch.object(providers, "model_provider", return_value=FakeProvider(rows)), \
                     mock.patch.object(providers.ClaudeProvider, "startup_catalog_checked") as checked:
                    asyncio.run(sidecar.run())

                self.assertEqual(refresh.call_count, 1)
                checked.assert_called_with("refreshed")
                catalog_replies = [frame for frame in replies
                                   if frame.get("type") == "reply" and frame.get("id") in (2, 3)]
                self.assertEqual(len(catalog_replies), 2)
                self.assertTrue(all(frame["ok"] for frame in catalog_replies))
                self.assertTrue(all(frame["result"]["models"] == expected for frame in catalog_replies))
                self.assertTrue(all(frame["result"]["note"] == note for frame in catalog_replies))

    def test_effort_capabilities_require_exact_connected_model_metadata(self):
        engine = types.SimpleNamespace(set_effort=lambda _: None, server_info={"models": [
            {"value": "sonnet", "supportedEffortLevels": ["low", "high", "invented"]},
            {"value": "opus", "supportedEffortLevels": ["high", "xhigh", "max"]},
            {"value": "disabled", "supportsEffort": False, "supportedEffortLevels": ["low"]},
        ]})
        self.assertEqual(sidecar.model_effort_capabilities(engine, "sonnet"), ["low", "high"])
        self.assertEqual(sidecar.model_effort_capabilities(engine, "opus"), ["high", "xhigh", "max"])
        self.assertEqual(sidecar.model_effort_capabilities(engine, "disabled"), [])
        self.assertEqual(sidecar.model_effort_capabilities(engine, "sonnet-other"), [])
        engine.server_info = {"models": [{"value": "sonnet", "supportsEffort": True}]}
        self.assertEqual(sidecar.model_effort_capabilities(engine, "sonnet"), [])
        engine.server_info = None
        self.assertEqual(sidecar.model_effort_capabilities(engine, "sonnet"), [])

    def test_effort_aliases_use_advertised_resolved_model_and_explicit_id_precedence(self):
        # Primary CLI 2.1.283 model-info schema/constructor supplies resolvedModel;
        # an alias's spelling or human-readable description alone proves nothing.
        engine = types.SimpleNamespace(set_effort=lambda _: None, server_info={"models": [
            {"value": "opus", "resolvedModel": "claude-opus-4-6",
             "supportedEffortLevels": ["low", "medium", "high", "xhigh", "max"]},
            {"value": "sonnet[1m]", "resolvedModel": "claude-sonnet-4-6[1m]",
             "supportedEffortLevels": ["low", "medium", "high"]},
        ]})
        self.assertEqual(sidecar.model_effort_capabilities(engine, "claude-opus-4-6"),
                         ["low", "medium", "high", "xhigh", "max"])
        self.assertEqual(sidecar.model_effort_capabilities(engine, "claude-sonnet-4-6[1m]"),
                         ["low", "medium", "high"])
        self.assertEqual(sidecar.model_effort_capabilities(engine, "claude-sonnet-4-6"), [])
        self.assertEqual(sidecar.model_effort_capabilities(engine, "claude-opus-4-6-other"), [])
        engine.server_info["models"].append({"value": "claude-opus-4-6", "supportedEffortLevels": ["low"]})
        self.assertEqual(sidecar.model_effort_capabilities(engine, "claude-opus-4-6"), ["low"])
        engine.server_info["models"][-1]["supportsEffort"] = False
        self.assertEqual(sidecar.model_effort_capabilities(engine, "claude-opus-4-6"), [])

    def test_catalog_retries_empty_after_five_seconds_and_refreshes_verified_after_thirty(self):
        from doxa import claude_catalog, providers
        class FakeEngine:
            def __init__(self, peer_presence=True, **_options): pass
            async def start(self): return types.SimpleNamespace(type="started", data={})
            async def finalize(self): return types.SimpleNamespace(type="finalized", data={})
            async def peer_events(self):
                if False: yield None
        class FakeProvider:
            def __init__(self, populated): self.populated = populated
            async def list_models(self):
                return [types.SimpleNamespace(id="verified", source="cache")] if self.populated else []
            def catalog_note(self, _models): return "verified cache"
        engine_module = types.ModuleType("doxa.engine")
        engine_module.SessionEngine = FakeEngine
        for populated, ttl in [(False, 5.0), (True, 30.0)]:
            with self.subTest(populated=populated):
                clock = [100.0]
                frames = iter(enumerate([
                    ("start", {"cwd": str(SIDECAR.parent), "session_id": "catalog"}),
                    ("list_models", {}), ("list_models", {}),
                    ("list_models", {}), ("list_models", {}), ("finalize", {}),
                ], 1))
                async def read_frame(_reader, _limit):
                    try: identity, (method, params) = next(frames)
                    except StopIteration: return b""
                    if identity == 3: clock[0] = 100.0 + ttl - 0.1
                    if identity == 4: clock[0] = 100.0 + ttl
                    await asyncio.sleep(0)
                    return json.dumps({"type":"request", "id":identity, "method":method, "params":params}).encode()+b"\n"
                replies = []
                probe = mock.AsyncMock(return_value="refreshed")
                with mock.patch.dict(sys.modules, {"doxa.engine": engine_module}), \
                     mock.patch.object(sidecar.asyncio, "to_thread", read_frame), \
                     mock.patch.object(sidecar, "time", types.SimpleNamespace(monotonic=lambda:clock[0])), \
                     mock.patch.object(sidecar, "emit", replies.append), \
                     mock.patch.object(claude_catalog, "attempt_cli_catalog_refresh", probe), \
                     mock.patch.object(providers, "model_provider", return_value=FakeProvider(populated)), \
                     mock.patch.object(providers.ClaudeProvider, "startup_catalog_checked"):
                    asyncio.run(sidecar.run())
                self.assertEqual(probe.call_count, 2)
                by_id = {frame["id"]:frame["result"] for frame in replies if frame.get("type")=="reply"}
                self.assertNotIn("loading", by_id[3])
                self.assertTrue(by_id[4]["loading"])
                self.assertEqual(by_id[4]["models"], ["verified"] if populated else [])
                self.assertNotIn("loading", by_id[5])

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

    def test_oversize_frames_keep_reply_id_and_terminal_event(self):
        writes = []

        def record(_fd, data):
            writes.append(bytes(data))
            return len(data)

        with mock.patch.object(sidecar.os, "write", record):
            sidecar.emit({"type": "reply", "id": 37, "ok": True,
                          "result": {"secret": "x" * sidecar.MAX_FRAME}})
            sidecar.emit({"type": "event", "event": "turn_done",
                          "data": {"is_error": False, "text": "x" * sidecar.MAX_FRAME}})
            sidecar.emit({"type": "event", "event": "text_delta",
                          "data": {"text": "x" * sidecar.MAX_FRAME}})
        frames = [json.loads(raw) for raw in writes]
        self.assertEqual(frames[0], {"type": "reply", "id": 37, "ok": False,
                                     "error": "frame_too_large"})
        self.assertEqual(frames[1]["event"], "turn_done")
        self.assertEqual(frames[1]["data"], {"truncated": True,
                                              "error": "frame_too_large", "is_error": True})
        self.assertEqual(frames[2]["event"], "text_delta")
        self.assertTrue(frames[2]["data"]["truncated"])
        self.assertTrue(all(len(raw) <= sidecar.MAX_FRAME for raw in writes))

    def test_peer_pump_failure_reports_error_and_rejects_next_request(self):
        class FakeEngine:
            instance = None

            def __init__(self, peer_presence=True, **_options):
                self.finalize_calls = 0
                FakeEngine.instance = self

            async def start(self):
                return types.SimpleNamespace(type="started", data={})

            async def peer_events(self):
                raise RuntimeError("sensitive SDK detail")
                yield None

            async def finalize(self):
                self.finalize_calls += 1

        frames = [
            {"type": "request", "id": 1, "method": "start",
             "params": {"cwd": str(SIDECAR.parent), "session_id": "peer-fail"}},
            {"type": "request", "id": 2, "method": "list_models", "params": {}},
        ]
        replies = []

        async def read_frame(_reader, _limit):
            if not frames:
                return b""
            await asyncio.sleep(0)
            return json.dumps(frames.pop(0)).encode() + b"\n"

        engine_module = types.ModuleType("doxa.engine")
        engine_module.SessionEngine = FakeEngine
        with mock.patch.dict(sys.modules, {"doxa.engine": engine_module}), \
             mock.patch.object(sidecar.asyncio, "to_thread", read_frame), \
             mock.patch.object(sidecar, "emit", replies.append):
            asyncio.run(sidecar.run())
        self.assertIn({"type": "error", "code": "peer_pump_failed"}, replies)
        self.assertIn({"type": "reply", "id": 2, "ok": False,
                       "error": "peer_pump_failed"}, replies)
        self.assertEqual(FakeEngine.instance.finalize_calls, 1)
        self.assertNotIn("sensitive SDK detail", repr(replies))

    def test_start_emit_failure_finalizes_candidate_and_allows_retry(self):
        class FakeEngine:
            instances = []

            def __init__(self, peer_presence=True, **_options):
                self.finalize_calls = 0
                FakeEngine.instances.append(self)

            async def start(self):
                return types.SimpleNamespace(type="started", data={})

            async def finalize(self):
                self.finalize_calls += 1
                return types.SimpleNamespace(type="finalized", data={})

            async def peer_events(self):
                if False:
                    yield None

        frames = [
            {"type": "request", "id": i, "method": method,
             "params": {"cwd": str(SIDECAR.parent), "session_id": "retry"}
             if method == "start" else {}}
            for i, method in enumerate(("start", "start", "finalize"), 1)
        ]
        replies = []
        failed = False

        async def read_frame(_reader, _limit):
            return json.dumps(frames.pop(0)).encode() + b"\n" if frames else b""

        def fail_once(frame):
            nonlocal failed
            if frame.get("type") == "reply" and frame.get("id") == 1 and not failed:
                failed = True
                raise ValueError("write failed")
            replies.append(frame)

        engine_module = types.ModuleType("doxa.engine")
        engine_module.SessionEngine = FakeEngine
        with mock.patch.dict(sys.modules, {"doxa.engine": engine_module}), \
             mock.patch.object(sidecar.asyncio, "to_thread", read_frame), \
             mock.patch.object(sidecar, "emit", fail_once):
            asyncio.run(sidecar.run())
        self.assertEqual([engine.finalize_calls for engine in FakeEngine.instances], [1, 1])
        self.assertEqual([r["ok"] for r in replies if r.get("type") == "reply"],
                         [False, True, True])

    def test_oversize_start_reply_closes_candidate(self):
        class FakeEngine:
            instance = None

            def __init__(self, peer_presence=True, **_options):
                self.finalize_calls = 0
                FakeEngine.instance = self

            async def start(self):
                return types.SimpleNamespace(type="started",
                                             data={"huge": "x" * sidecar.MAX_FRAME})

            async def finalize(self):
                self.finalize_calls += 1

        frames = [{"type": "request", "id": 7, "method": "start",
                   "params": {"cwd": str(SIDECAR.parent), "session_id": "large-start"}}]
        writes = []

        async def read_frame(_reader, _limit):
            return json.dumps(frames.pop(0)).encode() + b"\n" if frames else b""

        def record(_fd, data):
            writes.append(bytes(data))
            return len(data)

        engine_module = types.ModuleType("doxa.engine")
        engine_module.SessionEngine = FakeEngine
        with mock.patch.dict(sys.modules, {"doxa.engine": engine_module}), \
             mock.patch.object(sidecar.asyncio, "to_thread", read_frame), \
             mock.patch.object(sidecar.os, "write", record):
            asyncio.run(sidecar.run())
        self.assertEqual(FakeEngine.instance.finalize_calls, 1)
        self.assertEqual(json.loads(writes[1]), {"type": "reply", "id": 7,
                                                 "ok": False, "error": "frame_too_large"})

    def test_finalize_cancels_catalog_probe(self):
        from doxa import claude_catalog

        class FakeEngine:
            def __init__(self, peer_presence=True, **_options):
                pass

            async def start(self):
                return types.SimpleNamespace(type="started", data={})

            async def finalize(self):
                return types.SimpleNamespace(type="finalized", data={})

            async def peer_events(self):
                if False:
                    yield None

        frames = [
            {"type": "request", "id": 1, "method": "start",
             "params": {"cwd": str(SIDECAR.parent), "session_id": "catalog-cancel"}},
            {"type": "request", "id": 2, "method": "finalize", "params": {}},
        ]
        probe_started = asyncio.Event()
        probe_cancelled = False

        async def probe():
            nonlocal probe_cancelled
            probe_started.set()
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                probe_cancelled = True
                raise

        async def read_frame(_reader, _limit):
            if not frames:
                return b""
            if frames[0]["id"] == 2:
                await probe_started.wait()
            return json.dumps(frames.pop(0)).encode() + b"\n"

        engine_module = types.ModuleType("doxa.engine")
        engine_module.SessionEngine = FakeEngine
        with mock.patch.dict(sys.modules, {"doxa.engine": engine_module}), \
             mock.patch.object(sidecar.asyncio, "to_thread", read_frame), \
             mock.patch.object(sidecar, "emit"), \
             mock.patch.object(claude_catalog, "attempt_cli_catalog_refresh", probe):
            asyncio.run(sidecar.run())
        self.assertTrue(probe_cancelled)

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

            def __init__(self, peer_presence=True, **_options):
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

            def __init__(self, peer_presence=True, **_options):
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

            def __init__(self, peer_presence=True, **_options):
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

            def __init__(self, peer_presence=True, **_options):
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

    def test_live_controls_validate_before_calling_engine(self):
        class FakeEngine:
            instance = None

            def __init__(self, peer_presence=True, **_options):
                FakeEngine.instance = self
                self.permission_mode = "default"
                self._turn_running = False
                self._prompt_queue = []
                self.models = []
                self.modes = []

            async def start(self):
                return types.SimpleNamespace(type="session_started", data={"model": "opus"})

            async def set_model(self, model):
                self.models.append(model)
                return model or "default"

            async def set_permission_mode(self, mode):
                self.modes.append(mode)
                self.permission_mode = mode
                return mode

            async def finalize(self):
                return types.SimpleNamespace(type="session_done", data={})

            async def peer_events(self):
                if False:
                    yield None

        frames = [
            ("start", {"cwd": str(SIDECAR.parent), "session_id": "controls"}),
            ("set_model", {"model": "haiku"}),
            ("set_model", {"model": "bad\nname"}),
            ("set_permission_mode", {"mode": "plan"}),
            ("set_permission_mode", {"mode": "bypassPermissions"}),
            ("finalize", {}),
        ]
        requests = iter({"type": "request", "id": i, "method": method, "params": params}
                        for i, (method, params) in enumerate(frames, 1))
        replies = []

        async def read_frame(_reader, _limit):
            try:
                return json.dumps(next(requests)).encode() + b"\n"
            except StopIteration:
                return b""

        engine_module = types.ModuleType("doxa.engine")
        engine_module.SessionEngine = FakeEngine
        with mock.patch.dict(sys.modules, {"doxa.engine": engine_module}), \
             mock.patch.object(sidecar.asyncio, "to_thread", read_frame), \
             mock.patch.object(sidecar, "emit", replies.append):
            asyncio.run(sidecar.run())
        self.assertIn("set_model", replies[0]["capabilities"])
        self.assertEqual([r["ok"] for r in replies if r["type"] == "reply"],
                         [True, True, False, True, False, True])
        self.assertEqual(FakeEngine.instance.models, ["haiku"])
        self.assertEqual(FakeEngine.instance.modes, ["plan"])


if __name__ == "__main__":
    unittest.main()

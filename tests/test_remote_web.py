# SPDX-License-Identifier: AGPL-3.0-only
"""The browser bridge enforces remote policy at its actual ASGI routes."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from doxa import peernet, remote_transport, remote_web, transcript
from doxa.events import EngineEvent


LOGIN = "alice@example.com"


@pytest.fixture(autouse=True)
def _remote_off(monkeypatch):
    monkeypatch.delenv("DOXA_REMOTE_ENABLED", raising=False)
    monkeypatch.delenv("DOXA_REMOTE_ALLOWED_LOGINS", raising=False)
    monkeypatch.delenv("DOXA_REMOTE_ALLOW_SHELL", raising=False)
    monkeypatch.delenv("DOXA_REMOTE_ALLOW_BYPASS", raising=False)


def _client(*, login=LOGIN, address="127.0.0.1"):
    headers = {"Tailscale-User-Login": login} if login else {}
    return TestClient(remote_web.create_app(), headers=headers, client=(address, 50000))


@pytest.fixture
def attested_test_proxy(monkeypatch):
    """Renderer fixture only; production TCP never supplies this evidence."""
    monkeypatch.setattr(remote_web, "_attested_proxy", lambda scope:
                        scope.get("client", (None,))[0] == "127.0.0.1")


def _session():
    return SimpleNamespace(
        session_id="session-1", title="Code review", engine="codex",
        model="gpt-6-sol", clients=1, cwd="/repo", daemon_socket="/tmp/fake.sock",
    )


def test_bridge_routes_reject_by_default_even_with_identity(monkeypatch):
    monkeypatch.setattr(remote_web.peers, "list_daemons", lambda: [_session()])
    with _client() as client:
        response = client.get("/api/sessions")
        assert response.status_code == 403
        assert response.json()["error"]
        assert client.get("/").status_code == 403
        assert client.get("/api/sessions/session-1/transcript").status_code == 403


def test_identity_allow_list_and_attested_transport_gate_real_routes(monkeypatch, attested_test_proxy):
    monkeypatch.setenv("DOXA_REMOTE_ENABLED", "1")
    monkeypatch.setenv("DOXA_REMOTE_ALLOWED_LOGINS", LOGIN)
    monkeypatch.setattr(remote_web.peers, "list_daemons", lambda: [_session()])

    with _client(login="mallory@example.com") as client:
        refused = client.get("/api/sessions")
        assert refused.status_code == 403
        assert "mallory@example.com" in refused.json()["error"]
    with _client(login=None) as client:
        assert client.get("/api/sessions").status_code == 403
    with _client(address="198.51.100.7") as client:
        refused = client.get("/api/sessions")
        assert refused.status_code == 403
        assert "loopback" in refused.json()["error"].lower()

    with _client() as client:
        allowed = client.get("/api/sessions")
        assert allowed.status_code == 200
        assert allowed.json() == {"sessions": [{
            "id": "session-1", "title": "Code review", "engine": "codex",
            "model": "gpt-6-sol", "clients": 1,
        }]}
        assert allowed.headers["cache-control"] == "no-store"


def test_transcript_reads_only_a_live_session_after_authorization(monkeypatch, attested_test_proxy):
    monkeypatch.setenv("DOXA_REMOTE_ENABLED", "1")
    monkeypatch.setenv("DOXA_REMOTE_ALLOWED_LOGINS", LOGIN)
    monkeypatch.setattr(remote_web.peers, "list_daemons", lambda: [_session()])
    reads = []

    def read(session_id, cwd):
        reads.append((session_id, cwd))
        return transcript.Transcript(
            turns=[transcript.Turn(prompt="Review this", text="Looks good")],
            dropped_turns=2,
        )

    monkeypatch.setattr(remote_web.transcript, "read", read)
    with _client(login="mallory@example.com") as client:
        assert client.get("/api/sessions/session-1/transcript").status_code == 403
    assert reads == []

    with _client() as client:
        missing = client.get("/api/sessions/missing/transcript")
        assert missing.status_code == 404
        assert reads == []
        response = client.get("/api/sessions/session-1/transcript")
        assert response.status_code == 200
        assert response.json() == {
            "turns": [{"prompt": "Review this", "text": "Looks good", "tools": [],
                       "text_truncated": False, "tools_dropped": 0}],
            "dropped_turns": 2,
        }
        assert response.headers["cache-control"] == "no-store"
    assert reads == [("session-1", "/repo")]


def test_cross_origin_websocket_cannot_attach_even_when_identity_is_allowed(monkeypatch, attested_test_proxy):
    monkeypatch.setenv("DOXA_REMOTE_ENABLED", "1")
    monkeypatch.setenv("DOXA_REMOTE_ALLOWED_LOGINS", LOGIN)
    monkeypatch.setattr(remote_web.peers, "list_daemons", lambda: [_session()])

    def must_not_attach(*args, **kwargs):
        raise AssertionError("authorization should run before EngineClient attach")

    monkeypatch.setattr(remote_web, "EngineClient", must_not_attach)
    with _client() as client:
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(
                "/api/sessions/session-1/events",
                headers={"Origin": "https://evil.example"},
            ):
                pass
        assert exc.value.code == 1008


def test_same_origin_allowed_websocket_attaches_and_receives_hello(monkeypatch, attested_test_proxy):
    """Companion to the cross-origin refusal: a valid origin can attach."""
    monkeypatch.setenv("DOXA_REMOTE_ENABLED", "1")
    monkeypatch.setenv("DOXA_REMOTE_ALLOWED_LOGINS", LOGIN)
    monkeypatch.setattr(remote_web.peers, "list_daemons", lambda: [_session()])
    attached = []
    prompts = []

    class FakeEngineClient:
        def __init__(self, socket, *, skip_backlog, remote_login):
            assert socket == "/tmp/fake.sock"
            assert skip_backlog is True
            assert remote_login == LOGIN
            self.session_id = "session-1"
            self.engine_id = "codex"
            self.model = "gpt-6-sol"
            self.permission_mode = "bypassPermissions"

        async def start(self):
            attached.append("started")

        async def refresh_status(self):
            return {"pending_inputs": [{
                "id": "waiting-1", "kind": "permission", "tool_name": "Bash",
                "input_summary": "run tests",
            }]}

        async def send(self, prompt):
            prompts.append(prompt)
            yield EngineEvent("turn_done", {})

        async def peer_events(self):
            await asyncio.Future()
            yield  # pragma: no cover — make this an async generator

        async def finalize(self):
            attached.append("finalized")

    monkeypatch.setattr(remote_web, "EngineClient", FakeEngineClient)
    with _client() as client:
        with client.websocket_connect(
            "/api/sessions/session-1/events", headers={"Origin": "http://testserver"}
        ) as ws:
            assert ws.receive_json() == {
                "type": "hello", "session_id": "session-1",
                "engine": "codex", "model": "gpt-6-sol",
            }
            assert ws.receive_json() == {"type": "event", "event": {
                "type": "needs_input", "data": {
                    "id": "waiting-1", "kind": "permission",
                    "tool_name": "Bash", "input_summary": "run tests",
                },
            }}
            ws.send_json({"op": "prompt", "text": "run this"})
            refused = ws.receive_json()
            assert refused["type"] == "error"
            assert "remote_allow_bypass" in refused["message"]
            assert prompts == []
            monkeypatch.setenv("DOXA_REMOTE_ALLOW_BYPASS", "1")
            ws.send_json({"op": "prompt", "text": "run this"})
            assert ws.receive_json()["event"]["type"] == "turn_done"
            assert prompts == ["run this"]
            monkeypatch.setenv("DOXA_REMOTE_ENABLED", "0")
            with pytest.raises(WebSocketDisconnect) as revoked:
                ws.receive_json()
            assert revoked.value.code == 1008
    assert attached == ["started", "finalized"]


def test_listener_requires_opt_in_allow_list_and_private_unix_socket(monkeypatch, tmp_path):
    with pytest.raises(SystemExit) as off:
        remote_web.main([])
    assert off.value.code == 2

    monkeypatch.setenv("DOXA_REMOTE_ENABLED", "1")
    with pytest.raises(SystemExit) as empty_list:
        remote_web.main([])
    assert empty_list.value.code == 2

    monkeypatch.setenv("DOXA_REMOTE_ALLOWED_LOGINS", LOGIN)
    with pytest.raises(SystemExit) as public_bind:
        remote_web.main(["--host", "0.0.0.0"])
    assert public_bind.value.code == 2

    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path))
    calls = []
    class FakeServer:
        def __init__(self, config):
            assert config.proxy_headers is False
            calls.append(config)
        def run(self, *, sockets):
            assert len(sockets) == 1
            assert sockets[0].family == __import__("socket").AF_UNIX
            assert (tmp_path / "remote-browser.sock").stat().st_mode & 0o777 == 0o600
    monkeypatch.setattr("uvicorn.Server", FakeServer)
    assert remote_web.main([]) == 0
    assert len(calls) == 1
    assert not (tmp_path / "remote-browser.sock").exists()


def test_duplicate_identity_header_refused_even_on_attested_scope(monkeypatch, attested_test_proxy):
    monkeypatch.setenv("DOXA_REMOTE_ENABLED", "1")
    monkeypatch.setenv("DOXA_REMOTE_ALLOWED_LOGINS", LOGIN)
    with _client() as client:
        response = client.get("/api/sessions", headers=[("Tailscale-User-Login", LOGIN),
                                                        ("Tailscale-User-Login", "mallory@example.com")])
        assert response.status_code == 403
        assert "exactly one" in response.json()["error"]


def test_real_unix_http_transport_attests_each_connection(monkeypatch, tmp_path):
    import httpx
    import uvicorn
    from websockets.exceptions import InvalidStatus
    from websockets.sync.client import unix_connect

    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("DOXA_REMOTE_ENABLED", "1")
    monkeypatch.setenv("DOXA_REMOTE_ALLOWED_LOGINS", LOGIN)
    monkeypatch.setattr(remote_web.peers, "list_daemons", lambda: [_session()])
    path = tmp_path / "remote-browser.sock"
    listener = remote_transport.private_listener(path)
    http, ws = remote_transport.protocols()
    server = uvicorn.Server(uvicorn.Config(remote_web.create_app(), http=http, ws=ws,
                                            interface="asgi3", lifespan="off", log_level="error"))
    worker = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    worker.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        with httpx.Client(transport=httpx.HTTPTransport(uds=str(path)), base_url="http://localhost",
                          headers={"Tailscale-User-Login": LOGIN}) as client:
            if os.geteuid() != 0:
                assert client.get("/api/sessions").status_code == 403  # same-user socket is not tailscaled
                with pytest.raises(InvalidStatus):
                    unix_connect(str(path), "ws://localhost/api/sessions/session-1/events",
                                 additional_headers={"Tailscale-User-Login": LOGIN}, origin="http://localhost")
        monkeypatch.setattr(peernet, "proxy_uid", lambda: os.geteuid())
        with httpx.Client(transport=httpx.HTTPTransport(uds=str(path)), base_url="http://localhost",
                          headers={"Tailscale-User-Login": LOGIN}) as client:
            assert client.get("/api/sessions").status_code == 200
        class FakeEngineClient:
            def __init__(self, *args, **kwargs):
                self.session_id = "session-1"
                self.engine_id = "codex"
                self.model = "gpt-6-sol"
            async def start(self):
                pass
            async def refresh_status(self):
                return {}
            async def peer_events(self):
                await asyncio.Future()
                yield
            async def finalize(self):
                pass
        monkeypatch.setattr(remote_web, "EngineClient", FakeEngineClient)
        with unix_connect(str(path), "ws://localhost/api/sessions/session-1/events",
                          additional_headers={"Tailscale-User-Login": LOGIN}, origin="http://localhost") as connection:
            assert json.loads(connection.recv()) == {"type": "hello", "session_id": "session-1",
                                                     "engine": "codex", "model": "gpt-6-sol"}
    finally:
        server.should_exit = True
        worker.join(timeout=5)
        listener.close()
    assert not worker.is_alive()


def test_forged_loopback_identity_and_scope_metadata_never_attest_proxy(monkeypatch):
    monkeypatch.setenv("DOXA_REMOTE_ENABLED", "1")
    monkeypatch.setenv("DOXA_REMOTE_ALLOWED_LOGINS", LOGIN)
    monkeypatch.setattr(remote_web.peers, "list_daemons", lambda: pytest.fail("unattested caller reached registry"))
    monkeypatch.setattr(remote_web, "EngineClient", lambda *args, **kwargs: pytest.fail("unattested caller attached"))
    headers = {"tailscale-user-login": LOGIN, "host": "127.0.0.1:47601", "origin": "http://127.0.0.1:47601"}
    scope = {"client": ("127.0.0.1", 12345), "extensions": {"proxy_attested": True},
             "tailscale_user_login": LOGIN, "headers": [(b"tailscale-user-login", LOGIN.encode())]}
    decision = remote_web._decision(remote_web.remote_policy.REQUEST_SEND_PROMPT, scope, headers)
    assert not decision.allowed
    assert "identity header ignored" in decision.reason
    with _client() as client:
        for route in ["/", "/remote.js", "/remote.css", "/api/sessions", "/api/sessions/session-1/transcript"]:
            assert client.get(route).status_code == 403
        with pytest.raises(WebSocketDisconnect) as refused:
            with client.websocket_connect("/api/sessions/session-1/events", headers={"Origin": "http://testserver"}):
                pass
        assert refused.value.code == 1008


@pytest.mark.asyncio
async def test_browser_socket_drives_the_same_live_daemon_as_a_local_client(
    tmp_path, monkeypatch, attested_test_proxy,
):
    """Exercise the real Unix socket through the ASGI bridge and WebSocket."""
    websockets = pytest.importorskip("websockets")
    import uvicorn

    from doxa.client import EngineClient
    from tests.test_daemon import _drain_oob, running_daemon

    monkeypatch.setenv("DOXA_REMOTE_ENABLED", "1")
    monkeypatch.setenv("DOXA_REMOTE_ALLOWED_LOGINS", LOGIN)
    async with running_daemon(tmp_path, monkeypatch) as (daemon, _, _):
        local = EngineClient(str(daemon.socket_path), skip_backlog=True)
        await local.start()
        server = uvicorn.Server(uvicorn.Config(
            remote_web.create_app(), host="127.0.0.1", port=0,
            log_level="error", lifespan="off",
        ))
        server_task = asyncio.create_task(server.serve())
        try:
            for _ in range(200):
                if server.started:
                    break
                await asyncio.sleep(0.01)
            assert server.started
            port = server.servers[0].sockets[0].getsockname()[1]
            url = f"ws://127.0.0.1:{port}/api/sessions/{daemon.session_id}/events"
            async with websockets.connect(
                url, origin=f"http://127.0.0.1:{port}",
                additional_headers={"Tailscale-User-Login": LOGIN},
            ) as ws:
                hello = json.loads(await asyncio.wait_for(ws.recv(), 5))
                assert hello["type"] == "hello"
                assert hello["session_id"] == daemon.session_id
                await _drain_oob(local, "remote_driver_changed")
                assert local.remote_driver == LOGIN
                assert (await local.refresh_status())["remote_driver"] == LOGIN
                await ws.send(json.dumps({"op": "prompt", "text": "Say hello"}))
                kinds = []
                for _ in range(30):
                    message = json.loads(await asyncio.wait_for(ws.recv(), 5))
                    if message["type"] == "event":
                        kinds.append(message["event"]["type"])
                    if "turn_done" in kinds:
                        break
                assert "turn_started" in kinds
                assert "text_delta" in kinds
                assert "turn_done" in kinds
            await _drain_oob(local, "remote_driver_changed")
            assert local.remote_driver is None
        finally:
            server.should_exit = True
            await asyncio.wait_for(server_task, 5)
            await local.finalize()

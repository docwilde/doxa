# SPDX-License-Identifier: AGPL-3.0-only
"""A small browser client for daemon-hosted DOXA sessions.

Run behind Tailscale Serve. The bridge binds loopback, accepts only the
identity header Serve supplies, and asks remote_policy about every action.
The daemon's Unix socket and protocol remain local and unchanged.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import ipaddress
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import peers, remote_policy, transcript
from .client import EngineClient


def _loopback(scope: dict) -> bool:
    client = scope.get("client")
    try:
        return bool(client) and ipaddress.ip_address(client[0]).is_loopback
    except ValueError:
        return False


def _same_origin(headers: Any) -> bool:
    """Reject cross-site browser writes and WebSocket reads."""
    origin = headers.get("origin")
    if not origin:
        return True  # Non-browser clients still need the Serve identity gate.
    host = headers.get("host", "")
    parsed = urlsplit(origin)
    return parsed.scheme in ("http", "https") and parsed.netloc == host


def _decision(kind: str, scope: dict, headers: Any) -> remote_policy.Decision:
    return remote_policy.evaluate(
        kind,
        login=headers.get("tailscale-user-login"),
        from_loopback=_loopback(scope),
    )


def _session(session_id: str):
    return next(
        (p for p in peers.list_daemons() if p.session_id == session_id), None
    )


def create_app():
    """Build the optional ASGI bridge without importing web dependencies on launch."""
    from starlette.applications import Starlette
    from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse
    from starlette.routing import Route, WebSocketRoute
    from starlette.websockets import WebSocketDisconnect

    async def index(request):
        decision = _decision(remote_policy.REQUEST_READ_STATUS, request.scope, request.headers)
        if not decision.allowed:
            return PlainTextResponse(decision.reason, status_code=403)
        return HTMLResponse(_HTML, headers={
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'",
            "Cache-Control": "no-store",
        })

    async def script(request):
        decision = _decision(remote_policy.REQUEST_READ_STATUS, request.scope, request.headers)
        if not decision.allowed:
            return PlainTextResponse(decision.reason, status_code=403)
        source = Path(__file__).with_name("remote_client.js").read_text()
        return PlainTextResponse(source, media_type="text/javascript")

    async def style(request):
        decision = _decision(remote_policy.REQUEST_READ_STATUS, request.scope, request.headers)
        if not decision.allowed:
            return PlainTextResponse(decision.reason, status_code=403)
        return PlainTextResponse(_STYLE, media_type="text/css")

    async def sessions(request):
        decision = _decision(remote_policy.REQUEST_READ_STATUS, request.scope, request.headers)
        if not decision.allowed:
            return JSONResponse({"error": decision.reason}, status_code=403)
        return JSONResponse({"sessions": [
            {"id": p.session_id, "title": p.title, "engine": p.engine,
             "model": p.model, "clients": p.clients}
            for p in peers.list_daemons()
        ]}, headers={"Cache-Control": "no-store"})

    async def history(request):
        decision = _decision(remote_policy.REQUEST_READ_TRANSCRIPT, request.scope, request.headers)
        if not decision.allowed:
            return JSONResponse({"error": decision.reason}, status_code=403)
        entry = _session(request.path_params["session_id"])
        if entry is None:
            return JSONResponse({"error": "session not found"}, status_code=404)
        record = await asyncio.to_thread(transcript.read, entry.session_id, entry.cwd)
        return JSONResponse({"turns": [asdict(t) for t in record.turns],
                             "dropped_turns": record.dropped_turns},
                            headers={"Cache-Control": "no-store"})

    async def events(ws):
        decision = _decision(remote_policy.REQUEST_READ_STATUS, ws.scope, ws.headers)
        if not decision.allowed or not _same_origin(ws.headers):
            await ws.close(code=1008, reason=decision.reason if not decision.allowed else "cross-origin connection refused")
            return
        entry = _session(ws.path_params["session_id"])
        if entry is None:
            await ws.close(code=1008, reason="session not found")
            return
        await ws.accept()
        client = EngineClient(
            entry.daemon_socket, skip_backlog=True,
            remote_login=ws.headers["tailscale-user-login"],
        )
        try:
            await client.start()
        except Exception as exc:
            await ws.send_json({"type": "error", "message": f"cannot attach: {exc}"})
            await ws.close(code=1011)
            return

        outbound: asyncio.Queue[dict] = asyncio.Queue(maxsize=512)
        overloaded = False

        def enqueue(message: dict) -> None:
            nonlocal overloaded
            if overloaded:
                return
            try:
                outbound.put_nowait(message)
            except asyncio.QueueFull:
                # A slow browser must not make the daemon's event pump wait.
                overloaded = True
                while not outbound.empty():
                    outbound.get_nowait()
                outbound.put_nowait({"type": "error", "message":
                                     "remote client fell behind; reconnect for the transcript"})

        async def pump_events():
            async for ev in client.peer_events():
                enqueue({"type": "event", "event": asdict(ev)})

        async def pump_send(prompt: str):
            try:
                async for ev in client.send(prompt):
                    enqueue({"type": "event", "event": asdict(ev)})
            except Exception as exc:
                enqueue({"type": "error", "message": str(exc)})

        async def pump_outbound():
            while True:
                await ws.send_json(await outbound.get())

        async def watch_access():
            while True:
                await asyncio.sleep(2)
                if not _decision(remote_policy.REQUEST_READ_STATUS, ws.scope, ws.headers).allowed:
                    return

        tasks = [asyncio.create_task(pump_events()),
                 asyncio.create_task(pump_outbound()),
                 asyncio.create_task(watch_access())]
        turns: set[asyncio.Task] = set()
        access_revoked = False
        enqueue({"type": "hello", "session_id": client.session_id,
                 "engine": client.engine_id, "model": client.model})
        try:
            status = await client.refresh_status()
            for pending in status.get("pending_inputs") or []:
                enqueue({"type": "event", "event": {"type": "needs_input", "data": pending}})
            while True:
                incoming = asyncio.create_task(ws.receive_json())
                done, _ = await asyncio.wait(
                    {incoming, *tasks}, return_when=asyncio.FIRST_COMPLETED
                )
                if incoming not in done:
                    access_revoked = tasks[2] in done
                    incoming.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await incoming
                    for finished in done:
                        with contextlib.suppress(Exception, asyncio.CancelledError):
                            finished.result()
                    break
                message = incoming.result()
                if not isinstance(message, dict):
                    continue
                op = message.get("op")
                if op == "prompt":
                    allowed = _decision(remote_policy.REQUEST_SEND_PROMPT, ws.scope, ws.headers)
                    prompt = message.get("text")
                    if not allowed.allowed or not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 60_000:
                        enqueue({"type": "error", "message": allowed.reason if not allowed.allowed else "invalid prompt"})
                        continue
                    if (
                        client.permission_mode == remote_policy.BYPASS_MODE_NAME
                        and not remote_policy.remote_allow_bypass()
                    ):
                        enqueue({"type": "error", "message":
                                 "remote prompts in bypassPermissions require remote_allow_bypass"})
                        continue
                    if turns:
                        enqueue({"type": "error", "message": "wait for this turn to finish"})
                        continue
                    task = asyncio.create_task(pump_send(prompt))
                    turns.add(task)
                    task.add_done_callback(turns.discard)
                elif op == "answer":
                    answer = message.get("answer")
                    kind = (remote_policy.REQUEST_APPROVE_TOOL
                            if isinstance(answer, dict) and answer.get("decision") == "allow"
                            else remote_policy.REQUEST_DENY_TOOL)
                    allowed = _decision(kind, ws.scope, ws.headers)
                    if not allowed.allowed:
                        enqueue({"type": "error", "message": allowed.reason})
                        continue
                    request_id = message.get("id")
                    if not isinstance(request_id, str) or not isinstance(answer, dict):
                        enqueue({"type": "error", "message": "invalid answer"})
                        continue
                    ok = await client.answer_needs_input(request_id, answer)
                    if not ok:
                        enqueue({"type": "error", "message": "question has already been resolved"})
                else:
                    enqueue({"type": "error", "message": "unknown operation"})
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            for task in tasks + list(turns):
                task.cancel()
            await client.finalize()
            with contextlib.suppress(Exception):
                await ws.close(code=1008 if access_revoked else 1000)

    return Starlette(routes=[
        Route("/", index), Route("/remote.js", script), Route("/remote.css", style),
        Route("/api/sessions", sessions),
        Route("/api/sessions/{session_id}/transcript", history),
        WebSocketRoute("/api/sessions/{session_id}/events", events),
    ])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve DOXA sessions to a browser through Tailscale Serve")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=47601)
    args = parser.parse_args(argv)
    if args.host != "127.0.0.1":
        parser.error("the browser bridge only binds 127.0.0.1")
    decision = remote_policy.remote_listening_decision(enabled=remote_policy.remote_enabled())
    if not decision.allowed:
        parser.error(decision.reason)
    if not remote_policy.allowed_logins():
        parser.error("remote_allowed_logins is empty; no one can authenticate")
    import uvicorn

    uvicorn.run(create_app(), host=args.host, port=args.port, ws_max_size=64 * 1024)
    return 0


_HTML = """<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>DOXA remote</title><link rel="stylesheet" href="/remote.css"><main><header><strong>DOXA</strong><span id="status">Connecting…</span></header><nav id="sessions" aria-label="Sessions"></nav><section id="conversation" aria-live="polite"></section><section id="question" hidden></section><form id="prompt"><textarea id="prompt-text" aria-label="Message" placeholder="Message this session" rows="3"></textarea><button>Send</button></form></main><script src="/remote.js" defer></script></html>"""

_STYLE = """*{box-sizing:border-box}body{margin:0;background:#111318;color:#ececf1;font:16px/1.45 system-ui}main{max-width:880px;margin:auto;min-height:100vh;display:flex;flex-direction:column}header{padding:14px 18px;border-bottom:1px solid #343946;display:flex;justify-content:space-between}nav{display:flex;overflow:auto;gap:8px;padding:10px;border-bottom:1px solid #343946}nav button{white-space:nowrap}button{background:#29394c;color:white;border:1px solid #526985;border-radius:7px;padding:9px 12px;cursor:pointer}button[aria-current=true]{background:#486582}#conversation{flex:1;overflow:auto;padding:14px;white-space:pre-wrap}.turn{margin:0 0 20px}.user{color:#a9cef1}.assistant{color:#f0f0f2}.tool,.error,.notice{color:#cbbf9b}#question{padding:12px;background:#29303b}#question button{margin:8px 8px 0 0}form{display:flex;gap:8px;padding:12px;border-top:1px solid #343946}textarea{flex:1;min-width:0;background:#1b1f27;color:white;border:1px solid #526985;border-radius:7px;padding:10px;font:inherit}form button{align-self:end}"""




if __name__ == "__main__":
    raise SystemExit(main())

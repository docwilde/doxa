# SPDX-License-Identifier: AGPL-3.0-only
"""Versioned stdio bridge to the existing Python Claude Agent SDK engine.

This process is intentionally separate from the Rust core. Only protocol
frames go to stdout; stderr diagnostics are suppressed by the Rust client.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

MAX_FRAME = 64 * 1024
PROTOCOL = "doxa-claude-sidecar"
VERSION = 1
EOF_FINALIZE_TIMEOUT = 5.0
TASK_CANCEL_TIMEOUT = 1.0


def validate_identity(session_id: str | None, resume: str | None) -> tuple[str | None, str | None]:
    """Reject unsafe or contradictory IDs before SessionEngine touches paths."""
    from doxa.identity import valid_session_id

    if session_id is not None and (
        not isinstance(session_id, str) or not valid_session_id(session_id)
    ):
        raise ValueError("invalid session id")
    if resume is not None and (
        not isinstance(resume, str) or not valid_session_id(resume)
    ):
        raise ValueError("invalid resume id")
    if resume is not None:
        if session_id is not None and session_id != resume:
            raise ValueError("resume must match session id")
        session_id = resume
    return session_id, resume


def emit(frame: dict) -> None:
    raw = json.dumps(frame, ensure_ascii=False, separators=(",", ":")).encode()
    if len(raw) + 1 > MAX_FRAME:
        # Do not truncate a JSON event into a misleading partial result.
        raw = json.dumps({"type": "error", "code": "frame_too_large"}).encode()
    data = memoryview(raw + b"\n")
    while data:
        written = os.write(sys.stdout.fileno(), data)
        if written <= 0:
            raise OSError("sidecar stdout closed")
        data = data[written:]


async def run() -> None:
    from doxa.engine import SessionEngine

    emit({"type": "hello", "protocol": PROTOCOL, "version": VERSION,
          "capabilities": ["start", "prompt", "answer", "interrupt", "finalize",
                           "set_model", "set_permission_mode"]})
    engine = None
    turn = None

    async def publish_turn(prompt: str) -> None:
        try:
            async for event in engine.send(prompt):
                emit({"type": "event", "event": event.type, "data": event.data})
        except asyncio.CancelledError:
            try:
                emit({"type": "event", "event": "turn_interrupted", "data": {}})
            except OSError:
                # The parent may have closed stdout along with stdin.
                pass
            raise
        except Exception:
            emit({"type": "event", "event": "turn_done",
                  "data": {"is_error": True, "error": "Claude turn failed"}})

    async def publish_out_of_band() -> None:
        async for event in engine.peer_events():
            emit({"type": "event", "event": event.type, "data": event.data})

    peer_task = None
    reached_eof = False
    finalized = False
    while True:
        raw = await asyncio.to_thread(sys.stdin.buffer.readline, MAX_FRAME + 1)
        if not raw:
            reached_eof = True
            break
        if len(raw) > MAX_FRAME or not raw.endswith(b"\n"):
            emit({"type": "error", "code": "invalid_frame"})
            break
        try:
            frame = json.loads(raw)
            if not isinstance(frame, dict) or frame.get("type") != "request":
                raise ValueError()
            request_id = frame["id"]
            if not isinstance(request_id, int) or isinstance(request_id, bool) or request_id < 1:
                raise ValueError()
            method = frame["method"]
            params = frame["params"]
            if not isinstance(params, dict):
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            emit({"type": "error", "code": "invalid_request"})
            continue
        try:
            if method == "start" and engine is None:
                cwd = params["cwd"]
                if not isinstance(cwd, str) or not os.path.isdir(cwd):
                    raise ValueError("invalid cwd")
                session_id, resume = validate_identity(
                    params.get("session_id"), params.get("resume")
                )
                model = params.get("model")
                if model is not None and not isinstance(model, str):
                    raise ValueError("invalid start option")
                options = {"cwd": cwd, "session_id": session_id, "resume": resume}
                if model is not None:
                    options["model"] = model
                candidate = SessionEngine(**options)
                started = await candidate.start()
                engine = candidate
                emit({"type": "reply", "id": request_id, "ok": True,
                      "result": {"event": started.type, "data": started.data,
                                 "permission_mode": getattr(candidate, "permission_mode", "default")}})
                peer_task = asyncio.create_task(publish_out_of_band())
            elif method == "prompt" and engine is not None:
                prompt = params["text"]
                if not isinstance(prompt, str):
                    raise ValueError("invalid prompt")
                if turn is not None and not turn.done():
                    raise ValueError("turn running")
                turn = asyncio.create_task(publish_turn(prompt))
                emit({"type": "reply", "id": request_id, "ok": True, "result": {}})
            elif method == "answer" and engine is not None:
                answer = params["answer"]
                if not isinstance(answer, dict) or not isinstance(params["id"], str):
                    raise ValueError("invalid answer")
                applied = await engine.answer_needs_input(params["id"], answer)
                emit({"type": "reply", "id": request_id, "ok": True,
                      "result": {"applied": applied}})
            elif method == "set_model" and engine is not None:
                model = params["model"]
                if model is not None and (
                    not isinstance(model, str) or not model.strip()
                    or len(model) > 256 or any(ord(char) < 32 for char in model)
                ):
                    raise ValueError("invalid model")
                selected = await engine.set_model(model)
                emit({"type": "reply", "id": request_id, "ok": True,
                      "result": {"model": selected}})
            elif method == "set_permission_mode" and engine is not None:
                mode = params["mode"]
                if mode not in ("default", "acceptEdits", "plan", "auto", "dontAsk"):
                    # The Rust launcher has no bypass-arming option. Even if
                    # an inherited environment arms the Python engine, this
                    # sidecar must not make bypass reachable through the RPC.
                    raise ValueError("unsupported permission mode")
                if mode == "dontAsk" and (
                    getattr(engine, "_turn_running", False)
                    or len(getattr(engine, "_prompt_queue", ()))
                ):
                    raise ValueError("dontAsk requires an idle session")
                selected = await engine.set_permission_mode(mode)
                emit({"type": "reply", "id": request_id, "ok": True,
                      "result": {"mode": selected}})
            elif method == "interrupt" and engine is not None:
                if engine._client is None:
                    raise ValueError("not connected")
                await engine._client.interrupt()
                emit({"type": "reply", "id": request_id, "ok": True, "result": {}})
            elif method == "finalize" and engine is not None:
                if turn and not turn.done():
                    raise ValueError("turn running")
                done = await engine.finalize()
                finalized = True
                emit({"type": "reply", "id": request_id, "ok": True,
                      "result": {"event": done.type, "data": done.data}})
                break
            else:
                raise ValueError("invalid state or method")
        except Exception:
            # SDK exception strings can contain sensitive request material.
            emit({"type": "reply", "id": request_id, "ok": False,
                  "error": "operation_failed"})
    running = [task for task in (peer_task, turn) if task is not None and not task.done()]
    for task in running:
        task.cancel()
    settled = True
    if running:
        _, pending = await asyncio.wait(running, timeout=TASK_CANCEL_TIMEOUT)
        settled = not pending
    if reached_eof and engine is not None and not finalized and settled:
        # A disappearing parent cannot send an explicit finalize request.
        # Give the engine a bounded chance to close its SDK client, stop peer
        # presence, and index the transcript before this process exits. A
        # cancellation-resistant task may still be using the SDK; do not run
        # finalization concurrently with it.
        try:
            await asyncio.wait_for(engine.finalize(), timeout=EOF_FINALIZE_TIMEOUT)
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(run())

"""doxa.client -- the thin-client side of the daemon split.

:class:`EngineClient` presents the SAME surface doxa.app consumes from
``SessionEngine`` (start / send / peer_events / list_peers / peer_count /
send_peer_message / belief_count / disabled_tools / finalize, plus the
model/total_cost_usd/last_ctx_percentage attributes) -- the app swaps one
handle for the other behind a factory and barely notices. Underneath, every
call is a line-JSON frame over the daemon's Unix socket (protocol sketch in
doxa/daemon.py's docstring).

Semantics that differ from the in-process engine, deliberately:

* ``finalize()`` DETACHES: it closes the socket and leaves the daemon (and
  the session) running -- the daemon finalizes on its own once the last
  client has been gone for the linger window. ``stop()`` is the explicit
  "finalize NOW" path (`doxa stop`, the palette's quit-stop).
* Events the client did not initiate -- replayed history after a reattach,
  turns driven by another attached client, peer traffic, two-strikes
  disables -- all arrive on :meth:`peer_events`, the same out-of-band
  stream the app already pumps. Turn events are told apart from the
  client's own live turn by the ``turn`` tag the daemon stamps on every
  event frame.
* Status values (belief count, peers, disabled tools, cost) are CACHED from
  the daemon's status replies -- the app reads them synchronously mid-render
  exactly like it reads the engine's attributes, so a socket round-trip per
  read is not an option. The cache refreshes on start, after every local
  turn, and whenever a peer join/leave or tool-disable event arrives.

Nothing in this module persists anything: scrub discipline lives where it
always lived, at the engine's persistence choke point, daemon-side.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
from collections.abc import AsyncIterator
from typing import Any

from .daemon import PROTOCOL_VERSION
from .engine import EngineEvent
from .peers import MAX_FRAME_BYTES, PeerInfo, PeerSendError

CALL_TIMEOUT_SECS = 15.0
HELLO_TIMEOUT_SECS = 10.0


class EngineClientError(RuntimeError):
    """Connection/protocol failure between the TUI and the daemon."""


class EngineClient:
    """One attached client of a :class:`doxa.daemon.SessionDaemon`."""

    detachable = True  # the app's status bar shows the attach chip on this

    def __init__(self, socket_path: str, cursor: int | None = None) -> None:
        self.socket_path = str(socket_path)
        self.cursor = cursor  # next seq we have NOT seen; None = replay all
        self.session_id: str | None = None
        self.model: str | None = None
        self.cwd: str | None = None
        self.total_cost_usd = 0.0
        self.last_ctx_percentage: float | None = None
        # Identity surface, cached from status replies -- same engine-parity
        # attributes SessionEngine carries (account fields the CLI actually
        # reported at connect; {} / None until the first status refresh).
        self.account: dict = {}
        self.lore_root: str | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task | None = None
        self._req_ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._turn_queue: asyncio.Queue[EngineEvent] = asyncio.Queue()
        self._oob_queue: asyncio.Queue[EngineEvent | None] = asyncio.Queue()
        self._active_turn: str | None = None
        self._peers: list[PeerInfo] = []
        self._belief_count = 0
        self._disabled: list[str] = []
        self._usage: dict = {}
        self._closed = False

    # -- lifecycle ---------------------------------------------------

    async def start(self) -> EngineEvent:
        """Connect, verify the version-stamped hello, attach (which replays
        the ring from our cursor), and seed the status cache."""
        try:
            self._reader, self._writer = await asyncio.open_unix_connection(
                self.socket_path, limit=MAX_FRAME_BYTES
            )
            raw = await asyncio.wait_for(
                self._reader.readline(), HELLO_TIMEOUT_SECS
            )
            hello = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise EngineClientError(f"cannot attach to daemon: {exc}") from exc
        if hello.get("type") != "hello":
            raise EngineClientError(f"unexpected first frame: {hello.get('type')!r}")
        if hello.get("proto") != PROTOCOL_VERSION:
            raise EngineClientError(
                f"protocol mismatch: daemon speaks v{hello.get('proto')} "
                f"(doxa {hello.get('doxa')}), this client speaks "
                f"v{PROTOCOL_VERSION} -- run `doxa stop` and start fresh"
            )
        self.session_id = hello.get("session_id")
        self.model = hello.get("model")
        self.cwd = hello.get("cwd")
        self._write_frame({"type": "attach", "cursor": self.cursor})
        self._reader_task = asyncio.create_task(self._read_loop())
        with contextlib.suppress(Exception):
            await self.refresh_status()
        return EngineEvent("session_started", {
            "session_id": self.session_id, "model": self.model,
            "cwd": self.cwd, "attached": True,
        })

    async def finalize(self) -> EngineEvent:
        """DETACH -- see module docstring. Idempotent; the daemon lingers
        and finalizes (or another client keeps it alive)."""
        self._close()
        return EngineEvent("session_done", {"detached": True})

    async def stop(self) -> EngineEvent:
        """Explicit finalize-now: the daemon runs the LORE review + index
        and exits; every attached client sees the socket close."""
        try:
            reply = await self._call("stop")
        except EngineClientError:
            reply = {}  # daemon already gone: stopped is what we wanted
        self._close()
        return EngineEvent("session_done", {"stopped": True, **(
            {} if reply.get("ok") else {"note": "daemon did not confirm"}
        )})

    def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._reader_task is not None:
            self._reader_task.cancel()
        if self._writer is not None:
            with contextlib.suppress(Exception):
                self._writer.close()
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(EngineClientError("connection closed"))
        self._pending.clear()
        self._oob_queue.put_nowait(None)  # end the peer_events iterator

    # -- wire --------------------------------------------------------

    def _write_frame(self, frame: dict) -> None:
        if self._writer is None or self._closed:
            raise EngineClientError("not connected")
        self._writer.write((json.dumps(frame) + "\n").encode("utf-8"))

    async def _call(self, method: str, **params: Any) -> dict:
        req_id = next(self._req_ids)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        self._write_frame(
            {"type": "call", "id": req_id, "method": method, "params": params}
        )
        try:
            return await asyncio.wait_for(fut, CALL_TIMEOUT_SECS)
        except asyncio.TimeoutError as exc:
            self._pending.pop(req_id, None)
            raise EngineClientError(f"daemon call {method!r} timed out") from exc

    async def _read_loop(self) -> None:
        assert self._reader is not None
        try:
            while True:
                try:
                    line = await self._reader.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    break
                if not line:
                    break
                try:
                    frame = json.loads(line.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue
                if not isinstance(frame, dict):
                    continue
                if frame.get("type") == "reply":
                    self._handle_reply(frame)
                elif frame.get("type") == "event":
                    self._handle_event(frame)
        except asyncio.CancelledError:
            return
        finally:
            if not self._closed:
                # Daemon went away underneath us (stop from another client,
                # crash): unblock a waiting send() with an error turn_done,
                # then close out the same way a local detach would.
                if self._active_turn is not None:
                    self._active_turn = None
                    self._turn_queue.put_nowait(EngineEvent("turn_done", {
                        "is_error": True, "error": "connection to daemon lost",
                    }))
                self._close()

    def _handle_reply(self, frame: dict) -> None:
        if frame.get("ok") and frame.get("turn"):
            # Set by the READER, before any of the turn's tagged events can
            # be routed -- same-socket ordering makes this race-free.
            self._active_turn = frame["turn"]
        fut = self._pending.pop(frame.get("id"), None)
        if fut is not None and not fut.done():
            fut.set_result(frame)

    def _handle_event(self, frame: dict) -> None:
        seq = frame.get("seq")
        if isinstance(seq, int):
            self.cursor = seq + 1
        payload = frame.get("event") or {}
        ev = EngineEvent(str(payload.get("type")), dict(payload.get("data") or {}))
        # Cache maintenance, regardless of who initiated the event.
        if ev.type == "turn_done":
            if ev.data.get("session_cost_usd") is not None:
                self.total_cost_usd = ev.data["session_cost_usd"]
            if ev.data.get("ctx_percentage") is not None:
                self.last_ctx_percentage = ev.data["ctx_percentage"]
        elif ev.type == "tool_disabled":
            name = ev.data.get("name")
            if name and name not in self._disabled:
                self._disabled.append(name)
        elif ev.type == "model_changed":
            # Another client (or this one) switched the session's model:
            # the cached value follows immediately, not at the next status.
            if ev.data.get("model"):
                self.model = str(ev.data["model"])
        elif ev.type in ("peer_joined", "peer_left"):
            asyncio.ensure_future(self._refresh_status_quietly())
        if frame.get("turn") and frame["turn"] == self._active_turn:
            if ev.type == "turn_done":
                self._active_turn = None
            self._turn_queue.put_nowait(ev)
        else:
            self._oob_queue.put_nowait(ev)

    # -- engine-parity surface ----------------------------------------

    async def send(self, prompt: str) -> AsyncIterator[EngineEvent]:
        reply = await self._prompt(prompt)
        if not reply.get("ok"):
            raise EngineClientError(reply.get("error") or "prompt refused")
        while True:
            ev = await self._turn_queue.get()
            yield ev
            if ev.type == "turn_done":
                break
        await self._refresh_status_quietly()

    async def _prompt(self, text: str) -> dict:
        req_id = next(self._req_ids)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        self._write_frame({"type": "prompt", "id": req_id, "text": text})
        try:
            return await asyncio.wait_for(fut, CALL_TIMEOUT_SECS)
        except asyncio.TimeoutError as exc:
            self._pending.pop(req_id, None)
            raise EngineClientError("daemon prompt ack timed out") from exc

    async def peer_events(self) -> AsyncIterator[EngineEvent]:
        while True:
            item = await self._oob_queue.get()
            if item is None:
                return
            yield item

    async def set_model(self, model: "str | None") -> str:
        """/model over the socket. The daemon does the control request; a
        refusal comes back as an error the caller shows verbatim."""
        reply = await self._call("set_model", model=model)
        if not reply.get("ok"):
            raise EngineClientError(reply.get("error") or "model switch refused")
        self.model = reply.get("model") or model
        return str(self.model)

    def usage_summary(self) -> dict:
        """Engine-parity surface for /usage -- the daemon's own numbers,
        cached from the last status reply (same read-it-synchronously
        contract as belief_count and the cost figure)."""
        return dict(self._usage)

    async def refresh_status(self) -> dict:
        reply = await self._call("status")
        status = reply.get("status") or {}
        if status.get("model"):
            self.model = status["model"]
        if isinstance(status.get("account"), dict):
            self.account = status["account"]
        if status.get("lore_root"):
            self.lore_root = str(status["lore_root"])
        if status.get("total_cost_usd") is not None:
            self.total_cost_usd = status["total_cost_usd"]
        self.last_ctx_percentage = status.get("ctx_percentage")
        self._belief_count = int(status.get("belief_count") or 0)
        if isinstance(status.get("usage"), dict):
            self._usage = status["usage"]
        self._disabled = list(status.get("disabled_tools") or [])
        self._peers = [
            PeerInfo(**p) for p in status.get("peers") or []
            if isinstance(p, dict)
        ]
        return status

    async def _refresh_status_quietly(self) -> None:
        with contextlib.suppress(Exception):
            await self.refresh_status()

    def list_peers(self) -> list[PeerInfo]:
        return list(self._peers)

    def peer_count(self) -> int:
        return len(self._peers)

    async def send_peer_message(self, target_prefix: str, text: str) -> PeerInfo:
        reply = await self._call("msg", target=target_prefix, text=text)
        if not reply.get("ok"):
            raise PeerSendError(reply.get("error") or "send failed")
        return PeerInfo(**reply["peer"])

    def belief_count(self) -> int:
        return self._belief_count

    def disabled_tools(self) -> list[str]:
        return list(self._disabled)


async def attach(socket_path: str, cursor: int | None = None) -> EngineClient:
    """Connect-and-start convenience for the CLI and the attach picker."""
    client = EngineClient(socket_path, cursor=cursor)
    await client.start()
    return client

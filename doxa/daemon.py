"""doxa.daemon -- the engine host process behind a Unix socket.

The Phase 2 daemon split: the ``SessionEngine`` (and with it the PeerHost,
the transcript, the LORE hooks -- everything stateful) moves OUT of the TUI
process into this daemon, and the Textual app becomes a thin client
(doxa/client.py) speaking line-JSON over the daemon's socket. Sessions then
detach and reattach without tmux: closing the TUI leaves the daemon (and the
conversation) running; ``doxa attach`` picks it back up.

Socket idioms are doxa.peers' own, reused deliberately: sockets live in
``peers.runtime_dir()`` (DOXA_RUNTIME_DIR-overridable), are chmod 0600, and
speak one JSON object per line with a hard :data:`peers.MAX_FRAME_BYTES` cap
per frame. Discovery reuses the peer registry too -- the daemon's presence
IS the session's peer presence (PeerHost is owned by the engine, which this
process hosts), extended with the ``daemon_socket`` marker field
(peers.PeerInfo.daemon_socket) so ``doxa attach`` finds sessions through the
one registry that already exists, not a second one.

Protocol (all frames are single JSON lines, <= MAX_FRAME_BYTES):

server -> client
  {"type": "hello", "proto": 1, "doxa": <version>, "session_id", "model",
   "cwd", "next_seq"}                       -- version-stamped, sent on connect
  {"type": "event", "seq": N, "turn": <id|null>,
   "event": {"type": ..., "data": {...}}}   -- one EngineEvent, live or replayed
  {"type": "reply", "id": N, "ok": bool, ...}  -- response to prompt/call

client -> server
  {"type": "attach", "cursor": N | null}    -- replay ring from cursor (null =
                                               everything buffered), then live
  {"type": "prompt", "id": N, "text": ...}  -- run one turn; its events arrive
                                               on the event stream tagged with
                                               the reply's "turn" id
  {"type": "call", "id": N, "method": "status"|"peers"|"msg"|"stop"|
   "set_model"|"answer_needs_input", "params": {...}}

Interactive permission (queue item 5): a pending ``AskUserQuestion`` or
permission request (``doxa.engine.SessionEngine._on_can_use_tool``) rides
the SAME out-of-band ``needs_input``/``needs_input_resolved`` events every
other peer-layer signal does -- queued through ``engine.peer_events()``,
fanned out by ``_peer_pump`` below like ``tool_disabled`` already is, and
landing in the ring like anything else :meth:`_publish` touches. That
ring is therefore ALSO the parking mechanism for a fully detached session
(no client attached at all when the question is asked): nothing special
has to happen for the question to survive until someone attaches --
``EventRing.since()`` replays it like any other buffered frame -- but
nobody is here to see the tab blink or hear a beep either, so
:meth:`_peer_pump` fires the desktop notification itself (focus is moot
with zero clients -- always the "unfocused" gate) in exactly that one
case, leaving the attached-client case to the TUI's own real
``app_has_focus``-gated call, the same division of labor ``notify_lore``
already has between lore_core and doxa.notify. The client answers with
``{"type": "call", "method": "answer_needs_input", "params": {"id", "answer"}}``;
the engine's own resolution fires ``needs_input_resolved`` back out on the
SAME out-of-band stream (see ``SessionEngine._wait_for_answer``), so every
attached client -- not just whichever one answered -- drops its own copy
of the dialog, same as ``model_changed`` already keeps every tab's cached
model in sync after one of them calls ``set_model``.

Replay ring: every published event gets a monotonically increasing ``seq``
and lands in a bounded ring (the ask_buffers idea reused: seq-numbered ring,
replay-from-cursor, reuse-don't-copy -- stored frames are replayed verbatim).
A reattaching client sends the cursor it last saw; the daemon replays
``seq >= cursor`` from the ring, then the live tail follows on the same
stream. The ring is in-memory only -- nothing here persists; every string
that persists anywhere still goes through the engine's scrub choke point.

Lifecycle: the daemon finalizes the session (LORE review + index, via
``SessionEngine.finalize``) when the LAST client detaches AND ``linger_secs``
passes with nobody reattaching, or immediately on an explicit stop call
(``doxa stop`` / the palette's quit-stop). SIGTERM finalizes too -- the
review gate should never be skipped just because systemd or the user got
impatient.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import signal
import sys
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Callable

from . import __version__
from . import notify as notify_mod
from . import worktrees as worktrees_mod
from .engine import EngineEvent, SessionEngine
from .peers import MAX_FRAME_BYTES, registry_dir, runtime_dir

PROTOCOL_VERSION = 1
DEFAULT_LINGER_SECS = 120.0
# A freshly spawned daemon that NO client has attached to yet gets this
# claim window (>= spawn_daemon's own wait) before giving up, regardless of
# how short --linger is -- the linger knob times detach-to-finalize, not
# spawn-to-first-attach.
INITIAL_CLAIM_SECS = 120.0
RING_CAPACITY = 512

# Turn-event kinds, as doxa.engine.EngineEvent documents them. Everything
# else (peer_*, tool_disabled) is out-of-band and travels with turn=None.
TURN_EVENT_TYPES = frozenset(
    {"turn_started", "text_delta", "tool_call", "tool_result", "turn_done"}
)


class EventRing:
    """Bounded, seq-numbered replay ring. append() stamps the next seq and
    stores the complete wire frame; since(cursor) hands the stored frames
    back for replay -- reuse, not copy. Old frames fall off the far end;
    a cursor older than the ring simply gets everything still buffered."""

    def __init__(self, capacity: int = RING_CAPACITY) -> None:
        self._frames: deque[dict] = deque(maxlen=capacity)
        self._next_seq = 0

    @property
    def next_seq(self) -> int:
        return self._next_seq

    def append(self, turn_id: str | None, event: EngineEvent) -> dict:
        frame = {
            "type": "event",
            "seq": self._next_seq,
            "turn": turn_id,
            "event": {"type": event.type, "data": event.data},
        }
        self._next_seq += 1
        self._frames.append(frame)
        return frame

    def since(self, cursor: int | None) -> list[dict]:
        if cursor is None:
            return list(self._frames)
        return [f for f in self._frames if f["seq"] >= cursor]


def encode_frame(frame: dict) -> bytes:
    """One frame as a wire line, enforcing the peers-style 64KB cap. An
    oversize event frame degrades to a marker carrying the event type --
    the client stays in sync (seq intact) and the model-side data is never
    silently split across frames."""
    payload = (json.dumps(frame, ensure_ascii=False) + "\n").encode("utf-8")
    if len(payload) <= MAX_FRAME_BYTES:
        return payload
    slim = dict(frame)
    if slim.get("type") == "event":
        slim["event"] = {
            "type": slim["event"]["type"],
            "data": {"truncated": True,
                     "note": "event exceeded the frame cap; see the transcript"},
        }
    else:
        slim = {"type": slim.get("type"), "id": slim.get("id"),
                "ok": False, "error": "reply exceeded the frame cap"}
    return (json.dumps(slim, ensure_ascii=False) + "\n").encode("utf-8")


def daemon_socket_path(session_id: str) -> Path:
    """Same AF_UNIX path-length discipline as peers.PeerHost: session-id
    prefix + pid, and readers never derive it -- they read it verbatim from
    the registry entry's daemon_socket field."""
    return runtime_dir() / f"daemon-{session_id[:8]}-{os.getpid()}.sock"


class SessionDaemon:
    """One detachable session: hosts the SessionEngine, serves the socket.

    ``engine_factory(cwd, session_id, daemon_socket)`` builds the engine --
    injectable so the test suite runs the whole daemon over a fake SDK
    client. The default builds a real SessionEngine.
    """

    def __init__(
        self,
        cwd: str | None = None,
        model: str | None = None,
        session_id: str | None = None,
        linger_secs: float = DEFAULT_LINGER_SECS,
        engine_factory: Callable[[str, str, str], SessionEngine] | None = None,
        ring_capacity: int = RING_CAPACITY,
    ) -> None:
        self.cwd = str(cwd or os.getcwd())
        self.model = model
        self.session_id = session_id or str(uuid.uuid4())
        self.linger_secs = linger_secs
        self.socket_path = daemon_socket_path(self.session_id)
        self._engine_factory = engine_factory or (
            lambda cwd, sid, dsock: SessionEngine(
                cwd=cwd, model=self.model, session_id=sid, daemon_socket=dsock,
            )
        )
        self.engine: SessionEngine | None = None
        self.ring = EventRing(ring_capacity)
        self.ready = asyncio.Event()
        self._done = asyncio.Event()
        self._server: asyncio.AbstractServer | None = None
        self._clients: set[asyncio.StreamWriter] = set()
        self._had_client = False
        self._turn_task: asyncio.Task | None = None
        self._linger_task: asyncio.Task | None = None
        self._pump_task: asyncio.Task | None = None
        self._stopping = False
        # Worktree-per-session (#3, doxa.worktrees): computed once, before
        # the engine is built, so the engine (and everything downstream --
        # the "hello" frame, EngineClient.cwd, SessionPane's GitLine) just
        # sees a cwd that happens to be a worktree. Cached so a headless
        # shutdown (linger/signal) and an explicit "stop" reply (which
        # needs the message inline, see _handle_call) never run the git
        # cleanup twice.
        self._worktree_note: "str | None" = None
        self._worktree_done = False

    # -- lifecycle ---------------------------------------------------

    def _apply_worktree(self) -> None:
        """Substitute ``self.cwd`` for its own worktree BEFORE the engine
        is built -- a no-op (returns None, leaves cwd alone) when the
        setting is off, ``cwd`` is not a git repo, or worktree creation
        fails for any reason: worktree-per-session is strictly additive,
        never a reason a session fails to start."""
        path = worktrees_mod.create(self.cwd, self.session_id)
        if path:
            self.cwd = path

    def _finalize_worktree(self) -> "str | None":
        """Worktree cleanup at REAL finalize (never at a mere detach --
        see doxa.worktrees.finalize's docstring). Runs at most once;
        later callers (a headless _shutdown after an RPC-driven one, or
        vice versa) get the cached result. A "kept" message always also
        goes to the daemon's own log -- the one channel guaranteed to
        exist even when finalize runs with no client attached."""
        if self._worktree_done:
            return self._worktree_note
        self._worktree_done = True
        try:
            note = worktrees_mod.finalize(self.cwd)
        except Exception:  # noqa: BLE001 -- cleanup bookkeeping must never
            note = None    # block a shutdown that is already underway
        self._worktree_note = note
        if note:
            print(f"doxa: {note}", file=sys.stderr)
        return note

    async def serve(self) -> None:
        """Start the engine, serve the socket, run until finalized (linger
        expiry, explicit stop, or SIGTERM)."""
        registry_dir()  # ensure runtime dirs exist with clamped perms
        with contextlib.suppress(OSError):
            self.socket_path.unlink()
        self._apply_worktree()
        self.engine = self._engine_factory(
            self.cwd, self.session_id, str(self.socket_path)
        )
        await self.engine.start()
        if self.engine.peer_host is None:
            # The registry entry IS this daemon's discoverability -- without
            # it `doxa attach` can never find the session, so a presence
            # failure is fatal here (unlike the strictly-additive in-process
            # case). The cause travels in the exception.
            await self.engine.finalize()
            raise RuntimeError(
                f"daemon presence entry failed: {self.engine.peer_error}"
            )
        self._server = await asyncio.start_unix_server(
            self._handle_client, path=str(self.socket_path),
            limit=MAX_FRAME_BYTES,
        )
        os.chmod(self.socket_path, 0o600)
        self._pump_task = asyncio.create_task(self._peer_pump())
        self._sync_client_count()  # 0 until someone attaches: detached, honestly
        self._arm_linger()  # nobody attached yet: don't run forever unclaimed
        self.ready.set()
        try:
            await self._done.wait()
        finally:
            await self._teardown()

    async def _teardown(self) -> None:
        for task in (self._pump_task, self._linger_task, self._turn_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None
        for writer in list(self._clients):
            self._drop_client(writer)
        with contextlib.suppress(OSError):
            self.socket_path.unlink()

    async def _shutdown(self, reason: str) -> None:
        """Finalize exactly once (LORE review + index run inside the
        engine's own finalize, worktree remove-or-keep run inside
        _finalize_worktree), then let serve() unwind."""
        if self._stopping:
            return
        self._stopping = True
        if self._turn_task is not None and not self._turn_task.done():
            self._turn_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._turn_task
        if self.engine is not None:
            with contextlib.suppress(Exception):
                await self.engine.finalize()
        self._finalize_worktree()  # cached: a no-op if the stop RPC below
        # already ran it to embed the "kept" note in its reply.
        self._done.set()

    # -- linger ------------------------------------------------------

    def _arm_linger(self) -> None:
        if self._stopping or self._linger_task is not None:
            return
        # Before the first client has EVER attached, wait the (generous)
        # claim window; after a detach, wait exactly the linger knob.
        delay = (
            self.linger_secs if self._had_client
            else max(self.linger_secs, INITIAL_CLAIM_SECS)
        )
        self._linger_task = asyncio.create_task(self._linger_then_stop(delay))

    def _cancel_linger(self) -> None:
        if self._linger_task is not None:
            self._linger_task.cancel()
            self._linger_task = None

    async def _linger_then_stop(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        if not self._clients:
            await self._shutdown("linger expired with no client attached")

    # -- event fan-out -----------------------------------------------

    async def _peer_pump(self) -> None:
        assert self.engine is not None
        async for ev in self.engine.peer_events():
            if ev.type == "needs_input" and not self._clients:
                # Parked with nobody attached at all: the ring below still
                # carries it for a later attach to replay, but a fully
                # detached session has no window to blink and no operator
                # watching it -- the desktop notification is the one signal
                # available, so it fires here rather than waiting on a TUI
                # that may not exist for hours. Always the "unfocused" gate
                # -- there is no focus concept with zero clients.
                notify_mod.notify_needs_input(
                    False, self._notify_label(), self._needs_input_summary(ev.data),
                )
            self._publish(None, ev)

    def _notify_label(self) -> str:
        return Path(self.cwd).name or self.session_id[:8]

    @staticmethod
    def _needs_input_summary(data: dict) -> str:
        if data.get("kind") == "ask_user":
            questions = data.get("questions") or []
            if questions and isinstance(questions[0], dict):
                return str(questions[0].get("question") or "question")
            return "question"
        return str(data.get("input_summary") or data.get("tool_name") or "")

    def _publish(self, turn_id: str | None, event: EngineEvent) -> None:
        frame = self.ring.append(turn_id, event)
        payload = encode_frame(frame)
        for writer in list(self._clients):
            try:
                writer.write(payload)
            except Exception:
                self._drop_client(writer)

    def _sync_client_count(self) -> None:
        """Keep the presence entry's attached-client count honest -- it is
        what tells every other session whether this one is detached."""
        host = getattr(self.engine, "peer_host", None)
        if host is not None:
            with contextlib.suppress(Exception):
                host.set_client_count(len(self._clients))

    def _drop_client(self, writer: asyncio.StreamWriter) -> None:
        self._clients.discard(writer)
        self._sync_client_count()
        with contextlib.suppress(Exception):
            writer.close()
        if not self._clients and self._had_client and not self._stopping:
            self._arm_linger()

    # -- client protocol ---------------------------------------------

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        assert self.engine is not None
        writer.write(encode_frame({
            "type": "hello",
            "proto": PROTOCOL_VERSION,
            "doxa": __version__,
            "session_id": self.session_id,
            "model": self.engine.model,
            "cwd": self.cwd,
            "next_seq": self.ring.next_seq,
        }))
        try:
            await writer.drain()
            while True:
                try:
                    line = await reader.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    break  # oversize frame: drop the client, not the daemon
                if not line:
                    break
                try:
                    frame = json.loads(line.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue
                if not isinstance(frame, dict):
                    continue
                await self._handle_frame(frame, writer)
                if self._stopping:
                    break
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            self._drop_client(writer)

    async def _handle_frame(self, frame: dict, writer: asyncio.StreamWriter) -> None:
        ftype = frame.get("type")
        if ftype == "attach":
            cursor = frame.get("cursor")
            cursor = int(cursor) if isinstance(cursor, (int, float)) else None
            # No awaits between computing the replay set, joining the live
            # broadcast set, and buffering the replay writes: that ordering
            # (with the loop unable to interleave _publish in between) is
            # what guarantees replay-then-tail with no gap and no overlap.
            replay = self.ring.since(cursor)
            self._clients.add(writer)
            self._had_client = True
            self._sync_client_count()
            self._cancel_linger()
            for f in replay:
                writer.write(encode_frame(f))
            await writer.drain()
        elif ftype == "prompt":
            await self._handle_prompt(frame, writer)
        elif ftype == "call":
            await self._handle_call(frame, writer)

    async def _handle_prompt(self, frame: dict, writer: asyncio.StreamWriter) -> None:
        req_id = frame.get("id")
        text = str(frame.get("text") or "")
        if not text.strip():
            await self._reply(writer, req_id, ok=False, error="empty prompt")
            return
        if self._turn_task is not None and not self._turn_task.done():
            await self._reply(
                writer, req_id, ok=False,
                error="a turn is already running in this session",
            )
            return
        turn_id = uuid.uuid4().hex[:12]
        # Claim the turn slot and BUFFER the reply bytes synchronously (no
        # await between the busy-check above and here): a racing prompt from
        # another client cannot slip past the busy-check, and the reply hits
        # the socket before the turn task -- which first runs on the next
        # loop tick -- can publish any tagged event. The client therefore
        # always learns its turn id ahead of the first event that carries it.
        self._turn_task = asyncio.create_task(self._run_turn(turn_id, text))
        await self._reply(writer, req_id, ok=True, turn=turn_id)

    async def _run_turn(self, turn_id: str, text: str) -> None:
        assert self.engine is not None
        try:
            async for ev in self.engine.send(text):
                self._publish(turn_id, ev)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 -- a turn failure must reach the client
            self._publish(turn_id, EngineEvent("turn_done", {
                "is_error": True,
                "error": f"{type(exc).__name__}: {exc}",
                "session_cost_usd": self.engine.total_cost_usd,
            }))

    async def _handle_call(self, frame: dict, writer: asyncio.StreamWriter) -> None:
        assert self.engine is not None
        req_id = frame.get("id")
        method = frame.get("method")
        params = frame.get("params") or {}
        if method == "status":
            await self._reply(writer, req_id, ok=True, status=self._status())
        elif method == "peers":
            await self._reply(
                writer, req_id, ok=True,
                peers=[vars(p) for p in self.engine.list_peers()],
            )
        elif method == "msg":
            from .peers import PeerSendError
            try:
                peer = await self.engine.send_peer_message(
                    str(params.get("target") or ""), str(params.get("text") or ""),
                )
                await self._reply(writer, req_id, ok=True, peer=vars(peer))
            except PeerSendError as exc:
                await self._reply(writer, req_id, ok=False, error=str(exc))
        elif method == "set_model":
            # /model over the daemon split: a control request to the SDK
            # client the daemon already owns -- no reconnect, so the
            # transcript, this ring and every attached client survive it.
            try:
                model = await self.engine.set_model(params.get("model") or None)
            except Exception as exc:  # noqa: BLE001 -- the client shows it
                await self._reply(writer, req_id, ok=False,
                                  error=f"{type(exc).__name__}: {exc}")
                return
            # Every attached client learns the new model, not just the one
            # that asked -- two tabs on one daemon must not disagree.
            self._publish(None, EngineEvent("model_changed", {"model": model}))
            await self._reply(writer, req_id, ok=True, model=model)
        elif method == "answer_needs_input":
            # The resolution's own needs_input_resolved broadcast comes
            # from the ENGINE side (SessionEngine._wait_for_answer's
            # finally), over the same peer_events stream _peer_pump
            # already fans out -- nothing extra to publish here.
            ok = await self.engine.answer_needs_input(
                str(params.get("id") or ""), dict(params.get("answer") or {}),
            )
            await self._reply(writer, req_id, ok=ok)
        elif method == "stop":
            # Worktree cleanup runs BEFORE the ack (fast, git-only) so a
            # "kept" note can ride in the SAME reply -- unlike
            # engine.finalize()'s LORE review below, which stays
            # ack-first/finalize-after so a slow review can never make a
            # `doxa stop` / quit-stop call itself time out.
            note = self._finalize_worktree()
            await self._reply(writer, req_id, ok=True, stopping=True, note=note)
            await self._shutdown("explicit stop")
        else:
            await self._reply(writer, req_id, ok=False,
                              error=f"unknown method: {method!r}")

    def _status(self) -> dict:
        assert self.engine is not None
        return {
            "session_id": self.session_id,
            "model": self.engine.model,
            "cwd": self.cwd,
            # Identity surface for the client's status cache: the account
            # block the CLI reported at connect (may be {}), and where the
            # LORE store lives daemon-side.
            "account": getattr(self.engine, "account", None) or {},
            "lore_root": getattr(self.engine, "lore_root", None),
            "total_cost_usd": self.engine.total_cost_usd,
            "ctx_percentage": self.engine.last_ctx_percentage,
            # /usage over the split: the engine's own token accounting,
            # cached client-side like every other status value.
            "usage": self.engine.usage_summary(),
            "belief_count": self.engine.belief_count(),
            "disabled_tools": self.engine.disabled_tools(),
            "peers": [vars(p) for p in self.engine.list_peers()],
            "clients": len(self._clients),
        }

    async def _reply(
        self, writer: asyncio.StreamWriter, req_id: Any, ok: bool, **extra: Any
    ) -> None:
        frame = {"type": "reply", "id": req_id, "ok": ok, **extra}
        try:
            writer.write(encode_frame(frame))
            await writer.drain()
        except Exception:
            self._drop_client(writer)


def spawn_daemon(
    cwd: str,
    model: str | None = None,
    linger_secs: float = DEFAULT_LINGER_SECS,
    wait_secs: float = 60.0,
) -> "tuple[str, str]":
    """Spawn a detached daemon for ``cwd`` and wait for its registry entry.

    Returns (session_id, daemon_socket). The session id is minted HERE so
    the spawner can poll the one registry surface for exactly its own entry
    -- no scanning race with concurrently spawned sessions. Daemon stdout/
    stderr go to a per-session log under the runtime dir (diagnostics only;
    the engine never prints transcript text)."""
    import subprocess
    import time as _time

    session_id = str(uuid.uuid4())
    reg = registry_dir()
    log_path = runtime_dir() / f"daemon-{session_id[:8]}.log"
    cmd = [
        sys.executable, "-m", "doxa.daemon",
        "--cwd", cwd, "--session-id", session_id,
        "--linger", str(linger_secs),
    ]
    if model:
        cmd += ["--model", model]
    with open(log_path, "ab") as log:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            start_new_session=True, cwd=cwd,
        )
    entry_path = reg / f"{session_id}.json"
    deadline = _time.monotonic() + wait_secs
    while _time.monotonic() < deadline:
        if proc.poll() is not None:
            tail = ""
            with contextlib.suppress(OSError):
                tail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
            raise RuntimeError(
                f"doxa daemon exited during startup (code {proc.returncode}). "
                f"Log tail:\n{tail}"
            )
        if entry_path.exists():
            with contextlib.suppress(OSError, ValueError):
                data = json.loads(entry_path.read_text(encoding="utf-8"))
                dsock = data.get("daemon_socket")
                if dsock and Path(dsock).exists():
                    return session_id, str(dsock)
        _time.sleep(0.1)
    raise RuntimeError(f"doxa daemon did not become ready within {wait_secs:.0f}s")


def install_signal_handlers(
    daemon: SessionDaemon, loop: "asyncio.AbstractEventLoop | None" = None
) -> None:
    """SIGTERM and SIGINT both mean the same thing to a session daemon:
    finalize gracefully NOW (LORE review + index via engine.finalize), then
    exit -- an impatient user's Ctrl+C aimed at the daemon must never skip
    the review gate any more than systemd's SIGTERM does. Split out of
    _amain so the graceful-signal contract is testable in-process."""
    loop = loop or asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(
            sig, lambda: asyncio.ensure_future(daemon._shutdown("signal"))
        )


async def _amain(args: argparse.Namespace) -> int:
    daemon = SessionDaemon(
        cwd=args.cwd,
        model=args.model,
        session_id=args.session_id,
        linger_secs=args.linger,
    )
    install_signal_handlers(daemon)
    await daemon.serve()
    return 0


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(prog="doxa-daemon")
    parser.add_argument("--cwd", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--session-id", default=None)
    parser.add_argument("--linger", type=float, default=DEFAULT_LINGER_SECS)
    args = parser.parse_args(argv)
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())

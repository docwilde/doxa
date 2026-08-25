"""doxa.client -- the thin-client side of the daemon split.

:class:`EngineClient` presents the SAME surface doxa.app consumes from
``SessionEngine`` (start / send / peer_events / list_peers / peer_count /
send_peer_message / belief_count / list_beliefs / list_pending /
disabled_tools / finalize, plus the model/total_cost_usd/last_ctx_percentage attributes) --
the app swaps one handle for the other behind a factory and barely notices.
Underneath, every call is a line-JSON frame over the daemon's Unix socket
(protocol sketch in doxa/daemon.py's docstring).

Semantics that differ from the in-process engine, deliberately:

* ``finalize()`` DETACHES: it closes the socket and leaves the daemon (and
  the session) running -- the daemon finalizes on its own once the last
  client has been gone for the linger window. ``stop()`` is the explicit
  "finalize NOW" path (`doxa stop`, the palette's quit-stop).
* Events the client did not initiate -- replayed history after a reattach,
  turns driven by another attached client, peer traffic, two-strikes
  disables, a pending AskUserQuestion/permission request (queue item 5's
  ``needs_input``, and ``needs_input_resolved`` once ANY attached client
  answers it via :meth:`answer_needs_input`) -- all arrive on
  :meth:`peer_events`, the same out-of-band stream the app already pumps.
  Turn events are told apart from the client's own live turn by the
  ``turn`` tag the daemon stamps on every event frame.
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
from .engine import (
    BELIEF_EVIDENCE_LIMIT,
    BELIEF_LIST_LIMIT,
    PENDING_LIST_LIMIT,
    EngineEvent,
)
from .peers import MAX_FRAME_BYTES, PeerInfo, PeerSendError

CALL_TIMEOUT_SECS = 15.0
HELLO_TIMEOUT_SECS = 10.0


class EngineClientError(RuntimeError):
    """Connection/protocol failure between the TUI and the daemon."""


class EngineClient:
    """One attached client of a :class:`doxa.daemon.SessionDaemon`."""

    detachable = True  # the app's status bar shows the attach chip on this

    def __init__(
        self,
        socket_path: str,
        cursor: int | None = None,
        *,
        skip_backlog: bool = False,
    ) -> None:
        self.socket_path = str(socket_path)
        self.cursor = cursor  # next seq we have NOT seen; None = replay all
        # v0.32.0, tab restore: attach at the ring's CURRENT head instead of
        # replaying it, because this client's pane is about to render the
        # same conversation from the session's persisted transcript
        # (doxa.transcript) -- which is complete, where the 512-frame ring
        # is only ever a tail. Replaying both would double every turn the
        # ring still happens to hold. The head seq comes from the daemon's
        # own hello ("next_seq"), read in start() before attach is sent; a
        # daemon too old to send one leaves this a no-op and the pane falls
        # back to the v0.31.0 replay-only behavior rather than duplicating.
        self.skip_backlog = bool(skip_backlog)
        self.backlog_skipped: "int | None" = None
        self.session_id: str | None = None
        self.model: str | None = None
        # Permission mode (v0.42.0), engine parity: SessionEngine carries
        # the same attribute name, so the status chip reads whichever
        # object this pane has without knowing which side of the socket it
        # is on. Seeded to the safe default rather than None because the
        # chip can paint before the first reply lands -- and if this client
        # is ever wrong for one frame, "default" is the answer that
        # UNDERSTATES the session's freedom rather than overstating it, so
        # the transient error is "you were not told about a gated mode
        # yet", never "you were told there is none". The hello frame
        # corrects it immediately; see SessionDaemon._hello.
        self.permission_mode: str = "default"
        # Engine parity (v0.58.0): whether the DAEMON's CLI was spawned
        # able to reach bypassPermissions. False until the hello frame
        # says otherwise, which is the safe direction to be wrong in for
        # one frame -- the narrower cycle, never the wider one.
        self.bypass_armed: bool = False
        self.cwd: str | None = None
        self.total_cost_usd = 0.0
        self.last_ctx_percentage: float | None = None
        # Item X (ctx absolute): engine parity for the absolute halves of
        # the same measurement -- SessionEngine carries these under the
        # same names, and the status bar reads whichever object it has
        # without knowing which side of the socket it is on.
        self.last_ctx_tokens: int | None = None
        self.last_ctx_max_tokens: int | None = None
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
        if hello.get("permission_mode"):
            self.permission_mode = str(hello["permission_mode"])
        self.bypass_armed = bool(hello.get("bypass_armed"))
        self.cwd = hello.get("cwd")
        if self.skip_backlog:
            # The daemon has advertised its ring head since the protocol's
            # first version; treat a missing/odd value as "cannot skip"
            # and replay as before -- a duplicated transcript is a worse
            # failure than an un-skipped one, and the pane checks
            # backlog_skipped before deciding to render from disk.
            head = hello.get("next_seq")
            if isinstance(head, int) and head >= 0:
                self.cursor = head
                self.backlog_skipped = head
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
        and exits; every attached client sees the socket close.

        ``note``, when present, is worktree-per-session's (#3) closing
        word: `kept doxa/<id> — merge when ready` when the daemon kept a
        dirty or unmerged worktree rather than removing it. The daemon
        computes and embeds it in the "stop" reply itself (fast, git-only,
        ahead of the potentially-slow LORE review) so it survives even
        though this method closes the socket right after."""
        try:
            reply = await self._call("stop")
        except EngineClientError:
            reply = {}  # daemon already gone: stopped is what we wanted
        self._close()
        data: dict[str, Any] = {"stopped": True}
        if not reply.get("ok"):
            data["note"] = "daemon did not confirm"
        elif reply.get("note"):
            data["note"] = reply["note"]
        return EngineEvent("session_done", data)

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
            # Item X: absolute halves of the same reading, cached the same
            # way. Guarded individually rather than as a group -- a daemon
            # too old to send them must leave the last known values alone
            # rather than blanking them on every turn.
            if ev.data.get("ctx_tokens") is not None:
                self.last_ctx_tokens = ev.data["ctx_tokens"]
            if ev.data.get("ctx_max_tokens") is not None:
                self.last_ctx_max_tokens = ev.data["ctx_max_tokens"]
        elif ev.type == "tool_disabled":
            name = ev.data.get("name")
            if name and name not in self._disabled:
                self._disabled.append(name)
        elif ev.type == "model_changed":
            # Another client (or this one) switched the session's model:
            # the cached value follows immediately, not at the next status.
            if ev.data.get("model"):
                self.model = str(ev.data["model"])
        elif ev.type == "permission_mode_changed":
            # v0.42.0, the same shape as model_changed directly above and
            # for a sharper version of its reason: two tabs on one daemon
            # must not disagree about the model, and MUST not disagree
            # about whether this session still asks before it acts. The
            # cached value follows the daemon's broadcast immediately
            # rather than waiting for a status refresh.
            # No status round-trip is issued here, deliberately: the event
            # already CARRIES the new mode, and this frame goes on to the
            # out-of-band queue whose pump repaints the status bar for
            # every event it sees (see SessionPane._peer_pump's trailing
            # _refresh_status). base_changed below does ask for one, but
            # only because GitLine has to re-read a file this event does
            # not contain.
            if ev.data.get("mode"):
                self.permission_mode = str(ev.data["mode"])
        elif ev.type == "base_changed":
            # Item S #4/#5: another client (or this one) switched the
            # session's base. Unlike model_changed, nothing needs caching
            # HERE -- GitLine re-reads the worktree sidecar fresh, mtime-
            # guarded, on its own next render (see GitLine.base_branch);
            # this event's only job is to be SOMETHING that reaches every
            # attached client's out-of-band stream, so the trailing
            # _refresh_status() doxa.app's _peer_pump already runs after
            # every out-of-band event lands on the next event-driven
            # render, same as it always has.
            pass
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

    async def set_permission_mode(self, mode: str) -> str:
        """``/mode`` over the socket (v0.42.0) -- engine parity with
        :meth:`doxa.engine.SessionEngine.set_permission_mode`.

        The SAME shape as :meth:`set_model` above: the daemon owns the
        operation (it holds the SDK client), a refusal comes back as an
        error the caller shows verbatim, and every attached client -- not
        just this one -- learns the result through the daemon's
        ``permission_mode_changed`` broadcast. The confirmation for a gated
        mode is NOT on this path and must not be: it is a UI act, it
        happens in ``_cmd_mode`` before this is ever called, and a socket
        RPC that popped a dialog would be one that no headless caller
        could satisfy."""
        reply = await self._call("set_permission_mode", mode=mode)
        if not reply.get("ok"):
            raise EngineClientError(
                reply.get("error") or "permission mode switch refused"
            )
        self.permission_mode = str(reply.get("mode") or mode)
        return self.permission_mode

    async def switch_branch(self, target: "str | None") -> dict:
        """/branch over the socket (item S #4): the daemon does the git op
        either way (doxa.worktrees owns it), and on a successful SWITCH
        every attached client -- not just this one -- gets the
        ``base_changed`` echo (see :meth:`_handle_event`). Raises only on
        a TRANSPORT failure; an ordinary refusal (dirty tree, no such
        branch, ...) comes back as ``result["ok"] is False`` for the
        caller to show verbatim, same shape doxa.worktrees.switch_base
        itself returns."""
        reply = await self._call("branch", target=target)
        if not reply.get("ok"):
            raise EngineClientError(reply.get("error") or "branch call failed")
        return dict(reply.get("result") or {})

    async def answer_needs_input(self, req_id: str, answer: dict) -> bool:
        """Queue item 5: resolve one pending AskUserQuestion/permission
        request over the socket. False (rather than an exception) for a
        stale/unknown id or a daemon that is already gone -- the pane
        that sent it either raced another attached client's answer or is
        closing anyway, neither of which is worth surfacing as an error."""
        try:
            reply = await self._call("answer_needs_input", id=req_id, answer=answer)
        except EngineClientError:
            return False
        return bool(reply.get("ok"))

    def usage_summary(self) -> dict:
        """Engine-parity surface for /usage -- the daemon's own numbers,
        cached from the last status reply (same read-it-synchronously
        contract as belief_count and the cost figure)."""
        return dict(self._usage)

    async def context_usage(self) -> "dict | None":
        """``/context`` (item K) over the socket -- engine parity with
        :meth:`doxa.engine.SessionEngine.context_usage`.

        ASYNC and un-cached, unlike :meth:`usage_summary` above: the
        breakdown is a live control request to the CLI (only the daemon can
        issue it), it is far larger than a status field, and a session that
        has not been asked recently would otherwise report a stale picture
        of its own window. None means "this session cannot report one" --
        the same absence the in-process engine returns, so the pane's
        rendering has one case to handle, not two."""
        reply = await self._call("context")
        if not reply.get("ok"):
            raise EngineClientError(reply.get("error") or "context call failed")
        usage = reply.get("usage")
        return dict(usage) if isinstance(usage, dict) else None

    async def refresh_status(self) -> dict:
        reply = await self._call("status")
        status = reply.get("status") or {}
        if status.get("model"):
            self.model = status["model"]
        if status.get("permission_mode"):
            self.permission_mode = str(status["permission_mode"])
        if "bypass_armed" in status:
            self.bypass_armed = bool(status["bypass_armed"])
        if isinstance(status.get("account"), dict):
            self.account = status["account"]
        if status.get("lore_root"):
            self.lore_root = str(status["lore_root"])
        if status.get("total_cost_usd") is not None:
            self.total_cost_usd = status["total_cost_usd"]
        self.last_ctx_percentage = status.get("ctx_percentage")
        self.last_ctx_tokens = status.get("ctx_tokens")
        self.last_ctx_max_tokens = status.get("ctx_max_tokens")
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

    async def list_beliefs(
        self, limit: int = BELIEF_LIST_LIMIT, offset: int = 0
    ) -> list[dict]:
        """Engine parity for :meth:`SessionEngine.list_beliefs` (item 3's
        beliefs picker) -- a round trip, not a cache: unlike belief_count
        (refreshed on every status reply and read synchronously), the
        chip's picker only ever calls this on click, so a socket hop here
        is the whole cost discipline the status bar itself is exempt from.

        PAGED since v0.28.0, and the paging is entirely inside this method
        on purpose. Reported: "clicking on 'beliefs' chip leads to error
        message 'too much for a message'" -- one reply carrying every
        active belief WITH its claim body blows past MAX_FRAME_BYTES
        (the operator has ~517 of them), and doxa.daemon.encode_frame
        answers an oversize NON-event reply by discarding it entirely in
        favour of {"ok": false, "error": "reply exceeded the frame cap"},
        which this method then raised and the picker printed instead of
        opening. So the daemon now serves whatever fits per frame plus a
        resume offset, and this loops until it has the whole window.

        The caller sees exactly what SessionEngine.list_beliefs returns for
        the same arguments -- one complete list of full-text beliefs, no
        truncation, no cursor to manage. That parity is the requirement:
        doxa.app calls this through a plain ``getattr(engine,
        "list_beliefs")`` and cannot tell the two engines apart."""
        out: list[dict] = []
        cursor = max(0, offset)
        while len(out) < limit:
            reply = await self._call(
                "beliefs", offset=cursor, limit=limit - len(out),
            )
            if not reply.get("ok"):
                raise EngineClientError(
                    reply.get("error") or "beliefs call failed"
                )
            page = list(reply.get("beliefs") or [])
            out.extend(page)
            nxt = reply.get("next_offset")
            if not isinstance(nxt, int) or nxt <= cursor:
                # None (the store is exhausted) or a daemon that failed to
                # advance -- either way this loop is done. The non-advancing
                # guard is what keeps an old or buggy peer from spinning
                # here forever instead of just returning a short list.
                break
            cursor = nxt
        return out[:limit]

    async def belief_evidence(
        self, belief_id: int, limit: int = BELIEF_EVIDENCE_LIMIT
    ) -> list[dict]:
        """Engine parity for :meth:`SessionEngine.belief_evidence` (item V)
        -- one belief's evidence trail over the daemon split.

        Unpaged, unlike the two list calls around it, and that is the
        design rather than an omission: the trail is fetched for ONE
        belief that a reader expanded and the engine caps it at
        :data:`BELIEF_EVIDENCE_LIMIT` rows, so there is no unbounded list
        here for a pager to protect. The daemon still runs the page
        through the shared byte budget and reports ``evidence_truncated``
        when it bit; that flag rides back onto the last row so the surface
        says the trail was cut instead of showing a short one as whole."""
        reply = await self._call(
            "belief_evidence", belief_id=int(belief_id), limit=int(limit),
        )
        if not reply.get("ok"):
            raise EngineClientError(
                reply.get("error") or "belief_evidence call failed"
            )
        rows = [dict(row) for row in (reply.get("evidence") or [])]
        if reply.get("evidence_truncated") and rows:
            rows[-1]["trail_truncated"] = True
        return rows

    async def list_pending(
        self, limit: int = PENDING_LIST_LIMIT, offset: int = 0
    ) -> list[dict]:
        """Engine parity for :meth:`SessionEngine.list_pending` -- the
        `/pending` list over the daemon split.

        Paged the same way, and for the same reason,
        :meth:`list_beliefs` above is: a staged proposal is free text of
        unbounded length, and ``doxa.daemon.encode_frame`` discards an
        oversize reply rather than shortening it. The caller sees exactly
        what ``SessionEngine.list_pending`` returns for the same arguments
        -- one complete list, no cursor to manage -- because ``doxa.app``
        reaches both engines through the same ``getattr(engine,
        "list_pending")`` and must not be able to tell them apart.

        RECORDS since item V, where they were bare strings: a proposal has
        to carry its pending id (there is nothing to approve without one)
        and the fields the proposed verdict is computed from. A row that
        arrives as a string anyway -- an already-running daemon on the
        older build, which installing a new DOXA does not restart -- is
        passed through as it came and renders without a verdict rather
        than with a guessed one (``doxa.ui.labels.as_proposal``)."""
        out: list[dict] = []
        cursor = max(0, offset)
        while len(out) < limit:
            reply = await self._call(
                "pending", offset=cursor, limit=limit - len(out),
            )
            if not reply.get("ok"):
                raise EngineClientError(
                    reply.get("error") or "pending call failed"
                )
            out.extend(reply.get("pending") or [])
            nxt = reply.get("next_offset")
            if not isinstance(nxt, int) or nxt <= cursor:
                # Exhausted, or a daemon that failed to advance -- the same
                # non-advancing guard list_beliefs carries, keeping an old
                # or buggy peer from spinning here forever.
                break
            cursor = nxt
        return out[:limit]

    async def lore_write_state(self) -> dict:
        """Engine parity for :meth:`SessionEngine.lore_write_state` --
        asked of the DAEMON, because the daemon is the process holding
        lore_core and the store. A terminal with a current wheel installed
        can be driving a daemon that loaded a stale plugin checkout (the
        plugin checkout wins -- see doxa._lore_bootstrap), so answering
        this locally would report the wrong process's capability.

        async where ``SessionEngine``'s is sync: a socket round trip
        cannot be made synchronous, and the one caller awaits whichever it
        got."""
        reply = await self._call("lore_write")
        if not reply.get("ok"):
            raise EngineClientError(
                reply.get("error") or "lore_write call failed"
            )
        return dict(reply.get("state") or {})

    async def belief_action_state(self) -> dict:
        """Engine parity for :meth:`SessionEngine.belief_action_state` --
        asked of the DAEMON, which is the process holding lore_core and
        the store. Same reasoning as :meth:`lore_write_state`, and a
        separate call because it is a separate (narrower) capability."""
        reply = await self._call("belief_action_state")
        if not reply.get("ok"):
            raise EngineClientError(
                reply.get("error") or "belief_action_state call failed"
            )
        return dict(reply.get("state") or {})

    async def record_belief_outcome(
        self, belief_id: int, event: str, note: "str | None" = None,
    ) -> "str | None":
        """Engine parity for :meth:`SessionEngine.record_belief_outcome` --
        one belief, one verdict, and the outcome sentence back."""
        reply = await self._call("belief_outcome", belief_id=int(belief_id),
                                 event=str(event), note=note)
        return reply.get("error") or (None if reply.get("ok") else "outcome failed")

    async def retract_belief(
        self, belief_id: int, reason: str = "retracted from DOXA",
    ) -> "str | None":
        """Engine parity for :meth:`SessionEngine.retract_belief`."""
        reply = await self._call("retract_belief", belief_id=int(belief_id),
                                 reason=str(reason))
        return reply.get("error") or (None if reply.get("ok") else "retract failed")

    async def approve_pending(self, pid: str) -> "str | None":
        """Engine parity for :meth:`SessionEngine.approve_pending` -- ONE
        staged proposal, by id. The write happens in the daemon, through
        lore_core's own approve path; this carries the id there and the
        outcome back, and takes no list for the same reason that method
        does not."""
        reply = await self._call("approve_pending", pid=str(pid))
        return reply.get("error") or (None if reply.get("ok") else "approve failed")

    async def reject_pending(self, pid: str) -> "str | None":
        """Engine parity for :meth:`SessionEngine.reject_pending`."""
        reply = await self._call("reject_pending", pid=str(pid))
        return reply.get("error") or (None if reply.get("ok") else "reject failed")

    def disabled_tools(self) -> list[str]:
        return list(self._disabled)


async def attach(socket_path: str, cursor: int | None = None) -> EngineClient:
    """Connect-and-start convenience for the CLI and the attach picker."""
    client = EngineClient(socket_path, cursor=cursor)
    await client.start()
    return client

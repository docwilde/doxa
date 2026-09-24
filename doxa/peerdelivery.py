# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.peerdelivery -- the ONE outbound peer path, shared by every engine.

Everything that sends a peer message goes through :class:`PeerDelivery` --
a human typing ``/msg``, a model calling ``peer_send``, a broadcast -- and
it does so whichever engine is hosting the session. That is not tidiness.
The rate limit, the ledger record and the status bar's send light are
three things that must happen for EVERY send, and one send site per engine
each remembering to do all three is one chance per engine to ship a send
nobody can see. It was taken twice: ``doxa.vendors`` and ``doxa.codex``
both called :func:`doxa.peers.send_message` directly, so a DeepSeek or
Codex session's ``/msg`` was unlimited, unrecorded and invisible in the
mesh graph, while the identical keystroke in a Claude session was none of
those things (docwilde/doxa#39).

What an engine owes this class is a handful of callables rather than
itself. The values that CHANGE during a session -- the ``PeerHost``, the
model, the current turn id -- are read through a zero-argument callable at
the moment they are needed, because each of them is None for part of a
session's life and a value captured at construction would be the wrong
one for the rest of it. The engine module is never imported here, the same
rule :mod:`doxa.operators` follows for the seam this class hands it.

ONE SESSION, ONE SENDER, ACROSS PROCESSES. Not every engine runs the
model in DOXA's process. :mod:`doxa.codex` drives ``codex exec`` as a
subprocess and reaches it with a stdio MCP sidecar
(:mod:`doxa.mcpserver`), and a ``peer_send`` performed THERE would build
its own :class:`PeerDelivery`: a second rate limiter that knows nothing
of the session's budget, a second ledger writer, and lamps nobody in the
TUI is watching. So the sidecar does not send. :class:`SidecarDelivery`
forwards the operator's request over a per-session Unix socket that
:class:`EngineControl` serves in the ENGINE's process, and the engine
performs it through the same :class:`PeerDelivery` instance ``/msg``
already uses. Both halves of that protocol live in this module on
purpose -- a client and a server that agree by being written together
cannot drift apart the way two files can.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import Callable, Iterator, Sequence
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import peerledger as peerledger_mod
from . import peers as peers_mod
from .events import EngineEvent

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .peers import PeerHost, PeerInfo

__all__ = [
    "CONTROL_OP_PEER_SEND",
    "ENGINE_SOCKET_ENV",
    "ENGINE_TURN_ENV",
    "EngineControl",
    "PeerDelivery",
    "SidecarDelivery",
    "attributed_to_turn",
    "control_socket_path",
    "delivery_for",
]


# -- the engine control socket: constants both ends read ---------------

#: Where a sidecar process is told to find its engine. An IDENTITY
#: variable, never a secret: it reaches the sidecar through
#: ``-c mcp_servers.doxa.env.<KEY>``, which lands on ``codex exec``'s argv
#: and is readable by every process on this machine through ``ps``. A
#: path survives that; a token would not, which is why there is none --
#: see :class:`EngineControl` for the boundary that is used instead.
ENGINE_SOCKET_ENV = "DOXA_MCP_ENGINE_SOCKET"

#: Which turn the sidecar belongs to, same channel and same reasoning: a
#: turn id names a row in this session's own ledger and is of no use to
#: anyone reading it out of ``ps``.
ENGINE_TURN_ENV = "DOXA_MCP_TURN_ID"

#: The ONE operation the control socket serves. Anything else is refused
#: by name rather than ignored -- a socket that quietly accepted an
#: unknown op would be a socket whose surface nobody can state.
CONTROL_OP_PEER_SEND = "peer_send"

#: The request/reply line bound, shared with the peer protocol's own
#: frame cap (:data:`doxa.peers.MAX_FRAME_BYTES`, 64 KiB) because the
#: payload is the same payload: a body the operator already capped at
#: :data:`doxa.operators.MAX_PEER_BODY_CHARS` (8000) plus a session id.
CONTROL_MAX_BYTES = peers_mod.MAX_FRAME_BYTES

#: How long the engine waits for a request line before it gives up on the
#: connection. The sidecar writes the whole line immediately, so this is
#: only ever spent on a caller that stalled -- the same number and the
#: same reason as :data:`doxa.peers.RECV_TIMEOUT_SECS`.
CONTROL_RECV_TIMEOUT_SECS = peers_mod.RECV_TIMEOUT_SECS

#: How long the SIDECAR waits for the whole round trip. Generous on
#: purpose, and the arithmetic is the reason: the engine performs the
#: send inside this window, a broadcast fans out to every live session at
#: :data:`doxa.peers.SEND_TIMEOUT_SECS` (2 s) each, and the ledger append
#: takes a cross-process lock other sessions can be holding. A tight
#: bound here would report "nothing was sent" for a message that WAS
#: sent, which is the one failure mode worth paying wall clock to avoid.
CONTROL_CALL_TIMEOUT_SECS = 120.0


def control_socket_path(session_id: str) -> Path:
    """One session's engine control socket, beside its peer socket.

    Same directory and same naming discipline as
    :attr:`doxa.peers.PeerHost.socket_path`: the 0700 runtime dir, the
    session-id PREFIX rather than the whole id (an AF_UNIX path is capped
    at about 108 bytes and a full uuid under a pytest tmp dir blows the
    budget), and the pid so two live same-user sessions cannot collide on
    a shared prefix. Nothing derives this path from an id -- the engine
    hands the sidecar the path verbatim -- so the truncation costs
    nothing."""
    return peers_mod.runtime_dir() / f"engine-{session_id[:8]}-{os.getpid()}.sock"


#: "No override in force" -- distinct from an override TO None, which is
#: how a sidecar says its turn had ended.
_UNSET: Any = object()

#: The turn a send made in THIS execution context belongs to.
#:
#: A ContextVar and not an attribute on :class:`PeerDelivery`, and the
#: difference is a real race rather than a preference: asyncio copies the
#: context when a task is created, so an override set inside one control
#: connection's handler task is invisible to the ``/msg`` a human types
#: on the same event loop at the same moment. An attribute would leak
#: across exactly that interleaving, and the session would record one
#: send under another's turn.
_TURN_OVERRIDE: "ContextVar[Any]" = ContextVar(
    "doxa_peer_turn_override", default=_UNSET
)


@contextlib.contextmanager
def attributed_to_turn(turn_id: "str | None") -> "Iterator[None]":
    """Attribute every send made inside this block to ``turn_id``,
    whatever the hosting engine's own current turn is.

    Used by :class:`EngineControl` for a request that arrived from a
    sidecar: the sidecar was spawned FOR a turn and carries that turn's
    id, and the engine's own ``_turn_id`` is the right answer for every
    other send but this one. ``None`` is a legitimate override and means
    idle."""
    token = _TURN_OVERRIDE.set(turn_id)
    try:
        yield
    finally:
        _TURN_OVERRIDE.reset(token)


class PeerDelivery:
    """One session's outbound peer channel: the limiter, the ledger handle
    and the four steps a send takes, in the order they have to happen.

    Constructed by the engine that hosts the session and given read
    callables for everything about that session which is not fixed. It
    owns no event loop state of its own, so a test can drive it with four
    lambdas and no engine at all."""

    def __init__(
        self,
        *,
        session_id: str,
        engine_id: str,
        host: "Callable[[], PeerHost | None]",
        model: "Callable[[], str | None]",
        turn_id: "Callable[[], str | None]",
        emit: "Callable[[EngineEvent], None]",
        ledger: "peerledger_mod.PeerLedger | None" = None,
        limiter: "peerledger_mod.RateLimiter | None" = None,
    ) -> None:
        self.session_id = session_id
        self.engine_id = engine_id
        #: The process-wide ledger for this DOXA_HOME unless one is
        #: injected. Shared by every session in this process on purpose --
        #: it is one append-only file and the offsets a reader has already
        #: parsed are the whole value of holding an instance.
        self.ledger = ledger if ledger is not None else peerledger_mod.ledger()
        #: Per SESSION, never shared: the limit is this session's budget,
        #: and a limiter two sessions charged would be a limit on neither.
        self.limiter = limiter if limiter is not None else peerledger_mod.RateLimiter()
        self._host = host
        self._model = model
        self._turn_id = turn_id
        self._emit = emit

    # -- what this session is ------------------------------------------

    @property
    def host(self) -> "PeerHost | None":
        """The live ``PeerHost``, or None when the peer layer is not up in
        this session. Read through the engine's callable on every access
        rather than captured: it is None before ``start()`` and None again
        after ``finalize()``, and a send in either window must be refused
        rather than dispatched at a socket that is gone."""
        return self._host()

    def addressable_peers(self) -> "list[PeerInfo]":
        """Every live session this one may address, across every repo.

        Deliberately NOT :func:`peers.list_peers`, which filters to this
        session's own scope. The owner's decision is that addressing
        crosses repositories, and docs/plans/peer-publishing.md already
        named the prerequisite for that: a widened discovery surface is
        its own decision with a larger blast radius than same-repo
        discovery, because it can see work in every project the user has
        open. This is that surface, and it is why the sender's repo now
        travels with the message and is displayed on arrival -- a reader
        who cannot tell which project a message is about cannot weigh it.

        ``/peers`` and the peers status chip keep the scoped view: those
        answer "who is working on THIS with me", which is a different
        question from "whom could I address"."""
        return [
            peer for peer in peers_mod.read_registry(probe=True)
            if peer.session_id != self.session_id
        ]

    def sender(self) -> peerledger_mod.Sender:
        """Who this session says it is, for the ``from`` block of a
        record. Every field but ``session`` is self-description and the
        ledger treats it as such."""
        host = self.host
        return peerledger_mod.Sender(
            session=self.session_id,
            title=host.title if host is not None else None,
            repo=host.scope_key if host is not None else None,
            model=self._model(),
            engine=self.engine_id,
        )

    def current_turn_id(self) -> "str | None":
        """Which turn a send made right here belongs to.

        Normally the hosting engine's own callable -- it is the object
        that mints turn ids and the only one that knows when a turn ends.
        The exception is a send that arrived from a SIDECAR process
        (:class:`EngineControl`): that request names the turn its sidecar
        was spawned for, and that name wins, because the engine's current
        turn and the sidecar's are the same turn only by coincidence of
        timing and the ledger row has to be right by construction. See
        :func:`attributed_to_turn` for why the override is a ContextVar."""
        override = _TURN_OVERRIDE.get()
        if override is not _UNSET:
            return override
        return self._turn_id()

    def turn_ref(self) -> peerledger_mod.TurnRef:
        """This send's turn context. ``idle`` when no turn is running, and
        that is a measurement rather than bookkeeping: a message sent
        while the sender is idle is unprompted, and an unprompted message
        is the shape a coordinator has (peerledger.TurnRef)."""
        turn_id = self.current_turn_id()
        if turn_id is None:
            return peerledger_mod.IDLE_TURN
        return peerledger_mod.TurnRef(id=turn_id, state="running")

    # -- the one outbound path -----------------------------------------

    async def deliver(
        self,
        targets: "Sequence[PeerInfo]",
        text: str,
        *,
        kind: str = "direct",
        in_reply_to: "str | None" = None,
    ) -> dict:
        """Send one message to ``targets``, record it, and light the lamp.

        Order is load-bearing and each step's position is argued in a
        module this one only calls:

        1. **Charge the limit first.** It is a SEND-side limit, priced in
           DELIVERIES -- a broadcast to 31 peers costs 31, which is the
           difference between a limit and a decoration at this fan-out
           (peerledger.decide_send). A refusal raises
           :class:`peerledger.SendRefused`, whose decision carries the
           reason AND the reset; the caller surfaces both verbatim,
           because an agent told why it was refused can reason about it
           and one silently throttled just retries. The charge is for the
           ATTEMPT, so deliveries that then fail are still spent. That is
           the safe direction and not an oversight: a session hammering a
           dead socket is precisely the loop this bound exists to stop,
           and a limit that refunded failures would not stop it.
        2. **Send, per peer, tolerating per-peer failure.** A dead socket
           or an oversize frame fails one delivery, not the call.
        3. **Append AFTER, naming only the peers actually reached**, and
           with the RAW body -- ``PeerLedger.append`` hashes before it
           scrubs, so a caller that pre-scrubbed would hand it a hash of
           the redaction and two identical messages would stop matching. A
           record written before the attempt counts a delivery that never
           happened, which inflates out-degree -- the first measure the
           experiment reports. One append per send, never one per
           recipient: the record IS the message, and a partial broadcast
           is a record whose ``to`` is short.
        4. **Emit ``peer_sent``** so the status bar's send light flashes
           for a send this pane did not type.

        Raises :class:`peers.PeerSendError` when nothing could be
        delivered, so a total failure is never mistaken for a quiet
        success. A partial one returns, with ``failed`` naming who missed
        out -- the record already says the same thing by omission."""
        host = self.host
        if host is None:
            raise peers_mod.PeerSendError("peer layer is not running in this session")
        if not targets:
            raise peers_mod.PeerSendError("no addressable peer")
        body = str(text or "")
        if not body.strip():
            raise peers_mod.PeerSendError("refusing to send an empty message")

        # A delivery crosses several awaits.  Its turn is the turn in which
        # it started, even if the engine starts or finishes another turn
        # while a peer socket or ledger lock is pending.
        turn = self.turn_ref()

        # 1. the limit, before a byte moves
        decision = self.limiter.charge(
            recipients=[peer.session_id for peer in targets],
            turn_id=turn.id if turn.state == "running" else None,
        )
        decision.raise_if_refused()

        # 2. the sends
        delivered: "list[PeerInfo]" = []
        failed: "list[tuple[PeerInfo, str]]" = []
        for peer in targets:
            try:
                await peers_mod.send_message(
                    peer.socket_path,
                    from_id=self.session_id,
                    from_title=host.title,
                    body=body,
                    from_repo=host.scope_key,
                    kind=kind,
                )
            except peers_mod.PeerSendError as exc:
                failed.append((peer, str(exc)))
            else:
                delivered.append(peer)

        if not delivered:
            reasons = "; ".join(f"{p.session_id[:8]}: {why}" for p, why in failed)
            raise peers_mod.PeerSendError(f"nothing was delivered -- {reasons}")

        # 3. the record -- off the event loop, because appending takes a
        # cross-process lock that N other sessions can be holding.
        record: "peerledger_mod.Message | None" = None
        ledger_error: "str | None" = None
        try:
            record = await self.ledger.append_async(
                sender=self.sender(),
                to=[peer.session_id for peer in delivered],
                body=body,
                kind=kind,
                in_reply_to=in_reply_to,
                turn=turn,
            )
        except Exception as exc:  # noqa: BLE001 -- a full ledger must not eat a delivered message
            # The message HAS been delivered; refusing to return now would
            # tell the sender it failed when it did not. The failure is
            # reported instead of swallowed -- an unrecorded send is a
            # measurement that cannot be taken again, and the one thing
            # worse than losing it is losing it quietly.
            ledger_error = f"{type(exc).__name__}: {exc}"

        # 4. the send light
        self._emit(EngineEvent("peer_sent", {
            "to": [peer.session_id for peer in delivered],
            "kind": kind,
            "message_id": record.id if record is not None else None,
        }))

        out: dict = {
            "delivered_to": [
                {"session_id": peer.session_id, "title": peer.title,
                 "repo": peer.scope_key}
                for peer in delivered
            ],
            "kind": kind,
            "message_id": record.id if record is not None else None,
            "deliveries_charged": decision.fanout,
            "turn_deliveries_used": decision.turn_used + decision.fanout,
            "turn_delivery_limit": decision.turn_limit,
            "window_deliveries_used": decision.window_used + decision.fanout,
            "window_delivery_limit": decision.window_limit,
        }
        if failed:
            out["failed"] = [
                {"session_id": peer.session_id, "error": why} for peer, why in failed
            ]
        if ledger_error is not None:
            out["ledger_error"] = (
                f"delivered, but NOT recorded in the peer ledger ({ledger_error}) "
                "-- this send is missing from the mesh graph"
            )
        return out

    async def send_to(self, target_prefix: str, text: str) -> "PeerInfo":
        """``/msg``'s path: one message to ONE peer, resolved by full
        session id or by a prefix matching exactly one. Raises
        peers.PeerSendError on no match, ambiguity, a refusing rate limit,
        or transport failure -- always the sender's problem to see, never
        the receiver's.

        It goes through :meth:`deliver` like every other send, on every
        engine. A human-typed message that did not appear in the mesh
        graph would make the graph a picture of the model's traffic rather
        than of the fleet's."""
        if self.host is None:
            raise peers_mod.PeerSendError("peer layer is not running in this session")
        peer = peers_mod.resolve_peer(self.addressable_peers(), target_prefix)
        try:
            await self.deliver([peer], text)
        except peerledger_mod.SendRefused as exc:
            # The limiter's refusal is already a sentence naming the reason
            # and the reset. Re-raised as the error type every caller of
            # this method already handles, with that sentence intact rather
            # than replaced by a shorter one that says less.
            raise peers_mod.PeerSendError(str(exc)) from exc
        return peer

    async def tool_send(self, request: dict) -> dict:
        """The ``peer_send`` seam the tool gate's OperatorContext carries.

        Takes an already-validated request from doxa.operators (a target
        prefix or the broadcast marker, the body, an optional in_reply_to)
        and returns an ordinary result dict -- including for a refusal,
        which is a soft error the model reads and recovers from, never an
        exception that would cost it a strike on the two-strikes tracker
        for a limit doing its job."""
        body = str(request.get("body") or "")
        in_reply_to = request.get("in_reply_to") or None
        broadcast = bool(request.get("broadcast"))
        target = str(request.get("to") or "").strip()

        if self.host is None:
            return {"error": "peer_send: the peer layer is not running in this session"}
        candidates = self.addressable_peers()
        if broadcast:
            if not candidates:
                return {"error": "peer_send: no other session is running -- nothing to broadcast to"}
            targets, kind = candidates, "broadcast"
        else:
            try:
                targets, kind = [peers_mod.resolve_peer(candidates, target)], "direct"
            except peers_mod.PeerSendError as exc:
                # Ambiguity and no-match both land here, and the message
                # already names the candidates. A soft error: the model
                # can fix it by naming a full session id.
                return {"error": f"peer_send: {exc}"}

        try:
            return await self.deliver(
                targets, body, kind=kind, in_reply_to=in_reply_to,
            )
        except peerledger_mod.SendRefused as exc:
            decision = exc.decision
            return {
                "error": f"peer_send: {decision.reason}",
                "refused_by": decision.scope,
                "resets": decision.reset_description(),
                "deliveries_requested": decision.fanout,
            }
        except peers_mod.PeerSendError as exc:
            return {"error": f"peer_send: {exc}"}


class EngineControl:
    """The ENGINE's half of the sidecar seam: one Unix socket per session
    over which a sidecar process asks this engine to perform a send.

    WHY IT EXISTS. :mod:`doxa.mcpserver` runs in a process ``codex exec``
    spawns and kills per turn. A ``peer_send`` performed there would build
    its own :class:`PeerDelivery`: a rate limiter that starts empty every
    turn (and so limits nothing), a second ledger writer racing the
    first's lock, and ``peer_sent`` lamps emitted into a queue no TUI
    reads. Forwarding the request to the engine instead keeps the session
    at ONE limiter, ONE ledger writer and ONE event queue -- the property
    the whole of this module exists to hold -- at the cost of one socket.

    THE PROTOCOL, whole. One connection carries one request and one
    reply, both a single JSON object on one line, both bounded by
    :data:`CONTROL_MAX_BYTES`::

        -> {"op": "peer_send", "turn_id": "a1b2c3", "request": {...}}
        <- {"ok": true,  "result": {...}}
        <- {"ok": false, "error": "..."}

    ``request`` is the operator's own request dict, already validated by
    :func:`doxa.operators._peer_send` in the sidecar (target or broadcast
    marker, body, optional ``in_reply_to``); ``result`` is whatever
    :meth:`PeerDelivery.tool_send` returned, verbatim, refusals included.
    ``turn_id`` is the turn the sidecar was spawned for and it WINS over
    the engine's own (see :func:`attributed_to_turn`). ``op`` has exactly
    one legal value: anything else is refused by name, so the surface
    this socket offers is stateable in one sentence.

    THE TRUST BOUNDARY is the filesystem's -- same uid, same machine --
    exactly as it is for :class:`doxa.peers.PeerHost`'s socket: 0600
    inside the 0700 runtime dir. A process that can connect here is one
    running as this user, which can already rewrite this session's
    config, read its transcript and write frames straight into its peer
    socket. There is deliberately NO token: a token would have to reach
    the sidecar through ``-c mcp_servers.doxa.env.<KEY>``, which lands on
    ``codex exec``'s argv and is readable by every process on the machine
    through ``ps``. A secret published to everyone is not a secret, and
    inventing one would trade a real boundary for the look of a second.

    The socket stays open for the life of the session rather than for the
    life of a turn, and the user's ``DOXA_AGENT_PEER_SEND`` switch is
    re-read on every request rather than at ``start()``: the setting lives
    in the environment or ``~/.doxa/config.toml`` and can be turned off
    mid-session, and a socket that had captured the answer would keep
    sending after the user said stop."""

    def __init__(
        self,
        delivery: PeerDelivery,
        *,
        path: "str | Path | None" = None,
    ) -> None:
        self.delivery = delivery
        self.path = (
            Path(path) if path is not None
            else control_socket_path(delivery.session_id)
        )
        self._server: "asyncio.AbstractServer | None" = None

    @property
    def running(self) -> bool:
        """Is the socket actually being served? What the engine asks
        before it tells a sidecar the path -- a path to a socket nobody
        is listening on would offer the model a tool that always fails."""
        return self._server is not None

    async def start(self) -> None:
        """Bind and serve. Clamps the runtime dir to 0700 through the same
        :func:`doxa.peers.registry_dir` the peer layer uses, unlinks a
        stale socket left by a crashed same-pid run, and chmods to 0600 --
        the same three steps, in the same order, as
        :meth:`doxa.peers.PeerHost.start`."""
        peers_mod.registry_dir()
        with contextlib.suppress(OSError):
            self.path.unlink()
        self._server = await asyncio.start_unix_server(
            self._handle_conn, path=str(self.path), limit=CONTROL_MAX_BYTES,
        )
        os.chmod(self.path, 0o600)

    async def stop(self) -> None:
        """Close and UNLINK. A session that ended must leave no socket
        behind for the next one to inherit -- the pid in the filename
        makes a collision unlikely, not impossible."""
        server, self._server = self._server, None
        if server is not None:
            server.close()
            with contextlib.suppress(Exception):
                await server.wait_closed()
        with contextlib.suppress(OSError):
            self.path.unlink()

    async def _handle_conn(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """One connection: read one line, answer one line, close.

        Never raises and never leaves the caller hanging: every failure
        below becomes an ``ok: false`` reply, because the caller is a
        model's tool call and a dropped connection would reach it as
        ``peer_send failed: ...`` -- the shape
        :func:`doxa.gate.is_hard_failure` counts as a strike, for an
        engine-side refusal that is working as designed."""
        reply: dict
        try:
            try:
                async with asyncio.timeout(CONTROL_RECV_TIMEOUT_SECS):
                    line = await reader.readline()
            except (asyncio.TimeoutError, ValueError):
                # ValueError is readline's LimitOverrunError: one line over
                # CONTROL_MAX_BYTES. Both are refusals, not crashes.
                reply = {"ok": False, "error": (
                    "the control request was unreadable -- no line arrived "
                    f"within {CONTROL_RECV_TIMEOUT_SECS:g}s, or it was over "
                    f"the {CONTROL_MAX_BYTES}-byte bound"
                )}
            else:
                reply = await self._serve(line)
        except Exception as exc:  # noqa: BLE001 -- see the docstring
            reply = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        with contextlib.suppress(Exception):
            writer.write(
                json.dumps(reply, ensure_ascii=False).encode("utf-8") + b"\n"
            )
            await writer.drain()
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()

    async def _serve(self, line: bytes) -> dict:
        """One parsed request, or the reason it was refused.

        Every check here is about the SHAPE of the request and the user's
        own switch. Nothing validates the peer message itself -- the
        sidecar's :func:`doxa.operators._peer_send` already did that, and
        :meth:`PeerDelivery.tool_send` does the rest -- because a second
        validator is a second set of rules to keep in step with the
        first."""
        if not line.strip():
            return {"ok": False, "error": "empty control request"}
        try:
            request = json.loads(line.decode("utf-8", errors="replace"))
        except ValueError:
            return {"ok": False, "error": "the control request was not JSON"}
        if not isinstance(request, dict):
            return {"ok": False, "error": (
                "the control request was not a JSON object"
            )}

        op = request.get("op")
        if op != CONTROL_OP_PEER_SEND:
            return {"ok": False, "error": (
                f"unsupported op {op!r} -- this socket serves "
                f"{CONTROL_OP_PEER_SEND!r} and nothing else"
            )}
        payload = request.get("request")
        if not isinstance(payload, dict):
            return {"ok": False, "error": (
                "'request' must be the operator's request object"
            )}

        # Re-read, not captured: the switch is the user's and lives
        # outside any repository a session has open (peers.peer_send_enabled).
        if not peers_mod.peer_send_enabled():
            return {"ok": False, "error": (
                "messaging other sessions is off on this DOXA install -- the "
                f"user turns it on in ~/.doxa/config.toml (agent_peer_send) "
                f"or {peers_mod.PEER_SEND_ENV}, and nothing in this "
                "repository can"
            )}

        raw_turn = request.get("turn_id")
        turn_id = str(raw_turn).strip() if raw_turn is not None else ""
        with attributed_to_turn(turn_id or None):
            result = await self.delivery.tool_send(payload)
        return {"ok": True, "result": result}


class SidecarDelivery:
    """The SIDECAR's half: a ``peer_send`` seam that sends nothing itself.

    Built by :func:`delivery_for` inside :mod:`doxa.mcpserver` and handed
    to the tool gate as ``OperatorContext.peer_send``; its
    :meth:`tool_send` is the exact callable
    :func:`doxa.operators._peer_send` invokes, so the operator cannot tell
    that the send happens in another process -- which is the point.

    It imports no engine and holds no ledger, no limiter and no
    ``PeerHost``. All it knows about the session is a socket path and a
    turn id, both read off the environment the engine set.

    Every failure is an ordinary result dict in the operator's own
    single-colon refusal shape (``{"error": "peer_send: ..."}``) rather
    than an exception. A raise here would be turned into
    ``peer_send failed: ...`` by the gate, which
    :func:`doxa.gate.is_hard_failure` counts as a strike -- so an engine
    that had merely gone away would disable the tool for the rest of the
    turn."""

    def __init__(
        self,
        session_id: str,
        cwd: str,
        *,
        socket_path: str,
        turn_id: "str | None" = None,
    ) -> None:
        self.session_id = session_id
        self.cwd = cwd
        self.socket_path = str(socket_path)
        self.turn_id = turn_id or None

    async def tool_send(self, request: dict) -> dict:
        """Forward one already-validated request to the engine and return
        its result dict verbatim.

        The turn id rides the request rather than being looked up on the
        far side: the engine's ``_turn_id`` is whatever is running when
        the frame arrives, and this sidecar's turn is the one that has to
        appear in the ledger row."""
        frame = json.dumps({
            "op": CONTROL_OP_PEER_SEND,
            "turn_id": self.turn_id,
            "request": dict(request or {}),
        }, ensure_ascii=False).encode("utf-8") + b"\n"
        if len(frame) > CONTROL_MAX_BYTES:
            return {"error": (
                f"peer_send: the request is {len(frame)} bytes, over the "
                f"{CONTROL_MAX_BYTES} this session's engine accepts -- "
                "shorten the body, or point at a file in the repository"
            )}

        try:
            async with asyncio.timeout(CONTROL_CALL_TIMEOUT_SECS):
                reader, writer = await asyncio.open_unix_connection(
                    self.socket_path, limit=CONTROL_MAX_BYTES,
                )
                try:
                    writer.write(frame)
                    await writer.drain()
                    line = await reader.readline()
                finally:
                    writer.close()
                    with contextlib.suppress(Exception):
                        await writer.wait_closed()
        except Exception as exc:  # noqa: BLE001 -- a soft error, see the class
            return {"error": (
                f"peer_send: this session's engine did not answer on its "
                f"control socket ({type(exc).__name__}) -- nothing was sent"
            )}

        return self._result(line)

    @staticmethod
    def _result(line: bytes) -> dict:
        """The engine's reply line, as the operator's result dict.

        A malformed or missing reply is reported as exactly that, never as
        a success: the one thing worse than a refused send is a send the
        model believes happened."""
        if not line.strip():
            return {"error": (
                "peer_send: this session's engine closed the control "
                "connection without answering -- whether the message was "
                "sent cannot be stated"
            )}
        try:
            reply = json.loads(line.decode("utf-8", errors="replace"))
        except ValueError:
            return {"error": "peer_send: the engine's reply was not JSON"}
        if not isinstance(reply, dict):
            return {"error": (
                "peer_send: the engine's reply was not a JSON object"
            )}
        if not reply.get("ok"):
            reason = reply.get("error") or "the engine refused the send"
            return {"error": f"peer_send: {reason}"}
        result = reply.get("result")
        if not isinstance(result, dict):
            return {"error": (
                "peer_send: the engine answered without a result -- whether "
                "the message was sent cannot be stated"
            )}
        return result


def delivery_for(session_id: str, cwd: str) -> "SidecarDelivery | None":
    """The factory :data:`doxa.mcpserver.PEER_DELIVERY_FACTORY` names.

    Returns a :class:`SidecarDelivery` when this process was told where
    its engine listens (:data:`ENGINE_SOCKET_ENV`), and None when it was
    not. None is the honest answer rather than a refusing object: the
    server projects ``peer_send`` only when this returns something, so a
    sidecar with no engine behind it offers no tool at all -- absence,
    which the model cannot call, rather than a refusal it can retry.

    Named and shaped for the sidecar, but it reads only the environment,
    so anything that can set these two variables can use it."""
    path = str(os.environ.get(ENGINE_SOCKET_ENV) or "").strip()
    if not path:
        return None
    return SidecarDelivery(
        session_id,
        cwd,
        socket_path=path,
        turn_id=str(os.environ.get(ENGINE_TURN_ENV) or "").strip() or None,
    )

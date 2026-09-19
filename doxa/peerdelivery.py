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
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from . import peerledger as peerledger_mod
from . import peers as peers_mod
from .events import EngineEvent

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .peers import PeerHost, PeerInfo

__all__ = ["PeerDelivery"]


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

    def turn_ref(self) -> peerledger_mod.TurnRef:
        """This send's turn context. ``idle`` when no turn is running, and
        that is a measurement rather than bookkeeping: a message sent
        while the sender is idle is unprompted, and an unprompted message
        is the shape a coordinator has (peerledger.TurnRef)."""
        turn_id = self._turn_id()
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

        # 1. the limit, before a byte moves
        decision = self.limiter.charge(
            recipients=[peer.session_id for peer in targets],
            turn_id=self._turn_id(),
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
                turn=self.turn_ref(),
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

# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.peernet -- peers that are not on this machine.

``doxa.peers`` finds other sessions through a directory of presence files
under ``$XDG_RUNTIME_DIR/doxa/registry``, mode 0700. That is a good design
and its security model is the filesystem's: same uid, same machine, no
protocol to get wrong. It also means two machines cannot see each other at
all, and docs/plans/emergent-organization.md wants a cluster -- several
machines each running a fleet, with the fleets able to talk -- because one
box's memory is the ceiling on N.

This module is the transport that crosses that gap, and it is track R2 of
docs/plans/remote.md: the piece that spec says is unbuilt, with the policy
(``doxa.remote_policy``) already shipped as track R1. **Every authorization
question here is asked of that module.** There is no second policy in this
file: no allow-list of its own, no notion of what a remote driver may do,
no opinion about whether listening is on. It owns the WIRE and nothing
else, and hands every decision to :func:`doxa.remote_policy.evaluate`.

WHAT A REACHABLE DAEMON IS. remote.md says it plainly and it governs
everything below: "a reachable daemon socket is **remote code execution
with the user's privileges**, and no amount of UI care compensates for
getting this wrong." So:

* **Loopback is the default and is not merely a default.**
  :func:`bind_host` returns ``127.0.0.1`` unless told otherwise, AND
  :meth:`PeerNetServer.start` refuses to bind anything at all unless
  ``remote_listening_decision`` allows it. Two independent gates, because
  the failure mode of one is a machine on a coffee-shop network serving
  the user's shell.
* **No new credential store.** Nothing here reads or writes a token, a
  password or a key file. Identity is the ``Tailscale-User-Login`` header
  that ``tailscale serve`` attaches, and the tailnet is what vouches for
  it. remote.md: "If DOXA finds itself writing a password or token file,
  the design took a wrong turn."
* **The header is believed on ONE path.** ``tailscale serve`` terminates
  TLS and forwards to a loopback listener; a header arriving anywhere else
  was written by whoever connected. :meth:`PeerNetServer` therefore
  computes ``from_loopback`` from the SOCKET's own peer address -- never
  from anything in the request -- and hands that to
  ``remote_policy.identity_decision``, which refuses unconditionally when
  it is false, before it looks at the login at all.
* **DOXA's allow-list is DOXA's own.** ``remote_policy.allowed_logins()``,
  empty by default, and an empty list refuses everyone rather than
  everyone. Defence in depth over the tailnet's own ACLs.
* **Say who is connected.** A peer that came over the wire carries
  :attr:`doxa.peers.PeerInfo.origin` naming where it came from, and every
  surface that shows a local peer shows that marker. remote.md: "A silent
  second driver is the thing a user cannot detect and cannot consent to."
  Here the equivalent is a silent second FLEET -- a roster row that looks
  like the session next to you and is actually a machine in another room.

THE REQUEST KINDS ARE REMOTE_POLICY'S, NOT NEW ONES. This is the part
worth reading twice, because the temptation was to invent
``REQUEST_PEER_ROSTER`` and friends. Mapping onto the existing vocabulary
instead is what keeps one policy:

    roster  -> REQUEST_READ_STATUS      it is a status read: who is here
    history -> REQUEST_READ_TRANSCRIPT  it returns recorded conversation
    deliver -> REQUEST_SEND_PROMPT      an arriving peer message can START
                                        a turn (peers.peer_inbound_turns_
                                        enabled), so it spends this
                                        machine's budget exactly as a
                                        prompt does. Calling it anything
                                        cheaper would be a lie about its
                                        cost.

An op this module does not recognise is refused before policy is consulted,
and an op that IS recognised still goes through ``evaluate`` -- recognition
is not permission.

WHAT THIS IS NOT. Not a second daemon protocol: it never touches a session
socket's ``prompt``/``call`` surface, so ``!`` shell and
``bypassPermissions`` are unreachable from here by construction rather
than by a check (they are still refused by ``request_kind_decision``, and
that refusal is tested, but the stronger statement is that this file has
no code that could reach them). Not multi-user: the allow-list is the
operator's own logins on their own tailnet.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import ipaddress
import json
from dataclasses import dataclass, replace
from typing import Any, Callable

from . import peers as peers_mod
from . import remote_policy as policy_mod

__all__ = [
    "DEFAULT_PORT",
    "Endpoint",
    "PeerNetError",
    "PeerNetServer",
    "RemoteRefused",
    "bind_host",
    "combined_roster",
    "deliver",
    "endpoints",
    "fetch_roster",
    "listen_decision",
    "loopback",
]


DEFAULT_PORT = 47600
"""The loopback port ``tailscale serve`` is pointed at when remote peering
is on. High, fixed and unprivileged; overridable with
``DOXA_REMOTE_PORT``. Fixed rather than ephemeral because the forwarding
rule on the other side has to name it, and a port that moves per launch is
a forwarding rule that breaks per launch."""

DEFAULT_BIND = "127.0.0.1"
"""Loopback, and the only default there will be. See the module docstring's
first bullet: this is half of a two-gate design, not a cautious constant."""

MAX_BODY_BYTES = peers_mod.MAX_FRAME_BYTES
"""Same 64 KiB ceiling the local peer transport enforces. One number, so a
message that crosses machines and a message that does not are subject to
the same bound -- two ceilings would eventually disagree and the disagreement
would only show up on the path nobody tests."""

CONNECT_TIMEOUT_SECS = 5.0
READ_TIMEOUT_SECS = 10.0

OP_ROSTER = "roster"
OP_DELIVER = "deliver"
OP_HISTORY = "history"

#: Each op and the ``doxa.remote_policy`` request kind it IS. See the
#: module docstring for why these are the existing kinds rather than new
#: ones. An op absent from this mapping is refused before policy is
#: consulted -- recognition is not permission, but non-recognition is
#: certainly refusal.
OP_KINDS = {
    OP_ROSTER: policy_mod.REQUEST_READ_STATUS,
    OP_DELIVER: policy_mod.REQUEST_SEND_PROMPT,
    OP_HISTORY: policy_mod.REQUEST_READ_TRANSCRIPT,
}


class PeerNetError(RuntimeError):
    """A cross-machine peer operation failed. Always surfaced to the
    caller, the same contract :class:`doxa.peers.PeerSendError` has: a
    message that did not cross has to be visible as one."""


class RemoteRefused(PeerNetError):
    """The other end -- or this end's own policy -- refused, and said why.

    A distinct class from :class:`PeerNetError` because the two need
    different words in a UI: a refusal is an answer, a failure is a
    question. ``reason`` is never empty; docs/plans/remote.md's testing bar
    requires that a refusal be "visible rather than silent", and an
    exception with no sentence in it is silent in every way that matters."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# -- configuration, read through doxa.config like every other knob -----


def bind_host() -> str:
    """``DOXA_REMOTE_BIND`` / the config's ``remote_bind`` row.

    ``127.0.0.1`` unless explicitly changed, and changing it is not by
    itself enough to listen: :meth:`PeerNetServer.start` still asks
    ``remote_policy``. The intended production value stays the default --
    ``tailscale serve`` forwards to loopback, so DOXA never binds a
    tailnet address itself and never terminates TLS. A user who sets this
    to ``0.0.0.0`` has left the design behind, and
    :func:`listen_decision`'s reason says so."""
    from . import config as config_mod

    raw = config_mod.raw("DOXA_REMOTE_BIND").strip()
    return raw or DEFAULT_BIND


def bind_port() -> int:
    from . import config as config_mod

    raw = config_mod.raw("DOXA_REMOTE_PORT").strip()
    try:
        return int(raw) if raw else DEFAULT_PORT
    except ValueError:
        return DEFAULT_PORT


def loopback(host: "str | None") -> bool:
    """Is this address one only this machine can reach?

    Parsed as an address rather than string-compared: ``127.0.0.1`` is not
    the only loopback address, ``127.0.0.2`` is one too, and a check that
    only knew the famous one would call a genuinely local connection
    remote. An unparseable or absent host is NOT loopback -- fails closed,
    the same direction an empty allow-list fails in."""
    if not host:
        return False
    try:
        return ipaddress.ip_address(str(host).strip()).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class Endpoint:
    """One other machine's peer bridge: a name to show, and where it is.

    ``label`` is what a roster row SAYS -- the whole point of
    :attr:`doxa.peers.PeerInfo.origin` is that a human and a model can both
    see which machine a peer is on, so the label is a name a person chose
    (``workstation``), not a socket address they have to decode."""

    label: str
    host: str
    port: int = DEFAULT_PORT

    def __post_init__(self) -> None:
        if not str(self.label).strip():
            raise ValueError("a remote endpoint needs a label")
        if not str(self.host).strip():
            raise ValueError(f"endpoint {self.label!r} has no host")

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"


def endpoints() -> "tuple[Endpoint, ...]":
    """``DOXA_REMOTE_PEERS`` / the config's ``remote_peers`` row, parsed.

    Format: ``label=host:port``, comma-separated --
    ``workstation=ws.tail1234.ts.net:47600, laptop=lp.tail1234.ts.net``.
    A malformed entry is DROPPED rather than raised on, and that is the
    conservative direction here rather than the lazy one: this is read on
    the display path, and a typo in one endpoint must not empty a roster
    that three good endpoints would have filled. The dropped entry is
    invisible to a reader, which is why ``doxa doctor``'s own row for this
    prints the parsed list rather than the raw string.

    Contains no credential and cannot: an endpoint is a hostname. What
    makes the connection trustworthy is the tailnet it runs on, which is
    exactly remote.md's "no new credential store"."""
    from . import config as config_mod

    raw = config_mod.raw("DOXA_REMOTE_PEERS")
    found: "list[Endpoint]" = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry or "=" not in entry:
            continue
        label, _, target = entry.partition("=")
        host, _, port = target.strip().partition(":")
        try:
            found.append(
                Endpoint(
                    label=label.strip(),
                    host=host.strip(),
                    port=int(port) if port.strip() else DEFAULT_PORT,
                )
            )
        except ValueError:
            continue
    return tuple(found)


def listen_decision() -> policy_mod.Decision:
    """May a cross-machine listener exist at all right now?

    Straight through to ``remote_policy.remote_listening_decision`` -- this
    function adds ONE thing, a louder reason when the bind address has been
    moved off loopback, and adds no authority whatsoever. It cannot allow
    what the policy refuses."""
    decision = policy_mod.remote_listening_decision(
        enabled=policy_mod.remote_enabled()
    )
    if decision.allowed and not loopback(bind_host()):
        return policy_mod.Decision.allow(
            f"remote listening is enabled AND bound to {bind_host()}, which "
            "is NOT loopback -- DOXA is reachable from the network directly "
            "rather than through a tailscale serve forwarder, and the "
            "Tailscale-User-Login header will be refused on every request "
            "that arrives there (doxa.remote_policy.identity_decision)"
        )
    return decision


# -- the listener ------------------------------------------------------


class PeerNetServer:
    """The bridge: a minimal HTTP listener that answers three ops.

    HTTP rather than the line-JSON the local socket speaks, for one
    reason: ``tailscale serve`` is an HTTP forwarder and the
    ``Tailscale-User-Login`` header is an HTTP header. Speaking anything
    else would mean inventing the identity channel this design exists not
    to invent.

    Deliberately NOT the daemon's protocol, and not a process inside the
    daemon. remote.md's own open question 1 asks whether the daemon should
    serve the network or a bridge should, and answers itself: "A bridge
    keeps the daemon's attack surface exactly as it is today and makes
    'remote off' the trivial default." This is that bridge. The daemon is
    unchanged and unreachable from here.

    ``handlers`` is injected so the whole authorization path is testable
    without a fleet behind it -- and so that the refusal tests can be run
    against a DELIBERATELY PERMISSIVE handler set, which is the only way a
    refusal test proves anything (docs/plans/remote.md's testing bar, and
    tests/test_remote_policy.py's own note on vacuity)."""

    def __init__(
        self,
        handlers: "dict[str, Callable[[dict], Any]] | None" = None,
        *,
        host: "str | None" = None,
        port: "int | None" = None,
        evaluate: "Callable[..., policy_mod.Decision] | None" = None,
    ) -> None:
        self.host = host or bind_host()
        self.port = int(port) if port is not None else bind_port()
        self.handlers = dict(handlers or {})
        # The policy seam, injected ONLY so a test can watch what this
        # module asks. It defaults to the real function and there is no
        # code path that answers a policy question itself: see the module
        # docstring's "no second policy".
        self._evaluate = evaluate or policy_mod.evaluate
        self._server: "asyncio.AbstractServer | None" = None
        self.refusals: "list[str]" = []

    @property
    def listening(self) -> bool:
        return self._server is not None

    async def start(self) -> policy_mod.Decision:
        """Bind, if and only if policy allows. Returns the decision either
        way -- never raises for a refusal, because "remote is off" is the
        ordinary state of almost every machine and an exception is the
        wrong shape for the common case.

        THE ASSERTION THE SPEC ASKS FOR is about what happens when nobody
        does anything: with no configuration at all, this returns a refusal
        and :attr:`listening` is False, so there is no socket. Not a socket
        that rejects -- no socket."""
        decision = listen_decision()
        if not decision.allowed:
            self.refusals.append(decision.reason)
            return decision
        self._server = await asyncio.start_server(
            self._handle, host=self.host, port=self.port
        )
        return decision

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        with contextlib.suppress(Exception):
            await self._server.wait_closed()
        self._server = None

    @property
    def port_in_use(self) -> "int | None":
        """The port actually bound, for a caller that passed 0. None when
        nothing is listening -- which is the default, and is the answer a
        test asserting absence reads."""
        if self._server is None:
            return None
        for sock in self._server.sockets or ():
            with contextlib.suppress(Exception):
                return int(sock.getsockname()[1])
        return None

    # -- the wire -----------------------------------------------------

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            async with asyncio.timeout(READ_TIMEOUT_SECS):
                head = await reader.readuntil(b"\r\n\r\n")
                headers = _parse_headers(head)
                length = min(int(headers.get("content-length", "0") or 0), MAX_BODY_BYTES)
                raw = await reader.readexactly(length) if length else b"{}"
            peername = writer.get_extra_info("peername")
            from_loopback = loopback(peername[0] if peername else None)
            body = json.loads(raw.decode("utf-8", errors="replace") or "{}")
            status, payload = await self._dispatch(
                body if isinstance(body, dict) else {},
                login=headers.get("tailscale-user-login"),
                from_loopback=from_loopback,
            )
        except Exception as exc:  # malformed, oversize, truncated, timed out
            status, payload = 400, {"ok": False, "reason": f"bad request: {exc}"}
        with contextlib.suppress(Exception):
            writer.write(_response(status, payload))
            await writer.drain()
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()

    async def _dispatch(
        self, body: dict, *, login: "str | None", from_loopback: bool
    ) -> "tuple[int, dict]":
        """One request, decided and answered.

        Async ONLY because one handler (``deliver``) has to await a Unix
        socket write; every decision above that point is synchronous and a
        test drives this method directly with no listener bound at all --
        which is how the refusal tests reach the policy path on a machine
        where remote listening is off, i.e. on every machine."""
        op = str(body.get("op") or "")
        kind = OP_KINDS.get(op)
        if kind is None:
            reason = f"{op!r} is not an operation this bridge serves"
            self.refusals.append(reason)
            return 400, {"ok": False, "reason": reason}
        decision = self._evaluate(kind, login=login, from_loopback=from_loopback)
        if not decision.allowed:
            # VISIBLE, both ways: the reason goes back on the wire for the
            # caller, AND onto this server's own list so the machine that
            # refused can say what it refused and why. A refusal only the
            # refuser knows about is the silent failure remote.md's testing
            # bar exists to prevent.
            self.refusals.append(decision.reason)
            return 403, {"ok": False, "reason": decision.reason}
        handler = self.handlers.get(op)
        if handler is None:
            reason = f"{op!r} is permitted but this bridge wires no handler for it"
            return 501, {"ok": False, "reason": reason}
        try:
            result = handler(body)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            return 500, {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
        payload = {"ok": True, "reason": decision.reason}
        payload.update(result if isinstance(result, dict) else {"result": result})
        return 200, payload


def _parse_headers(head: bytes) -> "dict[str, str]":
    """Request line + headers, lowercased. Only the header names this
    module reads matter, and it reads two."""
    out: "dict[str, str]" = {}
    for line in head.decode("latin-1").split("\r\n")[1:]:
        name, sep, value = line.partition(":")
        if sep:
            out[name.strip().lower()] = value.strip()
    return out


def _response(status: int, payload: dict) -> bytes:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    reason = {200: "OK", 400: "Bad Request", 403: "Forbidden",
              500: "Internal Server Error", 501: "Not Implemented"}.get(status, "OK")
    head = (
        f"HTTP/1.1 {status} {reason}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    )
    return head.encode("latin-1") + body


# -- the client side ---------------------------------------------------


async def request(
    endpoint: Endpoint,
    body: dict,
    *,
    login: "str | None" = None,
    timeout: float = CONNECT_TIMEOUT_SECS,
) -> dict:
    """One request to one endpoint. Raises rather than returning a failure,
    because every caller here has to distinguish "no peers there" from
    "could not ask".

    ``login`` sets ``Tailscale-User-Login`` and exists for the LOOPBACK
    path -- a test, or two bridges on one machine. In the deployment this
    is designed for, ``tailscale serve`` attaches the header and whatever
    DOXA put there is replaced; a client setting it gains nothing, because
    the receiving side believes the header only on a loopback connection,
    and anyone who can make a loopback connection to this machine is
    already the user. That is worth stating rather than leaving as an
    apparent hole: this parameter is not a credential and cannot be used as
    one."""
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    if len(payload) > MAX_BODY_BYTES:
        raise PeerNetError(
            f"request too large ({len(payload)} bytes > {MAX_BODY_BYTES} max)"
        )
    headers = [
        "POST /peers HTTP/1.1",
        f"Host: {endpoint.host}:{endpoint.port}",
        "Content-Type: application/json",
        f"Content-Length: {len(payload)}",
        "Connection: close",
    ]
    if login:
        headers.append(f"Tailscale-User-Login: {login}")
    request_bytes = ("\r\n".join(headers) + "\r\n\r\n").encode("latin-1") + payload
    try:
        async with asyncio.timeout(timeout):
            reader, writer = await asyncio.open_connection(endpoint.host, endpoint.port)
            try:
                writer.write(request_bytes)
                await writer.drain()
                raw = await reader.read(MAX_BODY_BYTES + 4096)
            finally:
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
    except (RemoteRefused, PeerNetError):
        raise
    except Exception as exc:
        raise PeerNetError(f"{endpoint.label}: {exc}") from exc
    _head, _sep, raw_body = raw.partition(b"\r\n\r\n")
    try:
        answer = json.loads(raw_body.decode("utf-8", errors="replace") or "{}")
    except ValueError as exc:
        raise PeerNetError(f"{endpoint.label}: unreadable reply ({exc})") from exc
    if not isinstance(answer, dict):
        raise PeerNetError(f"{endpoint.label}: reply was not an object")
    if not answer.get("ok"):
        raise RemoteRefused(
            f"{endpoint.label}: {answer.get('reason') or 'refused with no reason given'}"
        )
    return answer


#: The :class:`doxa.peers.PeerInfo` fields that describe how to reach a
#: session ON ITS OWN MACHINE, and the values that say "not from here".
#:
#: A Unix socket path, a pid and a daemon socket are coordinates in one
#: kernel's namespace. Sent across a machine boundary they are at best
#: meaningless and at worst a map of the other machine -- and a reader that
#: adopted them would hold a PeerInfo whose ``socket_path`` names a path in
#: ITS OWN filesystem that some entirely unrelated process may own. So they
#: are blanked on the way out (:func:`local_handlers`) AND on the way in
#: (:func:`fetch_roster`), by the same rule ``origin`` already follows:
#: what the reader can establish for itself, it establishes for itself, and
#: this it establishes by knowing the row came off a socket.
#:
#: Blanked rather than dropped so the dataclass keeps its declared types --
#: ``socket_path: str``, ``pid: int`` -- and every consumer's falsiness
#: check (``socket_alive("")`` is False, ``if peer.daemon_socket``) already
#: reads them as "no". Delivery to a remote session goes through
#: :data:`OP_DELIVER`, which resolves the target on the machine that owns
#: it; there is nothing a local caller could do with these anyway.
_TRANSPORT_LOCAL_BLANKS = {"socket_path": "", "pid": 0, "daemon_socket": None}


async def fetch_roster(
    endpoint: Endpoint, *, login: "str | None" = None
) -> "list[peers_mod.PeerInfo]":
    """That machine's live sessions, every one of them marked remote.

    THE LINE THAT MATTERS is the ``replace(..., origin=endpoint.label)``
    below: the origin is stamped from the endpoint WE dialled, never from
    anything the reply said. A machine that returns a row claiming
    ``origin: null`` -- i.e. claiming to be local -- gets overwritten, and
    that is not paranoia about a hostile peer so much as the same rule the
    rest of this layer already follows: doxa.peers treats every field
    another process wrote as a claim. "Which machine is this on" is the one
    fact the reader can establish for itself, so it establishes it."""
    answer = await request(endpoint, {"op": OP_ROSTER}, login=login)
    rows = answer.get("peers") or []
    out: "list[peers_mod.PeerInfo]" = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        with contextlib.suppress(Exception):
            peer = peers_mod.peer_from_mapping(row)
            out.append(replace(
                peer, origin=endpoint.label, **_TRANSPORT_LOCAL_BLANKS
            ))
    return out


async def deliver(
    endpoint: Endpoint,
    *,
    target: str,
    from_id: str,
    from_title: str,
    body: str,
    from_repo: "str | None" = None,
    kind: str = "direct",
    login: "str | None" = None,
) -> dict:
    """Hand one message to a session on another machine.

    The receiving bridge's handler is what actually puts it on that
    session's local peer socket -- so a message that crosses a machine
    boundary lands in exactly the same inbox, scrubbed by exactly the same
    receive path (``PeerHost._handle_conn``), as one from the session next
    door. There is no second delivery path and no second scrub."""
    return await request(
        endpoint,
        {
            "op": OP_DELIVER,
            "target": target,
            "from_id": from_id,
            "from_title": from_title,
            "body": body,
            "from_repo": from_repo,
            "kind": kind,
        },
        login=login,
    )


# -- the handlers a real bridge wires -----------------------------------


def local_handlers(
    *, scope_key: "str | None" = None
) -> "dict[str, Callable[[dict], Any]]":
    """The three ops, answered from THIS machine's own peer layer.

    Split out of :class:`PeerNetServer` rather than built into it for the
    reason the class's own docstring gives: a refusal test that cannot run
    against a deliberately permissive handler set proves nothing, so the
    handlers have to be something a test supplies. These are what a real
    bridge supplies.

    ``scope_key`` bounds ALL THREE ops, not just the roster: the sessions
    a roster lists, the sessions ``deliver`` can resolve a target among,
    and the repo whose ledger rows ``history`` returns. A scope that one
    op honours and two ignore is not a scope.

    ``roster`` returns registry rows with their ``origin: None`` intact --
    TRUE from here (these sessions are local to this machine), and the
    fetching side overwrites it with its own label anyway -- and with
    ``socket_path``/``pid``/``daemon_socket`` blanked, because those name
    a place in THIS kernel and mean nothing, or something wrong, anywhere
    else. See :data:`_TRANSPORT_LOCAL_BLANKS`.

    ``deliver`` resolves the target against the live registry and hands the
    message to ``peers.send_message`` -- the SAME call a local ``/msg``
    makes, onto the SAME Unix socket, scrubbed by the SAME receive path.
    There is no second delivery mechanism, which is why a message from
    another machine cannot arrive with fewer checks than one from the
    session next door."""

    def _in_scope(rows: "list[peers_mod.PeerInfo]") -> "list[peers_mod.PeerInfo]":
        """``scope_key`` applied, for the handlers that answer about
        sessions. All three ops are scoped by it or none of them is: a
        roster that hides the sessions in another repo while `deliver`
        still reaches them, and `history` still quotes them, is not a
        scope -- it is a filter on one screen."""
        if not scope_key:
            return rows
        return [p for p in rows if p.scope_key == scope_key]

    def _roster(_body: dict) -> dict:
        live = _in_scope(peers_mod.read_registry(probe=True))
        rows = []
        for peer in live:
            # vars(), then the transport-local fields blanked -- see
            # _TRANSPORT_LOCAL_BLANKS for why a socket path and a pid do
            # not cross a machine boundary.
            row = dict(vars(peer))
            row.update(_TRANSPORT_LOCAL_BLANKS)
            rows.append(row)
        return {"peers": rows, "count": len(rows)}

    async def _deliver(body: dict) -> dict:
        target = str(body.get("target") or "").strip()
        if not target:
            raise PeerNetError("deliver: no target session named")
        body_text = str(body.get("body") or "")
        if not body_text:
            raise PeerNetError("deliver: empty message")
        peer = peers_mod.resolve_peer(
            _in_scope(peers_mod.read_registry(probe=True)), target
        )
        await peers_mod.send_message(
            peer.socket_path,
            from_id=str(body.get("from_id") or "?"),
            from_title=str(body.get("from_title") or "?"),
            body=body_text,
            from_repo=body.get("from_repo"),
            kind=str(body.get("kind") or "direct"),
        )
        return {"delivered_to": peer.session_id, "title": peer.title}

    def _history(body: dict) -> dict:
        from . import peerledger as peerledger_mod

        limit = max(1, min(int(body.get("limit") or peerledger_mod.DEFAULT_LIMIT), 200))
        ledger = peerledger_mod.ledger()
        # Scoped by the SENDER's repo, which is the same
        # ``PeerHost.scope_key`` a roster row is filtered by -- see
        # _in_scope. The predicate goes into the scan, so a scoped caller
        # gets `limit` rows from its own repo rather than whatever
        # survived a global slice.
        rows = (
            ledger.in_repo(scope_key, limit=limit) if scope_key
            else ledger.recent(limit=limit)
        )
        return {"messages": [m.to_obj() for m in rows], "count": len(rows)}

    return {OP_ROSTER: _roster, OP_DELIVER: _deliver, OP_HISTORY: _history}


async def combined_roster(
    *,
    scope_key: "str | None" = None,
    self_id: "str | None" = None,
    remote: "tuple[Endpoint, ...] | None" = None,
    login: "str | None" = None,
) -> "tuple[list[peers_mod.PeerInfo], list[str]]":
    """Local peers and remote peers in one list, each marked, plus the
    problems.

    Returns ``(peers, problems)`` rather than swallowing failures, because
    a roster missing a machine has to SAY it is missing a machine: a
    cluster where one node quietly dropped out looks exactly like a cluster
    where one node has no sessions, and those are very different facts.

    Local peers keep ``origin=None``. That is not laziness about marking
    them -- None IS the marker for local, read by
    :func:`doxa.ui.labels.peer_origin`, and it means a build that has never
    heard of remote peers reads every row it can see as local, which is
    exactly right."""
    local = (
        peers_mod.list_peers(scope_key, self_id=self_id)
        if scope_key
        else [p for p in peers_mod.read_registry(probe=True) if p.session_id != self_id]
    )
    out = list(local)
    problems: "list[str]" = []
    targets = remote if remote is not None else endpoints()
    for endpoint in targets:
        try:
            out.extend(await fetch_roster(endpoint, login=login))
        except PeerNetError as exc:
            problems.append(str(exc))
    return out, problems

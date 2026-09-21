# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.meshgraph -- who is messaging whom, in a browser, while it happens.

DOXA is about to let agents message each other on their own initiative.
The rule this inherits from DOXA's remote spec is the one that decides the
whole shape of this module: *a silent second driver is the thing a user
cannot detect and cannot consent to.* Agent-to-agent traffic is a second
driver. This view is how it stops being silent -- so the target is not a
pretty picture, it is **legibility of traffic the user did not type**.

WHY A BROWSER AND NOT THE TUI, which is where the rest of DOXA lives. A
graph is the one artifact a terminal is genuinely bad at: 32 nodes and a
few hundred edges have no honest character-cell rendering, and
``doxa.beliefgraph`` already measured what happens when you try -- a
whole-graph mermaid view came out 1188x13814, readable at no zoom. The
page is not a second UI for DOXA; it is one view of one file, opened on
demand and closed again.

WHAT IT READS. ``doxa/peerledger.py`` appends one JSON object per line,
append-only, the shape documented on :func:`parse_record`. **This module
does not import that one AT MODULE LEVEL.** ``doxa.peerledger`` pulls in
``lore_core.scrub`` to do its own scrubbing, which this view has no
reason to load just to serve a page; :func:`ledger_path` imports it
lazily, inside the function, the same way it already deferred
``doxa.config``. The whole coupling is :func:`ledger_path` and
:func:`parse_record`, and nothing else in this file or the page has to
know which. Reading a file nobody has written yet is not an error here:
an absent ledger is an empty graph, which is the truthful picture of a
fleet that has not said anything.

THE SECURITY POSTURE IS ``doxa.beliefgraph``'S, FOR A SHARPER REASON.
That module serves rendered belief pages over a loopback-only HTTP server
on an ephemeral port, token-gated, started on demand -- and its comment
explains why loopback alone was not judged enough: *"loopback" is not
"this user"*. An HTTP server on 127.0.0.1 answers any LOCAL process no
matter whose it is, and 65k ports is not a secret. That argument is
strictly stronger here. A rendered belief page holds claims the user
wrote about themselves; this ledger holds **full message bodies** between
agents working in the user's repositories, which the emergence plan
requires be recorded untruncated because the content is the measurement.
So: loopback binding is the boundary (:func:`require_loopback` refuses to
start on anything else, rather than trusting a caller to pass the right
string), and a per-process token gates every route on top of it.

BODIES ARE UNTRUSTED TEXT AND NEVER REACH THE DOM AS MARKUP. The rule is
structural rather than a matter of remembering to escape at each call
site: **the page is static and every record arrives as JSON**, so a body
containing ``<script>`` is a JSON string value at every point in its life
-- never a byte the HTML parser looks at. The graph itself is drawn on a
``<canvas>``, whose ``fillText`` cannot express markup at all, and the
side panel writes through ``textContent``. ``assets/mesh/mesh.js``
therefore contains no ``innerHTML`` anywhere, and
``tests/test_meshgraph.py`` asserts that as a standing property of the
file, because this is exactly the kind of invariant a later edit breaks
by accident.

TWO ENDPOINTS, AND THE CURSOR THAT JOINS THEM. ``/ledger`` returns
everything so far plus the byte ``offset`` it stopped at; ``/events``
streams what arrives after a given offset. The page opens the stream at
the offset the snapshot handed back, which is what keeps the two from
either double-counting a record or dropping one written between the two
requests. Server-sent events rather than a websocket: the traffic is
one-way and SSE is a text protocol over the HTTP server already here --
a websocket would mean a dependency, and DOXA adds none for this.

EDGES ARE DERIVED IN PYTHON, NOT IN THE PAGE (:func:`edges_for`). One
broadcast at N=32 is 31 deliveries, and the emergence plan turns on
telling that apart from 31 people choosing to speak -- so the fan-out is
computed once, server-side, where a test can pin it, and tagged with the
kind so the page can draw it as a single fan rather than 31 unrelated
strokes. The page draws what it is given and derives no topology of its
own.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Iterable

__all__ = [
    "LEDGER_ENV",
    "MeshServer",
    "edges_for",
    "json_bytes",
    "ledger_path",
    "parse_record",
    "read_batch",
    "read_records",
    "require_loopback",
    "serve",
]

#: Points :func:`ledger_path` somewhere else -- the override a test uses,
#: and the one knob that lets this view follow the writer if
#: ``doxa.peerledger`` settles on a different home than the default below.
LEDGER_ENV = "DOXA_PEER_LEDGER"

#: How often the SSE loop looks for new bytes. 250ms is under the
#: threshold where an edge appearing feels like a consequence of the
#: message rather than a refresh, and costs one ``stat`` per quarter
#: second per open page -- a page nobody has open costs nothing, because
#: the loop only exists inside a live request.
POLL_SECS = 0.25

#: A comment frame every this often on an idle stream. Without it a proxy
#: or a laptop suspend can leave a dead connection that looks open, and
#: the page's reconnect never fires because nothing ever errored.
HEARTBEAT_SECS = 15.0

#: Refuse a ledger line longer than this rather than buffer it. A body is
#: full and unbounded by design, but a single line past a megabyte is a
#: corrupt file or a hostile one, and neither deserves the memory.
MAX_LINE_BYTES = 1 << 20


# -- the ledger, behind the seam ------------------------------------------
#
# Everything this module knows about how the ledger is stored lives in the
# three functions below. ledger_path() delegates to doxa.peerledger, and
# parse_record() has been checked against its emitter (doxa.peerledger's
# Message.to_obj()) field for field -- see parse_record's own docstring
# for the shape both sides now agree on. No other line in this file or in
# assets/mesh/ has to move if peerledger's storage changes again.


def ledger_path() -> Path:
    """The append-only ledger this view reads.

    :data:`LEDGER_ENV` overrides first, which is how a test points this
    at a fixture and how this view would follow the writer to a
    different home if ``doxa.peerledger`` ever chose one. Absent that,
    this DELEGATES to :func:`doxa.peerledger.ledger_path` --
    ``$DOXA_HOME/peers/messages.jsonl``, DOXA's durable state home,
    deliberately NOT the runtime dir the peer registry uses. ``doxa.peers``
    puts presence files under ``$XDG_RUNTIME_DIR``, which is correct for
    presence (it SHOULD evaporate when the machine reboots, because the
    sessions did) and wrong for this: the emergence experiment's whole
    output is the ledger, collected after a run of 640 agent-sessions has
    finished and torn itself down. A record that vanishes on reboot
    cannot be the measurement.

    ``doxa.peerledger`` offers no env override of its own, so
    :data:`LEDGER_ENV` is not a precedence question -- it is honoured
    here, before the delegation, and peerledger is never even asked."""
    override = os.environ.get(LEDGER_ENV, "").strip()
    if override:
        return Path(override)
    # Deferred, not at module level: doxa.peerledger pulls in
    # lore_core.scrub to do its own scrubbing on write, which this
    # read-only view has no reason to load just to compute a path.
    from . import peerledger as peerledger_mod

    return peerledger_mod.ledger_path()


def parse_record(line: str) -> "dict[str, Any] | None":
    """One ledger line as the page consumes it, or None if the line is not
    a usable record.

    The shape written by ``doxa.peerledger``::

        {"v": 1, "id": "<uuid4 hex>", "ts": "2026-09-17T19:32:00.123456Z",
         "from": {"session": "<id>", "title": str|null, "repo": str|null,
                  "model": str|null, "engine": str|null},
         "to": ["<session id>", ...],
         "kind": "direct" | "broadcast",
         "in_reply_to": "<message id>" | null,
         "body": "<scrubbed text>", "body_sha256": "<hex>",
         "latency_ms": <int> | null,
         "turn": {"id": "<turn id>" | null, "state": "idle" | "running"}}

    FOUR PROPERTIES OF THAT RECORD THAT THE VIEW MUST NOT MISREAD, each
    confirmed with the writer rather than assumed:

    * **The sender writes it, exactly once; a receiver never appends.**
      That is what makes one ``from`` and N ``to`` coherent, and it is
      why this module only ever reads. Anything that appended on receipt
      would double every message and put out-degree -- the experiment's
      first measure -- out by a factor of two.
    * **It is written after the send succeeded, and ``to`` names the
      peers actually reached.** A per-peer failure means that peer is
      simply absent. So an edge on this graph means delivery *happened*,
      not that it was attempted, and the view must never suggest
      otherwise.
    * **``turn`` and ``latency_ms`` are both SENDER-side.** One record
      with N recipients cannot carry N receiver states, so ``turn`` is
      what the sender was doing; ``latency_ms`` is the wall time the
      sender took to compose this message, measured from the message in
      ``in_reply_to``, and is ``null`` whenever there is no reference
      point -- which is common. Null is not zero and is not rendered.
    * **Every identity field except ``session`` may be null.** A session
      outside a repository has no root, and an older build reports no
      model. The page falls back to the short session id rather than
      showing the word "null".

    **None rather than a raise, for every kind of bad line**, and that is
    the load-bearing decision here rather than laziness about validation.
    This file is read while it is being appended to by a different
    process: a half-flushed line, a line from a future schema version, a
    truncated tail after a crash. If any of those could take down the
    reader, the view would go dark exactly when the fleet got busy --
    which is the moment it exists for. A skipped line is a missing edge;
    a raised exception is a blind operator.

    What a record must have to be drawable at all: a sender session id
    and a list of recipients. Everything else is presentation, and a
    record missing it still counts as traffic."""
    try:
        record = json.loads(line)
    except (ValueError, TypeError):
        return None
    if not isinstance(record, dict):
        return None

    sender = record.get("from")
    if not isinstance(sender, dict):
        return None
    session = sender.get("session")
    if not isinstance(session, str) or not session:
        return None

    # `to` is normalized to a list of non-empty strings here rather than
    # trusted: the page indexes nodes by these values, and a null or a
    # nested object in the list would become a node labelled "undefined"
    # that no session corresponds to. The writer already de-duplicates and
    # preserves order; doing it again costs nothing and means a repeated
    # id can never silently double an edge's weight.
    raw_to = record.get("to")
    recipients: "list[str]" = []
    if isinstance(raw_to, list):
        for target in raw_to:
            if isinstance(target, str) and target and target not in recipients:
                recipients.append(target)

    # KIND IS READ, NEVER INFERRED FROM len(to). It is tempting to treat
    # one recipient as "direct", and it is wrong: a broadcast to a
    # two-session fleet reaches exactly one peer and is still a
    # broadcast. Since broadcast-vs-pairwise is the emergence plan's
    # primary manipulation, guessing it from shape would fabricate the
    # experiment's independent variable out of its dependent one.
    #
    # So a record whose kind is missing or unrecognised becomes
    # "unknown" -- drawn in neutral grey, counted as neither. That keeps
    # the traffic visible (it is still real) without inventing a fact
    # about it, and it stays honest when the writer adds a third kind
    # this build has never heard of.
    kind = record.get("kind")
    if kind not in ("direct", "broadcast"):
        kind = "unknown"

    turn = record.get("turn")
    if not isinstance(turn, dict):
        turn = {}

    # bool is an int in Python, and a float is a plausible thing for a
    # writer to emit for milliseconds. Accept a real number, reject True.
    latency = record.get("latency_ms")
    if isinstance(latency, bool) or not isinstance(latency, (int, float)):
        latency = None

    return {
        "id": record.get("id") if isinstance(record.get("id"), str) else "",
        "ts": record.get("ts") if isinstance(record.get("ts"), str) else "",
        "from": session,
        "title": sender.get("title") if isinstance(sender.get("title"), str) else "",
        "repo": sender.get("repo") if isinstance(sender.get("repo"), str) else "",
        "model": sender.get("model") if isinstance(sender.get("model"), str) else "",
        "engine": sender.get("engine") if isinstance(sender.get("engine"), str) else "",
        "to": recipients,
        "kind": kind,
        "in_reply_to": (
            record.get("in_reply_to") if isinstance(record.get("in_reply_to"), str) else None
        ),
        "body": record.get("body") if isinstance(record.get("body"), str) else "",
        # Both of these are the SENDER's, never the recipients' -- see the
        # contract notes above. The names say so on the wire.
        "sender_latency_ms": latency,
        "sender_turn_state": turn.get("state") if isinstance(turn.get("state"), str) else "",
        "edges": edges_for(session, recipients, kind),
    }


def edges_for(sender: str, recipients: "Iterable[str]", kind: str) -> "list[dict[str, str]]":
    """The drawn edges one record produces: one per delivery, tagged with
    the record's kind.

    A broadcast at the experiment's N=32 is 31 edges laid down in a single
    instant. Telling that apart from 31 sessions independently choosing to
    speak is not a cosmetic distinction -- broadcast-vs-pairwise is the
    emergence plan's *primary manipulation*, and a view that renders them
    identically cannot show the thing the experiment is measuring. So the
    kind rides on every edge, and the page draws a broadcast as one fan.

    **Self-delivery is dropped.** A broadcast is naturally addressed to
    the whole roster including the sender, and a node with an edge to
    itself is a loop the force layout cannot place and a reader cannot
    interpret."""
    return [
        {"from": sender, "to": target, "kind": kind}
        for target in recipients
        if target != sender
    ]


def read_batch(
    path: Path, offset: int = 0
) -> "tuple[list[tuple[dict[str, Any], int]], int]":
    """``([(record, offset_after_it), ...], offset_consumed_to)``.

    Two different offsets, and the distinction is the point:

    * the **per-record** one tags each SSE frame as its event id, so a
      browser that drops the connection reconnects with ``Last-Event-ID``
      naming the last record it actually dispatched -- not the end of the
      batch it was halfway through, which would silently skip the rest.
    * the **consumed** one is how far the reader got regardless of what
      it yielded. It has to be separate, because a line that is skipped
      still has to be stepped over: a batch of nothing but malformed
      lines would otherwise leave the cursor where it was and re-read the
      same garbage on every poll, forever.

    **Only whole lines are consumed, and neither offset ever passes the
    last newline seen.** This is the entire correctness argument for
    tailing a file another process is appending to: read at any instant
    and the tail may be half a line, because a write is not atomic.
    Advancing past a partial line would drop the record it belongs to
    permanently -- the cursor only moves forward, so it would never be
    re-read. Stopping at the last newline means the partial line is read
    again, complete, on the next poll.

    A missing file is an empty batch, not an error: the ledger does not
    exist until a session sends something, and an empty graph is the
    honest picture of a fleet that has not spoken."""
    try:
        with open(path, "rb") as handle:
            handle.seek(offset)
            chunk = handle.read()
    except OSError:
        return [], offset

    # Keep only through the final newline; anything after it is a partial
    # line, re-read on the next poll once its writer has finished it.
    cut = chunk.rfind(b"\n") if chunk else -1
    if cut < 0:
        return [], offset

    found: "list[tuple[dict[str, Any], int]]" = []
    position = offset
    for raw in chunk[: cut + 1].split(b"\n")[:-1]:
        position += len(raw) + 1  # the line, plus the newline it ended on
        if not raw.strip() or len(raw) > MAX_LINE_BYTES:
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            # A corrupt byte range is a skipped line, never a dead reader.
            continue
        record = parse_record(text)
        if record is not None:
            found.append((record, position))
    return found, position


def read_records(path: Path, offset: int = 0) -> "tuple[list[dict[str, Any]], int]":
    """Every record after ``offset``, and the offset to resume at -- the
    plain form of :func:`read_batch`, and what ``/ledger`` serves.

    The returned offset is how far the reader consumed rather than the
    file's size, so a record half-written at the instant of the snapshot
    is left for the stream to deliver whole instead of falling into the
    gap between the two requests."""
    found, position = read_batch(path, offset)
    return [record for record, _ in found], position


# -- the loopback boundary ------------------------------------------------


def require_loopback(host: str) -> str:
    """``host`` if it names the loopback interface; otherwise
    :class:`ValueError`.

    A guard rather than a documented convention, because the failure it
    prevents is silent and total. Every byte of protection around this
    ledger is the bind address: full message bodies between agents in the
    user's repositories, served without any authentication step to pass.
    Bound to ``0.0.0.0`` on a laptop on a cafe network, that is the whole
    corpus offered to the subnet, and nothing about the running process
    would look different -- same page, same URL, same logs.

    So the address is not a parameter a caller may get wrong. ``0.0.0.0``
    is rejected, as is a routable address and a hostname that is not
    loopback; ``localhost`` is accepted and resolved by the stack."""
    import ipaddress

    candidate = (host or "").strip()
    if candidate.lower() in ("localhost", "localhost."):
        return "127.0.0.1"
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError as exc:
        raise ValueError(
            f"mesh graph refuses to bind {host!r}: not a loopback address. "
            "This server has no authentication and the ledger holds full "
            "message bodies -- 127.0.0.1 is the security boundary."
        ) from exc
    if not address.is_loopback:
        raise ValueError(
            f"mesh graph refuses to bind {host!r}: not a loopback address. "
            "This server has no authentication and the ledger holds full "
            "message bodies -- 127.0.0.1 is the security boundary."
        )
    return candidate


# -- where the page lives -------------------------------------------------


def assets_dir() -> Path:
    """``assets/mesh`` -- the packaged copy when DOXA was installed, the
    repo's own when it is a checkout.

    The two-step is ``doxa.banner``'s, which resolves ``assets/logo.png``
    the same way and for the same reason: one copy in git, mapped into the
    wheel at build time rather than duplicated under ``doxa/``.

    NOTE for packaging: ``pyproject.toml``'s
    ``[tool.hatch.build.targets.wheel.force-include]`` currently maps
    ``assets/icon.png`` and ``assets/logo.png`` only. Until it also maps
    this directory, the page is a source-checkout feature and an installed
    DOXA finds nothing here -- which :meth:`MeshServer` reports as a plain
    404 naming the directory it looked in, rather than a blank page."""
    try:
        import importlib.resources

        packaged = importlib.resources.files("doxa") / "assets" / "mesh"
        if packaged.is_dir():  # type: ignore[union-attr]
            return Path(str(packaged))
    except (ImportError, TypeError, ModuleNotFoundError, AttributeError):
        pass
    return Path(__file__).resolve().parent.parent / "assets" / "mesh"


#: What may be served out of :func:`assets_dir`, by exact name and with
#: its content type. An allow-list rather than a directory walk: this
#: server sits next to a file of message bodies, and "serve whatever is
#: in that folder" is how a stray file becomes a route. The key is the
#: path segment AFTER the token; nothing in it is user-supplied and no
#: filesystem path is ever built from the request, so there is nothing
#: here to traverse.
STATIC_FILES = {
    "": ("index.html", "text/html; charset=utf-8"),
    "index.html": ("index.html", "text/html; charset=utf-8"),
    "mesh.js": ("mesh.js", "text/javascript; charset=utf-8"),
    "mesh.css": ("mesh.css", "text/css; charset=utf-8"),
}


# -- the server -----------------------------------------------------------


class MeshServer:
    """A loopback-only HTTP server for one ledger file.

    Started explicitly and never by default -- the same posture as
    ``doxa.beliefgraph``'s page server and DOXA's sync hub: *off unless
    the owner turned it on*. A DOXA that never opens this view never opens
    a socket.

    Use it as a context manager, or call :meth:`stop` when done::

        with MeshServer() as mesh:
            webbrowser.open(mesh.url)

    With no ``path``, this serves :func:`ledger_path`'s default -- this
    MACHINE's own peer ledger, the same file ``/mesh`` with no argument
    serves. Every other caller in this codebase (``/mesh <run-id>``, the
    fleet tab, a test) has a specific ledger in mind and passes ``path=``
    explicitly rather than relying on the default; do the same unless
    "this machine's own traffic" is actually what is wanted.
    """

    def __init__(
        self,
        path: "Path | None" = None,
        host: str = "127.0.0.1",
        port: int = 0,
        token: "str | None" = None,
    ) -> None:
        # Validate BEFORE any socket work: a rejected host must never
        # reach a bind() call, even one that would fail anyway.
        self.host = require_loopback(host)
        self.path = Path(path) if path is not None else ledger_path()
        # 24 bytes of urlsafe entropy, minted per server and held only in
        # memory -- never written to disk, where it would outlive the
        # process that needed it. beliefgraph's reasoning exactly.
        self.token = token or secrets.token_urlsafe(24)
        self._stopping = threading.Event()
        self._server = self._build(port)
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="doxa-mesh-http", daemon=True
        )
        self._thread.start()

    # -- lifecycle --

    def __enter__(self) -> "MeshServer":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def url(self) -> str:
        """The one URL worth handing a browser: the page, under the token.

        **The token is a PATH segment, not a query parameter**, and that
        differs deliberately from ``doxa.beliefgraph``, which puts its
        own in ``?k=`` so that file resolution stays
        ``SimpleHTTPRequestHandler``'s unmodified -- path traversal is
        not something to reimplement. That reasoning does not apply here
        and the opposite one does: this handler resolves no filesystem
        path from a request at all (:data:`STATIC_FILES` is an exact-name
        allow-list), while the page has to load ``mesh.js``, ``mesh.css``,
        ``ledger`` and ``events`` as RELATIVE urls. Relative to a query
        token they resolve to ``/mesh.js`` and lose it; relative to a
        path token they stay inside ``/<token>/`` and carry it for free.

        The alternative was a cookie, and it is worse: cookies are scoped
        by host and NOT by port, so a cookie minted here would be
        attached to requests to any other local server the browser
        happens to visit on 127.0.0.1."""
        return f"http://{self.host}:{self.port}/{self.token}/"

    def stop(self) -> None:
        """Shut down and release the port. Idempotent.

        The stop event is set FIRST so an open SSE loop notices on its
        next poll and returns; ``shutdown()`` alone stops the accept loop
        but would wait on a handler thread still sitting in a stream that
        by design never ends."""
        self._stopping.set()
        try:
            self._server.shutdown()
            self._server.server_close()
        except Exception:  # noqa: BLE001 -- teardown is best-effort
            pass

    # -- routing --

    def _build(self, port: int):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from urllib.parse import parse_qs, urlparse

        mesh = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            # The stock handler logs every request to stderr. DOXA is a
            # full-screen Textual app and that is a line drawn over the
            # UI -- beliefgraph silences it for the same reason.
            def log_message(self, *_args: object) -> None:
                return

            def do_GET(self) -> None:  # noqa: N802 -- BaseHTTPRequestHandler's name
                parsed = urlparse(self.path)
                query = parse_qs(parsed.query)

                # "/<token>/<route>", split once so a route may never
                # smuggle a second segment past the check.
                supplied, _, route = parsed.path.lstrip("/").partition("/")

                # 404 rather than 401/403, and before the route is even
                # looked at: a probe of the port learns neither that this
                # is DOXA nor that a valid token exists. There is nothing
                # here to authenticate INTO -- the token is the whole
                # capability. compare_digest, not ==, so the check does
                # not leak its progress through timing.
                #
                # BYTES on both sides, not str. compare_digest accepts str
                # only when BOTH arguments are ASCII and raises TypeError
                # otherwise; http.server decodes the request line as
                # iso-8859-1, so a raw 0xE9 byte in the path -- no
                # percent-encoding needed -- put a non-ASCII str here, the
                # handler raised before answering, and the traceback
                # printed over the Textual UI. The encoding is total, so
                # every request now gets the same 404 a wrong token gets.
                if not secrets.compare_digest(
                    supplied.encode("utf-8", "surrogateescape"),
                    mesh.token.encode("utf-8", "surrogateescape"),
                ):
                    self.send_error(404)
                    return

                # "/<token>" without the trailing slash would serve the
                # page against a base path of "/", so every relative url
                # in it would resolve to "/mesh.js" -- outside the token
                # and therefore 404. The page would load and stay blank.
                # Redirect instead of guessing.
                if not route and not parsed.path.endswith("/"):
                    self.send_response(301)
                    self.send_header("Location", f"/{supplied}/")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return

                if route in STATIC_FILES:
                    mesh._serve_static(self, route)
                elif route == "ledger":
                    mesh._serve_ledger(self, query)
                elif route == "events":
                    mesh._serve_events(self, query)
                else:
                    self.send_error(404)

        return ThreadingHTTPServer((self.host, port), Handler)

    # -- responses --

    def _serve_static(self, handler: Any, route: str) -> None:
        name, content_type = STATIC_FILES[route]
        target = assets_dir() / name
        try:
            payload = target.read_bytes()
        except OSError:
            # Naming the path is the difference between "the page is
            # broken" and "the wheel did not carry assets/mesh" -- see
            # assets_dir()'s packaging note.
            handler.send_error(404, f"missing page asset: {target}")
            return
        handler.send_response(200)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(payload)))
        # The page loads nothing from anywhere else and must not be
        # framed by anything either. Written as headers rather than a
        # <meta> tag so they also cover the JSON routes.
        handler.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; base-uri 'none'; "
            "form-action 'none'; frame-ancestors 'none'",
        )
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.send_header("Referrer-Policy", "no-referrer")
        handler.end_headers()
        handler.wfile.write(payload)

    def _serve_ledger(self, handler: Any, query: "dict[str, list[str]]") -> None:
        """Everything so far, plus the offset to open the stream at.

        The offset is the point: the page calls this, then opens
        ``/events?from=<offset>``. Anything appended between the two
        requests sits after that offset and arrives on the stream, and
        nothing already in this response can arrive twice."""
        offset = _int_param(query, "from", 0)
        records, next_offset = read_records(self.path, offset)
        body = json_bytes({"records": records, "offset": next_offset})
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.end_headers()
        handler.wfile.write(body)

    def _serve_events(self, handler: Any, query: "dict[str, list[str]]") -> None:
        """Server-sent events: records appended after ``?from=``.

        **Default is end-of-file, not zero.** A stream opened with no
        cursor sends what happens NEXT and never replays history -- the
        page has already fetched history from ``/ledger``, and a stream
        that re-sent it would double every edge on the canvas.
        ``?from=0`` is still available and means "replay everything",
        which is what a reader who opened the page mid-run wants.

        ``Last-Event-ID`` WINS OVER ``?from=``, because it has to.
        ``EventSource`` reconnects to the URL it was constructed with --
        the page cannot rewrite it -- so a dropped connection would
        otherwise resume at the page's ORIGINAL cursor and replay every
        record since. The browser sends this header with the id of the
        last event it dispatched, which is exactly the right place to
        resume, and honouring it is what makes a reconnect free of both
        gaps and duplicates."""
        resumed = handler.headers.get("Last-Event-ID", "")
        if resumed.strip().isdigit():
            offset = max(0, int(resumed.strip()))
        else:
            offset = _int_param(query, "from", _file_size(self.path))

        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("X-Content-Type-Options", "nosniff")
        # SSE is a stream of unknown length: no Content-Length, and no
        # keep-alive reuse of this connection afterwards.
        handler.send_header("Connection", "close")
        handler.end_headers()

        last_beat = time.monotonic()
        try:
            # An opening comment flushes headers immediately, so the page's
            # `onopen` fires now rather than whenever the first record
            # happens to arrive -- which on a quiet fleet could be never,
            # and would read as "the view is broken".
            handler.wfile.write(b": open\n\n")
            handler.wfile.flush()
            while not self._stopping.is_set():
                found, offset = read_batch(self.path, offset)
                for record, position in found:
                    payload = json_bytes(record).decode("utf-8")
                    # The id is THIS record's end offset, not the batch's
                    # -- see read_batch on why a reconnect depends on it.
                    frame = f"id: {position}\ndata: {payload}\n\n".encode("utf-8")
                    handler.wfile.write(frame)
                if found:
                    handler.wfile.flush()
                    last_beat = time.monotonic()
                elif time.monotonic() - last_beat >= HEARTBEAT_SECS:
                    handler.wfile.write(b": beat\n\n")
                    handler.wfile.flush()
                    last_beat = time.monotonic()
                self._stopping.wait(POLL_SECS)
        except (BrokenPipeError, ConnectionResetError, OSError, ValueError):
            # The reader closed the tab. Not an error, and above all not a
            # traceback on the terminal DOXA is drawing on.
            return


def json_bytes(payload: Any) -> bytes:
    """JSON with ``<``, ``>`` and ``&`` escaped to their ``\\uXXXX`` form.

    Those three are not JSON syntax -- they can only ever occur inside a
    string value -- so replacing them wholesale is safe, and a parser
    decodes the result to the identical string. What it buys is that the
    bytes this server emits **never contain a markup-looking sequence at
    all**, whatever a message body holds.

    Strictly, this is belt and braces: the response is
    ``application/json`` with ``nosniff``, the page is static, and every
    body reaches the screen through ``textContent`` or canvas
    ``fillText``. But the instruction this view is built to satisfy is to
    assume a body contains a script tag and make that harmless, and the
    cheapest way to be sure is for the dangerous characters never to
    survive serialization in the first place. It costs one string pass
    and removes a whole class of "what if this JSON is ever rendered
    somewhere else" from consideration."""
    return (
        json.dumps(payload, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .encode("utf-8")
    )


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _int_param(query: "dict[str, list[str]]", name: str, default: int) -> int:
    """A non-negative int from the query string, or ``default``.

    Clamped rather than rejected: a junk cursor should reopen the view at
    a sane place, not 400 a page that is trying to reconnect."""
    try:
        value = int(query.get(name, [""])[0])
    except (TypeError, ValueError):
        return default
    return max(0, value)


def serve(path: "Path | None" = None, open_browser: bool = True) -> MeshServer:
    """Start the view and (by default) open it. The caller owns
    :meth:`MeshServer.stop`."""
    mesh = MeshServer(path=path)
    if open_browser:
        import webbrowser

        webbrowser.open(mesh.url)
    return mesh

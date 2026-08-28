# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.beliefgraph -- ONE belief's graph neighbourhood, two ways.

The beliefs picker's ``g`` row action (see
``doxa.session.chips.BELIEF_GRAPH_ROW_ACTION``) lands here. What it draws
is the neighbourhood of the ONE belief the highlight is sitting on, and
that scoping is the whole design rather than a first cut at a whole-graph
view:

    A whole-graph view was built first, filtered to asserted relations,
    and MEASURED: 63 edges over 104 beliefs resolved to 44 disconnected
    clusters, which mermaid stacks vertically -- 1188x13814 pixels,
    aspect 0.09, fitting on screen at 5% and unreadable at every zoom
    above it. ``khop`` from a single belief is connected by construction
    and never fragments. So the picker row -- which already names one
    belief -- is the right and only home for this, and there is
    deliberately no whole-graph view to reach from anywhere in DOXA.

Two renderings, chosen by the ``graph_view`` setting (``DOXA_GRAPH_VIEW``,
see ``doxa.config``):

``ascii``
    ``lore_core.beliefs.format_edges``, already formatted, inserted as
    rows directly beneath the belief -- the same fold the evidence trail
    uses on Right (``ChipPicker.expand_rows``). Nothing is rendered here;
    LORE's own edge block IS the output.

``browser`` (default)
    ``lore_core.graph``'s mermaid page, written to DOXA's own state home
    and opened. Every part of that page -- drag-to-pan, wheel-to-zoom,
    double-click-to-fit, fit-on-load, and the self-describing failure
    text when mermaid cannot be fetched -- is ``render_html``'s, not
    DOXA's. There is no template here to keep in step with LORE's.

THREE THINGS THIS MODULE DOES NOT DO, each because LORE already does it:
it does not build mermaid source, it does not format an edge block, and
it does not traverse. It selects a belief, decides which of the two
renderings the user asked for, and puts the result somewhere a reader can
see it.

WHERE THE PAGE LANDS: ``$DOXA_HOME/graphs`` (``~/.doxa/graphs``), which is
DOXA's durable state home -- deliberately NOT ``LORE_ROOT``. The belief
store is a store the Claude Code LORE plugin shares; a rendered artifact
of DOXA's UI is not part of it, and writing one there would put DOXA's
scratch output inside the one directory whose contents are supposed to be
memory. The path is also PRINTED into the transcript on every open, so a
headless box, an SSH session or a machine with no browser at all still
ends up with something to scp.

HTTP RATHER THAN ``file://`` WHEN IT CAN. ``render_html``'s page fetches
mermaid from ``cdn.jsdelivr.net`` as an ES module on first open, and a
``file://`` document is a null origin that some browsers refuse a module
fetch from outright -- a page that loads, throws nothing, and draws
nothing. :func:`page_url` therefore serves the graphs directory over a
loopback-only HTTP server (127.0.0.1, ephemeral port, this one directory,
started on first use) and hands back an ``http://`` URL, falling back to
``file://`` when that cannot be started. Either way the page states its
own case: LORE's template explains the null-origin refusal itself if the
fetch never resolves.

CAPABILITY IS MEASURED OFF THE API, NEVER OFF A VERSION -- the same rule
``doxa.engine.belief_action_state`` states, and for a sharper reason here.
``doxa._lore_bootstrap`` prefers a plugin CHECKOUT over the pinned wheel,
so the ``lore_core`` this process loaded can be OLDER than
``pyproject.toml``'s pin (an operator running a stale plugin) as easily as
newer. :func:`graph_state` asks whether the functions are there to call,
per rendering, and the action reports what is missing rather than raising.
"""

from __future__ import annotations

import os
import threading
import webbrowser
from contextlib import closing
from pathlib import Path
from urllib.parse import quote
from typing import Any  # noqa: F401 -- annotation-only

from . import config as config_mod

#: How far :func:`write_page` walks from the seed belief. Two hops is the
#: neighbourhood a reader can still take in at a glance; ``khop`` is
#: connected by construction at any depth, so this is a legibility
#: budget, not a correctness one.
GRAPH_DEPTH = 2

#: What the ascii rendering says for the COMMON case, which is very
#: nearly the ONLY case. Measured on the live store this was built
#: against: 745 of 799 active beliefs (93%) have no row in
#: ``belief_edges`` at all, and 776 of 799 (97%) have no ASSERTED one --
#: the store holds 121 structural edges against 13 derived. Hide-at-zero,
#: the same rule the [BELIEF GRAPH] prompt block and the ctx chip already
#: follow: say it in one line rather than open an empty page for nine
#: beliefs in ten.
NO_RELATIONS = "  (no relations recorded)"

MODES = ("browser", "ascii")


def graph_view_mode() -> str:
    """``"browser"`` or ``"ascii"`` -- ``DOXA_GRAPH_VIEW`` / the config
    file's ``graph_view`` row, through the one precedence every other knob
    uses.

    An unset or unrecognized value reads as ``browser``, the same
    fall-back-rather-than-crash posture :func:`doxa.config.background_mode`
    applies to a hand-edited file or a typo'd env var."""
    value = config_mod.raw("DOXA_GRAPH_VIEW").strip().lower()
    return value if value in MODES else "browser"


def graph_dir() -> Path:
    """Where rendered pages land: DOXA's own state home, never
    ``LORE_ROOT``. Created on demand and clamped to 0700 like every other
    directory under ``$DOXA_HOME``."""
    path = config_mod.doxa_home() / "graphs"
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


# -- capability: what this lore_core can actually draw --------------------


def graph_state(mode: str = "") -> dict:
    """Whether the loaded ``lore_core`` can render ``mode``, and if not,
    which functions are missing.

    Per MODE rather than one blanket answer, because the two renderings
    need different halves of LORE's API and a checkout that has one half
    should not lose the other: ``ascii`` needs only
    ``lore_core.beliefs.format_edges``, while ``browser`` also needs
    ``lore_core.graph``'s ``adjacency``/``khop``/``mermaid_source``/
    ``render_html``. An empty ``mode`` asks about both.

    Measured off the API. ``doxa._lore_bootstrap`` prefers a plugin
    checkout over the pinned wheel, so the version in ``pyproject.toml``
    is a floor for a bare clone and says nothing about the copy actually
    imported -- which can be older. A version comparison here would
    refuse a perfectly renderable graph on a newer plugin and, worse,
    promise one on an older one."""
    from . import _lore_bootstrap
    from . import version as version_mod

    version = version_mod.lore_core_version()
    source = _lore_bootstrap.resolved_source()
    where = f"{source[0]} at {source[1]}" if source else "unknown source"
    state = {
        "capable": False, "version": version,
        "source": source[0] if source else None, "missing": [], "reason": "",
    }
    wanted: "list[tuple[str, str]]" = [("lore_core.beliefs", "format_edges")]
    if mode != "ascii":
        wanted += [("lore_core.graph", name) for name in
                   ("adjacency", "khop", "mermaid_source", "render_html")]
    missing: "list[str]" = []
    for module_name, func_name in wanted:
        try:
            module = __import__(module_name, fromlist=[func_name])
            func = getattr(module, func_name, None)
        except Exception:  # noqa: BLE001 -- an absent API is a reason, not a crash
            func = None
        if not callable(func):
            missing.append(f"{module_name.split('.')[-1]}.{func_name}")
    if missing:
        state["missing"] = missing
        state["reason"] = (
            f"lore_core {version or 'of unknown version'} ({where}) is missing "
            f"{', '.join(missing)} — DOXA draws LORE's own graph rather than "
            "reimplementing one, so there is nothing here to draw with. "
            "Upgrade the copy DOXA loaded (/about's 'lore from' row says "
            "which one that is)."
        )
        return state
    state["capable"] = True
    return state


# -- the two renderings ---------------------------------------------------


def _connect():
    """A store connection, CLOSED by the caller -- always via
    ``contextlib.closing``, never left to the garbage collector.

    ``lore_core.store.db_connect`` hands back a fresh ``sqlite3``
    connection per call (and one that may only be used on the thread that
    opened it), so every entry point here opens its own and closes it
    before returning. Note that ``with conn:`` would NOT do this -- a
    sqlite3 connection's context manager commits or rolls back a
    transaction and leaves the handle open, which is the easy mistake to
    make here. These functions run on a keystroke, so an un-closed handle
    per press is a file descriptor per press."""
    from lore_core.store import db_connect

    return db_connect()


def edge_block(bid: int) -> str:
    """``format_edges``' own block for one belief, or ``""`` when it has
    no rows in ``belief_edges`` at all.

    ONE STORE READ, AND ONE GATE FOR BOTH RENDERINGS. Emptiness here is
    what both ``ascii`` and ``browser`` mean by "no relations recorded",
    so the two can never disagree about whether a belief has anything to
    show -- and the caller branches on the string it already fetched
    rather than asking a second time.

    Asked of ``format_edges`` -- rows in LORE's own ``belief_edges``
    table -- rather than of ``adjacency``, which folds in ``co_derived``
    by projection. A co-derivation is computed at read time from shared
    evidence and is never an asserted relation; drawing a page whose only
    content was a projection would answer "what does this belief relate
    to" with something nobody recorded.

    Blocking (SQLite): call it off the UI thread."""
    from lore_core.beliefs import format_edges

    with closing(_connect()) as conn:
        return format_edges(conn, int(bid))


def edge_lines(block: str) -> "list[str]":
    """The ``ascii`` rendering: :func:`edge_block`'s output split into one
    row per line, so the picker inserts each as its own option -- never
    one joined blob, the same shape the evidence trail already hands back.

    An empty block becomes :data:`NO_RELATIONS` rather than an empty
    expansion, which a reader cannot tell from a fetch that silently
    failed. Pure: no store access, nothing to run off-thread."""
    return block.splitlines() if block.strip() else [NO_RELATIONS]


def write_page(bid: int, out: "Path | None" = None,
               depth: int = GRAPH_DEPTH) -> "tuple[Path, str]":
    """The ``browser`` rendering: LORE's mermaid page for the ``depth``-hop
    neighbourhood of ``bid``, written to ``out`` (default: this belief's
    file under :func:`graph_dir`). Returns ``(path, note)`` -- the note is
    the one-line summary the page's own header carries.

    Raises ``KeyError(bid)`` when the belief is not in the ACTIVE graph
    (retracted, superseded, dormant, or gone). Callers report that; it is
    not an error condition so much as an answer.

    Every rendering decision below the mermaid source belongs to
    ``render_html``: this writes the string LORE returns, unmodified.

    Blocking, and not trivially so -- ``adjacency`` builds the WHOLE
    store's adjacency before ``khop`` walks two steps of it, which on a
    store of several hundred beliefs is a scan plus the ``co_derived``
    projection over ``belief_evidence``. Call it off the UI thread."""
    from lore_core.graph import adjacency, khop, mermaid_source, render_html

    bid = int(bid)
    with closing(_connect()) as conn:
        adj, claims = adjacency(conn)
        if bid not in claims:
            raise KeyError(bid)
        nodes = sorted(khop(adj, bid, depth))
        subjects = dict(conn.execute(
            "SELECT id, subject FROM beliefs WHERE id IN (%s)"
            % ",".join("?" * len(nodes)), nodes))
    src = mermaid_source(adj, claims, nodes, subjects=subjects)
    note = f"belief {bid}, {depth} hop(s) · {len(nodes)} belief(s)"
    path = Path(out) if out is not None else graph_dir() / f"belief-{bid}.html"
    path.write_text(render_html(src, f"belief {bid}", note), encoding="utf-8")
    return path, note


# -- getting the page in front of a reader --------------------------------
#
# A loopback HTTP server rather than a bare file:// URL, for a measured
# reason: render_html's page imports mermaid from cdn.jsdelivr.net as an
# ES module, and a file:// document is a null origin that some browsers
# refuse a module fetch from -- producing a page with no console error and
# no diagram, which is the hardest kind of failure to read. Serving the
# graphs directory over 127.0.0.1 gives the page a real origin.
#
# Scope, deliberately narrow: bound to the loopback interface only, on an
# ephemeral port, serving exactly graph_dir() and nothing above it, in a
# daemon thread that dies with the process. It holds no state and answers
# only GETs for files DOXA itself wrote. It is started LAZILY -- a DOXA
# that never opens a graph never opens a socket -- and a failure to start
# is not an error: page_url falls back to file://, where LORE's template
# explains the null-origin case in the page itself.
#
# AND IT REQUIRES A TOKEN, because "loopback" is not "this user". The
# graphs directory is 0700, so a co-tenant on a shared machine cannot read
# a rendered page off disk -- but an HTTP server on 127.0.0.1 answers any
# LOCAL process regardless of whose it is, and 65k ports is not a secret.
# Without this, adding the server would have widened access to the very
# thing DOXA exists to keep auditable: the user's own belief claims, which
# are in the page in full. So every request carries a per-process token
# (`?k=`), minted once at startup and never written to disk; anything else
# gets 404, which is also what a probe of the port with no token sees.
# The token rides in the QUERY rather than the path so the file resolution
# below stays SimpleHTTPRequestHandler's own, unmodified -- including its
# path-traversal handling, which is not something to reimplement here.

_SERVER_LOCK = threading.Lock()
#: The per-process capability every served request must carry. Minted
#: lazily on first use, held only in memory, and never part of a path --
#: see the note above on why the server needs one at all.
_TOKEN: "str | None" = None

#: ``(server, base url, directory it was rooted at)``. The directory is
#: part of the key, not just a detail: ``graph_dir`` follows ``DOXA_HOME``,
#: which a test moves and a relaunched process can differ on, and a cached
#: server still rooted at the OLD directory would answer 404 for a page
#: that is plainly on disk -- the worst kind of wrong, because the file
#: the transcript names really is there.
_SERVER: "tuple[Any, str, str] | None" = None


def _start_server() -> "str | None":
    """The loopback server's base URL, starting it on first call and
    RESTARTING it if :func:`graph_dir` has moved since. None when it
    cannot be started at all."""
    global _SERVER, _TOKEN
    directory = str(graph_dir())
    with _SERVER_LOCK:
        if _SERVER is not None:
            if _SERVER[2] == directory:
                return _SERVER[1]
            stale, _base, _dir = _SERVER
            _SERVER = None
            try:
                stale.shutdown()
                stale.server_close()
            except Exception:  # noqa: BLE001
                pass
        try:
            import functools
            import secrets
            from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
            from urllib.parse import parse_qs, urlparse

            # Minted once per PROCESS, not per server: a restart after
            # DOXA_HOME moves keeps the same capability, and stop_server
            # deliberately does not clear it.
            if _TOKEN is None:
                _TOKEN = secrets.token_urlsafe(24)
            token = _TOKEN

            class _GuardedHandler(SimpleHTTPRequestHandler):
                # The stock handler logs every request to stderr, which in
                # a full-screen Textual app is a line drawn over the UI.
                def log_message(self, *_args) -> None:  # noqa: D102
                    return

                def _authorized(self) -> bool:
                    supplied = parse_qs(urlparse(self.path).query).get("k", [""])[0]
                    return secrets.compare_digest(supplied, token)

                def do_GET(self) -> None:  # noqa: N802 -- BaseHTTPRequestHandler's name
                    # 404 rather than 403: a port probe with no token
                    # learns nothing it did not already know, and there is
                    # nothing here to authenticate INTO.
                    if not self._authorized():
                        self.send_error(404)
                        return
                    super().do_GET()

                def do_HEAD(self) -> None:  # noqa: N802
                    if not self._authorized():
                        self.send_error(404)
                        return
                    super().do_HEAD()

            handler = functools.partial(_GuardedHandler, directory=directory)
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(
                target=server.serve_forever, name="doxa-graph-http", daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            _SERVER = (server, base, directory)
            return base
        except Exception:  # noqa: BLE001 -- no server is a fallback, not a crash
            return None


def stop_server() -> None:
    """Shut the loopback server down. Idempotent, and never required --
    the thread is a daemon -- but a test that starts one should not leave
    it listening for the rest of the session."""
    global _SERVER
    # Shut down INSIDE the lock, not after releasing it: dropping the lock
    # between clearing _SERVER and closing the socket leaves a window in
    # which another thread's page_url sees "no server" and starts a second
    # one, which the shutdown below then does not own. Holding the lock
    # across shutdown() cannot deadlock -- serve_forever's loop takes no
    # lock of ours -- it only makes a concurrent _start_server wait.
    with _SERVER_LOCK:
        if _SERVER is None:
            return
        server, _base, _dir = _SERVER
        _SERVER = None
        try:
            server.shutdown()
            server.server_close()
        except Exception:  # noqa: BLE001
            pass


def page_url(path: Path) -> str:
    """The URL to hand a browser for a page under :func:`graph_dir` --
    ``http://127.0.0.1:PORT/<name>?k=<token>`` when the loopback server is
    up, ``file://`` when it is not. A path outside the graphs directory is
    always ``file://``: this server serves one directory."""
    path = Path(path)
    try:
        name = path.resolve().relative_to(graph_dir().resolve()).as_posix()
    except (ValueError, OSError):
        return path.as_uri()
    base = _start_server()
    if not base or _TOKEN is None:
        return path.as_uri()
    return f"{base}/{quote(name)}?k={_TOKEN}"


def open_url(url: str) -> bool:
    """Hand ``url`` to the desktop browser. False when there is none to
    hand it to -- a headless box, an SSH session, a machine with no
    ``BROWSER`` -- which is not a failure: the caller has already printed
    the file's own path into the transcript, and that path is the whole
    point of printing it."""
    try:
        return bool(webbrowser.open(url))
    except Exception:  # noqa: BLE001 -- no browser is a caveat, not a crash
        return False

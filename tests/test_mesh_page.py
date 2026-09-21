# SPDX-License-Identifier: AGPL-3.0-only
"""``assets/mesh/`` in a real browser, against a real server.

THE ONE THING THIS SUITE DOES THAT NOTHING ELSE COULD. DOXA serves
exactly one page it wrote itself, and until now nothing executed it.
``tests/test_meshgraph.py`` proves a great deal about the SERVER and then
makes two claims about the page by reading it as a string -- that
``mesh.js`` contains no ``innerHTML`` and that the response carries a
content security policy. Both are worth asserting and neither is
evidence that the page works: a grep cannot tell whether a body
containing ``<script>`` is inert, only whether one particular way of
making it live is absent. So this file loads the shipped page in Chrome,
over a real :class:`doxa.meshgraph.MeshServer` on a real ledger, and asks
the page what it did.

HOW THE PAGE IS INTERROGATED, AND WHY IT IS NOT PIXELS.
``assets/mesh/mesh.js`` is a classic script, so its top-level
``const`` bindings (``nodes``, ``pairs``, ``messages``, ``selected``,
``view``) live in the realm's global LEXICAL environment -- not on
``window``, but reachable from any later global evaluation, which is
exactly what ``Runtime.evaluate`` performs. A test can therefore read the
renderer's own model instead of inferring it from a screenshot, and the
assertions are about ids and counts rather than about colours of pixels
that a font change would move.

For what is DRAWN, which has no model to read, the suite wraps the canvas
calls the renderer paints with (``beginPath``, ``moveTo``,
``quadraticCurveTo``, ``arc``, ``stroke``, ``fillText``) and records one
complete frame. That records what ``drawEdges`` and ``drawNodes``
actually emitted -- a broadcast fan, a stroke width, a label that
collision-skipping dropped -- which is the level the interesting claims
live at and the level no string search can reach.

INPUT IS REAL INPUT. Selection and the two checkboxes are driven with
``Input.dispatchMouseEvent`` and the fade slider with
``Input.dispatchKeyEvent``, both of which become the same events a mouse
and a keyboard produce. Nothing here dispatches a synthetic DOM event the
page would never see from a user, and nothing writes the panel's contents
from outside -- otherwise the test would be asserting about its own
writes.

ONE BROWSER FOR THE WHOLE FILE, many pages. Chrome costs about half a
second to start and about a hundredth of one to navigate, so the browser
is session-scoped and each test gets a fresh ledger, a fresh server and a
fresh document instead of a fresh process.

AND THEREFORE NO MARKER: this file runs in the default ``uv run pytest``
and in CI's single unqualified command. Measured on the machine it was
written on, ten runs: 37.4s to 38.2s, and 41.0s under twelve deliberate
CPU burners -- against a suite of about 22 minutes, so roughly a 3%
increase for the only browser surface DOXA has. A marker would have
bought back half a minute and cost the thing that makes this worth
writing: a suite that is only run when somebody remembers to run it does
not catch the edit that breaks the page. Half of that half-minute is one
unavoidable wait, the layout's own cooling schedule, and
:meth:`Page.settle` says where it is spent and why only a click needs to
pay it.

This also matches how the repository already handles a test group that
needs something the machine may not have: ``tests/test_lore_sync.py``
and ``tests/test_engine_picker.py`` both gate on a module-level
``skipif`` and stay in the default run. There is no marker registered in
``pyproject.toml`` and no second pytest invocation anywhere in
``.github/workflows/ci.yml``, and this change adds neither.

THE LEDGER FIXTURES ARE ``scripts/mesh_shot.py``'S. That module already
writes the on-the-wire record shape for the committed gallery asset, and
its tables are now parameters, so a two-session graph and a hostile body
go through the same writer as the nine-session picture. Expected edge
sets come from ``doxa.meshgraph.edges_for`` -- the function the server
itself calls -- so a test and the page cannot disagree about what the
input meant.

WHEN THERE IS NO CHROME, THE EIGHTEEN TESTS THAT NEED ONE SKIP, the way
the Node-based test this file replaces skipped without Node, so a machine
that cannot run a browser stays green. The other eight -- the token-gate
sweep, the response headers and the fixture's own scope -- need no
browser and run everywhere, which is deliberate: the half of each claim
that can still be checked should still be checked. That silence is
dangerous on its own, so ``tests/conftest.py`` prints a marked line in
the terminal summary naming how many were skipped: a run in which the
browser suite did nothing must not read as a run in which it passed.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from doxa import meshgraph
from scripts import cdp, mesh_shot

#: The string ``tests/conftest.py`` looks for to count this file's skips.
#: A sentinel rather than a match on the module name, because a skip is
#: reported as free text and the reason is the only part of it this file
#: controls.
NO_BROWSER = "no browser on this machine (doxa mesh web suite)"

CHROME = cdp.chrome_binary()

requires_chrome = pytest.mark.skipif(CHROME is None, reason=NO_BROWSER)

#: Big enough that the side panel and the canvas both have room, small
#: enough to render fast. Not the gallery's geometry, which is
#: ``scripts/mesh_shot.py``'s business: nothing here is a photograph.
WINDOW = (1200, 780)

#: ``VIEW.restAlphaDirect`` in ``assets/mesh/mesh.js``: the floor an edge
#: fades to rather than disappearing at, because the structure is the
#: point and not just the flash.
REST_ALPHA_DIRECT = 0.16

#: ``assets/mesh/mesh.js``'s own palette, quoted rather than imported
#: because there is nothing to import from -- a JS object literal. The
#: page normalises a canvas colour to lowercase, which is why these are.
DIRECT = "#d97757"
BROADCAST = "#5fb3b3"
UNKNOWN = "#8a8073"

#: What a body must survive being: a script element, an event-handler
#: attribute, a javascript: url and an iframe, all at once. Each one sets
#: a different global, so a test can name WHICH of them ran.
HOSTILE = (
    '<script>window.__pwned = "script"</script>'
    '<img src=x onerror="window.__pwned = \'onerror\'">'
    '<a href="javascript:window.__pwned=\'href\'">click</a>'
    '<iframe srcdoc="<script>parent.__pwned=1</script>"></iframe>'
    "\nand a second line, with an & and a < in it"
)


# =======================================================================
# The browser, the server, and the page between them
# =======================================================================

#: Installed into each fresh document. Two patches, both reversible by
#: navigating away (a new document is a new realm):
#:
#: * the ``CanvasRenderingContext2D`` methods the renderer paints with,
#:   which record what a frame emitted rather than changing it --
#:   every one of them calls through to the real implementation, so the
#:   page draws exactly what it would have drawn;
#: * ``draw`` itself, which is a top-level FUNCTION declaration and
#:   therefore a property of the global object (unlike the ``const``
#:   state above it), so it can be wrapped to mark where one frame ends.
#:
#: ``__want`` is a predicate a test supplies to latch a PARTICULAR frame
#: -- the one holding a broadcast ring, say, which is on screen for 1.4
#: seconds out of a page that lives for minutes. Without it a test would
#: race the animation and fail once in however many runs.
RECORDER = """
(() => {
  const P = CanvasRenderingContext2D.prototype;
  if (!P.__doxaRecorder) {
    P.__doxaRecorder = true;
    const real = {
      beginPath: P.beginPath, moveTo: P.moveTo,
      quadraticCurveTo: P.quadraticCurveTo, arc: P.arc,
      stroke: P.stroke, fillText: P.fillText,
    };
    P.beginPath = function () {
      this.__f = null; this.__t = null; this.__a = null;
      return real.beginPath.call(this);
    };
    P.moveTo = function (x, y) {
      this.__f = [x, y]; return real.moveTo.call(this, x, y);
    };
    P.quadraticCurveTo = function (cx, cy, x, y) {
      this.__t = [x, y]; return real.quadraticCurveTo.call(this, cx, cy, x, y);
    };
    P.arc = function (x, y, r, s, e) {
      this.__a = [x, y, r]; return real.arc.call(this, x, y, r, s, e);
    };
    P.stroke = function () {
      if (window.__cur) window.__cur.strokes.push({
        from: this.__f, to: this.__t, circle: this.__a,
        width: this.lineWidth, colour: String(this.strokeStyle).toLowerCase(),
        alpha: this.globalAlpha,
      });
      return real.stroke.call(this);
    };
    P.fillText = function (text, x, y) {
      if (window.__cur) window.__cur.labels.push(String(text));
      return real.fillText.call(this, text, x, y);
    };
  }
  if (!window.__doxaDraw) {
    window.__doxaDraw = true;
    const real = window.draw;
    window.draw = function () {
      // Nothing is recorded once a frame has been latched: an armed
      // recorder that kept running would cost a snapshot per frame for
      // as long as the page is open, and there is nothing left to catch.
      const recording = !window.__hit;
      if (recording) window.__cur = { strokes: [], labels: [] };
      const out = real.apply(this, arguments);
      const frame = window.__cur;
      window.__cur = null;
      if (recording && frame) {
        // The positions AS OF THIS FRAME, not as of whenever the frame is
        // read back. A record arriving wakes the layout, so a node the
        // ring was drawn around has moved by the time anyone asks -- and
        // the stroke would then match no node at all.
        frame.nodes = Array.from(nodes.values()).map(n => [n.id, n.x, n.y]);
        if (!window.__want || window.__want(frame)) window.__hit = frame;
      }
      return out;
    };
  }
  return true;
})()
"""


def _guard(expression: str) -> str:
    """An expression that answers ``false`` instead of throwing.

    Every wait in this file polls across a navigation, and on the far
    side of one ``nodes`` does not exist yet. A ReferenceError there is
    not information -- the page simply has not booted -- so a poll must
    be able to ask the question before the answer can exist."""
    return f"(() => {{ try {{ return ({expression}); }} catch (e) {{ return false; }} }})()"


class Page:
    """One loaded mesh page, and the questions worth asking it."""

    def __init__(self, chrome: cdp.Chrome, server: meshgraph.MeshServer) -> None:
        self.chrome = chrome
        self.server = server

    # -- asking --

    def ask(self, expression: str):
        return self.chrome.ask(expression)

    def until(self, expression: str, what: str, timeout: float = 15.0) -> None:
        self.chrome.until(_guard(expression), what, timeout=timeout)

    def text(self, element_id: str) -> str:
        return self.ask(
            f"(document.getElementById({element_id!r}).textContent || '').trim()"
        )

    def shows(self, element_id: str, value: str) -> None:
        """Assert an element's text, allowing for the frame it lands on.

        Every number in the header is written by ``renderStats`` and
        every one in the panel by ``renderPanel``, and both run on the
        next animation frame after the state they describe changed --
        ``statsDirty`` and ``feedDirty`` are flags the frame loop reads,
        not writes the ingest performs. Reading straight after the state
        is therefore a race that passes on an idle machine and fails on a
        busy one, which is precisely the kind of test that gets rerun
        instead of fixed. Caught here as exactly that: `stat-nodes` was
        read one frame early and said 2 where the graph already had 3,
        once in five runs, and only while the machine was loaded."""
        try:
            self.until(
                f"document.getElementById({element_id!r}).textContent.trim()"
                f" === {value!r}",
                f"#{element_id} showing {value!r}",
                timeout=5.0,
            )
        except cdp.CdpError:
            raise AssertionError(
                f"#{element_id} shows {self.text(element_id)!r}, not {value!r}"
            ) from None

    def json(self, expression: str):
        return json.loads(self.ask(f"JSON.stringify({expression})"))

    def rect(self, element_id: str) -> "dict":
        return self.json(
            f"document.getElementById({element_id!r}).getBoundingClientRect()"
        )

    # -- acting, the way a user does --

    def click_element(self, element_id: str) -> None:
        box = self.rect(element_id)
        self.chrome.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)

    def click_node(self, session: str) -> None:
        """A real pointer press on wherever the page has put a node.

        The graph has no DOM, so there is no element to click and no
        coordinate to be told: the position is read out of the renderer's
        own model through its own view transform, which is the inverse of
        the ``toWorld`` the page hit-tests with -- in ONE evaluation, so
        the answer cannot describe two different moments. The layout is
        settled first, or the node would have moved between the question
        and the press."""
        self.settle()
        at = self.json(f"""(() => {{
          const node = nodes.get({session!r});
          if (!node) return null;
          const box = document.getElementById('graph').getBoundingClientRect();
          return {{x: box.left + view.x + node.x * view.k,
                   y: box.top + view.y + node.y * view.k}};
        }})()""")
        assert at is not None, f"no node for session {session!r}"
        self.chrome.click(at["x"], at["y"])
        self.until(f"selected === {session!r}", f"{session!r} selected")
        # `selected` is assigned in the pointerdown handler; the panel is
        # written on the next animation frame. Returning between the two
        # would hand every caller a panel that is one frame stale -- which
        # is a race that passes locally and fails under load.
        self.until(
            "!document.getElementById('panel').classList.contains('is-empty')",
            "the panel to repaint on the selection",
        )

    def unhover(self) -> None:
        """Move the pointer to an empty corner of the canvas.

        A hovered node is FORCED to draw its label and its repository
        line, which would make a label count depend on where the last
        click happened to leave the mouse. It has to be INSIDE the canvas:
        the hover handler is the canvas's own, so a pointer parked on the
        header never tells the page it left and the last node stays
        hovered forever. ``fit()`` keeps a 90-unit margin, so a corner is
        empty."""
        box = self.rect("graph")
        self.chrome.hover(box["left"] + 3, box["top"] + 3)
        self.until("hovered === null", "nothing hovered")

    def press_in(self, element_id: str, key: str) -> None:
        self.ask(f"document.getElementById({element_id!r}).focus()")
        self.chrome.press(key)

    # -- waiting --

    def settle(self, timeout: float = 20.0) -> None:
        """Hold until the force layout has stopped moving.

        Called from :meth:`click_node` and almost nowhere else, because
        it is the expensive wait in this file -- the layout's own cooling
        schedule takes about three seconds -- and only a COORDINATE needs
        it. A recorded frame carries the positions it was drawn from, so
        every assertion about what was drawn reads a moving layout
        perfectly well; a press at a position read a frame ago does not,
        because a node may move up to a whole temperature in one frame.

        ``energy`` is the renderer's own measure and ``SIM.sleepBelow``
        its own threshold -- below it ``frame()`` stops calling
        ``step()`` and the positions are frozen, which is what makes a
        recorded frame comparable with a later read of ``nodes``. A graph
        of fewer than two nodes never updates ``energy`` at all (``step``
        returns early), and has nothing to settle."""
        self.until(
            "nodes.size < 2 || energy <= SIM.sleepBelow", "a settled layout",
            timeout=timeout,
        )

    # -- what one frame drew --

    def arm(self, want: str = "null") -> None:
        """Install the recorder and start looking for a frame that
        satisfies ``want`` -- a JavaScript predicate over the frame, or
        ``null`` for the very next one.

        Separate from :meth:`latched` because a test after the frame that
        holds a 1.4-second animation has to be armed BEFORE the thing it
        is waiting for is caused."""
        self.ask(RECORDER)
        self.ask(f"window.__hit = null; window.__want = {want};")

    def latched(self, timeout: float = 10.0) -> "Frame":
        self.until("window.__hit !== null", "a recorded frame", timeout=timeout)
        return Frame(self.json("window.__hit"))

    def frame(self, want: str = "null", timeout: float = 10.0) -> "Frame":
        self.arm(want)
        return self.latched(timeout=timeout)

    # -- what the browser complained about --

    def faults(self) -> "list[str]":
        """Every error the page reported since the last call: an uncaught
        exception, a console error, or a browser-level log entry such as
        a policy violation or a failed subresource.

        Drained rather than read, so a test that provokes one on purpose
        can consume it and the fixture's own check stays meaningful."""
        found = []
        for event in self.chrome.take("Runtime.exceptionThrown"):
            details = event["params"]["exceptionDetails"]
            found.append("uncaught: " + details.get("text", "?"))
        for event in self.chrome.take("Runtime.consoleAPICalled"):
            if event["params"].get("type") in ("error", "assert"):
                found.append("console: " + json.dumps(event["params"].get("args")))
        for event in self.chrome.take("Log.entryAdded"):
            entry = event["params"]["entry"]
            if entry.get("level") == "error":
                found.append(f"{entry.get('source')}: {entry.get('text')}")
        return found


class Frame:
    """One painted frame, as ids rather than coordinates.

    The renderer strokes in world space, and ``nodes`` holds the same
    world coordinates, so a stroke is matched back to the pair it drew by
    its endpoints. Exact equality is correct here: both sides are the
    same JavaScript numbers, read out through the same serializer."""

    def __init__(self, raw: "dict") -> None:
        self.labels: "list[str]" = raw["labels"]
        self._at = {(round(x, 6), round(y, 6)): node for node, x, y in raw["nodes"]}
        self.edges: "list[dict]" = []
        self.circles: "list[dict]" = []
        for stroke in raw["strokes"]:
            if stroke["to"] and stroke["from"]:
                self.edges.append({
                    "from": self._id(stroke["from"]),
                    "to": self._id(stroke["to"]),
                    "width": stroke["width"],
                    "colour": stroke["colour"],
                    "alpha": stroke["alpha"],
                })
            elif stroke["circle"]:
                self.circles.append({
                    "at": self._id(stroke["circle"][:2]),
                    "radius": stroke["circle"][2],
                    "colour": stroke["colour"],
                    "width": stroke["width"],
                })

    def _id(self, point) -> "str | None":
        return self._at.get((round(point[0], 6), round(point[1], 6)))

    def pairs(self) -> "set[tuple[str, str]]":
        return {(e["from"], e["to"]) for e in self.edges}

    def edge(self, sender: str, target: str) -> "dict":
        for candidate in self.edges:
            if candidate["from"] == sender and candidate["to"] == target:
                return candidate
        # `str()` around each end, because an endpoint that matched no
        # node is None -- which is itself a failure worth reporting, and
        # would otherwise make this diagnostic raise a TypeError instead
        # of printing.
        raise AssertionError(
            f"no edge {sender[:8]}->{target[:8]} was drawn; drawn: "
            f"{sorted((str(a)[:8], str(b)[:8]) for a, b in self.pairs())}"
        )


@pytest.fixture(scope="session")
def browser():
    """One headless Chrome for every test in this file.

    Session-scoped because launching is about half a second and
    navigating about a hundredth of one: eighteen launches would put ten
    seconds of process start-up on a file that takes thirty-eight, for
    nothing. Nothing survives a navigation anyway -- a new document is a
    new realm, so no test can leave state in another's page."""
    if CHROME is None:
        pytest.skip(NO_BROWSER)
    root = Path(tempfile.mkdtemp(prefix="doxa-mesh-web-"))
    chrome = cdp.Chrome(root / "profile", window=WINDOW, scale=1, binary=CHROME)
    # Browser-level log entries -- policy violations, failed subresources
    # -- arrive on this domain and nowhere else. Runtime is already on.
    chrome.call("Log.enable")
    try:
        yield chrome
    finally:
        chrome.close()
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def ledger(tmp_path) -> Path:
    return tmp_path / "ledger.jsonl"


@pytest.fixture
def open_page(browser, ledger):
    """A factory: write the ledger, then call this to open the page on it.

    A factory rather than a fixture returning a page, because the ledger
    has to exist (or deliberately not) BEFORE the server is started and
    the page fetches it -- which is the order a test controls and a
    fixture cannot guess."""
    started: "list[meshgraph.MeshServer]" = []

    def _open(path: "Path | None" = None) -> Page:
        server = meshgraph.MeshServer(path=ledger if path is None else path)
        started.append(server)
        # Away first, so the poll below cannot be satisfied by the
        # PREVIOUS test's document -- which would still be current for a
        # moment after `navigate` returns, and holds a `nodes` of its own.
        browser.navigate("about:blank")
        browser.until(
            _guard("typeof nodes === 'undefined'"), "the previous page to go"
        )
        browser.take()  # nothing before this point is this test's fault
        browser.navigate(server.url)
        page = Page(browser, server)
        # The page's own ready signals, in the order it reaches them: its
        # script has run, the ledger snapshot has been folded in, and the
        # stream behind it is open. The third is reached even on an empty
        # ledger, so every test can wait for it.
        # `!!`, and it is not noise: the wait is evaluated by value over
        # the protocol, and `el.conn` is a DOM element, which comes back
        # as an empty object -- true in the page and false to the poller.
        page.until(
            "typeof nodes !== 'undefined' && !!el.conn", "the page's script"
        )
        page.until(
            "document.getElementById('conn').className === 'conn-live'",
            "the event stream",
        )
        return page

    yield _open

    # Away BEFORE the servers stop, or the page's EventSource would see
    # its connection cut and log a network error the next test would find.
    browser.navigate("about:blank")
    for server in started:
        server.stop()


# =======================================================================
# Fixture ledgers
# =======================================================================

#: The committed gallery ledger: nine sessions, four engines, broadcasts
#: and pairwise traffic. ``scripts/mesh_shot.py``'s own tables.
GALLERY = dict(sessions=mesh_shot._SESSIONS, traffic=mesh_shot._TRAFFIC)


def expected(sessions, traffic, session: str) -> "dict":
    """What the panel must say about ``session``, derived through
    :func:`doxa.meshgraph.edges_for`.

    The server calls that function for every record it serves, so an
    expectation computed with it cannot drift from the input the page was
    given. A second implementation here would only pin what this file
    believes -- which is exactly how issue #60 survived: ``sent`` and
    ``received`` were right, ``peers`` was not, and the number nobody had
    derived independently was the one that was wrong."""
    directed: "set[tuple[str, str]]" = set()
    sent = received = 0
    for _ago, sender, targets, _body in traffic:
        sender_id = sessions[sender][0]
        recipients = (
            [s[0] for i, s in enumerate(sessions) if i != sender]
            if targets is None
            else [sessions[i][0] for i in targets]
        )
        kind = "broadcast" if targets is None else "direct"
        if sender_id == session:
            sent += 1
        for edge in meshgraph.edges_for(sender_id, recipients, kind):
            directed.add((edge["from"], edge["to"]))
            if edge["to"] == session:
                received += 1
    peers = (
        {to for frm, to in directed if frm == session and to != session}
        | {frm for frm, to in directed if to == session and frm != session}
    )
    return {
        "sent": sent,
        "received": received,
        "peers": len(peers),
        "directed_hits": sum(
            1 for frm, to in directed if session in (frm, to)
        ),
    }


def sessions_of(sessions, traffic) -> "set[str]":
    """Every session id the ledger names, as a sender or a recipient --
    which is exactly the set of nodes the page must draw."""
    ids = set()
    for _ago, sender, targets, _body in traffic:
        ids.add(sessions[sender][0])
        if targets is None:
            ids.update(s[0] for i, s in enumerate(sessions) if i != sender)
        else:
            ids.update(sessions[i][0] for i in targets)
    return ids


def tiny(count: int = 4) -> "list[tuple[str, str, str, str]]":
    """``count`` sessions, all on one engine.

    One engine on purpose where a test looks at colour: the page rims a
    node in its engine's colour, and codex's is the same teal a broadcast
    edge is drawn in -- so a mixed-engine fixture would make "is this
    circle a broadcast ring or a node rim" unanswerable."""
    return [
        (f"{i:012x}", f"session {i}", f"/home/you/repo/r{i}", "claude")
        for i in range(count)
    ]


# =======================================================================
# 1. the security invariant, at runtime
# =======================================================================


@requires_chrome
def test_a_hostile_body_is_drawn_as_letters_and_creates_no_element(
    ledger, open_page
):
    """The claim ``doxa/meshgraph.py`` and ``assets/mesh/index.html`` both
    make in their docstrings, executed rather than grepped.

    ``tests/test_meshgraph.py`` proves the body survives the JSON
    boundary and that ``mesh.js`` contains no HTML sink. Neither is proof
    that the rendered page is inert: a sink that arrives by another name,
    a framework that interprets text, or a future ``srcdoc`` would pass
    both and still execute. So the body is put through the real server
    into the real page, the panel is opened on it with a real click, and
    the question is asked of the DOM that resulted -- did any element
    come into existence, and did anything in that string run."""
    sessions = tiny(2)
    mesh_shot.write_ledger(
        ledger, sessions=sessions, running=set(),
        traffic=[(5.0, 0, [1], HOSTILE)],
    )

    page = open_page()
    page.until("messages.length === 1", "the record")
    page.click_node(sessions[0][0])
    page.until("document.getElementById('sel-feed').children.length === 1", "the feed")

    # The body reached the screen, whole, as text.
    body = page.ask(
        "document.querySelector('#sel-feed .msg-body').textContent"
    )
    assert body == HOSTILE, "the body must be rendered, and rendered intact"

    # Nothing in it ran. Each payload sets a different value, so a failure
    # names which one did.
    assert page.ask("typeof window.__pwned") == "undefined", (
        f"something in the body executed: {page.ask('String(window.__pwned)')}"
    )

    # And nothing in it became an element. The page's own markup has no
    # img, iframe, object, embed or second script, so any of those is
    # necessarily something the body grew into.
    assert page.json(
        "Array.from(document.querySelectorAll("
        "'img, iframe, object, embed, svg, a[href^=\"javascript:\"]'"
        ")).map(e => e.tagName)"
    ) == []
    scripts = page.json(
        "Array.from(document.querySelectorAll('script'))"
        ".map(s => s.getAttribute('src'))"
    )
    assert scripts == ["mesh.js"], (
        f"the page must load exactly its own one script, not {scripts}"
    )

    assert page.faults() == []


@requires_chrome
def test_the_loaded_page_has_no_inline_style_handler_or_script(ledger, open_page):
    """``assets/mesh/index.html`` claims this in a comment -- "No inline
    <style>, no inline style="", no onclick=, no <script> body" -- and a
    comment is not a test.

    Asserted against the LIVE DOM rather than the file, which is the
    stronger statement of the two: it covers what the script added after
    the page loaded as well as what the author typed, and the panel is
    open when it is asked, so the elements built for a message row are in
    scope. The policy the server sends forbids all of these, so an
    inline handler added later would not merely violate a convention --
    it would silently stop working, which is the failure this catches at
    the moment it is introduced rather than in a bug report."""
    mesh_shot.write_ledger(ledger, **GALLERY)
    page = open_page()
    page.until(f"nodes.size === {len(mesh_shot._SESSIONS)}", "every session")
    page.click_node(mesh_shot._SESSIONS[7][0])

    offenders = page.json("""(() => {
      const bad = [];
      for (const node of document.querySelectorAll('*')) {
        for (const attr of node.attributes) {
          if (attr.name === 'style' || attr.name.startsWith('on')) {
            bad.push(node.tagName + '[' + attr.name + ']');
          }
        }
        if (node.tagName === 'STYLE') bad.push('STYLE element');
        if (node.tagName === 'SCRIPT' && !node.getAttribute('src')) {
          bad.push('inline SCRIPT');
        }
      }
      return bad;
    })()""")
    assert offenders == []
    assert page.faults() == []


@requires_chrome
def test_the_policy_the_server_sends_is_enforced_by_the_browser(
    ledger, open_page
):
    """The headers are defence in depth, and depth nobody measured is
    decoration.

    ``tests/test_meshgraph.py`` asserts the policy is in the response.
    This asserts the browser is ACTING on it: an inline script appended
    to the document by the page's own realm is refused, sets nothing, and
    reports a violation naming the directive. That is the mechanism the
    whole "even if a body somehow reached the DOM as markup" argument
    rests on, and it is worth knowing it is switched on rather than
    merely sent."""
    mesh_shot.write_ledger(ledger, sessions=tiny(2),
                           traffic=[(5.0, 0, [1], "hello")], running=set())
    page = open_page()

    page.ask("""(() => {
      window.__violated = null;
      document.addEventListener('securitypolicyviolation',
        (ev) => { window.__violated = ev.violatedDirective; }, { once: true });
      const script = document.createElement('script');
      script.textContent = 'window.__csp = "ran"';
      document.body.appendChild(script);
      script.remove();
    })()""")
    # The report is dispatched as a task rather than synchronously with
    # the insertion, so it is waited for rather than read back.
    page.until("window.__violated !== null", "a reported policy violation")

    assert page.ask("typeof window.__csp") == "undefined", (
        "an inline script ran: the page is not under script-src 'self'"
    )
    assert page.ask("window.__violated").startswith("script-src"), (
        f"the wrong directive refused it: {page.ask('window.__violated')!r}"
    )
    # The violation is this test's own doing, and consuming it is what
    # keeps the check meaningful everywhere else.
    assert any("Content Security Policy" in fault for fault in page.faults())


def test_the_headers_that_carry_the_policy_are_on_every_page_asset(ledger):
    """Every file the page loads, not only the document.

    No browser: the test above proves the policy is enforced, which needs
    one; this proves it is SENT on every asset, which does not -- and on
    a machine with no Chrome it is the half that can still run.

    ``mesh.js`` is where the renderer lives and ``mesh.css`` is what
    positions it; a policy that covered ``index.html`` alone would leave
    the two files a compromise would actually want unprotected. The
    allow-list is read from the server's own table so a fifth file added
    later is covered here the moment it is added there."""
    mesh_shot.write_ledger(ledger, sessions=tiny(2),
                           traffic=[(5.0, 0, [1], "hello")], running=set())
    with meshgraph.MeshServer(path=ledger) as server:
        for route in sorted(meshgraph.STATIC_FILES):
            with urllib.request.urlopen(server.url + route, timeout=5) as res:
                csp = res.headers.get("Content-Security-Policy", "")
                assert "default-src 'none'" in csp, route
                assert "script-src 'self'" in csp, route
                assert "frame-ancestors 'none'" in csp, route
                assert "unsafe-inline" not in csp, route
                assert res.headers.get("X-Content-Type-Options") == "nosniff", route
                assert res.headers.get("Referrer-Policy") == "no-referrer", route


# =======================================================================
# 2. the token gate, on every route
# =======================================================================


@pytest.mark.parametrize(
    "route", sorted(set(meshgraph.STATIC_FILES) | {"ledger", "events"})
)
def test_a_route_answers_inside_the_capability_token_and_nowhere_else(
    ledger, route
):
    """Every route, not just the page.

    The token IS the capability -- there is nothing here to authenticate
    into -- so a route that forgot to be behind it would be the whole
    ledger served to any local process, and "any local process" is the
    reason loopback alone was judged insufficient. Parametrised over the
    server's own route table plus the two dynamic endpoints, so a route
    added without a gate fails here rather than being noticed.

    No browser: this is a property of the server that the browser would
    only observe second-hand, and a test a machine without Chrome should
    still run."""
    mesh_shot.write_ledger(ledger, sessions=tiny(2),
                           traffic=[(5.0, 0, [1], "hello")], running=set())
    with meshgraph.MeshServer(path=ledger) as server:
        base = f"http://{server.host}:{server.port}"

        with urllib.request.urlopen(server.url + route, timeout=5) as res:
            assert res.status == 200, route
            res.close()

        for wrong in (
            f"/{route}",
            f"/wrong-token/{route}",
            f"/{server.token}x/{route}",
            f"/{server.token[:-1]}/{route}",
        ):
            try:
                with urllib.request.urlopen(base + wrong, timeout=5) as res:
                    pytest.fail(f"{wrong} answered {res.status} without the token")
            except urllib.error.HTTPError as exc:
                assert exc.code == 404, wrong


# =======================================================================
# 3. the panel
# =======================================================================


@requires_chrome
def test_selecting_a_session_fills_the_panel_with_its_own_traffic(
    ledger, open_page
):
    """What the panel is for, asserted through a real click on a node
    that exists only on a canvas.

    Every number here is derived from the ledger through
    ``doxa.meshgraph.edges_for`` rather than written down, so the test and
    the server cannot disagree about what the file meant."""
    mesh_shot.write_ledger(ledger, **GALLERY)
    session, title, repo, engine = mesh_shot._SESSIONS[7]
    assert title == mesh_shot._SELECT, "the gallery's own selected session"
    want = expected(mesh_shot._SESSIONS, mesh_shot._TRAFFIC, session)

    page = open_page()
    page.until(f"nodes.size === {len(mesh_shot._SESSIONS)}", "every session")
    assert page.ask("document.getElementById('panel').classList.contains('is-empty')")

    page.click_node(session)

    assert page.text("sel-title") == title
    assert page.text("sel-repo") == repo
    meta = page.text("sel-meta")
    assert engine in meta, meta
    assert "own turn running" in meta, (
        "the sender-side turn state is what the halo draws and the panel "
        "must name whose it is"
    )

    assert page.text("sel-out") == str(want["sent"])
    assert page.text("sel-in") == str(want["received"])
    assert page.text("sel-peers") == str(want["peers"])

    # The feed, including the row that proves a broadcast reads as one
    # thing rather than as a burst: this session's own fan is ONE row,
    # and it says how many it reached.
    rows = page.json(
        "Array.from(document.querySelectorAll('#sel-feed li')).map(li => "
        "({cls: li.className, head: li.querySelector('.msg-head').textContent, "
        "body: li.querySelector('.msg-body').textContent}))"
    )
    assert rows, "the panel opened with no traffic in it"
    broadcasts = [row for row in rows if "is-broadcast" in row["cls"]]
    assert broadcasts, "no broadcast row in the feed"
    assert all("BCAST" in row["head"] for row in broadcasts)
    # "\u2192" is the outgoing marker `messageRow` draws.
    sent_fan = [row for row in broadcasts if "\u2192" in row["head"]]
    assert len(sent_fan) == 1, (
        f"this session broadcast once and the feed shows {len(sent_fan)} "
        f"rows for it"
    )
    assert f"{len(mesh_shot._SESSIONS) - 1} recipients" in sent_fan[0]["head"]
    # The most recent one, by its "seconds ago" alone: sorting the rows
    # whole would compare a broadcast's `None` against a list of targets
    # whenever two of them shared a timestamp.
    newest = min(
        (row for row in mesh_shot._TRAFFIC
         if mesh_shot._SESSIONS[row[1]][0] == session
         or row[2] is None or 7 in row[2]),
        key=lambda row: row[0],
    )
    assert newest[3] in {row["body"] for row in rows}, (
        "the most recent message involving this session is missing from "
        "its own feed"
    )

    assert page.faults() == []


@requires_chrome
def test_the_panel_counts_distinct_peers_not_directed_pairs(ledger, open_page):
    """Issue #60, pinned on the page that had the bug rather than on an
    extract of it.

    ``pairs`` is keyed directionally, so a session that both sent to and
    received from the same peer holds two entries -- and the panel used
    to count entries. On this ledger that reads 15 where the truth is 8,
    and 8 is the honest maximum for a nine-session graph.

    This supersedes the regular-expression extraction that
    ``tests/test_screenshot_driver.py`` carried: that test lifted
    ``renderPanel``'s peer-counting statements out of the file between
    two anchor lines and ran them in Node, which was the right call for
    one bug and could not survive an edit that moved either anchor. The
    page now answers for itself."""
    mesh_shot.write_ledger(ledger, **GALLERY)
    session = mesh_shot._SESSIONS[7][0]
    want = expected(mesh_shot._SESSIONS, mesh_shot._TRAFFIC, session)
    assert want["directed_hits"] == 15 and want["peers"] == 8, (
        f"the fixture ledger changed shape ({want['directed_hits']} directed "
        f"pairs, {want['peers']} peers) -- update this test, README.md's alt "
        f"text and the issue reference together"
    )

    page = open_page()
    page.until(f"nodes.size === {len(mesh_shot._SESSIONS)}", "every session")
    page.click_node(session)

    assert page.text("sel-peers") == "8"
    assert page.text("sel-peers") != "15", "the directed-pair count is back"
    # And the graph agrees with the panel about which peers those are.
    assert page.json(
        "Array.from(pairs.values()).filter(p => p.from === selected "
        "|| p.to === selected).length"
    ) == 15


@requires_chrome
def test_closing_the_panel_clears_the_selection(ledger, open_page):
    """Deselection is a state the page has to get back to cleanly: the
    focus dimming, the forced label and the feed all key off `selected`,
    so a close that left any of them behind would be a graph permanently
    greyed around a session nobody is looking at."""
    mesh_shot.write_ledger(ledger, **GALLERY)
    page = open_page()
    page.until(f"nodes.size === {len(mesh_shot._SESSIONS)}", "every session")
    page.click_node(mesh_shot._SESSIONS[7][0])
    assert not page.ask(
        "document.getElementById('panel').classList.contains('is-empty')"
    )

    page.click_element("panel-close")

    page.until("selected === null", "the selection cleared")
    page.until(
        "document.getElementById('panel').classList.contains('is-empty')",
        "the panel's empty state",
    )
    assert page.ask("document.getElementById('panel-body').hidden") is True
    assert page.faults() == []


# =======================================================================
# 4. the graph
# =======================================================================


@requires_chrome
def test_the_graph_draws_one_node_per_session_in_the_ledger(ledger, open_page):
    """Nodes come from both ends of a record: a session that has only
    ever RECEIVED is still a node, and one that has only ever sent is
    too. Counting senders would quietly lose the first kind."""
    mesh_shot.write_ledger(ledger, **GALLERY)
    want = sessions_of(mesh_shot._SESSIONS, mesh_shot._TRAFFIC)

    page = open_page()
    page.until(f"nodes.size === {len(want)}", "every session")
    assert set(page.json("Array.from(nodes.keys())")) == want
    page.shows("stat-nodes", str(len(want)))
    page.shows("stat-msgs", str(len(mesh_shot._TRAFFIC)))


@requires_chrome
def test_a_broadcast_is_drawn_as_one_fan_not_n_unrelated_deliveries(
    ledger, open_page
):
    """The emergence plan's PRIMARY manipulation, and therefore the one
    distinction this view may not blur.

    One broadcast at N=32 is 31 deliveries, and a view that drew those
    identically to 31 sessions independently choosing to speak could not
    show the thing the experiment measures. The page's answer is a single
    expanding ring from the sender with the spokes drawn faint beneath
    it, so what is asserted is exactly that: one ring, N spokes, all of
    them leaving the same node, in a frame latched while the ring is
    actually on screen.

    The broadcast is APPENDED rather than loaded, because the renderer
    deliberately animates only live traffic -- replaying a thousand
    historical records must not fire a thousand rings."""
    sessions = tiny(5)
    ids = [s[0] for s in sessions]
    mesh_shot.write_ledger(
        ledger, sessions=sessions, traffic=[(30.0, 1, [2], "warming up")],
        running=set(),
    )
    page = open_page()
    page.until("messages.length === 1", "the snapshot")

    # Prime the recorder to latch the frame holding a broadcast-coloured
    # ring, then append the broadcast. Nothing else in this fixture draws
    # a circle in that colour: every session is on one engine, whose rim
    # colour is not the broadcast teal.
    page.arm(
        "(f) => f.strokes.some("
        f"s => s.circle && !s.to && s.colour === '{BROADCAST}')"
    )
    with ledger.open("a", encoding="utf-8") as handle:
        handle.writelines(mesh_shot.ledger_lines(
            sessions=sessions, traffic=[(0.0, 0, None, "everyone, please read")],
            running=set(), first=500,
        ))

    page.until("messages.length === 2", "the appended broadcast")
    fan = meshgraph.edges_for(ids[0], ids[1:], "broadcast")
    assert len(fan) == 4

    # The renderer's own model of the fan: ONE ring object carrying the
    # whole fan-out, not one per delivery.
    rings = page.json("pulses.filter(p => p.type === 'ring')")
    assert len(rings) == 1, f"{len(rings)} rings for one broadcast"
    assert rings[0]["node"] == ids[0]
    assert rings[0]["n"] == len(fan)

    frame = page.latched()
    drawn_rings = [c for c in frame.circles if c["colour"] == BROADCAST]
    assert len(drawn_rings) == 1, (
        f"{len(drawn_rings)} broadcast circles drawn in one frame -- a "
        f"broadcast must read as one front, not as {len(fan)} events"
    )
    assert drawn_rings[0]["at"] == ids[0]

    spokes = [e for e in frame.edges if e["colour"] == BROADCAST]
    assert {(e["from"], e["to"]) for e in spokes} == {
        (edge["from"], edge["to"]) for edge in fan
    }
    assert {e["from"] for e in spokes} == {ids[0]}, "a fan has one source"


@requires_chrome
def test_a_heavier_pair_is_drawn_with_a_thicker_stroke(ledger, open_page):
    """Thickness carries volume, which is the only thing on the canvas
    that says a pair talked more than once.

    Three pairs, one message, four and sixteen, and the assertion is that
    the order comes out in the strokes -- not that a particular width
    appears, which would only re-implement the formula the renderer
    already has and would have to be edited every time it was tuned."""
    sessions = tiny(6)
    ids = [s[0] for s in sessions]
    traffic = (
        [(40.0, 0, [1], "once")]
        + [(39.0 - i, 2, [3], f"four {i}") for i in range(4)]
        + [(30.0 - i * 0.1, 4, [5], f"sixteen {i}") for i in range(16)]
    )
    mesh_shot.write_ledger(ledger, sessions=sessions, traffic=traffic, running=set())

    page = open_page()
    page.until(f"messages.length === {len(traffic)}", "the snapshot")
    page.unhover()
    frame = page.frame()

    light = frame.edge(ids[0], ids[1])["width"]
    middle = frame.edge(ids[2], ids[3])["width"]
    heavy = frame.edge(ids[4], ids[5])["width"]
    assert light < middle < heavy, (
        f"stroke widths {light}, {middle}, {heavy} do not follow message "
        f"counts 1, 4, 16"
    )
    # And the page's own weights are what fed them.
    weights = page.json(
        "Array.from(pairs.values()).map(p => [p.from, p.to, p.count])"
    )
    assert sorted(w for _f, _t, w in weights) == [1, 4, 16]


@requires_chrome
def test_no_node_is_ever_drawn_with_an_edge_to_itself(ledger, open_page):
    """A broadcast is addressed to the whole roster INCLUDING the sender,
    and ``edges_for`` drops that delivery before a record is ever served.
    A loop is a shape the force layout cannot place and a reader cannot
    interpret, so its absence is checked on the canvas rather than only in
    the server that promised it -- this is the one assertion that would
    catch the page inventing topology of its own."""
    sessions = tiny(4)
    mesh_shot.write_ledger(
        ledger, sessions=sessions, running=set(),
        traffic=[(20.0, 0, None, "all of you"), (10.0, 2, None, "and again")],
    )
    page = open_page()
    page.until("messages.length === 2", "the snapshot")

    assert page.json(
        "Array.from(pairs.values()).filter(p => p.from === p.to)"
    ) == []
    frame = page.frame()
    assert [e for e in frame.edges if e["from"] == e["to"]] == []
    assert None not in {e["from"] for e in frame.edges}, (
        "an edge was drawn from a point that is not any node"
    )


# =======================================================================
# 5. the controls
# =======================================================================


@requires_chrome
def test_muting_broadcast_removes_the_pairs_that_are_only_broadcast(
    ledger, open_page
):
    """The toggle is a control for the experiment, not a cosmetic filter:
    with the fan muted the view IS the pairwise-only condition.

    Which is why the assertion is in two halves. A pair carrying only
    broadcast traffic must disappear; a pair that also carries direct
    messages must stay, drawn on its direct weight alone. A filter that
    hid both would be hiding evidence."""
    sessions = tiny(4)
    ids = [s[0] for s in sessions]
    mesh_shot.write_ledger(
        ledger, sessions=sessions, running=set(),
        traffic=[
            (30.0, 0, None, "everyone"),      # 0 -> 1, 2, 3, all broadcast
            (20.0, 0, [1], "and you again"),  # 0 -> 1 also direct
        ],
    )
    page = open_page()
    page.until("messages.length === 2", "the snapshot")
    page.unhover()

    before = page.frame().pairs()
    assert before == {(ids[0], ids[1]), (ids[0], ids[2]), (ids[0], ids[3])}

    page.click_element("opt-broadcast")
    page.until("showBroadcast === false", "the toggle")

    after = page.frame().pairs()
    assert after == {(ids[0], ids[1])}, (
        "muting the fan must leave the pair that also spoke directly, and "
        "only that one"
    )
    assert page.frame().edge(ids[0], ids[1])["colour"] == DIRECT

    page.click_element("opt-broadcast")
    page.until("showBroadcast === true", "the toggle back")
    assert page.frame().pairs() == before


@requires_chrome
def test_all_labels_draws_the_ones_collision_skipping_drops(ledger, open_page):
    """The toggle exists because a dense graph with every label drawn is
    a wall of overlapping text, and the collision skipping that fixes
    that is invisible until you ask for the labels it dropped.

    The fixture is deliberately extreme -- twenty sessions, every one of
    them broadcasting, long titles -- because the default nine-session
    graph is sparse enough that nothing collides and the toggle would
    correctly change nothing. If the first assertion below fails, the
    fixture has stopped being dense rather than the feature having
    broken."""
    sessions = [
        (f"{i:012x}", f"long running session title number {i:02d} in a repository",
         "/home/you/repo/doxa", "claude")
        for i in range(20)
    ]
    mesh_shot.write_ledger(
        ledger, sessions=sessions, running=set(),
        traffic=[(60.0 - i, i, None, f"broadcast {i}") for i in range(20)],
    )
    page = open_page()
    page.until(f"nodes.size === {len(sessions)}", "every session")
    page.unhover()

    skipped = page.frame().labels
    assert len(skipped) < len(sessions), (
        f"{len(skipped)} labels for {len(sessions)} nodes -- nothing "
        f"collided, so this fixture no longer tests the toggle"
    )

    page.click_element("opt-labels")
    page.until("allLabels === true", "the toggle")

    every = page.frame().labels
    assert len(every) == len(sessions), (
        f"{len(every)} labels with 'all labels' on, for {len(sessions)} nodes"
    )
    assert set(every) == {s[1] for s in sessions}
    assert set(skipped) < set(every), (
        "the dropped labels must be a subset of the drawn ones, not "
        "different text"
    )


@requires_chrome
def test_the_fade_slider_changes_how_brightly_an_aged_edge_is_drawn(
    ledger, open_page
):
    """Fade is how the view says "recently" -- an edge decays toward a
    faint resting weight over the window this slider sets, and the
    structure stays drawn underneath.

    Driven with real key events on the focused slider (``Home`` is its
    minimum, ``End`` its maximum) rather than by writing to ``value``,
    because the handler this exercises listens for ``input`` and a value
    assigned from outside fires nothing. Measured off the recorded alpha
    of the one drawn edge, at both ends of the range, on traffic old
    enough to be fully aged at the short end and still warm at the
    long one."""
    sessions = tiny(2)
    ids = [s[0] for s in sessions]
    mesh_shot.write_ledger(
        ledger, sessions=sessions, running=set(),
        traffic=[(100.0, 0, [1], "a hundred seconds ago")],
    )
    page = open_page()
    page.until("messages.length === 1", "the snapshot")
    page.unhover()

    page.press_in("opt-fade", "End")
    page.until("fadeSecs === 300", "the slider at its maximum")
    assert page.text("opt-fade-read") == "300s"
    warm = page.frame().edge(ids[0], ids[1])["alpha"]

    page.press_in("opt-fade", "Home")
    page.until("fadeSecs === 5", "the slider at its minimum")
    assert page.text("opt-fade-read") == "5s"
    cold = page.frame().edge(ids[0], ids[1])["alpha"]

    assert warm > cold, (
        f"a 300s fade window drew a 100s-old edge at {warm}, a 5s window "
        f"at {cold} -- the slider changed nothing"
    )
    assert cold == pytest.approx(REST_ALPHA_DIRECT, abs=1e-9), (
        "fully aged, an edge must sit at its resting weight and still be "
        "drawn: the structure is the point, not just the flash"
    )


# =======================================================================
# 6. empty and degenerate ledgers
# =======================================================================


@requires_chrome
def test_a_ledger_that_does_not_exist_renders_the_empty_state(
    tmp_path, open_page
):
    """Reading a file nobody has written yet is not an error here: an
    absent ledger is the truthful picture of a fleet that has not said
    anything. What must not happen is a blank page, a spinner, or a
    stream that never opens."""
    page = open_page(tmp_path / "nothing-here.jsonl")

    assert page.ask("document.getElementById('empty').hidden") is False
    assert "No peer traffic recorded yet" in page.text("empty-head")
    page.shows("stat-nodes", "0")
    page.shows("stat-msgs", "0")
    assert page.text("conn") == "live", (
        "the stream must open even with nothing to stream -- a quiet fleet "
        "would otherwise read as a broken view"
    )
    assert page.ask("document.getElementById('panel').classList.contains('is-empty')")
    assert page.faults() == []


@requires_chrome
def test_an_empty_ledger_file_renders_the_empty_state(ledger, open_page):
    """The file exists and holds nothing -- a fleet that has started and
    not yet spoken, which is a different path through the reader than no
    file at all and must reach the same screen."""
    ledger.write_text("", encoding="utf-8")
    page = open_page()

    assert page.ask("document.getElementById('empty').hidden") is False
    page.shows("stat-nodes", "0")
    assert page.faults() == []


@requires_chrome
def test_a_malformed_line_is_skipped_and_the_rest_of_the_file_draws(
    ledger, open_page
):
    """The reader shares this file with a writer in another process, so
    it will meet a half-flushed line, a truncated tail after a crash and
    outright garbage. A skipped line is a missing edge; a raised
    exception is a dark view exactly when the fleet got busy.

    Asserted at the PAGE, which is one layer further out than
    ``tests/test_meshgraph.py``'s version of this: the records either
    side of the damage have to arrive, be drawn, and leave the renderer
    without an exception."""
    sessions = tiny(3)
    good = mesh_shot.ledger_lines(
        sessions=sessions, running=set(),
        traffic=[(30.0, 0, [1], "before"), (10.0, 1, [2], "after")],
    )
    with ledger.open("w", encoding="utf-8") as handle:
        handle.write(good[0])
        handle.write('{"from": {"session": "truncated"\n')
        handle.write("not json at all\n")
        handle.write('{"v": 1, "id": "x", "from": null, "to": []}\n')
        handle.write("\n")
        handle.write(good[1])

    page = open_page()
    page.until("messages.length === 2", "the two usable records")

    page.shows("stat-msgs", "2")
    assert set(page.json("Array.from(nodes.keys())")) == {s[0] for s in sessions}
    assert page.frame().pairs() == {
        (sessions[0][0], sessions[1][0]), (sessions[1][0], sessions[2][0])
    }
    assert page.faults() == []


@requires_chrome
def test_a_record_from_a_newer_schema_draws_what_it_can_and_claims_no_more(
    ledger, open_page
):
    """A build that meets a field it has never heard of must stay useful
    and stay honest.

    Both halves matter. The unknown keys are ignored and the traffic is
    still drawn -- it is real traffic. And the unrecognised ``kind`` is
    NOT guessed from the recipient count, because broadcast-versus-
    pairwise is the experiment's independent variable and inferring it
    from the dependent one would fabricate the measurement. So the edge
    draws in the neutral grey and the feed row says KIND? rather than
    picking a side."""
    sessions = tiny(2)
    ids = [s[0] for s in sessions]
    record = mesh_shot.ledger_records(
        sessions=sessions, traffic=[(10.0, 0, [1], "from the future")],
        running=set(),
    )[0]
    record["kind"] = "telepathy"
    record["v"] = 99
    record["attachments"] = [{"kind": "image", "bytes": 4}]
    record["from"]["capabilities"] = ["quantum"]
    ledger.write_text(json.dumps(record) + "\n", encoding="utf-8")

    page = open_page()
    page.until("messages.length === 1", "the record")
    page.unhover()

    assert page.ask("messages[0].kind") == "unknown"
    assert page.frame().edge(ids[0], ids[1])["colour"] == UNKNOWN

    page.click_node(ids[0])
    page.until("document.getElementById('sel-feed').children.length === 1", "the feed")
    assert page.ask(
        "document.querySelector('#sel-feed .msg-tag').textContent"
    ) == "KIND?"
    assert page.ask(
        "document.querySelector('#sel-feed .msg-body').textContent"
    ) == "from the future"
    assert page.faults() == []


# =======================================================================
# 7. live update
# =======================================================================


@requires_chrome
def test_a_message_appended_after_load_reaches_the_graph_without_a_reload(
    ledger, open_page
):
    """The whole reason this is a live view rather than a report.

    A session that appears only in the appended record has to become a
    node, its edge has to be drawn, and its body has to reach the panel
    of a session that was already selected before it arrived -- all of it
    on the stream the page opened at the offset its snapshot stopped at.
    The sentinel is what makes "without a reload" an assertion rather
    than a hope: a value set in this document survives only as long as
    this document does."""
    sessions = tiny(3)
    ids = [s[0] for s in sessions]
    mesh_shot.write_ledger(
        ledger, sessions=sessions, running=set(),
        traffic=[(30.0, 0, [1], "just the two of us")],
    )
    page = open_page()
    page.until("messages.length === 1", "the snapshot")
    page.shows("stat-nodes", "2")
    page.click_node(ids[0])
    page.ask("window.__sentinel = 'this document'")

    with ledger.open("a", encoding="utf-8") as handle:
        handle.writelines(mesh_shot.ledger_lines(
            sessions=sessions, running=set(), first=700,
            traffic=[(0.0, 2, [0], "a third session, arriving live")],
        ))

    page.until("messages.length === 2", "the appended record")
    page.until("nodes.size === 3", "the session that arrived on the stream")
    page.shows("stat-nodes", "3")
    page.shows("stat-msgs", "2")

    assert page.frame().pairs() == {(ids[0], ids[1]), (ids[2], ids[0])}

    page.until(
        "document.getElementById('sel-feed').children.length === 2",
        "the new message in the open panel",
    )
    assert "a third session, arriving live" in page.ask(
        "document.getElementById('sel-feed').textContent"
    )

    assert page.ask("window.__sentinel") == "this document", (
        "the page reloaded -- the update did not arrive on the stream"
    )
    assert page.faults() == []


# =======================================================================
# 8. the suite's own cost
# =======================================================================


def test_one_browser_is_shared_by_every_test_in_this_file():
    """The reason this suite runs in the default ``uv run pytest`` rather
    than behind a marker is that it costs 38 seconds, and the only reason
    it costs that is that Chrome is launched once.

    Measured on the machine this was written on: 0.5s to launch a
    browser, 0.01s to navigate one. Eighteen launches would be ten
    seconds of pure process start-up on top of the work -- a quarter
    again on the whole file -- and the edit that causes it, changing this
    scope or
    building a browser inside a test, is a one-word change nobody would
    see in review. So the scope is asserted rather than commented. No
    browser is needed to check it, which is the point: this one runs
    everywhere."""
    marker = (
        getattr(browser, "_fixture_function_marker", None)      # pytest >= 9
        or getattr(browser, "_pytestfixturefunction", None)     # pytest < 9
    )
    assert marker is not None, "pytest no longer records a fixture's scope here"
    assert marker.scope == "session", (
        "the mesh web suite must share one browser: a browser per test "
        "costs minutes and would put this file behind a marker"
    )

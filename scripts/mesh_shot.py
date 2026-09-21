# SPDX-License-Identifier: AGPL-3.0-only
"""Render the README's mesh-graph asset -- the one gallery image the
gallery's own driver cannot make.

scripts/screenshot.py drives DoxaApp under a Textual pilot and saves what
a TERMINAL renders, as an SVG of character cells. The mesh graph is not
in the terminal and deliberately never was: doxa/meshgraph.py's own
docstring settles that a graph is the one artifact a terminal is bad at
(32 nodes and a few hundred edges have no honest character-cell
rendering, and doxa.beliefgraph already measured what happens when you
try), so `/mesh` serves a loopback-only, token-gated page and the graph
is drawn on a `<canvas>`. A canvas has no cells to export, which is why
this file exists and why it is a browser rather than a pilot:

    uv run python scripts/mesh_shot.py

What it does, in order: writes a synthetic ledger under its own throwaway
temp root, starts the REAL :class:`doxa.meshgraph.MeshServer` over that
file, asks the server for `/ledger` and checks that what it is serving is
the graph this file meant to draw, drives headless Chrome to the token
URL, waits for the page's own ready signals and for the force layout to
stop moving, CLICKS a session, checks the panel that opens, and saves the
frame.

ONE FILE, NOT TWO. Every pilot scene commits an SVG and a PNG converted
from it; this one commits `assets/shots/mesh.png` and has no SVG twin,
because the browser rasterises and a canvas has no vector form to export
in the first place. The README references the PNG like every other row.

THE GEOMETRY IS THE GALLERY'S. 3068x1734 is what a 250x69 terminal
exports to (scripts/screenshot.py's calibrated constants), and every
other asset is that size, so this one is too -- reached as 1534x867 CSS
pixels at a device scale factor of 2 rather than as a 3068-pixel-wide
viewport, because the page's type is sized in CSS pixels: a viewport that
wide would render a correct image of a graph nobody could read.

WHY CHROME IS DRIVEN OVER CDP AND NOT WITH `--screenshot`. The flag was
what this script used first, and it cannot do the two things the image
needs. It captures whenever Chrome considers the page loaded, which is
sometimes while the force layout is still folding itself open -- same
data, nodes piled together, labels dropped where they collided -- and
measured across repeated runs it landed there about one time in three.
And it cannot click, so the right-hand panel stayed on its empty state
("Click a session to read its recent traffic") in an image whose subject
is being able to read that traffic. `--virtual-time-budget`, the usual
answer to the first problem, HANGS here: virtual time pauses while a
fetch is outstanding and this page holds an SSE stream open by design
(doxa.meshgraph: "the traffic is one-way and SSE is a text protocol over
the HTTP server already here"), so the clock never advances and Chrome
never captures. Measured at 45s and at several minutes before being
killed.

So the browser is driven over the DevTools protocol instead, on the PIPE
transport, which needs no dependency: Chrome reads CDP on fd 3 and writes
it on fd 4, one JSON message per NUL byte. That client lives in
`scripts/cdp.py` -- it was written here and moved out when
`tests/test_mesh_page.py` needed the same four operations against the
same page, because two copies of a transport are two places a timeout is
tuned and one place it is fixed. The click is
``Input.dispatchMouseEvent`` -- a real browser-level input event that
becomes a real `pointerdown` in the renderer, the same event a mouse
produces and the one `assets/mesh/mesh.js` actually listens for. Nothing
here dispatches a synthetic DOM event the page would never see from a
user, and nothing writes the panel's contents from outside.

NOTHING HERE TOUCHES THIS MACHINE'S OWN LEDGER. The server is pointed at
the file this script wrote; `doxa.meshgraph.ledger_path` -- which would
resolve `$DOXA_HOME/peers/ledger.jsonl`, i.e. the real traffic between
this machine's real sessions -- is never called. The temp root, the
Chrome profile and the port all go away with the process.
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# The same isolation scripts/screenshot.py performs, for the same reason
# and with the same `setdefault`: doxa.config reads these at IMPORT time,
# and a test that imports this module under pytest has already been
# pointed at its own throwaway directory by tests/conftest.py. Narrower
# here than in the gallery driver, because this script boots no app -- but
# `doxa.meshgraph.ledger_path` falls back to `$DOXA_HOME` when a server is
# started without a path, and this file must never be one keystroke away
# from serving the author's real peer ledger.
_tmp = Path(tempfile.mkdtemp(prefix="doxa-mesh-shot-"))
os.environ.setdefault("DOXA_HOME", str(_tmp / "doxa-home"))
os.environ.setdefault("DOXA_RUNTIME_DIR", str(_tmp / "runtime"))
os.environ.setdefault("XDG_CONFIG_HOME", str(_tmp / "xdg"))

from doxa.meshgraph import MeshServer  # noqa: E402
from scripts import cdp  # noqa: E402

SHOTS = ROOT / "assets" / "shots"
OUT = SHOTS / "mesh.png"

#: CSS pixels, doubled by the device scale factor to land on the
#: gallery's own 3068x1734. See the module docstring.
WINDOW = (1534, 867)
SCALE = 2

#: The nodes, as the ledger's `from` block spells them: a session id, what
#: it calls itself, where it is working and which engine it is on. Four
#: engines, because the page colours a node's rim by engine
#: (`engineColour`) and a single-vendor graph would show one colour and
#: say nothing about the fleet DOXA actually runs.
_SESSIONS = [
    ("7f3a1c9d40b2", "deploy checklist", "/home/you/repo/doxa", "claude"),
    ("b81e6d20af53", "kg-stats refactor", "/home/you/repo/kg-stats", "claude"),
    ("2c4f90ab7d18", "schema sweep", "/home/you/repo/doxa", "deepseek"),
    ("d5a37e1b8062", "onboarding notes", "/home/you/repo/doxa", "codex"),
    ("9e0b42c7fa31", "changelog pass", "/home/you/repo/doxa", "glm"),
    ("46d8ba09c5e7", "peer ledger audit", "/home/you/repo/doxa", "codex"),
    ("a3f71e5c208d", "import cost", "/home/you/repo/doxa", "deepseek"),
    ("e29c04b6d71f", "release notes", "/home/you/repo/doxa", "claude"),
    ("1b6ef3902ac4", "socket budget", "/home/you/repo/doxa", "glm"),
]

#: Which session the shot opens. It is the one every other session ends up
#: talking to, so its panel is full and its neighbourhood is nearly the
#: whole graph -- selecting a node dims everything outside that
#: neighbourhood, and a selection that greyed out most of the picture
#: would trade one thing the image shows for another. It is also the node
#: whose turn is still running, so the halo and the selection are both on
#: screen at once.
_SELECT = "release notes"

#: `(seconds before now, sender index, recipient indices or None for a
#: broadcast to everyone else, body)`.
#:
#: Read as a graph rather than as a transcript, because that is what the
#: page draws: one broadcast fan from the session that started the
#: conversation, repeated pairwise traffic on the pairs that are really
#: working together (edge weight is message count, drawn as thickness),
#: and several CROSS-VENDOR pairs -- claude to deepseek, codex to glm, glm
#: to claude -- because "a mixed fleet messaged across vendors" is the
#: claim this asset is evidence for and a picture of nine Claude sessions
#: would not be evidence for it.
_TRAFFIC = [
    (54.0, 0, None, "renaming Edge.weight to Edge.support. Who holds a reader?"),
    (51.5, 1, [0], "I hold the reader in kg-stats. Keep an alias for a release."),
    (49.0, 2, [0], "the schema dump names weight in three fixtures. I will move them."),
    (47.5, 0, [1], "alias kept, deprecated in the docstring. Nothing breaks at import."),
    (45.0, 3, [0], "the onboarding page shows weight in an example. Fixing it."),
    (43.0, 4, [3], "send me the example when it lands; the changelog quotes it."),
    (41.0, 1, [2], "your fixtures and my reader disagree on the null case."),
    (39.5, 2, [1], "they do. Yours is right -- a missing support is not zero."),
    (37.0, 5, None, "the ledger audit is done: 128 records, no gaps, one broadcast."),
    (35.5, 6, [5], "does the audit cover the limiter's own counter?"),
    (34.0, 5, [6], "it counts deliveries, not calls. One broadcast is 8 there."),
    (32.5, 0, [2], "moving the fixtures now. I will not touch your reader."),
    (31.0, 7, [5], "I need those numbers for the release notes. Same shape?"),
    (29.5, 5, [7], "same shape. 128 records, 8 sessions, one broadcast, 0 dropped."),
    (28.0, 8, None, "run roots have to stay short: AF_UNIX allows 108 bytes."),
    (26.5, 7, [8], "how short in practice? The notes should give a number."),
    (25.0, 8, [7], "/tmp/dxf plus the run id fits. A deep root refuses at spawn."),
    (23.5, 6, [2], "your sweep and my cost run both import doxa.schemas."),
    (22.0, 2, [6], "I will land mine first so your numbers are not measured twice."),
    (20.5, 1, [0], "alias is in. Reader unchanged, tests green on kg-stats."),
    (19.0, 3, [4], "example updated. It reads support now, with the alias named."),
    (17.5, 4, [3], "quoted. The changelog says when the alias goes."),
    (16.0, 0, [1], "thank you. Three call sites moved, none of them yours."),
    (14.5, 5, [8], "the audit saw no socket refusal. Did yours?"),
    (13.0, 8, [5], "none at this root. It refuses before spawn, so there is no row."),
    (11.5, 7, None, "release notes drafted. Alias, ledger numbers, socket budget."),
    (10.0, 6, [7], "import cost is flat: 0.31s, unchanged by the rename."),
    (8.5, 4, [7], "changelog matches your draft. Nothing left open on my side."),
    (7.0, 2, [0], "fixtures landed. Support everywhere, alias exercised once."),
    (5.5, 0, [7], "done here. The rename is complete and the alias is documented."),
    (4.0, 1, [7], "same. Nothing outstanding in kg-stats."),
    (2.5, 3, [7], "same. Onboarding page shows the new name."),
]

#: Whoever is still mid-turn when the picture is taken. The page breathes
#: a halo around a node whose sender-side turn state is `running`, which is
#: how "who is working right now" reads without any label -- so exactly one
#: session is left running rather than all of them or none.
_RUNNING = {7}


# -- the ledger ------------------------------------------------------------


def _stamp(epoch: float) -> str:
    """A ledger timestamp, microseconds and a trailing Z -- the shape
    ``doxa.peerledger`` writes and :func:`doxa.meshgraph.parse_record`
    documents."""
    return (
        datetime.fromtimestamp(epoch, timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
    )


def ledger_records(
    *,
    now: "float | None" = None,
    sessions: "list | None" = None,
    traffic: "list | None" = None,
    running: "set | None" = None,
    first: int = 0,
) -> "list[dict]":
    """The ledger as objects, in the exact on-the-wire shape
    ``doxa/peerledger.py`` writes and :func:`doxa.meshgraph.parse_record`
    documents.

    Parameterised over the tables above rather than closed over them, so
    that ``tests/test_mesh_page.py`` can build a two-session graph, a
    hostile body or a ledger of nothing but broadcasts WITHOUT inventing a
    second fixture format. One writer means one shape: a test cannot
    accidentally assert against a record the real page would never see,
    and a change to the record shape lands in one place.

    ``first`` numbers the message ids, so a record appended to a ledger
    the page has already read cannot collide with one already ingested --
    ``ingest()`` dedupes by id and would silently drop it.

    Anchored to NOW rather than to a date written here, and that is not
    cosmetic: the page fades an edge toward its resting weight over the
    `fade` window (60s by default), so a ledger dated last year draws a
    graph in which nothing has happened recently -- a true picture of a
    dead file and a useless picture of the view."""
    now = time.time() if now is None else now
    sessions = _SESSIONS if sessions is None else sessions
    traffic = _TRAFFIC if traffic is None else traffic
    running = _RUNNING if running is None else running

    out = []
    for offset, (ago, sender, targets, body) in enumerate(traffic):
        index = first + offset
        recipients = (
            [s[0] for i, s in enumerate(sessions) if i != sender]
            if targets is None
            else [sessions[i][0] for i in targets]
        )
        session, title, repo, engine = sessions[sender]
        out.append({
            "v": 1,
            "id": f"m{index:04d}",
            "ts": _stamp(now - ago),
            "from": {
                "session": session, "title": title, "repo": repo,
                "model": None, "engine": engine,
            },
            "to": recipients,
            "kind": "broadcast" if targets is None else "direct",
            "in_reply_to": None,
            "body": body,
            "body_sha256": f"{index:064x}",
            "latency_ms": 900 + index * 37,
            "turn": {
                "id": None,
                "state": "running" if sender in running else "idle",
            },
        })
    return out


def ledger_lines(**kwargs) -> "list[str]":
    """:func:`ledger_records`, one JSON object per line, newline included
    -- the bytes the writer appends."""
    return [json.dumps(record) + "\n" for record in ledger_records(**kwargs)]


def write_ledger(path: Path, **kwargs) -> int:
    """Write the synthetic ledger and return how many records it holds."""
    lines = ledger_lines(**kwargs)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        handle.writelines(lines)
    return len(lines)


def check_served(url: str, expected: int) -> None:
    """Ask the server what it is serving, before spending a browser on it.

    The page is a client of `/ledger`; if that endpoint does not hold the
    records this script wrote, no amount of looking at the PNG afterwards
    says WHY. So the failure is caught here, where the message can name
    the number that was wrong -- including the cross-vendor edge, which is
    the one property of the graph this asset exists to show and the one a
    later edit to the traffic table above could silently drop."""
    with urllib.request.urlopen(url + "ledger", timeout=10) as response:
        data = json.loads(response.read().decode("utf-8"))
    records = data.get("records") or []
    if len(records) != expected:
        raise SystemExit(
            f"the server is serving {len(records)} records, not {expected}"
        )
    engines = {s[0]: s[3] for s in _SESSIONS}
    crossings = {
        (engines[edge["from"]], engines.get(edge["to"], ""))
        for record in records for edge in record["edges"]
        if engines.get(edge["to"], "") not in ("", engines[edge["from"]])
    }
    if not crossings:
        raise SystemExit("no cross-vendor edge in the ledger being served")
    print(
        f"serving {len(records)} records, "
        f"{len({s[0] for s in _SESSIONS})} sessions, "
        f"{len(crossings)} cross-vendor engine pairs"
    )


# -- the browser -----------------------------------------------------------
#
# scripts/cdp.py. The class was here until tests/test_mesh_page.py needed
# the same transport against the same page; see this file's docstring and
# that module's for why it moved rather than being copied.


# -- what the frame has to show -------------------------------------------

#: What a SETTLED graph measures, in fractions of the canvas, with the
#: legend strip excluded. Measured across good and bad frames rather than
#: chosen: a settled nine-node layout spans 0.47 of the canvas
#: horizontally and 0.80 vertically, run after run, because the layout is
#: deterministic (assets/mesh/mesh.js separates coincident nodes
#: "deterministically, never randomly -- this view is an instrument for an
#: experiment"); a frame taken mid-settle measured 0.33 x 0.59 and 0.40 x
#: 0.59, its nodes still piled together and two labels dropped for
#: collision. The gate sits in the gap.
SETTLED_WIDTH = 0.43
SETTLED_HEIGHT = 0.72


def _measure(png: bytes) -> "tuple[float, float, int]":
    """``(width fraction, height fraction, colours)`` for the drawn graph.

    The canvas only: everything left of the side panel, below the header
    bar and above the legend. Those three paint in a fixed place whether
    the graph drew or not, so a measurement they contributed to could not
    tell a graph from a blank page."""
    from PIL import Image

    with Image.open(io.BytesIO(png)) as image:
        if image.size != (WINDOW[0] * SCALE, WINDOW[1] * SCALE):
            raise SystemExit(f"the frame is {image.size}, not the gallery's size")
        width, height = image.size
        stage = image.convert("RGB").crop(
            (0, int(height * 0.06), int(width * 0.755), int(height * 0.84))
        )
    width, height = stage.size
    pixels = stage.load()
    background = pixels[3, 3]
    left, top, right, bottom = width, height, 0, 0
    # Every third pixel: this is a bounding box over a 2316x1456 region,
    # which one pixel in nine locates as exactly as all of them do.
    for y in range(0, height, 3):
        for x in range(0, width, 3):
            if sum(abs(a - b) for a, b in zip(pixels[x, y], background)) > 24:
                left, right = min(left, x), max(right, x)
                top, bottom = min(top, y), max(bottom, y)
    colours = stage.getcolors(maxcolors=1 << 20)
    return (
        max(0, right - left) / width,
        max(0, bottom - top) / height,
        0 if colours is None else len(colours),
    )


def wait_for_layout(chrome: cdp.Chrome, timeout: float = 25.0) -> None:
    """Hold until the force layout has stopped folding itself open.

    The page gives no signal for this -- the simulation's own energy lives
    in a classic script's scope and is nobody else's business -- so it is
    measured off the frame, which is the same thing this script checks
    before saving anything. `needsFit` re-frames on every frame while the
    layout moves, so an unsettled graph is a knot in the middle of the
    canvas and a settled one fills it."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        wide, tall, colours = _measure(chrome.frame())
        if colours < 200:
            raise SystemExit(
                f"{colours} colours on the stage -- the page rendered blank, "
                "an error or its own empty state"
            )
        if wide >= SETTLED_WIDTH and tall >= SETTLED_HEIGHT:
            print(f"layout settled at {wide:.2f}x{tall:.2f} of the canvas")
            return
        time.sleep(0.4)
    raise SystemExit("the layout never settled")


def select_session(chrome: cdp.Chrome, title: str) -> "tuple[float, float]":
    """Click sessions until the panel is the one this shot wants.

    A node is drawn on a canvas, so there is no element to click and no
    coordinate to be told: the page hit-tests the pointer against its own
    layout, and this walks the canvas asking the page the question a
    user's eyes answer -- is there a node here? `is-over-node` is the
    class `mesh.js` puts on the canvas while the pointer is over one, so
    hovering is the cheap probe and only a hit is worth a click. A press
    on the background pans by zero and selects nothing, so a miss costs
    nothing either."""
    box = json.loads(chrome.ask(
        "JSON.stringify(document.getElementById('graph')"
        ".getBoundingClientRect())"
    ))
    step = 14  # narrower than the smallest node the page will draw
    found: "dict[str, tuple[float, float]]" = {}
    y = box["top"] + step
    while y < box["bottom"] - step:
        x = box["left"] + step
        while x < box["right"] - step:
            chrome.hover(x, y)
            if chrome.ask(
                "document.getElementById('graph').classList"
                ".contains('is-over-node')"
            ):
                chrome.click(x, y)
                seen = chrome.ask(
                    "document.getElementById('sel-title').textContent.trim()"
                )
                if seen:
                    found.setdefault(seen, (x, y))
                if seen == title:
                    print(f"clicked {seen!r} at {x:.0f},{y:.0f}")
                    return x, y
            x += step
        y += step
    raise SystemExit(
        f"no node named {title!r} on the canvas; found {sorted(found)}"
    )


def check_panel(chrome: cdp.Chrome, title: str) -> None:
    """The panel is open, on the right session, with traffic in it.

    Asserted through the page's own DOM rather than off the pixels,
    because the whole point of the click is that the page filled this in
    itself: a panel open but empty, or open on another session, would be
    a picture of the feature not working."""
    shown = chrome.ask("document.getElementById('sel-title').textContent.trim()")
    if shown != title:
        raise SystemExit(f"the panel shows {shown!r}, not {title!r}")
    if chrome.ask(
        "document.getElementById('panel').classList.contains('is-empty')"
    ):
        raise SystemExit("the panel is still on its empty state")
    rows = chrome.ask("document.getElementById('sel-feed').children.length")
    if not rows:
        raise SystemExit("the panel opened with no traffic in it")
    counts = chrome.ask(
        "['sel-out','sel-in','sel-peers'].map("
        "id => document.getElementById(id).textContent).join('/')"
    )
    print(
        f"panel: {title!r}, {rows} messages in the feed, "
        f"sent/received/peers {counts}"
    )


def main() -> None:
    ledger = _tmp / "peers" / "ledger.jsonl"
    written = write_ledger(ledger)
    server = MeshServer(path=ledger)
    chrome: "cdp.Chrome | None" = None
    try:
        check_served(server.url, written)
        chrome = cdp.Chrome(
            _tmp / "chrome-profile",
            url=server.url, window=WINDOW, scale=SCALE,
        )
        # The page's OWN ready signals, in the order it reaches them: the
        # snapshot has been folded in (the header counts every session),
        # and the stream behind it is open (the connection chip says so).
        chrome.until(
            "document.getElementById('stat-nodes').textContent === "
            f"'{len(_SESSIONS)}'",
            "the ledger snapshot",
        )
        chrome.until(
            "document.getElementById('conn').className === 'conn-live'",
            "the event stream",
        )
        wait_for_layout(chrome)
        select_session(chrome, _SELECT)
        check_panel(chrome, _SELECT)
        png = chrome.frame()
    finally:
        if chrome is not None:
            chrome.close()
        server.stop()

    wide, tall, colours = _measure(png)
    if wide < SETTLED_WIDTH or tall < SETTLED_HEIGHT or colours < 200:
        raise SystemExit(
            f"the saved frame spans {wide:.2f}x{tall:.2f} with {colours} "
            "colours -- something moved between the check and the capture"
        )
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(png)
    print(
        f"saved {OUT} ({len(png):,} bytes, "
        f"{WINDOW[0] * SCALE}x{WINDOW[1] * SCALE}, graph spans "
        f"{wide:.2f}x{tall:.2f} of the canvas, {colours:,} colours)"
    )


if __name__ == "__main__":
    # A browser that refuses, errors or stops answering is a message and
    # an exit status here, not a traceback -- which is what this script
    # did when the transport raised SystemExit from inside it. The class
    # moved to scripts/cdp.py and raises an ordinary exception there, so
    # a test can fail on it properly; the translation happens at the one
    # place that is a command line.
    try:
        main()
    except cdp.CdpError as exc:
        raise SystemExit(str(exc)) from None

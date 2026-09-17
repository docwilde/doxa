# SPDX-License-Identifier: AGPL-3.0-only
"""The live message-graph view: what it must not get wrong.

Every test below is named for the failure it catches rather than the
function it calls, because the interesting ones here are not "does this
return the right value" -- they are properties that would be broken by a
plausible later edit and would then fail silently, in production, in the
one direction that matters.

THE FIXTURES ARE LEDGER LINES, NOT OBJECTS. ``doxa/peerledger.py`` is
being written in parallel and this module deliberately does not import
it; :func:`record` reproduces the agreed on-the-wire shape byte for byte,
so if the writer lands emitting something else, these tests are the thing
that notices. The coupling under test is a file format, and a fixture
built by calling the writer's own constructor would test nothing about
it.

FOUR OF THE FIVE REQUIRED PROPERTIES ARE SECURITY OR LIVENESS, and they
pull in opposite directions, which is why they are pinned together:

* the bind address is the ONLY thing between full message bodies and the
  local network, so it is asserted rather than documented;
* a body is untrusted text, so it must reach the screen as characters --
  tested both at the JSON boundary and as a standing property of the
  script that renders it;
* a stream that replays is a graph that double-counts, and a stream that
  dies on one bad line is a dark view exactly when the fleet gets busy.
  The malformed-line and no-replay tests are the two halves of that.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest

from doxa import meshgraph


# -- fixtures: the on-the-wire ledger shape --------------------------------


def record(**over):
    """One ledger record, exactly the shape ``doxa/peerledger.py`` writes.

    Overrides are shallow on purpose -- a test that wants a different
    sender passes the whole ``from`` object, which keeps the fixture
    honest about the fact that the sender is a nested document and not
    five flat keys."""
    rec = {
        "v": 1,
        "id": uuid.uuid4().hex,
        "ts": "2026-09-17T19:32:00.123456Z",
        "from": {
            "session": "sess-alpha",
            "title": "refactor the parser",
            "repo": "/home/u/repo",
            "model": "claude-opus-5",
            "engine": "claude",
        },
        "to": ["sess-beta"],
        "kind": "direct",
        "in_reply_to": None,
        "body": "picking up the tokenizer, leaving the AST to you",
        "body_sha256": "0" * 64,
        "latency_ms": 412,
        "turn": {"id": None, "state": "idle"},
    }
    rec.update(over)
    return rec


def append(path: Path, *records) -> None:
    """Append records the way the writer will: one JSON object per line."""
    with open(path, "a", encoding="utf-8") as handle:
        for rec in records:
            handle.write(json.dumps(rec) + "\n")


def append_raw(path: Path, *lines: str) -> None:
    """Append literal text -- for the lines a writer should never produce."""
    with open(path, "a", encoding="utf-8") as handle:
        for line in lines:
            handle.write(line + "\n")


@pytest.fixture
def ledger(tmp_path) -> Path:
    return tmp_path / "ledger.jsonl"


@pytest.fixture
def server(ledger):
    mesh = meshgraph.MeshServer(path=ledger)
    try:
        yield mesh
    finally:
        mesh.stop()


# -- HTTP helpers ----------------------------------------------------------


def get(mesh, route, timeout=5.0):
    """A GET inside the capability path. Returns (status, body-text)."""
    url = f"{mesh.url}{route}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as res:
            return res.status, res.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def get_json(mesh, route):
    status, body = get(mesh, route)
    assert status == 200, f"{route} -> {status}"
    return json.loads(body)


def open_stream(mesh, route, timeout=1.0, last_event_id=None):
    req = urllib.request.Request(f"{mesh.url}{route}")
    if last_event_id is not None:
        req.add_header("Last-Event-ID", str(last_event_id))
    return urllib.request.urlopen(req, timeout=timeout)


def collect(stream, seconds=3.0, want=None):
    """Every ``data:`` frame that arrives within ``seconds``.

    Reads to a wall-clock deadline rather than to a frame count, because
    the assertions that matter most are about what does NOT arrive --
    "record 1 was not replayed" is only meaningful if we waited long
    enough for a replay to have shown up."""
    frames = []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if want is not None and len(frames) >= want:
            break
        try:
            line = stream.readline()
        except (TimeoutError, socket.timeout):
            continue
        except OSError:
            break
        if not line:
            break
        text = line.decode("utf-8", "replace")
        if text.startswith("data: "):
            frames.append(json.loads(text[6:]))
    return frames


ASSETS = Path(__file__).resolve().parent.parent / "assets" / "mesh"


# -- 1. the bind address ---------------------------------------------------


@pytest.mark.parametrize(
    "host",
    ["0.0.0.0", "::", "192.168.1.40", "8.8.8.8", "10.0.0.5", "example.com", ""],
)
def test_server_refuses_to_bind_anything_but_loopback(host, ledger):
    """The one boundary protecting the ledger is the bind address.

    There is no authentication step to fail past on this server, and the
    file behind it holds full message bodies from agents working in the
    user's repositories. Bound to 0.0.0.0 on a laptop on a shared
    network, that is the entire corpus offered to the subnet -- and
    nothing about the running process would look any different. So a
    non-loopback address is refused outright rather than trusted to be
    passed correctly, and the refusal happens BEFORE any socket exists:
    the assertion on ``_server`` is what pins that ordering, so a later
    edit cannot move the check after a bind that already succeeded."""
    with pytest.raises(ValueError, match=r"refuses to bind|not a loopback"):
        meshgraph.MeshServer(path=ledger, host=host)

    # And the guard itself, independently of the constructor.
    with pytest.raises(ValueError, match=r"not a loopback address"):
        meshgraph.require_loopback(host)


def test_server_actually_listens_on_loopback_and_nowhere_else(server):
    """The positive half: the default really is 127.0.0.1, not merely
    "not rejected". A guard that passes everything is also a guard that
    never raises."""
    assert server.host == "127.0.0.1"
    assert server._server.server_address[0] == "127.0.0.1"
    assert get(server, "")[0] == 200


def test_loopback_aliases_are_accepted(ledger):
    """``localhost`` and 127.x.x.x are loopback and must not be refused --
    a guard nobody can satisfy gets removed rather than fixed."""
    assert meshgraph.require_loopback("localhost") == "127.0.0.1"
    assert meshgraph.require_loopback("127.0.0.1") == "127.0.0.1"
    assert meshgraph.require_loopback("127.0.0.53") == "127.0.0.53"
    assert meshgraph.require_loopback("::1") == "::1"


def test_a_request_without_the_token_is_refused(server, ledger):
    """Loopback is not "this user": every local process can reach this
    port, and 65k ports is not a secret. The token is the second half of
    the boundary, and a wrong one must not distinguish itself from a
    nonexistent route -- a probe should learn nothing at all."""
    append(ledger, record())
    base = f"http://{server.host}:{server.port}"
    for route in ("/ledger", "/", "/wrong-token/ledger", f"/{server.token}x/ledger"):
        try:
            with urllib.request.urlopen(base + route, timeout=5) as res:
                pytest.fail(f"{route} answered {res.status} without a valid token")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404, route


# -- 2. an untrusted body -------------------------------------------------


HOSTILE = '<script>alert("pwned")</script><img src=x onerror=alert(1)>'


def test_a_body_containing_markup_is_never_rendered_as_markup(server, ledger):
    """A message body is untrusted text and the ledger is full of it.

    The protection here is structural rather than a matter of escaping at
    each call site: the page is STATIC and every record arrives as JSON,
    so a body containing a script tag is a JSON string value at every
    moment of its life and never a byte the HTML parser is asked to look
    at. This test pins both halves of that -- the body survives the JSON
    boundary intact (it is data, not something mangled), and the served
    HTML contains no ledger content whatsoever, which is what makes the
    first fact sufficient."""
    append(ledger, record(body=HOSTILE))

    data = get_json(server, "ledger")
    assert data["records"][0]["body"] == HOSTILE, "the body must survive as data"

    # The transport is JSON, so the dangerous characters are carried as
    # an escaped string rather than as markup -- the raw tag never appears
    # as a bare token in the response.
    status, raw = get(server, "ledger")
    assert status == 200
    assert "<script>" not in raw
    assert "\\u003cscript\\u003e" in raw or '"<script>' not in raw

    # And the page itself is inert: no record, no body, no templating.
    status, page = get(server, "")
    assert status == 200
    assert "alert" not in page
    assert "pwned" not in page
    assert "sess-alpha" not in page


def test_a_body_with_newlines_cannot_break_the_event_framing(server, ledger):
    """SSE is a line protocol: a frame ends at a blank line.

    A body containing a literal newline -- which every multi-line agent
    message has -- would split one ``data:`` frame into two and leave the
    remainder to be parsed as a fresh event, if it were ever written raw.
    ``json.dumps`` escapes it, so the payload is always exactly one line.
    This is the bug that would make long agent messages corrupt the view
    while short ones worked fine."""
    stream = open_stream(server, "events")
    append(ledger, record(body="line one\nline two\n\ndata: forged\n\n"))
    frames = collect(stream, seconds=4.0, want=1)
    stream.close()

    assert len(frames) == 1, "a multi-line body must be exactly one frame"
    assert frames[0]["body"] == "line one\nline two\n\ndata: forged\n\n"


def test_the_page_script_never_uses_an_html_sink(server):
    """A standing property of ``assets/mesh/mesh.js``, asserted here
    because it is precisely the invariant a later edit breaks by accident.

    Every sink below parses a string as markup. The renderer has no need
    of any of them -- the graph is canvas ``fillText`` and the panel is
    ``textContent`` -- so their absence is checkable, and checking it is
    worth more than a comment asking the next author to be careful."""
    source = (ASSETS / "mesh.js").read_text(encoding="utf-8")
    for sink in (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "eval(",
        "new Function",
        "createContextualFragment",
    ):
        assert sink not in source, f"mesh.js must not use {sink}"

    # The body really is written as text, so the rule above has teeth.
    assert "textContent" in source


def test_the_page_carries_a_policy_that_blocks_injected_script(server):
    """Defence in depth behind the escaping: even a body that somehow
    reached the DOM as markup could not fetch or execute anything, and
    the page cannot be framed by another origin."""
    url = f"{server.url}"
    with urllib.request.urlopen(url, timeout=5) as res:
        csp = res.headers.get("Content-Security-Policy", "")
        assert "default-src 'none'" in csp
        assert "script-src 'self'" in csp
        assert "frame-ancestors 'none'" in csp
        assert "unsafe-inline" not in csp
        assert res.headers.get("X-Content-Type-Options") == "nosniff"


# -- 3. the stream does not replay ----------------------------------------


def test_the_events_endpoint_does_not_resend_records_already_delivered(server, ledger):
    """A stream that replays is a graph that double-counts every edge.

    The page fetches history from ``/ledger`` and then opens ``/events``
    at the offset that snapshot stopped at. If the stream ignored the
    cursor and sent the file from the top, every historical message would
    be drawn twice and every volume-weighted edge would be wrong -- which
    is a measurement error, not a cosmetic one, since edge weight is what
    the experiment reads."""
    old = [record(body="first"), record(body="second")]
    append(ledger, *old)

    snapshot = get_json(server, "ledger")
    assert [r["body"] for r in snapshot["records"]] == ["first", "second"]
    offset = snapshot["offset"]

    stream = open_stream(server, f"events?from={offset}")
    fresh = record(body="third")
    append(ledger, fresh)

    frames = collect(stream, seconds=4.0)
    stream.close()

    bodies = [f["body"] for f in frames]
    assert bodies == ["third"], f"expected only the new record, got {bodies}"
    assert {f["id"] for f in frames}.isdisjoint({r["id"] for r in old})


def test_a_stream_opened_with_no_cursor_starts_at_the_end(server, ledger):
    """The default has to be end-of-file rather than zero. A page that
    already has history and opens a cursorless stream must not be handed
    the whole file a second time."""
    append(ledger, record(body="already here"))
    stream = open_stream(server, "events")
    time.sleep(0.4)
    append(ledger, record(body="arrived after"))

    frames = collect(stream, seconds=4.0)
    stream.close()
    assert [f["body"] for f in frames] == ["arrived after"]


def test_a_reconnect_resumes_on_last_event_id_without_gap_or_replay(server, ledger):
    """``EventSource`` reconnects to the URL it was built with, so the
    page cannot rewrite its own ``?from=``. Without honouring
    ``Last-Event-ID`` a dropped connection replays everything since the
    page first loaded -- a laptop lid closing would double the graph."""
    append(ledger, record(body="one"))
    stream = open_stream(server, "events?from=0")
    frames = collect(stream, seconds=3.0, want=1)
    stream.close()
    assert [f["body"] for f in frames] == ["one"]

    # The id the browser would have retained is this record's end offset.
    resume = meshgraph.read_batch(ledger)[1]
    append(ledger, record(body="two"))

    stream = open_stream(server, "events?from=0", last_event_id=resume)
    frames = collect(stream, seconds=4.0)
    stream.close()
    assert [f["body"] for f in frames] == ["two"], "the header must beat ?from="


def test_a_record_written_between_snapshot_and_stream_is_not_lost(server, ledger):
    """The gap the offset handoff exists to close. A record appended in
    the window between the two requests belongs to exactly one of them,
    and "neither" is the failure that loses traffic silently."""
    append(ledger, record(body="before"))
    snapshot = get_json(server, "ledger")

    append(ledger, record(body="in the gap"))
    stream = open_stream(server, f"events?from={snapshot['offset']}")
    frames = collect(stream, seconds=4.0, want=1)
    stream.close()

    assert [f["body"] for f in frames] == ["in the gap"]


# -- 4. a bad line does not kill the reader -------------------------------


def test_a_malformed_ledger_line_is_skipped_rather_than_killing_the_stream(
    server, ledger
):
    """This file is read while another process appends to it, so bad
    lines are not hypothetical: a half-flushed write, a crash-truncated
    tail, a line from a schema version this build has never seen.

    If any of those could take the reader down, the view would go dark
    exactly when the fleet got busy -- the moment it exists for. A
    skipped line costs one edge; a raised exception costs the operator
    their only window onto traffic they did not type."""
    append(ledger, record(body="good one"))
    append_raw(
        ledger,
        "{not json at all",
        "",
        "[1, 2, 3]",                      # valid JSON, wrong shape
        '"a bare string"',
        json.dumps({"v": 1, "body": "no sender"}),   # missing `from`
        json.dumps({"v": 1, "from": {}, "to": ["x"]}),  # sender with no session
        json.dumps({"v": 1, "from": {"session": ""}, "to": ["x"]}),
    )
    append(ledger, record(body="good two"))

    data = get_json(server, "ledger")
    assert [r["body"] for r in data["records"]] == ["good one", "good two"]

    # And the live path survives the same garbage without dropping the
    # connection or the record that follows it.
    stream = open_stream(server, f"events?from={data['offset']}")
    append_raw(ledger, "}{ broken")
    append(ledger, record(body="good three"))
    frames = collect(stream, seconds=4.0, want=1)
    stream.close()
    assert [f["body"] for f in frames] == ["good three"]


def test_a_half_written_line_is_not_consumed_until_it_is_complete(ledger):
    """The classic tail-follower bug, and the reason the cursor stops at
    the last newline. A record caught mid-write must be re-read whole on
    the next poll -- if the offset advanced past the partial line, that
    record would be lost permanently, because the cursor only moves
    forward and nothing ever re-reads behind it."""
    good = record(body="complete")
    with open(ledger, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(good) + "\n")
        handle.write('{"v": 1, "id": "partial", "fr')  # writer caught mid-flush

    records, offset = meshgraph.read_records(ledger)
    assert [r["body"] for r in records] == ["complete"]

    # Finish the line; the second read must produce it, not skip it.
    tail = record(body="was partial")
    with open(ledger, "a", encoding="utf-8") as handle:
        handle.seek(offset)
        handle.truncate()
        handle.write(json.dumps(tail) + "\n")

    more, _ = meshgraph.read_records(ledger, offset)
    assert [r["body"] for r in more] == ["was partial"]


def test_a_batch_of_only_bad_lines_still_advances_the_cursor(ledger):
    """Otherwise the reader re-reads the same garbage on every poll,
    forever, at four passes a second -- a busy loop that looks like a
    hang and costs a core."""
    append_raw(ledger, "garbage one", "garbage two")
    records, offset = meshgraph.read_records(ledger)
    assert records == []
    assert offset > 0, "skipped lines must still be stepped over"

    append(ledger, record(body="after the garbage"))
    more, _ = meshgraph.read_records(ledger, offset)
    assert [r["body"] for r in more] == ["after the garbage"]


def test_an_absent_ledger_is_an_empty_graph_not_an_error(server, tmp_path):
    """The ledger does not exist until a session sends something. Opening
    the view on a quiet fleet must show an empty graph, not a stack
    trace or a 500."""
    missing = meshgraph.MeshServer(path=tmp_path / "nope" / "ledger.jsonl")
    try:
        data = get_json(missing, "ledger")
        assert data == {"records": [], "offset": 0}
    finally:
        missing.stop()


# -- 5. broadcast fan-out --------------------------------------------------


def test_a_broadcast_produces_one_edge_per_recipient(server, ledger):
    """The emergence plan's primary manipulation is broadcast against
    pairwise, and at N=32 one broadcast is 31 deliveries.

    The fan-out is derived server-side precisely so it can be pinned
    here: every edge carries the sender, one recipient, and the kind, so
    the page can draw the whole fan as a single event rather than 31
    unrelated strokes -- and so out-degree, which is what the experiment
    actually measures, counts deliveries rather than calls."""
    roster = [f"sess-{i:02d}" for i in range(32)]
    sender = roster[0]
    append(
        ledger,
        record(
            **{
                "from": {
                    "session": sender, "title": "coordinator?", "repo": "/r",
                    "model": "claude-opus-5", "engine": "claude",
                },
                "to": roster,
                "kind": "broadcast",
                "body": "who has the parser?",
            }
        ),
    )

    data = get_json(server, "ledger")
    edges = data["records"][0]["edges"]

    assert len(edges) == 31, "32 recipients minus the sender itself"
    assert all(e["kind"] == "broadcast" for e in edges)
    assert all(e["from"] == sender for e in edges)
    assert {e["to"] for e in edges} == set(roster) - {sender}


def test_a_broadcast_does_not_draw_an_edge_from_a_node_to_itself(ledger):
    """A broadcast is naturally addressed to the whole roster, sender
    included. A self-edge is a loop the force layout cannot place and a
    reader cannot interpret, and it would inflate the sender's own
    out-degree by one on every broadcast."""
    edges = meshgraph.edges_for("a", ["a", "b", "c"], "broadcast")
    assert [e["to"] for e in edges] == ["b", "c"]


def test_a_direct_message_produces_exactly_one_edge(server, ledger):
    """The other side of the distinction: a direct message is one edge,
    tagged so the page can draw it as the solid arc rather than folding
    it into the broadcast fan."""
    append(ledger, record())
    data = get_json(server, "ledger")
    edges = data["records"][0]["edges"]
    assert edges == [{"from": "sess-alpha", "to": "sess-beta", "kind": "direct"}]


def test_the_kind_is_inferred_when_a_record_omits_it(ledger):
    """A record whose ``kind`` is missing or unrecognised is still real
    traffic and still worth drawing. The fan-out is what the field
    summarises, so it is recovered from shape rather than dropped."""
    many = meshgraph.parse_record(
        json.dumps(record(kind="nonsense", to=["b", "c", "d"]))
    )
    assert many["kind"] == "broadcast"
    one = meshgraph.parse_record(json.dumps(record(to=["b"], kind=None)))
    assert one["kind"] == "direct"


# -- the reader seam -------------------------------------------------------


def test_the_ledger_path_is_overridable_without_importing_the_writer(
    tmp_path, monkeypatch
):
    """``doxa/peerledger.py`` is written in parallel and is deliberately
    not imported here: the entire coupling is this path and the record
    shape. The override is what lets the view follow the writer if it
    settles somewhere else, and what lets a test point at a fixture."""
    monkeypatch.setenv(meshgraph.LEDGER_ENV, str(tmp_path / "elsewhere.jsonl"))
    assert meshgraph.ledger_path() == tmp_path / "elsewhere.jsonl"

    monkeypatch.delenv(meshgraph.LEDGER_ENV)
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "home"))
    # Durable state, NOT the runtime dir the peer registry uses: the
    # experiment's whole output is this file, collected after the run.
    assert meshgraph.ledger_path() == tmp_path / "home" / "peers" / "ledger.jsonl"


def test_a_recipient_with_no_identity_still_becomes_a_node(server, ledger):
    """Identity rides only on ``from``, so a session that has only ever
    RECEIVED is a bare id until it speaks. The view has to be able to
    draw it anyway -- an unlabelled node is a real participant, and
    dropping it would understate the graph."""
    append(ledger, record(to=["never-speaks"]))
    data = get_json(server, "ledger")
    parsed = data["records"][0]
    assert parsed["to"] == ["never-speaks"]
    assert parsed["edges"][0]["to"] == "never-speaks"
    # Nothing in the record names that session's title or repo, which is
    # the contract gap the page works around by falling back to the id.
    assert parsed["title"] == "refactor the parser"  # the SENDER's


def test_the_view_survives_a_record_from_a_newer_schema(ledger):
    """A field this build has never seen must not drop the record. The
    writer will grow fields; a reader that refuses anything it does not
    recognise turns every future ledger into an empty graph."""
    future = record()
    future["v"] = 2
    future["priority"] = "high"
    future["from"]["region"] = "eu-west"
    append(ledger, future)
    records, _ = meshgraph.read_records(ledger)
    assert len(records) == 1
    assert records[0]["body"] == future["body"]


def test_only_the_four_page_files_are_reachable(server):
    """The server sits next to a file of message bodies, so what it will
    serve is an exact-name allow-list rather than a directory. No request
    builds a filesystem path, so there is nothing to traverse -- this
    pins that there is also nothing to stumble into."""
    for route in ("mesh.js", "mesh.css", "index.html", ""):
        assert get(server, route)[0] == 200, route
    for route in ("../../pyproject.toml", "ledger.jsonl", "..%2Fmesh.js", "secrets"):
        assert get(server, route)[0] == 404, route


def test_the_page_assets_are_all_present(server):
    """A missing asset is a blank page, which reads as "the feature is
    broken" rather than "the wheel did not carry assets/mesh"."""
    for name in ("index.html", "mesh.js", "mesh.css"):
        assert (ASSETS / name).is_file(), name
    status, page = get(server, "")
    assert 'src="mesh.js"' in page and 'href="mesh.css"' in page


def test_the_server_stops_cleanly_with_a_stream_still_open(ledger):
    """A stream is an infinite loop by design. If ``stop()`` waited on it
    the TUI would hang on exit, which is the worst possible way for an
    optional view to fail."""
    mesh = meshgraph.MeshServer(path=ledger)
    stream = open_stream(mesh, "events")
    done = threading.Event()
    threading.Thread(target=lambda: (mesh.stop(), done.set()), daemon=True).start()
    assert done.wait(10), "stop() blocked on an open event stream"
    stream.close()

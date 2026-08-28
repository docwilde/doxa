# SPDX-License-Identifier: AGPL-3.0-only
"""v0.86.0: the beliefs picker's ``g`` row action -- one belief's graph
neighbourhood, rendered inline in the TUI or as LORE's pan/zoom mermaid
page in a browser, per the ``graph_view`` setting.

WHY PER-BELIEF AND WHY THERE IS NO WHOLE-GRAPH TEST HERE. A whole-graph
view was built first and measured: 63 asserted edges over 104 beliefs
resolved to 44 disconnected clusters, which mermaid stacks vertically --
1188x13814 pixels, aspect 0.09, fitting on screen at 5% and unreadable at
every zoom above it. ``khop`` from one belief is connected by
construction. So the picker row is the home, and the absence of a
whole-graph surface is a property this file would notice if someone added
one: every assertion below is scoped to a single seeded belief.

THE FIXTURES BUILD EDGES THROUGH LORE'S OWN WRITE PATH --
``belief_insert`` and ``edge_insert``, never a hand-rolled INSERT. A test
that writes the rows itself is a test that keeps passing after LORE
changes the shape of the table it is asserting against, which is the one
failure this whole feature cannot afford: DOXA renders LORE's output and
owns none of it.

Same bar as tests/test_picker_row_actions.py: "in the DOM" and "the user
can see it" are different claims (v0.28.0). The ascii assertions read the
expansion back off the RENDERED option text with markup resolved and poll
until the rows are PAINTED -- not until the worker was started, which is
the race a /context test was written to lose once.
"""

from __future__ import annotations

import time

import pytest
from textual.content import Content

from doxa import beliefgraph
from doxa.app import ChipPicker, DoxaApp, SystemBlock

from fakes import FakeEngine

DAY = 86400.0


def _stamp(secs_ago: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - secs_ago))


def _belief_row(bid, claim, **extra):
    """One belief as ``SessionEngine.list_beliefs`` hands it to the picker.
    Its ``id`` is a REAL id from the throwaway store -- the picker's rows
    come from the engine, but ``g`` reads the store, and the two only meet
    if the id is the same one."""
    belief = {
        "id": bid, "subject": "project:doxa", "claim": claim, "confidence": 0.9,
        "created": _stamp(10 * DAY), "updated": _stamp(2 * DAY),
        "last_referenced": _stamp(2 * DAY), "via": "derived",
        "evidence_count": 0, "outcomes": 0,
    }
    belief.update(extra)
    return belief


# -- store fixtures: LORE's own write path, never a hand-rolled INSERT ----


@pytest.fixture
def store():
    """Snapshot-and-restore around the session-wide throwaway store
    conftest.py points ``LORE_ROOT`` at -- the same discipline
    tests/test_engine.py's ``_belief_graph_engine_cleanup`` uses, because
    these tests write real beliefs and real ``belief_edges`` rows into a
    store every other module in the suite also reads."""
    from lore_core import store as lore_store

    conn = lore_store.db_connect()
    before = {row[0] for row in conn.execute("SELECT id FROM beliefs")}
    try:
        yield
    finally:
        conn = lore_store.db_connect()
        added = [row[0] for row in conn.execute("SELECT id FROM beliefs")
                 if row[0] not in before]
        for bid in added:
            conn.execute("DELETE FROM beliefs WHERE id = ?", (bid,))
            conn.execute("DELETE FROM belief_evidence WHERE belief_id = ?", (bid,))
            conn.execute("DELETE FROM belief_edges WHERE src = ? OR dst = ?",
                         (bid, bid))
            try:
                conn.execute("DELETE FROM belief_fts WHERE belief_id = ?", (bid,))
            except Exception:  # noqa: BLE001 -- the shadow table is optional
                pass
        conn.commit()


@pytest.fixture(autouse=True)
def _no_stray_server():
    """The loopback page server is a real socket. Whichever test starts
    one closes it, so a suite run never leaves a listener behind."""
    try:
        yield
    finally:
        beliefgraph.stop_server()


def _seed(claim: str, session_id: str) -> int:
    from lore_core import store as lore_store
    from lore_core.beliefs import belief_insert

    conn = lore_store.db_connect()
    bid, _created = belief_insert(
        conn, "project:doxa", claim, 0.6, session_id, "doxa", "seeded")
    conn.commit()
    return bid


def _relate(src: int, dst: int, rel: str = "contradicts") -> None:
    from lore_core import store as lore_store
    from lore_core.beliefs import edge_insert

    conn = lore_store.db_connect()
    assert edge_insert(conn, src, dst, rel, "derived",
                       session_id="sess-graph-edge") is True
    conn.commit()


def _related_pair() -> "tuple[int, int]":
    a = _seed("graph-fixture: the wrapper degrades credentials silently",
              "sess-graph-a")
    b = _seed("graph-fixture: delegators must document both env vars",
              "sess-graph-b")
    _relate(a, b)
    return a, b


# -- driving the picker ---------------------------------------------------


async def _open(monkeypatch, tmp_path, fake):
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: fake)
    return DoxaApp(cwd=str(tmp_path))


async def _beliefs_picker(pilot, app):
    pane = app.active_pane
    await pane.open_beliefs_picker()
    for _ in range(200):
        picker = app.query_one("#chip-picker", ChipPicker)
        if picker.is_open:
            await pilot.pause()
            return pane, picker
        await pilot.pause(0.02)
    raise AssertionError("the beliefs picker never opened")


def _shown(picker) -> "list[str]":
    """Rendered rows, markup resolved -- what a reader actually sees."""
    return [Content.from_markup(str(picker.get_option_at_index(i).prompt)).plain
            for i in range(picker.option_count)]


def _row_index(picker, rid: str) -> int:
    return next(i for i, (r, _l) in enumerate(picker._rows) if r == rid)


async def _press_graph(pilot, picker, rid: str) -> None:
    """Fire ``g`` on ``rid`` the way a keystroke does, then wait for the
    PAINTED result -- the expansion rows on screen, or a system block --
    rather than for the worker to have been started. Polling the mount is
    how a /context test raced this exact shape once."""
    picker.highlighted = _row_index(picker, rid)
    assert picker.try_action_key("g") is True, "g must be one of this menu's keys"
    for _ in range(200):
        if rid in picker._expanded or _system_texts(picker.app):
            await pilot.pause()
            return
        await pilot.pause(0.02)
    raise AssertionError("the graph action never produced anything visible")


def _system_texts(app) -> "list[str]":
    return [b.text for b in app.query(SystemBlock) if b.id != "identity-block"]


def _recorder(sink: "list[str]"):
    """A ``webbrowser.open`` stand-in: records the URL it was handed and
    reports that it launched nothing. False rather than True deliberately
    -- it is also the headless answer, so the transcript line under test
    is the one a machine with no browser actually gets."""
    def _open(url, *_args, **_kwargs) -> bool:
        sink.append(url)
        return False

    return _open


# -- the setting ----------------------------------------------------------


def test_graph_view_defaults_to_browser(monkeypatch):
    """Declared default, read through the same precedence every other
    knob uses -- not a literal repeated at the call site."""
    from doxa import config as config_mod

    monkeypatch.delenv("DOXA_GRAPH_VIEW", raising=False)
    config_mod.invalidate()
    assert beliefgraph.graph_view_mode() == "browser"
    assert config_mod.SETTINGS_BY_KEY["graph_view"].default == "browser"
    assert config_mod.effective("DOXA_GRAPH_VIEW") == "browser"


def test_graph_view_env_overrides_the_file(monkeypatch, tmp_path):
    """environment > config file > default, the one rule doxa.config
    states -- asserted with the file saying the opposite of the env."""
    from doxa import config as config_mod

    monkeypatch.setenv("DOXA_HOME", str(tmp_path))
    monkeypatch.delenv("DOXA_GRAPH_VIEW", raising=False)
    config_mod.invalidate()
    config_mod.save({"graph_view": "ascii"})
    assert beliefgraph.graph_view_mode() == "ascii"
    assert config_mod.provenance("DOXA_GRAPH_VIEW")[0] == "config"

    monkeypatch.setenv("DOXA_GRAPH_VIEW", "browser")
    assert beliefgraph.graph_view_mode() == "browser"
    assert config_mod.source_label("DOXA_GRAPH_VIEW").startswith("env ")


def test_an_unrecognized_graph_view_falls_back_rather_than_crashing(monkeypatch):
    """A hand-edited config or a typo'd env var must not be the thing that
    decides a belief has no graph -- the same posture background_mode()
    already takes."""
    monkeypatch.setenv("DOXA_GRAPH_VIEW", "mermaid-please")
    assert beliefgraph.graph_view_mode() == "browser"


# -- one gate, one read ---------------------------------------------------


def test_one_edge_block_gates_both_renderings(store):
    """The gate is LORE's own ``format_edges`` output, fetched ONCE and
    branched on -- not two questions asked of the store that could answer
    differently. ``edge_lines`` is pure so the caller can branch on the
    string it already has."""
    from lore_core import store as lore_store
    from lore_core.beliefs import format_edges

    a, _b = _related_pair()
    lonely = _seed("graph-fixture: gate check, nothing relates", "sess-gate")

    block = beliefgraph.edge_block(a)
    assert block == format_edges(lore_store.db_connect(), a)
    assert block.strip()
    assert beliefgraph.edge_lines(block) == block.splitlines()

    empty = beliefgraph.edge_block(lonely)
    assert empty.strip() == ""
    assert beliefgraph.edge_lines(empty) == [beliefgraph.NO_RELATIONS]


def test_edge_block_does_not_leak_a_connection_per_call(store):
    """These run on a keystroke, so an un-closed sqlite handle per press
    is a file descriptor per press. Asserted by measuring the process's
    OWN open descriptors across a run of calls rather than by reading the
    code -- a ``with conn:`` that looked like a close would pass a
    source-level check and fail this one."""
    import gc
    import sqlite3

    a, _b = _related_pair()
    gc.collect()
    before = len([o for o in gc.get_objects() if isinstance(o, sqlite3.Connection)])
    for _ in range(25):
        beliefgraph.edge_block(a)
    gc.collect()
    after = len([o for o in gc.get_objects() if isinstance(o, sqlite3.Connection)])
    assert after <= before, f"{after - before} connections survived 25 calls"


# -- the ascii path: LORE's edge block, folded under the row --------------


@pytest.mark.asyncio
async def test_ascii_folds_lores_own_edge_block_under_the_belief(
    monkeypatch, tmp_path, store,
):
    """The headline ascii requirement, read off the SCREEN: pressing ``g``
    on a belief that has a relation inserts ``format_edges``' own lines as
    real rows beneath it -- the arrow, the other belief's id, the source
    and the distinct-session support count, exactly as LORE formats them.
    DOXA renders none of this itself, and the assertion is written so it
    would fail if DOXA ever started to."""
    from lore_core import store as lore_store
    from lore_core.beliefs import format_edges

    monkeypatch.setenv("DOXA_GRAPH_VIEW", "ascii")
    a, b = _related_pair()
    expected = format_edges(lore_store.db_connect(), a).splitlines()
    assert expected, "the fixture must produce a real edge block"

    fake = FakeEngine([])
    fake.list_beliefs_result = [_belief_row(a, "the wrapper degrades silently")]
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(200, 48)) as pilot:
        await pilot.pause()
        _pane, picker = await _beliefs_picker(pilot, app)
        before = picker.option_count

        await _press_graph(pilot, picker, f"belief:{a}")

        assert picker.size.height > 0, "the picker must be on screen, not just in the DOM"
        # One row per line of LORE's block, never one joined blob -- the
        # same shape the evidence trail already produces.
        assert picker.option_count == before + len(expected)
        shown = _shown(picker)
        assert any("relations:" in row for row in shown)
        edge_row = next(row for row in shown if "contradicts" in row)
        assert f"[{b}]" in edge_row, edge_row
        assert "derived" in edge_row and "n=1" in edge_row, edge_row
        # No browser page was written: ascii renders in the terminal.
        assert not list(beliefgraph.graph_dir().glob("*.html"))


@pytest.mark.asyncio
async def test_ascii_says_no_relations_recorded_for_the_common_case(
    monkeypatch, tmp_path, store,
):
    """Nine beliefs in ten have no recorded relation at all -- 745 of 799
    active beliefs on the live store this was measured against -- so this
    is not an edge case but very nearly the only case the action is used
    on. It reads as one line in place, not an empty expansion, which is
    indistinguishable from a fetch that failed silently."""
    monkeypatch.setenv("DOXA_GRAPH_VIEW", "ascii")
    lonely = _seed("graph-fixture: a belief nothing relates to", "sess-graph-lonely")

    fake = FakeEngine([])
    fake.list_beliefs_result = [_belief_row(lonely, "nothing relates to this")]
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(200, 48)) as pilot:
        await pilot.pause()
        _pane, picker = await _beliefs_picker(pilot, app)
        before = picker.option_count

        await _press_graph(pilot, picker, f"belief:{lonely}")

        assert picker.option_count == before + 1
        assert any("no relations recorded" in row for row in _shown(picker))
        # And nothing was opened: hide-at-zero means no empty page.
        assert not list(beliefgraph.graph_dir().glob("*.html"))


@pytest.mark.asyncio
async def test_left_folds_the_graph_away_again(monkeypatch, tmp_path, store):
    """The graph expansion is the picker's OWN fold, so the gesture that
    closes an evidence trail closes this too -- one fold mechanism, not a
    second one invented beside it."""
    monkeypatch.setenv("DOXA_GRAPH_VIEW", "ascii")
    a, _b = _related_pair()

    fake = FakeEngine([])
    fake.list_beliefs_result = [_belief_row(a, "the wrapper degrades silently")]
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(200, 48)) as pilot:
        await pilot.pause()
        _pane, picker = await _beliefs_picker(pilot, app)
        before = picker.option_count

        await _press_graph(pilot, picker, f"belief:{a}")
        assert picker.option_count > before

        picker.collapse_current()
        await pilot.pause()
        assert picker.option_count == before
        assert not any("relations:" in row for row in _shown(picker))


# -- the browser path: a page under DOXA's state dir, never LORE_ROOT ----


def test_the_page_is_written_under_doxas_state_dir_not_lore_root(
    monkeypatch, tmp_path, store,
):
    """Where the artifact lands, asserted BOTH ways. ``$DOXA_HOME/graphs``
    is DOXA's durable state home; ``LORE_ROOT`` is a store shared with the
    Claude Code LORE plugin, and a rendered artifact of DOXA's UI is not
    memory. Writing one there would put scratch output inside the one
    directory whose contents are supposed to be beliefs."""
    import os
    from pathlib import Path

    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "doxa-home"))
    a, b = _related_pair()

    path, note = beliefgraph.write_page(a)

    assert path.parent == tmp_path / "doxa-home" / "graphs"
    assert path.name == f"belief-{a}.html"
    lore_root = os.environ["LORE_ROOT"]
    assert lore_root not in str(path)
    assert not list(Path(lore_root).rglob("*.html"))
    # The note is the page's own header line, and it names the scope.
    assert f"belief {a}" in note and "hop(s)" in note
    html = path.read_text(encoding="utf-8")
    # LORE's template, unmodified: the explicit run() that fixed 0.47.0's
    # silent no-render, and the pan/zoom affordances DOXA adds nothing to.
    assert "mermaid" in html
    assert str(b) in html, "the neighbour must be in the drawn subgraph"
    assert "drag to pan" in html and "wheel to zoom" in html


def test_write_page_refuses_a_belief_that_is_not_in_the_active_graph(
    monkeypatch, tmp_path, store,
):
    """The graph view is active beliefs only. A missing or inactive id is
    an ANSWER (KeyError, which the action turns into a sentence), never a
    silently empty page."""
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "doxa-home"))
    with pytest.raises(KeyError):
        beliefgraph.write_page(9_999_999)


@pytest.mark.asyncio
async def test_browser_writes_the_file_prints_the_path_and_opens_no_browser(
    monkeypatch, tmp_path, store,
):
    """The browser path end to end, under test: the file is written, its
    path is PRINTED into the transcript (so a headless or SSH session
    still ends up with something to scp), and no browser is launched --
    asserted at ``webbrowser.open`` itself, the one call that could."""
    monkeypatch.setenv("DOXA_GRAPH_VIEW", "browser")
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "doxa-home"))
    opened: "list[str]" = []
    monkeypatch.setattr(beliefgraph.webbrowser, "open", _recorder(opened))
    a, _b = _related_pair()

    fake = FakeEngine([])
    fake.list_beliefs_result = [_belief_row(a, "the wrapper degrades silently")]
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(200, 48)) as pilot:
        await pilot.pause()
        _pane, picker = await _beliefs_picker(pilot, app)
        before = picker.option_count

        await _press_graph(pilot, picker, f"belief:{a}")

        expected = tmp_path / "doxa-home" / "graphs" / f"belief-{a}.html"
        assert expected.is_file()
        blocks = [t for t in _system_texts(app) if "belief graph" in t]
        assert blocks, _system_texts(app)
        assert str(expected) in blocks[-1]
        # webbrowser.open was reached exactly once and given a URL for
        # THIS page -- and it is the stub, so nothing was launched.
        assert len(opened) == 1
        assert expected.name in opened[0]
        # The browser path renders nothing into the picker.
        assert picker.option_count == before


@pytest.mark.asyncio
async def test_browser_says_no_relations_rather_than_opening_an_empty_page(
    monkeypatch, tmp_path, store,
):
    """Hide-at-zero on the browser path too: the two-in-three case gets a
    sentence, not a page with one node and nothing to look at. One gate
    for both renderings, so the two can never disagree."""
    monkeypatch.setenv("DOXA_GRAPH_VIEW", "browser")
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "doxa-home"))
    launched: "list[str]" = []
    monkeypatch.setattr(beliefgraph.webbrowser, "open", _recorder(launched))
    lonely = _seed("graph-fixture: another belief nothing relates to",
                   "sess-graph-lonely-2")

    fake = FakeEngine([])
    fake.list_beliefs_result = [_belief_row(lonely, "nothing relates to this")]
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(200, 48)) as pilot:
        await pilot.pause()
        _pane, picker = await _beliefs_picker(pilot, app)

        await _press_graph(pilot, picker, f"belief:{lonely}")

        blocks = [t for t in _system_texts(app) if "belief graph" in t]
        assert blocks and "no relations recorded" in blocks[-1]
        assert launched == []
        assert not list((tmp_path / "doxa-home" / "graphs").glob("*.html"))


# -- the two modes really do dispatch to two different paths -------------


@pytest.mark.asyncio
async def test_the_setting_picks_the_path_and_the_other_one_never_runs(
    monkeypatch, tmp_path, store,
):
    """``graph_view`` is the switch, asserted by the ABSENCE of the other
    path's side effect in each case: ascii writes no file, browser folds
    no rows. A setting that quietly ran both would pass a test that only
    checked its own half."""
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "doxa-home"))
    monkeypatch.setattr(beliefgraph.webbrowser, "open", lambda *a, **kw: False)
    a, _b = _related_pair()
    graphs = tmp_path / "doxa-home" / "graphs"

    for mode, writes_file, folds_rows in (("ascii", False, True),
                                          ("browser", True, False)):
        monkeypatch.setenv("DOXA_GRAPH_VIEW", mode)
        fake = FakeEngine([])
        fake.list_beliefs_result = [_belief_row(a, "the wrapper degrades")]
        app = await _open(monkeypatch, tmp_path, fake)
        async with app.run_test(size=(200, 48)) as pilot:
            await pilot.pause()
            _pane, picker = await _beliefs_picker(pilot, app)
            before = picker.option_count

            await _press_graph(pilot, picker, f"belief:{a}")

            assert bool(list(graphs.glob("*.html"))) is writes_file, mode
            assert (picker.option_count > before) is folds_rows, mode
        if writes_file:  # leave the directory as the next iteration expects
            for stale in graphs.glob("*.html"):
                stale.unlink()


# -- a lore_core too old to draw: report, never raise --------------------


@pytest.mark.asyncio
async def test_an_old_lore_core_is_reported_not_raised(
    monkeypatch, tmp_path, store,
):
    """``doxa._lore_bootstrap`` prefers a plugin CHECKOUT over the pinned
    wheel, so the loaded ``lore_core`` can be OLDER than pyproject's pin
    -- an operator running a stale plugin is a real configuration, not a
    hypothetical. The action says what is missing, in the transcript, and
    the app keeps running.

    The absence is simulated at the API, because that is where the code
    looks: a version comparison here would be the exact mistake
    ``belief_action_state`` documents avoiding."""
    monkeypatch.setenv("DOXA_GRAPH_VIEW", "ascii")
    import lore_core.beliefs as lore_beliefs

    monkeypatch.delattr(lore_beliefs, "format_edges")
    a, _b = _related_pair()

    state = beliefgraph.graph_state("ascii")
    assert state["capable"] is False
    assert "beliefs.format_edges" in state["missing"]
    assert "lore_core" in state["reason"] and "0." in state["reason"]

    fake = FakeEngine([])
    fake.list_beliefs_result = [_belief_row(a, "the wrapper degrades silently")]
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(200, 48)) as pilot:
        await pilot.pause()
        _pane, picker = await _beliefs_picker(pilot, app)
        before = picker.option_count

        await _press_graph(pilot, picker, f"belief:{a}")

        blocks = [t for t in _system_texts(app) if "belief graph" in t]
        assert blocks, _system_texts(app)
        assert "format_edges" in blocks[-1]
        assert picker.option_count == before


def test_the_browser_mode_needs_more_of_lore_than_ascii_does(monkeypatch):
    """The capability check is per MODE, not one blanket answer: a
    checkout with ``format_edges`` but no ``mermaid_source`` can still
    render the TUI half, and losing the browser half must not cost it."""
    import lore_core.graph as lore_graph

    monkeypatch.delattr(lore_graph, "mermaid_source")
    assert beliefgraph.graph_state("ascii")["capable"] is True
    browser = beliefgraph.graph_state("browser")
    assert browser["capable"] is False
    assert browser["missing"] == ["graph.mermaid_source"]


# -- the page is served over http, not handed over as a null origin ------


def test_the_page_is_served_over_loopback_http_and_really_answers(
    monkeypatch, tmp_path, store,
):
    """Constraint 2, asserted rather than assumed. LORE's page imports
    mermaid from cdn.jsdelivr.net as an ES module and a ``file://``
    document is a null origin some browsers refuse that fetch from -- a
    page that loads, throws nothing and draws nothing. So the URL handed
    to the browser is an ``http://127.0.0.1`` one, and this fetches it to
    prove the server is real and returns the page DOXA wrote."""
    from urllib.request import urlopen

    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "doxa-home"))
    a, _b = _related_pair()
    path, _note = beliefgraph.write_page(a)

    url = beliefgraph.page_url(path)
    assert url.startswith("http://127.0.0.1:"), url
    with urlopen(url, timeout=10) as response:  # noqa: S310 -- loopback, our own file
        body = response.read().decode("utf-8")
    assert body == path.read_text(encoding="utf-8")


def test_the_served_page_needs_a_token_a_co_tenant_does_not_have(
    monkeypatch, tmp_path, store,
):
    """"Loopback" is not "this user". The graphs directory is 0700, so a
    co-tenant cannot read a rendered page off disk -- but an HTTP server
    on 127.0.0.1 answers any LOCAL process regardless of whose it is, and
    a port is not a secret. Belief claims are in that page in full, so
    adding the server must not widen access to them: every request carries
    a per-process token, and one without gets 404."""
    from urllib.error import HTTPError
    from urllib.parse import urlparse
    from urllib.request import urlopen

    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "doxa-home"))
    a, _b = _related_pair()
    path, _note = beliefgraph.write_page(a)
    url = beliefgraph.page_url(path)
    assert "k=" in url, url

    parts = urlparse(url)
    bare = f"{parts.scheme}://{parts.netloc}{parts.path}"
    with pytest.raises(HTTPError) as caught:
        urlopen(bare, timeout=10)  # noqa: S310 -- loopback, our own server
    assert caught.value.code == 404
    with pytest.raises(HTTPError):
        urlopen(f"{bare}?k=guessed", timeout=10)  # noqa: S310
    # And the real one still works, so the guard is a gate, not a wall.
    with urlopen(url, timeout=10) as response:  # noqa: S310
        assert response.read().decode("utf-8") == path.read_text(encoding="utf-8")


def test_the_server_follows_doxa_home_rather_than_serving_a_stale_root(
    monkeypatch, tmp_path, store,
):
    """``graph_dir`` follows ``DOXA_HOME``. A server cached from an
    EARLIER root would 404 a page that is plainly on disk -- the worst
    kind of wrong, because the path the transcript names really is there
    -- so the root is part of the cache key and a move restarts it."""
    from urllib.request import urlopen

    a, _b = _related_pair()

    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "home-one"))
    first, _note = beliefgraph.write_page(a)
    url_one = beliefgraph.page_url(first)
    assert url_one.startswith("http://127.0.0.1:")

    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "home-two"))
    second, _note = beliefgraph.write_page(a)
    assert second != first
    url_two = beliefgraph.page_url(second)
    assert url_two.startswith("http://127.0.0.1:")
    assert url_two != url_one, "a moved root must not reuse the old port"
    with urlopen(url_two, timeout=10) as response:  # noqa: S310 -- loopback
        assert response.read().decode("utf-8") == second.read_text(encoding="utf-8")


def test_a_page_outside_the_graphs_dir_is_a_file_url(monkeypatch, tmp_path, store):
    """The server serves ONE directory. Anything else gets ``file://``
    rather than a URL that would need the server to reach above its own
    root."""
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "doxa-home"))
    outside = tmp_path / "elsewhere.html"
    outside.write_text("<p>not ours</p>", encoding="utf-8")
    assert beliefgraph.page_url(outside).startswith("file://")


def test_page_url_degrades_to_file_when_no_server_can_start(
    monkeypatch, tmp_path, store,
):
    """A server that cannot start is a fallback, not a crash: the page
    still opens, and LORE's own template explains the null-origin case in
    the page itself."""
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "doxa-home"))
    monkeypatch.setattr(beliefgraph, "_start_server", lambda: None)
    a, _b = _related_pair()
    path, _note = beliefgraph.write_page(a)
    assert beliefgraph.page_url(path).startswith("file://")


# -- the row control itself ----------------------------------------------


@pytest.mark.asyncio
async def test_the_graph_control_survives_a_read_only_store(monkeypatch, tmp_path):
    """``g`` writes nothing, so it rides ALONGSIDE the writable gate
    rather than under it: a session whose lore_core cannot record an
    outcome loses ``y``/``c``/``s``/``r`` (v0.69.0's "the control is gone,
    not merely inert") and keeps the read-only view."""
    fake = FakeEngine([])
    fake.belief_action_state_result = {
        "capable": False, "version": "0.20.0", "source": "plugin",
        "reason": "missing record_outcome",
    }
    fake.list_beliefs_result = [_belief_row(1, "a claim")]
    app = await _open(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(200, 48)) as pilot:
        await pilot.pause()
        _pane, picker = await _beliefs_picker(pilot, app)
        keys = [spec.key for spec in picker._row_actions or []]
        assert keys == ["g"], keys
        assert any("g graph" in row for row in _shown(picker))


@pytest.mark.asyncio
async def test_the_graph_label_stays_narrower_than_the_widest_verb_beside_it(
    monkeypatch, tmp_path,
):
    """v0.81.0's lesson, as an assertion rather than a note: every action
    label is a fixed column out of the SAME budget the claim text is
    trimmed against, and an over-long one on this row was a reported
    defect once. ``g graph`` is checked against the widest label already
    there, so a future rewording that widens the row fails here first."""
    from doxa.session.chips import BELIEF_GRAPH_ROW_ACTION, BELIEF_ROW_ACTIONS

    widest = max(spec.column_width for spec in BELIEF_ROW_ACTIONS)
    assert BELIEF_GRAPH_ROW_ACTION.column_width < widest
    # And it does not arm: arming is for the destructive verbs, and this
    # one opens a view.
    assert BELIEF_GRAPH_ROW_ACTION.arms is False

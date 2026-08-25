"""`/search`: the live session-search popup over LORE's session FTS index.

The queries run against a SEEDED tmp index (conftest points LORE_ROOT at a
throwaway dir), so these are real FTS5 hits with real snippets, not mocks.
What is pinned: the popup opens on the ``/search `` prefix and only on it,
re-queries incrementally as the query grows, refuses to let a slow query
overwrite a newer one's results, keeps the typed text on Esc, lists recents
for an empty query and says "no matches" quietly for a query with none.
Ctrl+R is a shortcut to the same surface -- there is no second search path
left to test.

Items I/J (v0.21.0, RE-DERIVED -- see CHANGELOG.md's 0.21.0 entry): a
result set spanning several sessions groups into a session-header tree,
collapsed by default, arrows/enter/left/right navigate and fold/unfold it
the same way the trace tree's own ``Collapsible`` rows do; Enter on a
snippet inserts its excerpt through ``doxa.paste``'s placeholder machinery
instead of the old flat one-line quoted reference.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from doxa import history as history_mod
from doxa.app import DoxaApp
from doxa.history import (
    SEARCH_PREFIX,
    SessionSearch,
    excerpt_provenance,
    excerpt_text,
    group_by_session,
    recent_sessions,
    row_label,
    search_sessions,
    snippet_markup,
)
from tests.fakes import FakeEngine

SESSION_ID = "cafe0001-2222-3333-4444-555566667777"
OTHER_ID = "beef0002-0000-0000-0000-000000000000"


def _seed_index() -> None:
    """Rows straight into lore_core's msg FTS5 table (and the sessions
    table the titles and the recents come from) -- the index `lore search`
    serves; growing it is out of scope here, so the test seeds it the way
    index_sessions would have."""
    from doxa import _lore_bootstrap  # noqa: F401
    from lore_core import store as lore_store

    conn = lore_store.db_connect()
    # Idempotent: LORE_ROOT (and so state.db) is shared across this
    # module's tests -- reseeding must not duplicate rows.
    conn.execute("DELETE FROM msg WHERE session_id IN (?, ?)", (SESSION_ID, OTHER_ID))
    conn.execute("DELETE FROM sessions WHERE session_id IN (?, ?)",
                 (SESSION_ID, OTHER_ID))
    conn.execute(
        "INSERT INTO msg(session_id, project, ts, role, content) VALUES(?,?,?,?,?)",
        (SESSION_ID, "some-project", "2026-08-20T10:00:00Z", "user",
         "the flux capacitor needs exactly 1.21 gigawatts to fire"),
    )
    # A second SESSION_ID message, sharing "docking" with an OTHER_ID
    # message below but no other seeded content: a query for "docking"
    # gets ONE hit from SESSION_ID and one from OTHER_ID -- the
    # tree-grouping case (two sessions, one hit each).
    conn.execute(
        "INSERT INTO msg(session_id, project, ts, role, content) VALUES(?,?,?,?,?)",
        (SESSION_ID, "some-project", "2026-08-20T10:05:00Z", "assistant",
         "docking clamp diagnostics ran clean after the retrofit"),
    )
    # A third SESSION_ID message sharing "diagnostics" ONLY with the one
    # above (same session): a query for "diagnostics" gets TWO hits from
    # this ONE session -- the "no pointless fold" case, several hits that
    # must still render flat, no header.
    conn.execute(
        "INSERT INTO msg(session_id, project, ts, role, content) VALUES(?,?,?,?,?)",
        (SESSION_ID, "some-project", "2026-08-20T10:06:00Z", "user",
         "run the diagnostics suite once more before we ship"),
    )
    conn.execute(
        "INSERT INTO msg(session_id, project, ts, role, content) VALUES(?,?,?,?,?)",
        (OTHER_ID, "some-project", "2026-08-21T11:00:00Z", "assistant",
         "unrelated chatter about breakfast"),
    )
    conn.execute(
        "INSERT INTO msg(session_id, project, ts, role, content) VALUES(?,?,?,?,?)",
        (OTHER_ID, "some-project", "2026-08-21T11:05:00Z", "user",
         "docking bay chatter continued after breakfast"),
    )
    # The SESSIONS rows are stamped ahead of any clock this suite can run
    # under; the msg rows above keep their fixed dates, which the search,
    # label and excerpt assertions quote literally.
    #
    # recent_sessions() orders by last_ts and pages at RESULT_LIMIT (20),
    # and every engine-driven test in this suite indexes a real session
    # stamped at RUN time -- always newer than a fixed 2026-08-2x seed. So
    # "are the seeded sessions in the recents?" was really asking "have
    # fewer than 20 other tests run first?", and it finally answered no
    # (v0.43.0, four more engine tests). Ordering between the two seeds,
    # which the assertion below does care about, is preserved.
    for sid, title, ts, count in (
        (SESSION_ID, "delorean wiring", "2099-08-20T10:00:00Z", 12),
        (OTHER_ID, "breakfast plans", "2099-08-21T11:00:00Z", 4),
    ):
        conn.execute(
            "INSERT INTO sessions(session_id, project, cwd, title, first_ts,"
            " last_ts, messages) VALUES(?,?,?,?,?,?,?)",
            (sid, "some-project", "/work", title, ts, ts, count),
        )
    conn.commit()


# -- the query layer -------------------------------------------------------


def test_search_sessions_finds_seeded_hit(tmp_path):
    _seed_index()
    hits = search_sessions("flux capacitor", cwd=str(tmp_path))
    assert len(hits) == 1
    hit = hits[0]
    assert hit["session_id"] == SESSION_ID
    assert "flux" in hit["snippet"]
    assert hit["title"] == "delorean wiring"  # filled from the sessions table
    assert search_sessions("", cwd=str(tmp_path)) == []
    assert search_sessions("zzz-no-such-token-zzz", cwd=str(tmp_path)) == []


def test_recent_sessions_are_the_empty_query_answer(tmp_path):
    """An empty box would teach the user nothing is indexed."""
    _seed_index()
    recents = recent_sessions(cwd=str(tmp_path))
    ids = [r["session_id"] for r in recents]
    assert SESSION_ID in ids and OTHER_ID in ids
    # Newest first, and each row says how big the session was.
    assert ids.index(OTHER_ID) < ids.index(SESSION_ID)
    assert "4 messages" in [r["snippet"] for r in recents]


def test_excerpt_provenance_names_session_and_when():
    """Item J: an inserted excerpt is a quote WITH a citation -- one
    short line naming the session and when, ahead of the body."""
    prov = excerpt_provenance({
        "session_id": SESSION_ID, "ts": "2026-08-20T10:00:00Z",
    })
    assert prov == f"[lore session {SESSION_ID} · 2026-08-20T10:00:00]"
    assert prov.count("\n") == 0  # ONE line, as specced


def test_excerpt_text_is_provenance_then_demarked_snippet():
    text = excerpt_text({
        "session_id": SESSION_ID, "ts": "2026-08-20T10:00:00Z",
        "snippet": "the [flux capacitor] needs…",
    })
    prov, body = text.split("\n", 1)
    assert prov == excerpt_provenance({"session_id": SESSION_ID, "ts": "2026-08-20T10:00:00Z"})
    assert body == "the flux capacitor needs…"
    assert "[flux" not in text  # FTS match markers stripped


# -- the tree (item I) ------------------------------------------------------


def test_group_by_session_preserves_rank_order_within_and_across():
    hits = [
        {"session_id": "a", "title": "A", "ts": "2026-08-01", "snippet": "1"},
        {"session_id": "b", "title": "B", "ts": "2026-08-02", "snippet": "2"},
        {"session_id": "a", "title": "A", "ts": "2026-08-01", "snippet": "3"},
    ]
    groups = group_by_session(hits)
    assert [g["session_id"] for g in groups] == ["a", "b"]  # first-seen order
    assert [h["snippet"] for h in groups[0]["hits"]] == ["1", "3"]
    assert [h["snippet"] for h in groups[1]["hits"]] == ["2"]
    assert groups[0]["collapsed"] is True and groups[1]["collapsed"] is True


def test_matched_terms_are_highlighted_from_the_index_markers():
    """FTS5's own snippet() brackets what matched; the popup paints those
    and invents no highlighting of its own."""
    text = snippet_markup("the [flux] capacitor needs [gigawatts]")
    assert text.plain == "the flux capacitor needs gigawatts"
    styled = [(text.plain[s.start:s.end], str(s.style)) for s in text.spans]
    assert [word for word, _style in styled] == ["flux", "gigawatts"]
    assert all(style for _word, style in styled)

    label = row_label({
        "session_id": SESSION_ID, "title": "delorean wiring",
        "ts": "2026-08-20T10:00:00Z", "snippet": "[flux] capacitor",
    })
    assert "delorean wiring" in label.plain
    assert "2026-08-20 10:00" in label.plain


# -- the popup -------------------------------------------------------------


async def _app(monkeypatch, tmp_path):
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: FakeEngine([]))
    return DoxaApp(cwd=str(tmp_path))


async def _type(pilot, text: str) -> None:
    for char in text:
        await pilot.press({"/": "slash", " ": "space"}.get(char, char))


async def _settle(pilot, popup, tries=200):
    for _ in range(tries):
        if popup.query_text is not None:
            return True
        await pilot.pause(0.02)
    return popup.query_text is not None


@pytest.mark.asyncio
async def test_the_separating_space_is_what_opens_the_popup(monkeypatch, tmp_path):
    _seed_index()
    app = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        popup = app.query_one("#session-search", SessionSearch)

        await _type(pilot, "/search")
        await pilot.pause(0.2)
        assert popup.is_open is False  # still just a command being typed
        # ...and the ordinary autocomplete is what is showing instead.
        from doxa.app import SlashComplete

        assert app.query_one("#slash-complete", SlashComplete).is_open is True

        await _type(pilot, " ")
        assert await _settle(pilot, popup)
        assert popup.is_open is True
        assert app.query_one("#slash-complete", SlashComplete).is_open is False
        # The caret never left the prompt.
        assert app.focused is app.query_one("#prompt-input")


@pytest.mark.asyncio
async def test_empty_query_lists_recent_sessions(monkeypatch, tmp_path):
    _seed_index()
    app = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        popup = app.query_one("#session-search", SessionSearch)
        await _type(pilot, SEARCH_PREFIX)
        assert await _settle(pilot, popup)
        assert popup.query_text == ""
        assert [h["session_id"] for h in popup.hits]
        assert popup.option_count == len(popup.hits)


@pytest.mark.asyncio
async def test_every_keystroke_requeries(monkeypatch, tmp_path):
    """Incremental: each keystroke is a new query, and the list follows."""
    _seed_index()
    app = await _app(monkeypatch, tmp_path)
    queries: list[str] = []
    real = history_mod.search_sessions

    def spy(query, cwd, limit=20):
        queries.append(query)
        return real(query, cwd, limit)

    monkeypatch.setattr(history_mod, "search_sessions", spy)
    async with app.run_test() as pilot:
        await pilot.pause()
        popup = app.query_one("#session-search", SessionSearch)
        await _type(pilot, SEARCH_PREFIX)
        assert await _settle(pilot, popup)

        # The tail keeps it ONE token: a second word would be rescued by
        # the operator's OR-widening and never show the empty case.
        for fragment, expect in (("flux", 1), ("fluxzzzqqq", 0)):
            before = popup.query_text
            await _type(pilot, fragment[len(before or ""):])
            for _ in range(200):
                if popup.query_text == fragment:
                    break
                await pilot.pause(0.02)
            assert popup.query_text == fragment
            assert len(popup.hits) == expect

        assert "flux" in queries  # the intermediate query really ran
        # Zero hits is a quiet row, not an error and not an empty box.
        assert popup.option_count == 1
        assert popup.get_option_at_index(0).disabled is True
        assert "no matches" in str(popup.get_option_at_index(0).prompt)


@pytest.mark.asyncio
async def test_a_slow_query_never_clobbers_a_newer_one(monkeypatch, tmp_path):
    """The race a debounce alone does not fix: the query for "au" comes
    back AFTER the query for "audit" and must drop its results."""
    app = await _app(monkeypatch, tmp_path)
    release = threading.Event()

    def staggered(query, cwd, limit=20):
        if query == "slow":
            release.wait(5)
            return [{"session_id": "slow0000", "project": "p", "ts": "",
                     "role": "user", "snippet": "STALE", "title": "stale"}]
        return [{"session_id": "fast0000", "project": "p", "ts": "",
                 "role": "user", "snippet": "FRESH", "title": "fresh"}]

    monkeypatch.setattr(history_mod, "search_sessions", staggered)
    async with app.run_test() as pilot:
        await pilot.pause()
        popup = app.query_one("#session-search", SessionSearch)
        prompt = app.query_one("#prompt-input")

        popup.sync(SEARCH_PREFIX + "slow")
        popup.launch("slow")           # in flight, blocked in its thread
        await pilot.pause(0.05)
        prompt.value = SEARCH_PREFIX + "fresher"
        popup.sync(SEARCH_PREFIX + "fresher")
        popup.launch("fresher")
        for _ in range(200):
            if popup.query_text == "fresher":
                break
            await pilot.pause(0.02)
        assert [h["snippet"] for h in popup.hits] == ["FRESH"]

        release.set()                  # the stale query returns LAST
        await asyncio.sleep(0.1)
        await pilot.pause(0.1)
        assert popup.query_text == "fresher"
        assert [h["snippet"] for h in popup.hits] == ["FRESH"]


@pytest.mark.asyncio
async def test_enter_inserts_the_selected_excerpt(monkeypatch, tmp_path):
    """Item J: Enter on a (single-session, flat) snippet inserts a small
    excerpt inline -- provenance line, then the snippet -- not the old
    one-line quoted reference."""
    _seed_index()
    app = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        popup = app.query_one("#session-search", SessionSearch)
        await _type(pilot, SEARCH_PREFIX + "flux")
        for _ in range(200):
            if popup.hits and popup.query_text == "flux":
                break
            await pilot.pause(0.02)
        assert popup.hits
        assert popup.current_kind() == "hit"  # single session: flat, no header

        await pilot.press("enter")
        await pilot.pause()
        prompt = app.query_one("#prompt-input")
        value = prompt.value
        assert value.startswith(f"[lore session {SESSION_ID} · ")  # provenance
        assert "flux" in value
        assert value.count("\n") == 1  # provenance line, then the snippet
        assert prompt._pending_pastes == []  # short excerpt: not collapsed
        assert popup.is_open is False
        # Material for the next prompt -- nothing was sent to the model.
        assert not app.query("TurnBlock")


@pytest.mark.asyncio
async def test_single_session_result_set_stays_flat(monkeypatch, tmp_path):
    """Item I: several hits, ONE session -- no pointless fold. Every row
    is still a "hit" row, same as the pre-tree popup."""
    _seed_index()
    app = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        popup = app.query_one("#session-search", SessionSearch)
        await _type(pilot, SEARCH_PREFIX + "diagnostics")
        for _ in range(200):
            if popup.hits and popup.query_text == "diagnostics":
                break
            await pilot.pause(0.02)
        assert len(popup.hits) == 2
        assert {h["session_id"] for h in popup.hits} == {SESSION_ID}
        assert popup.option_count == 2
        assert [kind for kind, _p, _g in popup._rows] == ["hit", "hit"]


@pytest.mark.asyncio
async def test_multi_session_result_set_groups_collapsed(monkeypatch, tmp_path):
    """Item I: hits spanning two sessions restructure into a tree,
    collapsed to session headers by default."""
    _seed_index()
    app = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        popup = app.query_one("#session-search", SessionSearch)
        await _type(pilot, SEARCH_PREFIX + "docking")
        for _ in range(200):
            if popup.hits and popup.query_text == "docking":
                break
            await pilot.pause(0.02)
        assert len(popup.hits) == 2
        assert {h["session_id"] for h in popup.hits} == {SESSION_ID, OTHER_ID}
        # Collapsed by default: two header rows, no snippets showing yet.
        assert popup.option_count == 2
        assert [kind for kind, _p, _g in popup._rows] == ["header", "header"]
        assert popup.current_kind() == "header"
        assert all(payload["collapsed"] for _kind, payload, _group in popup._rows)


@pytest.mark.asyncio
async def test_right_expands_left_collapses_a_header(monkeypatch, tmp_path):
    """Item I keyboard: → opens a fold, ← closes it -- additive to the
    trace tree's own Enter-toggles convention (below), not a replacement
    for it."""
    _seed_index()
    app = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        popup = app.query_one("#session-search", SessionSearch)
        await _type(pilot, SEARCH_PREFIX + "docking")
        for _ in range(200):
            if popup.hits and popup.query_text == "docking":
                break
            await pilot.pause(0.02)
        assert popup.option_count == 2  # both headers, collapsed

        await pilot.press("right")
        await pilot.pause()
        assert popup.option_count == 3  # one header expanded: +1 snippet row
        kinds = [kind for kind, _p, _g in popup._rows]
        assert kinds.count("hit") == 1
        # The highlight stayed on the header that was just expanded.
        assert popup.current_kind() == "header"

        await pilot.press("left")
        await pilot.pause()
        assert popup.option_count == 2  # closed again

        # Right on an already-open header, then Left from ITS CHILD row,
        # collapses the parent and lands back on the header.
        await pilot.press("right")
        await pilot.pause()
        await pilot.press("down")
        await pilot.pause()
        assert popup.current_kind() == "hit"
        await pilot.press("left")
        await pilot.pause()
        assert popup.option_count == 2
        assert popup.current_kind() == "header"


@pytest.mark.asyncio
async def test_enter_on_a_header_no_longer_toggles_the_fold(monkeypatch, tmp_path):
    """v0.45.0 REPURPOSED this key, and this test is the old one rewritten
    rather than deleted, so the change is legible where it happened.

    Through v0.44.0 Enter on a session header toggled its fold, reasoning
    that "a header row is never itself an excerpt, so this is the ONLY
    thing Enter can mean here". It now means RESUME that conversation --
    the fold keeps Right and Left (asserted directly above), which is what
    made Enter free.

    Asserted here: Enter does NOT fold, does NOT touch the prompt line,
    and does NOT insert an excerpt. That it opens the confirm dialog is
    tests/test_resume.py's job -- this file owns the popup's key protocol,
    that one owns the resume."""
    _seed_index()
    app = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        popup = app.query_one("#session-search", SessionSearch)
        prompt = app.query_one("#prompt-input")
        await _type(pilot, SEARCH_PREFIX + "docking")
        for _ in range(200):
            if popup.hits and popup.query_text == "docking":
                break
            await pilot.pause(0.02)

        before = prompt.value
        assert popup.current_kind() == "header"
        assert popup.option_count == 2  # both headers, both collapsed

        await pilot.press("enter")
        await pilot.pause()
        # Still two rows: the fold did not open. The prompt line is
        # untouched, so nothing was inserted either.
        assert popup.option_count == 2
        assert prompt.value == before


@pytest.mark.asyncio
async def test_matched_terms_stay_highlighted_in_a_nested_snippet(monkeypatch, tmp_path):
    """Item I must not cost item highlighting: a snippet under a header
    still carries FTS5's own match markers, painted the same way."""
    _seed_index()
    app = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        popup = app.query_one("#session-search", SessionSearch)
        await _type(pilot, SEARCH_PREFIX + "docking")
        for _ in range(200):
            if popup.hits and popup.query_text == "docking":
                break
            await pilot.pause(0.02)
        await pilot.press("right")  # expand the highlighted header
        await pilot.pause()
        label = popup.get_option_at_index(1).prompt  # header, then its snippet
        assert any(str(span.style) for span in label.spans)
        assert "docking" in label.plain


@pytest.mark.asyncio
async def test_large_excerpt_collapses_ctrl_g_expands_and_submit_sends_full_text(
    monkeypatch, tmp_path
):
    """Item J: an excerpt over paste.py's collapse threshold behaves
    EXACTLY like a large clipboard paste -- placeholder, Ctrl+G expands
    it, and the full text goes out on submit either way."""
    from doxa import paste as paste_mod
    from doxa.engine import EngineEvent

    SCRIPT = [
        EngineEvent("turn_started", {}),
        EngineEvent("text_delta", {"text": "ok"}),
        EngineEvent("turn_done", {"cost_usd": 0.0, "duration_ms": 5, "is_error": False}),
    ]
    fake = FakeEngine(SCRIPT)
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: fake)
    app = DoxaApp(cwd=str(tmp_path))
    big_snippet = "\n".join(f"line {i} of a long excerpt" for i in range(30))
    big_hit = {
        "session_id": SESSION_ID, "title": "delorean wiring",
        "ts": "2026-08-20T10:00:00Z", "snippet": big_snippet,
    }
    async with app.run_test() as pilot:
        await pilot.pause()
        popup = app.query_one("#session-search", SessionSearch)
        prompt = app.query_one("#prompt-input")
        prompt.value = SEARCH_PREFIX + "anything"
        await pilot.pause()
        # Paint the scripted hit directly and disarm whatever real query
        # ``sync()`` already armed for "anything" -- same discipline
        # scripts/record_gif.py's own ``_show_search_hits`` uses, so a
        # real (empty) query can never race in and clobber this.
        if popup._timer is not None:
            popup._timer.stop()
            popup._timer = None
        popup._seq += 1
        popup._render("anything", [big_hit])
        await pilot.pause()
        assert popup.current_kind() == "hit"

        expected = history_mod.excerpt_text(big_hit)
        assert paste_mod.should_collapse(expected)  # sanity: this scene IS large

        await pilot.press("enter")
        await pilot.pause()
        assert prompt.value.startswith("⧉ pasted")  # collapsed, not the raw text
        assert prompt._pending_pastes and prompt._pending_pastes[0][1] == expected

        await pilot.press("ctrl+g")
        await pilot.pause()
        assert prompt.value == expected  # expanded in place
        assert prompt._pending_pastes == []

        await pilot.press("enter")  # submit
        await pilot.pause()
        for _ in range(50):
            if fake.received_prompts:
                break
            await pilot.pause(0.02)
        assert fake.received_prompts == [expected]  # full text sent regardless


@pytest.mark.asyncio
async def test_escape_closes_but_keeps_the_typed_text(monkeypatch, tmp_path):
    _seed_index()
    app = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        popup = app.query_one("#session-search", SessionSearch)
        await _type(pilot, SEARCH_PREFIX + "flux")
        assert await _settle(pilot, popup)

        await pilot.press("escape")
        await pilot.pause()
        assert popup.is_open is False
        assert app.query_one("#prompt-input").value == SEARCH_PREFIX + "flux"
        # Latched: typing on does not re-open it for this line...
        await _type(pilot, "y")
        await pilot.pause(0.2)
        assert popup.is_open is False


@pytest.mark.asyncio
async def test_backspacing_past_the_space_closes_the_popup(monkeypatch, tmp_path):
    _seed_index()
    app = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        popup = app.query_one("#session-search", SessionSearch)
        await _type(pilot, SEARCH_PREFIX)
        assert await _settle(pilot, popup)

        await pilot.press("backspace")  # the separating space is gone
        await pilot.pause(0.2)
        assert popup.is_open is False
        assert app.query_one("#prompt-input").value == "/search"
        # ...and the prefix, retyped, opens a fresh popup (no stale latch).
        await _type(pilot, " ")
        assert await _settle(pilot, popup)
        assert popup.is_open is True


@pytest.mark.asyncio
async def test_ctrl_r_is_a_shortcut_to_the_same_surface(monkeypatch, tmp_path):
    """One search path: the key prefills the command, it does not push an
    overlay of its own."""
    _seed_index()
    app = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+r")
        assert await _settle(pilot, app.query_one("#session-search", SessionSearch))
        assert app.query_one("#prompt-input").value == SEARCH_PREFIX
        assert app.query_one("#session-search", SessionSearch).is_open is True
        assert app.screen is app.screen_stack[0]  # no modal was pushed
        assert not hasattr(history_mod, "HistorySearchScreen")


@pytest.mark.asyncio
async def test_submitting_the_command_prints_the_same_hits(monkeypatch, tmp_path):
    """The submitted form is a fallback, never a no-op."""
    _seed_index()
    app = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.active_pane
        prompt = app.query_one("#prompt-input")
        prompt.value = "/search flux capacitor"
        app.query_one("#session-search", SessionSearch).dismiss_for_this_line()
        await pilot.press("enter")
        from doxa.app import SystemBlock

        for _ in range(200):
            blocks = [b for b in pane.query(SystemBlock) if "search:" in b.text]
            if blocks:
                break
            await pilot.pause(0.02)
        assert blocks and "delorean wiring" in blocks[0].text

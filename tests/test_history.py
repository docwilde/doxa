"""`/search`: the live session-search popup over LORE's session FTS index.

The queries run against a SEEDED tmp index (conftest points LORE_ROOT at a
throwaway dir), so these are real FTS5 hits with real snippets, not mocks.
What is pinned: the popup opens on the ``/search `` prefix and only on it,
re-queries incrementally as the query grows, refuses to let a slow query
overwrite a newer one's results, keeps the typed text on Esc, inserts the
chosen session's reference on Enter, lists recents for an empty query and
says "no matches" quietly for a query with none. Ctrl+R is a shortcut to
the same surface -- there is no second search path left to test.
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
    hit_reference,
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
    conn.execute(
        "INSERT INTO msg(session_id, project, ts, role, content) VALUES(?,?,?,?,?)",
        (OTHER_ID, "some-project", "2026-08-21T11:00:00Z", "assistant",
         "unrelated chatter about breakfast"),
    )
    for sid, title, ts, count in (
        (SESSION_ID, "delorean wiring", "2026-08-20T10:00:00Z", 12),
        (OTHER_ID, "breakfast plans", "2026-08-21T11:00:00Z", 4),
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


def test_hit_reference_carries_session_and_snippet():
    ref = hit_reference({
        "session_id": SESSION_ID, "ts": "2026-08-20T10:00:00Z",
        "role": "user", "snippet": "the [flux capacitor] needs…",
    })
    assert SESSION_ID in ref
    assert "2026-08-20T10:00:00" in ref
    assert "flux capacitor" in ref
    assert "[flux" not in ref  # FTS match markers stripped


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
async def test_enter_inserts_the_selected_session(monkeypatch, tmp_path):
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

        await pilot.press("enter")
        await pilot.pause()
        value = app.query_one("#prompt-input").value
        assert SESSION_ID in value
        assert "flux" in value
        assert popup.is_open is False
        # Material for the next prompt -- nothing was sent to the model.
        assert not app.query("TurnBlock")


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

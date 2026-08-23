"""Ctrl+R history search tests: the overlay queries a SEEDED tmp session
index (the same lore_core FTS table `lore search` serves -- conftest points
LORE_ROOT at a throwaway dir), debounced as-you-type, and Enter inserts the
chosen hit's text reference into the prompt input.
"""

from __future__ import annotations

import pytest

from doxa.app import DoxaApp
from doxa.history import HistorySearchScreen, hit_reference, search_sessions
from tests.fakes import FakeEngine

SESSION_ID = "cafe0001-2222-3333-4444-555566667777"


def _seed_index() -> None:
    """Rows straight into lore_core's msg FTS5 table -- the index the store
    module's own search serves; growing it is out of overlay scope, so the
    test seeds it the way index_sessions would have."""
    from doxa import _lore_bootstrap  # noqa: F401
    from lore_core import store as lore_store

    conn = lore_store.db_connect()
    # Idempotent: LORE_ROOT (and so state.db) is shared across this test
    # module's tests -- reseeding must not duplicate rows.
    conn.execute("DELETE FROM msg WHERE session_id IN (?, ?)",
                 (SESSION_ID, "beef0002-0000-0000-0000-000000000000"))
    conn.execute(
        "INSERT INTO msg(session_id, project, ts, role, content) VALUES(?,?,?,?,?)",
        (SESSION_ID, "some-project", "2026-08-20T10:00:00Z", "user",
         "the flux capacitor needs exactly 1.21 gigawatts to fire"),
    )
    conn.execute(
        "INSERT INTO msg(session_id, project, ts, role, content) VALUES(?,?,?,?,?)",
        ("beef0002-0000-0000-0000-000000000000", "some-project",
         "2026-08-21T11:00:00Z", "assistant",
         "unrelated chatter about breakfast"),
    )
    conn.commit()


def test_search_sessions_finds_seeded_hit(tmp_path):
    _seed_index()
    hits = search_sessions("flux capacitor", cwd=str(tmp_path))
    assert len(hits) == 1
    hit = hits[0]
    assert hit["session_id"] == SESSION_ID
    assert "flux" in hit["snippet"]
    assert search_sessions("", cwd=str(tmp_path)) == []
    assert search_sessions("zzz-no-such-token-zzz", cwd=str(tmp_path)) == []


def test_hit_reference_carries_session_and_snippet():
    ref = hit_reference({
        "session_id": SESSION_ID, "ts": "2026-08-20T10:00:00Z",
        "role": "user", "snippet": "the [flux capacitor] needs…",
    })
    assert SESSION_ID in ref
    assert "2026-08-20T10:00:00" in ref
    assert "flux capacitor" in ref
    assert "[flux" not in ref  # FTS match markers stripped


@pytest.mark.asyncio
async def test_ctrl_r_overlay_queries_and_inserts_reference(monkeypatch, tmp_path):
    _seed_index()
    monkeypatch.setattr("doxa.history.DEBOUNCE_SECS", 0.01)
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: FakeEngine([]))

    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+r")
        assert isinstance(app.screen, HistorySearchScreen)
        overlay = app.screen

        overlay.query_one("#history-input").value = "flux capacitor"
        for _ in range(200):  # debounce + threaded query
            if overlay.hits:
                break
            await pilot.pause(0.02)
        assert len(overlay.hits) == 1
        results = overlay.query_one("#history-results")
        assert results.option_count == 1

        await pilot.press("enter")
        await pilot.pause()
        assert not isinstance(app.screen, HistorySearchScreen)
        value = app.query_one("#prompt-input").value
        assert SESSION_ID in value
        assert "flux capacitor" in value

        # The reference is material for the user's next prompt -- nothing
        # was auto-sent to the model.
        assert not app.query("TurnBlock")


@pytest.mark.asyncio
async def test_escape_dismisses_without_inserting(monkeypatch, tmp_path):
    monkeypatch.setattr("doxa.history.DEBOUNCE_SECS", 0.01)
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: FakeEngine([]))

    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+r")
        assert isinstance(app.screen, HistorySearchScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, HistorySearchScreen)
        assert app.query_one("#prompt-input").value == ""

"""Queue item 5's UI surface: the needs-input dialog (NeedsInputPopup),
its key/click protocol, the tab blink it drives (set_needs_input, already
tested mechanism-only in test_tab_status.py -- these tests are what
finally triggers it for real), and the status-bar hint. Same headless
Pilot + FakeEngine pattern as tests/test_tab_status.py.
"""

from __future__ import annotations

import pytest

from doxa.app import DoxaApp, NeedsInputPopup
from doxa.engine import EngineEvent
from textual.widgets import Static, TabbedContent
from tests.fakes import FakeEngine

ASK_USER_DATA = {
    "id": "req-1", "kind": "ask_user", "tool_name": "AskUserQuestion",
    "questions": [{
        "question": "which color?", "header": "Pick one",
        "options": [{"label": "Red", "description": "warm"}, {"label": "Blue", "description": "cool"}],
        "multiSelect": False,
    }],
}

PERMISSION_DATA = {
    "id": "req-2", "kind": "permission", "tool_name": "Bash",
    "input_summary": "Bash rm -rf /tmp/x", "title": "Claude wants to run rm -rf /tmp/x",
    "display_name": "Run command", "description": None,
}


async def _wait(pilot, cond, tries=100):
    for _ in range(tries):
        if cond():
            return True
        await pilot.pause(0.02)
    return cond()


def _tab(app, pane):
    return app.query_one("#session-tabs", TabbedContent).get_tab(pane.id)


def _popup(pane) -> NeedsInputPopup:
    """Scoped through the PANE, not the app -- with more than one tab
    mounted, every pane carries its own ``#needs-input-popup`` (same id,
    different subtree, exactly like ``#prompt-input``/``#slash-complete``
    already do), so an app-wide query would be ambiguous."""
    return pane.query_one("#needs-input-popup", NeedsInputPopup)


@pytest.mark.asyncio
async def test_ask_user_question_opens_the_popup_and_blinks_the_tab(tmp_path, monkeypatch):
    engine = FakeEngine([])
    monkeypatch.setattr("doxa.app.notify_mod.notify", lambda *a, **k: None)
    app = DoxaApp(cwd=str(tmp_path), engine_factory=lambda: engine, new_session_factory=lambda: engine)
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.active_pane
        engine.push_peer_event(EngineEvent("needs_input", ASK_USER_DATA))
        assert await _wait(pilot, lambda: _popup(pane).is_open)
        assert pane.needs_input is True
        assert pane._attention_timer is not None
        assert "needs input" in str(pane.query_one("#status-bar", Static).renderable)


@pytest.mark.asyncio
async def test_number_key_answers_ask_user_question_and_resolves(tmp_path, monkeypatch):
    engine = FakeEngine([])
    monkeypatch.setattr("doxa.app.notify_mod.notify", lambda *a, **k: None)
    app = DoxaApp(cwd=str(tmp_path), engine_factory=lambda: engine, new_session_factory=lambda: engine)
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.active_pane
        engine.push_peer_event(EngineEvent("needs_input", ASK_USER_DATA))
        assert await _wait(pilot, lambda: _popup(pane).is_open)

        await pilot.press("1")  # "Red" -- the first option
        assert await _wait(pilot, lambda: bool(engine.needs_input_answers))

        assert engine.needs_input_answers == [("req-1", {"answers": {"which color?": "Red"}})]
        assert _popup(pane).is_open is False
        assert pane.needs_input is False
        assert pane._attention_timer is None


@pytest.mark.asyncio
async def test_escape_declines_the_ask_user_question(tmp_path, monkeypatch):
    engine = FakeEngine([])
    monkeypatch.setattr("doxa.app.notify_mod.notify", lambda *a, **k: None)
    app = DoxaApp(cwd=str(tmp_path), engine_factory=lambda: engine, new_session_factory=lambda: engine)
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.active_pane
        engine.push_peer_event(EngineEvent("needs_input", ASK_USER_DATA))
        assert await _wait(pilot, lambda: _popup(pane).is_open)

        await pilot.press("escape")
        assert await _wait(pilot, lambda: bool(engine.needs_input_answers))

        assert engine.needs_input_answers == [("req-1", {"declined": True})]
        assert _popup(pane).is_open is False


@pytest.mark.asyncio
async def test_permission_request_offers_allow_deny_and_deny_round_trips(tmp_path, monkeypatch):
    engine = FakeEngine([])
    monkeypatch.setattr("doxa.app.notify_mod.notify", lambda *a, **k: None)
    app = DoxaApp(cwd=str(tmp_path), engine_factory=lambda: engine, new_session_factory=lambda: engine)
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.active_pane
        engine.push_peer_event(EngineEvent("needs_input", PERMISSION_DATA))
        assert await _wait(pilot, lambda: _popup(pane).is_open)
        popup = _popup(pane)
        assert popup.kind == "permission"

        await pilot.press("2")  # "Deny" -- the second option
        assert await _wait(pilot, lambda: bool(engine.needs_input_answers))

        assert engine.needs_input_answers == [("req-2", {"decision": "deny"})]
        assert popup.is_open is False


@pytest.mark.asyncio
async def test_up_down_enter_also_answers(tmp_path, monkeypatch):
    engine = FakeEngine([])
    monkeypatch.setattr("doxa.app.notify_mod.notify", lambda *a, **k: None)
    app = DoxaApp(cwd=str(tmp_path), engine_factory=lambda: engine, new_session_factory=lambda: engine)
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.active_pane
        engine.push_peer_event(EngineEvent("needs_input", PERMISSION_DATA))
        assert await _wait(pilot, lambda: _popup(pane).is_open)

        await pilot.press("down")  # highlight moves from Allow to Deny
        await pilot.press("enter")
        assert await _wait(pilot, lambda: bool(engine.needs_input_answers))

        assert engine.needs_input_answers == [("req-2", {"decision": "deny"})]


@pytest.mark.asyncio
async def test_needs_input_resolved_from_another_client_closes_the_popup_without_reanswering(
    tmp_path, monkeypatch,
):
    """Two-client daemon case, exercised in-process: a needs_input_resolved
    event (as the daemon would fan out after ANOTHER client answered) must
    close this pane's own copy of the dialog WITHOUT calling
    answer_needs_input again."""
    engine = FakeEngine([])
    monkeypatch.setattr("doxa.app.notify_mod.notify", lambda *a, **k: None)
    app = DoxaApp(cwd=str(tmp_path), engine_factory=lambda: engine, new_session_factory=lambda: engine)
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.active_pane
        engine.push_peer_event(EngineEvent("needs_input", ASK_USER_DATA))
        assert await _wait(pilot, lambda: _popup(pane).is_open)

        engine.push_peer_event(EngineEvent("needs_input_resolved", {"id": "req-1"}))
        assert await _wait(pilot, lambda: not _popup(pane).is_open)
        assert pane.needs_input is False
        assert engine.needs_input_answers == []


@pytest.mark.asyncio
async def test_tab_activation_clears_the_blink_but_leaves_the_dialog_open(tmp_path, monkeypatch):
    """set_needs_input(False)'s existing tab-activation clear (already
    tested mechanism-only in test_tab_status.py) stops the BLINK -- it
    must not silently answer the pending question just because you looked
    at the tab."""
    engines: list[FakeEngine] = []

    def make():
        engines.append(FakeEngine([]))
        return engines[-1]

    monkeypatch.setattr("doxa.app.notify_mod.notify", lambda *a, **k: None)
    app = DoxaApp(cwd=str(tmp_path), engine_factory=make, new_session_factory=make)
    async with app.run_test() as pilot:
        await pilot.pause()
        first = app.active_pane
        await pilot.press("ctrl+t")
        assert await _wait(pilot, lambda: len(app.panes()) == 2)
        second = app.panes()[1]
        second_engine = engines[1]

        second_engine.push_peer_event(EngineEvent("needs_input", ASK_USER_DATA))
        assert await _wait(pilot, lambda: second.needs_input is True)

        await pilot.press("ctrl+left")  # activates `first`
        await pilot.press("ctrl+right")  # back to `second`
        await pilot.pause()

        assert app.active_pane is second
        assert second.needs_input is False  # blink cleared
        assert second_engine.needs_input_answers == []  # NOT auto-answered
        assert _popup(second).is_open is True  # the dialog itself is still up

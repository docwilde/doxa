# SPDX-License-Identifier: AGPL-3.0-only
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
from textual.containers import VerticalScroll
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
    return app.query_one("#session-tabs", TabbedContent).get_tab(pane.tab_id)


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


# -- the dialog must stay answerable wherever focus went (v0.43.0) ------
#
# The dialog is ``can_focus = False`` and driven entirely through
# PromptInput's key protocol, so it answers to a key only while the
# PROMPT holds focus. Nothing used to guarantee that. Three ordinary
# gestures put focus somewhere else -- and unlike every other popup in
# this app, this one BLOCKS the session, so a dialog that has gone deaf
# is a wedged tab whose documented way out (Esc) has gone deaf with it.
#
# Each test below drives the gesture and then asserts the USER-VISIBLE
# outcome: the request actually gets answered.


def _prompt(pane):
    from doxa.ui.prompt import PromptInput

    return pane.query_one("#prompt-input", PromptInput)


@pytest.mark.asyncio
async def test_dialog_answers_after_clicking_the_blinking_tab_it_is_already_on(
    tmp_path, monkeypatch,
):
    """THE REPORTED GESTURE. The tab blinks, you click it -- but it was
    already the active tab, so Textual focuses the tab STRIP and posts no
    TabActivated, which means _on_tab_activated (the one hook the mouse
    path has) never runs and never focuses the prompt."""
    engine = FakeEngine([])
    monkeypatch.setattr("doxa.app.notify_mod.notify", lambda *a, **k: None)
    app = DoxaApp(cwd=str(tmp_path), engine_factory=lambda: engine, new_session_factory=lambda: engine)
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.active_pane
        engine.push_peer_event(EngineEvent("needs_input", PERMISSION_DATA))
        assert await _wait(pilot, lambda: _popup(pane).is_open)

        tabbed = app.query_one("#session-tabs", TabbedContent)
        await pilot.click(tabbed.get_tab(pane.tab_id))
        await pilot.pause()

        await pilot.press("2")  # "Deny"
        assert await _wait(pilot, lambda: bool(engine.needs_input_answers))
        assert engine.needs_input_answers == [("req-2", {"decision": "deny"})]


@pytest.mark.asyncio
async def test_dialog_answers_after_reading_the_transcript_first(tmp_path, monkeypatch):
    """Clicking the transcript to scroll back and see what the agent was
    doing before you decide -- ``#block-list`` is a focusable
    VerticalScroll, so that click takes the keyboard, and its own up/down
    bindings then eat the arrow keys the dialog wanted."""
    engine = FakeEngine([])
    monkeypatch.setattr("doxa.app.notify_mod.notify", lambda *a, **k: None)
    app = DoxaApp(cwd=str(tmp_path), engine_factory=lambda: engine, new_session_factory=lambda: engine)
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.active_pane
        engine.push_peer_event(EngineEvent("needs_input", PERMISSION_DATA))
        assert await _wait(pilot, lambda: _popup(pane).is_open)

        await pilot.click(pane.query_one("#block-list", VerticalScroll))
        await pilot.pause()

        await pilot.press("down")  # Allow -> Deny
        await pilot.pause()
        assert _popup(pane).highlighted == 2  # row 0 is the heading
        await pilot.press("enter")
        assert await _wait(pilot, lambda: bool(engine.needs_input_answers))
        assert engine.needs_input_answers == [("req-2", {"decision": "deny"})]


@pytest.mark.asyncio
async def test_dialog_answers_after_tab_moved_focus_off_the_prompt(tmp_path, monkeypatch):
    """The prompt's ``tab_behavior`` is "focus", so a stray Tab hands the
    keyboard to the next focusable widget."""
    engine = FakeEngine([])
    monkeypatch.setattr("doxa.app.notify_mod.notify", lambda *a, **k: None)
    app = DoxaApp(cwd=str(tmp_path), engine_factory=lambda: engine, new_session_factory=lambda: engine)
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.active_pane
        await pilot.press("tab")
        await pilot.pause()
        assert app.focused is not _prompt(pane)  # the gesture really did move it

        engine.push_peer_event(EngineEvent("needs_input", PERMISSION_DATA))
        assert await _wait(pilot, lambda: _popup(pane).is_open)

        await pilot.press("1")  # "Allow"
        assert await _wait(pilot, lambda: bool(engine.needs_input_answers))
        assert engine.needs_input_answers == [("req-2", {"decision": "allow"})]


@pytest.mark.asyncio
async def test_escape_still_declines_when_focus_had_wandered(tmp_path, monkeypatch):
    """Esc is the documented way out of a request you do not want to
    answer. It went deaf with everything else, which is what turned a
    misplaced focus into a session with no way forward at all."""
    engine = FakeEngine([])
    monkeypatch.setattr("doxa.app.notify_mod.notify", lambda *a, **k: None)
    app = DoxaApp(cwd=str(tmp_path), engine_factory=lambda: engine, new_session_factory=lambda: engine)
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.active_pane
        await pilot.click(pane.query_one("#block-list", VerticalScroll))
        await pilot.pause()

        engine.push_peer_event(EngineEvent("needs_input", ASK_USER_DATA))
        assert await _wait(pilot, lambda: _popup(pane).is_open)

        await pilot.press("escape")
        assert await _wait(pilot, lambda: bool(engine.needs_input_answers))
        assert engine.needs_input_answers == [("req-1", {"declined": True})]


@pytest.mark.asyncio
async def test_background_request_answers_to_arrows_digits_and_enter(tmp_path, monkeypatch):
    """The reported path end to end: the request arrives while the pane is
    in the BACKGROUND (so the tab blinks), the user comes over to it, reads
    the transcript, and then answers -- with an arrow, with Enter, and with
    a number key."""
    engines: list[FakeEngine] = []

    def make():
        engines.append(FakeEngine([]))
        return engines[-1]

    monkeypatch.setattr("doxa.app.notify_mod.notify", lambda *a, **k: None)
    app = DoxaApp(cwd=str(tmp_path), engine_factory=make, new_session_factory=make)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+t")
        assert await _wait(pilot, lambda: len(app.panes()) == 2)
        second = app.panes()[1]
        second_engine = engines[1]
        await pilot.press("ctrl+left")  # leave `second` in the background
        await pilot.pause()

        second_engine.push_peer_event(EngineEvent("needs_input", PERMISSION_DATA))
        assert await _wait(pilot, lambda: _popup(second).is_open)
        assert second.needs_input is True  # the blinking tab the user saw

        tabbed = app.query_one("#session-tabs", TabbedContent)
        await pilot.click(tabbed.get_tab(second.tab_id))  # go to the blinking tab
        await pilot.pause()
        await pilot.click(second.query_one("#block-list", VerticalScroll))  # read it
        await pilot.pause()

        await pilot.press("down")
        await pilot.pause()
        assert _popup(second).highlighted == 2
        await pilot.press("up")
        await pilot.pause()
        assert _popup(second).highlighted == 1
        await pilot.press("2")
        assert await _wait(pilot, lambda: bool(second_engine.needs_input_answers))
        assert second_engine.needs_input_answers == [("req-2", {"decision": "deny"})]


@pytest.mark.asyncio
async def test_opening_the_dialog_claims_the_keyboard_only_for_the_active_pane(
    tmp_path, monkeypatch,
):
    """A blocking request opening in the tab you are LOOKING AT takes the
    keyboard -- that is what makes it answerable. One arriving in a
    BACKGROUND tab must not, or it would yank the tab you are typing in
    out from under you (focusing a widget inside a TabPane activates that
    pane); the blink is that case's whole signal."""
    engines: list[FakeEngine] = []

    def make():
        engines.append(FakeEngine([]))
        return engines[-1]

    monkeypatch.setattr("doxa.app.notify_mod.notify", lambda *a, **k: None)
    app = DoxaApp(cwd=str(tmp_path), engine_factory=make, new_session_factory=make)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+t")
        assert await _wait(pilot, lambda: len(app.panes()) == 2)
        first, second = app.panes()[0], app.panes()[1]
        await pilot.press("ctrl+left")  # the user is on `first`
        await pilot.pause()

        engines[1].push_peer_event(EngineEvent("needs_input", PERMISSION_DATA))
        assert await _wait(pilot, lambda: _popup(second).is_open)
        assert app.active_pane is first
        assert app.focused is _prompt(first)

        # ... and in the active pane, it does claim the keyboard.
        await pilot.click(first.query_one("#block-list", VerticalScroll))
        await pilot.pause()
        engines[0].push_peer_event(EngineEvent("needs_input", ASK_USER_DATA))
        assert await _wait(pilot, lambda: _popup(first).is_open)
        assert app.focused is _prompt(first)


@pytest.mark.asyncio
async def test_no_dialog_means_focus_is_left_alone(tmp_path, monkeypatch):
    """The hold is scoped to an OPEN dialog: with none up, the transcript
    still takes focus when you click it, and keeps it."""
    engine = FakeEngine([])
    monkeypatch.setattr("doxa.app.notify_mod.notify", lambda *a, **k: None)
    app = DoxaApp(cwd=str(tmp_path), engine_factory=lambda: engine, new_session_factory=lambda: engine)
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.active_pane
        block_list = pane.query_one("#block-list", VerticalScroll)
        await pilot.click(block_list)
        await pilot.pause()
        assert app.focused is block_list

        # And once a dialog is answered, the hold is over.
        engine.push_peer_event(EngineEvent("needs_input", PERMISSION_DATA))
        assert await _wait(pilot, lambda: _popup(pane).is_open)
        await pilot.press("1")
        assert await _wait(pilot, lambda: bool(engine.needs_input_answers))
        await pilot.click(block_list)
        await pilot.pause()
        assert app.focused is block_list

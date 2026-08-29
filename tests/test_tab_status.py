# SPDX-License-Identifier: AGPL-3.0-only
"""Per-tab status colors: -working while a turn is in flight, -done-unseen
on a tab that finished a turn while you were looking elsewhere (cleared the
moment you look), and the -attention blink mechanism (timer discipline
only -- nothing wires needs_input=True yet; that is phase 2).

Same headless Pilot + FakeEngine pattern as tests/test_tabs.py.
"""

from __future__ import annotations

import asyncio

import pytest

from doxa.app import DoxaApp
from doxa.engine import EngineEvent
from textual.widgets import TabbedContent
from tests.fakes import FakeEngine

SCRIPT = [
    EngineEvent("turn_started", {}),
    EngineEvent("text_delta", {"text": "hi"}),
    EngineEvent("turn_done", {
        "cost_usd": 0.001, "duration_ms": 10, "is_error": False,
    }),
]


class GatedEngine(FakeEngine):
    """A FakeEngine whose turn holds open (after turn_started) until the
    test releases it -- the only way to observe -working mid-turn
    deterministically, since a plain FakeEngine.send() runs its whole
    script without ever yielding control back to the test."""

    def __init__(self, model: str = "claude-haiku-4-5") -> None:
        super().__init__([], model=model)
        self.release = asyncio.Event()

    async def send(self, prompt: str):
        self.received_prompts.append(prompt)
        yield EngineEvent("turn_started", {})
        await self.release.wait()
        self.total_cost_usd += 0.001
        yield EngineEvent(
            "turn_done", {"cost_usd": 0.001, "duration_ms": 5, "is_error": False}
        )


async def _wait(pilot, cond, tries=100):
    for _ in range(tries):
        if cond():
            return True
        await pilot.pause(0.02)
    return cond()


def _tab(app, pane):
    return app.query_one("#session-tabs", TabbedContent).get_tab(pane.tab_id)


# -- working -----------------------------------------------------------


@pytest.mark.asyncio
async def test_working_class_appears_during_the_turn_and_clears_after(tmp_path):
    engine = GatedEngine()
    app = DoxaApp(
        cwd=str(tmp_path), engine_factory=lambda: engine,
        new_session_factory=lambda: engine,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.active_pane
        assert await _wait(pilot, lambda: engine.started)

        pane.query_one("#prompt-input").value = "hi"
        await pilot.press("enter")
        assert await _wait(pilot, lambda: pane.turn_in_flight)
        tab = _tab(app, pane)
        assert await _wait(pilot, lambda: tab.has_class("-working"))

        engine.release.set()
        assert await _wait(pilot, lambda: not pane.turn_in_flight)
        assert not tab.has_class("-working")


# -- done-unseen ---------------------------------------------------------


@pytest.mark.asyncio
async def test_done_unseen_marks_a_background_tab_and_clears_on_activation(
    tmp_path,
):
    engines: list[FakeEngine] = []

    def make():
        engines.append(FakeEngine(list(SCRIPT)))
        return engines[-1]

    app = DoxaApp(cwd=str(tmp_path), engine_factory=make, new_session_factory=make)
    async with app.run_test() as pilot:
        await pilot.pause()
        first = app.active_pane
        assert await _wait(pilot, lambda: engines and engines[0].started)

        await pilot.press("ctrl+t")
        assert await _wait(pilot, lambda: len(app.panes()) == 2)
        assert await _wait(pilot, lambda: len(engines) == 2 and engines[1].started)
        second = app.panes()[1]

        # Back to the first tab -- the second is now the background one.
        await pilot.press("ctrl+left")
        await pilot.pause()
        assert app.active_pane is first

        # A turn finishes on the BACKGROUND tab (driven directly, as if
        # another attached client -- or an earlier keystroke -- drove it;
        # the assertion under test is what happens at turn_done, not how
        # the turn got started).
        await second._run_turn("hi from the background")

        tab_second = _tab(app, second)
        assert tab_second.has_class("-done-unseen")
        assert not _tab(app, first).has_class("-done-unseen")

        # Looking at it clears the dot.
        await pilot.press("ctrl+right")
        await pilot.pause()
        assert app.active_pane is second
        assert not tab_second.has_class("-done-unseen")


@pytest.mark.asyncio
async def test_a_finished_turn_never_pops_a_desktop_notification(
    tmp_path, monkeypatch,
):
    """v0.85.0, the core of the reported defect: "response finished
    should not trigger a desktop notification. Only when user input is
    required." A turn finishing on a BACKGROUND tab while the window is
    UNFOCUSED -- exactly the shape that used to fire notify_turn_done,
    the most permissive case for it to fire in -- must still mark
    -done-unseen (the in-window, non-desktop signal) but never reach
    doxa.notify.notify at all."""
    sent: list = []
    monkeypatch.setattr(
        "doxa.app.notify_mod.notify", lambda title, body: sent.append((title, body))
    )
    engines: list[FakeEngine] = []

    def make():
        engines.append(FakeEngine(list(SCRIPT)))
        return engines[-1]

    app = DoxaApp(cwd=str(tmp_path), engine_factory=make, new_session_factory=make)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.app_has_focus = False  # notify's own "auto" mode would fire here
        assert await _wait(pilot, lambda: engines and engines[0].started)
        await pilot.press("ctrl+t")
        assert await _wait(pilot, lambda: len(app.panes()) == 2)
        assert await _wait(pilot, lambda: len(engines) == 2 and engines[1].started)
        second = app.panes()[1]
        await pilot.press("ctrl+left")
        await pilot.pause()

        await second._run_turn("hi from the background")
        assert _tab(app, second).has_class("-done-unseen")
    assert sent == []


@pytest.mark.asyncio
async def test_done_unseen_is_never_set_on_the_active_tab(tmp_path):
    engine = FakeEngine(list(SCRIPT))
    app = DoxaApp(
        cwd=str(tmp_path), engine_factory=lambda: engine,
        new_session_factory=lambda: engine,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.active_pane
        assert await _wait(pilot, lambda: engine.started)

        pane.query_one("#prompt-input").value = "hi"
        await pilot.press("enter")
        assert await _wait(pilot, lambda: not pane.turn_in_flight)
        assert not _tab(app, pane).has_class("-done-unseen")


@pytest.mark.asyncio
async def test_a_fresh_turn_clears_a_stale_done_unseen_dot(tmp_path):
    """A tab that finished a turn unseen, then gets a SECOND turn started
    on it (without ever being activated in between) -- the new turn is
    itself "you are seeing this tab do something", so the old dot must not
    linger through it."""
    engine = FakeEngine(list(SCRIPT) + list(SCRIPT))
    app = DoxaApp(
        cwd=str(tmp_path), engine_factory=lambda: engine,
        new_session_factory=lambda: engine,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.active_pane
        assert await _wait(pilot, lambda: engine.started)

        await pane._run_turn("first")
        pane._set_tab_class("-done-unseen", True)  # simulate "seen from afar"
        assert _tab(app, pane).has_class("-done-unseen")

        await pane._run_turn("second")
        assert pane.turn_in_flight is False
        # The second turn started on the (still active) pane, which clears
        # the stale dot at _run_turn's very first line -- and since this
        # pane is active, turn_done never re-sets it either.
        assert not _tab(app, pane).has_class("-done-unseen")


# -- attention-blink infra (mechanism only -- nothing triggers it yet) ----


@pytest.mark.asyncio
async def test_needs_input_starts_and_stops_the_blink_timer(tmp_path):
    engine = FakeEngine([])
    app = DoxaApp(
        cwd=str(tmp_path), engine_factory=lambda: engine,
        new_session_factory=lambda: engine,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.active_pane
        assert pane.needs_input is False
        assert pane._attention_timer is None

        pane.set_needs_input(True)
        assert pane._attention_timer is not None

        pane.set_needs_input(False)
        assert pane._attention_timer is None
        assert not _tab(app, pane).has_class("-attention")


@pytest.mark.asyncio
async def test_needs_input_is_idempotent_and_never_stacks_timers(tmp_path):
    engine = FakeEngine([])
    app = DoxaApp(
        cwd=str(tmp_path), engine_factory=lambda: engine,
        new_session_factory=lambda: engine,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.active_pane
        pane.set_needs_input(True)
        timer = pane._attention_timer
        pane.set_needs_input(True)  # already on -- must not replace the timer
        assert pane._attention_timer is timer


@pytest.mark.asyncio
async def test_the_blink_toggles_the_attention_class(tmp_path):
    engine = FakeEngine([])
    app = DoxaApp(
        cwd=str(tmp_path), engine_factory=lambda: engine,
        new_session_factory=lambda: engine,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.active_pane
        tab = _tab(app, pane)
        pane.set_needs_input(True)
        assert not tab.has_class("-attention")  # starts off

        pane._blink_attention()
        assert tab.has_class("-attention")
        pane._blink_attention()
        assert not tab.has_class("-attention")
        pane.set_needs_input(False)


@pytest.mark.asyncio
async def test_tab_activation_clears_needs_input_and_its_timer(tmp_path):
    engines: list[FakeEngine] = []

    def make():
        engines.append(FakeEngine([]))
        return engines[-1]

    app = DoxaApp(cwd=str(tmp_path), engine_factory=make, new_session_factory=make)
    async with app.run_test() as pilot:
        await pilot.pause()
        first = app.active_pane
        await pilot.press("ctrl+t")
        assert await _wait(pilot, lambda: len(app.panes()) == 2)
        second = app.panes()[1]

        second.set_needs_input(True)
        assert second._attention_timer is not None

        await pilot.press("ctrl+left")  # activates `first`, not `second`
        await pilot.pause()
        assert app.active_pane is first
        assert second._attention_timer is not None  # untouched: not activated

        await pilot.press("ctrl+right")  # now activate `second`
        await pilot.pause()
        assert app.active_pane is second
        assert second._attention_timer is None
        assert second.needs_input is False
        assert not _tab(app, second).has_class("-attention")


@pytest.mark.asyncio
async def test_a_new_tab_starts_with_zero_status_timers(tmp_path):
    """The idle-CPU discipline this app already follows elsewhere: opening
    a tab must not, by itself, start any timer -- attention is opt-in and
    only ever runs between set_needs_input(True) and its matching False."""
    engine = FakeEngine([])
    app = DoxaApp(
        cwd=str(tmp_path), engine_factory=lambda: engine,
        new_session_factory=lambda: engine,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.active_pane
        assert pane._attention_timer is None

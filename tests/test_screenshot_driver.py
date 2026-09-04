# SPDX-License-Identifier: AGPL-3.0-only
"""scripts/screenshot.py's own driver helpers, against a real Pilot.

The gallery script is the only thing that regenerates assets/shots, so
until v1.7.1 the only thing that tested it was running it -- and its one
genuine defect was invisible that way, because it did not fail the script,
it made the script SLOW and flaky: `_activate` switched the visible tab
and said nothing about where the keyboard went, which is the one thing
v0.38.0 forbids and every caller in doxa/app.py obeys.

What that cost, measured at v1.7.1 (textual 5.3.0): hiding the TabPane the
keyboard is in makes Textual re-home focus (`Screen._reset_focus`) onto
another focusable widget INSIDE that same just-hidden pane; focusing a
widget inside a TabPane re-ACTIVATES it
(`TabbedContent._on_tab_pane_focused`), hiding the other tab;
`DoxaApp._on_tab_activated` then moves the keyboard into the newly active
tab's prompt -- back inside whichever pane is about to be hidden next.
The window never goes idle again. Since `_fill_hero_conversation` calls
`_activate` and nearly every scene calls `_fill_hero_conversation`, every
later `pilot.pause`-polled wait in the gallery was racing a busy pump:
`split-panes` failed about half its runs, the whole gallery could not
complete, and `live-diff` intermittently saw "no turn in flight".

So the helper gets a test of its own, at the level the defect lived at.
"""
from __future__ import annotations

import pytest

from doxa import config as config_mod
from doxa.app import DoxaApp, SessionPane
from doxa.ui.prompt import PromptInput
from scripts import screenshot
from tests.fakes import FakeEngine


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "doxa-home"))
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    config_mod.invalidate()
    yield
    config_mod.invalidate()


def _app(tmp_path):
    def make() -> FakeEngine:
        return FakeEngine([])

    return DoxaApp(
        cwd=str(tmp_path), engine_factory=make, new_session_factory=make,
    )


async def _wait(pilot, cond, tries=200):
    for _ in range(tries):
        if cond():
            return True
        await pilot.pause(0.02)
    return cond()


def _prompt_of(pane: SessionPane) -> PromptInput:
    return pane.query_one("#prompt-input", PromptInput)


async def _three_tabs(app: DoxaApp, pilot):
    await pilot.pause()
    await app.action_new_tab()
    await pilot.pause()
    await app.action_new_tab()
    assert await _wait(pilot, lambda: len(app.panes()) == 3)
    await pilot.pause()
    return app.panes()


@pytest.mark.asyncio
async def test_activate_takes_the_keyboard_with_it(tmp_path):
    """The regression. Fails deterministically before the fix.

    The keyboard must arrive AND STAY. Arrival alone is not the assertion
    to make here: while the activation/focus loop was running, focus
    visited this very prompt every other turn on its way past, so a
    one-shot `app.focused is ...` poll passed on the broken code. Twenty
    consecutive quiet turns is the difference between "the keyboard is
    here" and "the keyboard is going round in circles through here"."""
    app = _app(tmp_path)
    async with app.run_test(size=(160, 48)) as pilot:
        panes = await _three_tabs(app, pilot)
        first = panes[0]
        assert app.focused is not _prompt_of(first)

        screenshot._activate(app, first)

        assert await _wait(pilot, lambda: app.focused is _prompt_of(first))
        assert app.active_pane is first
        for _ in range(20):
            await pilot.pause(0.02)
            assert app.focused is _prompt_of(first), (
                "the keyboard left the tab _activate switched to"
            )


@pytest.mark.asyncio
async def test_activate_leaves_the_window_idle(tmp_path):
    """The same fix stated as the symptom it actually caused: after
    `_activate`, nothing keeps moving the keyboard on its own.

    Counted rather than timed, so it is deterministic: focus moves during
    message-pump turns in which the test asks the app for nothing. Before
    the fix this was ~90 and unbounded; a settled window makes none."""
    app = _app(tmp_path)
    async with app.run_test(size=(160, 48)) as pilot:
        panes = await _three_tabs(app, pilot)

        screenshot._activate(app, panes[0])

        # Let the deliberate move land, then watch a quiet window.
        for _ in range(10):
            await pilot.pause(0.02)
        seen = [0]
        screen = app.screen
        original = screen.set_focus

        def counting(widget, scroll_visible=True, from_app_focus=False):
            seen[0] += 1
            return original(
                widget,
                scroll_visible=scroll_visible,
                from_app_focus=from_app_focus,
            )

        screen.set_focus = counting  # type: ignore[method-assign]
        for _ in range(40):
            await pilot.pause(0.02)

        assert seen[0] == 0, (
            f"{seen[0]} unasked-for focus moves in 40 idle turns -- the "
            f"activation/focus loop is back"
        )

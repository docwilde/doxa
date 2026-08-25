# SPDX-License-Identifier: AGPL-3.0-only
"""Chrome: the closeable inspector, tab cycling, /help's hotkeys, no animation.

The animation assertions are the load-bearing ones. DOXA already paid once
for a leaked 16 Hz repaint timer (idle CPU grew linearly with scrollback),
and the fix then was to stop the widget on hide. The fix now is stronger:
the widget is gone. Nothing in DOXA's own chrome animates, and these tests
say so in the two places it could regress -- an in-flight turn, and every
overlay the app can put on screen.
"""

from __future__ import annotations

import pytest

from doxa.app import (
    BeliefInspector,
    ClockChip,
    DoxaApp,
    ThinkingMarker,
    TurnBlock,
    app_bindings,
    help_text,
)
from doxa.engine import EngineEvent
from tests.fakes import FakeEngine

SCRIPT = [
    EngineEvent("turn_started", {}),
    EngineEvent("text_delta", {"text": "ok"}),
    EngineEvent("turn_done", {"cost_usd": 0.0, "duration_ms": 5, "is_error": False}),
]


async def _app(monkeypatch, tmp_path, script=None):
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    engines: list[FakeEngine] = []

    def make():
        engines.append(FakeEngine(list(script or [])))
        return engines[-1]

    app = DoxaApp(cwd=str(tmp_path), engine_factory=make, new_session_factory=make)
    return app, engines


# -- (a) the inspector closes ---------------------------------------------


@pytest.mark.asyncio
async def test_inspector_has_a_clickable_close(monkeypatch, tmp_path):
    app, _e = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        panel = app.query_one("#belief-inspector", BeliefInspector)
        assert panel.display is False

        app.action_toggle_inspector()
        await pilot.pause()
        assert panel.display is True
        assert "3 active beliefs" in panel.text

        # The ✕ exists, is inside the panel, and closes it on click.
        close = app.query_one("#inspector-close")
        assert close in panel.query("*")
        await pilot.click("#inspector-close")
        await pilot.pause()
        assert panel.display is False

        # ...and the key toggle still works alongside it.
        app.action_toggle_inspector()
        await pilot.pause()
        assert panel.display is True


# -- (c) tab cycling ------------------------------------------------------


@pytest.mark.asyncio
async def test_ctrl_arrows_cycle_tabs_both_ways_with_wraparound(
    monkeypatch, tmp_path
):
    app, engines = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(2):
            await pilot.press("ctrl+t")
            await pilot.pause(0.05)
        for _ in range(100):
            if len(app.panes()) == 3:
                break
            await pilot.pause(0.02)
        panes = app.panes()
        assert len(panes) == 3
        ids = [p.id for p in panes]
        tabbed = app.query_one("#session-tabs")
        assert tabbed.active == ids[2]  # the newest tab is focused

        await pilot.press("ctrl+right")   # wraps forward to the first
        await pilot.pause()
        assert tabbed.active == ids[0]

        await pilot.press("ctrl+left")    # wraps backward to the last
        await pilot.pause()
        assert tabbed.active == ids[2]

        await pilot.press("ctrl+left")
        await pilot.pause()
        assert tabbed.active == ids[1]


@pytest.mark.asyncio
async def test_tab_cycling_is_a_no_op_with_one_tab(monkeypatch, tmp_path):
    app, _e = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        before = app.query_one("#session-tabs").active
        await pilot.press("ctrl+right")
        await pilot.press("ctrl+left")
        await pilot.pause()
        assert app.query_one("#session-tabs").active == before


def _tab_marker(app):
    """(underline, start, end, expected_span) for the active tab."""
    from textual.widgets import Tabs
    from textual.widgets._tabs import Underline

    tabs = app.query_one("#session-tabs").query_one(Tabs)
    underline = tabs.query_one(Underline)
    active = tabs.query_one("#tabs-list > Tab.-active")
    span = active.virtual_region.shrink(active.styles.gutter).column_span
    return underline, underline.highlight_start, underline.highlight_end, span


@pytest.mark.asyncio
async def test_switching_tabs_arms_no_animation(monkeypatch, tmp_path):
    """The reported "tab switching is laggy": Textual's Tabs SLIDES its
    underline to the new tab (0.3 s animation, armed behind a 0.02 s
    timer). Measured at ~290-345 ms of extra wall time per switch,
    independent of scrollback. DOXA turns Textual's animation off wholesale
    -- so a switch must leave nothing animating behind it."""
    from textual.widgets._tabs import Underline

    app, _e = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.animation_level == "none"
        for _ in range(2):
            await pilot.press("ctrl+t")
            await pilot.pause(0.05)
        assert len(app.panes()) == 3

        underline = app.query_one(Underline)
        await pilot.press("ctrl+right")
        # Same frame as the activation: nothing animating, nothing armed.
        for attribute in ("highlight_start", "highlight_end"):
            assert not app.animator.is_being_animated(underline, attribute)
        assert app.animator._animations == {}
        assert _armed(app) == []


@pytest.mark.asyncio
async def test_the_tab_marker_lands_on_the_switch_frame(monkeypatch, tmp_path):
    """Not merely un-animated: AT its new position immediately. Textual's
    no-animate path still defers the move to call_after_refresh, one frame
    late; DOXA places the marker itself as the tab activates."""
    app, _e = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(2):
            await pilot.press("ctrl+t")
            await pilot.pause(0.05)
        await pilot.pause(0.1)

        app._cycle_tab(1)  # exactly what Ctrl+→ calls
        # No pause: the reactives are already at their final values.
        _underline, start, end, span = _tab_marker(app)
        assert (start, end) == span
        assert end > start

        await pilot.pause(0.3)  # ...and they stay there, unmoved
        _underline, settled_start, settled_end, _span = _tab_marker(app)
        assert (settled_start, settled_end) == (start, end)


# -- (b) /help carries the bindings ---------------------------------------


def test_help_lists_every_app_binding_exactly_once():
    """Generated from BINDINGS itself: a binding cannot exist without being
    documented, and cannot be documented without existing."""
    text = help_text()
    assert "hotkeys (no slash form)" in text
    from doxa import commands
    from doxa.app import _pretty_key

    bound = {c.binding for c in commands.REGISTRY if c.binding}
    for key, description in app_bindings():
        if key in bound:
            # Documented on its command's row instead, with its key.
            assert _pretty_key(key) in text, key
        else:
            assert description in text, key
    # The commands section still carries every registry row.
    for command in commands.REGISTRY:
        assert command.call_form() in text


def test_help_shows_a_commands_binding_beside_its_command():
    """A command that also has a key shows the key on its own row, and does
    NOT get repeated in the hotkeys section."""
    text = help_text()
    command_section, hotkey_section = text.split("hotkeys (no slash form)")
    assert "/settings" in command_section and "Ctrl+," in command_section
    assert "Ctrl+," not in hotkey_section


def test_help_documents_the_tab_and_quit_keys():
    text = help_text()
    for key in ("Ctrl+←", "Ctrl+→", "Ctrl+T", "Ctrl+W", "Ctrl+R", "Ctrl+P"):
        assert key in text, key
    assert "twice = stop" in text  # the Ctrl+C double-press semantics


# -- (d) nothing animates -------------------------------------------------


def test_the_in_flight_marker_is_static():
    block = TurnBlock("hi")
    assert isinstance(block.thinking, ThinkingMarker)
    assert block.thinking.auto_refresh is None
    from textual.widgets import LoadingIndicator

    assert not isinstance(block.thinking, LoadingIndicator)


@pytest.mark.asyncio
async def test_no_armed_timers_while_a_turn_is_in_flight(monkeypatch, tmp_path):
    """The regression that mattered: the old indicator armed its 16 Hz
    animation the moment a turn started. Nothing arms one now -- not
    during the turn, and not after it."""
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))

    import asyncio

    class SlowEngine(FakeEngine):
        async def send(self, prompt):
            yield EngineEvent("turn_started", {})
            await asyncio.sleep(0.6)  # a turn genuinely in flight
            yield EngineEvent("turn_done", {"cost_usd": 0.0})

    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: SlowEngine([]))
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#prompt-input").value = "go"
        await pilot.press("enter")

        # Sampled across the whole turn rather than at one instant: under
        # load the turn can finish between two pauses, and a timing race
        # is not what this test is about.
        saw_in_flight = False
        for _ in range(400):
            blocks = list(app.query(TurnBlock))
            if blocks:
                assert _armed(app) == []
                if blocks[0].thinking.display:
                    saw_in_flight = True
                elif saw_in_flight:
                    break
            await pilot.pause(0.02)
        assert saw_in_flight, "the marker never showed -- nothing was sampled"
        assert _armed(app) == []


@pytest.mark.asyncio
async def test_markdown_stream_leaves_no_running_task_after_turn_done(
    monkeypatch, tmp_path
):
    """v0.13.0's streaming Markdown body (item c) arms a background
    asyncio.Task while a turn's text is in flight -- NOT a Textual
    auto_refresh timer, so ``_armed()`` above cannot see it -- and
    mark_done() must stop it the same way hide_thinking() already
    guarantees no leaked timer. One completed turn, zero live tasks
    behind its stream."""
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setattr(
        "doxa.app.SessionEngine", lambda cwd, model=None: FakeEngine(list(SCRIPT))
    )
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#prompt-input").value = "go"
        await pilot.press("enter")
        for _ in range(100):
            blocks = list(app.query(TurnBlock))
            if blocks and "5ms" in str(blocks[0].title):  # SCRIPT's duration_ms
                break
            await pilot.pause(0.02)
        block = app.query_one(TurnBlock)
        assert block._stream is not None  # this turn DID stream text
        assert block._stream._stopped is True
        assert block._stream._task is None


def _armed(app) -> list:
    """Every node with a live ``_auto_refresh_timer`` -- except
    :class:`ClockChip` (item M), the ONE permitted standing timer in this
    app's chrome. It is boundary-aligned and self-rescheduling (see its
    docstring in doxa/app.py), never a fixed-Hz repaint, and disabled
    config leaves it with no timer at all (test_clock.py covers that off
    switch directly) -- so excluding it here is excluding a timer that
    was deliberately built to be the one exception, not widening what
    this guard tolerates. Anything else armed still fails the test."""
    return [
        node for node in app.query("*")
        if not isinstance(node, ClockChip)
        and getattr(node, "_auto_refresh_timer", None) is not None
    ]


@pytest.mark.asyncio
async def test_no_armed_timers_with_every_overlay_open(monkeypatch, tmp_path):
    """Each overlay the app can raise, checked for animation: the
    inspector, the settings modal, the history modal, the autocomplete."""
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "doxa-home"))
    monkeypatch.setattr(
        "doxa.app.SessionEngine", lambda cwd, model=None: FakeEngine(SCRIPT)
    )
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        for i in range(3):
            app.query_one("#prompt-input").value = f"turn {i}"
            await pilot.press("enter")
            for _ in range(100):
                if len(list(app.query(TurnBlock))) == i + 1:
                    break
                await pilot.pause(0.02)
        assert _armed(app) == []

        app.action_toggle_inspector()
        await pilot.pause()
        assert _armed(app) == []

        await pilot.press("slash")  # autocomplete dropdown
        await pilot.pause()
        assert _armed(app) == []
        await pilot.press("escape")

        app.action_settings()
        for _ in range(100):
            from doxa.settings import SettingsScreen

            if isinstance(app.screen, SettingsScreen):
                break
            await pilot.pause(0.02)
        assert _armed(app) == []
        await pilot.press("escape")
        for _ in range(100):
            from doxa.settings import SettingsScreen

            if not isinstance(app.screen, SettingsScreen):
                break
            await pilot.pause(0.02)
        await pilot.pause()

        await pilot.press("ctrl+r")  # the /search popup, not a modal now
        from doxa.history import SessionSearch

        popup = app.query_one("#session-search", SessionSearch)
        for _ in range(200):
            if popup.is_open:
                break
            await pilot.pause(0.02)
        assert popup.is_open is True
        assert _armed(app) == []
        await pilot.press("escape")
        await pilot.pause()

        # ...and the tab bar, whose underline is the one piece of chrome
        # DOXA does not draw itself: Textual animates it unless told not to.
        await pilot.press("ctrl+t")
        for _ in range(100):
            if len(app.panes()) == 2:
                break
            await pilot.pause(0.02)
        await pilot.press("ctrl+left")
        assert _armed(app) == []
        assert app.animator._animations == {}


def test_no_animated_widget_types_are_imported_by_the_app():
    """A guard against reintroduction: the app module must not pull in
    Textual's animated widgets at all."""
    import doxa.app as app_mod

    for name in ("LoadingIndicator", "ProgressBar", "Sparkline"):
        assert not hasattr(app_mod, name), name


def test_the_theme_declares_no_transitions():
    from pathlib import Path

    css = (Path(__file__).resolve().parents[1] / "doxa" / "theme.tcss").read_text()
    for line in css.splitlines():
        stripped = line.strip()
        if stripped.startswith("/*") or stripped.startswith("*"):
            continue
        assert not stripped.startswith("transition:"), line


# -- (e) the clock (item M) -------------------------------------------------


@pytest.mark.asyncio
async def test_clock_shows_by_default_and_is_the_only_armed_timer(monkeypatch, tmp_path):
    app, _ = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        chip = app.query_one(ClockChip)
        assert chip.display is True
        assert chip.renderable  # something is actually painted, not ""
        assert chip._auto_refresh_timer is not None
        # It is the ONLY thing armed -- _armed() already excludes it by
        # type, so an empty result here means nothing ELSE is running.
        assert _armed(app) == []


@pytest.mark.asyncio
async def test_clock_disabled_arms_nothing_at_all(monkeypatch, tmp_path):
    monkeypatch.setenv("DOXA_CLOCK_SHOW", "0")
    app, _ = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        chip = app.query_one(ClockChip)
        assert chip.display is False
        assert chip._auto_refresh_timer is None
        # Unlike every other assertion in this file, this one does NOT
        # exclude ClockChip -- with the clock off, _auto_refresh_timer
        # must be bare on EVERY node, the chip included.
        armed = [
            node for node in app.query("*")
            if getattr(node, "_auto_refresh_timer", None) is not None
        ]
        assert armed == []


@pytest.mark.asyncio
async def test_clock_seconds_shown_still_arms_exactly_one_timer(monkeypatch, tmp_path):
    """Turning seconds on changes the boundary the timer re-aligns to
    (second- instead of minute-), never the COUNT of timers."""
    monkeypatch.setenv("DOXA_CLOCK_SECONDS", "1")
    app, _ = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        chip = app.query_one(ClockChip)
        assert chip.cfg.show_seconds is True
        assert chip._auto_refresh_timer is not None
        assert chip.auto_refresh <= 1.0 + 1e-6  # second-aligned, not minute
        assert _armed(app) == []


@pytest.mark.asyncio
async def test_clock_never_reserves_width_from_the_tab_bar(monkeypatch, tmp_path):
    """The load-bearing layout claim: docking the clock on its OWN layer
    (theme.tcss) means the tab bar and the pane body keep their full
    screen width -- the clock paints over the corner, it does not shrink
    anything to make room for itself."""
    from textual.widgets import TabbedContent

    app, _ = await _app(monkeypatch, tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        tabs = app.query_one("#session-tabs", TabbedContent)
        chip = app.query_one(ClockChip)
        assert tabs.region.width == 80
        assert chip.region.width < 80  # the chip itself IS fixed-width
        assert chip.region.right == tabs.region.right  # flush to the edge


@pytest.mark.asyncio
async def test_clicking_the_tab_bar_away_from_the_clock_still_hits_a_tab(monkeypatch, tmp_path):
    """The other half of the layout claim: the clock's hit box is only
    its own painted text, so a click on the tab bar anywhere else still
    reaches the tab underneath -- the overlay layer does not swallow
    input for space it isn't actually drawing into."""
    app, _ = await _app(monkeypatch, tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        widget, _offset = app.get_widget_at(2, 0)  # far left of the tab bar
        assert not isinstance(widget, ClockChip)


@pytest.mark.asyncio
async def test_settings_save_reconfigures_the_clock_live(monkeypatch, tmp_path):
    from doxa import config as config_mod
    from doxa.settings import SettingsScreen, field_id

    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "doxa-home"))
    app, _ = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.query_one(ClockChip).display is True
        await pilot.press("ctrl+comma")
        for _ in range(100):
            if isinstance(app.screen, SettingsScreen):
                break
            await pilot.pause(0.02)
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        screen.query_one(f"#{field_id('clock_show')}").value = "0"
        await pilot.press("ctrl+s")  # writes the file, modal stays open
        await pilot.pause()
        await pilot.press("escape")  # dismiss(True) -- what fires _saved
        for _ in range(100):
            if not isinstance(app.screen, SettingsScreen):
                break
            await pilot.pause(0.02)
        chip = app.query_one(ClockChip)
        assert chip.display is False
        assert chip._auto_refresh_timer is None
        config_mod.invalidate()


@pytest.mark.asyncio
async def test_clock_tooltip_carries_the_visible_error_for_a_bad_timezone(monkeypatch, tmp_path):
    monkeypatch.setenv("DOXA_CLOCK_TZ", "Not/A_Real_Zone")
    app, _ = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        chip = app.query_one(ClockChip)
        assert chip.tooltip is not None
        assert "Not/A_Real_Zone" in chip.tooltip
        assert chip.renderable  # still rendered -- the fallback, not a blank chip

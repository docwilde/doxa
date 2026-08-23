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
            await asyncio.sleep(0.25)  # a turn genuinely in flight
            yield EngineEvent("turn_done", {"cost_usd": 0.0})

    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: SlowEngine([]))
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#prompt-input").value = "go"
        await pilot.press("enter")
        for _ in range(100):
            if list(app.query(TurnBlock)):
                break
            await pilot.pause(0.02)
        block = list(app.query(TurnBlock))[0]
        assert block.thinking.display is True  # the marker IS showing
        assert _armed(app) == []               # ...and costs nothing

        for _ in range(200):
            if block.thinking.display is False:
                break
            await pilot.pause(0.02)
        assert _armed(app) == []


def _armed(app) -> list:
    return [
        node for node in app.query("*")
        if getattr(node, "_auto_refresh_timer", None) is not None
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

        await pilot.press("ctrl+r")
        from doxa.history import HistorySearchScreen

        assert isinstance(app.screen, HistorySearchScreen)
        assert _armed(app) == []


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

"""Tab-system tests (Phase 3, the README sketch): SessionPane extraction
under a TabbedContent -- one engine handle per tab. Covers the lifecycle
headlines: Ctrl+T spawns a fresh session (new_session_factory) in a new
tab without touching the first tab's engine; Ctrl+W close-DETACHES only
the active tab; closing the last tab closes the app on detach semantics;
per-tab status lines and identity blocks stay independent; app-level
Ctrl+C detaches ALL tabs (stop-all on double press); and the palette
carries the tab surface (new/close/picker). Headless Pilot + FakeEngine,
same pattern as tests/test_app.py.
"""

from __future__ import annotations

import pytest

from doxa.app import DoxaApp, SessionPane, SystemBlock, TurnBlock
from doxa.engine import EngineEvent
from tests.fakes import FakeEngine

SCRIPT = [
    EngineEvent("turn_started", {}),
    EngineEvent("text_delta", {"text": "hello from this tab"}),
    EngineEvent("turn_done", {
        "cost_usd": 0.001, "duration_ms": 10, "is_error": False,
        "session_cost_usd": 0.001, "ctx_percentage": 2.0,
    }),
]


def _tracked_factories():
    """(engine_factory, new_session_factory, engines): every build appends,
    so tests can tell WHICH factory fed which tab."""
    engines: list[FakeEngine] = []

    def make() -> FakeEngine:
        engines.append(FakeEngine(list(SCRIPT)))
        return engines[-1]

    return make, make, engines


def _app(tmp_path):
    engine_factory, new_session_factory, engines = _tracked_factories()
    app = DoxaApp(
        cwd=str(tmp_path),
        engine_factory=engine_factory,
        new_session_factory=new_session_factory,
    )
    return app, engines


async def _wait(pilot, cond, tries=100):
    for _ in range(tries):
        if cond():
            return True
        await pilot.pause(0.02)
    return cond()


@pytest.mark.asyncio
async def test_new_tab_spawns_second_engine_and_keeps_the_first(tmp_path):
    app, engines = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert len(app.panes()) == 1
        assert await _wait(pilot, lambda: engines and engines[0].started)

        await pilot.press("ctrl+t")
        assert await _wait(pilot, lambda: len(app.panes()) == 2)
        assert await _wait(pilot, lambda: len(engines) == 2 and engines[1].started)
        # The new tab is active and drives the NEW engine; tab one's engine
        # is untouched (no finalize, no restart).
        assert app.engine is engines[1]
        assert engines[0].started and not engines[0].finalized
        # Each pane holds its own handle.
        assert [p.engine for p in app.panes()] == engines


@pytest.mark.asyncio
async def test_per_tab_status_and_identity_are_independent(tmp_path):
    app, engines = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+t")
        assert await _wait(pilot, lambda: len(engines) == 2 and engines[1].started)

        # Distinct accounts per engine -> distinct status lines per pane.
        engines[0].account = {"subscriptionType": "Claude Max"}
        engines[1].account = {}
        engines[1].total_cost_usd = 0.42
        for pane in app.panes():
            pane._refresh_status()
        first, second = app.panes()
        assert "sub:max" in str(first.query_one("#status-bar").renderable)
        second_status = str(second.query_one("#status-bar").renderable)
        assert "sub:" not in second_status and "$0.4200" in second_status

        # One identity block per session, each inside its own pane.
        assert await _wait(
            pilot,
            lambda: all(p.query("#identity-block") for p in app.panes()),
        )


@pytest.mark.asyncio
async def test_turns_land_in_their_own_tab_only(tmp_path):
    app, engines = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+t")
        assert await _wait(pilot, lambda: len(engines) == 2 and engines[1].started)
        second = app.panes()[1]

        second.query_one("#prompt-input").value = "hi tab two"
        await pilot.press("enter")
        assert await _wait(
            pilot,
            lambda: [b.assistant_text for b in second.query(TurnBlock)]
            == ["hello from this tab"],
        )
        first = app.panes()[0]
        assert not list(first.query(TurnBlock))


@pytest.mark.asyncio
async def test_close_tab_detaches_only_that_session(tmp_path):
    app, engines = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+t")
        assert await _wait(pilot, lambda: len(engines) == 2 and engines[1].started)
        assert app.engine is engines[1]

        await pilot.press("ctrl+w")  # close the active (second) tab
        assert await _wait(pilot, lambda: len(app.panes()) == 1)
        # Close-detach: the closed tab's engine handle was finalized
        # (detach over a daemon client), its neighbor untouched.
        assert engines[1].finalized is True
        assert engines[0].finalized is False
        assert app.engine is engines[0]


@pytest.mark.asyncio
async def test_closing_the_last_tab_closes_the_app_detached(tmp_path):
    app, engines = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert await _wait(pilot, lambda: engines and engines[0].started)
        await pilot.press("ctrl+w")
        await pilot.pause()
    # App exited via the detach path: the engine was finalized (which for a
    # daemon client merely detaches), never stop()ped.
    assert engines[0].finalized is True


@pytest.mark.asyncio
async def test_ctrl_c_detaches_all_tabs(tmp_path):
    from doxa.app import CTRL_C_DOUBLE_SECS

    app, engines = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+t")
        assert await _wait(pilot, lambda: len(engines) == 2 and engines[1].started)
        await pilot.press("ctrl+c")
        assert engines[0].finalized is False  # window armed, nothing detached yet
        await pilot.pause(CTRL_C_DOUBLE_SECS + 0.5)
    assert engines[0].finalized is True
    assert engines[1].finalized is True


@pytest.mark.asyncio
async def test_double_ctrl_c_stops_all_tabs(tmp_path):
    stopped: list[str] = []

    class StoppableEngine(FakeEngine):
        def __init__(self, name: str):
            super().__init__([])
            self.name = name

        async def stop(self):
            stopped.append(self.name)

    serial = iter(("one", "two"))
    app = DoxaApp(
        cwd=str(tmp_path),
        engine_factory=lambda: StoppableEngine(next(serial)),
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+t")
        await _wait(pilot, lambda: len(app.panes()) == 2)
        await pilot.press("ctrl+c")
        await pilot.press("ctrl+c")
        await pilot.pause()
    assert sorted(stopped) == ["one", "two"]


@pytest.mark.asyncio
async def test_palette_carries_tab_surface_and_picker(tmp_path):
    app, engines = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        from doxa.palette import SECTION_TABS

        names = [entry.label for entry in app.doxa_commands()]
        assert "New tab" in names and "Close tab" in names
        # Every open tab is listed -- including the one you are on, marked
        # as active: "where am I" is a question the palette should answer.
        tabs = [e for e in app.doxa_commands() if e.section == SECTION_TABS]
        assert len(tabs) == 1 and tabs[0].label.endswith("· active")

        await pilot.press("ctrl+t")
        assert await _wait(pilot, lambda: len(app.panes()) == 2)
        tabs = [e for e in app.doxa_commands() if e.section == SECTION_TABS]
        assert len(tabs) == 2
        assert [t.label.endswith("· active") for t in tabs] == [False, True]

        # The entry for the other tab actually switches the active tab.
        tabs[0].callback()
        await pilot.pause()
        assert app.engine is engines[0]


@pytest.mark.asyncio
async def test_palette_stop_session_is_tab_scoped(tmp_path):
    class StoppableEngine(FakeEngine):
        def __init__(self):
            super().__init__([])
            self.stopped = False

        async def stop(self):
            self.stopped = True

    engines: list[StoppableEngine] = []

    def make():
        engines.append(StoppableEngine())
        return engines[-1]

    app = DoxaApp(cwd=str(tmp_path), engine_factory=make, new_session_factory=make)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+t")
        assert await _wait(pilot, lambda: len(engines) == 2 and engines[1].started)
        app._cmd_stop_active()  # palette 'Quit: stop session' on tab two
        assert await _wait(pilot, lambda: len(app.panes()) == 1)
        assert engines[1].stopped is True
        assert engines[0].stopped is False
        assert app.engine is engines[0]


@pytest.mark.asyncio
async def test_slash_commands_stay_pane_scoped(tmp_path):
    """A /img usage error in tab two lands in tab two's block list, not in
    tab one's -- the input handler lives on the pane."""
    app, engines = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+t")
        assert await _wait(pilot, lambda: len(engines) == 2 and engines[1].started)
        second = app.panes()[1]
        second.query_one("#prompt-input").value = "/img"
        await pilot.press("enter")

        def _sys_blocks(pane):
            return [b for b in pane.query(SystemBlock) if b.id != "identity-block"]

        assert await _wait(pilot, lambda: _sys_blocks(second))
        assert "usage: /img" in _sys_blocks(second)[0].text
        assert not _sys_blocks(app.panes()[0])

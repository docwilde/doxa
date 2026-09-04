# SPDX-License-Identifier: AGPL-3.0-only
"""Focus ownership (v0.38.0): focus follows EXPLICIT user intent, and
activation is no longer a side effect of a pane mounting.

The defect these lock down: ``SessionPane.on_mount`` used to focus its own
prompt, and focusing a widget inside a ``TabPane`` ACTIVATES that pane
(``TabbedContent._on_tab_pane_focused``). So "which tab is active" was
decided by Textual's mount scheduling rather than by the keystroke that
opened the tab -- observable as the v0.23.0 restored-active-tab defect
(three restored tabs always landed on the last one), and as a ~17%
standalone flake in tests/test_tab_status.py's done-unseen test.

Every assertion here is about the user-visible outcome -- which tab is
active, and which widget has the keyboard -- never about the mechanism
that produced it. Headless Pilot + FakeEngine, same pattern as
tests/test_tabs.py.
"""

from __future__ import annotations

import pytest

from doxa import config as config_mod
from doxa.app import ArchivedSessionTab, DoxaApp, RestoreTabSpec, SessionPane
from doxa.ui.prompt import PromptInput
from textual.widgets import TabbedContent
from tests.fakes import FakeEngine


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    """Own DOXA_HOME + runtime dir: nothing here may read or write the
    developer's real tabsets/registry state (same guard tests/
    test_tabsets.py installs, and for the same reason -- the restore
    cases below persist)."""
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "doxa-home"))
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    config_mod.invalidate()
    yield
    config_mod.invalidate()


def _tracked():
    engines: list[FakeEngine] = []

    def make() -> FakeEngine:
        engines.append(FakeEngine([]))
        return engines[-1]

    return make, engines


def _restore_factory(session_id: str):
    def make() -> FakeEngine:
        engine = FakeEngine([])
        engine.session_id = session_id
        return engine

    return make


def _app(tmp_path):
    make, engines = _tracked()
    return DoxaApp(
        cwd=str(tmp_path), engine_factory=make, new_session_factory=make,
    ), engines


async def _wait(pilot, cond, tries=200):
    for _ in range(tries):
        if cond():
            return True
        await pilot.pause(0.02)
    return cond()


def _prompt_of(pane: SessionPane) -> PromptInput:
    return pane.query_one("#prompt-input", PromptInput)


def _tabbed(app: DoxaApp) -> TabbedContent:
    return app.query_one("#session-tabs", TabbedContent)


# -- startup ------------------------------------------------------------


@pytest.mark.asyncio
async def test_startup_opens_with_the_first_prompt_focused(tmp_path):
    """The keyboard is IN the prompt when the window opens.

    This used to be true only because the one pane focused itself on
    mount. With that gone, startup says so on purpose
    (DoxaApp._activate_initial_tab) -- and the measured reason it says so
    rather than leaving it to Textual is in that method's docstring."""
    app, _engines = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.active_pane
        assert pane is not None
        assert app.focused is _prompt_of(pane)
        assert _tabbed(app).active == pane.tab_id


@pytest.mark.asyncio
async def test_startup_types_straight_into_the_prompt(tmp_path):
    """The same thing said the way a user would notice it: the first
    keystroke after launch is text in the prompt, not a lost key."""
    app, _engines = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("h", "i")
        assert _prompt_of(app.active_pane).text == "hi"


# -- Ctrl+T -------------------------------------------------------------


@pytest.mark.asyncio
async def test_ctrl_t_activates_and_focuses_the_new_prompt(tmp_path):
    app, _engines = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        first = app.active_pane

        await pilot.press("ctrl+t")
        assert await _wait(pilot, lambda: len(app.panes()) == 2)
        second = app.panes()[1]

        assert second is not first
        assert app.active_pane is second
        assert app.focused is _prompt_of(second)


@pytest.mark.asyncio
async def test_ctrl_t_then_typing_lands_in_the_new_tab_only(tmp_path):
    app, _engines = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        first = app.active_pane

        await pilot.press("ctrl+t")
        assert await _wait(pilot, lambda: len(app.panes()) == 2)
        await pilot.press("y", "o")

        second = app.panes()[1]
        assert _prompt_of(second).text == "yo"
        assert _prompt_of(first).text == ""


# -- a background mount stays in the background -------------------------


@pytest.mark.asyncio
async def test_a_pane_mounted_without_activating_stays_in_the_background(
    tmp_path,
):
    """Mounting a pane is not the same act as switching to it.

    Before v0.38.0 it was: the new pane focused its own prompt on mount,
    which activated its tab, so ANY code path that added a pane also
    switched the user to it whether it said so or not. Adding a pane
    without setting `active` now leaves the user exactly where they
    were."""
    app, _engines = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        first = app.active_pane

        background = app._make_pane(app._new_session_factory)
        await _tabbed(app).add_pane(app._make_tab(background))
        await pilot.pause()
        await pilot.pause()

        assert len(app.panes()) == 2
        assert app.active_pane is first
        assert app.focused is _prompt_of(first)


@pytest.mark.asyncio
async def test_a_background_mount_cannot_steal_the_tab_you_just_switched_to(
    tmp_path,
):
    """The done-unseen flake, made deterministic.

    Ctrl+T then Ctrl+← puts the user back on the first tab. A pane that
    mounts AFTER that must not take the activation back -- which is
    exactly what a mount-time focus did, arriving a message-pump turn or
    two late and leaving _on_turn_done_status reading `active=True` for
    the wrong pane."""
    app, _engines = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        first = app.active_pane

        await pilot.press("ctrl+t")
        assert await _wait(pilot, lambda: len(app.panes()) == 2)
        await pilot.press("ctrl+left")
        await pilot.pause()
        assert app.active_pane is first

        late = app._make_pane(app._new_session_factory)
        await _tabbed(app).add_pane(app._make_tab(late))
        for _ in range(5):
            await pilot.pause()

        assert app.active_pane is first
        assert app.focused is _prompt_of(first)


# -- cycling and jumping ------------------------------------------------


@pytest.mark.asyncio
async def test_cycling_tabs_takes_the_focus_with_it(tmp_path):
    app, _engines = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        first = app.active_pane
        await pilot.press("ctrl+t")
        assert await _wait(pilot, lambda: len(app.panes()) == 2)
        second = app.panes()[1]

        await pilot.press("ctrl+left")
        await pilot.pause()
        assert app.active_pane is first
        assert app.focused is _prompt_of(first)

        await pilot.press("ctrl+right")
        await pilot.pause()
        assert app.active_pane is second
        assert app.focused is _prompt_of(second)


@pytest.mark.asyncio
async def test_cycling_reaches_a_read_only_archived_tab(tmp_path):
    """Reported live: "CTRL+ArrowLeft ... only seems to work to switch
    among active sessions ... not between read-only finished sessions".
    _cycle_tab used to walk panes() -- SESSION tabs only -- so an
    ArchivedSessionTab sitting right there in the strip was never
    reachable by keyboard cycling. It must be, in strip order, same as
    every other tab."""
    where = tmp_path / "scratch"
    where.mkdir()
    specs = [
        RestoreTabSpec("sid-live", _restore_factory("sid-live")),
        RestoreTabSpec("sid-dead", None, cwd=str(where), archived=True),
    ]
    app = DoxaApp(cwd=str(where), restore_tabs=specs, restore_active_id="sid-live")
    async with app.run_test() as pilot:
        assert await _wait(pilot, lambda: len(app.panes()) == 1)
        await pilot.pause()
        live = app.panes()[0]
        assert app.active_pane is live

        await pilot.press("ctrl+right")
        await pilot.pause()
        assert isinstance(_tabbed(app).active_pane, ArchivedSessionTab)
        assert _tabbed(app).active_pane.session_id == "sid-dead"

        # And back around: two tabs, so a second cycle in the same
        # direction returns to the live one.
        await pilot.press("ctrl+right")
        await pilot.pause()
        assert app.active_pane is live


@pytest.mark.asyncio
async def test_jumping_to_a_tab_by_id_focuses_it(tmp_path):
    """_switch_to_tab -- the palette's open-tab entries and the peer
    chip's "that session is already open here" jump."""
    app, _engines = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        first = app.active_pane
        await pilot.press("ctrl+t")
        assert await _wait(pilot, lambda: len(app.panes()) == 2)

        app._switch_to_tab(first.id or "")
        await pilot.pause()
        assert app.active_pane is first
        assert app.focused is _prompt_of(first)


@pytest.mark.asyncio
async def test_open_tab_at_activates_and_focuses_the_new_tab(tmp_path):
    """The repo picker's spawn (item 4) is as explicit an intent as
    Ctrl+T and lands the same way."""
    where = tmp_path / "elsewhere"
    where.mkdir()
    app, _engines = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()

        assert await app.open_tab_at(str(where)) is None
        await pilot.pause()

        opened = app.panes()[-1]
        assert app.active_pane is opened
        assert app.focused is _prompt_of(opened)


# -- restore ------------------------------------------------------------


@pytest.mark.asyncio
async def test_three_tab_restore_activates_and_focuses_the_saved_tab(tmp_path):
    """Three tabs, saved active in the MIDDLE -- the shape that exposed
    the v0.23.0 defect (two tabs hid it, because the saved active tab
    happened to be the one that mounted last)."""
    where = tmp_path / "scratch"
    where.mkdir()
    specs = [
        RestoreTabSpec(f"sid-{n}", _restore_factory(f"sid-{n}")) for n in (1, 2, 3)
    ]
    app = DoxaApp(cwd=str(where), restore_tabs=specs, restore_active_id="sid-2")
    async with app.run_test() as pilot:
        assert await _wait(pilot, lambda: len(app.panes()) == 3)
        await pilot.pause()
        middle = app.panes()[1]
        assert middle._session_id == "sid-2" or middle.id == "restore-sid-2"
        assert app.active_pane is middle
        assert app.focused is _prompt_of(middle)


@pytest.mark.asyncio
async def test_a_restore_with_no_saved_active_tab_lands_on_the_first_session(
    tmp_path,
):
    """No saved active id, and the first tab in the strip is an ARCHIVE.
    The user gets a prompt anyway -- the first LIVE pane -- which is what
    the old mount-time focus picked, and is preserved deliberately."""
    where = tmp_path / "scratch"
    where.mkdir()
    specs = [
        RestoreTabSpec("sid-dead", None, cwd=str(where), archived=True),
        RestoreTabSpec("sid-live", _restore_factory("sid-live")),
    ]
    app = DoxaApp(cwd=str(where), restore_tabs=specs)
    async with app.run_test() as pilot:
        assert await _wait(pilot, lambda: len(app.panes()) == 1)
        await pilot.pause()
        live = app.panes()[0]
        assert len(app.archived_tabs()) == 1
        assert app.active_pane is live
        assert app.focused is _prompt_of(live)


@pytest.mark.asyncio
async def test_an_all_archived_restore_lands_on_the_fresh_pane(tmp_path):
    """Every restored tab is an archive, so compose() adds one fresh
    session beside them -- and THAT is the tab the window opens on,
    because it is the only one with a prompt."""
    where = tmp_path / "scratch"
    where.mkdir()
    specs = [
        RestoreTabSpec("sid-dead-1", None, cwd=str(where), archived=True),
        RestoreTabSpec("sid-dead-2", None, cwd=str(where), archived=True),
    ]
    make, _engines = _tracked()
    app = DoxaApp(
        cwd=str(where), engine_factory=make, new_session_factory=make,
        restore_tabs=specs,
    )
    async with app.run_test() as pilot:
        assert await _wait(pilot, lambda: len(app.panes()) == 1)
        await pilot.pause()
        fresh = app.panes()[0]
        assert len(app.archived_tabs()) == 2
        assert app.active_pane is fresh
        assert app.focused is _prompt_of(fresh)


@pytest.mark.asyncio
async def test_a_restore_onto_an_archived_tab_still_activates_it(tmp_path):
    """The saved active tab was a session that has since ended: the
    window comes up on its read-only archive, not on the live pane beside
    it. (Nothing to focus there -- an archive has no prompt -- which is
    unchanged.)"""
    where = tmp_path / "scratch"
    where.mkdir()
    specs = [
        RestoreTabSpec("sid-live", _restore_factory("sid-live")),
        RestoreTabSpec("sid-dead", None, cwd=str(where), archived=True),
    ]
    app = DoxaApp(
        cwd=str(where), restore_tabs=specs, restore_active_id="sid-dead",
    )
    async with app.run_test() as pilot:
        assert await _wait(pilot, lambda: len(app.panes()) == 1)
        await pilot.pause()
        assert isinstance(_tabbed(app).active_pane, ArchivedSessionTab)
        assert app.active_pane is None


# -- activation and the keyboard move TOGETHER (v1.7.2) -----------------
#
# The invariant every tab-moving site in doxa/app.py already keeps, stated
# once so a ninth caller cannot quietly drop it: setting
# `TabbedContent.active` without also saying where the keyboard goes does
# not merely leave focus stale, it LIVELOCKS the window.
#
# The cycle, measured at v1.7.1 against textual 5.3.0: hiding the TabPane
# the keyboard is in makes Textual re-home focus (`Screen._reset_focus`)
# onto another focusable widget INSIDE that same just-hidden pane -- its
# `#block-list` scroll; focusing a widget inside a TabPane re-ACTIVATES
# that pane (`TabbedContent._on_tab_pane_focused`), which hides the other
# tab; `DoxaApp._on_tab_activated` then moves the keyboard into the newly
# active tab's prompt, i.e. back inside whichever pane is about to be
# hidden next. Two writers, permanently one step out of phase, forever.
#
# Deterministic rather than statistical: it counts focus moves during
# message-pump turns in which NOTHING is asked of the app. A settled
# window makes none at all; the loop made ~90. The threshold below is far
# from both.


def _count_focus_moves(app: DoxaApp) -> "list[int]":
    """Arm a counter on the screen's own focus setter. Returns the
    single-element list it counts into (a list so the closure can be
    installed once and read after)."""
    seen = [0]
    screen = app.screen
    original = screen.set_focus

    def counting(widget, scroll_visible=True, from_app_focus=False):
        seen[0] += 1
        return original(
            widget, scroll_visible=scroll_visible, from_app_focus=from_app_focus,
        )

    screen.set_focus = counting  # type: ignore[method-assign]
    return seen


async def _idle_focus_moves(
    app: DoxaApp, pilot, turns: int = 40, settle: int = 10,
) -> int:
    """How many times the keyboard moves across TURNS message-pump turns
    in which the test asks the app for nothing.

    SETTLE turns are burned first and not counted: `Widget.focus()` is
    deferred in textual 5.3, so a switch's OWN focus move -- one, plus
    `_on_tab_activated`'s no-op refocus behind it -- lands a turn or two
    after the call that asked for it. Those are the gesture arriving, not
    churn. A window that has genuinely settled makes none after them; the
    loop this file pins makes them for as long as anyone watches, so no
    finite settle can hide it."""
    for _ in range(settle):
        await pilot.pause(0.02)
    seen = _count_focus_moves(app)
    for _ in range(turns):
        await pilot.pause(0.02)
    return seen[0]


@pytest.mark.asyncio
async def test_a_settled_three_tab_window_moves_the_keyboard_never(tmp_path):
    """The control for the test below: left alone, a three-tab window is
    completely quiet. Without this, "the loop is gone" could just as well
    mean "the counter is broken"."""
    app, _engines = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_tab()
        await pilot.pause()
        await app.action_new_tab()
        assert await _wait(pilot, lambda: len(app.panes()) == 3)
        await pilot.pause()

        assert await _idle_focus_moves(app, pilot) == 0


@pytest.mark.asyncio
async def test_activating_a_tab_without_moving_the_keyboard_livelocks(tmp_path):
    """The defect itself, pinned as the reason the rule exists.

    This deliberately does the WRONG thing -- switches the visible tab and
    says nothing about the keyboard -- and asserts the window will not
    settle. It is the only test here that asserts a failure mode rather
    than a behaviour, and it earns that: `scripts/screenshot.py::_activate`
    was written exactly this way, which cost the gallery about half of its
    `split-panes` runs and every complete end-to-end pass, and nothing in
    the tree said why that was forbidden."""
    app, _engines = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_tab()
        await pilot.pause()
        await app.action_new_tab()
        assert await _wait(pilot, lambda: len(app.panes()) == 3)
        await pilot.pause()
        first = app.panes()[0]
        assert app.focused is not _prompt_of(first)

        # Show the first tab; say NOTHING about the keyboard.
        tab_id = first.tab_id or ""
        tabbed = app.tabbed_holding(tab_id)
        assert tabbed is not None
        tabbed.active = tab_id

        assert await _idle_focus_moves(app, pilot) > 10


@pytest.mark.asyncio
async def test_activating_a_tab_and_focusing_it_settles(tmp_path):
    """The same switch done the way every caller in doxa/app.py does it:
    activate, then name the pane the keyboard lands in. The window is
    quiet, and the keyboard is where the switch said."""
    app, _engines = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_new_tab()
        await pilot.pause()
        await app.action_new_tab()
        assert await _wait(pilot, lambda: len(app.panes()) == 3)
        await pilot.pause()
        first = app.panes()[0]

        tab_id = first.tab_id or ""
        tabbed = app.tabbed_holding(tab_id)
        assert tabbed is not None
        tabbed.active = tab_id
        app._focus_tab(first)

        assert await _idle_focus_moves(app, pilot) == 0
        assert app.focused is _prompt_of(first)
        assert app.active_pane is first


@pytest.mark.asyncio
async def test_ctrl_n_puts_the_keyboard_in_the_new_pane(tmp_path):
    """v0.91.0's stated rule, asserted about the KEYBOARD rather than
    about `active_pane`.

    `active_pane` is not a proxy for it: `_focus_tab` records
    `_last_group_id` synchronously and `focused_group()` falls back to
    that id, so `active_pane` reports the move whether or not focus ever
    arrives. scripts/screenshot.py's `split-panes` scene asserted the
    proxy and passed while the keyboard sat in the pane the user had just
    split away from."""
    app, _engines = _app(tmp_path)
    async with app.run_test(size=(160, 48)) as pilot:
        await pilot.pause()
        left = app.active_pane
        assert left is not None
        before = {id(pane) for pane in app.panes()}

        await pilot.press("ctrl+n")
        assert await _wait(pilot, lambda: len(app.panes()) > len(before))
        right = next(p for p in app.panes() if id(p) not in before)

        assert await _wait(pilot, lambda: app.focused is _prompt_of(right))
        assert app.active_pane is right
        assert await _idle_focus_moves(app, pilot) == 0


@pytest.mark.asyncio
async def test_typing_after_ctrl_n_lands_in_the_new_pane(tmp_path):
    """The same claim as a user meets it: split, type, and the text is in
    the pane that just appeared -- not in the one it was split off."""
    app, _engines = _app(tmp_path)
    async with app.run_test(size=(160, 48)) as pilot:
        await pilot.pause()
        left = app.active_pane
        assert left is not None
        before = {id(pane) for pane in app.panes()}

        await pilot.press("ctrl+n")
        assert await _wait(pilot, lambda: len(app.panes()) > len(before))
        right = next(p for p in app.panes() if id(p) not in before)
        assert await _wait(pilot, lambda: app.focused is _prompt_of(right))

        await pilot.press("h", "i")
        assert _prompt_of(right).text == "hi"
        assert _prompt_of(left).text == ""

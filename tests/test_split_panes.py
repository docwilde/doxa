# SPDX-License-Identifier: AGPL-3.0-only
"""Recursive split panes (v0.91.0), against a real Pilot.

The v0.28.0 lesson applies with full force here and the spec says so:
assertions must be about what the USER SEES. A split that "exists" in the
widget tree and paints nothing is the invisible-button defect again, so
every structural claim below is paired with a rendered rectangle.

What each section pins:

* **rendering** -- a split really paints two panes, both with non-zero
  width and height, and on the axis that was asked for;
* **focus** -- splits inherit v0.38.0's rule rather than re-litigating it:
  a new leaf mounts unfocused and whatever creates it says where the
  keyboard goes. Exactly one pane per window holds it;
* **seen vs visible** -- the settled question. A pane that is merely
  visible has NOT been seen, so its markers survive;
* **directional movement** -- geometric, in a real 2x2;
* **closing** -- a leaf collapses its split; the last leaf closes the tab;
* **refusals** -- a split that would make a sliver, or that has spent its
  depth allowance, says so and changes nothing;
* **the in-pane divider** -- the status bar, moving in a SINGLE-LEAF tab
  with no splits at all, which is the case that provoked the feature.
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
import threading
import time

import pytest

from doxa import config as config_mod
from doxa import layout
from doxa.app import DoxaApp, SessionPane
from doxa.ui.prompt import PromptInput
from doxa.ui.split import PaneTab
from tests.fakes import FakeEngine


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "doxa-home"))
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    config_mod.invalidate()
    yield
    config_mod.invalidate()


def _app(tmp_path):
    engines: list[FakeEngine] = []

    def make() -> FakeEngine:
        engines.append(FakeEngine([]))
        return engines[-1]

    return DoxaApp(
        cwd=str(tmp_path), engine_factory=make, new_session_factory=make,
    ), engines


async def _wait(pilot, cond, tries=250):
    for _ in range(tries):
        if cond():
            return True
        await pilot.pause(0.02)
    return cond()


def _prompt_of(pane: SessionPane) -> PromptInput:
    return pane.query_one("#prompt-input", PromptInput)


#: Big enough that no split below is refused for size -- the refusal
#: itself gets its own test on a deliberately small screen.
BIG = (160, 48)


# -- rendering ----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_vsplit_paints_two_panes_side_by_side(tmp_path):
    """Non-zero width AND height for both, and the second one genuinely
    to the RIGHT -- not merely present in the DOM."""
    app, _engines = _app(tmp_path)
    async with app.run_test(size=BIG) as pilot:
        await pilot.pause()
        first = app.active_pane
        assert await app.split_active_pane(layout.ROW) is None
        assert await _wait(pilot, lambda: len(app.panes()) == 2)
        await pilot.pause()

        left, right = app.panes()
        assert left is first
        for pane in (left, right):
            assert pane.region.width > 0
            assert pane.region.height > 0
        assert right.region.x > left.region.x
        assert right.region.y == left.region.y
        # v0.97.0: a split makes a GROUP, and a group owns its own tabs.
        # Through v0.95.0 this asserted the opposite -- one tab, two panes
        # -- and the inversion is exactly the change that turned the
        # reported defect ("the split out sessions go with the tab") into
        # a property the model cannot express.
        assert len(app.groups()) == 2
        assert left.tab is not right.tab
        assert len(app.query(PaneTab)) == 2


@pytest.mark.asyncio
async def test_a_split_paints_two_panes_stacked(tmp_path):
    app, _engines = _app(tmp_path)
    async with app.run_test(size=BIG) as pilot:
        await pilot.pause()
        assert await app.split_active_pane(layout.COLUMN) is None
        assert await _wait(pilot, lambda: len(app.panes()) == 2)
        await pilot.pause()

        top, bottom = app.panes()
        assert bottom.region.y > top.region.y
        assert bottom.region.x == top.region.x
        assert top.region.height > 0 and bottom.region.height > 0


@pytest.mark.asyncio
async def test_both_panes_of_a_split_carry_a_live_prompt_and_status_bar(tmp_path):
    """A leaf is a whole session surface, not a viewport onto one: each
    has its own prompt, its own status bar and its own engine."""
    app, _engines = _app(tmp_path)
    async with app.run_test(size=BIG) as pilot:
        await pilot.pause()
        assert await app.split_active_pane(layout.ROW) is None
        assert await _wait(pilot, lambda: len(app.panes()) == 2)
        await _wait(pilot, lambda: all(p.engine is not None for p in app.panes()))
        await pilot.pause()

        engines = {id(p.engine) for p in app.panes()}
        assert len(engines) == 2  # two sessions, not one shown twice
        for pane in app.panes():
            assert pane.query_one("#status-bar").region.height == 1
            assert _prompt_of(pane).region.height > 0


# -- focus --------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_split_hands_the_keyboard_to_the_new_pane_explicitly(tmp_path):
    """v0.38.0's rule, inherited rather than re-litigated: the leaf did
    not focus itself on mount -- ``split_active_pane`` said where the
    keyboard goes, the way Ctrl+T does for a new tab."""
    app, _engines = _app(tmp_path)
    async with app.run_test(size=BIG) as pilot:
        await pilot.pause()
        first = app.active_pane
        assert await app.split_active_pane(layout.ROW) is None
        assert await _wait(pilot, lambda: len(app.panes()) == 2)
        await pilot.pause()

        new = app.panes()[1]
        assert app.focused is _prompt_of(new)
        assert app.active_pane is new
        assert app.focused is not _prompt_of(first)


@pytest.mark.asyncio
async def test_exactly_one_pane_per_window_holds_the_keyboard(tmp_path):
    app, _engines = _app(tmp_path)
    async with app.run_test(size=BIG) as pilot:
        await pilot.pause()
        assert await app.split_active_pane(layout.ROW) is None
        assert await app.split_active_pane(layout.COLUMN) is None
        assert await _wait(pilot, lambda: len(app.panes()) == 3)
        await pilot.pause()

        focused = [p for p in app.panes() if app.focused is _prompt_of(p)]
        assert len(focused) == 1
        assert app.focused_pane() is focused[0]


@pytest.mark.asyncio
async def test_an_unfocused_visible_pane_still_renders_live_output(tmp_path):
    """Visible and focused are different states, and the transcript must
    not stall because focus moved."""
    from doxa.ui.transcript import SystemBlock

    app, _engines = _app(tmp_path)
    async with app.run_test(size=BIG) as pilot:
        await pilot.pause()
        first = app.active_pane
        assert await app.split_active_pane(layout.ROW) is None
        assert await _wait(pilot, lambda: len(app.panes()) == 2)
        await pilot.pause()
        assert app.active_pane is not first  # first is visible, not focused

        await first._system("output that arrived while you were elsewhere")

        def _painted():
            # PAINTED, not merely mounted -- the whole point of the
            # assertion, and the reason this polls a rectangle rather than
            # a mount: a block exists in the DOM one turn before the
            # compositor has given it any cells.
            return [
                b for b in first.query(SystemBlock)
                if "while you were elsewhere" in b.text
                and b.region.height > 0 and b.region.width > 0
            ]

        assert await _wait(pilot, lambda: bool(_painted()))
        assert first.region.width > 0  # in a pane that is visible, unfocused


# -- visible is not seen ------------------------------------------------


@pytest.mark.asyncio
async def test_a_visible_but_unfocused_pane_keeps_its_unseen_marks(tmp_path):
    """The settled question, and the one this release decides against the
    old behaviour: through v0.88.0 tab ACTIVATION cleared every marker,
    because a tab held one pane. It holds several now, and a pane in the
    corner of a grid may genuinely be unread -- so only the pane that
    gets the KEYBOARD is counted as seen."""
    app, _engines = _app(tmp_path)
    async with app.run_test(size=BIG) as pilot:
        await pilot.pause()
        first = app.active_pane
        assert await app.split_active_pane(layout.ROW) is None
        assert await _wait(pilot, lambda: len(app.panes()) == 2)
        await pilot.pause()
        new = app.panes()[1]

        # Something happened in the pane you are NOT typing in.
        first._set_tab_class("-done-unseen", True)
        first.set_staged(True)
        first.set_needs_input(True)
        await pilot.pause()

        assert first.region.width > 0  # it is right there on screen
        assert first.has_mark("-done-unseen")
        assert first.staged_pending
        assert first.needs_input
        # ...and the pane with the keyboard has nothing to clear.
        assert not new.has_mark("-done-unseen")

        # The keyboard arriving is what counts as looking at it.
        await _focus(app, pilot, first)
        assert not first.has_mark("-done-unseen")
        assert not first.staged_pending
        assert not first.needs_input


@pytest.mark.asyncio
async def test_the_tab_header_is_the_or_over_its_leaves(tmp_path):
    """A tab whose corner pane finished still reads as "something
    happened in here" from the strip -- one header, several panes, so it
    can only carry the disjunction."""
    from textual.widgets import TabbedContent

    app, _engines = _app(tmp_path)
    async with app.run_test(size=BIG) as pilot:
        await pilot.pause()
        first = app.active_pane
        assert await app.split_active_pane(layout.ROW) is None
        assert await _wait(pilot, lambda: len(app.panes()) == 2)
        await pilot.pause()

        header = app.query_one("#session-tabs", TabbedContent).get_tab(first.tab_id)
        first._set_tab_class("-done-unseen", True)
        await pilot.pause()
        assert header.has_class("-done-unseen")

        # Focusing the OTHER pane does not clear the tab: the marked pane
        # has still not been looked at.
        await _focus(app, pilot, app.panes()[1])
        assert header.has_class("-done-unseen")

        await _focus(app, pilot, first)
        assert not header.has_class("-done-unseen")


# -- directional movement -----------------------------------------------


async def _moved(app, pilot, direction):
    """Press a directional key and WAIT for the keyboard to arrive.

    ``Widget.focus()`` is deferred in Textual 5.3 (it schedules
    ``screen.set_focus`` with ``call_later``), so every focus move in this
    app lands one message-pump turn after the handler returns -- which is
    why this suite polls for the settled state rather than reading it back
    synchronously."""
    assert app.focus_pane_towards(direction) is True
    await pilot.pause()
    await pilot.pause()
    return app.active_pane


async def _focus(app, pilot, pane):
    """Put the keyboard on a pane and WAIT for it to arrive -- a leaf
    mounted this turn composes its prompt on the next one, so focus lands
    a refresh later (DoxaApp._focus_tab re-states it)."""
    app._focus_tab(pane)
    assert await _wait(pilot, lambda: app.active_pane is pane)
    await pilot.pause()
    return pane


async def _split(app, pilot, orientation):
    assert await app.split_active_pane(orientation) is None
    assert await _wait(pilot, lambda: app.active_pane is not None)
    await pilot.pause()
    return app.panes()


async def _grid(app, pilot):
    """The 2x2 the spec's directional test is written against: split
    sideways, then split each half the other way."""
    a = app.active_pane
    await _split(app, pilot, layout.ROW)                        # a | b
    assert await _wait(pilot, lambda: len(app.panes()) == 2)
    b = await _focus(app, pilot, app.panes()[1])
    await _split(app, pilot, layout.COLUMN)                     # b over d
    assert await _wait(pilot, lambda: len(app.panes()) == 3)
    d = app.active_pane
    assert d is not b
    await _focus(app, pilot, a)
    await _split(app, pilot, layout.COLUMN)                     # a over c
    assert await _wait(pilot, lambda: len(app.panes()) == 4)
    c = app.active_pane
    assert c not in (a, b, d)
    await pilot.pause()
    return a, b, c, d


@pytest.mark.asyncio
async def test_directional_focus_moves_through_a_real_2x2(tmp_path):
    app, _engines = _app(tmp_path)
    async with app.run_test(size=BIG) as pilot:
        await pilot.pause()
        a, b, c, d = await _grid(app, pilot)

        # The grid is actually a grid on screen, not just in the tree.
        assert b.region.x > a.region.x
        assert c.region.y > a.region.y
        assert d.region.x > c.region.x and d.region.y > b.region.y
        for pane in (a, b, c, d):
            assert pane.region.width > 0 and pane.region.height > 0

        await _focus(app, pilot, a)
        assert await _moved(app, pilot, "right") is b
        assert await _moved(app, pilot, "down") is d
        assert await _moved(app, pilot, "left") is c
        assert await _moved(app, pilot, "up") is a


@pytest.mark.asyncio
async def test_directional_focus_at_the_edge_does_nothing_at_all(tmp_path):
    app, _engines = _app(tmp_path)
    async with app.run_test(size=BIG) as pilot:
        await pilot.pause()
        a, _b, _c, _d = await _grid(app, pilot)
        await _focus(app, pilot, a)
        assert app.focus_pane_towards("up") is False
        assert app.focus_pane_towards("left") is False
        assert app.active_pane is a


@pytest.mark.asyncio
async def test_directional_focus_is_a_no_op_in_an_unsplit_tab(tmp_path):
    app, _engines = _app(tmp_path)
    async with app.run_test(size=BIG) as pilot:
        await pilot.pause()
        only = app.active_pane
        for direction in ("left", "right", "up", "down"):
            assert app.focus_pane_towards(direction) is False
        assert app.active_pane is only


# -- closing ------------------------------------------------------------


@pytest.mark.asyncio
async def test_closing_a_leaf_collapses_the_split_and_keeps_the_tab(tmp_path):
    app, _engines = _app(tmp_path)
    async with app.run_test(size=BIG) as pilot:
        await pilot.pause()
        first = app.active_pane
        assert await app.split_active_pane(layout.ROW) is None
        assert await _wait(pilot, lambda: len(app.panes()) == 2)
        await pilot.pause()
        new = app.panes()[1]
        whole = app._window_root().region

        await app._close_pane(new, terminate=False)
        assert await _wait(pilot, lambda: len(app.panes()) == 1)
        await pilot.pause()

        assert len(app.query(PaneTab)) == 1  # the tab is still there
        assert len(app.groups()) == 1  # ...and so is exactly one group
        assert app.panes() == [first]
        assert app.active_pane is first
        assert app.focused is _prompt_of(first)
        # The survivor took the whole tab back -- no dead strip where the
        # closed pane was.
        assert first.region.width == whole.width


@pytest.mark.asyncio
async def test_closing_the_last_leaf_of_a_tab_closes_the_tab(tmp_path):
    app, _engines = _app(tmp_path)
    async with app.run_test(size=BIG) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+t")
        assert await _wait(pilot, lambda: len(app.panes()) == 2)
        await pilot.pause()
        assert len(app.query(PaneTab)) == 2

        await app._close_pane(app.active_pane, terminate=False)
        assert await _wait(pilot, lambda: len(app.query(PaneTab)) == 1)
        assert len(app.panes()) == 1


# -- refusals -----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_split_with_no_room_is_refused_and_changes_nothing(tmp_path):
    app, _engines = _app(tmp_path)
    async with app.run_test(size=(50, 30)) as pilot:
        await pilot.pause()
        before = list(app.panes())

        note = await app.split_active_pane(layout.ROW)
        await pilot.pause()
        assert note is not None
        assert str(layout.MIN_LEAF_WIDTH) in note
        assert app.panes() == before


@pytest.mark.asyncio
async def test_splitting_past_the_depth_cap_is_refused_in_words(tmp_path):
    """Recursion is in the model; the interactive depth is capped, and a
    deeper split is refused with a message rather than performed."""
    app, _engines = _app(tmp_path)
    async with app.run_test(size=BIG) as pilot:
        await pilot.pause()
        pane = app.active_pane
        for _ in range(layout.SPLIT_SLOTS):
            await _focus(app, pilot, pane)
            assert await app.split_active_pane(layout.COLUMN) is None
            await pilot.pause()

        await _focus(app, pilot, pane)
        before = len(app.panes())
        note = await app.split_active_pane(layout.COLUMN)
        assert note is not None and "deep" in note
        assert len(app.panes()) == before


# -- the in-pane divider ------------------------------------------------


@pytest.mark.asyncio
async def test_the_status_bar_divider_moves_in_a_tab_with_no_splits(tmp_path):
    """The case the owner actually hit: one tab, no splits, a surface too
    short for what was in it. Ctrl+Up grows the transcript, Ctrl+Down
    grows the prompt."""
    app, _engines = _app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        pane = app.active_pane
        assert not pane.is_split_leaf
        prompt = _prompt_of(pane)
        blocks = pane.query_one("#block-list")
        start_prompt = prompt.region.height
        start_blocks = blocks.region.height

        await pilot.press("ctrl+down")
        await pilot.press("ctrl+down")
        await pilot.press("ctrl+down")
        await pilot.pause()
        assert prompt.region.height > start_prompt
        assert blocks.region.height < start_blocks

        grown = prompt.region.height
        await pilot.press("ctrl+up")
        await pilot.pause()
        assert prompt.region.height < grown


@pytest.mark.asyncio
async def test_the_divider_never_takes_the_prompt_below_a_typable_line(tmp_path):
    """The one region whose collapse makes DOXA unusable rather than
    merely awkward -- so the floor is enforced, and pressing into it is a
    silent no-op rather than a sliver."""
    app, _engines = _app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        pane = app.active_pane
        prompt = _prompt_of(pane)
        for _ in range(40):
            await pilot.press("ctrl+up")
        await pilot.pause()
        assert prompt.region.height >= layout.MIN_PROMPT_ROWS + 2
        assert pane._effective_prompt_rows() == layout.MIN_PROMPT_ROWS


@pytest.mark.asyncio
async def test_the_divider_never_swallows_the_transcript(tmp_path):
    app, _engines = _app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        pane = app.active_pane
        blocks = pane.query_one("#block-list")
        for _ in range(60):
            await pilot.press("ctrl+down")
        await pilot.pause()
        assert blocks.region.height >= layout.MIN_TRANSCRIPT_ROWS


@pytest.mark.asyncio
async def test_the_divider_position_is_proportional_across_a_resize(tmp_path):
    """Sizes are proportional, never absolute -- so the SAME divider
    position on a taller terminal is more rows, not the same rows."""
    app, _engines = _app(tmp_path)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        pane = app.active_pane
        prompt = _prompt_of(pane)
        for _ in range(4):
            await pilot.press("ctrl+down")
        await pilot.pause()
        small = prompt.region.height
        ratio = pane.prompt_ratio
        assert ratio > 0

        await pilot.resize_terminal(120, 60)
        await pilot.pause()
        await pilot.pause()
        assert pane.prompt_ratio == ratio      # the RATIO did not move
        assert prompt.region.height > small    # the rows did


@pytest.mark.asyncio
async def test_the_divider_acts_on_the_focused_leaf_only(tmp_path):
    """Once splits exist there are dividers between leaves too, and
    Ctrl+Up/Down cannot mean two things: they act on the FOCUSED leaf's
    own status-bar divider, and its neighbour is untouched."""
    app, _engines = _app(tmp_path)
    async with app.run_test(size=BIG) as pilot:
        await pilot.pause()
        first = app.active_pane
        assert await app.split_active_pane(layout.ROW) is None
        assert await _wait(pilot, lambda: len(app.panes()) == 2)
        await pilot.pause()
        new = app.active_pane
        other_before = _prompt_of(first).region.height

        for _ in range(3):
            await pilot.press("ctrl+down")
        await pilot.pause()

        assert new.prompt_ratio > 0
        assert first.prompt_ratio == 0
        assert _prompt_of(first).region.height == other_before


@pytest.mark.asyncio
async def test_alt_arrow_moves_the_divider_between_two_leaves(tmp_path):
    """The between-leaf divider gets its OWN gesture rather than
    overloading Ctrl+Up/Down -- the spec's explicit instruction."""
    app, _engines = _app(tmp_path)
    async with app.run_test(size=BIG) as pilot:
        await pilot.pause()
        first = app.active_pane
        assert await app.split_active_pane(layout.ROW) is None
        assert await _wait(pilot, lambda: len(app.panes()) == 2)
        await pilot.pause()
        new = app.active_pane
        before = new.region.width

        for _ in range(4):
            await pilot.press("alt+left")
        await pilot.pause()

        assert new.region.width > before
        assert first.region.width > 0  # never dragged into nothing
        assert (
            first.region.width + new.region.width
            == app._window_root().region.width
        )


# -- the event loop, during a split (v0.95.0) ---------------------------
#
# Reported from live use: "After splitting the pane with vsplit, the whole
# TUI lags hard (maybe CPU load loop wo wait or not async?)".
#
# It was the second guess. `SessionPane.on_mount` opened with a plain
# `self.engine = self._engine_factory()`, and in production that factory
# is `doxa.cli.new_session_factory` -> `daemon.spawn_daemon`, which
# Popens a daemon and then blocks its caller in a
# `while monotonic() < deadline: _time.sleep(0.1)` registry poll for up to
# 60 seconds. Measured at 2320.8 ms of unbroken event-loop stall against a
# 12.0 ms idle gap. There was no busy loop: idle cost was 0.8-2.0% of one
# core before AND after a split, over eleven scenarios and again through a
# real PTY, and never diverged.
#
# NOTHING in this suite could see it, and the reason is worth stating,
# because it is why the defect shipped: every test here hands DoxaApp a
# factory that returns a FakeEngine instantly. A blocking factory is not
# an exotic case to simulate -- it is the ONLY kind that ships.


class _SpawnProbe:
    """The stand-in for ``doxa.cli.new_session_factory``.

    It blocks its caller the way the real one does, and it records the two
    facts the probes below assert on: the thread it was called on, and the
    exact window it spent blocking."""

    def __init__(self, block_secs: float) -> None:
        self.block_secs = block_secs
        self.threads: list[int] = []
        self.windows: list[tuple[float, float]] = []

    def __call__(self) -> FakeEngine:
        self.threads.append(threading.get_ident())
        began = time.perf_counter()
        time.sleep(self.block_secs)
        self.windows.append((began, time.perf_counter()))
        return FakeEngine([])


def _blocking_app(tmp_path, block_secs: float):
    """A DoxaApp whose new-session factory behaves like the real one: it
    blocks its caller, synchronously, before returning an engine.

    Returns the app AND the probe, because where and when that factory ran
    is the thing under test."""
    probe = _SpawnProbe(block_secs)
    return DoxaApp(
        cwd=str(tmp_path),
        engine_factory=lambda: FakeEngine([]),
        new_session_factory=probe,
    ), probe


class _Heartbeat:
    """A task that wakes every 10ms and records how long it was actually
    kept from waking. The longest gap IS the freeze: an event loop that
    someone is calling `time.sleep` on cannot run this, cannot dispatch a
    key, and cannot repaint.

    It also keeps the tick TIMESTAMPS, so a caller can ask the sharper
    question: did the loop wake at all across a named window?"""

    def __init__(self, interval: float = 0.01) -> None:
        self.interval = interval
        self.gaps: list[float] = []
        self.ticks: list[float] = []
        self._task: "asyncio.Task | None" = None

    async def _beat(self) -> None:
        last = time.perf_counter()
        while True:
            await asyncio.sleep(self.interval)
            now = time.perf_counter()
            self.gaps.append(now - last)
            self.ticks.append(now)
            last = now

    def start(self) -> None:
        self._task = asyncio.ensure_future(self._beat())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    def clear(self) -> None:
        self.gaps.clear()
        self.ticks.clear()

    def ticks_within(self, began: float, ended: float) -> int:
        """How many times the loop woke between two `perf_counter` marks."""
        return sum(1 for t in self.ticks if began <= t <= ended)

    @property
    def worst(self) -> float:
        return max(self.gaps) if self.gaps else 0.0


@contextlib.contextmanager
def _without_collector_pauses():
    """Hold CPython's cyclic collector off across the measured window.

    See the note below on what these probes measure. A generation-2 pass
    is not the thing under test and it is the whole of the noise, so it is
    excluded from the window rather than budgeted for -- which is what
    raising the bar to clear it would have amounted to."""
    was_on = gc.isenabled()
    gc.collect()
    gc.disable()
    try:
        yield
    finally:
        if was_on:
            gc.enable()


# -- what these probes measure, and what they deliberately do not -------
#
# v0.95.0 wrote the two probes below as a single wall-clock assertion:
# start a 10ms heartbeat, do the split, require the worst inter-tick gap
# to come in under 250 ms. That is the right INTENT and it was the wrong
# WINDOW, and between v0.95.0 and v1.0.2 the pair failed intermittently
# for three separate readers -- always late in a full run, always passing
# in isolation, and, decisively, ALSO ON THE FIXED CODE. Two of those
# readings: 621 ms and 446 ms, both against that 250 ms bar, both with the
# factory demonstrably running in a thread.
#
# The window contained two things. One is the stand-in factory's
# `time.sleep`, which IS under test. The other is Textual's own mount
# work, which is not, and which is where the bar went wrong. Measured on
# this machine with a NON-blocking factory -- nothing under test inside
# the window -- ten splits each:
#
#   idle, collector running   worst gap  20.7 - 167.3 ms
#   idle, collector held off  worst gap  18.2 -  43.9 ms
#   ~290 MB live heap, collector held off  265 ms (split) / 310 ms (Ctrl+T)
#   full suite, collector held off         379 ms (split) / 399 ms (Ctrl+T)
#
# Two separate costs, both outside the test's control:
#
# 1. GENERATION-2 COLLECTION. With `gc.callbacks` recording every pass,
#    each of the four idle runs above 90 ms contained exactly one gen-2
#    collection, of 85.8, 101.5, 112.3, 115.4 and 140.4 ms. Nothing else
#    correlated. A gen-2 pause costs what the live heap costs -- i.e. what
#    fraction of the suite has already run. `_without_collector_pauses`
#    takes it out of the window, because it is not what is being measured.
# 2. TEXTUAL'S AGGREGATE MOUNT COST, which survives that. Mounting a pane
#    runs ~545 `Stylesheet.apply` calls and up to 8 `Screen._refresh_layout`
#    calls as one uninterrupted run of the message pump. NO SINGLE CALL is
#    the stall -- with the collector off and a 290 MB heap, apply's worst
#    call is 1.8 ms and layout's is 96.7 ms -- but their sums (121 ms and
#    183 ms) land back to back with nothing awaiting in between, and the
#    heartbeat sees one 265 ms gap. It grows with the heap the same way,
#    which is why it reads as "fails at 85-95% of a run": conftest.py's
#    reaper clears the leaked agent subprocesses, it cannot clear the
#    pytest process's own heap. (The 305/334 ms figures in
#    tests/test_sidebar.py's rail note are this same aggregate, attributed
#    to the two functions and measured with the collector running.)
#
# So the old bar sat at 250 ms, BELOW a floor measured at 265-399 ms on
# correct code. A bar that cannot be met on correct code is not a strict
# test, it is a coin toss, and this one had already been waved off as
# noise three times.
#
# What replaces it, in the order the failures should be read:
#
#   1. the MECHANISM. The stand-in factory records `threading.get_ident()`;
#      the loop's own id must not be in it. This is the property the fix
#      actually established, it cannot be fooled by a fast machine or
#      failed by a slow one, and it is the assertion to trust.
#   2. LIVENESS while the factory blocks. The heartbeat must have woken
#      inside the factory's own blocking window. Threaded: ~200 wakes in
#      2 s. On the loop: none, by construction -- one thread cannot run
#      the heartbeat and `time.sleep` at once. The bar is a wake COUNT and
#      not a duration precisely so that a mount burst landing inside the
#      window cannot fail it.
#   3. the whole-window gap, kept but re-derived, because 1 and 2 only
#      watch the factory's own window and a future blocking call could
#      land outside it. Its bar is now set FROM the measurements above
#      rather than guessed under them.

#: How long the stand-in factory blocks. Long enough that a synchronous
#: call is unmistakable, short enough that the test costs half a second.
#: Used by the far-side test below, which only needs the spawn to be slow.
BLOCK_SECS = 0.5

#: What the two loop probes block for. Four times BLOCK_SECS, and the
#: extra 1.5 s is bought deliberately: STALL_LIMIT has to clear a 399 ms
#: measured mount cost with room to spare AND still sit well under the
#: stall the defect produces, and only a longer block gives both margins
#: at once. Pre-fix, the worst gap is this number.
PROBE_BLOCK_SECS = 2.0

#: The gap that counts as a freeze. Measured ceiling above: 399 ms of
#: Textual mount work with the collector held off, in a full run. This is
#: 2.5x that ceiling and half of PROBE_BLOCK_SECS -- the first margin is
#: why it does not fail on correct code, the second is why it still fails
#: on the defect.
STALL_LIMIT = 1.0

#: How many times the loop must wake while the factory is blocking.
#: PROBE_BLOCK_SECS / 0.01 = ~200 when it is threaded, exactly 0 when it
#: is not. A mount burst can eat a quarter of that window; it cannot take
#: it to zero.
LIVE_TICKS_MIN = 5


def _assert_the_spawn_stayed_off_the_loop(probe, beat, loop_thread, gesture):
    """The three assertions, in order of how much they should be trusted."""
    assert probe.threads, f"the session factory never ran during {gesture}"
    assert loop_thread not in probe.threads, (
        f"the session factory ran ON the event loop thread during "
        f"{gesture} — it is being called synchronously again "
        f"(SessionPane.on_mount / PaneRuntimeMixin._build_and_boot)"
    )
    began, ended = probe.windows[-1]
    awake = beat.ticks_within(began, ended)
    assert awake >= LIVE_TICKS_MIN, (
        f"the event loop woke {awake} times in the "
        f"{(ended - began) * 1000:.0f} ms the session factory spent "
        f"blocking during {gesture} — a live loop wakes about "
        f"{int((ended - began) / beat.interval)} times and a frozen one "
        f"wakes none"
    )
    assert beat.worst < STALL_LIMIT, (
        f"the event loop stalled {beat.worst * 1000:.0f} ms during "
        f"{gesture}. If the two assertions above passed, the session "
        f"factory is not the cause — it ran off the loop and the loop "
        f"kept waking through it — so either something ELSE on this "
        f"path is blocking, or Textual's mount cost has grown past the "
        f"399 ms this bar was measured against"
    )


@pytest.mark.asyncio
async def test_a_vsplit_never_blocks_the_event_loop(tmp_path):
    """The regression test for the reported lag.

    Asserts on the LOOP, not on a duration: a split is allowed to take as
    long as spawning a session takes, and is not allowed to stop the
    application while it does. That distinction is the whole fix -- the
    engine is built in a thread now, so the ~2.3 seconds still happen and
    the TUI keeps painting, keeps accepting keys and keeps streaming the
    OTHER pane's turn throughout.

    On the pre-fix code -- `await asyncio.to_thread(self._engine_factory)`
    in `PaneRuntimeMixin._build_and_boot` put back to a plain call -- all
    three assertions fail, and each was checked with the earlier ones
    neutralised: the factory runs on the loop thread; the loop wakes
    0 times in the 2000 ms block; the worst gap is 2011 ms here and
    2017 ms in the Ctrl+T probe, against a 1000 ms bar."""
    app, probe = _blocking_app(tmp_path, PROBE_BLOCK_SECS)
    async with app.run_test(size=BIG) as pilot:
        await _wait(pilot, lambda: app.active_pane is not None)
        await pilot.pause()
        loop_thread = threading.get_ident()
        beat = _Heartbeat()
        beat.start()
        await pilot.pause()
        with _without_collector_pauses():
            beat.clear()

            assert await app.split_active_pane(layout.ROW) is None
            assert await _wait(pilot, lambda: len(app.panes()) == 2)
            # The mount returns BEFORE the factory does now -- that is the
            # fix -- so the window being measured has to be waited out.
            assert await _wait(pilot, lambda: bool(probe.windows), tries=400)

            await beat.stop()
        _assert_the_spawn_stayed_off_the_loop(
            probe, beat, loop_thread, "a vsplit"
        )


@pytest.mark.asyncio
async def test_a_new_tab_never_blocks_the_event_loop_either(tmp_path):
    """Ctrl+T went through the SAME line and had the same freeze; the
    split is only where it is most visible, because splitting is the
    gesture that mounts a pane while you are watching another one stop
    repainting. Pinned separately so a future change to one path cannot
    quietly reintroduce it on the other."""
    app, probe = _blocking_app(tmp_path, PROBE_BLOCK_SECS)
    async with app.run_test(size=BIG) as pilot:
        await _wait(pilot, lambda: app.active_pane is not None)
        await pilot.pause()
        loop_thread = threading.get_ident()
        beat = _Heartbeat()
        beat.start()
        await pilot.pause()
        with _without_collector_pauses():
            beat.clear()

            await app.action_new_tab()
            assert await _wait(pilot, lambda: len(app.panes()) == 2)
            assert await _wait(pilot, lambda: bool(probe.windows), tries=400)

            await beat.stop()
        _assert_the_spawn_stayed_off_the_loop(
            probe, beat, loop_thread, "Ctrl+T"
        )


@pytest.mark.asyncio
async def test_a_slow_spawn_still_produces_a_working_pane(tmp_path):
    """Moving the factory off the loop must not lose the engine.

    The window it opens is real -- `pane.engine` is None between mount and
    the thread returning -- and it is the window `_peer_pump` has always
    modelled and `_run_turn` now waits through. This asserts the far side:
    the pane ends up holding the engine the factory built, `_boot` really
    started it, and the pane declares itself ready -- i.e. nothing about
    moving the construction into the worker dropped the handle."""
    app, _probe = _blocking_app(tmp_path, BLOCK_SECS)
    async with app.run_test(size=BIG) as pilot:
        await _wait(pilot, lambda: app.active_pane is not None)
        assert await app.split_active_pane(layout.ROW) is None
        assert await _wait(pilot, lambda: len(app.panes()) == 2)
        new_pane = app.active_pane
        assert new_pane is not None
        assert await _wait(pilot, lambda: new_pane.engine is not None, tries=400)
        assert isinstance(new_pane.engine, FakeEngine)
        assert await _wait(
            pilot, lambda: new_pane._engine_ready.is_set(), tries=400
        )
        assert new_pane.engine.started


@pytest.mark.asyncio
async def test_the_idle_app_arms_nothing_new_when_it_splits(tmp_path):
    """The reporter's FIRST guess, pinned as the fact it turned out to be.

    DOXA's no-timer rule says an idle app arms none, and a split must not
    change that -- measured directly rather than trusted, because a busy
    loop is otherwise invisible to a test suite. `_auto_refresh_timer` is
    the slot Textual's own `auto_refresh` uses and the one the existing
    no-idle-timer guards watch."""
    app, _engines = _app(tmp_path)
    async with app.run_test(size=BIG) as pilot:
        await _wait(pilot, lambda: app.active_pane is not None)
        await pilot.pause()

        def armed() -> int:
            return sum(
                1 for node in app.query("*")
                if getattr(node, "_auto_refresh_timer", None) is not None
            )

        before = armed()
        assert await app.split_active_pane(layout.ROW) is None
        assert await _wait(pilot, lambda: len(app.panes()) == 2)
        assert await app.split_active_pane(layout.COLUMN) is None
        assert await _wait(pilot, lambda: len(app.panes()) == 3)
        for _ in range(20):
            await pilot.pause()
        assert armed() == before, (
            "a split armed an auto-refresh timer — the no-timer rule is "
            "per app, not per pane"
        )

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
        # One tab, two panes -- the tab strip did not grow.
        assert len(app.query(PaneTab)) == 1
        assert left.tab is right.tab


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
        whole = first.tab.region

        await app._close_pane(new, terminate=False)
        assert await _wait(pilot, lambda: len(app.panes()) == 1)
        await pilot.pause()

        assert len(app.query(PaneTab)) == 1  # the tab is still there
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
        assert first.region.width + new.region.width == first.tab.region.width

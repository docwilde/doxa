# SPDX-License-Identifier: AGPL-3.0-only
"""Pane groups (v0.97.0) -- the inversion, against a real Pilot.

``window -> tabs -> each tab owns a tree of panes`` became
``window -> one tree of GROUPS -> each group owns its own tabs``. What
this file pins is the six things docs/plans/pane-groups.md says are most
likely to go wrong, and nothing that would pass with the screen blank --
the v0.28.0 rule the split-panes suite already states: a structural claim
is paired with a painted rectangle.

* **independence** -- the reported defect. Switching tabs in one group
  leaves every other group exactly where it was;
* **moving a tab between groups** -- the single hardest constraint in the
  spec. Textual cannot re-parent a mounted widget, so the tab is
  re-created at the destination and torn down at the source, and the
  SESSION must survive that untouched;
* **persistence across three eras** -- with THREE or more of each, because
  v0.91.0's own spec notes the old two-tab test passed only because the
  saved tab happened to be last;
* **seen state** -- an inactive tab inside a VISIBLE group is neither
  visible nor focused, so its marks must not clear;
* **Ctrl+1..9 and the number overlay** -- numbered from the painted
  rectangles in reading order, flashing even when the digit names no
  group, on a one-shot timer;
* **the tab strip's width rungs** -- measured, not chosen.
"""

from __future__ import annotations

import json

import pytest

from doxa import config as config_mod
from doxa import layout
from doxa import tabsets as tabsets_mod
from doxa.app import DoxaApp, RestoreTabSpec
from doxa.ui.split import GroupNumber, PaneGroup, PaneTab
from tests.fakes import FakeEngine


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "doxa-home"))
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    config_mod.invalidate()
    yield
    config_mod.invalidate()


def _app(tmp_path, **kwargs):
    engines: list[FakeEngine] = []

    def make() -> FakeEngine:
        engine = FakeEngine([], cwd=str(tmp_path))
        # A session id, because half of what this file asserts is that a
        # SESSION survived something -- and a session with no id is not
        # one the persisted record or a tab move can even name.
        engine.session_id = f"sid-{len(engines) + 1}"
        engines.append(engine)
        return engine

    return DoxaApp(
        cwd=str(tmp_path), engine_factory=make, new_session_factory=make,
        **kwargs,
    ), engines


def _factory(session_id: str, cwd: str = ""):
    def make() -> FakeEngine:
        engine = FakeEngine([], cwd=cwd)
        engine.session_id = session_id
        return engine

    return make


async def _wait(pilot, cond, tries=250):
    for _ in range(tries):
        if cond():
            return True
        await pilot.pause(0.02)
    return cond()


#: Big enough that no split below is refused for size.
BIG = (160, 48)


# -- the inversion, structurally ---------------------------------------


@pytest.mark.asyncio
async def test_an_unsplit_window_is_one_group_holding_every_tab(tmp_path):
    """The migration's floor: with no splits the window looks exactly as
    it did, down to the id of the one tab strip."""
    app, _engines = _app(tmp_path)
    async with app.run_test(size=BIG) as pilot:
        await pilot.pause()
        await app.action_new_tab()
        assert await _wait(pilot, lambda: len(app.panes()) == 2)
        await pilot.pause()
        assert len(app.groups()) == 1
        assert len(app.query(PaneTab)) == 2
        # The literal id every release before this one used, so an unsplit
        # window's DOM is unchanged.
        assert app.groups()[0].tabbed.id == "session-tabs"


@pytest.mark.asyncio
async def test_a_split_makes_a_SECOND_GROUP_with_its_own_tab_strip(tmp_path):
    """The whole inversion in one assertion: splitting no longer adds a
    pane to a tab, it adds a REGION with a strip of its own."""
    app, _engines = _app(tmp_path)
    async with app.run_test(size=BIG) as pilot:
        await pilot.pause()
        assert await app.split_active_pane(layout.ROW) is None
        assert await _wait(pilot, lambda: len(app.panes()) == 2)
        await pilot.pause()
        groups = app.groups()
        assert len(groups) == 2
        left, right = app._group_order()
        assert right.region.x > left.region.x
        for group in groups:
            assert group.region.width > 0 and group.region.height > 0
            assert group.tabbed.is_mounted
            assert len(group.tabs()) == 1
        # Two strips, two ids -- an id cannot be in two places.
        assert len({g.tabbed.id for g in groups}) == 2


# -- independence: the reported defect ---------------------------------


@pytest.mark.asyncio
async def test_switching_tabs_in_one_group_leaves_the_other_alone(tmp_path):
    """*"if i switch tabs, the split out sessions go with the tab.
    Shouldn't the split out sessions be independent?"* -- reported
    2026-08-31, and this is the assertion that it is fixed."""
    app, _engines = _app(tmp_path)
    async with app.run_test(size=BIG) as pilot:
        await pilot.pause()
        assert await app.split_active_pane(layout.ROW) is None
        assert await _wait(pilot, lambda: len(app.groups()) == 2)
        await pilot.pause()
        left, right = app._group_order()
        # A second tab in the LEFT group only.
        app._focus_tab(next(iter(left.surfaces())))
        await pilot.pause()
        await app.action_new_tab()
        assert await _wait(pilot, lambda: len(left.tabs()) == 2)
        await pilot.pause()

        right_pane_before = next(iter(right.surfaces()))
        right_active_before = right.tabbed.active
        left_active_before = left.tabbed.active

        await pilot.press("ctrl+right")
        await pilot.pause()

        assert left.tabbed.active != left_active_before, "the focused group cycled"
        assert right.tabbed.active == right_active_before, "the other group did not"
        assert next(iter(right.surfaces())) is right_pane_before
        assert right_pane_before.region.width > 0, "and it is still painted"


@pytest.mark.asyncio
async def test_closing_a_tab_closes_ONE_session(tmp_path):
    """Through v0.95.0 closing a tab that held a three-way split ended
    three sessions. A tab holds one surface now, so it ends one."""
    app, engines = _app(tmp_path)
    async with app.run_test(size=BIG) as pilot:
        await pilot.pause()
        await app.action_new_tab()
        assert await _wait(pilot, lambda: len(app.panes()) == 2)
        await pilot.pause()
        assert await app.split_active_pane(layout.ROW) is None
        assert await _wait(pilot, lambda: len(app.panes()) == 3)
        await pilot.pause()
        assert len(app.groups()) == 2

        victim = app.active_pane
        survivors = [p for p in app.panes() if p is not victim]
        await app._close_pane(victim, terminate=True)
        assert await _wait(pilot, lambda: len(app.panes()) == 2)
        await pilot.pause()
        assert sorted(p._session_id for p in app.panes()) == sorted(
            p._session_id for p in survivors
        )
        for engine in engines:
            if engine is victim.engine:
                continue
        assert sum(1 for e in engines if e.finalized) == 1


# -- moving a tab between groups: the hardest constraint ----------------


@pytest.mark.asyncio
async def test_moving_a_tab_between_groups_keeps_the_SAME_SESSION_running(
    tmp_path,
):
    """The single hardest constraint in the spec, proved rather than
    asserted.

    Textual 5.3 cannot re-parent a mounted widget -- ``mount`` of a mounted
    widget is a silent no-op that ORPHANS it -- so the tab CANNOT move. It
    is re-created at the destination and torn down at the source, and the
    session, which lives in the daemon and not in the widget, must not
    notice: same engine object, same session id, never finalized, and its
    new pane genuinely painted in the destination group."""
    app, engines = _app(tmp_path)
    async with app.run_test(size=BIG) as pilot:
        await pilot.pause()
        assert await app.split_active_pane(layout.ROW) is None
        assert await _wait(pilot, lambda: len(app.groups()) == 2)
        await pilot.pause()
        left, right = app._group_order()
        # Two tabs in the left group, so moving one is not also a close.
        app._focus_tab(next(iter(left.surfaces())))
        await pilot.pause()
        await app.action_new_tab()
        assert await _wait(pilot, lambda: len(left.tabs()) == 2)
        await pilot.pause()

        travelling = app.active_pane
        engine = travelling.engine
        session_id = travelling._session_id
        assert engine is not None and session_id
        old_widget = travelling

        assert await app.move_tab_to_group(2) is None
        assert await _wait(pilot, lambda: len(right.tabs()) == 2)
        await pilot.pause()

        arrived = next(
            (p for p in app.panes() if p._session_id == session_id), None
        )
        assert arrived is not None, "the session came with the tab"
        # A DIFFERENT widget -- the constraint, stated as an assertion.
        assert arrived is not old_widget
        # Torn down at the source, not re-parented: out of the DOM, out of
        # panes(), and with no parent left. (Textual leaves ``is_mounted``
        # True on the descendants of a removed subtree, so the parent link
        # is the honest check -- measured here rather than assumed.)
        assert await _wait(pilot, lambda: old_widget not in app.panes())
        assert old_widget.parent is None
        # ...and no second engine was built for the destination: the SAME
        # handle was re-seated, which is what "the session survived" means.
        assert len(engines) == 3
        # The SAME session, still running.
        assert arrived.engine is engine
        assert engine.finalized is False
        assert arrived._session_id == session_id
        # In the destination group, and painted there.
        from doxa.ui import split as split_mod

        assert split_mod.group_of(arrived) is right
        assert arrived.region.width > 0 and arrived.region.height > 0
        assert arrived.region.x >= right.region.x
        # The source group kept its other tab and its region.
        assert len(left.tabs()) == 1
        assert left.region.width > 0


@pytest.mark.asyncio
async def test_moving_the_last_tab_out_of_a_group_is_refused_in_words(tmp_path):
    """A move that would also close a region is two gestures, and they
    have different undo stories. Refused, and nothing changes."""
    app, _engines = _app(tmp_path)
    async with app.run_test(size=BIG) as pilot:
        await pilot.pause()
        assert await app.split_active_pane(layout.ROW) is None
        assert await _wait(pilot, lambda: len(app.groups()) == 2)
        await pilot.pause()
        before = [len(g.tabs()) for g in app._group_order()]
        # Focus is in the group the split just made (v0.91.0's rule, which
        # groups inherit), so group 1 is the one it is not already in.
        note = await app.move_tab_to_group(1)
        assert note and "last tab" in note
        await pilot.pause()
        assert [len(g.tabs()) for g in app._group_order()] == before


@pytest.mark.asyncio
async def test_movepane_refuses_a_group_number_that_names_nothing(tmp_path):
    app, _engines = _app(tmp_path)
    async with app.run_test(size=BIG) as pilot:
        await pilot.pause()
        assert await app.split_active_pane(layout.ROW) is None
        assert await _wait(pilot, lambda: len(app.groups()) == 2)
        await pilot.pause()
        note = await app.move_tab_to_group(7)
        assert note and "no pane group 7" in note


# -- Ctrl+1..9, numbering, and the overlay ------------------------------


@pytest.mark.asyncio
async def test_groups_are_numbered_in_reading_order_from_the_rectangles(
    tmp_path,
):
    """Left to right, then top to bottom -- derived from what is PAINTED,
    never from tree order, because what the user counts is what is on
    screen. A 2x2 numbers 1 2 / 3 4."""
    app, _engines = _app(tmp_path)
    async with app.run_test(size=BIG) as pilot:
        await pilot.pause()
        # A -> A | B, then split each half vertically: a real 2x2.
        assert await app.split_active_pane(layout.ROW) is None
        assert await _wait(pilot, lambda: len(app.groups()) == 2)
        await pilot.pause()
        assert await app.split_active_pane(layout.COLUMN) is None
        assert await _wait(pilot, lambda: len(app.groups()) == 3)
        await pilot.pause()
        first = app._group_order()[0]
        app._focus_tab(next(iter(first.surfaces())))
        await pilot.pause()
        assert await app.split_active_pane(layout.COLUMN) is None
        assert await _wait(pilot, lambda: len(app.groups()) == 4)
        await pilot.pause()

        order = app._group_order()
        assert len(order) == 4
        boxes = [(g.region.x, g.region.y) for g in order]
        # Reading order: sorted by row first, then column.
        assert boxes == sorted(boxes, key=lambda xy: (xy[1], xy[0]))
        assert boxes[0][1] == boxes[1][1], "1 and 2 share a row"
        assert boxes[0][0] < boxes[1][0], "1 is left of 2"
        assert boxes[2][1] > boxes[0][1], "3 is below 1"


@pytest.mark.asyncio
async def test_ctrl_digit_jumps_to_that_group_immediately(tmp_path):
    app, _engines = _app(tmp_path)
    async with app.run_test(size=BIG) as pilot:
        await pilot.pause()
        assert await app.split_active_pane(layout.ROW) is None
        assert await _wait(pilot, lambda: len(app.groups()) == 2)
        await pilot.pause()
        first, second = app._group_order()
        app._focus_tab(next(iter(second.surfaces())))
        await pilot.pause()
        assert app.focused_group() is second

        app.action_focus_group(1)
        await pilot.pause()
        assert app.focused_group() is first, "the jump is not deferred"


@pytest.mark.asyncio
async def test_the_overlay_flashes_every_group_and_fires_on_a_miss(tmp_path):
    """It fires even when the digit names NO group -- Ctrl+7 in a two-group
    layout shows 1 and 2 and moves nothing. That is the case it earns the
    most in: it answers "what are my choices" for a user who guessed."""
    app, _engines = _app(tmp_path)
    async with app.run_test(size=BIG) as pilot:
        await pilot.pause()
        assert await app.split_active_pane(layout.ROW) is None
        assert await _wait(pilot, lambda: len(app.groups()) == 2)
        await pilot.pause()
        here = app.focused_group()

        app.action_focus_group(7)
        await pilot.pause()

        assert app.focused_group() is here, "a miss moves nothing"
        numbers = []
        for group in app._group_order():
            overlay = group.query_one(GroupNumber)
            assert overlay.styles.display == "block"
            numbers.append(str(overlay.renderable))
        assert numbers == ["1", "2"]
        assert app._group_flash_timer is not None, "a ONE-SHOT timer, armed"


@pytest.mark.asyncio
async def test_a_single_group_window_flashes_nothing(tmp_path):
    """Hide-at-zero: with no splits there is no choice to make."""
    app, _engines = _app(tmp_path)
    async with app.run_test(size=BIG) as pilot:
        await pilot.pause()
        app.action_focus_group(1)
        await pilot.pause()
        assert app._group_flash_timer is None
        overlay = app.groups()[0].query_one(GroupNumber)
        assert overlay.styles.display == "none"


@pytest.mark.asyncio
async def test_the_next_key_takes_the_overlay_away(tmp_path):
    """Cancelled on any subsequent key, so a user already moving never
    waits for the timer."""
    app, _engines = _app(tmp_path)
    async with app.run_test(size=BIG) as pilot:
        await pilot.pause()
        assert await app.split_active_pane(layout.ROW) is None
        assert await _wait(pilot, lambda: len(app.groups()) == 2)
        await pilot.pause()
        app.action_focus_group(1)
        await pilot.pause()
        assert app._group_flash_timer is not None

        await pilot.press("a")
        await pilot.pause()
        assert app._group_flash_timer is None
        for group in app.groups():
            assert group.query_one(GroupNumber).styles.display == "none"


@pytest.mark.asyncio
async def test_slash_pane_is_the_door_that_always_works(tmp_path):
    """Ctrl+<digit> is unreachable under the legacy key encoding
    (doxa.keyboard). /pane is not, and it reaches the same place."""
    app, _engines = _app(tmp_path)
    async with app.run_test(size=BIG) as pilot:
        await pilot.pause()
        assert await app.split_active_pane(layout.ROW) is None
        assert await _wait(pilot, lambda: len(app.groups()) == 2)
        await pilot.pause()
        first, second = app._group_order()
        app._focus_tab(next(iter(second.surfaces())))
        await pilot.pause()
        assert app.focus_group_number(1) is None
        await pilot.pause()
        assert app.focused_group() is first
        note = app.focus_group_number(9)
        assert note and "no pane group 9" in note


# -- seen state ---------------------------------------------------------


@pytest.mark.asyncio
async def test_an_inactive_tab_in_a_visible_group_is_not_seen(tmp_path):
    """The spec settles this one level up from v0.91.0: a pane that is
    merely VISIBLE has not been seen, and an INVISIBLE tab is the stronger
    case of the same thing. -done-unseen, the needs-input blink and the
    -staged tint must all survive."""
    app, _engines = _app(tmp_path)
    async with app.run_test(size=BIG) as pilot:
        await pilot.pause()
        assert await app.split_active_pane(layout.ROW) is None
        assert await _wait(pilot, lambda: len(app.groups()) == 2)
        await pilot.pause()
        left, right = app._group_order()
        app._focus_tab(next(iter(left.surfaces())))
        await pilot.pause()
        buried = app.active_pane
        await app.action_new_tab()
        assert await _wait(pilot, lambda: len(left.tabs()) == 2)
        await pilot.pause()
        # `buried` is now an INACTIVE tab of a VISIBLE group.
        assert buried.region.width == 0, "not painted"
        assert buried.is_mounted, "still mounted, still running"

        buried._set_tab_class("-done-unseen", True)
        buried.set_needs_input(True)
        buried.set_staged(True)

        # Everything that is not "the keyboard arrived in THIS pane".
        app._focus_tab(next(iter(right.surfaces())))
        await pilot.pause()
        app.action_focus_group(1)
        await pilot.pause()

        assert buried.has_mark("-done-unseen")
        # The blink is a mechanism, not a class: `needs_input` is what
        # arms it, and the class flickers underneath.
        assert buried.needs_input is True
        assert buried.has_mark("-staged")

        # ...and the keyboard actually arriving DOES clear them.
        app._focus_tab(buried)
        await pilot.pause()
        assert not buried.has_mark("-done-unseen")
        assert buried.needs_input is False
        assert not buried.has_mark("-staged")


# -- the tab strip's width rungs ---------------------------------------


def test_the_tab_strip_thresholds_are_the_measured_ones():
    """The measurement, restated where it can fail if either input moves.

    A tab header costs its label plus Textual's own ``Tab`` padding of one
    column each side. The label's documented floor is
    ``TAB_MODEL_MIN + " · " + TAB_REPO_MIN`` plus the provider glyph and
    its space. A strip that can show TWO full headers -- the least a tab
    strip is FOR -- is the compact threshold; one that can show ONE is the
    floor below which there is no strip at all."""
    from doxa.ui.labels import TAB_MODEL_MIN, TAB_REPO_MIN

    label_floor = TAB_MODEL_MIN + len(" · ") + TAB_REPO_MIN
    header = label_floor + len("✳ ") + 2  # Tab's own `padding: 0 1`
    assert header == 17
    assert layout.GROUP_STRIP_MIN_COLS == header
    assert layout.GROUP_STRIP_COMPACT_COLS == header * 2
    # And the rung a real split actually lands on. This is NOT a
    # coincidence: MIN_LEAF_WIDTH's own comment derives it from the same
    # two label floors ("the narrowest the status bar's own chip row stays
    # legible at"), so the narrowest group DOXA will create sits exactly
    # ON the compact boundary -- it can just show two full headers, and one
    # column narrower it compacts.
    assert layout.MIN_LEAF_WIDTH == layout.GROUP_STRIP_COMPACT_COLS


@pytest.mark.asyncio
async def test_a_narrow_group_renders_its_strip_compactly(tmp_path):
    """Two tab strips is more chrome than one. Below the measured width a
    group compacts its strip; below the floor it drops it entirely -- and
    the row it gives back goes to the transcript, which is the difference
    between hiding a widget and zeroing its height."""
    app, _engines = _app(tmp_path)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        assert await app.split_active_pane(layout.ROW) is None
        assert await _wait(pilot, lambda: len(app.groups()) == 2)
        # THE RAIL IS SHUT, said rather than assumed. This test is about
        # what a group's WIDTH does to its tab strip, and the rail takes
        # columns off the tree -- two sessions in a 100-column window is
        # exactly the case its `auto` mode opens for (v1.5.0 stopped it
        # double-counting its own columns and flapping shut again), so
        # leaving it to the heuristic would make the number below depend
        # on chrome this test is not measuring.
        assert app.set_sidebar(False) is None
        await pilot.pause()
        for group in app.groups():
            assert group.region.width == 50
            assert not group.has_class("-strip-compact")
            assert not group.has_class("-strip-hidden")

        # Now force each group under both rungs and check the class moves.
        group = app.groups()[0]
        group.size  # noqa: B018 -- documents that the rung is size-driven
        for width, compact, hidden in (
            (layout.GROUP_STRIP_COMPACT_COLS - 1, True, False),
            (layout.GROUP_STRIP_MIN_COLS - 1, False, True),
            (layout.GROUP_STRIP_COMPACT_COLS + 1, False, False),
        ):
            group._apply_strip_width_for(width)
            assert group.has_class("-strip-compact") is compact, width
            assert group.has_class("-strip-hidden") is hidden, width


# -- persistence: three eras, three or more of each ---------------------


def _write_record(tmp_path, payload: dict) -> str:
    scope = str(tmp_path)
    monkey = tabsets_mod._file_for(scope)
    monkey.write_text(json.dumps(payload), encoding="utf-8")
    return scope


def _rows(*session_ids: str) -> list:
    return [
        {"session_id": sid, "pinned_name": None, "cwd": None}
        for sid in session_ids
    ]


def test_era_one_a_flat_record_reads_as_one_group_of_tabs(tmp_path):
    """v0.23.0 to v0.90.0: no ``trees``, no ``groups``. N tabs were N
    tabs, one at a time, in one region -- so one group holding all of them
    says exactly that, and the saved active tab is the one it shows.

    FOUR tabs, and the active one is the SECOND: v0.91.0's own spec notes
    the old two-tab test passed only because the saved tab happened to be
    last."""
    scope = _write_record(tmp_path, {
        "scope_key": str(tmp_path),
        "active_session_id": "sid-2",
        "tabs": _rows("sid-1", "sid-2", "sid-3", "sid-4"),
        "layout": {"kind": "tabs", "tabs": _rows(
            "sid-1", "sid-2", "sid-3", "sid-4"
        )},
    })
    record = tabsets_mod.load(scope)
    assert record is not None
    tree = record.groups
    assert isinstance(tree, layout.Group)
    assert [leaf.session_id for leaf in tree.tabs] == [
        "sid-1", "sid-2", "sid-3", "sid-4",
    ]
    assert tree.active == 1
    assert tree.active_tab.session_id == "sid-2"


def test_era_two_trees_without_groups_read_as_one_group_per_leaf(tmp_path):
    """v0.91.0 to v0.95.0: ``trees``, one per TAB. The absence of
    ``groups`` is the migration -- each leaf of the tree that was ACTIVE
    becomes a single-tab group, and every other saved tab becomes a tab of
    the group holding the active session.

    THREE trees, and the active session is in the SECOND of them."""
    trees = [
        layout.to_json(layout.Leaf("sid-1")),
        layout.to_json(layout.Split(
            layout.ROW,
            (layout.Leaf("sid-2"), layout.Leaf("sid-3"), layout.Leaf("sid-4")),
        )),
        layout.to_json(layout.Leaf("sid-5")),
    ]
    scope = _write_record(tmp_path, {
        "scope_key": str(tmp_path),
        "active_session_id": "sid-3",
        "tabs": _rows("sid-1", "sid-2", "sid-3", "sid-4", "sid-5"),
        "layout": {
            "kind": "tabs",
            "tabs": _rows("sid-1", "sid-2", "sid-3", "sid-4", "sid-5"),
            "trees": trees,
        },
    })
    record = tabsets_mod.load(scope)
    assert record is not None
    tree = record.groups
    # The active tab's own tree became the window: three regions.
    assert isinstance(tree, layout.Split)
    groups = layout.groups(tree)
    assert len(groups) == 3
    assert [g.tabs[0].session_id for g in groups] == ["sid-2", "sid-3", "sid-4"]
    # ...and the tabs the tree did not place joined the FIRST group.
    assert [leaf.session_id for leaf in groups[0].tabs] == [
        "sid-2", "sid-1", "sid-5",
    ]
    # The saved active session is what its own group shows.
    assert groups[1].active_tab.session_id == "sid-3"
    # Nothing was lost and nothing was duplicated.
    assert sorted(leaf.session_id for leaf in layout.leaves(tree)) == [
        "sid-1", "sid-2", "sid-3", "sid-4", "sid-5",
    ]


def test_era_three_a_groups_record_round_trips_exactly(tmp_path):
    """v0.97.0. Three groups, and one of them holds three tabs with the
    SECOND active -- the shape the two eras above cannot express and the
    reason this key exists."""
    tree = layout.Split(
        layout.ROW,
        (
            layout.Group(
                (layout.Leaf("sid-1"), layout.Leaf("sid-2"), layout.Leaf("sid-3")),
                1,
            ),
            layout.Group((layout.Leaf("sid-4"),), 0),
            layout.Group((layout.Leaf("sid-5"), layout.Leaf("sid-6")), 1),
        ),
        (0.5, 0.25, 0.25),
    )
    tabs = [
        tabsets_mod.TabRecord(f"sid-{n}", None, str(tmp_path))
        for n in range(1, 7)
    ]
    tabsets_mod.save(str(tmp_path), tabs, "sid-2", groups=tree)
    record = tabsets_mod.load(str(tmp_path))
    assert record is not None
    assert record.groups == tree
    # The FLAT list stays authoritative and complete: every session, in
    # layout order, so an older DOXA restores N tabs rather than nothing.
    assert [t.session_id for t in record.tabs] == [f"sid-{n}" for n in range(1, 7)]


def test_a_groups_record_still_carries_the_older_trees_shape(tmp_path):
    """What a v0.91.0-v0.95.0 DOXA sees: one tree per GROUP, each region's
    leaf being that group's ACTIVE tab. It is the most of this record's
    truth that shape can hold, and the tabs it cannot express are still in
    the flat list."""
    tree = layout.Split(
        layout.ROW,
        (
            layout.Group((layout.Leaf("sid-1"), layout.Leaf("sid-2")), 1),
            layout.Group((layout.Leaf("sid-3"),), 0),
            layout.Group((layout.Leaf("sid-4"), layout.Leaf("sid-5")), 0),
        ),
    )
    tabs = [
        tabsets_mod.TabRecord(f"sid-{n}", None, str(tmp_path))
        for n in range(1, 6)
    ]
    tabsets_mod.save(str(tmp_path), tabs, "sid-2", groups=tree)
    raw = json.loads(tabsets_mod._file_for(str(tmp_path)).read_text())
    assert raw["layout"]["kind"] == "tabs", "every DOXA since v0.32.0 reads this"
    old = raw["layout"]["trees"]
    assert len(old) == 1
    read_back = layout.from_json(old[0])
    assert [leaf.session_id for leaf in layout.leaves(read_back)] == [
        "sid-2", "sid-3", "sid-4",
    ]
    # ...and the two the old shape dropped are still in the flat list.
    assert [row["session_id"] for row in raw["tabs"]] == [
        f"sid-{n}" for n in range(1, 6)
    ]


def test_an_unreadable_groups_node_falls_back_rather_than_losing_tabs(tmp_path):
    """A layout is chrome; a corrupt one costs the user their arrangement,
    never their sessions."""
    scope = _write_record(tmp_path, {
        "scope_key": str(tmp_path),
        "active_session_id": None,
        "tabs": _rows("sid-1", "sid-2", "sid-3"),
        "layout": {
            "kind": "tabs",
            "tabs": _rows("sid-1", "sid-2", "sid-3"),
            "groups": {"kind": "not-a-thing-this-version-knows"},
        },
    })
    record = tabsets_mod.load(scope)
    assert record is not None
    assert [t.session_id for t in record.tabs] == ["sid-1", "sid-2", "sid-3"]
    assert sorted(
        leaf.session_id for leaf in layout.leaves(record.groups)
    ) == ["sid-1", "sid-2", "sid-3"]


def test_pruning_a_group_drops_dead_tabs_and_keeps_the_region(tmp_path):
    """Three tabs of which one session survived is still a group, showing
    the survivor. Only a group that lost every tab is gone."""
    tree = layout.Split(
        layout.ROW,
        (
            layout.Group(
                (layout.Leaf("a"), layout.Leaf("b"), layout.Leaf("c")), 2
            ),
            layout.Group((layout.Leaf("d"),), 0),
        ),
    )
    kept = layout.prune(tree, ["a", "c"])
    # The second group lost its only tab, so the split collapsed.
    assert isinstance(kept, layout.Group)
    assert [leaf.session_id for leaf in kept.tabs] == ["a", "c"]
    # ...and the active tab followed its session rather than its index.
    assert kept.active_tab.session_id == "c"


# -- restore, end to end ------------------------------------------------


@pytest.mark.asyncio
async def test_a_saved_two_group_layout_restores_as_two_groups(tmp_path):
    """The round trip that matters: what the user left is what comes
    back, painted."""
    where = tmp_path / "scope"
    where.mkdir()
    tree = layout.Split(
        layout.ROW,
        (
            layout.Group((layout.Leaf("sid-1"), layout.Leaf("sid-2")), 1),
            layout.Group((layout.Leaf("sid-3"),), 0),
        ),
    )
    specs = [
        RestoreTabSpec(f"sid-{n}", _factory(f"sid-{n}", str(where)))
        for n in (1, 2, 3)
    ]
    app = DoxaApp(
        cwd=str(where), restore_tabs=specs, restore_groups=tree,
        restore_active_id="sid-2",
    )
    async with app.run_test(size=BIG) as pilot:
        assert await _wait(pilot, lambda: len(app.panes()) == 3)
        await pilot.pause()
        groups = app._group_order()
        assert len(groups) == 2
        assert [len(g.tabs()) for g in groups] == [2, 1]
        assert groups[1].region.x > groups[0].region.x
        for group in groups:
            assert group.region.width > 0 and group.region.height > 0
        # The saved active tab is the one its group is SHOWING.
        showing = next(iter(groups[0].surfaces()))
        assert showing._session_id == "sid-2"


@pytest.mark.asyncio
async def test_the_persisted_record_names_every_group_and_tab(tmp_path):
    """What a window writes is what it would restore to."""
    app, _engines = _app(tmp_path)
    async with app.run_test(size=BIG) as pilot:
        await pilot.pause()
        assert await _wait(pilot, lambda: app.active_pane._session_id != "")
        assert await app.split_active_pane(layout.ROW) is None
        assert await _wait(pilot, lambda: len(app.groups()) == 2)
        await pilot.pause()
        assert await _wait(
            pilot, lambda: all(p._session_id for p in app.panes())
        )
        app._persist_tabset()
        record = tabsets_mod.load(str(tmp_path))
        assert record is not None
        tree = record.groups
        assert isinstance(tree, layout.Split)
        assert len(layout.groups(tree)) == 2
        assert len(record.tabs) == 2


@pytest.mark.asyncio
async def test_the_record_follows_the_keyboard_into_the_new_group(tmp_path):
    """A split moves the keyboard into the group it made (v0.91.0's rule,
    which groups inherit), and the persisted record follows it.

    Not asserted synchronously, on purpose. ``Widget.focus()`` is DEFERRED
    in Textual 5.3, so for one message-pump turn after a split
    ``self.focused`` -- and therefore ``focused_group()`` -- still names
    the group the user came FROM. Believing ``_focus_tab``'s intent over
    the DOM instead was tried and reverted: an unpainted group has a
    zero-area rectangle, which makes the next split refuse and
    ``active_pane`` answer with a pane the keyboard has not reached. See
    ``DoxaApp.focused_group``'s own docstring. What has to be true is that
    the record is right ONCE the move has landed, and it is -- the
    restored pane's own boot persists again (``_note_pane_booted``)."""
    app, _engines = _app(tmp_path)
    async with app.run_test(size=BIG) as pilot:
        await pilot.pause()
        assert await _wait(pilot, lambda: app.active_pane._session_id != "")
        came_from = app.active_pane._session_id
        assert await app.split_active_pane(layout.ROW) is None
        assert await _wait(pilot, lambda: len(app.groups()) == 2)
        assert await _wait(
            pilot, lambda: all(p._session_id for p in app.panes())
        )
        assert await _wait(
            pilot, lambda: app.active_pane._session_id != came_from
        ), "the split moved the keyboard into the group it made"
        arrived = app.active_pane
        from doxa.ui import split as split_mod

        assert app.focused_group() is split_mod.group_of(arrived)
        app._persist_tabset()
        record = tabsets_mod.load(str(tmp_path))
        assert record is not None
        assert record.active_session_id == arrived._session_id


# -- the check this spec owes itself -----------------------------------


@pytest.mark.asyncio
async def test_a_group_can_hold_a_diff_leaf(tmp_path):
    """v0.91.0 asked whether its layout could express its first consumer
    and answered "nearly -- the geometry worked, the model could not". The
    equivalent here, answered by construction: a group's tab list is a
    list of SURFACES, and v0.92.0's diff is a surface. No special case."""
    app, _engines = _app(tmp_path)
    async with app.run_test(size=BIG) as pilot:
        await pilot.pause()
        assert await _wait(pilot, lambda: app.active_pane._session_id != "")
        pane = app.active_pane
        assert await app.toggle_diff_pane() is None
        assert await _wait(pilot, lambda: len(app.groups()) == 2)
        await pilot.pause()

        diff = app.diff_pane_for(pane._session_id)
        assert diff is not None
        from doxa.ui import split as split_mod

        holder = split_mod.group_of(diff)
        assert isinstance(holder, PaneGroup)
        # The tab it sits in is an ordinary PaneTab in an ordinary strip.
        tab = split_mod.tabbed_of(diff)
        assert tab is holder.tabbed
        assert isinstance(diff.parent, PaneTab)
        # ...and the group answers for it in the layout model, with no
        # branch anywhere that mentions diffs.
        model = holder.layout_group()
        assert model is not None
        assert len(model.tabs) == 1
        assert model.tabs[0].is_diff
        assert model.tabs[0].session_id == pane._session_id
        # Both painted, side by side, which is what the diff is FOR.
        assert diff.region.width > 0 and pane.region.width > 0
        assert diff.region.x > pane.region.x


# -- the accessors, on a window that is gone ---------------------------


def test_the_group_accessors_answer_none_when_there_is_no_window(tmp_path):
    """"Which group holds the keyboard" must never RAISE -- not even after
    the window it is about has been torn down.

    Every one of these is read from message handlers, and Textual drains
    the app's own message queue AFTER ``_close_all`` has cleared the screen
    stacks: a ``DescendantFocus`` posted as the last screen lets go of its
    widgets reaches ``_hold_focus_for_a_blocking_dialog`` -> ``active_pane``
    with no screen left to ask. ``App.focused`` raises ``ScreenStackError``
    there, and through v0.96.0 nothing noticed because ``active_pane``
    asked the strip first and answered None; making the GROUP the first
    question put that raise in front of every caller -- the error surface
    included, which then could not draw the block about it either. The
    whole failure was one unguarded property read, so this pins the
    contract at the accessors rather than at any one handler.

    An app that has never been started is the same state as one whose
    window has gone (an empty ``_screen_stack``), and it needs no engine,
    no pilot and no timing to be in it."""
    app = DoxaApp(cwd=str(tmp_path))
    assert app.focused_group() is None
    assert app.focused_pane() is None
    assert app.focused_surface() is None
    assert app.active_pane is None
    # The error surface is the caller that made this fatal rather than
    # merely wrong: it has to survive being asked where to draw a block
    # while the window is going away.
    assert app._failure_surface() is None

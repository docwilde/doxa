# SPDX-License-Identifier: AGPL-3.0-only
"""The session sidebar (v1.0.0) -- the first chrome that is NOT part of
the layout tree.

docs/plans/session-sidebar.md names what is most likely to go wrong, and
this file pins exactly those things and nothing that would pass with the
screen blank -- the v0.28.0 rule the split-panes and pane-groups suites
already state: a structural claim is paired with a painted rectangle.

* **the boundary** -- the rail is a SIBLING of the window root, not a node
  in it. ``_window_root()`` still answers, splits never see the rail,
  ``_pane_regions`` never names it. This is the one decision the whole
  design rests on and the one that a later refactor could silently undo;
* **the width refusal** -- MEASURED, not chosen, and re-derived here from
  the label constants it is derived from, so moving either end moves the
  test;
* **one source of truth for a mark** -- the rail and the tab header read
  the same derivation, so they cannot disagree;
* **the record** -- absence of the ``collections`` key is the migration,
  ``layout.kind`` stays ``"tabs"``, and a member not in ``tabs`` is
  dropped;
* **the design check the spec owes itself** -- can the rail show a session
  that is not mounted in any group? If it cannot, it is a second tab strip
  and the design is wrong.
"""

from __future__ import annotations

import json

import pytest

from doxa import collections as collections_mod
from doxa import config as config_mod
from doxa import layout
from doxa import tabsets as tabsets_mod
from doxa.app import DoxaApp
from doxa.ui import labels as labels_mod
from doxa.ui.sidebar import LOOSE_HEADING, NOT_OPEN, Row, SessionSidebar, SidebarLine, build_rows
from doxa.ui.split import PaneGroup, SplitBox
from tests.fakes import FakeEngine


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "doxa-home"))
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    config_mod.invalidate()
    yield
    config_mod.invalidate()


def _app(tmp_path, **kwargs):
    engines: "list[FakeEngine]" = []

    def make() -> FakeEngine:
        engine = FakeEngine([], cwd=str(tmp_path))
        engine.session_id = f"sid-{len(engines) + 1}"
        engines.append(engine)
        return engine

    return DoxaApp(
        cwd=str(tmp_path), engine_factory=make, new_session_factory=make,
        **kwargs,
    ), engines


async def _wait(pilot, cond, tries=250):
    for _ in range(tries):
        if cond():
            return True
        await pilot.pause(0.02)
    return cond()


#: Wide enough that the rail never hits its own width refusal, and no
#: split below is refused for size.
BIG = (160, 48)


def _describe(labels):
    """A ``build_rows`` describe callback out of a plain dict."""
    def describe(session_id):
        return labels.get(session_id, ("", (), False))

    return describe


def _lines(app):
    """The rail's SHOWING lines. Not ``query(SidebarLine)``: the rail
    reuses its line widgets and hides the surplus rather than removing
    them (see ``SessionSidebar.set_rows``), so a raw query answers with
    rows that are no longer on the rail."""
    rail = app.sidebar()
    return rail.lines() if rail is not None else []


def _settled(app, expected=None):
    """Is the rail PAINTED and showing the model it claims to?

    ``SessionSidebar.rows()`` is only written once the lines have actually
    been re-pointed, so agreement between it and the painted widgets is
    the real "the rail is showing this" -- and a line COUNT alone is not:
    it matches the previous state just as happily, which is how a click
    landed on a row the rail had already moved on from.

    Painted, not merely mounted -- the v0.28.0 rule the split-panes suite
    states: a widget with no rectangle is not one a click can reach."""
    rail = app.sidebar()
    if rail is None or rail.region.width == 0:
        return False
    lines = rail.lines()
    if len(lines) != len(rail.rows()):
        return False
    if expected is not None and len(lines) != expected:
        return False
    if not all(line.is_mounted and line.region.width > 0 for line in lines):
        return False
    # ...and the surplus really is hidden, not merely off the model: a
    # line the rail no longer shows must have no rectangle at all.
    return all(line.region.height == 0 for line in rail._pool[len(lines):])


# -- the model: a collection is a name, an order and a fold ------------


def test_a_session_belongs_to_at_most_one_collection():
    """The invariant the whole feature rests on, enforced in the MODEL so
    no caller can violate it by forgetting."""
    items, note = collections_mod.assign((), "ampiric", "abc")
    assert note is None
    items, note = collections_mod.assign(items, "doxa", "abc")
    assert note is None
    assert [c.name for c in items] == ["ampiric", "doxa"]
    assert items[0].sessions == ()          # moved OUT of the first
    assert items[1].sessions == ("abc",)
    assert collections_mod.collection_of(items, "abc").name == "doxa"


def test_re_adding_a_session_to_its_own_collection_changes_nothing():
    """The order in a collection is the user's. Running the same command
    twice must not quietly reorder it, and must not duplicate the id."""
    items, _ = collections_mod.assign((), "ampiric", "abc")
    items, _ = collections_mod.assign(items, "ampiric", "def")
    again, note = collections_mod.assign(items, "ampiric", "abc")
    assert note is None
    assert again == items
    assert again[0].sessions == ("abc", "def")


def test_an_empty_collection_survives_a_prune_but_a_emptied_one_does_not():
    """``new`` makes an empty collection ON PURPOSE -- the user is about
    to move a session into it -- so a prune must tell "never had any"
    apart from "has none left"."""
    fresh, _ = collections_mod.new((), "ampiric")
    assert collections_mod.prune(fresh, []) == fresh
    filled, _ = collections_mod.assign((), "doxa", "abc")
    assert collections_mod.prune(filled, []) == ()


def test_deleting_a_collection_ungroups_its_sessions_rather_than_losing_them():
    """A grouping is a LABEL. Deleting a label must never be a way to lose
    a session -- so the sessions fall back under the implicit heading."""
    items, _ = collections_mod.assign((), "ampiric", "abc")
    items, note = collections_mod.delete(items, "ampiric")
    assert note is None
    assert items == ()
    assert collections_mod.loose(items, ["abc"]) == ["abc"]


def test_rename_refuses_a_name_already_taken_and_changes_nothing():
    items, _ = collections_mod.new((), "ampiric")
    items, _ = collections_mod.new(items, "doxa")
    after, note = collections_mod.rename(items, "doxa", "AMPIRIC")
    assert note == "there is already a collection called 'AMPIRIC'"
    assert after == items  # a refusal changes nothing at all


def test_a_collection_naming_a_dead_session_is_pruned_like_a_dead_leaf():
    """doxa.layout.prune's rule, one shelf over: the record names
    sessions, and by the time it is read some of them are gone."""
    items, _ = collections_mod.assign((), "ampiric", "abc")
    items, _ = collections_mod.assign(items, "ampiric", "gone")
    items, _ = collections_mod.assign(items, "empty", "also-gone")
    pruned = collections_mod.prune(items, ["abc"])
    assert [c.name for c in pruned] == ["ampiric"]
    assert pruned[0].sessions == ("abc",)


def test_a_hand_edited_record_naming_one_session_twice_reads_once():
    """from_json enforces the invariants on the way IN, so no reader
    downstream has to. A record is a record even when hand-edited."""
    items = collections_mod.from_json([
        {"name": "a", "sessions": ["x", "y"]},
        {"name": "A", "sessions": ["z"]},        # same name, case-folded
        {"name": "b", "sessions": ["x", "q"]},   # x already placed
    ])
    assert [c.name for c in items] == ["a", "b"]
    assert items[0].sessions == ("x", "y")
    assert items[1].sessions == ("q",)


# -- the width: MEASURED, not chosen -----------------------------------


def test_the_sidebar_width_thresholds_are_the_measured_ones():
    """Re-derived here from the constants they are derived FROM, so
    moving either end moves this test rather than leaving a stale number
    documented as measured.

    The same discipline tests/test_pane_groups.py applies to
    GROUP_STRIP_COMPACT_COLS / GROUP_STRIP_MIN_COLS one level down."""
    label_floor = (
        labels_mod.TAB_MODEL_MIN + len(" · ") + labels_mod.TAB_REPO_MIN
    )
    assert label_floor == 13
    # 1 left pad + 2 collection indent + 2 mark and its space + 1 right pad.
    assert layout.SIDEBAR_CHROME == 6
    assert layout.SIDEBAR_MIN_WIDTH == layout.SIDEBAR_CHROME + label_floor == 19
    # The default: chrome plus HALF the cap the tab strip's own ellipsize
    # writes labels at.
    assert layout.SIDEBAR_WIDTH == (
        layout.SIDEBAR_CHROME + labels_mod.TAB_LABEL_MAX // 2
    ) == 22
    # The ceiling: the width at which the whole capped label fits.
    assert layout.SIDEBAR_MAX_WIDTH == (
        layout.SIDEBAR_CHROME + labels_mod.TAB_LABEL_MAX
    ) == 38
    # The absolute floor on TOTAL width: the narrowest rail plus the
    # narrowest pane DOXA will create.
    assert layout.SIDEBAR_MIN_COLS == (
        layout.SIDEBAR_MIN_WIDTH + layout.MIN_LEAF_WIDTH
    ) == 53
    # The cross-check against reality: on the 100-column reference
    # terminal with one vertical split, the rail must not push either
    # group onto the compact tab-strip rung.
    assert layout.SIDEBAR_WIDTH <= 100 - 2 * layout.GROUP_STRIP_COMPACT_COLS


def test_the_rail_refuses_to_open_below_the_measured_total_width():
    """Below SIDEBAR_MIN_COLS it refuses rather than squeezing the tree
    under its own minimum -- split_refusal's posture, applied to the one
    piece of chrome that is not in the tree."""
    rail = layout.SIDEBAR_MIN_WIDTH
    assert layout.sidebar_refusal(layout.SIDEBAR_MIN_COLS, 0, rail) is None
    note = layout.sidebar_refusal(layout.SIDEBAR_MIN_COLS - 1, 0, rail)
    assert note is not None
    # A refusal a user can ACT on: it names both floors and the width.
    assert str(layout.MIN_LEAF_WIDTH) in note
    assert str(layout.SIDEBAR_MIN_COLS - 1) in note


def test_the_rail_refuses_when_it_would_squeeze_the_narrowest_group():
    """The measurement is against the real painted rectangles, not against
    a constant: three groups on a 120-column terminal are 40 each, and a
    22-column rail would leave the narrowest at 32 -- below
    MIN_LEAF_WIDTH."""
    assert layout.sidebar_refusal(120, 40, 22) is not None
    # ...and with two groups on the same terminal (60 each) it fits.
    assert layout.sidebar_refusal(120, 60, 22) is None
    # Nothing painted degrades to the single-group case, which is the
    # SIDEBAR_MIN_COLS floor reached the other way.
    assert layout.sidebar_refusal(56, 0, 22) is None
    assert layout.sidebar_refusal(55, 0, 22) is not None


def test_a_width_is_clamped_rather_than_rejected():
    assert layout.clamp_sidebar_width(1) == layout.SIDEBAR_MIN_WIDTH
    assert layout.clamp_sidebar_width(999) == layout.SIDEBAR_MAX_WIDTH
    assert layout.clamp_sidebar_width("nonsense") == layout.SIDEBAR_WIDTH
    assert layout.clamp_sidebar_width(24) == 24


# -- the record: absence of the key is the migration -------------------


def _record(tmp_path, scope, **kwargs):
    tabsets_mod.save(scope, **kwargs)
    return json.loads(tabsets_mod._file_for(scope).read_text())


def test_absence_of_the_collections_key_is_the_migration(tmp_path):
    """A record written before v1.0.0 -- no ``collections`` key at all --
    reads as no collections, with no version field and no upgrade step.
    And a window with no collections writes no key, so this version's own
    records stay byte-comparable with the ones already on disk."""
    scope = str(tmp_path)
    tabs = [tabsets_mod.TabRecord("abc"), tabsets_mod.TabRecord("def")]
    raw = _record(tmp_path, scope, tabs=tabs, active_session_id="abc")
    assert "collections" not in raw
    # ...and the older keys are untouched: the flat list stays
    # authoritative and layout.kind stays "tabs", so an older DOXA sees a
    # record it fully understands.
    assert [r["session_id"] for r in raw["tabs"]] == ["abc", "def"]
    assert raw["layout"]["kind"] == "tabs"
    assert tabsets_mod.load(scope).collections == ()


def test_collections_round_trip_beside_tabs_and_layout(tmp_path):
    scope = str(tmp_path)
    items = (
        collections_mod.Collection("ampiric", ("abc", "def"), collapsed=True),
    )
    raw = _record(
        tmp_path, scope,
        tabs=[tabsets_mod.TabRecord("abc"), tabsets_mod.TabRecord("def")],
        active_session_id="abc", collections=items,
    )
    # A TOP-LEVEL key, beside tabs and layout -- not inside the layout
    # node, because a collection is not geometry.
    assert raw["collections"] == [
        {"name": "ampiric", "sessions": ["abc", "def"], "collapsed": True}
    ]
    assert "collections" not in raw["layout"]
    assert raw["layout"]["kind"] == "tabs"
    assert tabsets_mod.load(scope).collections == items


def test_a_member_not_in_tabs_is_dropped_on_load(tmp_path):
    """The rule the spec states outright, and it holds against a
    hand-edited file as well as against one this version wrote."""
    scope = str(tmp_path)
    tabsets_mod.save(scope, [tabsets_mod.TabRecord("abc")], "abc")
    path = tabsets_mod._file_for(scope)
    data = json.loads(path.read_text())
    data["collections"] = [
        {"name": "ampiric", "sessions": ["abc", "ghost"]},
        {"name": "all-dead", "sessions": ["ghost2"]},
    ]
    path.write_text(json.dumps(data))
    loaded = tabsets_mod.load(scope)
    assert [c.name for c in loaded.collections] == ["ampiric"]
    assert loaded.collections[0].sessions == ("abc",)


def test_an_empty_collection_is_not_written(tmp_path):
    scope = str(tmp_path)
    raw = _record(
        tmp_path, scope, tabs=[tabsets_mod.TabRecord("abc")],
        active_session_id="abc",
        collections=(collections_mod.Collection("nobody"),),
    )
    assert "collections" not in raw


# -- what the rail SHOWS (pure) ----------------------------------------


def test_a_collapsed_collection_hides_its_members():
    items = (collections_mod.Collection("ampiric", ("a", "b"), collapsed=True),)
    rows = build_rows(items, ["a", "b"], _describe({
        "a": ("alpha", (), True), "b": ("beta", (), True),
    }))
    assert [r.kind for r in rows] == [Row.HEADING]
    assert rows[0].collapsed is True


def test_the_ungrouped_heading_is_last_and_is_not_a_collection():
    """It is derived at render time and never persisted -- the moment it
    were, it would be a collection with a reserved name."""
    items = (collections_mod.Collection("ampiric", ("a",)),)
    rows = build_rows(items, ["a", "loose"], _describe({
        "a": ("alpha", (), True), "loose": ("stray", (), True),
    }))
    assert [(r.kind, r.text) for r in rows] == [
        (Row.HEADING, "ampiric"),
        (Row.SESSION, "alpha"),
        (Row.HEADING, LOOSE_HEADING),
        (Row.SESSION, "stray"),
    ]
    # The implicit heading names no collection, so nothing can rename or
    # delete it and nothing writes it down.
    assert rows[2].collection == ""
    assert collections_mod.to_json(items) == [
        {"name": "ampiric", "sessions": ["a"]}
    ]


def test_with_no_collections_the_rail_is_a_flat_list_with_no_heading():
    rows = build_rows((), ["a", "b"], _describe({
        "a": ("alpha", (), True), "b": ("beta", (), True),
    }))
    assert [r.kind for r in rows] == [Row.SESSION, Row.SESSION]


def test_the_rail_can_show_a_session_that_is_not_mounted_in_any_group():
    """**The design check docs/plans/session-sidebar.md owes itself.**

    If the rail could only list what the layout tree already contains it
    would be a second tab strip rather than a session index, and the
    design would be wrong. It can: a collection member whose tab was
    closed still gets a row, marked as not open."""
    items = (collections_mod.Collection("ampiric", ("live", "closed")),)
    rows = build_rows(items, ["live", "closed"], _describe({
        "live": ("alpha", (), True),
        "closed": ("beta", (), False),
    }))
    sessions = [r for r in rows if r.kind == Row.SESSION]
    assert [r.session_id for r in sessions] == ["live", "closed"]
    assert sessions[0].mounted is True
    assert sessions[1].mounted is False
    # ...and the row SAYS so rather than pretending it can be focused.
    line = SidebarLine(sessions[1])
    assert NOT_OPEN in line._text()


def test_a_member_this_window_never_heard_of_is_dropped_from_the_rail():
    items = (collections_mod.Collection("ampiric", ("a", "ghost")),)
    rows = build_rows(items, ["a"], _describe({"a": ("alpha", (), True)}))
    assert [r.session_id for r in rows if r.kind == Row.SESSION] == ["a"]


def test_a_row_glyph_and_the_tab_colour_agree_about_precedence():
    """The rail spends a column on a GLYPH the tab strip cannot afford,
    and the winner is TAB_STATE_MARKS' own order -- the one written-down
    statement doxa/theme.tcss cascades in too. Not a second derivation."""
    assert labels_mod.TAB_STATE_MARKS == (
        "-done-unseen", "-staged", "-working", "-attention",
    )
    assert labels_mod.top_mark(["-done-unseen", "-working"]) == "-working"
    assert labels_mod.top_mark(["-attention", "-working"]) == "-attention"
    assert labels_mod.sidebar_mark_glyph(()) == labels_mod.SIDEBAR_MARK_NONE
    assert labels_mod.sidebar_mark_glyph(["-attention"]) == "!"


# -- the boundary, against a real Pilot --------------------------------


@pytest.mark.asyncio
async def test_the_rail_is_a_SIBLING_of_the_window_root_not_a_node_in_it(tmp_path):
    """**The one decision the whole design rests on.**

    ``_window_root()`` needs no change and no isinstance special case
    because the rail is not a ``SplitBox``; splits, growth, directional
    focus and ``_pane_regions`` operate on the tree and never see it."""
    app, _engines = _app(tmp_path)
    async with app.run_test(size=BIG) as pilot:
        await pilot.pause()
        rail = app.sidebar()
        root = app._window_root()
        assert rail is not None and root is not None
        # Siblings, under one Horizontal that exists from compose.
        assert rail.parent is root.parent
        assert getattr(rail.parent, "id", "") == "window-row"
        # The rail is NOT a box, so the accessor that walks boxes is
        # blind to it, and so is every walk built on that accessor.
        assert not isinstance(rail, SplitBox)
        assert rail not in list(app.query(SplitBox))
        tree = __import__(
            "doxa.ui.split", fromlist=["tree_of"]
        ).tree_of(root)
        assert tree is not None
        assert all(
            leaf.session_id for leaf in layout.leaves(tree)
        )
        # ...and it is not a destination for directional focus either.
        assert rail.id not in app._pane_regions()


@pytest.mark.asyncio
async def test_a_split_never_sees_the_rail(tmp_path):
    """Opening the rail and then splitting produces two groups and leaves
    the rail exactly where it was -- painted, beside them."""
    app, _engines = _app(tmp_path)
    async with app.run_test(size=BIG) as pilot:
        await pilot.pause()
        assert app.set_sidebar(True) is None
        assert await _wait(pilot, lambda: app.sidebar().region.width > 0)
        assert await app.split_active_pane(layout.ROW) is None
        assert await _wait(pilot, lambda: len(app.groups()) == 2)
        await pilot.pause()
        rail = app.sidebar()
        root = app._window_root()
        assert rail.parent is root.parent          # still siblings
        assert rail.region.width == layout.SIDEBAR_WIDTH
        assert len(app.query(PaneGroup)) == 2
        # PAINTED, not merely structural: both groups have a real
        # rectangle and neither is the rail's.
        regions = app._pane_regions()
        assert len(regions) == 2
        assert all(w > 0 and h > 0 for _x, _y, w, h in regions.values())
        assert rail.region.x + rail.region.width <= min(
            x for x, _y, _w, _h in regions.values()
        )


@pytest.mark.asyncio
async def test_ctrl_b_toggles_the_rail_and_nothing_else_claims_it(tmp_path):
    """Re-verified against the CURRENT binding set, which is the check the
    spec asks for by name -- it moved three times this release series."""
    keys = [b.key for b in DoxaApp.BINDINGS]
    assert keys.count("f3") == 1
    # Deliverable under BOTH keyboard encodings, unlike ctrl+<digit> and
    # alt+<letter>, which this project chose and had to walk back.
    from doxa import keyboard as keyboard_mod

    assert keyboard_mod.unreachable_under_legacy("f3") is False
    app, _engines = _app(tmp_path)
    async with app.run_test(size=BIG) as pilot:
        await pilot.pause()
        assert app.sidebar().styles.display == "none"
        await pilot.press("f3")
        assert await _wait(pilot, lambda: app.sidebar().region.width > 0)
        await pilot.press("f3")
        assert await _wait(pilot, lambda: app.sidebar().region.width == 0)
        # The toggle WRITES, which is what ends hide-at-zero's guessing.
        assert config_mod.sidebar_mode() == config_mod.SIDEBAR_OFF


@pytest.mark.asyncio
async def test_hide_at_zero_one_session_and_no_collections(tmp_path):
    """With nothing to say the rail defaults hidden -- and it appears by
    itself the moment there IS something, without the user asking."""
    app, _engines = _app(tmp_path)
    async with app.run_test(size=BIG) as pilot:
        await pilot.pause()
        assert config_mod.sidebar_mode() == config_mod.SIDEBAR_AUTO
        assert app.sidebar_should_show() is False
        await app.action_new_tab()
        assert await _wait(pilot, lambda: len(app.panes()) == 2)
        await pilot.pause()
        assert app.sidebar_should_show() is True
        assert await _wait(pilot, lambda: app.sidebar().region.width > 0)


@pytest.mark.asyncio
async def test_the_rail_refuses_to_open_on_a_narrow_window(tmp_path):
    """A refusal with a reason, never a squeezed pane -- and it does NOT
    write the choice, because the terminal being narrow is not a decision
    about the rail."""
    app, _engines = _app(tmp_path)
    async with app.run_test(size=(layout.SIDEBAR_MIN_COLS - 5, 24)) as pilot:
        await pilot.pause()
        note = app.set_sidebar(True)
        assert note is not None and "not enough width" in note
        await pilot.pause()
        assert app.sidebar().region.width == 0
        assert config_mod.sidebar_mode() == config_mod.SIDEBAR_AUTO


@pytest.mark.asyncio
async def test_a_row_carries_the_marks_the_tab_header_carries(tmp_path):
    """ONE source, read twice. The rail reads the same ``_marks`` the tab
    header ORs, through the same ``labels.mark_over`` -- so the two
    surfaces cannot disagree."""
    app, _engines = _app(tmp_path)
    async with app.run_test(size=BIG) as pilot:
        await pilot.pause()
        await app.action_new_tab()
        assert await _wait(pilot, lambda: len(app.panes()) == 2)
        assert app.set_sidebar(True) is None
        assert await _wait(pilot, lambda: _settled(app, 2))
        pane = app.panes()[0]
        pane._set_tab_class("-done-unseen", True)
        assert await _wait(
            pilot,
            lambda: any(
                line.has_class("-done-unseen") for line in _lines(app)
            ),
        )
        line = next(
            line for line in _lines(app)
            if line.row.session_id == pane._session_id
        )
        # The header says the same thing, derived the same way.
        assert labels_mod.mark_over(pane.tab.leaves(), "-done-unseen") is True
        assert line.has_class("-done-unseen")
        assert labels_mod.SIDEBAR_MARK_GLYPHS["-done-unseen"] in line._text()
        pane._set_tab_class("-done-unseen", False)
        assert await _wait(pilot, lambda: not line.has_class("-done-unseen"))


@pytest.mark.asyncio
async def test_clicking_a_row_reveals_that_session(tmp_path):
    """Focus its group, activate its tab -- the three beats every explicit
    switch in doxa.app takes."""
    app, _engines = _app(tmp_path)
    async with app.run_test(size=BIG) as pilot:
        await pilot.pause()
        assert await app.split_active_pane(layout.ROW) is None
        assert await _wait(pilot, lambda: len(app.groups()) == 2)
        assert app.set_sidebar(True) is None
        assert await _wait(pilot, lambda: _settled(app, 2))
        first, other = app.panes()[0], app.panes()[1]
        # PUT the keyboard in the other group rather than assuming the
        # split left it there. tests/test_pane_groups.py establishes the
        # same precondition the same way, and for the reason this test
        # learned the hard way: focus after a split is applied through
        # ``call_after_refresh`` (doxa/app.py's own note on
        # ``screen.set_focus``), so "which pane has it" is not a fact that
        # holds at any particular instant -- and this test is about what a
        # CLICK does, not about where a split leaves the keyboard, which
        # tests/test_split_panes.py owns.
        app._focus_tab(other.tab)
        assert await _wait(pilot, lambda: app.focused_pane() is other)
        line = next(
            line for line in _lines(app)
            if line.row.session_id == first._session_id
        )
        await pilot.click(line)
        assert await _wait(pilot, lambda: app.focused_pane() is first)
        assert app.focused_group() is app.group_of(first)


@pytest.mark.asyncio
async def test_clicking_a_heading_folds_its_collection(tmp_path):
    app, _engines = _app(tmp_path)
    async with app.run_test(size=BIG) as pilot:
        await pilot.pause()
        await app.action_new_tab()
        assert await _wait(pilot, lambda: len(app.panes()) == 2)
        assert app.collection_new("ampiric") is None
        assert app.collection_assign("ampiric", app.panes()[0]._session_id) is None
        assert app.set_sidebar(True) is None
        assert await _wait(pilot, lambda: _settled(app, 4))
        heading = next(
            line for line in _lines(app)
            if line.row.collection == "ampiric" and line.row.kind == Row.HEADING
        )
        await pilot.click(heading)
        assert await _wait(pilot, lambda: app.collections()[0].collapsed)
        # ...and its member is off the rail while it is folded -- off the
        # PAINTED rail, which is what _settled's surplus check pins: a
        # line the rail stopped showing must lose its rectangle, or a
        # click could still land on a row that is not there.
        assert await _wait(pilot, lambda: _settled(app, 3))
        assert app.panes()[0]._session_id not in [
            line.row.session_id for line in _lines(app)
        ]


@pytest.mark.asyncio
async def test_a_detached_session_keeps_a_row_and_says_it_is_closed(tmp_path):
    """The design check, end to end against a running window: a session
    whose tab is gone is still a session this window knows about, so the
    rail keeps a row for it -- and selecting it says so rather than
    pretending it can be focused."""
    app, _engines = _app(tmp_path)
    async with app.run_test(size=BIG) as pilot:
        await pilot.pause()
        await app.action_new_tab()
        assert await _wait(pilot, lambda: len(app.panes()) == 2)
        await pilot.pause()
        doomed = app.panes()[1]
        session_id = doomed._session_id
        assert app.collection_new("ampiric") is None
        assert app.collection_assign("ampiric", session_id) is None
        await app._close_pane(doomed, terminate=False)
        assert await _wait(pilot, lambda: len(app.panes()) == 1)
        assert session_id in app._detached_this_run
        assert app.set_sidebar(True) is None
        assert await _wait(pilot, lambda: _settled(app))

        def closed_row():
            return next(
                (
                    line for line in _lines(app)
                    if line.row.session_id == session_id and not line.row.mounted
                ),
                None,
            )

        # Polled on the PAINTED state, not on the mount: the rail's model
        # moves ahead of what a widget scan alone would answer with.
        assert await _wait(pilot, lambda: closed_row() is not None)
        line = closed_row()
        assert NOT_OPEN in line._text()
        note = app.reveal_session(session_id)
        assert note is not None and "not open in this window" in note


@pytest.mark.asyncio
async def test_collections_survive_a_restart(tmp_path):
    """The record's own round trip, through the app that writes it."""
    from doxa import peers as peers_mod

    app, _engines = _app(tmp_path)
    async with app.run_test(size=BIG) as pilot:
        await pilot.pause()
        await app.action_new_tab()
        assert await _wait(pilot, lambda: len(app.panes()) == 2)
        await pilot.pause()
        ids = [pane._session_id for pane in app.panes()]
        assert app.collection_new("ampiric") is None
        assert app.collection_assign("ampiric", ids[0]) is None
        app._persist_tabset()
    scope = peers_mod.main_repo_root_of(str(tmp_path)) or str(tmp_path)
    record = tabsets_mod.load(scope)
    assert record is not None
    assert [c.name for c in record.collections] == ["ampiric"]
    assert record.collections[0].sessions == (ids[0],)
    # ...and a window handed them back starts with them.
    app2, _engines2 = _app(tmp_path, restore_collections=record.collections)
    async with app2.run_test(size=BIG) as pilot:
        await pilot.pause()
        assert [c.name for c in app2.collections()] == ["ampiric"]


@pytest.mark.asyncio
async def test_the_slash_commands_are_registered_and_reachable(tmp_path):
    """The registry describes and the pane executes -- and /sidebar names
    its key, so /help and the startup notice can see it."""
    from doxa import commands as commands_mod

    sidebar = commands_mod.find("/sidebar")
    assert sidebar is not None and sidebar.binding == "f3"
    assert sidebar.group == "Panes & tabs"
    collection = commands_mod.find("/collection")
    assert collection is not None and collection.usage.startswith("/collection ")
    app, _engines = _app(tmp_path)
    async with app.run_test(size=BIG) as pilot:
        await pilot.pause()
        pane = app.panes()[0]
        handlers = pane._command_handlers()
        assert "/sidebar" in handlers and "/collection" in handlers
        await handlers["/collection"]("new ampiric")
        assert [c.name for c in app.collections()] == ["ampiric"]
        await handlers["/collection"]("add ampiric")
        assert app.collections()[0].sessions == (pane._session_id,)
        await handlers["/sidebar"]("on")
        assert await _wait(pilot, lambda: app.sidebar().region.width > 0)
        await handlers["/sidebar"]("off")
        assert await _wait(pilot, lambda: app.sidebar().region.width == 0)


# -- what the rail costs the event loop (v1.0.0) -----------------------
#
# Textual's Stylesheet.apply and Screen._refresh_layout are SYNCHRONOUS,
# and on this codebase they are where the loop actually blocks: measured
# over tests/test_split_panes.py, max 305 ms and 334 ms in one module,
# against the 250 ms STALL_LIMIT that file asserts on. Chrome that adds
# widgets to the screen and rewrites their classes is therefore not paid
# for in microseconds, it is paid for out of that budget -- so the three
# facts below are pinned as facts, not left to a benchmark nobody runs.


@pytest.mark.asyncio
async def test_a_hidden_rail_is_not_rebuilt_when_a_mark_moves(tmp_path):
    """The measured one. ``_set_tab_class`` pokes the rail on every status
    change -- ``-working`` on and off per turn, ``-done-unseen``,
    ``-staged``, ``-attention`` per blink -- and the rail is HIDDEN in the
    overwhelmingly common window, which is every test in this suite that
    is not about the sidebar. A hidden rail holds no rows, so
    ``apply_marks`` could never find one and the "structure moved" fallback
    fired every single time, running the whole derivation
    (``_sidebar_order`` is ``query(TabPane)``, a full walk of the widget
    tree) for a rail nobody can see.

    A mark moving is not a structure change. ``_persist_tabset``,
    ``on_show`` and the collection edits are what re-derive."""
    app, _engines = _app(tmp_path)
    async with app.run_test(size=BIG) as pilot:
        await pilot.pause()
        rail = app.sidebar()
        assert rail is not None and rail.styles.display == "none"
        pane = app.panes()[0]

        walks: list[int] = []
        original = app._sidebar_order

        def counted():
            walks.append(1)
            return original()

        app._sidebar_order = counted
        try:
            pane._set_tab_class("-working", True)
            pane._set_tab_class("-working", False)
            pane._set_tab_class("-done-unseen", True)
        finally:
            app._sidebar_order = original
        assert walks == [], (
            f"a hidden rail re-derived its whole model {len(walks)} times "
            f"for three mark toggles that changed no structure"
        )


@pytest.mark.asyncio
async def test_one_refresh_derives_the_session_list_once(tmp_path):
    """``refresh_sidebar`` used to walk the DOM for the session list twice
    -- once through ``sidebar_should_show``, once again through
    ``sidebar_rows`` -- and then twice MORE per row, because
    ``_describe_session`` called ``panes()`` and ``archived_tabs()`` for
    every session it described. On a window with N sessions that is
    2 + 2N full walks of a tree that contains every transcript block on
    screen, per repaint."""
    app, _engines = _app(tmp_path)
    async with app.run_test(size=BIG) as pilot:
        await pilot.pause()
        await app.action_new_tab()
        assert await _wait(pilot, lambda: len(app.panes()) == 2)
        assert app.set_sidebar(True) is None
        assert await _wait(pilot, lambda: _settled(app, 2))

        orders: list[int] = []
        panes_calls: list[int] = []
        original_order, original_panes = app._sidebar_order, app.panes

        def counted_order():
            orders.append(1)
            return original_order()

        def counted_panes():
            panes_calls.append(1)
            return original_panes()

        app._sidebar_order = counted_order
        app.panes = counted_panes
        try:
            app.refresh_sidebar(force=True)
        finally:
            app._sidebar_order = original_order
            app.panes = original_panes
        assert len(orders) == 1, f"derived the session list {len(orders)}x"
        assert len(panes_calls) == 1, (
            f"walked the widget tree for panes {len(panes_calls)}x on a "
            f"two-session window -- once per row rather than once"
        )


def test_a_line_told_the_row_it_already_shows_writes_nothing():
    """``refresh_sidebar(force=True)`` -- what ``on_show`` and every
    collection edit pass -- clears the rail's own "nothing changed" cache,
    so without this guard each forced refresh rewrote eight classes and
    the text on every visible line. Each ``set_class`` marks the node for
    a stylesheet re-apply."""
    row = Row(Row.SESSION, "ampiric", session_id="abc", marks=("-working",))
    line = SidebarLine(row)

    writes: list[str] = []
    original = line.set_class

    def counted(value, *names, **kwargs):
        writes.extend(names)
        return original(value, *names, **kwargs)

    line.set_class = counted
    line.set_row(Row(Row.SESSION, "ampiric", session_id="abc",
                     marks=("-working",)))
    assert writes == [], f"rewrote {len(writes)} classes for the same row"

    # ...and a row that really did change is still written in full.
    line.set_row(Row(Row.SESSION, "ampiric", session_id="abc",
                     marks=("-attention",)))
    assert "-attention" in writes and "-working" in writes
    assert line.has_class("-attention") and not line.has_class("-working")

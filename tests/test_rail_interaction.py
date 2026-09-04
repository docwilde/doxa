# SPDX-License-Identifier: AGPL-3.0-only
"""The rail as a SWITCHER (v1.5.0) -- docs/plans/rail-interaction.md.

Four things were reported live against v1.2.0's rail, and this file pins
the part of each that a later change could silently undo:

* **hover** -- a highlight, in CSS, with no focus and no rebuild. The rail
  refreshes on every mark change, so presentation state that cost a
  rebuild would fight the list moving under the pointer;
* **heading contrast** -- the text colour is COMPUTED from the
  background's WCAG relative luminance, not chosen, and resolved once per
  palette name. A hardcoded ``black`` on a mid-tone project colour is the
  same class of defect as a hardcoded hex one level up, which v1.2.0's
  colours-by-name rule already refuses;
* **option C** -- a rail entry is a pane GROUP and its rows are that
  group's TABS. Every group is on screen at once, so before this a click
  could only move focus; a group's inactive tab is the one genuinely
  hidden thing a window has, and switching to it is the reveal the rail
  has never been able to perform;
* **the divider** -- draggable AND keyboard-driven, refusing at the same
  floor opening the rail refuses at. A drag that outran that floor would
  produce an arrangement the app will not create interactively.

And the check the spec owes itself: **with the rail collapsed (F3), is
every action still reachable?** ``test_every_action_has_a_door_that_is_not
_the_rail`` is that answer, written down.
"""

from __future__ import annotations

import json
import re

import pytest

from doxa import config as config_mod
from doxa import layout
from doxa import tabsets as tabsets_mod
from doxa import triage as triage_mod
from doxa.app import DoxaApp
from doxa.ui import sidebar as sidebar_mod
from doxa.ui.sidebar import Row, SidebarLine, build_rows
from tests.fakes import FakeEngine


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "doxa-home"))
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    config_mod.invalidate()
    yield
    config_mod.invalidate()


#: Wide enough that the rail never hits its own width refusal.
BIG = (160, 48)


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


def _lines(app):
    rail = app.sidebar()
    return rail.lines() if rail is not None else []


def _facts(label, **kwargs):
    return triage_mod.Facts(label=label, mounted=True, **kwargs)


def _describe(facts):
    def describe(session_id):
        return facts.get(session_id, triage_mod.Facts())

    return describe


# -- option C: the heading is the group, the rows are its tabs ----------


def test_a_group_heading_is_drawn_for_every_group_and_the_tabs_sit_under_it():
    """The reversal, stated as a shape.

    v1.2.0 drew the members always and the group's own row only above two
    or more of them. C is the other way round: the group's row is its
    HEADING and is always there, and the rows under it are its tabs --
    which are the only genuinely hidden thing a window holds and therefore
    the only thing a click can reveal."""
    facts = {
        "a": _facts("alpha", repo_root="/repo"),
        "b": _facts("beta", repo_root="/repo"),
        "solo": _facts("solo", repo_root="/repo"),
    }
    rows = build_rows(
        (), ["a", "b", "solo"], _describe(facts),
        panes=[
            triage_mod.PaneEntry("g1", ("a", "b"), "a"),
            triage_mod.PaneEntry("g2", ("solo",), "solo"),
        ],
    )
    assert [(r.kind, r.text) for r in rows] == [
        (Row.ENTRY, "alpha"),      # the two-tab group's heading
        (Row.SESSION, "alpha"),    # ...and its two tabs
        (Row.SESSION, "beta"),
        (Row.ENTRY, "solo"),       # the one-tab group: heading, no child
    ]
    # A tab row knows which group it is a tab OF, so a click can switch
    # that group without the widget keeping an index beside the list.
    assert [r.entry_key for r in rows] == ["g1", "g1", "g1", "g2"]
    # ...and the tabs are one level in from their heading.
    assert [r.indent for r in rows] == [0, 1, 1, 0]


def test_a_single_tab_group_grows_no_child_row():
    """Hide at zero, the judgment every other piece of chrome in this app
    makes. One tab is the heading's own subject and a row repeating it is
    noise -- so the ordinary window is one line per group, exactly as it
    was in v1.0.0 and v1.2.0."""
    rows = build_rows(
        (), ["a"], _describe({"a": _facts("alpha")}),
        panes=[triage_mod.PaneEntry("g1", ("a",), "a")],
    )
    assert [(r.kind, r.text) for r in rows] == [(Row.ENTRY, "alpha")]
    assert rows[0].count == 1
    # No caret either: there is nothing under it to fold, and a caret that
    # promised otherwise would be chrome that lies.
    line = SidebarLine(rows[0])
    assert line.folds() is False
    assert sidebar_mod.FOLD_OPEN not in line._text()
    assert sidebar_mod.FOLD_SHUT not in line._text()
    # ...and it is not MUTED against members it does not have.
    assert not line.has_class("-entry")


def test_a_tab_row_carries_its_own_marks_and_never_the_groups_rollup():
    """Two rows under one heading claiming the same state would be a lie.

    The HEADING is the aggregate (most urgent over every member, the
    invisible ones included -- Part 1b, kept); each TAB row is only ever
    itself."""
    facts = {
        "calm": _facts("calm"),
        "waiting": _facts("waiting", marks=(triage_mod.NEEDS_INPUT_MARK,)),
    }
    rows = build_rows(
        (), ["calm", "waiting"], _describe(facts),
        panes=[triage_mod.PaneEntry("g1", ("calm", "waiting"), "calm")],
    )
    heading, calm, waiting = rows
    assert heading.kind == Row.ENTRY
    assert triage_mod.NEEDS_INPUT_MARK in heading.marks   # the roll-up
    assert calm.marks == ()                               # ...and not the tab's
    assert waiting.marks == (triage_mod.NEEDS_INPUT_MARK,)
    # The heading also says WHICH member it is reporting, so a user who
    # opens the group and finds a calm active tab is not told a lie.
    assert heading.hidden is True
    assert "·2/2" in SidebarLine(heading)._text()


def test_a_folded_group_keeps_its_heading_and_hides_its_tabs():
    facts = {"a": _facts("alpha"), "b": _facts("beta")}
    panes = [triage_mod.PaneEntry("g1", ("a", "b"), "a")]
    shown = build_rows((), ["a", "b"], _describe(facts), panes=panes)
    folded = build_rows(
        (), ["a", "b"], _describe(facts), panes=panes,
        collapsed_groups=("g1",),
    )
    assert [r.kind for r in shown] == [Row.ENTRY, Row.SESSION, Row.SESSION]
    assert [r.kind for r in folded] == [Row.ENTRY]
    assert shown[0].expanded is True and folded[0].expanded is False
    # The caret says which, in the direction every tree view in every
    # terminal uses, so it needs no legend.
    assert SidebarLine(shown[0])._text().startswith(sidebar_mod.FOLD_OPEN)
    assert SidebarLine(folded[0])._text().startswith(sidebar_mod.FOLD_SHUT)
    # A key naming a group this window does not have costs nothing.
    assert build_rows(
        (), ["a", "b"], _describe(facts), panes=panes,
        collapsed_groups=("a-group-that-closed",),
    ) == shown


def test_the_fold_hit_zone_is_the_carets_own_columns_at_that_indent():
    """Two gestures on one line, told apart the way every tree view tells
    them apart: the arrow folds, the label selects. The zone is DERIVED
    from the indent doxa/theme.tcss paints, so moving either moves both."""
    facts = {"a": _facts("alpha"), "b": _facts("beta")}
    panes = [triage_mod.PaneEntry("g1", ("a", "b"), "a")]
    # Under a heading the entry sits one level in; with nothing to sit
    # under it sits at the margin.
    flat = build_rows((), ["a", "b"], _describe(facts), panes=panes)[0]
    assert SidebarLine(flat).fold_zone() == sidebar_mod.FOLD_COLUMNS == 2
    indented = Row(
        Row.ENTRY, "alpha", entry_key="g1", count=2, indent=1,
    )
    assert SidebarLine(indented).fold_zone() == (
        sidebar_mod.INDENT_COLUMNS + sidebar_mod.FOLD_COLUMNS
    ) == 4
    # A row with no caret has no zone at all, so no click on it can ever
    # be read as a fold.
    assert SidebarLine(Row(Row.ENTRY, "solo", count=1)).fold_zone() == 0
    assert SidebarLine(Row(Row.SESSION, "alpha")).fold_zone() == 0


# -- heading contrast: COMPUTED, and cached by name --------------------


def test_the_heading_text_is_computed_from_luminance_not_chosen():
    """WCAG relative luminance, threshold-picked -- and the threshold is
    itself derived rather than guessed at 0.5."""
    assert triage_mod.contrast_text("#FFFFFF") == triage_mod.CONTRAST_DARK
    assert triage_mod.contrast_text("#000000") == triage_mod.CONTRAST_LIGHT
    # The pivot is where black and white contrast EQUALLY well, which is
    # nowhere near 0.5 because luminance is not lightness.
    pivot = triage_mod.CONTRAST_PIVOT
    assert 0.17 < pivot < 0.19
    assert round((pivot + 0.05) / 0.05, 6) == round(1.05 / (pivot + 0.05), 6)
    # ...and the answer is by construction the better of the two on every
    # colour in the palette, which is the property "computed" buys.
    for name, hex_value in triage_mod.PALETTE_HEX.items():
        luminance = triage_mod.relative_luminance(hex_value)
        on_black = (luminance + 0.05) / 0.05
        on_white = 1.05 / (luminance + 0.05)
        expected = (
            triage_mod.CONTRAST_DARK if on_black >= on_white
            else triage_mod.CONTRAST_LIGHT
        )
        assert triage_mod.contrast_text(hex_value) == expected, name
    # Garbage is a colour nobody can parse, not a crash.
    assert triage_mod.contrast_text("nonsense") == triage_mod.CONTRAST_LIGHT
    assert triage_mod.relative_luminance(None) == 0.0


def test_the_pair_is_resolved_once_per_colour_and_cached_by_name():
    """v1.2.0 measured re-deriving in the rail at +22% layout time, and a
    pow() per row per paint is that mistake in a new place."""
    triage_mod._HEADING_PAINT.clear()
    first = triage_mod.heading_paint("teal")
    second = triage_mod.heading_paint("teal")
    assert first is second
    assert set(triage_mod._HEADING_PAINT) == {"teal"}
    # Keyed by NAME, so a theme change re-resolves rather than stranding a
    # contrast computed against a colour nobody paints any more.
    assert first == (
        triage_mod.PALETTE_HEX["teal"],
        triage_mod.contrast_text(triage_mod.PALETTE_HEX["teal"]),
    )
    # No project -- a collection's heading, the ungrouped one -- is a
    # heading too, so it is highlighted and its text is computed the same
    # way. It is not "no background".
    assert triage_mod.heading_paint(triage_mod.NO_COLOUR)[0] == (
        triage_mod.HEADING_HEX
    )
    # The cache cannot grow past the palette plus that one.
    for name in triage_mod.PALETTE:
        triage_mod.heading_paint(name)
    assert len(triage_mod._HEADING_PAINT) <= len(triage_mod.PALETTE) + 1


def test_the_stylesheet_and_the_palette_agree_about_every_hex():
    """The hexes are written twice -- doxa.triage.PALETTE_HEX computes the
    contrast from them, doxa/theme.tcss paints a row's identity with them
    -- so the agreement is asserted rather than assumed. Moving a hue in
    one place fails here instead of stranding a contrast."""
    from pathlib import Path

    import doxa

    css = (Path(doxa.__file__).parent / "theme.tcss").read_text("utf-8")
    painted = dict(
        re.findall(
            r"SidebarLine\.-project-(\w+)\s*\{\s*color:\s*(#[0-9A-Fa-f]{6})\s*;",
            css,
        )
    )
    assert painted, "the project colour rules moved out of doxa/theme.tcss"
    assert {k: v.upper() for k, v in painted.items()} == {
        k: v.upper() for k, v in triage_mod.PALETTE_HEX.items()
    }
    assert set(painted) == set(triage_mod.PALETTE)


def test_a_heading_line_paints_the_computed_pair_and_a_tab_row_clears_it():
    """A line is REUSED in place, so a heading that becomes a tab row must
    lose the paint -- or it would carry a project background into a row
    whose colour means something else entirely."""
    line = SidebarLine(Row(Row.HEADING, "doxa", project="teal"))
    background, text = triage_mod.heading_paint("teal")
    assert line.styles.background.hex.upper() == background.upper()
    assert line.styles.color.hex.upper() == text.upper()
    line.set_row(Row(Row.SESSION, "alpha", session_id="a", project="teal"))
    assert line.styles.has_rule("background") is False
    assert line.styles.has_rule("color") is False
    # ...and the row's identity is still a CLASS, so the four status rules
    # keep winning over it exactly as doxa/theme.tcss's cascade says.
    assert line.has_class("-project-teal")


# -- hover: presentation only, and never a rebuild ---------------------


def test_hover_is_css_and_costs_the_rail_nothing():
    """The one thing that must stay true. The rail already refreshes on
    every mark change; hover state driven from Python would be a second
    writer to the same lines, and a list that rebuilt under the pointer is
    the risk docs/plans/rail-interaction.md names first."""
    from pathlib import Path

    import doxa

    css = (Path(doxa.__file__).parent / "theme.tcss").read_text("utf-8")
    assert "SidebarLine:hover" in css
    # A TINT and not a background: it composites over whatever the row
    # already has -- a status colour, a heading's computed pair -- rather
    # than replacing it, which is what keeps one rule from having to
    # restate all of them.
    hover = css.split("SidebarLine:hover", 1)[1].split("}", 1)[0]
    assert "background-tint" in hover
    assert "background:" not in hover
    # No Python on the hover path at all, and no focus: a focusable widget
    # beside the prompt is the v0.85.0 AUTO_FOCUS defect v1.0.0 declined
    # to re-open, and it stays declined.
    assert SidebarLine(Row(Row.SESSION, "a")).can_focus is False
    for hook in ("on_enter", "on_leave", "on_mouse_move"):
        assert hook not in SidebarLine.__dict__, hook


# -- the record: absence of the key is the migration -------------------


def test_absence_of_the_rail_folded_key_is_the_migration(tmp_path):
    tabsets_mod.save(
        "scope", [tabsets_mod.TabRecord(session_id="a")], "a",
    )
    record = tabsets_mod.load("scope")
    assert record is not None and record.rail_folded == ()
    path = tabsets_mod._file_for("scope")
    assert "rail_folded" not in json.loads(path.read_text("utf-8"))


def test_folds_round_trip_beside_tabs_and_collections(tmp_path):
    tabsets_mod.save(
        "scope", [tabsets_mod.TabRecord(session_id="a")], "a",
        rail_folded=("session-tabs-2", "session-tabs"),
    )
    payload = json.loads(tabsets_mod._file_for("scope").read_text("utf-8"))
    # Top level, beside ``collections`` and NOT inside the layout node: a
    # fold is about the rail, not about geometry.
    assert payload["rail_folded"] == ["session-tabs", "session-tabs-2"]
    assert payload["layout"]["kind"] == "tabs"
    assert tabsets_mod.load("scope").rail_folded == (
        "session-tabs", "session-tabs-2",
    )
    # A hand-edited record costs a fold, never a session.
    path = tabsets_mod._file_for("scope")
    payload["rail_folded"] = ["ok", 7, "", None]
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert tabsets_mod.load("scope").rail_folded == ("ok",)


# -- the divider -------------------------------------------------------


def test_the_divider_keys_are_free_and_deliverable_under_both_encodings():
    """A modified ARROW, for the reason Alt+arrow survived v0.95.0's cull
    of alt+<letter>: CSI 1;4<final> is a different physical encoding from
    a modified letter, and Textual's parser decodes it either way.
    Measured, not assumed."""
    from textual._xterm_parser import XTermParser

    from doxa import keyboard as keyboard_mod

    for sequence, key in (("\x1b[1;4D", "alt+shift+left"),
                          ("\x1b[1;4C", "alt+shift+right")):
        assert [e.key for e in XTermParser().feed(sequence)] == [key]
        assert keyboard_mod.unreachable_under_legacy(key) is False
    app = DoxaApp(cwd=".")
    resolved = dict(app._bindings.key_to_bindings)
    assert [b.action for b in resolved["alt+shift+left"]] == ["sidebar_narrower"]
    assert [b.action for b in resolved["alt+shift+right"]] == ["sidebar_wider"]
    # priority, for the reason every global here needs it: the prompt is a
    # focused TextArea and would otherwise eat the key first.
    assert all(
        b.priority for key in ("alt+shift+left", "alt+shift+right")
        for b in resolved[key]
    )
    # ...and nothing else claims them.
    for key in ("alt+shift+left", "alt+shift+right"):
        assert len(resolved[key]) == 1


@pytest.mark.asyncio
async def test_a_drag_refuses_at_the_same_floor_opening_refuses_at(tmp_path):
    """The risk docs/plans/rail-interaction.md names third. A drag that
    outran the floor would produce an arrangement F3 will not create, and
    that arrangement would be the one thing neither a restart nor the
    settings modal could reproduce."""
    app, _engines = _app(tmp_path)
    async with app.run_test(size=BIG) as pilot:
        await pilot.pause()
        assert app.set_sidebar(True) is None
        await pilot.pause()
        # Both ends clamp rather than raise -- chrome never costs a
        # session -- and both refusals come from the ONE function.
        assert app.resize_sidebar(layout.SIDEBAR_MIN_WIDTH) is None
        assert app.sidebar_width() == layout.SIDEBAR_MIN_WIDTH
        assert app.resize_sidebar(1) is None
        assert app.sidebar_width() == layout.SIDEBAR_MIN_WIDTH
        assert app.resize_sidebar(9_999) is None
        assert app.sidebar_width() == layout.SIDEBAR_MAX_WIDTH
        # It is written, so the next launch opens where the user left it.
        assert config_mod.sidebar_width() == layout.SIDEBAR_MAX_WIDTH


@pytest.mark.asyncio
async def test_a_drag_that_would_squeeze_a_pane_is_refused_not_clamped(tmp_path):
    """...and the refusal is the same sentence, because it is the same
    function: resize asks sidebar_refusal, exactly as opening does."""
    app, _engines = _app(tmp_path)
    # Narrow enough that the rail fits at its floor and nowhere near its
    # ceiling -- which is the window this refusal exists for.
    config_mod.save({"sidebar_width": str(layout.SIDEBAR_MIN_WIDTH)})
    config_mod.invalidate()
    async with app.run_test(size=(layout.SIDEBAR_MIN_COLS + 2, 24)) as pilot:
        await pilot.pause()
        assert app.set_sidebar(True) is None
        await pilot.pause()
        note = app.resize_sidebar(layout.SIDEBAR_MAX_WIDTH)
        assert note is not None and "sidebar" in note
        # ...and it did not move, which is what "refuse" means.
        assert app.sidebar_width() < layout.SIDEBAR_MAX_WIDTH


@pytest.mark.asyncio
async def test_the_edge_really_drags_and_only_the_last_move_is_written(
    tmp_path,
):
    """The mouse half, driven through the real events.

    Only the mouse-UP is persisted: a config write per mouse-move event
    would be a file rewrite per cell the pointer crossed, and the rail has
    to track the pointer either way."""
    app, _engines = _app(tmp_path)
    async with app.run_test(size=BIG) as pilot:
        await pilot.pause()
        assert app.set_sidebar(True) is None
        await pilot.pause()
        rail = app.sidebar()
        start = app.sidebar_width()
        edge = rail.outer_size.width - 1
        # A click INSIDE the rail is a row gesture, not a drag.
        assert rail._edge_grabbed(0) is False
        assert rail._edge_grabbed(edge) is True

        await pilot.mouse_down(rail, offset=(edge, 1))
        await pilot.pause()
        assert rail._dragging is True
        await pilot.hover(app.screen, offset=(rail.region.x + start + 3, 1))
        assert await _wait(pilot, lambda: app.sidebar_width() == start + 4)
        # ...tracked, but not yet written down.
        assert config_mod.sidebar_width() == start
        await pilot.mouse_up(app.screen, offset=(rail.region.x + start + 3, 1))
        assert await _wait(pilot, lambda: rail._dragging is False)
        assert await _wait(pilot, lambda: config_mod.sidebar_width() == start + 4)
        # ...and the override is spent, so the registry is the answer again.
        assert app._sidebar_width_override is None


@pytest.mark.asyncio
async def test_the_rail_reports_a_width_it_can_still_open_at(tmp_path):
    """``sidebar_refusal`` is asked from the SHOWN state now, and the
    narrowest painted group is already short by the rail's own columns
    there. Feeding that back in would price the rail twice and refuse a
    width that fits -- which, on a resize, would close a rail the user
    never asked to close."""
    app, _engines = _app(tmp_path)
    async with app.run_test(size=BIG) as pilot:
        await pilot.pause()
        assert app.set_sidebar(True) is None
        await pilot.pause()
        assert app.sidebar_refusal() is None
        # The rail is open, so this is the double-count case; it must
        # still answer the same as it does with the rail hidden.
        assert app.sidebar().styles.display != "none"
        assert app.sidebar_refusal(layout.SIDEBAR_WIDTH) is None


# -- what a click DOES, against a real Pilot ---------------------------


@pytest.mark.asyncio
async def test_clicking_a_tab_row_switches_that_groups_active_tab(tmp_path):
    """**The reveal the rail has never been able to perform.** Every group
    is on screen at once, so the only hidden thing a window holds is a
    group's inactive tab -- and this is the gesture that reaches it."""
    app, _engines = _app(tmp_path)
    async with app.run_test(size=BIG) as pilot:
        await pilot.pause()
        await app.action_new_tab()
        assert await _wait(pilot, lambda: len(app.panes()) == 2)
        assert len(app.groups()) == 1
        assert app.set_sidebar(True) is None
        # One heading and two tab rows.
        assert await _wait(pilot, lambda: len(_lines(app)) == 3)
        first, second = app.panes()[0], app.panes()[1]
        group = app.groups()[0]
        assert group.active_tab() is second.tab
        row = next(
            line for line in _lines(app)
            if line.row.kind == Row.SESSION
            and line.row.session_id == first._session_id
        )
        await pilot.click(row)
        # The group SWITCHED -- not merely focused.
        assert await _wait(pilot, lambda: group.active_tab() is first.tab)
        assert await _wait(pilot, lambda: app.focused_pane() is first)


@pytest.mark.asyncio
async def test_clicking_a_group_heading_focuses_it_and_leaves_the_tab_alone(
    tmp_path,
):
    """A heading summarises every tab its group holds. A click on it that
    also switched the active tab would be the rail changing what you are
    looking at as a side effect of asking about it -- and there would then
    be no gesture left that means "go there and leave it alone"."""
    app, _engines = _app(tmp_path)
    async with app.run_test(size=BIG) as pilot:
        await pilot.pause()
        await app.action_new_tab()
        assert await _wait(pilot, lambda: len(app.panes()) == 2)
        assert await app.split_active_pane(layout.ROW) is None
        assert await _wait(pilot, lambda: len(app.groups()) == 2)
        assert app.set_sidebar(True) is None
        await pilot.pause()
        crowded = next(g for g in app.groups() if len(g.tabs()) > 1)
        other = next(g for g in app.groups() if g is not crowded)
        was_active = crowded.active_tab()
        app._focus_tab(next(iter(other.surfaces())))
        assert await _wait(pilot, lambda: app.focused_group() is other)
        heading = next(
            line for line in _lines(app)
            if line.row.kind == Row.ENTRY
            and line.row.entry_key == crowded.entry_key
        )
        # Past the caret's own columns, which is the FOLD gesture.
        await pilot.click(heading, offset=(heading.fold_zone() + 2, 0))
        assert await _wait(pilot, lambda: app.focused_group() is crowded)
        assert crowded.active_tab() is was_active


@pytest.mark.asyncio
async def test_the_caret_folds_the_group_and_the_fold_survives_a_restart(
    tmp_path,
):
    """Expansion is per group and persists, beside the collapsed flag a
    collection already has: a fold is how the user wants to read this
    window, and a window that forgot it on every restart would be asking
    them to say it again."""
    app, _engines = _app(tmp_path)
    async with app.run_test(size=BIG) as pilot:
        await pilot.pause()
        await app.action_new_tab()
        assert await _wait(pilot, lambda: len(app.panes()) == 2)
        assert app.set_sidebar(True) is None
        assert await _wait(pilot, lambda: len(_lines(app)) == 3)
        heading = next(
            line for line in _lines(app) if line.row.kind == Row.ENTRY
        )
        key = heading.row.entry_key
        assert heading.folds() is True
        await pilot.click(heading, offset=(0, 0))
        # The tab rows are gone; the heading is not.
        assert await _wait(pilot, lambda: len(_lines(app)) == 1)
        assert app._rail_folded == {key}
        assert _lines(app)[0].row.kind == Row.ENTRY
        assert _lines(app)[0].row.expanded is False
        # ...and it is on disk, at the top level, keyed by the group.
        from doxa import peers as peers_mod

        scope = peers_mod.main_repo_root_of(app.cwd) or app.cwd

        def written() -> bool:
            record = tabsets_mod.load(scope)
            return record is not None and key in record.rail_folded

        assert await _wait(pilot, written)
    # A fresh app told what the record holds opens with the same fold.
    fresh, _e = _app(tmp_path, restore_rail_folded=(key,))
    assert fresh._rail_folded == {key}


# -- the check the spec owes itself ------------------------------------


@pytest.mark.asyncio
async def test_every_action_has_a_door_that_is_not_the_rail(tmp_path):
    """**With the rail collapsed (F3), is every one of these still
    reachable?** The rail defaults hidden on a narrow window, so an action
    that existed only on it would be a capability a user lost by closing
    it -- which is exactly what ``/pane``, ``/split`` and ``Ctrl+←/→``
    exist to prevent.

    * switch a group's active tab -> ``Ctrl+←/→`` (``_cycle_tab``);
    * focus a group without switching -> ``Ctrl+1..9`` / ``/pane <n>``;
    * resize the rail -> ``Alt+Shift+←/→`` / ``/sidebar width <n>``;
    * fold a group -> rail chrome, not a capability: the tabs it hides
      are still reachable by every door above.
    """
    app, _engines = _app(tmp_path)
    async with app.run_test(size=BIG) as pilot:
        await pilot.pause()
        await app.action_new_tab()
        assert await _wait(pilot, lambda: len(app.panes()) == 2)
        assert await app.split_active_pane(layout.ROW) is None
        assert await _wait(pilot, lambda: len(app.groups()) == 2)
        # THE RAIL IS SHUT. Everything below is the door that does not
        # depend on it.
        assert app.set_sidebar(False) is None
        await pilot.pause()
        rail = app.sidebar()
        assert rail is not None and rail.styles.display == "none"

        # 1. Switching a group's active tab.
        crowded = next(g for g in app.groups() if len(g.tabs()) > 1)
        app._focus_tab(next(iter(crowded.surfaces())))
        assert await _wait(pilot, lambda: app.focused_group() is crowded)
        was = crowded.active_tab()
        app.action_next_tab()
        assert await _wait(pilot, lambda: crowded.active_tab() is not was)
        # ...and LET IT LAND before asking for something else. Textual 5.3
        # defers ``Widget.focus`` by a message-pump turn, so a switch's
        # focus arrives after the ``active`` assignment it followed --
        # which is exactly the race focused_group()'s own docstring
        # measures, and a test that raced it would be testing the pump.
        assert await _wait(pilot, lambda: app.focused_pane() in crowded.surfaces())

        # 2. Focusing a group. /pane <n> is the door for the terminals
        #    that cannot send Ctrl+<digit> at all.
        other = next(g for g in app.groups() if g is not crowded)
        number = app._group_order().index(other) + 1
        assert app.focus_group_number(number) is None
        assert await _wait(pilot, lambda: app.focused_group() is other)

        # 3. The divider. The keys report a hidden rail rather than moving
        #    a divider that is not on screen, and /sidebar width is the
        #    always-works door either way.
        assert app.resize_sidebar(layout.SIDEBAR_MIN_WIDTH + 1) is None
        assert config_mod.sidebar_width() == layout.SIDEBAR_MIN_WIDTH + 1
        assert hasattr(app, "action_sidebar_wider")
        assert hasattr(app, "action_sidebar_narrower")


def test_the_slash_door_for_the_divider_is_registered(tmp_path):
    """A command whose usage is not written here is a command /help
    cannot show -- the v0.92.0 defect."""
    from doxa import commands as commands_mod

    row = next(c for c in commands_mod.REGISTRY if c.name == "/sidebar")
    assert "width" in (row.usage or "")
    assert row.binding == "f3"
    from doxa.session import commands as session_commands

    assert any(
        b.name == "/sidebar" for b in session_commands.PANE_COMMANDS
    )

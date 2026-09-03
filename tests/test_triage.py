# SPDX-License-Identifier: AGPL-3.0-only
"""Collection triage (v1.2.0) -- glyphs, project colour, and what a rail
entry IS.

docs/plans/collection-triage.md names what is most likely to go wrong, and
this file pins exactly those things:

* **the check the spec owes itself** -- can a project AND every session's
  status be identified with colour stripped ENTIRELY?
  ``test_the_rail_reads_with_every_colour_stripped`` renders the whole
  rail as plain text and answers it. If that test ever needs a colour to
  pass, colour is doing work a glyph or a name should be doing;
* **unknown context is not "< 50%"** -- the one place this feature would
  start lying, and ``/context``'s ``?`` rule one level down;
* **font coverage** -- the two glyphs come from codepoint classes this
  codebase already ships, asserted against the shipped source rather than
  against a comment claiming it;
* **the same repo is the same colour everywhere** -- with nothing stored,
  which is a claim about the HASH and not about a cache;
* **collision is expected** -- and costs redundancy, never meaning;
* **an entry is a PANE** -- state aggregates most-urgent-wins over members
  the window is not showing, and the entry says so rather than letting the
  user open a calm tab and conclude the rail lied;
* **grey means exactly one thing** -- the absence of a project colour. Age
  is a separate channel and dims rather than recolours.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

from doxa import collections as collections_mod
from doxa import config as config_mod
from doxa import layout
from doxa import triage as triage_mod
from doxa.app import DoxaApp
from doxa.ui import labels as labels_mod
from doxa.ui.sidebar import LOOSE_HEADING, Row, SidebarLine, build_rows
from tests.fakes import FakeEngine


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "doxa-home"))
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    config_mod.invalidate()
    yield
    config_mod.invalidate()


def _facts(label, **kwargs):
    return triage_mod.Facts(label=label, mounted=True, **kwargs)


def _describe(table):
    return lambda session_id: table.get(session_id, triage_mod.Facts())


BIG = (160, 48)


async def _wait(pilot, cond, tries=250):
    for _ in range(tries):
        if cond():
            return True
        await pilot.pause(0.02)
    return cond()


# -- Part 0: the two states, and the honesty rule under them -----------


def test_the_context_threshold_is_a_named_constant_and_it_is_fifty():
    """``50%`` is the owner's number and it is a NAMED constant, not a
    literal: it is the first threshold anyone will want to tune, and a
    second one (75%? 90%?) must not have to go hunting for a bare 50 in
    the middle of a render method."""
    assert triage_mod.CTX_GLYPH_PCT == 50.0
    assert triage_mod.ctx_full(49.9) is False
    assert triage_mod.ctx_full(50.0) is True
    assert triage_mod.ctx_full(50.1) is True
    # And it really is the only statement of the number: nothing in the
    # rendering path re-types it.
    for name in ("doxa/ui/sidebar.py", "doxa/ui/labels.py"):
        source = pathlib.Path(name).read_text(encoding="utf-8")
        assert not re.search(r"^\s*\w*[Cc]tx\w*\s*>=\s*50", source, re.M)


def test_unknown_context_is_not_below_the_threshold_and_earns_no_glyph():
    """**The failure this feature would start with.** A session whose
    limit the CLI never reported gets NO ctx glyph -- not the
    absence-of-warning that reads as "plenty of room". ``/context``'s
    ``?`` rule, one level down.

    The load-bearing half is that ``None`` SURVIVES: it must still be
    ``None`` on the row, so that raising or lowering the threshold later
    can never accidentally rank an unmeasured session by it."""
    assert triage_mod.ctx_full(None) is False
    assert triage_mod.GLYPH_CTX not in triage_mod.entry_glyphs((), None)
    assert triage_mod.GLYPH_CTX not in triage_mod.entry_glyphs((), 10.0)
    assert triage_mod.GLYPH_CTX in triage_mod.entry_glyphs((), 90.0)
    # Unknown ranks as if the signal is absent -- NOT as 0%, which would
    # be a measurement the rail never made.
    assert triage_mod.urgency((), None) == 0
    rows = build_rows(
        (), ["u", "k"],
        _describe({
            "u": _facts("unknown", repo_root="/p"),
            "k": _facts("known", ctx_percentage=0.0, repo_root="/p"),
        }),
    )
    by_label = {row.text: row for row in rows if row.kind == Row.SESSION}
    assert by_label["unknown"].ctx_percentage is None
    assert by_label["known"].ctx_percentage == 0.0


def test_the_two_glyphs_come_from_codepoint_classes_that_already_ship():
    """Font coverage is a real risk and DOXA has been burned by it: the
    banner work rejected Geometric Shapes for tofu, and v0.81.0's
    draughts glyphs ship only behind ``context_grid = ascii``. So the two
    status glyphs come from the narrow set already PROVEN here -- and
    that is asserted against the shipped source, not against a docstring
    claiming it, because a comment cannot go stale loudly."""
    assert triage_mod.GLYPH_NEEDS_INPUT == "⏳"          # ⏳
    assert triage_mod.GLYPH_CTX == "⧉"                  # ⧉
    others = [
        path.read_text(encoding="utf-8")
        for path in sorted(pathlib.Path("doxa").rglob("*.py"))
        if path.name not in ("triage.py", "labels.py")
    ]
    assert any(triage_mod.GLYPH_NEEDS_INPUT in text for text in others)
    assert any(triage_mod.GLYPH_CTX in text for text in others)
    # No fifth codepoint class introduced anywhere on the rail: every
    # non-ASCII character a row can show is one of the five this codebase
    # already ships (the four mark glyphs plus the ctx glyph), and the
    # fold carets the rail shipped in v1.0.0.
    painted = set("".join(labels_mod.SIDEBAR_MARK_GLYPHS.values()))
    painted.add(triage_mod.GLYPH_CTX)
    assert {c for c in painted if ord(c) > 127} == {"✓", "▸", "⏳", "⧉"}


def test_needs_input_is_the_mark_the_tab_header_already_carries():
    """Not a second derivation. ``-attention`` is the class
    ``SessionPane._set_tab_class`` writes and the tab strip blinks; this
    feature names it, it does not decide it."""
    assert triage_mod.NEEDS_INPUT_MARK in labels_mod.TAB_STATE_MARKS
    assert triage_mod.needs_input(["-attention"]) is True
    assert triage_mod.needs_input(["-working"]) is False
    assert labels_mod.SIDEBAR_MARK_GLYPHS["-attention"] == (
        triage_mod.GLYPH_NEEDS_INPUT
    )


def test_two_states_get_two_columns_and_a_session_can_be_in_both():
    """One glyph per state, and exactly two states -- a scale of five is
    a gauge and the ctx chip already is one. Two independent columns and
    not one winner, because a session can be waiting for you AND half
    full, and dropping the second is dropping the one that stops being
    recoverable."""
    both = triage_mod.entry_glyphs(["-attention"], 80.0)
    assert both == triage_mod.GLYPH_NEEDS_INPUT + triage_mod.GLYPH_CTX
    assert len(both) == triage_mod.GLYPH_COLUMNS
    assert len(triage_mod.entry_glyphs((), None)) == triage_mod.GLYPH_COLUMNS
    assert triage_mod.entry_glyphs((), None).strip() == ""


def test_the_glyph_columns_are_what_the_rail_width_is_priced_against():
    """The one column this feature costs the rail is DERIVED, not typed
    twice -- moving the glyph budget moves the width."""
    assert layout.SIDEBAR_CHROME == 1 + 2 + triage_mod.GLYPH_COLUMNS + 1 + 1


# -- Part 1: colour keyed to the PROJECT -------------------------------


def test_the_same_repo_is_the_same_colour_on_every_machine():
    """Assigned, not configured, and stored NOWHERE. That is a claim
    about the hash: ``hash()`` is salted per process
    (``PYTHONHASHSEED``), so it would give one repo a different colour in
    every window on one machine. Pinned values here so that swapping the
    digest is a test failure rather than a silent recolouring of every
    user's rail."""
    assert triage_mod.colour_for("/home/me/src/doxa") in triage_mod.PALETTE
    assert triage_mod.colour_for("/home/me/src/doxa") == (
        triage_mod.colour_for("/home/me/src/doxa")
    )
    assert triage_mod.colour_for("/home/me/src/doxa") == "teal"
    assert triage_mod.colour_for("/home/me/src/lore") == "sky"
    assert "doxa" not in str(triage_mod.PALETTE)  # a palette of COLOURS
    # Parsed, not grepped: the docstring above explains why ``hash()`` is
    # wrong here, and a grep would find the explanation.
    tree = ast.parse(pathlib.Path("doxa/triage.py").read_text(encoding="utf-8"))
    called = {
        node.func.id for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "hash" not in called
    assert "hashlib.blake2b" in pathlib.Path(
        "doxa/triage.py"
    ).read_text(encoding="utf-8")


def test_a_session_outside_a_repo_has_no_project_and_therefore_no_colour():
    """**Grey means exactly one thing: the absence of a project colour.**
    Not "old", not "uncategorised-and-also-maybe-old" -- an ungrouped
    entry has no project, so it has no colour, and grey is what "no
    colour" looks like rather than a colour that means something."""
    assert triage_mod.colour_for("") == triage_mod.NO_COLOUR
    assert triage_mod.colour_for(None) == triage_mod.NO_COLOUR
    rows = build_rows(
        (), ["a", "b"],
        _describe({
            "a": _facts("alpha", repo_root="/repo"),
            "b": _facts("beta", repo_root=""),
        }),
    )
    by_label = {row.text: row for row in rows if row.kind == Row.SESSION}
    assert by_label["alpha"].project in triage_mod.PALETTE
    assert by_label["beta"].project == triage_mod.NO_COLOUR
    assert LOOSE_HEADING in [row.text for row in rows if row.kind == Row.HEADING]


def test_the_config_stores_a_palette_NAME_and_a_hex_is_refused(tmp_path):
    """An override for the person who wants ``doxa`` to be blue -- by
    NAME, never a hex, because terminals vary and a user-chosen
    ``#3a3a3a`` is unreadable on half of them. A name that is not in the
    palette falls back to the assignment rather than silently uncolouring
    the project."""
    home = tmp_path / "doxa-home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text(
        '[projects]\n"/home/me/src/doxa" = "rose"\n"/home/me/src/lore" = "#3a3a3a"\n',
        encoding="utf-8",
    )
    config_mod.invalidate()
    assert config_mod.project_colour("/home/me/src/doxa") == "rose"
    assert triage_mod.colour_for(
        "/home/me/src/doxa", config_mod.project_colour("/home/me/src/doxa")
    ) == "rose"
    # A hex is not a palette name: ignored, and the assignment stands.
    assert triage_mod.colour_for(
        "/home/me/src/lore", config_mod.project_colour("/home/me/src/lore")
    ) == triage_mod.colour_for("/home/me/src/lore")
    assert config_mod.project_colour("/somewhere/else") is None


def test_a_colour_collision_costs_redundancy_and_never_meaning():
    """A small palette and a hash means two projects eventually share a
    colour. The rail must not imply they are related: each keeps its own
    heading, named, keyed by ``repo_root`` and never by colour."""
    roots = [f"/tmp/project-{n}" for n in range(200)]
    seen: "dict[str, str]" = {}
    pair = None
    for root in roots:
        colour = triage_mod.colour_for(root)
        if colour in seen and seen[colour] != root:
            pair = (seen[colour], root)
            break
        seen[colour] = root
    assert pair is not None, "a six-name palette must collide within 200 repos"
    first, second = pair
    assert triage_mod.colour_for(first) == triage_mod.colour_for(second)
    rows = build_rows(
        (), ["a", "b"],
        _describe({
            "a": _facts("alpha", repo_root=first),
            "b": _facts("beta", repo_root=second),
        }),
    )
    headings = [row for row in rows if row.kind == Row.HEADING]
    assert len(headings) == 2
    assert headings[0].text != headings[1].text
    assert {h.text for h in headings} == {
        triage_mod.project_name(first), triage_mod.project_name(second)
    }
    # Same colour, two groups -- and no row of one is filed under the
    # other, which is the thing a collision must never cause.
    assert [row.text for row in rows if row.kind == Row.SESSION] == [
        "alpha", "beta"
    ]


def test_identity_and_urgency_are_not_the_same_channel():
    """A session cannot be "the red project" and "red because it needs
    you" at once. The project is a ``-project-<name>`` class; the state
    is glyphs and the four mark classes; nothing writes both into one."""
    line = SidebarLine(
        Row(
            Row.SESSION, "alpha", session_id="a", marks=("-attention",),
            project="rose", ctx_percentage=80.0,
        )
    )
    assert line.has_class("-project-rose")
    assert line.has_class("-attention")
    assert triage_mod.GLYPH_NEEDS_INPUT in line._text()
    assert triage_mod.GLYPH_CTX in line._text()
    # ...and re-pointing the line at another project clears the old class
    # rather than leaving a row wearing two identities.
    line.set_row(Row(Row.SESSION, "beta", session_id="b", project="teal"))
    assert line.has_class("-project-teal")
    assert not line.has_class("-project-rose")


# -- Part 1b: an entry is a PANE, and age is its own channel -----------


def test_old_is_ENDED_and_is_deliberately_not_detached_or_merely_idle():
    """"Old" needed a definition and this is it. A detached session is
    LIVE and may be doing work right now -- dimming it would be the rail
    saying "nothing here" about the row most likely to have something --
    and an idle attached one is a keystroke from being the thing you are
    doing (and would need a clock the rail deliberately does not run)."""
    assert triage_mod.OLD_STATES == frozenset({triage_mod.STATE_ENDED})
    assert triage_mod.is_old(triage_mod.STATE_ENDED) is True
    assert triage_mod.is_old(triage_mod.STATE_DETACHED) is False
    assert triage_mod.is_old(triage_mod.STATE_LIVE) is False


def test_age_dims_and_never_recolours_so_an_old_row_keeps_its_project():
    """The request put two facts on one appearance -- "old sessions fade
    to grey" and "uncategorized entries are grey" -- which would make a
    grey row ambiguous. Resolution: grey is the absence of a project
    colour, and age is a SEPARATE channel that reduces contrast."""
    rows = build_rows(
        (), ["a", "b"],
        _describe({
            "a": _facts("alive", repo_root="/repo"),
            "b": triage_mod.Facts(
                label="over", mounted=False,
                state=triage_mod.STATE_ENDED, repo_root="/repo",
            ),
        }),
    )
    by_label = {row.text: row for row in rows if row.kind == Row.SESSION}
    assert by_label["alive"].old is False
    assert by_label["over"].old is True
    # The old row is the SAME project colour as the live one, faded.
    assert by_label["over"].project == by_label["alive"].project
    assert by_label["over"].project != triage_mod.NO_COLOUR
    line = SidebarLine(by_label["over"])
    assert line.has_class("-old")
    assert line.has_class(f"-project-{by_label['over'].project}")


def test_an_entry_is_a_pane_and_says_how_many_tabs_it_holds():
    """One entry per pane GROUP, with the tabs as its members -- a
    three-tab pane must not look identical to a one-tab pane, which is
    what a flat session list gave. Hidden at zero: a one-tab pane gets no
    entry row at all and renders exactly as it did in v1.0.0."""
    facts = {
        "a": _facts("alpha", repo_root="/repo"),
        "b": _facts("beta", repo_root="/repo"),
        "c": _facts("gamma", repo_root="/repo"),
        "solo": _facts("solo", repo_root="/repo"),
    }
    rows = build_rows(
        (), ["a", "b", "c", "solo"], _describe(facts),
        panes=[
            triage_mod.PaneEntry("g1", ("a", "b", "c"), "a"),
            triage_mod.PaneEntry("g2", ("solo",), "solo"),
        ],
    )
    entries = [row for row in rows if row.kind == Row.ENTRY]
    assert len(entries) == 1
    assert entries[0].count == 3
    assert "·3" in SidebarLine(entries[0])._text()
    # ...and the one-tab pane has no entry row of its own.
    assert [row.text for row in rows if row.kind == Row.SESSION] == [
        "alpha", "beta", "gamma", "solo",
    ]


def test_a_hidden_tabs_state_reaches_the_entry_and_the_entry_says_so():
    """The whole point of the change: the invisible tab needing input is
    exactly what v1.0.0's rail could not surface. And the honest half --
    the entry must show the state is NOT its visible tab's, or a user
    opens the pane, sees a calm active tab, and concludes the rail
    lied."""
    facts = {
        "a": _facts("calm", repo_root="/repo"),
        "b": _facts("waiting", marks=("-attention",), repo_root="/repo"),
        "c": _facts("also calm", repo_root="/repo"),
    }
    rows = build_rows(
        (), ["a", "b", "c"], _describe(facts),
        panes=[triage_mod.PaneEntry("g1", ("a", "b", "c"), "a")],
    )
    entry = next(row for row in rows if row.kind == Row.ENTRY)
    # Most urgent wins, over a member the window is not showing.
    assert "-attention" in entry.marks
    assert entry.hidden is True
    assert entry.position == 2          # member 2 of 3, 1-based, strip order
    assert entry.session_id == "b"      # a click goes THERE, not to the tab
    painted = SidebarLine(entry)._text()
    assert triage_mod.GLYPH_NEEDS_INPUT in painted
    assert "·2/3" in painted       # ·2/3 -- and it is not the visible one
    assert "calm" in painted            # ...which IS what the pane shows


def test_the_visible_tabs_own_state_is_not_reported_as_hidden():
    facts = {
        "a": _facts("waiting", marks=("-attention",), repo_root="/repo"),
        "b": _facts("calm", repo_root="/repo"),
    }
    rows = build_rows(
        (), ["a", "b"], _describe(facts),
        panes=[triage_mod.PaneEntry("g1", ("a", "b"), "a")],
    )
    entry = next(row for row in rows if row.kind == Row.ENTRY)
    assert entry.hidden is False
    assert entry.position == 0
    assert SidebarLine(entry)._text().endswith("·2")


def test_most_urgent_wins_and_borrows_the_one_written_down_order():
    """An entry's state is the maximum urgency over its members. The
    mark ranks are ``TAB_STATE_MARKS``' own index -- not a second
    precedence table -- and the single thing this adds is where ctx%
    slots in: just under needs input."""
    assert triage_mod.urgency(["-attention"]) > triage_mod.urgency((), 99.0)
    assert triage_mod.urgency((), 99.0) > triage_mod.urgency(["-working"])
    assert triage_mod.urgency(["-working"]) > triage_mod.urgency(["-staged"])
    assert triage_mod.urgency(["-staged"]) > triage_mod.urgency(["-done-unseen"])
    assert triage_mod.urgency(["-done-unseen"]) > triage_mod.urgency(())
    state = triage_mod.aggregate(
        triage_mod.PaneEntry("g", ("a", "b", "c"), "a"),
        {
            "a": _facts("a", marks=("-done-unseen",)),
            "b": _facts("b", ctx_percentage=88.0),
            "c": _facts("c", marks=("-staged",)),
        },
    )
    assert state.session_id == "b"
    # Every member's mark reaches the entry -- the same OR over leaves the
    # tab header does, one level up.
    assert state.marks == ("-done-unseen", "-staged")
    assert state.ctx_percentage == 88.0


def test_an_entry_of_ended_members_is_old_but_one_live_member_is_not():
    entry = triage_mod.PaneEntry("g", ("a", "b"), "a")
    ended = triage_mod.Facts(label="x", state=triage_mod.STATE_ENDED)
    live = triage_mod.Facts(label="y", state=triage_mod.STATE_LIVE)
    assert triage_mod.aggregate(entry, {"a": ended, "b": ended}).old is True
    assert triage_mod.aggregate(entry, {"a": ended, "b": live}).old is False


def test_a_manual_collection_claims_SESSIONS_and_a_project_claims_the_rest():
    """Both groupings compose, and a session belongs to exactly one: the
    collection if it is in one, its project otherwise. A collection
    claiming one tab of a three-tab pane must not drag the other two in
    behind it -- the collection is about sessions, and the pane is not an
    argument against what the user said."""
    facts = {
        "a": _facts("alpha", repo_root="/one"),
        "b": _facts("beta", repo_root="/one"),
        "c": _facts("gamma", repo_root="/two"),
    }
    rows = build_rows(
        (collections_mod.Collection("work", ("a",)),),
        ["a", "b", "c"], _describe(facts),
        panes=[triage_mod.PaneEntry("g1", ("a", "b"), "a")],
    )
    shape = [(row.kind, row.text) for row in rows]
    assert shape == [
        (Row.HEADING, "work"),
        (Row.SESSION, "alpha"),
        (Row.HEADING, "one"),
        (Row.SESSION, "beta"),
        (Row.HEADING, "two"),
        (Row.SESSION, "gamma"),
    ]
    # ...and no entry row: with alpha claimed, the pane has one member
    # left, and an entry row over a single member is a sentence said
    # twice.
    assert not [row for row in rows if row.kind == Row.ENTRY]


def test_one_project_and_no_collections_is_still_a_flat_list():
    """Hide at zero, the same judgment v1.0.0 made: a heading over a flat
    list of one project's sessions answers nothing."""
    rows = build_rows(
        (), ["a", "b"],
        _describe({
            "a": _facts("alpha", repo_root="/one"),
            "b": _facts("beta", repo_root="/one"),
        }),
    )
    assert not [row for row in rows if row.kind == Row.HEADING]
    assert [row.indent for row in rows] == [0, 0]


# -- the check this spec owes itself -----------------------------------


def test_the_rail_reads_with_every_colour_stripped():
    """**Can a collection AND every session's status be identified with
    colour stripped entirely?**

    Render the rail monochrome -- a screenshot, a colour-blind operator, a
    terminal that ignores styling -- and read only the characters. This
    test takes the answer away from the stylesheet altogether: it asks
    each line for its TEXT, which carries no markup and no styles at all,
    and requires that

    * every project is identifiable, by its own NAME on its heading;
    * every session's status is identifiable, by Part 0's glyphs;
    * two projects that COLLIDE on a colour are still two named groups;
    * an entry holding tabs you cannot see says how many and which.

    If this ever needs a colour to pass, colour is doing work a glyph or a
    name should be doing, and Part 1 is wrong."""
    first, second = "/tmp/project-0", None
    for candidate in (f"/tmp/project-{n}" for n in range(1, 400)):
        if triage_mod.colour_for(candidate) == triage_mod.colour_for(first):
            second = candidate
            break
    assert second is not None

    facts = {
        "wait": _facts("needs me", marks=("-attention",), repo_root=first),
        "full": _facts("half gone", ctx_percentage=72.0, repo_root=first),
        "quiet": _facts("nothing doing", repo_root=first),
        "unknown": _facts("never measured", repo_root=second),
        "hidden": _facts("buried", marks=("-attention",), repo_root=second),
        "front": _facts("on screen", repo_root=second),
        "nowhere": triage_mod.Facts(
            label="no repo", mounted=False, state=triage_mod.STATE_ENDED,
        ),
    }
    rows = build_rows(
        (),
        ["wait", "full", "quiet", "unknown", "front", "hidden", "nowhere"],
        _describe(facts),
        panes=[
            triage_mod.PaneEntry("g1", ("wait",), "wait"),
            triage_mod.PaneEntry("g2", ("full",), "full"),
            triage_mod.PaneEntry("g3", ("quiet",), "quiet"),
            triage_mod.PaneEntry("g4", ("unknown",), "unknown"),
            triage_mod.PaneEntry("g5", ("front", "hidden"), "front"),
        ],
        width=40,
    )
    # PLAIN TEXT. No markup, no classes, no stylesheet -- this is the
    # screenshot a colour-blind operator files in a bug report.
    painted = [SidebarLine(row)._text() for row in rows]
    assert all("[" not in line for line in painted)

    # 1. Both projects are named, and they are two groups despite sharing
    #    a colour.
    assert triage_mod.colour_for(first) == triage_mod.colour_for(second)
    headings = [
        row.text for row in rows if row.kind == Row.HEADING
    ]
    assert triage_mod.project_name(first) in headings
    assert triage_mod.project_name(second) in headings
    assert LOOSE_HEADING in headings
    assert len(headings) == len(set(headings)) == 3

    # 2. Every session's status is legible from characters alone.
    text_of = {
        row.session_id: line for row, line in zip(rows, painted)
        if row.kind == Row.SESSION
    }
    assert triage_mod.GLYPH_NEEDS_INPUT in text_of["wait"]
    assert triage_mod.GLYPH_CTX in text_of["full"]
    assert text_of["quiet"].strip().startswith("nothing doing")
    # ...and the unmeasured one says nothing at all rather than saying
    # "fine", which is the whole of the honesty rule.
    assert triage_mod.GLYPH_CTX not in text_of["unknown"]
    assert triage_mod.GLYPH_NEEDS_INPUT not in text_of["unknown"]

    # 3. The pane hiding a waiting tab says so, in digits.
    entry = next(line for row, line in zip(rows, painted) if row.kind == Row.ENTRY)
    assert triage_mod.GLYPH_NEEDS_INPUT in entry
    assert "·2/2" in entry
    assert "on screen" in entry

    # 4. Every row's grouping is recoverable without colour: a heading,
    #    then its members, indented.
    depth = {row.text: row.indent for row in rows}
    assert depth[triage_mod.project_name(first)] == 0
    assert depth["needs me"] == 1
    assert depth["on screen"] == 2  # a member of the entry above it


# -- against a real Pilot ----------------------------------------------


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


@pytest.mark.asyncio
async def test_a_waiting_session_paints_its_glyph_on_the_live_rail(tmp_path):
    """Painted, not merely modelled -- the v0.28.0 rule: a structural
    claim is paired with a rectangle."""
    app, _engines = _app(tmp_path)
    async with app.run_test(size=BIG) as pilot:
        await pilot.pause()
        await app.action_new_tab()
        assert await _wait(pilot, lambda: len(app.panes()) == 2)
        assert app.set_sidebar(True) is None
        rail = app.sidebar()
        assert await _wait(pilot, lambda: rail is not None and rail.size.width > 0)
        pane = app.panes()[0]
        pane._set_tab_class("-attention", True)
        assert await _wait(
            pilot,
            lambda: any(
                triage_mod.GLYPH_NEEDS_INPUT in line._text()
                for line in rail.lines()
            ),
        )
        # ...and the rail's rows are still not focusable, the v0.85.0
        # defect v1.0.0 declined to re-open.
        assert all(line.can_focus is False for line in rail.lines())


@pytest.mark.asyncio
async def test_the_glyphs_are_deliberately_NOT_on_the_tab_header_yet(tmp_path):
    """**The scope question the spec leaves open, answered: deferred.**

    The same facts drive the tab header, and a user with the rail hidden
    is otherwise blind to what the rail exists to show -- so this is a
    real gap and it is named as one in the CHANGELOG, not left ambiguous.
    It is deferred because the tab strip signals with COLOUR alone for a
    measured reason (two columns of padding per header and no room for
    more; ``GROUP_STRIP_COMPACT_COLS`` already cuts the label to the model
    segment on a split window), so adding a glyph there is a width
    negotiation and not a render change, and it belongs with Part 2's
    label work rather than bolted onto this release.

    Pinned as a TEST so the decision is checkable rather than merely
    stated: the day a tab label starts carrying a status glyph, this test
    fails and someone re-reads the paragraph above."""
    app, _engines = _app(tmp_path)
    async with app.run_test(size=BIG) as pilot:
        await pilot.pause()
        pane = app.panes()[0]
        assert await _wait(pilot, lambda: bool(pane._session_id))
        pane._set_tab_class("-attention", True)
        await pilot.pause()
        header = labels_mod._strip_holding(app, pane.tab.id).get_tab(
            pane.tab.id
        )
        label = str(header.label)
        assert label
        assert triage_mod.GLYPH_NEEDS_INPUT not in label
        assert triage_mod.GLYPH_CTX not in label
        # The strip signals by COLOUR here, and that is unchanged.
        assert header.has_class("-attention")
        # The header still says it, in the channel it has always used.
        assert pane.has_mark("-attention") is True


@pytest.mark.asyncio
async def test_a_mark_on_a_hidden_member_rebuilds_rather_than_leaving_a_lie(
    tmp_path,
):
    """A mark moving on a member of a MULTI-tab pane can change which
    member wins, and that is a structure change. The rail refuses the
    in-place update and asks for the rebuild rather than quietly leaving
    the entry row reporting a state that has moved -- while the ordinary
    one-tab window keeps the cheap path, which is what the 2 Hz blink
    runs on."""
    app, _engines = _app(tmp_path)
    async with app.run_test(size=BIG) as pilot:
        await pilot.pause()
        await app.action_new_tab()
        assert await _wait(pilot, lambda: len(app.panes()) == 2)
        assert app.set_sidebar(True) is None
        rail = app.sidebar()
        assert await _wait(
            pilot, lambda: rail is not None and len(rail.rows()) == 3
        )
        assert any(row.kind == Row.ENTRY for row in rail.rows())
        hidden = app.panes()[0]
        assert rail.apply_marks(hidden._session_id, ("-attention",)) is False
        hidden._set_tab_class("-attention", True)
        assert await _wait(
            pilot,
            lambda: any(
                row.kind == Row.ENTRY
                and "-attention" in row.marks
                for row in rail.rows()
            ),
        )
        entry = next(row for row in rail.rows() if row.kind == Row.ENTRY)
        assert entry.count == 2

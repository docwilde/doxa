# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.ui.sidebar -- the session rail: chrome that is NOT in the layout
tree.

**The one decision this file exists to encode.** The rail is a SIBLING of
the window root, not a member of it (docs/plans/session-sidebar.md)::

    Screen
    └── Horizontal
        ├── SessionSidebar          ← this file
        └── SplitBox (window root)  ← v0.97.0's tree, untouched

That placement is the whole design, and it buys three things at once:
:meth:`doxa.app.DoxaApp._window_root` still returns the outermost
:class:`~doxa.ui.split.SplitBox` and therefore needs no change and no
``isinstance`` special case; splits, ``Alt+arrow`` growth, directional
focus and ``_pane_regions`` operate on the tree and never see the rail;
and collapsing the rail changes the tree's width and nothing else.

**The trap it avoids**, stated because it is the cheap-looking route:
making the rail a :class:`doxa.layout.Leaf` with a new ``view`` kind.
v0.92.0 added ``Leaf.view`` for the live diff and it worked, which is
exactly why it looks proven here. It is wrong: a leaf can be SPLIT,
CLOSED, MOVED between groups and PERSISTED per group, and a rail must be
none of those. The first ``Alt+D`` on it would prove the point.

**Why the container exists from ``compose``.** Textual 5.3 cannot
re-parent a mounted widget -- mounting an already-mounted one is a silent
no-op that orphans it (measured in v0.91.0, not assumed) -- so the
``Horizontal`` cannot be wrapped around the window root at runtime. It is
yielded by :meth:`doxa.app.DoxaApp.compose` with the rail already inside
it, hidden when off. Same constraint, same answer as
:func:`doxa.ui.split.chain`'s pre-made empty boxes.

**Why the rows are built from a pure function.** :func:`build_rows` takes
collections, an ordering and a label/mark lookup and returns a flat list
of :class:`Row`; the widget only mounts what it is handed. Which rows a
rail SHOWS -- a collapsed collection hides its members, a collection
member whose tab is closed still gets a row, the implicit heading is
always last and never persisted -- is the part that is hard to get right,
so it is the part that is testable without a running app. The same split
:mod:`doxa.layout` keeps from :mod:`doxa.ui.split`.

**What a click DOES** (v1.5.0, option C). A rail entry has been a pane
GROUP since v1.2.0, and every group is on screen at once -- so a click on
one could only ever move focus, which is the defect the owner reported:
there was nothing hidden to reveal. The rows under a group's heading are
now its TABS, which are the one genuinely hidden thing a window has, and
the three gestures are separated rather than overloaded:

* the heading's **caret** folds the group (persisted -- see
  :meth:`doxa.app.DoxaApp.toggle_group_expanded`);
* the rest of the **heading** focuses the group and leaves its active tab
  alone -- it is the group's summary, and a summary that silently switched
  what you were looking at would be the rail pointing at one thing and
  delivering another;
* a **tab row** switches that group's active tab and focuses it. That is
  the reveal, and it is the only gesture here that changes what is drawn.

None of the three is rail-only: ``Ctrl+1..9`` / ``/pane <n>`` focus a
group and ``Ctrl+←/→`` cycles the focused group's tabs, so a user who
closed the rail with ``F3`` has lost no capability -- which is the check
docs/plans/rail-interaction.md asks this feature to answer.

**A rail is not a tab strip**, and :func:`build_rows` is where that is
true rather than merely claimed: a row is built for every session the
CALLER knows about, and the caller knows about sessions that are not
mounted in any group -- a detached peer, an archived transcript, a
collection member whose tab was closed. Such a row is marked
``mounted=False``, renders dimmed with a note, and answers a click by
saying so instead of pretending it can be focused.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from textual import events
from textual.containers import VerticalScroll
from textual.message import Message
from textual.widgets import Static

from .. import collections as collections_mod
from .. import config as config_mod
from .. import layout as layout_mod
from .. import triage as triage_mod
from .labels import ellipsize

#: The implicit heading loose sessions live under: unnamed, always last,
#: and NOT a collection. It is derived at render time and never written to
#: the record -- see :mod:`doxa.collections`' docstring for why persisting
#: it would make it a collection with a reserved name and give every edit
#: function a case to carry.
#:
#: v1.2.0 narrows what lands here. An entry with a ``repo_root`` gets its
#: PROJECT's heading (auto-grouping, Part 1); only an entry with no
#: project at all -- a session outside any repo -- is ungrouped, and it is
#: the one row that is genuinely grey, because grey is the absence of a
#: project colour and nothing else (:data:`doxa.triage.NO_COLOUR`).
LOOSE_HEADING = "— ungrouped —"

#: What a row for a session with no pane behind it says. Short, because it
#: shares a 22-column rail with the label it qualifies.
NOT_OPEN = "· closed"

#: The caret on a collection heading: pointing right when it is folded,
#: down when it is open -- the direction every tree view in every terminal
#: uses, so it needs no legend.
FOLD_SHUT = "\u25b8"
FOLD_OPEN = "\u25be"

#: What a caret and the space after it cost, and the width of the hit area
#: a click has to land in to FOLD rather than to focus. Two gestures on one
#: line is what every tree view in every file manager does -- the arrow
#: folds, the label selects -- and the caret is the affordance that
#: promises it, so the zone is exactly the caret's own columns.
FOLD_COLUMNS = 2

#: Columns of indent per level, matching ``.-in-collection`` and
#: ``.-in-entry`` in doxa/theme.tcss. Capped at level 2 because that is
#: the deepest the rail goes -- heading, pane group, tab -- and priced
#: into :data:`doxa.layout.SIDEBAR_CHROME`.
INDENT_COLUMNS = 2
MAX_INDENT = 2


@dataclass(frozen=True)
class Row:
    """One line of the rail: a heading, or an ENTRY.

    ``collection`` is the heading's own name on a collection heading and
    the holding collection's name on an entry row (empty for an entry
    under a project heading or under the ungrouped one), so a click on
    either knows what it is about without the widget keeping an index
    beside the list.

    **An entry row is a PANE, not a session** (v1.2.0, Part 1b). Since
    v0.97.0 a :class:`doxa.ui.split.PaneGroup` owns its own tabs, so one
    visible pane can hold three sessions of which two are invisible; a
    rail that listed sessions flat could not show the two. ``count`` is
    how many tabs the entry holds and ``position``/``hidden`` say whether
    the state it is reporting is the visible tab's -- see
    :meth:`doxa.triage.EntryState.count_chip`. ``session_id`` is the
    member the STATE came from, so a click reveals the tab the row is
    actually talking about."""

    kind: str
    text: str
    session_id: str = ""
    collection: str = ""
    collapsed: bool = False
    marks: "tuple[str, ...]" = field(default=())
    mounted: bool = True
    #: The palette NAME for this row's project, or "" for none. Never a
    #: hex: doxa/theme.tcss resolves ``-project-<name>`` so a theme change
    #: re-resolves the colour rather than stranding one.
    project: str = ""
    #: Dim this row -- reduced contrast, never a recolour, so an old entry
    #: in a coloured project stays that project's colour, faded. See
    #: :data:`doxa.triage.OLD_STATES` for what "old" is and is not.
    old: bool = False
    #: The CLI's own accounting for this entry, or ``None`` for a session
    #: whose limit was never reported. ``None`` renders NO ctx glyph --
    #: not the absence of a warning, which would read as "plenty of room".
    ctx_percentage: "float | None" = None
    count: int = 0
    position: int = 0
    hidden: bool = False
    #: Which pane GROUP this row belongs to -- :attr:`doxa.ui.split.
    #: PaneGroup.entry_key`. Carried by an ENTRY row (it IS that group)
    #: and by every tab row under it (it is a tab OF that group), so a
    #: click knows which group's active tab it is switching without the
    #: widget keeping an index beside the list. Empty on a heading and on
    #: a collection member, neither of which is about one group.
    entry_key: str = ""
    #: Is this group's tab list showing? ENTRY rows only, and only
    #: meaningful when :attr:`count` is above 1 -- a single-tab group has
    #: nothing to fold and wears no caret (hide at zero).
    expanded: bool = True
    #: How deep this row sits: 0 at the rail's own margin, 1 under a
    #: heading (a collection's or a project's), 2 under an ENTRY row. An
    #: int rather than a flag because there are now three levels and a
    #: second bool would have to be kept consistent with the first.
    indent: int = 0

    HEADING = "heading"
    SESSION = "session"
    #: A PANE GROUP's own line, and since v1.5.0 the group's HEADING: it
    #: is drawn for every group, above its tab rows when it has more than
    #: one and alone when it has one (hide at zero -- a one-tab pane and a
    #: row repeating it are the same thing said twice). It carries the
    #: aggregate -- most urgent over every member, the invisible ones
    #: included -- and the count chip that says how many tabs there are
    #: and which of them the state came from.
    ENTRY = "entry"


def _facts(answer: Any) -> "triage_mod.Facts":
    """One session's facts, however the caller phrased them.

    :class:`doxa.triage.Facts` is what :meth:`doxa.app.DoxaApp.
    _describe_session` returns; the ``(label, marks, mounted)`` triple is
    what v1.0.0's callers passed and what a harness that only cares about
    row structure still wants to pass. Accepting both is four lines here
    and saves every such caller from restating four defaults."""
    if isinstance(answer, triage_mod.Facts):
        return answer
    label, marks, mounted = answer
    return triage_mod.Facts(
        label=label, marks=tuple(marks or ()), mounted=bool(mounted)
    )


def build_rows(
    items: "Sequence[collections_mod.Collection]",
    order: "Sequence[str]",
    describe: "Callable[[str], Any]",
    *,
    width: int = layout_mod.SIDEBAR_WIDTH,
    panes: "Sequence[triage_mod.PaneEntry]" = (),
    collapsed_groups: "Sequence[str]" = (),
) -> "list[Row]":
    """The rail's contents, top to bottom.

    ``order`` is every session the rail knows about, in the order the
    LOOSE ones should appear -- the caller passes strip order, so an
    ungrouped session sits where the tab bar has it. Collection members
    keep their COLLECTION's order instead, which is the user's and is the
    whole reason a collection stores a list rather than a set.

    ``describe`` answers, for one session id, its
    :class:`doxa.triage.Facts` -- the app's job, because a label is
    ``display_name()`` off a live pane, a mark is that pane's own
    ``has_mark`` and a ctx% is its engine's last reported number, and none
    of them belongs in a module that must stay importable without a
    screen.

    ``panes`` is the window's pane GROUPS, each with its member sessions
    in strip order and its visible one named. A session no entry claims
    becomes its own single-member entry -- a detached peer, an ended
    session, an archived transcript -- which is v1.0.0's "a rail is a
    session index, not a second tab strip" restated now that an entry is
    a pane.

    **Three grouping levels, and a session belongs to exactly one.** A
    manual collection claims it if one names it (the user decided); its
    PROJECT claims it otherwise (``repo_root``, derivable, no naming
    needed); and only a session with no project at all falls to the
    implicit ungrouped heading. Collections come first in their own
    order, projects next in first-appearance order, ungrouped last.

    **Headings are hidden at zero**, the same judgment v1.0.0 made and
    for the same reason: a header over a flat list answers nothing. They
    are drawn when there is a collection, or when the loose entries span
    more than one section -- never over a window that is one project's
    sessions and nothing else."""
    label_room = max(4, int(width) - layout_mod.SIDEBAR_CHROME)
    known = [s for s in order if s]
    known_set = set(known)
    facts = {session_id: _facts(describe(session_id)) for session_id in known}

    # -- who claims what, decided BEFORE anything is drawn.
    #
    # A collection claims SESSIONS, not panes. That is the whole meaning
    # of "a manual collection overrides the automatic grouping": the user
    # said these three sessions are one piece of work, and a pane group
    # they happen to share is not an argument against it. So a collection
    # member is drawn as a plain row under its heading, and the pane
    # grouping applies to what is LEFT -- which is also why a collection
    # can hold one tab of a three-tab pane without dragging the other two
    # in behind it.
    claimed: "dict[str, list[str]]" = {}
    taken: "set[str]" = set()
    for item in items:
        picked = [
            session_id for session_id in item.sessions
            # Dropped for the same reason doxa.layout.prune drops a dead
            # leaf: the record names sessions, and one this window has
            # never heard of is not a row it can describe.
            if session_id in known_set and session_id not in taken
            and facts[session_id].label
        ]
        taken.update(picked)
        claimed[item.name] = picked

    entries = triage_mod.entries_for(
        [s for s in known if s not in taken], panes
    )
    by_key = {entry.key: entry for entry in entries}
    states: "dict[str, triage_mod.EntryState]" = {}
    for entry in entries:
        state = triage_mod.aggregate(entry, facts)
        if state is not None:
            states[entry.key] = state

    #: The groups the user has folded shut. A SET of entry keys, defaulting
    #: empty, so the rail opens showing every tab it knows about and a fold
    #: is a thing the user did -- the same default a collection has, and
    #: the same posture ``Collection.collapsed`` takes about being written
    #: only when true. A key naming a group this window no longer has is
    #: simply never asked about; it costs nothing and survives the layout
    #: changing under it, which is the whole reason this is a set of keys
    #: rather than a flag on a widget.
    folded = {key for key in collapsed_groups if key}

    # One config read and one hash per PROJECT, not per row: a window is
    # far more rows than it is projects, and this runs on a paint path.
    colours: "dict[str, str]" = {}

    def colour_of(repo_root: str) -> str:
        if repo_root not in colours:
            colours[repo_root] = triage_mod.colour_for(
                repo_root, config_mod.project_colour(repo_root)
            )
        return colours[repo_root]

    def session_row(
        session_id: str, collection: str, indent: int, entry_key: str = ""
    ) -> "Row | None":
        fact = facts.get(session_id)
        if fact is None or not fact.label:
            return None
        return Row(
            Row.SESSION,
            ellipsize(fact.label, label_room),
            session_id=session_id,
            collection=collection,
            entry_key=entry_key,
            marks=fact.marks,
            mounted=fact.mounted,
            project=colour_of(fact.repo_root),
            old=triage_mod.is_old(fact.state),
            ctx_percentage=fact.ctx_percentage,
            indent=indent,
        )

    def entry_rows(
        key: str, collection: str, indent: int
    ) -> "list[Row]":
        """One pane group's lines: **its own row, always**, then a row per
        tab when it has more than one and is not folded.

        **The heading IS the group** (v1.5.0, option C -- the owner's
        choice of the three docs/plans/rail-interaction.md put up). It
        carries the group's colour, the aggregate over every member
        including the tabs no pixel of which is on screen (Part 1b's
        most-urgent-wins) and the count chip that says which member the
        state came from; a click on it focuses the group and does NOT
        move the active tab. The rows BELOW it are the group's tabs, each
        carrying its own marks rather than the roll-up, because two rows
        under one heading claiming the same state would be a lie.

        **A single-tab group grows no child row.** Hide at zero, the same
        judgment every other piece of chrome in this app makes: one tab is
        the heading's own subject, and a row repeating it is the same
        sentence twice. Such a group is one line, exactly as it was in
        v1.0.0 and v1.2.0 -- which is the overwhelmingly common window.

        v1.2.0 drew this the other way round: the members always, and the
        entry row only above two or more of them. The reversal is what
        makes the rail able to REVEAL anything at all. A group's inactive
        tab is the one genuinely hidden thing this window has, and until
        the rail listed tabs UNDER the group that owns them, a click could
        only move focus -- which is the defect the owner reported."""
        state = states.get(key)
        if state is None:
            return []
        entry = by_key[key]
        expanded = key not in folded
        visible = facts.get(entry.active)
        out: "list[Row]" = [
            Row(
                Row.ENTRY,
                ellipsize(
                    (visible.label if visible else state.label), label_room
                ),
                # The member the STATE came from, so a fallback that can
                # only reveal a session still lands on the one this row is
                # reporting -- see SessionSidebar.GroupFocused.
                session_id=state.session_id,
                collection=collection,
                entry_key=key,
                expanded=expanded,
                marks=state.marks,
                mounted=state.mounted,
                project=colour_of(state.repo_root),
                old=state.old,
                ctx_percentage=state.ctx_percentage,
                count=state.count,
                position=state.position,
                hidden=state.hidden,
                indent=indent,
            )
        ]
        if state.count <= 1 or not expanded:
            return out
        for session_id in entry.sessions:
            row = session_row(session_id, collection, indent + 1, key)
            if row is not None:
                out.append(row)
        return out

    #: project root -> its entry keys, in first-appearance order. "" is
    #: the ungrouped section and is always drawn last.
    sections: "dict[str, list[str]]" = {}
    for entry in entries:
        if entry.key not in states:
            continue
        sections.setdefault(states[entry.key].repo_root, []).append(entry.key)
    loose_keys = sections.pop("", [])

    headed = bool(items) or (len(sections) + (1 if loose_keys else 0)) > 1

    rows: "list[Row]" = []
    for item in items:
        rows.append(
            Row(
                Row.HEADING, ellipsize(item.name, max(4, int(width) - 4)),
                collection=item.name, collapsed=item.collapsed,
            )
        )
        # A collection with no members at all still gets its heading: it
        # is a thing the user made two seconds ago with `/collection new`
        # and is about to move a session into, and a heading that appeared
        # only once it was non-empty would read as the command having
        # failed.
        if item.collapsed:
            continue
        rows.extend(
            row for row in (
                session_row(session_id, item.name, 1)
                for session_id in claimed[item.name]
            ) if row is not None
        )

    for repo_root, keys in sections.items():
        if headed:
            rows.append(
                Row(
                    Row.HEADING,
                    ellipsize(
                        triage_mod.project_name(repo_root),
                        max(4, int(width) - 4),
                    ),
                    project=colour_of(repo_root),
                )
            )
        rows.extend(
            row for key in keys
            for row in entry_rows(key, "", 1 if headed else 0)
        )

    if loose_keys:
        if headed:
            rows.append(Row(Row.HEADING, LOOSE_HEADING))
        rows.extend(
            row for key in loose_keys
            for row in entry_rows(key, "", 1 if headed else 0)
        )
    return rows


class SidebarLine(Static):
    """One painted line of the rail.

    A plain ``Static`` carrying the row's classes, deliberately NOT a
    ``ListItem`` or an ``OptionList`` row: both are focusable, and a
    focusable widget beside the prompt is a second place the keyboard can
    end up. v0.85.0 measured what that costs -- ``App.AUTO_FOCUS = "*"``
    picking the first focusable widget in the DOM, unscoped by which tab
    is visible -- and the rail is not worth re-opening it. The rail is
    driven by the mouse, by ``F3`` and by ``/collection``; nothing in
    it ever takes the keyboard.

    Never overrides ``_render``: that is ``textual.widget.Widget``'s own
    paint hook and must return a ``Visual``."""

    def __init__(self, row: Row) -> None:
        super().__init__("")
        # Deliberately NOT ``self.row = row`` before the call below:
        # :meth:`set_row` short-circuits on a row it is already showing,
        # so seeding the attribute here would skip the one write that
        # gives a fresh line its classes at all.
        self.row: "Row | None" = None
        self.can_focus = False
        self.set_row(row)

    def set_row(self, row: Row) -> None:
        """Become that row -- IN PLACE.

        A line is reused rather than replaced (see
        :meth:`SessionSidebar.set_rows`), so this is the only path by which
        a row's identity changes and it must leave nothing of the previous
        one behind: every class it can carry is written on every call,
        true or false.

        **Unless it is already that row**, which is the common case and is
        not free to redo. ``refresh_sidebar(force=True)`` -- what
        ``on_show`` passes, and every collection edit -- clears the rail's
        own "nothing changed" cache, so without this guard every forced
        refresh rewrote eight classes and the text on every visible line.
        Each ``set_class`` marks the node for a stylesheet re-apply, and
        Textual's ``Stylesheet.apply`` / ``Screen._refresh_layout`` are
        SYNCHRONOUS: measured on tests/test_split_panes.py, they are where
        the event loop actually blocks (max 305 ms and 334 ms in one
        module), so redundant class writes are paid for in loop-stall
        budget rather than in microseconds. ``Row`` is a frozen dataclass,
        so the comparison is free."""
        if row == self.row:
            return
        previous = self.row
        self.row = row
        self.set_class(row.kind == Row.HEADING, "-heading")
        self.set_class(row.kind in (Row.SESSION, Row.ENTRY), "-session")
        # Muted against its members -- and therefore ONLY when it has
        # members drawn under it. A single-tab group's heading is the
        # whole of that group on the rail, and muting a line that
        # summarises nothing but itself would dim the ordinary window's
        # every row (see doxa/theme.tcss's ``.-entry``).
        self.set_class(row.kind == Row.ENTRY and row.count > 1, "-entry")
        self.set_class(
            row.indent == 1 and row.kind != Row.HEADING, "-in-collection"
        )
        self.set_class(
            row.indent >= 2 and row.kind != Row.HEADING, "-in-entry"
        )
        self.set_class(row.kind != Row.HEADING and not row.mounted, "-closed")
        # AGE is its own channel: dim, never a recolour. An old entry in a
        # coloured project stays that project's colour, faded -- which is
        # what "fade to grey" actually describes, and it composes with
        # grouping instead of competing with it. Grey stays reserved for
        # exactly one thing: the absence of a project colour.
        self.set_class(row.old, "-old")
        self._write_heading_paint(row)
        self._write_project(previous, row)
        self._write_marks(row.marks)
        self.update(self._text())

    def apply_marks(
        self, marks: "Sequence[str]", ctx_percentage: "float | None" = None
    ) -> None:
        """Update ONLY the status marks and the ctx reading, without
        rebuilding the row.

        What :meth:`doxa.app.DoxaApp.refresh_sidebar_marks` calls when a
        pane's marks move -- including once per blink of the needs-input
        timer, which is why it must not cost a row rebuild. Everything
        identity-shaped (the project colour, the count chip, whether the
        state came from a hidden tab) is carried over untouched: a mark
        moving is by definition not a structure change, and re-deriving
        the structure here is precisely the +22% layout cost v1.0.0
        measured and refused.

        ``ctx_percentage`` is passed through rather than defaulted,
        because ``None`` is a REAL value here -- a session whose limit was
        never reported -- and a default that meant "leave it alone" would
        make the two indistinguishable at the one call site that can tell
        them apart."""
        from dataclasses import replace

        self.row = replace(
            self.row,
            marks=tuple(marks or ()),
            ctx_percentage=ctx_percentage,
        )
        self._write_marks(self.row.marks)
        self.update(self._text())

    def _write_heading_paint(self, row: Row) -> None:
        """A heading wears a BACKGROUND, and its text is black or white --
        COMPUTED, never chosen.

        :func:`doxa.triage.heading_paint` resolves the pair once per
        palette name and keeps it, because this runs per heading per
        refresh and a refresh runs on every tab lifecycle event: v1.2.0
        measured re-deriving in the rail at +22% layout time, and a pow()
        per row per paint is that mistake in a new place.

        **Written INLINE and not as a class**, which is the one thing here
        worth arguing about. The obvious alternative -- a generated rule
        per palette name -- would have to out-rank doxa/theme.tcss's own
        ``.-project-<name>`` colour, and the moment it did it would also
        out-rank the four STATUS rules that are supposed to win on a row
        carrying both. A heading never carries a mark (``build_rows``
        builds it with none), so the conflict cannot arise here and
        nowhere else gets an inline colour: the three-channel design
        (identity / state / age) survives intact, and only the surface
        that has no state to show is painted from Python.

        Cleared on every non-heading row rather than left behind: a line is
        REUSED in place, so a heading that becomes a session row must lose
        the paint or it would carry a project background into a row whose
        colour means something else."""
        if row.kind != Row.HEADING:
            self.styles.clear_rule("background")
            self.styles.clear_rule("color")
            return
        background, text = triage_mod.heading_paint(row.project)
        self.styles.background = background
        self.styles.color = text

    def _write_project(self, previous: "Row | None", row: Row) -> None:
        """Paint this line's PROJECT -- identity, one class, resolved by
        doxa/theme.tcss.

        Only the class that changed is written: a line is reused in place
        and there are :data:`doxa.triage.PALETTE` classes it could carry,
        so writing all of them on every row would be six stylesheet
        re-applies per line per refresh. Each ``set_class`` marks the node
        for a synchronous ``Stylesheet.apply``, which is where the event
        loop actually blocks (see :meth:`set_row`)."""
        was = previous.project if previous is not None else ""
        if was == row.project:
            return
        if was:
            self.set_class(False, f"-project-{was}")
        if row.project:
            self.set_class(True, f"-project-{row.project}")

    def _write_marks(self, marks: "Sequence[str]") -> None:
        """Write the four status classes onto this line and let the
        STYLESHEET resolve them -- the same four classes, in the same
        cascade order, that a group's ``Tab`` header carries. The rail
        does not decide what outranks what; doxa/theme.tcss does, once,
        for both surfaces (see :data:`doxa.ui.labels.TAB_STATE_MARKS`)."""
        from .labels import TAB_STATE_MARKS

        held = set(marks or ())
        for name in TAB_STATE_MARKS:
            self.set_class(name in held, name)

    def _text(self) -> str:
        """The line, in plain text and nothing else.

        No markup, no styles, no colour: everything this string says, it
        says in characters. That is the design check the spec owes itself
        -- render the rail monochrome and every project and every session
        status must still be identifiable -- and this method is where the
        answer is either yes or no. A collection or project is named; a
        state is glyphed; a pane's tab count and which of them the state
        came from are digits. Colour is redundancy on all three."""
        row = self.row
        if row.kind == Row.HEADING:
            if not row.collection:
                # A project heading and the ungrouped one: neither folds,
                # so neither wears a caret that would promise it does.
                return row.text
            return f"{FOLD_SHUT if row.collapsed else FOLD_OPEN} {row.text}"
        glyphs = triage_mod.entry_glyphs(row.marks, row.ctx_percentage)
        chip = triage_mod.EntryState(
            count=row.count, position=row.position, hidden=row.hidden
        ).count_chip()
        tail = f" {NOT_OPEN}" if not row.mounted else ""
        if self.folds():
            caret = FOLD_OPEN if row.expanded else FOLD_SHUT
            return f"{caret} {glyphs} {row.text}{chip}{tail}"
        return f"{glyphs} {row.text}{chip}{tail}"

    def folds(self) -> bool:
        """Does this line wear a fold caret? A pane group with more than
        one tab, and nothing else.

        Hide at zero, the same rule the count chip follows one field over:
        a single-tab group has nothing under it to fold, and a caret that
        promised otherwise would be chrome that lies. A collection heading
        answers False here because its caret is written by the HEADING
        branch of :meth:`_text` and its whole row is the hit area."""
        row = self.row
        return bool(
            row is not None and row.kind == Row.ENTRY and row.count > 1
        )

    def fold_zone(self) -> int:
        """The column, relative to this line's own box, past which a click
        means FOCUS rather than FOLD. ``0`` when the row has no caret.

        Derived from the row's indent and doxa/theme.tcss's padding, so a
        change to either moves the hit area with the glyph rather than
        leaving the two a level apart."""
        if not self.folds():
            return 0
        return INDENT_COLUMNS * min(self.row.indent, MAX_INDENT) + FOLD_COLUMNS

    def staging(self) -> bool:
        """Does a DOUBLE click on this line stage ``/attach``?

        Exactly the rows that are drawn ``· closed``: an entry or a tab
        row naming a session with no pane behind it. Three exclusions,
        each of them a row that looks adjacent and is not:

        * a HEADING is not a session at all;
        * an ARCHIVED tab (``ArchivedSessionTab``) is MOUNTED, so it
          reveals like any other row and there is nothing to attach to --
          it is already here;
        * a REAPED session (``/sessions kill``) has no row on the rail in
          the first place (``DoxaApp._sidebar_order`` filters
          ``_killed_this_run``), because reaping is the one gesture in
          this app that means "forget this conversation". This method
          could not reach one, and the exclusion is stated here so that
          staying unreachable is a property somebody has written down."""
        row = self.row
        return bool(
            row is not None
            and row.kind != Row.HEADING
            and not row.mounted
            and row.session_id
        )

    def on_click(self, event: events.Click) -> None:
        """Three gestures, told apart by what was clicked -- see this
        module's docstring for why they are three and not one.

        A GROUP heading focuses its group and leaves the active tab where
        it is; its caret folds it instead. A TAB row switches that group's
        active tab and focuses it, which is the reveal the rail could not
        perform before v1.5.0. A COLLECTION heading folds, as it has since
        v1.0.0, on a click anywhere along it -- it has no second gesture to
        make room for.

        v1.2.0 sent an entry row's click to the member its state came
        from, which under option C would be the summary silently switching
        the tab underneath the user. The state's member is still carried
        (``session_id``) and is still where the FALLBACK lands, for the
        entry that is not a live group at all -- a detached or ended
        session, which :func:`doxa.triage.entries_for` gives an entry of
        its own."""
        event.stop()
        row = self.row
        if row is None:
            return
        # The FOURTH gesture (v1.5.1), and the only one that reads
        # ``event.chain``: a double click on a closed row stages
        # ``/attach``. It is checked first and returns, so the second
        # click of the pair does not ALSO repeat the first one's answer.
        #
        # The first click still lands, and still says "not open in this
        # window -- /attach abc12345 brings it back": Textual delivers
        # chain 1 and then chain 2, and swallowing the first would mean
        # guessing, on a timer, whether a second is coming. So the two
        # beats say the same thing in the two places it belongs -- the
        # transcript names the command, the prompt holds it -- and a
        # single click keeps exactly the behaviour it has always had.
        if event.chain >= 2 and self.staging():
            self.post_message(SessionSidebar.AttachStaged(row.session_id))
            return
        if row.kind == Row.HEADING:
            if row.collection:
                self.post_message(SessionSidebar.CollectionToggled(
                    row.collection
                ))
            return
        if row.kind == Row.ENTRY:
            if self.folds() and event.x < self.fold_zone():
                self.post_message(SessionSidebar.GroupToggled(row.entry_key))
                return
            self.post_message(
                SessionSidebar.GroupFocused(row.entry_key, row.session_id)
            )
            return
        if row.session_id:
            self.post_message(SessionSidebar.Revealed(row.session_id))


class SessionSidebar(VerticalScroll):
    """The rail itself.

    Scrolls, because a window can hold more sessions than a terminal has
    rows -- and therefore obeys the v0.99.0 rule about hidden geometry: a
    hidden widget has no geometry at all, ``container_size`` and
    ``virtual_size`` go stale at their last visible values while ``size``
    really does go to zero, so ``size`` is the readiness test and
    :meth:`on_show` is where an intent formed while hidden is spent. The
    rail's intent is only ever "rebuild me", which is why that handler
    asks the app rather than replaying a scroll -- but it is the same
    shape as ``SessionPane.scroll_transcript_to_end``'s and for the same
    measured reason."""

    class Revealed(Message):
        """A row was picked: take me to that session."""

        def __init__(self, session_id: str) -> None:
            super().__init__()
            self.session_id = session_id

    class CollectionToggled(Message):
        """A heading was clicked: collapse or expand it."""

        def __init__(self, name: str) -> None:
            super().__init__()
            self.name = name

    class GroupFocused(Message):
        """A pane group's heading was clicked: put the keyboard in that
        group and leave its active tab exactly where it is.

        ``session_id`` is the member the heading's STATE came from, and it
        is what the app falls back to when ``entry_key`` names no group in
        this window -- a detached or ended session, which gets an entry of
        its own (:func:`doxa.triage.entries_for`) and is the one case
        where "focus the group" has no group to mean."""

        def __init__(self, entry_key: str, session_id: str = "") -> None:
            super().__init__()
            self.entry_key = entry_key
            self.session_id = session_id

    class GroupToggled(Message):
        """A pane group's caret was clicked: show or hide its tab rows.
        Persisted per group, beside the collapsed flag a collection has
        (:mod:`doxa.tabsets`)."""

        def __init__(self, entry_key: str) -> None:
            super().__init__()
            self.entry_key = entry_key

    class AttachStaged(Message):
        """A CLOSED row was double-clicked: put ``/attach <id>`` in the
        prompt, unsent.

        Only a closed row can send this -- a row whose session has no pane
        behind it, which is the one row a click has never been able to do
        anything with. :meth:`doxa.app.DoxaApp.reveal_session` answers it
        by NAMING the command (``abc12345 is not open in this window --
        /attach abc12345 brings it back in a new tab``); this hands the
        user the same command typed out instead of asking them to copy it.

        **Staged, never submitted**, which is the whole of the gesture.
        ``/attach`` opens a tab against a live daemon; a double-click that
        ran it would be a mouse gesture with a session-shaped consequence
        and no step at which the user could read what it was about to do.
        The prompt is where a command waits to be read -- see
        :meth:`doxa.app.DoxaApp._cmd_prefill`, which ``Ctrl+R`` already
        uses for the same reason."""

        def __init__(self, session_id: str) -> None:
            super().__init__()
            self.session_id = session_id

    #: Width and the hidden default live HERE rather than in
    #: doxa/theme.tcss because both are DERIVED numbers
    #: (:data:`doxa.layout.SIDEBAR_WIDTH`) and a stylesheet cannot hold a
    #: derivation -- the same reason ``SplitBox`` sets its per-node
    #: weights per instance and leaves only the type selector to the
    #: theme. Hidden is the resting state: the rail is opened by
    #: :meth:`doxa.app.DoxaApp.action_toggle_sidebar`, which is also where
    #: the width refusal is priced.
    DEFAULT_CSS = f"""
    SessionSidebar {{
        width: {layout_mod.SIDEBAR_WIDTH};
        height: 1fr;
        display: none;
    }}
    """

    def __init__(self) -> None:
        super().__init__(id="session-sidebar")
        self.can_focus = False
        #: Every line this rail has ever needed, in order -- the POOL.
        #: Lines are REUSED and surplus ones hidden; none is ever removed.
        #: See :meth:`set_rows`.
        self._pool: "list[SidebarLine]" = []
        #: session id -> the line showing it, so a mark change is a class
        #: write on one widget rather than a rebuild of the whole rail.
        #: The needs-input blink runs at 2 Hz per waiting session; a
        #: rebuild per blink would be exactly the busy-idle cost
        #: ``GitLine``'s docstring warns about, reintroduced in new
        #: chrome.
        self._lines: "dict[str, SidebarLine]" = {}
        #: Sessions whose mark ALSO feeds an ENTRY row's aggregate -- the
        #: members of a multi-tab pane group. A mark moving on one of
        #: these can change which member wins, and that is a structure
        #: change, so :meth:`apply_marks` refuses the in-place update and
        #: asks for the rebuild instead of quietly leaving the entry row
        #: reporting a state that has moved. Empty on the overwhelmingly
        #: common window (every pane holds one tab), which is why the
        #: in-place path is still the one that runs per blink.
        self._aggregated: "set[str]" = set()
        self._rows: "list[Row]" = []
        #: Is the right edge being dragged right now? See
        #: :meth:`on_mouse_down`.
        self._dragging = False
        #: Is the pointer ON the divider (or holding it)? Presentation
        #: only -- see :meth:`_write_edge_paint`.
        self._edge_hot = False
        #: The divider's RESTING ``(edge type, colour)``, read off the
        #: stylesheet the first time it is painted hot and kept, because
        #: after that first write the inline rule is what reads back.
        #: Cached for the same reason :func:`doxa.triage.heading_paint`
        #: caches: this is a derivation, and re-deriving it per mouse-move
        #: is the +22% the rail already measured once.
        self._edge_rest: "tuple[str, str] | None" = None
        #: The last width a drag actually POSTED. A drag that has not
        #: crossed a column boundary has nothing to say -- see
        #: :meth:`on_mouse_move`.
        self._dragged_width: "int | None" = None

    # -- contents -----------------------------------------------------

    def set_rows(self, rows: "list[Row]") -> None:
        """Show these rows. A no-op when nothing changed, which is the
        common case: :meth:`doxa.app.DoxaApp.refresh_sidebar` runs from
        ``_persist_tabset`` on every tab lifecycle event and from
        ``on_resize`` on every terminal resize.

        **Lines are REUSED, never replaced, and nothing is ever removed.**
        A surplus line is hidden; a shortfall mounts more.

        That is not a micro-optimisation, it is the fix for a measured
        defect. The first version rebuilt with ``remove_children`` then
        ``mount_all``, which detaches widgets asynchronously -- and
        ``Pilot._wait_for_screen``, which every ``pilot.click`` and
        ``pilot.pause`` runs, snapshots the child list and waits for a
        ``call_later`` on each of them. A child removed inside that window
        never answers, and the wait times out: reproduced as
        ``WaitForScreenTimeout`` on a click that toggled a collection,
        intermittently and only under the whole test file. Reusing the
        lines removes the failure mode instead of timing around it, and it
        also means a click always lands on a widget that is still there --
        which is a property the user gets too, not only the suite.

        The hidden surplus is bounded by the most rows this rail has ever
        shown, i.e. by the number of sessions in one window."""
        if not self.is_mounted:
            # Nothing to mount into yet, and nothing remembered either:
            # the app refreshes the rail again once it is up, and a list
            # cached against a rail that never received it would make
            # THAT call a no-op.
            self._rows = []
            return
        if rows == self._rows:
            return
        if len(rows) > len(self._pool):
            extra = [SidebarLine(row) for row in rows[len(self._pool):]]
            self._pool.extend(extra)
            # Not awaited, and safe not to be: a MOUNT leaves the existing
            # children alone, so nothing a pending wait is counting can
            # disappear underneath it. That is exactly what a removal
            # could not promise.
            self.mount_all(extra)
        for index, line in enumerate(self._pool):
            if index < len(rows):
                line.set_row(rows[index])
                line.styles.display = "block"
            else:
                line.styles.display = "none"
        # A SINGLE-TAB group's heading is indexed too, and that is
        # load-bearing rather than tidy: since v1.5.0 the ordinary window
        # -- one group, one tab -- renders as an ENTRY row and no session
        # row at all, so keying this map on Row.SESSION alone would make
        # ``apply_marks`` miss every row on it and fall back to a full
        # rebuild per blink of the needs-input timer. Only the single-tab
        # case: with two tabs the row's state is an AGGREGATE, a mark
        # moving can change which member wins, and that is a structure
        # change ``_aggregated`` below routes to the rebuild on purpose.
        self._lines = {
            row.session_id: self._pool[index]
            for index, row in enumerate(rows)
            if row.session_id and (
                row.kind == Row.SESSION
                or (row.kind == Row.ENTRY and row.count <= 1)
            )
        }
        self._aggregated = set()
        depth = -1
        for row in rows:
            if row.kind == Row.ENTRY:
                # A single-tab group's heading is not an aggregate of
                # anything -- it IS its one member -- so it opens no
                # aggregated span. Indexed for the in-place mark path
                # above instead.
                depth = row.indent if row.count > 1 else -1
                continue
            if row.kind == Row.SESSION and depth >= 0 and row.indent > depth:
                self._aggregated.add(row.session_id)
                continue
            depth = -1
        self._rows = list(rows)

    def lines(self) -> "list[SidebarLine]":
        """The lines currently SHOWING, in order -- never the hidden
        surplus, which is mounted but is not part of the rail's
        contents."""
        return self._pool[: len(self._rows)]

    def apply_marks(
        self,
        session_id: str,
        marks: "Sequence[str]",
        ctx_percentage: "float | None" = None,
    ) -> bool:
        """Update ONE row's marks in place. Returns whether a row for
        that session was on the rail at all -- False is the caller's cue
        that the structure moved and a rebuild is due.

        Keyed by the session the ROW is reporting, which since v1.2.0 is
        the pane entry's most urgent member and not necessarily its
        visible tab. A mark moving on a member that is NOT the winner can
        change which member wins, and that is a structure change: it is
        answered by the False return and the rebuild behind it, not by a
        second aggregation here."""
        if session_id in self._aggregated:
            return False
        line = self._lines.get(session_id)
        if line is None or not line.is_mounted:
            return False
        line.apply_marks(marks, ctx_percentage)
        return True

    def rows(self) -> "list[Row]":
        return list(self._rows)

    # -- chrome -------------------------------------------------------

    def set_width(self, width: int) -> None:
        self.styles.width = layout_mod.clamp_sidebar_width(width)

    # -- the moveable divider (v1.5.0) --------------------------------
    #
    # The rail's right EDGE is a divider like the two this app already
    # has, and it behaves like them: draggable with the mouse AND
    # adjustable from the keyboard (``Alt+Shift+←/→``, ``/sidebar width``).
    # A mouse-only control is unreachable for a keyboard user, which this
    # project has ruled on twice -- and the keyboard half is also the half
    # that still works over ssh into a terminal with no mouse reporting.
    #
    # The WIDGET only reports the gesture. Whether a width is allowed is
    # doxa.layout.sidebar_refusal's answer and DoxaApp's to ask, exactly as
    # it is when the rail OPENS: a drag that could produce an arrangement
    # F3 refuses to create would be a second, looser floor.

    #: How wide the grab area on the right edge is. One column, which is
    #: the border the stylesheet already draws there -- a wider zone would
    #: be a strip of rail that swallows clicks meant for the rows under it.
    GRAB_COLUMNS = 1

    class WidthDragged(Message):
        """The right edge was dragged to this width in columns.

        ``final`` is the mouse BUTTON coming up. Every move posts a
        message so the rail tracks the pointer, and only the last of them
        is written to the settings registry: a config write per mouse-move
        event would be a file rewrite per cell crossed."""

        def __init__(self, width: int, final: bool = False) -> None:
            super().__init__()
            self.width = int(width)
            self.final = bool(final)

        def can_replace(self, message: Message) -> bool:
            """A queued drag position may be dropped for a newer one.

            Textual's own mechanism rather than a new one:
            ``textual.events.Resize`` declares exactly this, for exactly
            this reason (``textual/events.py:146``), and
            ``MessagePump._process_messages_loop`` peeks the queue and
            collapses a run of replaceable messages before dispatching
            (``textual/message_pump.py:633``). A drag position with a
            newer one already behind it names a rectangle nobody will ever
            see -- by the time it could be painted the pointer has left
            it.

            **What this is worth, honestly** -- see
            :meth:`SessionSidebar.on_mouse_move` for the measurement it
            shares. It removes work the rail was asking for and did not
            need. It does NOT make the drag visibly faster, because
            Textual was already collapsing the repaints those extra
            messages would have caused. The lag the owner reported is a
            repaint cost, not a message cost, and it is not fixed here.

            ``final`` is never dropped. It is the one message that WRITES
            (``resize_sidebar(persist=True)``), and a drag whose last
            event was swallowed would leave the width on screen and not on
            disk -- the disagreement between the painted rail and
            ``sidebar_width()`` that v1.5.0 already had to fix once."""
            return isinstance(message, SessionSidebar.WidthDragged) and (
                not self.final
            )

    def _edge_grabbed(self, x: int) -> bool:
        """Is this x -- measured in THIS widget's own columns -- on the
        divider rather than on a row?"""
        width = int(self.outer_size.width or 0)
        return width > 0 and x >= width - self.GRAB_COLUMNS

    def _edge_under(self, event: "events.MouseEvent") -> bool:
        """Is the POINTER on the divider?

        Screen coordinates rebased onto this widget, not ``event.x``,
        because a mouse event that started on a row arrives here by
        BUBBLING and keeps the row's coordinates: a row is inset by the
        rail's padding and never reaches the edge column, so reading
        ``event.x`` would answer "not the divider" for a reason that has
        nothing to do with where the pointer is. :meth:`_width_at` already
        measures in screen columns for the neighbouring reason."""
        width = int(self.outer_size.width or 0)
        if width <= 0:
            return False
        return self._edge_grabbed(int(event.screen_x) - int(self.region.x))

    def _write_edge_paint(self, hot: bool) -> None:
        """Light the divider, or put it back.

        **The affordance, and the whole of it in this terminal.** A GUI
        would say "draggable" with the pointer -- a west-east resize
        cursor over the edge -- and DOXA cannot: see this method's note in
        docs/manual.md. So the edge says it itself, by inverting: at rest
        it is the ground a heading wears, and under the pointer it is the
        colour that ground computes as its own maximum contrast.

        **Computed, never chosen**, and by the same function a heading's
        text is (:func:`doxa.triage.contrast_text`). The resting colour is
        read off the stylesheet rather than restated here, so re-hueing
        the rail in doxa/theme.tcss moves the hot colour with it instead
        of stranding a second hex that no longer contrasts against the
        first -- v1.2.0's colours-by-name rule, applied to the one surface
        that has no name to resolve.

        Written INLINE for the reason :meth:`SidebarLine._write_heading_paint`
        is: a stylesheet cannot hold a derivation. It is presentation and
        nothing else -- no rebuild, no refresh, no row touched -- which is
        the constraint docs/plans/rail-interaction.md names first about
        hover, and it holds here for the same reason: the rail refreshes
        on every mark change, and a list that rebuilt under the pointer
        would fight the gesture."""
        if hot == self._edge_hot:
            return
        self._edge_hot = hot
        with contextlib.suppress(Exception):
            if not hot:
                self.styles.clear_rule("border_right")
                return
            if self._edge_rest is None:
                edge, colour = self.styles.border_right
                self._edge_rest = (edge or "solid", str(colour.hex))
            edge, colour = self._edge_rest
            self.styles.border_right = (edge, triage_mod.contrast_text(colour))

    def edge_hot(self) -> bool:
        """Is the divider currently lit? Presentation state, exposed
        because it is what a test can read instead of a colour."""
        return self._edge_hot

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if not self._edge_under(event):
            return
        event.stop()
        self._dragging = True
        self._dragged_width = None
        self._write_edge_paint(True)
        # Capture, so the pointer can leave the rail -- which it must,
        # because dragging the edge RIGHT means the pointer is over the
        # panes for the whole gesture.
        with contextlib.suppress(Exception):
            self.capture_mouse()

    def _post_width(self, width: int, *, final: bool = False) -> None:
        """Ask for this width, and remember that we did."""
        self._dragged_width = width
        self.post_message(self.WidthDragged(width, final=final))

    def on_mouse_move(self, event: events.MouseMove) -> None:
        """Track the pointer -- and ask for as little as tracking needs.

        **The measurement behind everything below**, because the owner
        reported *"also moving the divider is laggy"* and a fix without
        one is not verified.

        One APPLIED width change costs about 138 ms on the reference
        window (160x48, seven panes, two groups): 2.1 full-screen layout
        passes at ~45 ms each, measured over ten clean changes. A
        cProfile of a twenty-move drag puts none of it in DOXA -- the top
        forty-five frames by cumulative time are all Textual's compositor
        (``render_update``, ``_render_chops``, ``Strip.divide``),
        re-rendering all ~49 widgets because the window really did change
        shape. Nothing in this file can make that cheaper.

        What this file CAN do is stop asking for changes that change
        nothing. A 125 Hz mouse reports about three times per column
        crossed, because a hand moving right also moves up and down:
        v1.5.0 posted all of them, and a twelve-column drag became 38
        width changes -- 25 of them naming a width the rail already had,
        each paying for a refusal check and a settings-registry decision
        to arrive back where it started. It is 13 now (one per column,
        plus the write), measured before and after on the same gesture.

        **And the honest half**: end to end, on that same gesture, this
        does not reduce wall-clock settle time. Textual coalesces its own
        repaints, so the 25 messages that no longer happen were not
        costing 25 repaints. The redundant work is gone; the ~138 ms per
        genuine column crossing is not, and a drag across a wide rail on
        a busy window will still trail the pointer. The only lever left
        would be to stop resizing DURING the gesture -- draw a guide and
        commit on release -- which trades away the live preview and the
        floor refusal you can currently see happen, so it is the owner's
        call and not a change this fix makes on its own."""
        if not self._dragging:
            # Not a drag: the only question is whether the pointer is on
            # the divider, and the answer is a colour.
            self._write_edge_paint(self._edge_under(event))
            return
        event.stop()
        # A pointer position with a NEWER one already queued behind it is
        # a position the pointer has left. Answering it costs a full
        # re-layout of the window to draw a rectangle that is superseded
        # before it reaches the screen -- and Textual's layout is
        # synchronous, so the time spent drawing it is time in which the
        # queue grows further. This is ``can_replace`` applied by hand,
        # for the one message class that does not declare it:
        # ``events.MouseMove`` inherits ``Message.can_replace`` -> False,
        # so the pump has no licence to collapse a run of them and the
        # rail has to notice for itself.
        with contextlib.suppress(Exception):
            if isinstance(self._peek_message(), events.MouseMove):
                return
        width = self._width_at(event)
        # A drag that has not crossed a column boundary has NOTHING to
        # say. A hand moving right also moves up and down, and a 125 Hz
        # mouse reports about three times per column crossed, so two
        # reports in three name a width the rail already has. Posting them
        # anyway cost a message, a refusal check and a settings-registry
        # decision per report, to arrive at the width already on screen.
        if width == self._dragged_width:
            return
        self._post_width(width)

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if not self._dragging:
            return
        event.stop()
        self._dragging = False
        with contextlib.suppress(Exception):
            self.release_mouse()
        # Released, so the pointer decides again. After a widening drag it
        # is standing on the edge it just placed, so the divider usually
        # stays lit -- which is the right answer, not a leftover: the thing
        # under the pointer really is the divider, and moving off it puts
        # it out.
        self._write_edge_paint(self._edge_under(event))
        # Unconditional: this is the width the user CHOSE and the only one
        # written to the settings registry, so it is posted whatever was
        # dropped on the way here -- and ``WidthDragged.can_replace`` will
        # not drop it either.
        self._post_width(self._width_at(event), final=True)
        self._dragged_width = None

    def on_leave(self, event: events.Leave) -> None:
        """The pointer left the rail. Put the divider back -- unless it is
        being HELD, which is the one case where the pointer being outside
        the rail is the gesture working rather than ending."""
        if not self._dragging:
            self._write_edge_paint(False)

    def _width_at(self, event: "events.MouseEvent") -> int:
        """The width the rail would have if its edge were under the
        pointer. Screen coordinates, not widget-relative: the widget's own
        box is the thing being resized, so measuring inside it would make
        the number chase itself."""
        left = int(self.region.x)
        return max(1, int(event.screen_x) - left + 1)

    def on_show(self) -> None:
        """The rail just got geometry. Ask the app to rebuild it: rows
        mounted while ``display: none`` were laid out against a zero box,
        and anything that changed while it was off has not been drawn
        into it. Duck-typed and suppressed for the harnesses that mount
        this widget without a ``DoxaApp`` around it."""
        refresh = getattr(self.app, "refresh_sidebar", None)
        if callable(refresh):
            try:
                refresh(force=True)
            except Exception:  # noqa: BLE001 -- chrome never costs a session
                return

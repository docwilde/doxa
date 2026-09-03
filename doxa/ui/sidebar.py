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

**A rail is not a tab strip**, and :func:`build_rows` is where that is
true rather than merely claimed: a row is built for every session the
CALLER knows about, and the caller knows about sessions that are not
mounted in any group -- a detached peer, an archived transcript, a
collection member whose tab was closed. Such a row is marked
``mounted=False``, renders dimmed with a note, and answers a click by
saying so instead of pretending it can be focused.
"""

from __future__ import annotations

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
    #: How deep this row sits: 0 at the rail's own margin, 1 under a
    #: heading (a collection's or a project's), 2 under an ENTRY row. An
    #: int rather than a flag because there are now three levels and a
    #: second bool would have to be kept consistent with the first.
    indent: int = 0

    HEADING = "heading"
    SESSION = "session"
    #: A PANE GROUP's own line, drawn above its member rows and only when
    #: it has more than one of them (hide at zero: a one-tab pane and its
    #: single row are the same thing said twice). It carries the
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

    # One config read and one hash per PROJECT, not per row: a window is
    # far more rows than it is projects, and this runs on a paint path.
    colours: "dict[str, str]" = {}

    def colour_of(repo_root: str) -> str:
        if repo_root not in colours:
            colours[repo_root] = triage_mod.colour_for(
                repo_root, config_mod.project_colour(repo_root)
            )
        return colours[repo_root]

    def session_row(session_id: str, collection: str, indent: int) -> "Row | None":
        fact = facts.get(session_id)
        if fact is None or not fact.label:
            return None
        return Row(
            Row.SESSION,
            ellipsize(fact.label, label_room),
            session_id=session_id,
            collection=collection,
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
        """One pane group's lines: its ENTRY row when it holds more than
        one tab, then a row per member.

        **Both, and that is the decision.** The spec left it open whether
        an entry expands to its tabs or shows only an aggregate with a
        count, and an aggregate ALONE would have cost the rail the
        property v1.0.0 built it for -- that every session this window
        knows about has a row of its own, reachable with one click. So the
        members stay, and the entry row is added ABOVE them: the aggregate
        is what a hidden tab's state needs to reach, and the members are
        what a click needs to land on.

        Hidden at zero, like every other piece of chrome in this app: a
        one-tab pane gets NO entry row, because an entry row over a single
        member is the same sentence twice, and such a window renders
        exactly as it did in v1.0.0."""
        state = states.get(key)
        if state is None:
            return []
        entry = by_key[key]
        out: "list[Row]" = []
        if state.count > 1:
            visible = facts.get(entry.active)
            out.append(
                Row(
                    Row.ENTRY,
                    ellipsize(
                        (visible.label if visible else state.label),
                        label_room,
                    ),
                    session_id=state.session_id,
                    collection=collection,
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
            )
        member_indent = indent + 1 if state.count > 1 else indent
        for session_id in entry.sessions:
            row = session_row(session_id, collection, member_indent)
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
        self.set_class(row.kind == Row.ENTRY, "-entry")
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
        return f"{glyphs} {row.text}{chip}{tail}"

    def on_click(self, event: events.Click) -> None:
        """A click on an ENTRY row goes to the member its state came
        from, not to the pane's visible tab: the row is reporting that
        member, its count chip names it, and a click that landed anywhere
        else would be the rail pointing at one thing and delivering
        another."""
        event.stop()
        if self.row.kind == Row.HEADING:
            if self.row.collection:
                self.post_message(SessionSidebar.CollectionToggled(
                    self.row.collection
                ))
            return
        if self.row.session_id:
            self.post_message(SessionSidebar.Revealed(self.row.session_id))


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
        self._lines = {
            row.session_id: self._pool[index]
            for index, row in enumerate(rows)
            if row.kind == Row.SESSION and row.session_id
        }
        self._aggregated = set()
        depth = -1
        for row in rows:
            if row.kind == Row.ENTRY:
                depth = row.indent
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

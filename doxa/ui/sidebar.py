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
from .. import layout as layout_mod
from .labels import ellipsize, sidebar_mark_glyph

#: The implicit heading loose sessions live under: unnamed, always last,
#: and NOT a collection. It is derived at render time and never written to
#: the record -- see :mod:`doxa.collections`' docstring for why persisting
#: it would make it a collection with a reserved name and give every edit
#: function a case to carry.
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
    """One line of the rail: a collection heading, or a session.

    ``collection`` is the heading's own name on a heading row and the
    holding collection's name on a session row (empty for a loose
    session), so a click on either knows what it is about without the
    widget keeping an index beside the list."""

    kind: str
    text: str
    session_id: str = ""
    collection: str = ""
    collapsed: bool = False
    marks: "tuple[str, ...]" = field(default=())
    mounted: bool = True

    HEADING = "heading"
    SESSION = "session"


def build_rows(
    items: "Sequence[collections_mod.Collection]",
    order: "Sequence[str]",
    describe: "Callable[[str], tuple[str, tuple[str, ...], bool]]",
    *,
    width: int = layout_mod.SIDEBAR_WIDTH,
) -> "list[Row]":
    """The rail's contents, top to bottom.

    ``order`` is every session the rail knows about, in the order the
    LOOSE ones should appear -- the caller passes strip order, so an
    ungrouped session sits where the tab bar has it. Collection members
    keep their COLLECTION's order instead, which is the user's and is the
    whole reason a collection stores a list rather than a set.

    ``describe`` answers, for one session id, ``(label, marks, mounted)``
    -- the app's job, because a label is ``display_name()`` off a live
    pane and a mark is that pane's own ``has_mark``, and neither belongs
    in a module that must stay importable without a screen.

    Collections come first in their own order, the implicit heading last.
    A collection with no members at all still gets its heading: it is a
    thing the user made two seconds ago with ``/collection new`` and is
    about to move a session into, and a heading that appeared only once it
    was non-empty would read as the command having failed."""
    label_room = max(4, int(width) - layout_mod.SIDEBAR_CHROME)
    rows: "list[Row]" = []

    def session_row(session_id: str, collection: str) -> "Row | None":
        label, marks, mounted = describe(session_id)
        if not label:
            return None
        text = ellipsize(label, label_room)
        return Row(
            Row.SESSION, text, session_id=session_id, collection=collection,
            marks=tuple(marks), mounted=bool(mounted),
        )

    known = {s for s in order if s}
    for item in items:
        rows.append(
            Row(
                Row.HEADING, ellipsize(item.name, max(4, int(width) - 4)),
                collection=item.name, collapsed=item.collapsed,
            )
        )
        if item.collapsed:
            continue
        for session_id in item.sessions:
            if session_id not in known:
                # Dropped for the same reason doxa.layout.prune drops a
                # dead leaf: the record names sessions, and one this
                # window has never heard of is not a row it can describe.
                continue
            row = session_row(session_id, item.name)
            if row is not None:
                rows.append(row)
    stray = collections_mod.loose(items, order)
    if stray:
        # The implicit heading is drawn only when it has something under
        # it, and it is drawn even when it is the ONLY heading -- a rail
        # with no collections at all is a flat list of sessions, and a
        # header over a flat list is chrome that answers nothing. So:
        # only when at least one real collection exists.
        if items:
            rows.append(Row(Row.HEADING, LOOSE_HEADING, collection=""))
        for session_id in stray:
            row = session_row(session_id, "")
            if row is not None:
                rows.append(row)
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
        self.row = row
        self.can_focus = False
        self.set_row(row)

    def set_row(self, row: Row) -> None:
        """Become that row -- IN PLACE.

        A line is reused rather than replaced (see
        :meth:`SessionSidebar.set_rows`), so this is the only path by which
        a row's identity changes and it must leave nothing of the previous
        one behind: every class it can carry is written on every call,
        true or false."""
        self.row = row
        self.set_class(row.kind == Row.HEADING, "-heading")
        self.set_class(row.kind == Row.SESSION, "-session")
        self.set_class(
            bool(row.collection) and row.kind == Row.SESSION, "-in-collection"
        )
        self.set_class(row.kind == Row.SESSION and not row.mounted, "-closed")
        self._write_marks(row.marks)
        self.update(self._text())

    def apply_marks(self, marks: "Sequence[str]") -> None:
        """Update ONLY the status marks, without rebuilding the row.

        What :meth:`doxa.app.DoxaApp.refresh_sidebar_marks` calls when a
        pane's marks move -- including once per blink of the needs-input
        timer, which is why it must not cost a row rebuild."""
        self.row = Row(
            self.row.kind, self.row.text, self.row.session_id,
            self.row.collection, self.row.collapsed, tuple(marks or ()),
            self.row.mounted,
        )
        self._write_marks(self.row.marks)
        self.update(self._text())

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
        row = self.row
        if row.kind == Row.HEADING:
            if not row.collection:
                return row.text
            return f"{FOLD_SHUT if row.collapsed else FOLD_OPEN} {row.text}"
        glyph = sidebar_mark_glyph(row.marks)
        tail = f" {NOT_OPEN}" if not row.mounted else ""
        return f"{glyph} {row.text}{tail}"

    def on_click(self, event: events.Click) -> None:
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
        self._rows = list(rows)

    def lines(self) -> "list[SidebarLine]":
        """The lines currently SHOWING, in order -- never the hidden
        surplus, which is mounted but is not part of the rail's
        contents."""
        return self._pool[: len(self._rows)]

    def apply_marks(self, session_id: str, marks: "Sequence[str]") -> bool:
        """Update ONE row's marks in place. Returns whether a row for
        that session was on the rail at all -- False is the caller's cue
        that the structure moved and a rebuild is due."""
        line = self._lines.get(session_id)
        if line is None or not line.is_mounted:
            return False
        line.apply_marks(marks)
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

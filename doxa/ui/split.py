# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.ui.split -- the widget half of recursive split panes.

Three widgets, and the builder that turns a :mod:`doxa.layout` tree into
them:

* :class:`SplitBox` -- one node of the tree. Unused (one child) it is a
  transparent container; used (two or more) it lays its children out
  along its orientation with proportional ``fr`` weights.
* :class:`PaneGroup` -- the LEAF, since v0.97.0: one region of the window,
  owning its own ``TabbedContent`` and therefore its own tab strip. This
  is the inversion the pane-groups spec asks for -- the window holds one
  tree of groups, and each group holds tabs, rather than the window
  holding tabs and each tab holding a tree.
* :class:`PaneTab` -- the TAB, and now what a group's tab STRIP holds
  rather than the root of a tree. Through v0.95.0 a ``PaneTab`` owned a
  whole ``SplitBox`` tree; it now holds exactly one surface (a
  ``SessionPane`` or a v0.92.0 ``DiffPane``), which is what it held
  through v0.88.0 -- the tree moved up a level, to the window.

**Why the group is a widget and not a bare ``TabbedContent``.** The strip
has to be able to hide itself below a measured width
(:data:`doxa.layout.GROUP_STRIP_MIN_COLS`) and to paint its own number
overlay, and both are per-region facts that a ``TabbedContent`` has no
place to hold. The group is also the thing a split divides and the thing
``Ctrl+1``..``Ctrl+9`` names, so it needs an identity of its own.

**Why the empty boxes.** Textual 5.3 cannot re-parent a mounted widget --
mounting an already-mounted widget is a silent no-op that orphans it
(measured against 5.3.0 before this was designed, not assumed). A split
therefore cannot WRAP a pane that already exists; the box it will need
has to be on screen before the user asks. Every leaf is born inside
:data:`doxa.layout.SPLIT_SLOTS` empty boxes, and splitting a pane
consumes the OUTERMOST one that is still unused -- outermost first,
because each successive split of the same pane must subdivide that pane's
own shrinking rectangle rather than the rectangle of everything beside
it. Run inward, that gives a 2x2 grid from two splits of one pane and its
neighbour, which is exactly the case the spec's directional-focus test is
written against.

The visible consequence, and it is a real one: every split node has the
pane that was split as its FIRST child. That is a constraint on the
shapes reachable through the UI, not on the shapes the model can hold --
:func:`doxa.layout.from_json` will read any tree, and
:func:`doxa.layout.rebuild_slots` puts a read tree back into the same
widget shape the gesture would have made.
"""

from __future__ import annotations

import contextlib
from typing import Any, Callable

from textual.app import ComposeResult
from textual.containers import Container
from textual.widgets import Static, TabbedContent, TabPane

from .. import layout as layout_mod


class SplitBox(Container):
    """One node of a tab's layout tree.

    ``orientation`` is ``None`` until the box is used, which is the state
    every box is born in: a container with one child and nothing to
    divide. The CSS type selector is all this needs from
    ``doxa/theme.tcss`` (``height: 1fr; width: 1fr``); everything about
    the division itself is set here, per instance, because it changes at
    runtime and a stylesheet cannot hold per-node weights."""

    DEFAULT_CSS = """
    SplitBox {
        layout: vertical;
        height: 1fr;
        width: 1fr;
    }
    """

    def __init__(self, *children: Any, orientation: "str | None" = None) -> None:
        super().__init__(*children)
        self.orientation: "str | None" = orientation
        #: One weight per child, summing to 1.0 -- see
        #: :func:`doxa.layout.normalise`. Empty while unused.
        self.weights: "tuple[float, ...]" = ()

    @property
    def is_used(self) -> bool:
        """Has this box actually been divided? An unused box has one child
        and no orientation, and reads as transparent everywhere the tree
        is walked."""
        return self.orientation is not None and len(self.children) > 1

    def on_mount(self) -> None:
        self._apply()

    #: Smallest proportional share a child of a split may hold.
    MIN_WEIGHT = 0.15

    def divide(self, orientation: str) -> None:
        """Take this box from unused to used (or record one more child on
        an already-used one) and re-weight it evenly. The new child must
        already be MOUNTED -- this reads ``self.children``, so a caller
        awaits the mount and then calls this once."""
        self.orientation = orientation
        self.weights = layout_mod.normalise((), max(len(self.children), 1))
        self._apply()

    def set_weights(self, weights: "tuple[float, ...]") -> None:
        self.weights = layout_mod.normalise(weights, len(self.children))
        self._apply()

    def nudge(self, index: int, delta: float) -> bool:
        """Move the divider AFTER child ``index`` by ``delta`` of the
        box's own extent, taking it from the next child. Returns whether
        anything moved -- ``False`` at the floor, which is what lets a
        keyboard handler stay silent rather than pretend.

        The floor is a fraction rather than a cell count on purpose: this
        node does not know how many cells it has been given, and a divider
        checked only against the CURRENT terminal size would let a drag on
        a wide monitor persist a ratio that is a sliver on a laptop.
        :data:`MIN_WEIGHT` is the smallest share a pane may hold anywhere;
        the CELL floors (:data:`doxa.layout.MIN_LEAF_WIDTH` /
        :data:`~doxa.layout.MIN_LEAF_HEIGHT`) are enforced where cells are
        actually known -- at split time."""
        if not self.is_used:
            return False
        weights = list(layout_mod.normalise(self.weights, len(self.children)))
        if not (0 <= index < len(weights) - 1):
            return False
        room = (
            weights[index + 1] - self.MIN_WEIGHT if delta > 0
            else weights[index] - self.MIN_WEIGHT
        )
        move = min(abs(delta), max(0.0, room))
        if move <= 1e-9:
            return False
        step = move if delta > 0 else -move
        weights[index] += step
        weights[index + 1] -= step
        self.set_weights(tuple(weights))
        return True

    def _apply(self) -> None:
        """Write orientation and weights onto the children as styles.

        ``fr`` units, never cells: that is the whole of "sizes are
        proportional, never absolute" at the rendering end, and it is what
        makes a terminal resize preserve the ratio for free instead of
        needing a resize handler."""
        kids = list(self.children)
        if not kids:
            return
        if not self.is_used:
            self.styles.layout = "vertical"
            for kid in kids:
                kid.styles.width = "1fr"
                kid.styles.height = "1fr"
            return
        weights = layout_mod.normalise(self.weights, len(kids))
        self.weights = weights
        row = self.orientation == layout_mod.ROW
        self.styles.layout = "horizontal" if row else "vertical"
        for kid, weight in zip(kids, weights):
            share = f"{max(1, round(weight * 1000))}fr"
            if row:
                kid.styles.width = share
                kid.styles.height = "1fr"
            else:
                kid.styles.height = share
                kid.styles.width = "1fr"

    # -- tree walking -------------------------------------------------

    def own_boxes(self) -> "list[SplitBox]":
        """This box and every unused box directly inside it, outermost
        first -- the chain a leaf is born inside. Stops at the first child
        that is not a lone SplitBox, which is the leaf itself or a
        division that has already happened."""
        chain = [self]
        node: SplitBox = self
        while True:
            kids = list(node.children)
            if len(kids) != 1 or not isinstance(kids[0], SplitBox):
                return chain
            node = kids[0]
            chain.append(node)

    def first_free(self) -> "SplitBox | None":
        """The OUTERMOST box in this chain that has not been divided yet
        -- where the next split of the leaf inside goes. ``None`` when the
        leaf has spent its whole allowance, which is the depth cap."""
        for box in self.own_boxes():
            if not box.is_used:
                return box
        return None


class GroupNumber(Static):
    """The brief ``Ctrl+<digit>`` overlay for ONE group: its own number,
    painted over its own region.

    One per group, mounted with the group and hidden, rather than one
    screen-level widget positioned from the rectangles: a widget inside the
    group IS the group's rectangle, so what is numbered and what is painted
    cannot disagree -- which is the property the spec asks for and the
    reason the numbering is derived from ``_pane_regions`` in the first
    place.

    Never on its own timer. :meth:`doxa.app.DoxaApp._flash_group_numbers`
    arms ONE ``set_timer`` for the whole flash and cancels it on the next
    key; see that method for why a one-shot timer is inside DOXA's no-timer
    rule and an interval is not."""

    DEFAULT_CSS = """
    GroupNumber {
        layer: groupnum;
        width: 100%;
        height: 100%;
        content-align: center middle;
        text-style: bold;
        display: none;
    }
    """

    def __init__(self) -> None:
        super().__init__("", classes="-group-number")


class PaneTab(TabPane):
    """One tab: a title in some group's strip, and exactly one surface.

    Three shapes in four releases, and this is the second one again.
    Through v0.88.0 ``SessionPane`` WAS the ``TabPane``. v0.91.0 made the
    tab a CONTAINER of a whole ``SplitBox`` tree, because two panes had to
    be visible at once and only a tab could hold them. v0.97.0 moves the
    tree up to the window, where the thing that is visible at once is a
    GROUP -- so a tab is back to holding one surface, and the surface it
    holds may be a ``SessionPane`` or (v0.92.0) a ``DiffPane``.

    That last clause is the pane-groups spec's own design check, answered
    by construction rather than by a special case: a group's tab list is a
    list of SURFACES, and a diff is a surface.

    What did NOT change across any of it: the tab keeps the id every caller
    in :mod:`doxa.app` already uses (``_restore_pane_id``'s
    ``restore-<session id>``, :data:`DoxaApp._FALLBACK_PANE_ID`), so
    activation, the tab strip, the rename field, the status classes and the
    persisted-set lookups all still key off the same strings.
    ``SessionPane`` reaches it through :attr:`SessionPane.tab`."""

    def __init__(self, title: Any, surface: Any, *, id: "str | None" = None) -> None:
        super().__init__(title, id=id)
        self._surface = surface
        #: Kept for the callers that ask a tab which leaf had the keyboard.
        #: A tab holds one surface now, so the answer is always that
        #: surface -- the attribute survives because ``_focus_tab`` and
        #: ``_persist_tabset`` both read it and the group above is where
        #: "which of several" is a real question again.
        self.focused_leaf: Any = surface

    def compose(self) -> ComposeResult:
        yield self._surface

    @property
    def surface(self) -> Any:
        return self._surface

    def leaves(self) -> "list[Any]":
        """This tab's SESSION surface, as a list of zero or one.

        Still a list, and still sessions-only, because every caller asks a
        question only a session can answer ("which session is this tab's",
        "which engine does this key mean") and none of them should have to
        learn that the answer became singular -- a diff tab correctly
        answers with an empty list, exactly as a diff LEAF did in
        v0.92.0."""
        from ..session.pane import SessionPane

        return list(self.query(SessionPane))

    def surfaces(self) -> "list[Any]":
        """Every surface in this tab, of any kind -- zero or one.

        A leaf qualifies by being able to name itself as one
        (``layout_leaf()``), which is the same test :func:`_node_of`
        applies when the tree is read off the widgets."""
        from ..session.pane import SessionPane

        return [
            node for node in self.query("*")
            if isinstance(node, SessionPane) or callable(
                getattr(node, "layout_leaf", None)
            )
        ]

    def layout_leaf(self) -> "layout_mod.Leaf | None":
        """This tab as one :mod:`doxa.layout` tab record, read off the
        widget rather than kept beside it -- the same one-source-of-truth
        rule ``PaneTab.tree()`` followed in v0.91.0, one level down."""
        from ..session.pane import SessionPane

        surface = self._surface
        if isinstance(surface, SessionPane):
            return _leaf_of(surface)
        own = getattr(surface, "layout_leaf", None)
        if callable(own):
            leaf = own()
            return leaf if isinstance(leaf, layout_mod.Leaf) else None
        return None


#: Every group's ``TabbedContent`` carries this class, and the stylesheet
#: selects on it. Through v0.95.0 the window had exactly one tab strip and
#: ``#session-tabs`` was an id; N groups means N strips, and an id cannot
#: be in two places. The FIRST group's strip keeps the literal id anyway
#: (see :func:`next_tabbed_id`) so that an unsplit window's DOM is byte for
#: byte the one every previous release produced.
STRIP_CLASS = "session-tabs"

_TABBED_SEQ = [0]


def reset_tabbed_ids() -> None:
    """Start the strip-id sequence over -- called once per
    :class:`doxa.app.DoxaApp`, so a suite that builds many apps in one
    process gets ``#session-tabs`` for each one's first group rather than
    a number that climbs across tests."""
    _TABBED_SEQ[0] = 0


def next_tabbed_id() -> str:
    """The next group strip's widget id. The first is ``session-tabs``
    exactly, and never reused afterwards even if that group closes: an id
    that moved between widgets would be a second answer to "which strip is
    this", which is the drift class this file's docstring keeps warning
    about."""
    _TABBED_SEQ[0] += 1
    return "session-tabs" if _TABBED_SEQ[0] == 1 else f"session-tabs-{_TABBED_SEQ[0]}"


class PaneGroup(Container):
    """One REGION of the window: its own tab strip, its own tabs, its own
    idea of which tab is active.

    This is what the pane-groups inversion makes a layout leaf. Everything
    about the tree above it -- :class:`SplitBox`, the dividers, the weights,
    directional focus, :func:`doxa.layout.neighbour` -- is untouched by the
    change, which is the whole argument for doing it as a re-rooting rather
    than a rebuild.

    **The tab strip hides itself when it cannot be read.** Two strips is
    more chrome than one, and a 40-column half of an 80-column terminal
    cannot show a tab label. :meth:`on_resize` puts the group on one of
    three rungs measured in :mod:`doxa.layout`
    (:data:`~doxa.layout.GROUP_STRIP_COMPACT_COLS`,
    :data:`~doxa.layout.GROUP_STRIP_MIN_COLS`) -- full, compact, gone --
    the same hide-at-zero discipline the context chip and the side-by-side
    diff already follow.

    **And when it has nothing to say.** A strip listing ONE tab is a row
    of chrome that answers a question nobody asked: there is nothing to
    switch to, nothing to compare against, and the tab's own label is
    already in this pane's status bar (:class:`doxa.ui.statusline.
    StatusBar`, which is per-pane and always visible). So a group holding
    exactly one tab wears ``-strip-hidden`` too, and gives the row to the
    transcript -- the same judgment the group-number overlay makes about a
    single-group window and the rail makes about a single session
    (:meth:`doxa.app.DoxaApp.sidebar_has_something_to_say`).

    The two conditions COMPOSE rather than fight, and that is why they are
    written as one OR in :meth:`_apply_strip_width_for` rather than as two
    writers of the same class: a 16-column group holding three tabs and a
    120-column group holding one are both hidden, and the second tab
    arriving in the narrow one must not un-hide it. ``-strip-compact``
    stays purely width-driven -- it says how a SHOWN strip renders, so it
    is already correct the instant the count half stops hiding it.

    No attention signal is lost to this. A pane that is visible but not
    focused paints its own marks on ITSELF since v0.89.0
    (``SessionPane.-done-unseen`` / ``-attention`` / ``-staged`` in
    ``doxa/theme.tcss``, written by the same
    :meth:`doxa.session.pane.SessionPane._set_tab_class` door that writes
    the tab header), and the status bar carries ``needs input`` and the
    staged count as chips of its own -- see the note on
    :meth:`refresh_strip`."""

    DEFAULT_CSS = """
    PaneGroup {
        layers: base groupnum;
        height: 1fr;
        width: 1fr;
    }
    """

    def __init__(
        self,
        *tabs: Any,
        active_id: "str | None" = None,
        id: "str | None" = None,
        tabbed_id: "str | None" = None,
    ) -> None:
        super().__init__(id=id)
        self._tabs = list(tabs)
        # "" is what TabbedContent means by "pick the first yourself", and
        # it is what an unrestored window has always passed.
        self._active_id = active_id or ""
        self._tabbed_id = tabbed_id or next_tabbed_id()
        #: Last width this group was actually MEASURED at, 0 until it has
        #: been painted once. Remembered because the two halves of
        #: ``-strip-hidden`` are asked at different moments: the tab count
        #: moves on a DOM event, which can land while the group has no
        #: geometry at all (Textual 5.3.0 gives a hidden or unpainted
        #: widget none -- the readiness rule ``SessionPane.
        #: scroll_transcript_to_end`` is written against). Recomputing the
        #: OR from a width of 0 would read as "not narrow" and silently
        #: un-hide a strip the rungs had hidden.
        self._strip_width = 0

    def compose(self) -> ComposeResult:
        with TabbedContent(
            id=self._tabbed_id, classes=STRIP_CLASS, initial=self._active_id
        ):
            for tab in self._tabs:
                yield tab
        yield GroupNumber()

    # -- the strip ----------------------------------------------------

    @property
    def tabbed(self) -> TabbedContent:
        """This group's own ``TabbedContent``. Raises ``NoMatches`` before
        :meth:`compose` has landed, which every caller either guards or
        deliberately lets propagate -- see
        :meth:`doxa.app.DoxaApp.tabbed_of`."""
        return self.query_one(TabbedContent)

    @property
    def entry_key(self) -> str:
        """This group's stable identity for the rail, which lists one
        entry per pane group (v1.2.0, Part 1b).

        The ``TabbedContent``'s id, because it is the one string that is
        already unique per group, already stable across a refresh, and
        already persisted-adjacent (:func:`next_tabbed_id`). Not
        ``self.id``, which is ``None`` for every group the interactive
        split gesture makes."""
        return self._tabbed_id

    def tabs(self) -> "list[Any]":
        """This group's tabs, in STRIP order -- every kind, so an archived
        tab and a subagent transcript count, because both sit right there
        in the strip and ``Ctrl+←/→`` must reach them.

        The ``parent`` check drops a tab whose ``remove_pane`` has been
        issued but whose detach has not landed: :meth:`doxa.app.DoxaApp.
        _close_group_tab` reads this list to decide whether a group has any
        tabs LEFT, and a half-removed one counted as remaining would leave
        an empty region behind."""
        return [kid for kid in self.query(TabPane) if kid.parent is not None]

    def active_tab(self) -> "Any | None":
        try:
            tabbed = self.tabbed
        except Exception:  # noqa: BLE001 -- not composed yet
            return None
        if not tabbed.is_mounted:
            return None
        try:
            return tabbed.active_pane
        except Exception:  # noqa: BLE001 -- active names no mounted tab yet
            return None

    def surfaces(self) -> "list[Any]":
        """The surfaces of this group's ACTIVE tab -- what is on screen in
        this region, which is never more than one thing.

        Deliberately not "every surface in the group": an inactive tab is
        mounted and running but is not painted, and a rectangle that is not
        painted is not a destination for directional focus. That is the
        same "painted, not structural" rule ``_pane_regions`` states."""
        tab = self.active_tab()
        return tab.surfaces() if hasattr(tab, "surfaces") else []

    def layout_group(self) -> "layout_mod.Group | None":
        """This group as a :mod:`doxa.layout` group, read off the widgets.

        One source of truth, the rule v0.91.0 wrote down for
        ``PaneTab.tree()`` and this inherits: a mirror of the tree kept in
        Python state is a second thing to keep in sync, and the persisted
        record is where that drift lands."""
        tabs: "list[layout_mod.Leaf]" = []
        active = 0
        active_tab = self.active_tab()
        for tab in self.tabs():
            own = getattr(tab, "layout_leaf", None)
            if not callable(own):
                continue
            leaf = own()
            if not isinstance(leaf, layout_mod.Leaf):
                continue
            if tab is active_tab:
                active = len(tabs)
            tabs.append(leaf)
        if not tabs:
            return None
        return layout_mod.Group(tuple(tabs), active)

    # -- chrome -------------------------------------------------------

    def on_resize(self) -> None:
        self._apply_strip_width()

    def on_mount(self) -> None:
        self._apply_strip_width()

    def holds_one_tab(self) -> bool:
        """Is there nothing in this group's strip worth painting a strip
        for? True at one tab and at none.

        None counts because a group mid-teardown (its last tab's
        ``remove_pane`` awaited, the group's own ``remove`` not yet) would
        otherwise flash its strip back on for the frames before it goes --
        and "hide" is the right answer for an empty strip on every reading
        of the rule.

        Counted through :meth:`tabs`, never through
        ``TabbedContent.tab_count``, so a half-removed tab is excluded by
        exactly the ``parent`` check ``_close_group_tab`` already relies
        on -- one derivation of "how many tabs are in here", not two."""
        return len(self.tabs()) <= 1

    def refresh_strip(self) -> bool:
        """Re-apply the strip's visibility after this group's TAB COUNT
        moved, and report whether the strip actually appeared or vanished.

        The return value is the whole reason this is not just a private
        call: showing the strip takes a row away from the transcript
        underneath, and a pane sitting at the tail of its transcript would
        be left one row short of it -- so
        :meth:`doxa.app.DoxaApp.refresh_strip_visibility` re-pins the tail
        of the panes that were AT it, and only when something moved.

        Called from the app, through two doors that between them see
        every count this window can reach
        (:meth:`doxa.app.DoxaApp.refresh_strip_visibility` names both),
        and never from a Textual message handled here: ``TabbedContent``
        posts nothing on ``add_pane``/``remove_pane`` that MEANS "the
        count changed" -- ``TabActivated`` is about an activation, which a
        removal of an inactive tab is not -- so a group that listened for
        itself would be right most of the time and wrong exactly where a
        tab closes in the background."""
        before = self.has_class("-strip-hidden")
        self._apply_strip_width()
        return self.has_class("-strip-hidden") != before

    def strip_should_hide(self) -> bool:
        """What ``-strip-hidden`` OUGHT to be right now, asked without
        writing anything.

        The cheap half of :meth:`refresh_strip`, so a caller that runs on
        a frequent event can find out for free that nothing is going to
        move and skip the expensive part -- which is not this class's at
        all: it is
        :meth:`doxa.app.DoxaApp._pinned_transcripts`, a query per leaf,
        and it is only worth paying when a strip is actually about to
        appear or vanish."""
        narrow = 0 < self._strip_width < layout_mod.GROUP_STRIP_MIN_COLS
        return narrow or self.holds_one_tab()

    def _apply_strip_width(self) -> None:
        width = self.size.width
        if width > 0:
            self._strip_width = width
        # NOT an early return at width 0 any more: the tab-count half of
        # the answer is a DOM fact and is true whether or not this group
        # has been painted, and it is at mount -- before the first resize
        # -- that a restored single-tab group has to come back already
        # hidden rather than flashing a strip for a frame.
        self._apply_strip_width_for(self._strip_width)

    def _apply_strip_width_for(self, width: int) -> None:
        """Put this group on its width rung AND on its tab-count rung. Two
        classes, and the stylesheet still holds all the rendering. Split
        from :meth:`_apply_strip_width` so the rungs can be measured
        against stated widths rather than against whatever rectangle a
        test terminal happens to produce.

        ``width`` of 0 means "never measured": the rungs are left exactly
        where they were and only the count is applied. That is the same
        readiness test ``SessionPane.scroll_transcript_to_end`` uses --
        ``size`` is the thing that is honest about a widget with no
        geometry, and a stale ``container_size`` is not."""
        if width > 0:
            self._strip_width = width
            self.set_class(
                layout_mod.GROUP_STRIP_MIN_COLS
                <= width
                < layout_mod.GROUP_STRIP_COMPACT_COLS,
                "-strip-compact",
            )
        # ONE writer of ``-strip-hidden``, over ONE statement of the OR
        # (:meth:`strip_should_hide`). Two writers of one boolean class is
        # the drift this file keeps warning about: whichever ran last would
        # clear the other's reason.
        self.set_class(self.strip_should_hide(), "-strip-hidden")

    # -- the number overlay -------------------------------------------

    def show_number(self, number: int) -> None:
        with contextlib.suppress(Exception):
            overlay = self.query_one(GroupNumber)
            if not overlay.is_mounted:
                return
            overlay.update(str(number))
            overlay.styles.display = "block"

    def hide_number(self) -> None:
        with contextlib.suppress(Exception):
            overlay = self.query_one(GroupNumber)
            if not overlay.is_mounted:
                return
            overlay.styles.display = "none"


def _node_of(kid: Any) -> "layout_mod.Node | None":
    """One child of a box as a tree node.

    A leaf answers for ITSELF, through a ``layout_group()`` method
    (:meth:`PaneGroup.layout_group` is the only one today). Duck-typed
    rather than an ``isinstance`` chain because this module deliberately
    does not import the widgets it lays out at module scope --
    ``SessionPane`` is imported inside functions to break the cycle, and a
    second such import for every new surface is a cycle waiting to be
    reintroduced.

    Returning ``None`` here is not harmless: a used box whose second child
    yields ``None`` collapses to one child, so :func:`tree_of` would report
    "no split" for a screen that plainly shows one, and the persisted
    record would carry that lie into the next launch."""
    if isinstance(kid, SplitBox):
        return _tree_of(kid)
    own = getattr(kid, "layout_group", None)
    if callable(own):
        group = own()
        return group if isinstance(group, layout_mod.Group) else None
    return None


def tree_of(box: "SplitBox | None") -> "layout_mod.Node | None":
    """The WINDOW's layout as a :mod:`doxa.layout` tree, read off the
    widgets. The public name; ``_tree_of`` stays as the recursive half."""
    return _tree_of(box) if isinstance(box, SplitBox) else None


def _tree_of(box: SplitBox) -> "layout_mod.Node | None":
    kids = list(box.children)
    if not kids:
        return None
    if not box.is_used:
        return _node_of(kids[0])  # an unused box is transparent
    children: "list[layout_mod.Node]" = []
    weights: "list[float]" = []
    for kid, weight in zip(kids, layout_mod.normalise(box.weights, len(kids))):
        node = _node_of(kid)
        if node is not None:
            children.append(node)
            weights.append(weight)
    if not children:
        return None
    if len(children) == 1:
        return children[0]
    return layout_mod.Split(
        box.orientation or layout_mod.COLUMN, tuple(children), tuple(weights)
    )


def _leaf_of(pane: Any) -> "layout_mod.Leaf | None":
    session_id = getattr(pane, "_session_id", "") or ""
    if not session_id:
        return None
    cwd = str(getattr(pane.engine, "cwd", None) or pane.cwd)
    return layout_mod.Leaf(
        session_id=session_id,
        pinned_name=pane.custom_name,
        cwd=cwd,
        prompt_ratio=layout_mod.clamp_prompt_ratio(getattr(pane, "prompt_ratio", 0.0)),
    )


def chain(leaf: Any, slots: int = layout_mod.SPLIT_SLOTS) -> SplitBox:
    """``slots`` empty boxes wrapped around one leaf, outermost returned.

    ``slots`` may be 0, in which case the leaf comes back bare -- that is
    what :func:`doxa.layout.rebuild_slots` asks for when the saved tree
    already spent that leaf's whole allowance."""
    node: Any = leaf
    for _ in range(max(0, slots)):
        node = SplitBox(node)
    return node


def build(
    node: "layout_mod.Node",
    make_group: "Callable[[layout_mod.Group], Any]",
    slots: int = layout_mod.SPLIT_SLOTS,
) -> Any:
    """A layout tree as unmounted widgets, in the same shape the
    interactive gesture would have produced -- see
    :func:`doxa.layout.rebuild_slots` for the arithmetic and the module
    docstring for why the shape has to match.

    ``make_group`` builds the :class:`PaneGroup` for one group; it is the
    app's job (a restored tab needs an engine factory, a pinned name and
    the transcript-restore flag, none of which belong in the widget
    layer). A bare ``Leaf`` reaching here -- a v0.91.0 tree that was not
    run through :func:`doxa.layout.groupify` first -- is read as the
    single-tab group it becomes, so the builder cannot be the place a
    migration is forgotten."""
    if not isinstance(node, layout_mod.Split):
        return chain(make_group(layout_mod.as_group(node)), slots)
    first = build(node.children[0], make_group, max(0, slots - 1))
    rest = [
        build(child, make_group, layout_mod.SPLIT_SLOTS)
        for child in node.children[1:]
    ]
    box = SplitBox(first, *rest, orientation=node.orientation)
    box.weights = layout_mod.normalise(node.weights, len(node.children))
    return box


def first_group(node: Any) -> "PaneGroup | None":
    """The first :class:`PaneGroup` inside an UNMOUNTED tree, in DOM order.

    Unmounted is the point: ``query`` walks the mounted DOM and answers
    nothing for a tree ``build`` has just returned. It walks
    ``_pending_children`` as well as ``children`` because Textual 5.3 puts
    constructor children in the FORMER until mount -- measured, and the
    reason a first attempt at this returned ``None`` for every tree
    ``build`` produced. What
    :meth:`doxa.app.DoxaApp._compose_restored_root` needs to hang the
    all-archived fallback pane on."""
    if isinstance(node, PaneGroup):
        return node
    kids = list(getattr(node, "children", ())) or list(
        getattr(node, "_pending_children", ())
    )
    for kid in kids:
        found = first_group(kid)
        if found is not None:
            return found
    return None


def group_of(widget: Any) -> "PaneGroup | None":
    """The :class:`PaneGroup` a widget sits in, or ``None``."""
    node = getattr(widget, "parent", None)
    while node is not None and not isinstance(node, PaneGroup):
        node = getattr(node, "parent", None)
    return node


def tabbed_of(widget: Any) -> "TabbedContent | None":
    """The tab strip a widget sits in -- a group's own, never the settings
    modal's, because the walk stops at the first ``TabbedContent`` above
    the widget and a widget only ever has one."""
    node = getattr(widget, "parent", None)
    while node is not None and not isinstance(node, TabbedContent):
        node = getattr(node, "parent", None)
    return node


def owning_box(pane: Any) -> "SplitBox | None":
    """The innermost :class:`SplitBox` a pane sits in."""
    node = pane.parent
    while node is not None and not isinstance(node, SplitBox):
        node = node.parent
    return node


def outer_chain(pane: Any) -> "list[SplitBox]":
    """A pane's OWN box chain, outermost first: the boxes that exist to be
    spent on splitting THIS pane, and no others.

    Walks up from the pane and keeps every ancestor box whose FIRST child
    is on the path -- owner-first is what makes that test correct. The
    moment an ancestor holds the path as a later child, that box belongs
    to some other pane's chain and this one's allowance has run out."""
    chain_up: "list[SplitBox]" = []
    node: Any = pane
    parent = node.parent
    while isinstance(parent, SplitBox):
        kids = list(parent.children)
        if not kids or kids[0] is not node:
            break
        chain_up.append(parent)
        node = parent
        parent = node.parent
    chain_up.reverse()
    return chain_up


def free_box(pane: Any) -> "SplitBox | None":
    """Where this pane's NEXT split goes: the outermost box in its own
    chain that has not been divided. ``None`` means the depth cap."""
    for box in outer_chain(pane):
        if not box.is_used:
            return box
    return None


async def prune_boxes(box: "SplitBox | None") -> None:
    """After a leaf is removed, drop the boxes it left behind that now
    hold nothing, collapse any split down to one child back to unused, and
    re-weight what is left.

    Without this a closed pane leaves an empty container still claiming
    its ``fr`` share -- the split visibly collapses to the right shape but
    a strip of dead space stays where the pane was, which is the same
    class of defect as the invisible button v0.28.0 shipped for a whole
    release: every structural assertion passes and the screen is wrong.

    Awaits each removal rather than firing and forgetting, because the
    NEXT iteration reads the parent's child list and Textual's
    ``Widget.remove`` only takes effect when its ``AwaitRemove`` is
    awaited."""
    while box is not None:
        parent = box.parent
        parent_box = parent if isinstance(parent, SplitBox) else None
        if list(box.children):
            if len(box.children) < 2:
                # A split that lost all but one child is not a split any
                # more -- collapsing it here is the spec's "closing the
                # last pane in a split collapses the split", and it also
                # hands the box back as a FREE slot the survivor can be
                # split into again.
                box.orientation = None
                box.weights = ()
            box._apply()
        else:
            with contextlib.suppress(Exception):
                await box.remove()
        if parent_box is not None:
            parent_box._apply()
        box = parent_box

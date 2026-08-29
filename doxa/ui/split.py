# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.ui.split -- the widget half of recursive split panes.

Two widgets, and the builder that turns a :mod:`doxa.layout` tree into
them:

* :class:`SplitBox` -- one node of the tree. Unused (one child) it is a
  transparent container; used (two or more) it lays its children out
  along its orientation with proportional ``fr`` weights.
* :class:`PaneTab` -- the TAB. Since v0.91.0 a tab is a CONTAINER of
  leaves, not a leaf itself; ``SessionPane`` stopped being a ``TabPane``
  and became an ordinary container so that two of them can be visible at
  once inside one tab.

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
from textual.widgets import TabPane

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


class PaneTab(TabPane):
    """One tab, and the whole layout tree inside it.

    Through v0.88.0 ``SessionPane`` WAS the ``TabPane``: one tab, one
    session, and "which pane is active" was derivable from "which tab is
    showing". Splits break that equivalence -- two panes can be visible at
    once -- so the tab became a container and the session surface became
    an ordinary widget that a container can hold two of.

    What did NOT change: the tab keeps the id every caller in
    :mod:`doxa.app` already uses (``_restore_pane_id``'s
    ``restore-<session id>``, :data:`DoxaApp._FALLBACK_PANE_ID`), so
    activation, the tab strip, the rename field, the status classes and
    the persisted-set lookups all still key off the same strings.
    ``SessionPane`` reaches it through :attr:`SessionPane.tab`."""

    def __init__(self, title: Any, root: SplitBox, *, id: "str | None" = None) -> None:
        super().__init__(title, id=id)
        self._root = root
        #: The leaf that had the keyboard last time this tab held it.
        #: Read when focus is somewhere that is not a leaf at all (a modal,
        #: the command palette) and the app still has to name ONE pane --
        #: the status bar reflects the FOCUSED pane, so it needs an answer
        #: that survives a dialog rather than jumping to the first leaf.
        self.focused_leaf: Any = None

    def compose(self) -> ComposeResult:
        yield self._root

    @property
    def root_box(self) -> SplitBox:
        return self._root

    def leaves(self) -> "list[Any]":
        """Every session leaf in this tab, in DOM order (which is
        left-to-right / top-to-bottom, the same order
        :func:`doxa.layout.leaves` produces)."""
        from ..session.pane import SessionPane

        return list(self.query(SessionPane))

    def tree(self) -> "layout_mod.Node | None":
        """This tab's layout as a :mod:`doxa.layout` tree, read off the
        widgets rather than kept beside them. One source of truth: a
        mirror of the tree in Python state is a second thing to keep in
        sync, and the defect class that produces (the model says split,
        the screen says not) is precisely what the persisted record would
        then carry into the next launch."""
        return _tree_of(self._root)


def _tree_of(box: SplitBox) -> "layout_mod.Node | None":
    from ..session.pane import SessionPane

    kids = list(box.children)
    if not kids:
        return None
    if not box.is_used:
        kid = kids[0]
        if isinstance(kid, SplitBox):
            return _tree_of(kid)  # an unused box is transparent
        if isinstance(kid, SessionPane):
            return _leaf_of(kid)
        return None
    children: "list[layout_mod.Node]" = []
    weights: "list[float]" = []
    for kid, weight in zip(kids, layout_mod.normalise(box.weights, len(kids))):
        node = (
            _tree_of(kid) if isinstance(kid, SplitBox)
            else (_leaf_of(kid) if isinstance(kid, SessionPane) else None)
        )
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
    make_leaf: "Callable[[layout_mod.Leaf], Any]",
    slots: int = layout_mod.SPLIT_SLOTS,
) -> Any:
    """A layout tree as unmounted widgets, in the same shape the
    interactive gesture would have produced -- see
    :func:`doxa.layout.rebuild_slots` for the arithmetic and the module
    docstring for why the shape has to match.

    ``make_leaf`` builds the session surface for one leaf; it is the app's
    job (a restored leaf needs an engine factory, a pinned name and the
    transcript-restore flag, none of which belong in the widget layer)."""
    if not isinstance(node, layout_mod.Split):
        return chain(make_leaf(node), slots)
    first = build(node.children[0], make_leaf, max(0, slots - 1))
    rest = [
        build(child, make_leaf, layout_mod.SPLIT_SLOTS)
        for child in node.children[1:]
    ]
    box = SplitBox(first, *rest, orientation=node.orientation)
    box.weights = layout_mod.normalise(node.weights, len(node.children))
    return box


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

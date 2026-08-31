# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.layout -- the layout tree: the model half of recursive split panes.

Pure data and pure functions. No widget, no ``self``, no I/O -- the same
rule :mod:`doxa.ui.labels` follows, and for the same reason: the parts of
a layout that are hard to get right (what serialises, what a direction
key means in a 2x2, when a split is refused) are the parts that must be
testable without a running app.

**The shape**, from docs/plans/split-panes.md:

- a **leaf** holds exactly one pane -- what a tab holds today;
- a **split** has an orientation (``row``: children side by side;
  ``column``: children stacked), an ordered list of children (leaf or
  split, so the recursion is genuine) and per-child weights.

**The inversion** (v0.97.0). Through v0.95.0 each TAB owned one tree and
a leaf held one session. That is now upside down:

    v0.91.0   window -> tabs -> each tab owns a layout tree of panes
    v0.97.0   window -> one layout tree of GROUPS -> each group owns tabs

So there is exactly ONE tree per window, its leaf nodes are
:class:`Group`, and a group holds an ordered list of :class:`Leaf` tab
records plus which one is active. ``Leaf`` did not change a field: it is
still what a flat ``tabs`` row carries, which is deliberate and is the
whole migration story -- the flat list stays authoritative and one reader
still works against either shape.

A window whose tree is a single group holding a single tab IS today's
single-tab window, which is what keeps the migration honest.

**Weights are proportional, never absolute** (the spec's own word). They
are stored normalised to sum to 1.0 and rendered as ``fr`` units, so a
restore into a terminal of a different size preserves ratios rather than
columns -- an absolute column count saved on a 200-column monitor is a
crushed pane on a laptop, and the two are indistinguishable in the record
unless the record itself refuses to carry columns.

**The owner-first invariant.** Widgets cannot be re-parented in Textual
5.3 (a ``mount`` of an already-mounted widget is a silent no-op; measured,
not assumed), so a split can never wrap a leaf that already exists -- it
can only be created around a leaf whose enclosing box was mounted empty
ahead of time. :data:`SPLIT_SLOTS` is how many such boxes each leaf is
born inside, and it is therefore both the interactive DEPTH CAP the spec
asks for and the reason every split node this app produces has the pane
that was split as its FIRST child. :func:`rebuild_slots` is the same
arithmetic run backwards, so a tree read off disk rebuilds into the same
widget shape the interactive gesture would have produced.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

#: What a leaf SHOWS. Through v0.91.0 this did not exist because there
#: was one answer: a leaf was a session pane and nothing else. v0.92.0's
#: live diff is the first leaf that is not, and the field is what makes
#: it expressible at all -- without it :func:`doxa.ui.split._leaf_of`
#: returns ``None`` for the diff, the split node collapses to its one
#: surviving child, and the persisted record says "one pane" while the
#: screen shows two. That is the exact defect class the split module's
#: own docstring warns about (the model says split, the screen says not),
#: so a leaf now names its kind rather than having it inferred from a
#: widget class.
#:
#: A DIFF leaf still carries the ``session_id`` of the session it is a
#: diff OF. That is not a spare field: it is what makes :func:`prune`
#: correct without knowing anything about diffs -- a diff whose session
#: died is pruned by the same rule that prunes the session.
VIEW_SESSION = "session"
VIEW_DIFF = "diff"
VIEWS = (VIEW_SESSION, VIEW_DIFF)

#: Children side by side -- a vertical divider between them.
ROW = "row"
#: Children stacked -- a horizontal divider between them.
COLUMN = "column"
ORIENTATIONS = (ROW, COLUMN)

#: How many enclosing split boxes each leaf is born inside, and therefore
#: how many times ONE pane can be split before DOXA refuses. Two is the
#: low end of the spec's "two or three levels" recommendation and is
#: exactly what a 2x2 grid costs: split A sideways, then split each half
#: the other way. A constant, not an architectural limit -- raising it
#: costs one number and one more empty container per leaf.
SPLIT_SLOTS = 2

#: A leaf below either floor is not a pane, it is a sliver. The height
#: floor is derived, not chosen: one status bar (1) + a bordered prompt at
#: its one-row minimum (3) + a transcript that can show a turn (5).
#: The width floor is the narrowest the status bar's own chip row stays
#: legible at (see TAB_MODEL_MIN / TAB_REPO_MIN in doxa.ui.labels).
MIN_LEAF_HEIGHT = 9
MIN_LEAF_WIDTH = 34

#: Below this many columns a group draws its tab strip COMPACTLY: the
#: label is cut to the model segment alone, which is the one part of
#: ``model · repo`` that differs between two tabs of the same repo.
#:
#: MEASURED, not chosen (tests/test_pane_groups.py::
#: test_the_tab_strip_thresholds_are_the_measured_ones). A tab header
#: costs its label plus Textual's own ``Tab`` padding of one column each
#: side; the label's own documented floor is
#: ``TAB_MODEL_MIN (4) + " · " (3) + TAB_REPO_MIN (6)`` from
#: :mod:`doxa.ui.labels`, plus the provider glyph and its space (2) = 15,
#: so a full header is 17 columns and a strip that can show TWO of them --
#: the least a tab strip is FOR -- needs 34. One more than
#: :data:`MIN_LEAF_WIDTH`, which is where the number is checked against
#: reality: the narrowest group DOXA will create cannot show two full
#: headers, so the compact rung is not a corner case, it is the ordinary
#: state of a three-way split on a 100-column terminal.
GROUP_STRIP_COMPACT_COLS = 34

#: ...and below THIS many, not at all. A group this narrow has room for
#: one truncated word; a strip there is chrome that costs a row and answers
#: nothing, and the pane's own status bar already names the session. The
#: same hide-at-zero discipline :data:`doxa.ui.labels.CTX_ABSOLUTE_MIN_COLS`
#: and :data:`doxa.diff.SIDE_BY_SIDE_MIN_COLS` follow. Derived the same
#: way as the rung above, for ONE header rather than two.
GROUP_STRIP_MIN_COLS = 17

#: The prompt's own floor, in CONTENT rows (the border adds two). A
#: resize must never leave the input line too small to type into -- the
#: one region whose collapse makes DOXA unusable rather than awkward.
MIN_PROMPT_ROWS = 1
#: ...and the transcript's, so growing the prompt cannot swallow the
#: conversation it is being typed into.
MIN_TRANSCRIPT_ROWS = 3


@dataclass(frozen=True)
class Leaf:
    """One pane. ``session_id`` is the only field a restore strictly
    needs; the rest ride along so a leaf inside a tree carries everything
    the flat ``tabs`` row of the same session carries, and one reader can
    be written against either.

    ``prompt_ratio`` is the in-pane divider's position (see
    :func:`clamp_prompt_ratio`): the fraction of the pane's height the
    prompt area occupies. ``0.0`` means "no divider has been moved here",
    which restores to the content-driven auto height DOXA has always had
    -- an explicit zero and an absent field are the same statement.

    ``view`` is which SURFACE this leaf holds -- see :data:`VIEW_SESSION`
    / :data:`VIEW_DIFF`. It defaults to the session, so every record
    written before v0.92.0 reads back as exactly the leaf it was."""

    session_id: str
    pinned_name: "str | None" = None
    cwd: "str | None" = None
    prompt_ratio: float = 0.0
    view: str = VIEW_SESSION

    @property
    def is_diff(self) -> bool:
        return self.view == VIEW_DIFF


@dataclass(frozen=True)
class Group:
    """A pane GROUP: the thing a leaf of the window's layout tree holds
    since v0.97.0 -- an ordered list of tab records and which one of them
    is active.

    The tab records are :class:`Leaf` values, unchanged, because a group's
    tab and a flat ``tabs`` row carry exactly the same five facts (session
    id, pinned name, cwd, prompt ratio, surface). That identity is not a
    convenience: it is what lets :func:`doxa.tabsets.load` keep reading the
    flat list as authoritative while the grouped tree rides beside it, and
    what makes "absence of the key is the migration" a one-line rule rather
    than a schema version.

    ``active`` is an INDEX, not an id. A saved group whose active tab died
    between sessions must still name one of its survivors, and an index
    clamped into range does that with no lookup and no None state --
    :func:`prune` re-clamps it after it drops the dead tabs. Out-of-range
    and negative values are clamped at construction for the same reason
    :func:`normalise` never raises on a corrupt weight list: a layout is
    chrome, and a corrupt one costs the user their arrangement, never
    their session."""

    tabs: "tuple[Leaf, ...]" = ()
    active: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "tabs", tuple(self.tabs))
        try:
            index = int(self.active)
        except (TypeError, ValueError):
            index = 0
        if not self.tabs:
            index = 0
        else:
            index = max(0, min(index, len(self.tabs) - 1))
        object.__setattr__(self, "active", index)

    @property
    def active_tab(self) -> "Leaf | None":
        """The tab this group is SHOWING -- the one leaf of this group that
        is on screen. The other tabs keep running and are neither visible
        nor focused, which is what :meth:`doxa.app.DoxaApp._clear_seen_marks`
        depends on."""
        return self.tabs[self.active] if self.tabs else None


@dataclass(frozen=True)
class Split:
    """A split node: orientation, ordered children, per-child weights.

    ``weights`` is always the same length as ``children`` and always sums
    to 1.0 -- :func:`normalise` is the only constructor path that should
    ever fill it, and :func:`from_json` runs everything it reads through
    that function, so a hand-edited record with three weights for two
    children degrades to an even split instead of raising."""

    orientation: str
    children: "tuple[Any, ...]"
    weights: "tuple[float, ...]" = field(default=())

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "weights", normalise(self.weights, len(self.children))
        )


Node = Any  # Group | Split -- a recursive alias mypy 1.x still cannot spell


def normalise(weights: "Sequence[float]", count: int) -> "tuple[float, ...]":
    """``count`` positive weights summing to 1.0.

    Every degenerate input a record can carry resolves to the even split
    rather than to an exception: the wrong number of weights, a zero or
    negative weight, a NaN, an empty tuple. A layout is chrome; a corrupt
    one costs the user their proportions, never their session -- the same
    posture :func:`doxa.config.load` takes on a broken settings file."""
    if count <= 0:
        return ()
    even = tuple(1.0 / count for _ in range(count))
    if len(weights) != count:
        return even
    try:
        values = [float(w) for w in weights]
    except (TypeError, ValueError):
        return even
    if any(not (v > 0.0) or v != v or v == float("inf") for v in values):
        return even
    total = sum(values)
    if not (total > 0.0):
        return even
    return tuple(v / total for v in values)


def clamp_prompt_ratio(ratio: float) -> float:
    """A prompt ratio the in-pane divider is allowed to hold: 0.0 (auto)
    or a real fraction below 0.9. A ratio of 1.0 is a pane that is all
    prompt and no conversation, which the transcript floor would refuse to
    render anyway -- refusing it in the MODEL means a record can never
    carry it into a restore."""
    try:
        value = float(ratio)
    except (TypeError, ValueError):
        return 0.0
    if value != value or value <= 0.0:  # NaN, negative, zero
        return 0.0
    return min(value, 0.9)


# -- serialisation ----------------------------------------------------
#
# The wire shape is the one v0.32.0 reserved: a ``{"kind": ...}`` dict.
# "leaf" and "split" are new kinds beside the "tabs" kind that node has
# carried since then, and the compatibility rule is unchanged -- see
# doxa.tabsets._layout_tabs, which still reads an unrecognised kind as
# "nothing this version can lay out" rather than guessing.


def to_json(node: Node) -> dict:
    """A layout tree as plain JSON-able dicts."""
    if isinstance(node, Split):
        return {
            "kind": "split",
            "orientation": node.orientation,
            "weights": [round(w, 6) for w in node.weights],
            "children": [to_json(child) for child in node.children],
        }
    if isinstance(node, Group):
        # ``active`` is written unconditionally, zero included: a group
        # whose active index is absent would read back as its first tab,
        # which is a DIFFERENT statement from "this group is showing its
        # first tab" only when the writer meant something else -- and no
        # writer here ever does. Written anyway, because the field is one
        # integer and a reader that has to distinguish "0" from "missing"
        # is a reader with two code paths for one fact.
        return {
            "kind": "group",
            "active": node.active,
            "tabs": [to_json(tab) for tab in node.tabs],
        }
    leaf: Leaf = node
    out: dict = {"kind": "leaf", "session_id": leaf.session_id}
    if leaf.pinned_name:
        out["pinned_name"] = leaf.pinned_name
    if leaf.cwd:
        out["cwd"] = leaf.cwd
    if leaf.prompt_ratio:
        out["prompt_ratio"] = round(leaf.prompt_ratio, 6)
    if leaf.view != VIEW_SESSION:
        # Written only when it is not the default, so a v0.91.0 reader
        # sees byte-identical records for every layout it could produce
        # and this version's own session-only records stay diffable
        # against the ones already on disk.
        out["view"] = leaf.view
    return out


def from_json(data: Any) -> "Node | None":
    """Read a layout tree, or ``None`` for anything this version cannot
    lay out. Never raises: a malformed tree is a tree we do not restore,
    the same answer :func:`doxa.tabsets.load` gives a malformed record."""
    if not isinstance(data, dict):
        return None
    kind = data.get("kind")
    if kind == "leaf":
        session_id = str(data.get("session_id") or "").strip()
        if not session_id:
            return None
        pinned = data.get("pinned_name")
        cwd = data.get("cwd")
        # An unrecognised view degrades to the session, never raises and
        # never drops the leaf: the same posture normalise() takes on a
        # corrupt weight list. A record from a LATER version naming a
        # third surface restores as the session it is attached to, which
        # is a worse layout than the one saved and an infinitely better
        # outcome than a pane-shaped hole.
        view = data.get("view")
        return Leaf(
            session_id=session_id,
            pinned_name=str(pinned) if pinned else None,
            cwd=str(cwd) if cwd else None,
            prompt_ratio=clamp_prompt_ratio(data.get("prompt_ratio") or 0.0),
            view=view if view in VIEWS else VIEW_SESSION,
        )
    if kind == "group":
        raw_tabs = data.get("tabs")
        if not isinstance(raw_tabs, list):
            return None
        tabs = [t for t in (from_json(t) for t in raw_tabs) if isinstance(t, Leaf)]
        if not tabs:
            # A group with no readable tab is not an empty group, it is a
            # group this version cannot lay out -- the same answer a split
            # with no surviving child gets one branch down, and the same
            # answer the whole record gets in doxa.tabsets.load. An empty
            # group would restore as a region with nothing in it, which is
            # the pane-shaped hole prune() exists to prevent.
            return None
        active = data.get("active")
        return Group(tuple(tabs), active if isinstance(active, int) else 0)
    if kind != "split":
        return None
    orientation = data.get("orientation")
    if orientation not in ORIENTATIONS:
        return None
    raw_children = data.get("children")
    if not isinstance(raw_children, list):
        return None
    children = [c for c in (from_json(c) for c in raw_children) if c is not None]
    if not children:
        return None
    if len(children) == 1:
        # A split with one surviving child is not a split. Collapsing it
        # here rather than at restore time is what makes a pruned tree
        # (see prune) round-trip as the same tree a user would have got by
        # closing that pane by hand.
        return children[0]
    raw_weights = data.get("weights")
    weights = tuple(raw_weights) if isinstance(raw_weights, list) else ()
    return Split(orientation, tuple(children), weights)


# -- queries ----------------------------------------------------------


def leaves(node: Node) -> "list[Leaf]":
    """Every TAB RECORD in the tree, left-to-right / top-to-bottom and,
    within a group, in tab-strip order -- the order the flat ``tabs`` list
    is written in, so the two halves of a record never disagree about
    order.

    Still returns :class:`Leaf` after the v0.97.0 inversion, and that is
    the point: every caller of this asks "which sessions are in this
    layout", which is a question about tab records and not about regions.
    :func:`groups` is the one that asks about regions. Reads a bare
    ``Leaf`` too, so a v0.91.0 tree read straight off disk (before
    :func:`groupify` has run over it) still answers."""
    if isinstance(node, Split):
        out: "list[Leaf]" = []
        for child in node.children:
            out.extend(leaves(child))
        return out
    if isinstance(node, Group):
        return list(node.tabs)
    return [node]


def groups(node: Node) -> "list[Group]":
    """Every GROUP, left-to-right / top-to-bottom -- the regions on
    screen, in the order :func:`doxa.app.DoxaApp._group_order` numbers
    them from the painted rectangles. A bare ``Leaf`` (a v0.91.0 tree)
    reads as the single-tab group it becomes."""
    if isinstance(node, Split):
        out: "list[Group]" = []
        for child in node.children:
            out.extend(groups(child))
        return out
    if isinstance(node, Group):
        return [node]
    return [Group((node,), 0)]


def as_group(node: "Node | Leaf") -> Group:
    """One node as a group. A ``Leaf`` becomes the single-tab group it
    always was; a ``Group`` is itself."""
    return node if isinstance(node, Group) else Group((node,), 0)


def groupify(node: "Node | Leaf | None") -> "Node | None":
    """A v0.91.0 tree (leaves hold sessions) as a v0.97.0 tree (leaves
    hold groups): **one single-tab group per leaf**, structure untouched.

    This IS the middle era's migration, and the reason it needs no version
    field: a record with ``trees`` but no ``groups`` was written when a
    leaf held exactly one session, so reading each of its leaves as a group
    of one is not a guess, it is the same statement in the new vocabulary.
    Idempotent -- a tree that is already grouped comes back unchanged --
    so the reader can run it over anything without asking which era it
    got."""
    if node is None:
        return None
    if isinstance(node, Split):
        kept: "list[Node]" = []
        kept_weights: "list[float]" = []
        for child, weight in zip(node.children, node.weights):
            grouped = groupify(child)
            if grouped is not None:
                kept.append(grouped)
                kept_weights.append(weight)
        if not kept:
            return None
        if len(kept) == 1:
            return kept[0]
        return Split(node.orientation, tuple(kept), tuple(kept_weights))
    return as_group(node)


def depth(node: Node) -> int:
    """Nesting depth of the SPLIT nodes. A single leaf is 0."""
    if isinstance(node, Split):
        return 1 + max((depth(c) for c in node.children), default=0)
    return 0


def prune(node: Node, keep: "Iterable[str]") -> "Node | None":
    """The tree with every leaf whose session is not in ``keep`` removed,
    splits that lose all but one child collapsed, and weights
    re-normalised over the survivors.

    This is what a restore needs and why it lives here rather than in
    :mod:`doxa.tabsets`: the saved tree names sessions, and by the time
    the record is read some of those sessions are dead. A tree that still
    named them would restore an empty region -- a pane-shaped hole -- so
    the tree is pruned to the sessions that actually came back and the
    survivors take the missing pane's space proportionally."""
    keep_set = {s for s in keep}
    if isinstance(node, Group):
        # A group loses its DEAD TABS, not its region: three tabs of which
        # one session survived is still a group, showing the survivor. Only
        # a group that lost every tab is gone, and then the split above it
        # collapses by the same rule that has always applied to a leaf.
        #
        # ``active`` is re-derived rather than re-clamped blindly: the
        # user's active tab surviving in a different POSITION must stay the
        # active tab, which an index alone cannot express across a
        # deletion.
        surviving = [tab for tab in node.tabs if tab.session_id in keep_set]
        if not surviving:
            return None
        was_active = node.active_tab
        index = 0
        if was_active is not None and was_active in surviving:
            index = surviving.index(was_active)
        return Group(tuple(surviving), index)
    if not isinstance(node, Split):
        return node if node.session_id in keep_set else None
    kept: "list[Node]" = []
    kept_weights: "list[float]" = []
    for child, weight in zip(node.children, node.weights):
        pruned = prune(child, keep_set)
        if pruned is not None:
            kept.append(pruned)
            kept_weights.append(weight)
    if not kept:
        return None
    if len(kept) == 1:
        return kept[0]
    return Split(node.orientation, tuple(kept), tuple(kept_weights))


def rebuild_slots(node: Node, slots: int = SPLIT_SLOTS) -> "list[tuple[Node, int]]":
    """How many EMPTY split boxes each leaf must be rebuilt inside, so a
    tree read off disk lands in the same widget shape the interactive
    gesture would have produced.

    The owner-first invariant (see the module docstring) says a split node
    consumed one of its FIRST leaf's slots; every other child starts from
    a full allowance. Running that backwards gives each leaf its remaining
    allowance, which is exactly what :class:`doxa.ui.split.SplitBox`'s
    builder needs and the only reason a restored pane can still be split
    the same number of times a never-saved one can."""
    if not isinstance(node, Split):
        return [(node, max(0, slots))]
    out: "list[tuple[Node, int]]" = []
    for index, child in enumerate(node.children):
        out.extend(rebuild_slots(child, slots - 1 if index == 0 else SPLIT_SLOTS))
    return out


# -- geometry ---------------------------------------------------------


def neighbour(
    regions: "dict[str, tuple[int, int, int, int]]",
    current: str,
    direction: str,
) -> "str | None":
    """The pane a directional focus key should land on, or ``None``.

    ``regions`` maps a pane key to ``(x, y, width, height)`` in screen
    cells -- the real painted rectangle, never a tree position, because
    "next in the tree" is precisely the answer the spec rejects: in a 2x2
    grid "next" has no meaning a user can predict, and a tree order that
    puts the bottom-left pane after the top-right one is indistinguishable
    from a bug.

    The rule, in order:

    1. only panes strictly BEYOND the current pane's edge in ``direction``
       are candidates (a pane that merely overlaps is not "to the left");
    2. of those, only ones whose perpendicular span OVERLAPS the current
       pane's -- moving right out of the top-left cell of a 2x2 must not
       land in the bottom-right one;
    3. nearest edge wins; ties break on the closest perpendicular centre,
       then on the key, so the answer is total and deterministic.

    Rule 2 is relaxed exactly once: if no candidate overlaps, the nearest
    one in that direction is taken anyway. Without that, a user in a
    narrow pane at the bottom of a column could press Up and get nothing
    at all, which reads as a broken key rather than as a considered
    refusal."""
    if direction not in ("left", "right", "up", "down"):
        return None
    here = regions.get(current)
    if here is None:
        return None
    x, y, w, h = here
    overlapping: "list[tuple[int, int, str]]" = []
    fallback: "list[tuple[int, int, str]]" = []
    for key, (cx, cy, cw, ch) in regions.items():
        if key == current:
            continue
        if direction == "left":
            if cx + cw > x:
                continue
            gap = x - (cx + cw)
            overlap = min(y + h, cy + ch) - max(y, cy)
            perp = abs((cy + ch / 2) - (y + h / 2))
        elif direction == "right":
            if cx < x + w:
                continue
            gap = cx - (x + w)
            overlap = min(y + h, cy + ch) - max(y, cy)
            perp = abs((cy + ch / 2) - (y + h / 2))
        elif direction == "up":
            if cy + ch > y:
                continue
            gap = y - (cy + ch)
            overlap = min(x + w, cx + cw) - max(x, cx)
            perp = abs((cx + cw / 2) - (x + w / 2))
        else:  # down
            if cy < y + h:
                continue
            gap = cy - (y + h)
            overlap = min(x + w, cx + cw) - max(x, cx)
            perp = abs((cx + cw / 2) - (x + w / 2))
        (overlapping if overlap > 0 else fallback).append(
            (gap, int(perp * 2), key)
        )
    pool = overlapping or fallback
    if not pool:
        return None
    return min(pool)[2]


def split_refusal(width: int, height: int, orientation: str) -> "str | None":
    """Why this pane cannot be split, in the words the user will see --
    or ``None`` when it can.

    A split that would violate the floor is REFUSED with a message, not
    performed into an unusable sliver: the spec's own requirement, and the
    only kind of refusal a layout system can make that the user can act
    on. The arithmetic is halving, because a split always divides the pane
    it is aimed at in two."""
    if orientation == ROW:
        if width // 2 < MIN_LEAF_WIDTH:
            return (
                f"not enough width to split: each pane needs "
                f"{MIN_LEAF_WIDTH} columns and this one has {width}"
            )
    elif orientation == COLUMN:
        if height // 2 < MIN_LEAF_HEIGHT:
            return (
                f"not enough height to split: each pane needs "
                f"{MIN_LEAF_HEIGHT} rows and this one has {height}"
            )
    else:
        return f"unknown split orientation: {orientation!r}"
    return None

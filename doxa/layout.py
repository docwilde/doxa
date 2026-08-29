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

Each TAB owns one tree. A tab whose tree is a single leaf IS today's tab,
which is what keeps the migration honest: a v0.23.0 flat record restores
to single-leaf trees and behaves identically.

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
    -- an explicit zero and an absent field are the same statement."""

    session_id: str
    pinned_name: "str | None" = None
    cwd: "str | None" = None
    prompt_ratio: float = 0.0


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


Node = Any  # Leaf | Split -- a recursive alias mypy 1.x still cannot spell


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
    leaf: Leaf = node
    out: dict = {"kind": "leaf", "session_id": leaf.session_id}
    if leaf.pinned_name:
        out["pinned_name"] = leaf.pinned_name
    if leaf.cwd:
        out["cwd"] = leaf.cwd
    if leaf.prompt_ratio:
        out["prompt_ratio"] = round(leaf.prompt_ratio, 6)
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
        return Leaf(
            session_id=session_id,
            pinned_name=str(pinned) if pinned else None,
            cwd=str(cwd) if cwd else None,
            prompt_ratio=clamp_prompt_ratio(data.get("prompt_ratio") or 0.0),
        )
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
    """Every leaf, left-to-right / top-to-bottom -- the order the flat
    ``tabs`` list is written in, so the two halves of a record never
    disagree about order."""
    if isinstance(node, Split):
        out: "list[Leaf]" = []
        for child in node.children:
            out.extend(leaves(child))
        return out
    return [node]


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


def rebuild_slots(node: Node, slots: int = SPLIT_SLOTS) -> "list[tuple[Leaf, int]]":
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
    out: "list[tuple[Leaf, int]]" = []
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

# SPDX-License-Identifier: AGPL-3.0-only
"""The layout MODEL (v0.91.0): doxa/layout.py, with no widget in sight.

Everything here is a pure function, which is the point: the parts of a
layout that are hardest to get right -- what serialises, what a direction
key means in a 2x2, when a split is refused, what an old record reads as
-- are the parts that must be checkable without a running app, because
that is what makes them checkable at all.

The rendered half (a split actually painting two panes with non-zero
width and height, focus landing where the geometry says) lives in
tests/test_split_panes.py, against a real Pilot.
"""

from __future__ import annotations

import json

from doxa import layout


# -- weights ------------------------------------------------------------


def test_weights_are_proportional_and_always_sum_to_one():
    node = layout.Split(
        layout.ROW,
        (layout.Leaf("a"), layout.Leaf("b"), layout.Leaf("c")),
        (2.0, 1.0, 1.0),
    )
    assert node.weights == (0.5, 0.25, 0.25)
    assert abs(sum(node.weights) - 1.0) < 1e-9


def test_every_degenerate_weight_input_reads_as_an_even_split():
    """A layout is chrome; a corrupt one costs proportions, never the
    session. Same posture doxa.config.load takes on a broken settings
    file -- so none of these raises, and all of them land on even."""
    even = (0.5, 0.5)
    assert layout.normalise((), 2) == even
    assert layout.normalise((1.0,), 2) == even          # wrong count
    assert layout.normalise((0.0, 1.0), 2) == even      # a zero share
    assert layout.normalise((-1.0, 2.0), 2) == even     # a negative share
    assert layout.normalise((float("nan"), 1.0), 2) == even
    assert layout.normalise((float("inf"), 1.0), 2) == even
    assert layout.normalise(("x", 1.0), 2) == even      # not numbers at all
    assert layout.normalise((1.0, 1.0), 0) == ()


def test_a_prompt_ratio_can_never_swallow_the_whole_pane():
    assert layout.clamp_prompt_ratio(0.0) == 0.0
    assert layout.clamp_prompt_ratio(-0.5) == 0.0       # negative reads as auto
    assert layout.clamp_prompt_ratio(float("nan")) == 0.0
    assert layout.clamp_prompt_ratio(0.4) == 0.4
    assert layout.clamp_prompt_ratio(1.0) == 0.9        # all prompt, no session


# -- serialisation ------------------------------------------------------


def _grid() -> layout.Split:
    """The 2x2: two side-by-side columns, each split in two."""
    return layout.Split(
        layout.ROW,
        (
            layout.Split(layout.COLUMN, (layout.Leaf("a"), layout.Leaf("c"))),
            layout.Split(layout.COLUMN, (layout.Leaf("b"), layout.Leaf("d"))),
        ),
        (0.6, 0.4),
    )


def test_a_tree_round_trips_through_json_unchanged():
    tree = _grid()
    # Through real JSON, not just the dicts -- the record is a file.
    again = layout.from_json(json.loads(json.dumps(layout.to_json(tree))))
    assert again == tree
    assert [leaf.session_id for leaf in layout.leaves(again)] == ["a", "c", "b", "d"]


def test_leaf_fields_survive_the_round_trip():
    leaf = layout.Leaf("sid", pinned_name="my tab", cwd="/tmp/x", prompt_ratio=0.35)
    assert layout.from_json(layout.to_json(leaf)) == leaf


def test_a_leaf_with_no_extras_serialises_to_the_minimum():
    """An unset divider and an absent field are the same statement, so
    the record does not carry a zero nobody chose."""
    assert layout.to_json(layout.Leaf("sid")) == {"kind": "leaf", "session_id": "sid"}


def test_anything_unreadable_is_nothing_to_restore_rather_than_a_crash():
    assert layout.from_json(None) is None
    assert layout.from_json({}) is None
    assert layout.from_json({"kind": "leaf"}) is None            # no session id
    assert layout.from_json({"kind": "tabs"}) is None            # a kind, not a tree
    assert layout.from_json(
        {"kind": "split", "orientation": "diagonal", "children": []}
    ) is None
    assert layout.from_json(
        {"kind": "split", "orientation": "row", "children": "nope"}
    ) is None


def test_a_split_that_reads_back_with_one_child_is_not_a_split():
    node = layout.from_json({
        "kind": "split", "orientation": "row",
        "children": [{"kind": "leaf", "session_id": "a"}, {"kind": "leaf"}],
    })
    assert node == layout.Leaf("a")


# -- pruning ------------------------------------------------------------


def test_pruning_drops_dead_sessions_and_collapses_what_is_left():
    """The saved tree names sessions; by restore time some are dead. A
    tree that still named them would restore a pane-shaped hole."""
    tree = _grid()
    pruned = layout.prune(tree, {"a", "b", "d"})
    assert pruned == layout.Split(
        layout.ROW,
        (
            layout.Leaf("a"),  # its column collapsed -- "c" is gone
            layout.Split(layout.COLUMN, (layout.Leaf("b"), layout.Leaf("d"))),
        ),
        (0.6, 0.4),
    )


def test_pruning_everything_is_nothing_to_restore():
    assert layout.prune(_grid(), set()) is None


def test_survivors_take_the_missing_pane_space_proportionally():
    tree = layout.Split(
        layout.ROW,
        (layout.Leaf("a"), layout.Leaf("b"), layout.Leaf("c")),
        (0.5, 0.25, 0.25),
    )
    pruned = layout.prune(tree, {"a", "c"})
    assert [leaf.session_id for leaf in layout.leaves(pruned)] == ["a", "c"]
    assert pruned.weights == (2 / 3, 1 / 3)


# -- the owner-first rebuild --------------------------------------------


def test_a_rebuilt_tree_gets_the_slots_the_gesture_would_have_left():
    """Widgets cannot be re-parented, so every leaf is born inside
    SPLIT_SLOTS empty boxes and a split spends one of its FIRST leaf's.
    Run backwards, that says how many each leaf still owns after a
    restore -- which is the only reason a restored pane can be split the
    same number of times a never-saved one can."""
    assert layout.rebuild_slots(layout.Leaf("a")) == [(layout.Leaf("a"), 2)]

    one = layout.Split(layout.ROW, (layout.Leaf("a"), layout.Leaf("b")))
    assert layout.rebuild_slots(one) == [(layout.Leaf("a"), 1), (layout.Leaf("b"), 2)]

    # The 2x2: "a" spent both of its own, everyone else spent fewer.
    assert layout.rebuild_slots(_grid()) == [
        (layout.Leaf("a"), 0),
        (layout.Leaf("c"), 2),
        (layout.Leaf("b"), 1),
        (layout.Leaf("d"), 2),
    ]


def test_depth_counts_splits_not_leaves():
    assert layout.depth(layout.Leaf("a")) == 0
    assert layout.depth(_grid()) == 2


# -- directional focus --------------------------------------------------


#: A 2x2 over an 80x24 surface: a | b on top, c | d below.
GRID = {
    "a": (0, 0, 40, 12),
    "b": (40, 0, 40, 12),
    "c": (0, 12, 40, 12),
    "d": (40, 12, 40, 12),
}


def test_directional_focus_lands_on_the_geometric_neighbour_in_a_2x2():
    """"Next pane" has no meaning a user can predict in a grid, so this
    is the whole of what the keys promise: the pane that is actually
    over there."""
    assert layout.neighbour(GRID, "a", "right") == "b"
    assert layout.neighbour(GRID, "a", "down") == "c"
    assert layout.neighbour(GRID, "b", "left") == "a"
    assert layout.neighbour(GRID, "b", "down") == "d"
    assert layout.neighbour(GRID, "d", "up") == "b"
    assert layout.neighbour(GRID, "d", "left") == "c"


def test_moving_right_out_of_a_grid_cell_never_lands_diagonally():
    """The perpendicular-overlap rule: from the top-left cell, "right" is
    the top-right one and nothing else, even though the bottom-right one
    is also strictly to the right."""
    assert layout.neighbour(GRID, "a", "right") == "b"
    assert layout.neighbour(GRID, "c", "right") == "d"


def test_the_edge_of_the_layout_is_a_silent_no_op():
    assert layout.neighbour(GRID, "a", "up") is None
    assert layout.neighbour(GRID, "a", "left") is None
    assert layout.neighbour(GRID, "d", "down") is None
    assert layout.neighbour(GRID, "a", "sideways") is None
    assert layout.neighbour(GRID, "nobody", "left") is None
    assert layout.neighbour({"a": (0, 0, 10, 10)}, "a", "right") is None


def test_a_pane_with_no_overlapping_neighbour_still_reaches_one():
    """The relaxation, and why it exists: without it, a narrow pane at the
    bottom of a column could press Up and get nothing, which reads as a
    broken key rather than as a considered refusal."""
    regions = {"wide": (0, 0, 80, 10), "narrow": (60, 12, 20, 10)}
    assert layout.neighbour(regions, "narrow", "up") == "wide"


# -- refusals -----------------------------------------------------------


def test_a_split_that_would_make_a_sliver_is_refused_in_words():
    refusal = layout.split_refusal(40, 40, layout.ROW)
    assert refusal is not None
    assert str(layout.MIN_LEAF_WIDTH) in refusal
    assert "width" in refusal

    refusal = layout.split_refusal(200, 12, layout.COLUMN)
    assert refusal is not None
    assert str(layout.MIN_LEAF_HEIGHT) in refusal
    assert "height" in refusal


def test_a_split_with_room_is_not_refused():
    assert layout.split_refusal(200, 60, layout.ROW) is None
    assert layout.split_refusal(200, 60, layout.COLUMN) is None


def test_an_unknown_orientation_is_refused_rather_than_guessed():
    assert layout.split_refusal(200, 60, "sideways") is not None

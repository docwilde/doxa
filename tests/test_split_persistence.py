# SPDX-License-Identifier: AGPL-3.0-only
"""The persisted layout (v0.91.0): what the record carries, and what BOTH
readers make of it.

The requirement the spec states twice, because it is the one that costs a
user their working set if it is got wrong: **round-trip both ways.**

* a v0.32.0 reader must not choke on a record with splits in it, and must
  still read an unrecognised ``layout.kind`` as nothing-to-restore;
* a v0.91.0 reader must restore old FLAT records as single-leaf trees.

The restore test uses THREE OR MORE leaves on purpose. The old two-tab
test passed only because the saved tab happened to be the one that
mounted last, and a two-leaf layout hides the same class of error.
"""

from __future__ import annotations

import json

import pytest

from doxa import config as config_mod
from doxa import layout
from doxa import tabsets
from doxa.app import DoxaApp, RestoreTabSpec
from doxa.ui.prompt import PromptInput
from tests.fakes import FakeEngine


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "doxa-home"))
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    config_mod.invalidate()
    yield
    config_mod.invalidate()


def _factory(session_id: str):
    def make() -> FakeEngine:
        engine = FakeEngine([])
        engine.session_id = session_id
        return engine

    return make


async def _wait(pilot, cond, tries=300):
    for _ in range(tries):
        if cond():
            return True
        await pilot.pause(0.02)
    return cond()


def _raw(scope: str) -> dict:
    return json.loads(tabsets._file_for(scope).read_text(encoding="utf-8"))


# -- what the record looks like ----------------------------------------


def test_a_split_record_still_carries_the_flat_list_an_old_doxa_reads(tmp_path):
    """The compatibility rule from v0.32.0, still load-bearing: ``tabs``
    stays at the TOP level, flat, every leaf in layout order. A DOXA
    without splits restores this as N ordinary tabs -- the honest
    degradation -- rather than as nothing."""
    scope = str(tmp_path)
    tree = layout.Split(
        layout.ROW,
        (
            layout.Split(layout.COLUMN, (layout.Leaf("sid-a"), layout.Leaf("sid-c"))),
            layout.Leaf("sid-b"),
        ),
    )
    rows = [
        tabsets.TabRecord("sid-a"), tabsets.TabRecord("sid-c"),
        tabsets.TabRecord("sid-b"),
    ]
    tabsets.save(scope, rows, "sid-c", trees=[tree])

    data = _raw(scope)
    assert [r["session_id"] for r in data["tabs"]] == ["sid-a", "sid-c", "sid-b"]
    # ...and the layout node's own KIND is unchanged, so a v0.32.0 reader
    # takes the "tabs" branch rather than the nothing-to-restore one.
    assert data["layout"]["kind"] == "tabs"
    assert [r["session_id"] for r in data["layout"]["tabs"]] == [
        "sid-a", "sid-c", "sid-b",
    ]
    assert data["layout"]["trees"][0]["kind"] == "split"


def test_the_old_reader_still_treats_an_unknown_kind_as_nothing_to_restore(tmp_path):
    """The branch v0.32.0 reserved and this release did NOT take: a
    layout node whose kind is not "tabs", with no top-level list beside
    it, is "nothing this version can lay out". Pinned so a future format
    change cannot quietly start guessing."""
    scope = str(tmp_path)
    path = tabsets._file_for(scope)
    path.write_text(json.dumps({
        "scope_key": scope,
        "active_session_id": None,
        "layout": {"kind": "split", "orientation": "row", "children": []},
    }), encoding="utf-8")
    assert tabsets.load(scope) is None


def test_a_flat_record_reads_as_one_single_leaf_tree_per_tab(tmp_path):
    """"A new reader must restore old flat records as single-leaf trees"
    -- implemented as the ABSENCE of the trees key, so there is no
    version field and no migration step."""
    scope = str(tmp_path)
    path = tabsets._file_for(scope)
    path.write_text(json.dumps({
        "scope_key": scope,
        "active_session_id": "sid-2",
        "tabs": [
            {"session_id": "sid-1", "pinned_name": "one", "cwd": "/tmp/one"},
            {"session_id": "sid-2", "pinned_name": None, "cwd": None},
        ],
        "layout": {"kind": "tabs", "tabs": [
            {"session_id": "sid-1"}, {"session_id": "sid-2"},
        ]},
    }), encoding="utf-8")

    record = tabsets.load(scope)
    assert record is not None
    assert record.trees == (
        layout.Leaf("sid-1", pinned_name="one", cwd="/tmp/one"),
        layout.Leaf("sid-2"),
    )
    assert all(layout.depth(tree) == 0 for tree in record.trees)


def test_a_tree_survives_the_file(tmp_path):
    scope = str(tmp_path)
    tree = layout.Split(
        layout.COLUMN,
        (
            layout.Leaf("sid-a", prompt_ratio=0.3),
            layout.Split(layout.ROW, (layout.Leaf("sid-b"), layout.Leaf("sid-c"))),
        ),
        (0.7, 0.3),
    )
    tabsets.save(
        scope,
        [tabsets.TabRecord(s) for s in ("sid-a", "sid-b", "sid-c")],
        "sid-a",
        trees=[tree],
    )
    record = tabsets.load(scope)
    assert record.trees == (tree,)
    assert record.trees[0].weights == (0.7, 0.3)
    assert layout.leaves(record.trees[0])[0].prompt_ratio == 0.3


def test_a_malformed_tree_falls_back_to_single_leaves_not_to_nothing(tmp_path):
    scope = str(tmp_path)
    path = tabsets._file_for(scope)
    path.write_text(json.dumps({
        "scope_key": scope,
        "active_session_id": None,
        "tabs": [{"session_id": "sid-1"}],
        "layout": {"kind": "tabs", "tabs": [{"session_id": "sid-1"}],
                   "trees": ["not a tree", {"kind": "wat"}]},
    }), encoding="utf-8")
    record = tabsets.load(scope)
    assert record is not None
    assert record.trees == (layout.Leaf("sid-1"),)


# -- what the app writes ------------------------------------------------


@pytest.mark.asyncio
async def test_a_live_split_is_written_into_the_record_as_a_tree(tmp_path):
    where = tmp_path / "scope"
    where.mkdir()
    engines: list[FakeEngine] = []
    serial = [0]

    def make() -> FakeEngine:
        serial[0] += 1
        engine = FakeEngine([])
        engine.session_id = f"sid-{serial[0]}"
        engines.append(engine)
        return engine

    app = DoxaApp(cwd=str(where), engine_factory=make, new_session_factory=make)
    async with app.run_test(size=(160, 48)) as pilot:
        await pilot.pause()
        assert await _wait(pilot, lambda: bool(app.panes()[0]._session_id))
        assert await app.split_active_pane(layout.ROW) is None
        assert await _wait(
            pilot,
            lambda: len(app.panes()) == 2 and all(p._session_id for p in app.panes()),
        )
        await pilot.pause()
        app._persist_tabset()

        record = tabsets.load(str(where))
        assert record is not None
        assert len(record.trees) == 1
        tree = record.trees[0]
        assert isinstance(tree, layout.Split)
        assert tree.orientation == layout.ROW
        assert [leaf.session_id for leaf in layout.leaves(tree)] == [
            p._session_id for p in app.panes()
        ]
        # The flat list agrees with the tree, in the same order.
        assert [t.session_id for t in record.tabs] == [
            leaf.session_id for leaf in layout.leaves(tree)
        ]


@pytest.mark.asyncio
async def test_a_moved_in_pane_divider_persists(tmp_path):
    """A drag (or a Ctrl+Down) is a state change with no keystroke behind
    it in the record's sense -- it must survive restore like any other
    layout state."""
    where = tmp_path / "scope"
    where.mkdir()

    def make() -> FakeEngine:
        engine = FakeEngine([])
        engine.session_id = "sid-only"
        return engine

    app = DoxaApp(cwd=str(where), engine_factory=make, new_session_factory=make)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert await _wait(pilot, lambda: bool(app.panes()[0]._session_id))
        for _ in range(3):
            await pilot.press("ctrl+down")
        await pilot.pause()
        ratio = app.active_pane.prompt_ratio
        assert ratio > 0

    record = tabsets.load(str(where))
    assert record is not None
    assert layout.leaves(record.trees[0])[0].prompt_ratio == pytest.approx(
        ratio, abs=1e-5
    )


# -- restore ------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_saved_split_restores_as_a_split_with_the_right_leaf_focused(
    tmp_path,
):
    """THREE leaves in one tab, saved active in the MIDDLE -- the shape
    the spec insists on, for the reason it gives: the old two-tab test
    passed only because the saved tab happened to be last."""
    where = tmp_path / "scope"
    where.mkdir()
    tree = layout.Split(
        layout.ROW,
        (
            layout.Leaf("sid-1"),
            layout.Split(layout.COLUMN, (layout.Leaf("sid-2"), layout.Leaf("sid-3"))),
        ),
    )
    specs = [RestoreTabSpec(f"sid-{n}", _factory(f"sid-{n}")) for n in (1, 2, 3)]
    app = DoxaApp(
        cwd=str(where), restore_tabs=specs, restore_active_id="sid-2",
        restore_layout=[tree],
    )
    async with app.run_test(size=(160, 48)) as pilot:
        assert await _wait(pilot, lambda: len(app.panes()) == 3)
        await pilot.pause()

        # v0.96.0: the saved v0.91.0 tree migrates as ONE SINGLE-TAB GROUP
        # PER LEAF -- the absence of the record's ``groups`` key is the
        # whole migration -- so three leaves come back as three regions,
        # each with a tab of its own. The GEOMETRY below is what v0.91.0
        # asserted and is unchanged; only the container is.
        assert len(app.query("PaneTab")) == 3
        assert len(app.groups()) == 3
        panes = app.panes()
        assert [p._session_id for p in panes] == ["sid-1", "sid-2", "sid-3"]
        for pane in panes:
            assert pane.region.width > 0 and pane.region.height > 0
        # ...and in the saved GEOMETRY, not merely the saved order.
        assert panes[1].region.x > panes[0].region.x
        assert panes[2].region.y > panes[1].region.y

        middle = panes[1]
        assert app.active_pane is middle
        assert app.focused is middle.query_one("#prompt-input", PromptInput)


@pytest.mark.asyncio
async def test_a_restore_with_no_saved_layout_gives_each_session_its_own_tab(
    tmp_path,
):
    """The migration, end to end: a pre-v0.91.0 record has no trees, so
    three saved sessions come back as three ordinary tabs and behave
    exactly as they did."""
    where = tmp_path / "scope"
    where.mkdir()
    specs = [RestoreTabSpec(f"sid-{n}", _factory(f"sid-{n}")) for n in (1, 2, 3)]
    app = DoxaApp(cwd=str(where), restore_tabs=specs, restore_active_id="sid-2")
    async with app.run_test(size=(160, 48)) as pilot:
        assert await _wait(pilot, lambda: len(app.panes()) == 3)
        await pilot.pause()
        assert len(app.query("PaneTab")) == 3
        assert app.active_pane._session_id == "sid-2"


@pytest.mark.asyncio
async def test_a_saved_leaf_whose_session_died_leaves_no_hole(tmp_path):
    """The tree names sessions; by restore time some are dead. The
    survivors take the missing pane's space rather than restoring a
    pane-shaped hole."""
    where = tmp_path / "scope"
    where.mkdir()
    tree = layout.Split(
        layout.ROW,
        (layout.Leaf("sid-1"), layout.Leaf("sid-gone"), layout.Leaf("sid-3")),
    )
    specs = [RestoreTabSpec(f"sid-{n}", _factory(f"sid-{n}")) for n in (1, 3)]
    app = DoxaApp(cwd=str(where), restore_tabs=specs, restore_layout=[tree])
    async with app.run_test(size=(160, 48)) as pilot:
        assert await _wait(pilot, lambda: len(app.panes()) == 2)
        await pilot.pause()
        panes = app.panes()
        assert [p._session_id for p in panes] == ["sid-1", "sid-3"]
        # The WINDOW's own width (v0.96.0: the tree moved up a level, so
        # the tab's rectangle is now one region's, not the whole thing's).
        whole = app._window_root().region.width
        assert panes[0].region.width + panes[1].region.width == whole


@pytest.mark.asyncio
async def test_a_restored_split_leaf_can_still_be_split(tmp_path):
    """The owner-first rebuild's whole point: a restored pane keeps the
    slot allowance the interactive gesture would have left it, so a
    restored layout is not a dead end."""
    where = tmp_path / "scope"
    where.mkdir()
    tree = layout.Split(layout.ROW, (layout.Leaf("sid-1"), layout.Leaf("sid-2")))
    specs = [RestoreTabSpec(f"sid-{n}", _factory(f"sid-{n}")) for n in (1, 2)]
    app = DoxaApp(cwd=str(where), restore_tabs=specs, restore_layout=[tree])
    async with app.run_test(size=(160, 48)) as pilot:
        assert await _wait(pilot, lambda: len(app.panes()) == 2)
        await pilot.pause()
        app._focus_tab(app.panes()[0])
        assert await _wait(pilot, lambda: app.active_pane is app.panes()[0])
        await pilot.pause()
        assert await app.split_active_pane(layout.COLUMN) is None
        assert await _wait(pilot, lambda: len(app.panes()) == 3)
        await pilot.pause()
        # Three regions, each its own group (v0.96.0). What this test is
        # about is unchanged: a RESTORED region keeps the slot allowance
        # the interactive gesture would have left it, so a restored layout
        # is not a dead end.
        assert len(app.groups()) == 3
        for pane in app.panes():
            assert pane.region.width > 0 and pane.region.height > 0

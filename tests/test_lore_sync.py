# SPDX-License-Identifier: AGPL-3.0-only
"""LORE sync, DOXA's half (LORE/docs/plans/sync.md, the "## DOXA" section).

DOXA owns three things there -- the tab set scope key, the worktree
sidecar's machine id, and the status chip. The first two are covered in
tests/test_tabsets.py and tests/test_worktrees.py, beside the restore
behaviour they must not change. This file covers the module both of them
ask (:mod:`doxa.lore_sync`) and the chip (item 3).

The governing rule, and what nearly every test below is named for: **sync
off is the default and has to be invisible.** Not "off shows a zero", not
"off shows an error" -- a machine that never turned sync on behaves
exactly as 1.9.2 did. The two ways that can break are a chip painting a
zero and a store read crashing a terminal, so both have their own test.

``lore_core`` is never asked to stand up a real op log here. DOXA does not
own the op log; a test that needed one would be testing lore_core, and it
would pass or fail on which lore_core ``doxa._lore_bootstrap`` happened to
resolve -- which on a machine with the LORE plugin checked out is not even
the pinned one.
"""

from __future__ import annotations

import sqlite3

import pytest

from doxa import lore_sync as lore_sync_mod
from doxa.ui.labels import sync_chip

# Does the lore_core this process actually resolved carry an op log at all?
# MEASURED, not assumed: doxa._lore_bootstrap PREFERS a plugin checkout over
# the pinned wheel, so a developer with a LORE checkout older than sync.md's
# PR 3 imports a lore_core with no sync_oplog module -- which is exactly the
# degradation the tests above pin, and exactly why the handful of tests that
# need the real thing have to say so rather than fail on a machine that is
# correctly configured for something else.
try:  # noqa: SIM105 -- the import IS the probe
    import lore_core.sync_apply  # noqa: F401
    import lore_core.sync_oplog  # noqa: F401

    _HAVE_OPLOG = True
except ImportError:
    _HAVE_OPLOG = False

requires_oplog = pytest.mark.skipif(
    not _HAVE_OPLOG,
    reason="this lore_core predates the op log (sync.md PR 3)",
)


@pytest.fixture(autouse=True)
def _fresh_machine_cache():
    """The machine id is read ONCE per process and cached on purpose (a
    repaint must not open a database). That cache would otherwise carry one
    test's identity into the next, so it is dropped around every test here
    -- the same reason tests/test_tabsets.py resets doxa.config's cache."""
    lore_sync_mod.invalidate()
    yield
    lore_sync_mod.invalidate()


@pytest.fixture
def _sync_configured(monkeypatch):
    """A hub URL set and the kill switch clear -- what sync.md's
    configuration table calls sync being on at all."""
    monkeypatch.setenv("LORE_SYNC_URL", "https://hub.example/lore")
    monkeypatch.delenv("LORE_SYNC_PEER", raising=False)
    monkeypatch.delenv("LORE_DISABLE_SYNC", raising=False)


# -- the master switch ---------------------------------------------------


def test_sync_is_off_without_a_hub_url(monkeypatch):
    """sync.md's configuration table: "unset means sync is off entirely".
    This is the default on every machine and the reason the rest of the
    feature costs nothing."""
    monkeypatch.delenv("LORE_SYNC_URL", raising=False)
    monkeypatch.delenv("LORE_SYNC_PEER", raising=False)
    assert lore_sync_mod.enabled() is False
    assert lore_sync_mod.tabsets_enabled() is False
    assert lore_sync_mod.worktrees_enabled() is False


def test_a_tailnet_peer_alone_turns_sync_on(monkeypatch):
    """Transport B has no hub URL at all -- gating only on LORE_SYNC_URL
    would leave a peer-to-peer machine reporting sync off forever."""
    monkeypatch.delenv("LORE_SYNC_URL", raising=False)
    monkeypatch.setenv("LORE_SYNC_PEER", "workstation")
    monkeypatch.delenv("LORE_DISABLE_SYNC", raising=False)
    assert lore_sync_mod.enabled() is True


def test_the_kill_switch_beats_a_configured_hub(monkeypatch, _sync_configured):
    """LORE_DISABLE_SYNC is the stage kill switch. A user who sets it has
    turned sync off, whatever else is still configured."""
    monkeypatch.setenv("LORE_DISABLE_SYNC", "1")
    assert lore_sync_mod.enabled() is False


def test_the_two_doxa_classes_are_opt_in_even_with_a_hub(monkeypatch, _sync_configured):
    """sync.md's "Default class set" decision (#9): tabsets and worktrees
    are opt-in, so a configured hub alone must not start stamping records
    the user never asked to travel."""
    monkeypatch.setenv("LORE_SYNC_CLASSES", "memory,filemap,beliefs")
    assert lore_sync_mod.enabled() is True
    assert lore_sync_mod.tabsets_enabled() is False
    assert lore_sync_mod.worktrees_enabled() is False
    monkeypatch.setenv("LORE_SYNC_CLASSES", "memory,tabsets,worktrees")
    assert lore_sync_mod.tabsets_enabled() is True
    assert lore_sync_mod.worktrees_enabled() is True


# -- the machine id ------------------------------------------------------


def _machine_table_store(tmp_path):
    """A store with the ``sync_machine`` table and NO row in it -- the state
    a machine that has the tables but has never minted an identity is in."""
    path = tmp_path / "state.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE sync_machine(machine_id TEXT, label TEXT, lamport INTEGER)"
    )
    conn.commit()
    conn.close()
    return path


def _machine_rows(path) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT count(*) FROM sync_machine").fetchone()[0]
    finally:
        conn.close()


def test_the_machine_id_probe_mints_nothing(monkeypatch, tmp_path):
    """The load-bearing property of ``create=False``: asking "is this record
    mine" on a machine with sync off must not leave an identity in a store
    nobody opted in to. If this regresses, every DOXA in the world starts
    writing a sync_machine row on first launch.

    Asserted against the TABLE, not against a mock of lore_core's minting
    function -- the claim is about what is on disk afterwards."""
    db = _machine_table_store(tmp_path)
    import lore_core.store as store_mod

    monkeypatch.setattr(store_mod, "db_connect", lambda: sqlite3.connect(db))
    monkeypatch.delenv("LORE_SYNC_URL", raising=False)
    monkeypatch.delenv("LORE_SYNC_PEER", raising=False)
    assert lore_sync_mod.machine_id() is None
    assert _machine_rows(db) == 0


def test_machine_id_refuses_to_mint_when_sync_is_off(monkeypatch, tmp_path):
    """``create=True`` is for write paths, but a write path that reaches it
    with sync off is not licence to mint -- the record would be stamped on a
    machine that never opted in."""
    db = _machine_table_store(tmp_path)
    import lore_core.store as store_mod

    monkeypatch.setattr(store_mod, "db_connect", lambda: sqlite3.connect(db))
    monkeypatch.delenv("LORE_SYNC_URL", raising=False)
    monkeypatch.delenv("LORE_SYNC_PEER", raising=False)
    assert lore_sync_mod.machine_id(create=True) is None
    assert _machine_rows(db) == 0


@requires_oplog
def test_machine_id_mints_once_when_sync_is_on(monkeypatch, tmp_path, _sync_configured):
    """Not vacuous: the SAME call with sync configured does mint, exactly
    once, and the row it writes is committed rather than rolled back."""
    db = _machine_table_store(tmp_path)
    import lore_core.store as store_mod

    monkeypatch.setattr(store_mod, "db_connect", lambda: sqlite3.connect(db))
    minted = lore_sync_mod.machine_id(create=True)
    assert minted
    assert _machine_rows(db) == 1


def test_machine_id_is_none_when_the_store_has_no_sync_tables(monkeypatch):
    """A store written by an older LORE -- which is a real configuration
    right now, because doxa._lore_bootstrap PREFERS a plugin checkout over
    the pinned wheel and a stale checkout has no op log at all. None, not
    an exception."""
    import lore_core.store as store_mod

    monkeypatch.setattr(store_mod, "db_connect", lambda: sqlite3.connect(":memory:"))
    assert lore_sync_mod.machine_id() is None


def test_machine_id_is_read_once_and_cached(monkeypatch, _sync_configured):
    """A repaint must not open a database. The id cannot change under a
    running process, so it is read once per session."""
    calls: list[int] = []

    def _one_row():
        calls.append(1)
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE sync_machine(machine_id TEXT, label TEXT)")
        conn.execute("INSERT INTO sync_machine VALUES('m-1', 'box')")
        return conn

    import lore_core.store as store_mod

    monkeypatch.setattr(store_mod, "db_connect", _one_row)
    assert lore_sync_mod.machine_id() == "m-1"
    assert lore_sync_mod.machine_id() == "m-1"
    assert lore_sync_mod.machine_id() == "m-1"
    assert len(calls) == 1


# -- the chip's own reading ----------------------------------------------


def test_read_state_is_none_with_sync_off(monkeypatch):
    """The chip is ABSENT with sync off -- not a zero, which would be a
    permanent reminder of nothing on every machine that never opted in."""
    monkeypatch.delenv("LORE_SYNC_URL", raising=False)
    monkeypatch.delenv("LORE_SYNC_PEER", raising=False)
    assert lore_sync_mod.read_state() is None


def test_read_state_is_none_when_the_sync_tables_do_not_exist(
    monkeypatch, _sync_configured
):
    """An older store degrades to NO CHIP rather than raising. This is the
    exact failure a missing sync_ops table produces (sqlite3.OperationalError
    out of unpushed_op_count), reproduced rather than imagined."""
    import lore_core.store as store_mod

    monkeypatch.setattr(store_mod, "db_connect", lambda: sqlite3.connect(":memory:"))
    assert lore_sync_mod.read_state() is None


def test_read_state_is_none_when_the_store_cannot_be_opened(
    monkeypatch, _sync_configured
):
    """A missing or unreadable store must not crash DOXA."""
    import lore_core.store as store_mod

    def _boom():
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(store_mod, "db_connect", _boom)
    assert lore_sync_mod.read_state() is None


def _synced_store(tmp_path):
    """A store shaped like a 0.54.0 one, with just the tables the status
    reading touches. FILE-backed, not ``:memory:``, because
    :func:`doxa.lore_sync.read_state` opens and closes its own connection --
    an in-memory database would be empty by the time it looked."""
    path = tmp_path / "state.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE sync_machine(machine_id TEXT, label TEXT, lamport INTEGER)"
    )
    conn.execute("INSERT INTO sync_machine VALUES('m-1','box',3)")
    conn.execute(
        "CREATE TABLE sync_ops(seq INTEGER PRIMARY KEY, op_id TEXT, machine_id TEXT,"
        " machine_seq INTEGER, lamport INTEGER, class TEXT, op TEXT, project_key TEXT,"
        " payload TEXT, mac TEXT, created TEXT, applied INTEGER DEFAULT 0)"
    )
    conn.execute(
        "CREATE TABLE sync_peers(peer TEXT PRIMARY KEY, pushed_seq INTEGER DEFAULT 0,"
        " pulled_cursor TEXT, last_push TEXT, last_pull TEXT, last_error TEXT)"
    )
    conn.execute(
        "CREATE TABLE sync_conflicts(kind TEXT, bucket TEXT, old_key TEXT,"
        " a_text TEXT, b_text TEXT, op_id TEXT, created TEXT)"
    )
    for seq in (1, 2, 3):
        conn.execute(
            "INSERT INTO sync_ops(seq, op_id, machine_id, machine_seq, lamport,"
            " class, op, payload, created, applied) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (seq, f"op-{seq}", "m-1", seq, seq, "memory", "add", "{}", "2026-01-01", 1),
        )
    conn.execute(
        "INSERT INTO sync_conflicts VALUES('belief','b','k','a','b','op-9','2026-01-01')"
    )
    conn.commit()
    conn.close()
    return path


@requires_oplog
def test_read_state_reports_unpushed_ops_and_a_conflict(
    monkeypatch, tmp_path, _sync_configured
):
    """The three numbers the chip shows come from lore_core, in one visit."""
    db = _synced_store(tmp_path)
    import lore_core.store as store_mod

    monkeypatch.setattr(store_mod, "db_connect", lambda: sqlite3.connect(db))
    state = lore_sync_mod.read_state()
    assert state is not None
    assert state.unpushed == 3
    # A 'belief' conflict short-circuits lore_core's both-present check, so
    # the pair is still unresolved and still counts.
    assert state.conflicts == 1
    assert state.last_pull_age_s is None  # no peer has ever pulled
    assert state.needs_attention is True


@requires_oplog
def test_read_state_reports_a_pull_age_from_the_freshest_peer(
    monkeypatch, tmp_path, _sync_configured
):
    """With two peers configured the question the chip answers is "how stale
    is what I am looking at", and the answer is the most recent arrival --
    not the laggard, which would make a healthy hub look broken."""
    from datetime import UTC, datetime, timedelta

    db = _synced_store(tmp_path)
    conn = sqlite3.connect(db)
    stale = (datetime.now(UTC) - timedelta(hours=9)).isoformat()
    fresh = (datetime.now(UTC) - timedelta(minutes=2)).isoformat()
    conn.execute(
        "INSERT INTO sync_peers(peer, pushed_seq, last_pull) VALUES('hub',0,?)", (stale,)
    )
    conn.execute(
        "INSERT INTO sync_peers(peer, pushed_seq, last_pull) VALUES('ws',0,?)", (fresh,)
    )
    conn.commit()
    conn.close()
    import lore_core.store as store_mod

    monkeypatch.setattr(store_mod, "db_connect", lambda: sqlite3.connect(db))
    state = lore_sync_mod.read_state()
    assert state is not None
    assert state.last_pull_age_s is not None
    assert state.last_pull_age_s < 600  # the fresh peer, not the nine-hour one


# -- the chip itself -----------------------------------------------------


def test_sync_chip_is_absent_without_a_reading():
    """None in, no chip out -- the single rule that keeps sync-off silent
    all the way to the status bar."""
    assert sync_chip(None) is None


def test_sync_chip_distinguishes_never_pulled_from_just_pulled():
    """"never" and "0s" are different claims about the same field and must
    not render alike."""
    never = sync_chip(lore_sync_mod.SyncState(None, 0, 0, 0))
    fresh = sync_chip(lore_sync_mod.SyncState(0.0, 0, 0, 0))
    assert never is not None and fresh is not None
    assert "never" in never[0]
    assert "never" not in fresh[0]


def test_sync_chip_hides_a_zero_unpushed_count_but_shows_a_real_one():
    """Hide-at-zero per segment, the convention every chip on this row
    follows -- and NOT vacuous: the same function shows the count when
    there is one."""
    quiet = sync_chip(lore_sync_mod.SyncState(60.0, 0, 0, 0))
    busy = sync_chip(lore_sync_mod.SyncState(60.0, 7, 0, 0))
    assert quiet is not None and busy is not None
    assert "↑" not in quiet[0]
    assert "↑7" in busy[0]


def test_sync_chip_flags_a_conflict_and_says_it_is_the_users_to_resolve():
    """sync.md's merge rule 1 keeps BOTH entries and waits for a human.
    There is no command to run, so the tooltip has to say what to do."""
    chip = sync_chip(lore_sync_mod.SyncState(30.0, 0, 2, 0))
    assert chip is not None
    assert "⚠2" in chip[0]
    assert "delete the one you do not want" in chip[1]


def test_sync_chip_flags_an_unverified_op_separately_from_a_conflict():
    """A bad MAC is staged and never applied -- a different fact, and a
    different action, from two entries in a file."""
    chip = sync_chip(lore_sync_mod.SyncState(30.0, 0, 0, 1))
    assert chip is not None
    assert "⚠1" in chip[0]
    assert "integrity check" in chip[1]
    assert "/pending" in chip[1]

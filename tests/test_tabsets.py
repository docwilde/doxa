# SPDX-License-Identifier: AGPL-3.0-only
"""Item D (tab restore): doxa.tabsets' own record store (save/load/resolve
against the live peer registry), and doxa.app's wiring of it into the tab
lifecycle -- persisted on open/close/rename, restored on launch with
saved order/names/active tab.

This item's spec text did not survive to the session that built it; see
doxa/tabsets.py's own module docstring and CHANGELOG.md's 0.23.0 entry for
what had to be re-derived and judgment-called. doxa.cli's OWN plumbing
(when `doxa` consults the record at all) is covered separately in
tests/test_cli_restore.py, same split test_cli_branch.py already
established between "the CLI decides right" and "the app/daemon actually
boots".
"""

from __future__ import annotations

import json
import os
import socket

import pytest

from doxa import config as config_mod
from doxa import peers as peers_mod
from doxa import tabsets
from doxa.app import DoxaApp, RestoreTabSpec, SystemBlock
from textual.widgets import TabbedContent
from tests.fakes import FakeEngine


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    """Every test gets its own DOXA_HOME (the tabsets/ dir lives under it)
    and its own peer-registry runtime dir -- nothing here may read or
    write the developer's real state."""
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "doxa-home"))
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    config_mod.invalidate()
    yield
    config_mod.invalidate()


def _daemon_entry(session_id: str, scope: str, cwd: "str | None" = None) -> None:
    """A live, daemon-hosted registry entry -- peers.list_daemons (no
    probe) only checks pid-alive and heartbeat-age, so os.getpid() plus a
    fresh timestamp is enough to read as live without an actual socket
    listening anywhere, same shortcut tests/test_peers.py's own helper
    takes for the non-probed paths."""
    reg = peers_mod.registry_dir()
    entry = {
        "session_id": session_id,
        "pid": os.getpid(),
        "socket_path": str(reg / f"{session_id}.sock"),
        "cwd": cwd or scope,
        "repo_root": scope,
        "title": session_id[:8],
        "started_at": peers_mod._iso_now(),
        "heartbeat_at": peers_mod._iso_now(),
        "daemon_socket": str(reg / f"{session_id}-daemon.sock"),
    }
    (reg / f"{session_id}.json").write_text(json.dumps(entry), encoding="utf-8")


def _listening_daemon_entry(session_id: str, scope: str) -> "socket.socket":
    """A daemon entry that also PASSES probe=True (real listening unix
    socket at the same path _daemon_entry writes) -- what /sessions kill
    actually reads (``peers.read_registry(probe=True)``), unlike the
    plain _daemon_entry helper above which only the non-probed
    list_daemons() paths need. Caller closes the returned socket."""
    _daemon_entry(session_id, scope)
    reg = peers_mod.registry_dir()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(reg / f"{session_id}.sock"))
    sock.listen(1)
    return sock


# -- the record store --------------------------------------------------


def test_save_and_load_round_trip(tmp_path):
    scope = str(tmp_path / "repo")
    tabs = [tabsets.TabRecord("sid-1", "alpha"), tabsets.TabRecord("sid-2", None)]
    tabsets.save(scope, tabs, "sid-2")
    record = tabsets.load(scope)
    assert record is not None
    assert record.scope_key == scope
    assert record.tabs == tuple(tabs)
    assert record.active_session_id == "sid-2"


def test_write_is_atomic_and_0600(tmp_path):
    scope = str(tmp_path / "repo")
    tabsets.save(scope, [tabsets.TabRecord("sid-1")], "sid-1")
    path = tabsets._file_for(scope)
    assert path.exists()
    assert not path.with_suffix(".json.tmp").exists()
    assert oct(path.stat().st_mode)[-3:] == "600"


def test_missing_file_is_none_not_a_crash(tmp_path):
    assert tabsets.load(str(tmp_path / "never-saved")) is None


def test_corrupt_file_degrades_to_none(tmp_path):
    scope = str(tmp_path / "repo")
    path = tabsets._file_for(scope)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json", encoding="utf-8")
    assert tabsets.load(scope) is None


def test_non_dict_json_degrades_to_none(tmp_path):
    scope = str(tmp_path / "repo")
    path = tabsets._file_for(scope)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert tabsets.load(scope) is None


def test_empty_tabs_list_reads_as_nothing_to_restore(tmp_path):
    scope = str(tmp_path / "repo")
    tabsets.save(scope, [], None)
    assert tabsets.load(scope) is None


def test_clear_removes_the_record(tmp_path):
    scope = str(tmp_path / "repo")
    tabsets.save(scope, [tabsets.TabRecord("sid-1")], None)
    assert tabsets.load(scope) is not None
    tabsets.clear(scope)
    assert tabsets.load(scope) is None
    tabsets.clear(scope)  # idempotent, never raises on an already-gone file


def test_enabled_defaults_on_and_env_toggles_off(monkeypatch):
    monkeypatch.delenv("DOXA_RESTORE_TABS", raising=False)
    assert tabsets.enabled() is True
    monkeypatch.setenv("DOXA_RESTORE_TABS", "0")
    assert tabsets.enabled() is False
    monkeypatch.setenv("DOXA_RESTORE_TABS", "1")
    assert tabsets.enabled() is True


# -- resolve() against the live peer registry ---------------------------


def test_resolve_returns_none_when_nothing_was_ever_saved():
    assert tabsets.resolve("/never/saved") is None


def test_resolve_filters_dead_sessions_and_preserves_saved_order():
    scope = "/some/repo"
    tabsets.save(
        scope,
        [
            tabsets.TabRecord("sid-a", "first"),
            tabsets.TabRecord("sid-b", None),
            tabsets.TabRecord("sid-c", "third"),
        ],
        "sid-c",
    )
    # Registry entries written in a DIFFERENT order than saved, and sid-b
    # gets none at all -- resolve() must return SAVED order, never
    # registry order, and skip the dead one silently.
    _daemon_entry("sid-c", scope)
    _daemon_entry("sid-a", scope)
    resolved = tabsets.resolve(scope)
    assert resolved is not None
    assert [t.session_id for t, _ in resolved.tabs] == ["sid-a", "sid-c"]
    assert [entry.session_id for _, entry in resolved.tabs] == ["sid-a", "sid-c"]
    assert resolved.skipped == 1
    assert resolved.active_session_id == "sid-c"


def test_resolve_with_every_saved_session_dead_reports_zero_restored(tmp_path):
    scope = str(tmp_path / "repo")
    tabsets.save(scope, [tabsets.TabRecord("sid-a"), tabsets.TabRecord("sid-b")], "sid-a")
    resolved = tabsets.resolve(scope)
    assert resolved is not None
    assert resolved.tabs == []
    assert resolved.skipped == 2
    assert resolved.active_session_id is None


def test_resolve_dead_active_tab_falls_back_to_none(tmp_path):
    scope = str(tmp_path / "repo")
    tabsets.save(scope, [tabsets.TabRecord("sid-a"), tabsets.TabRecord("sid-b")], "sid-a")
    _daemon_entry("sid-b", scope)  # sid-a (the saved active tab) is dead
    resolved = tabsets.resolve(scope)
    assert [t.session_id for t, _ in resolved.tabs] == ["sid-b"]
    assert resolved.active_session_id is None


def test_resolve_never_crosses_scopes(tmp_path):
    scope_a = str(tmp_path / "a")
    scope_b = str(tmp_path / "b")
    tabsets.save(scope_a, [tabsets.TabRecord("sid-shared")], "sid-shared")
    _daemon_entry("sid-shared", scope_b)  # live, but in the WRONG scope
    resolved = tabsets.resolve(scope_a)
    assert resolved.tabs == []
    assert resolved.skipped == 1


# -- doxa.app wiring: persist on tab lifecycle --------------------------


def _fake_factory(session_id: str, script=None):
    def make() -> FakeEngine:
        engine = FakeEngine(list(script or []))
        engine.session_id = session_id
        return engine

    return make


async def _wait(pilot, cond, tries=100):
    for _ in range(tries):
        if cond():
            return True
        await pilot.pause(0.02)
    return cond()


def _claim_failures(app_cls) -> list:
    """Patch ``app_cls.report_failure`` to CAPTURE (not swallow) whatever
    reaches it, and hand back the live list a test can assert against --
    the same shape as tests/conftest.py's own autouse
    ``_errors_must_be_claimed``, layered on top of it rather than
    replacing it: that fixture resets the method back to its true
    original in ITS OWN teardown regardless of what this leaves behind,
    so a test using this helper does not need to restore anything
    itself. A test that wants a CLEAR failure on a background error --
    rather than the fixture's own opaque teardown error -- calls this
    early and asserts the list is empty before its own assertions."""
    original = app_cls.report_failure
    seen: list = []

    def _record(self, failure):
        seen.append(failure)
        return original(self, failure)

    app_cls.report_failure = _record
    return seen


@pytest.mark.asyncio
async def test_boot_persists_the_first_tab(tmp_path):
    where = tmp_path / "scratch"
    where.mkdir()
    factory = _fake_factory("sid-open")
    app = DoxaApp(cwd=str(where), engine_factory=factory, new_session_factory=factory)
    async with app.run_test() as pilot:
        assert await _wait(pilot, lambda: app.panes()[0]._session_id)
        await pilot.pause()
    record = tabsets.load(str(where))
    assert record is not None
    assert [t.session_id for t in record.tabs] == ["sid-open"]
    assert record.active_session_id == "sid-open"


@pytest.mark.asyncio
async def test_new_tab_appends_to_the_persisted_order(tmp_path):
    where = tmp_path / "scratch"
    where.mkdir()
    ids = iter(["sid-a", "sid-b"])

    def factory() -> FakeEngine:
        engine = FakeEngine([])
        engine.session_id = next(ids)
        return engine

    app = DoxaApp(cwd=str(where), engine_factory=factory, new_session_factory=factory)
    async with app.run_test() as pilot:
        assert await _wait(pilot, lambda: app.panes()[0]._session_id)
        await pilot.press("ctrl+t")
        assert await _wait(
            pilot, lambda: len(app.panes()) == 2 and app.panes()[1]._session_id
        )
        await pilot.pause()
    record = tabsets.load(str(where))
    assert [t.session_id for t in record.tabs] == ["sid-a", "sid-b"]
    assert record.active_session_id == "sid-b"  # the new tab is active


@pytest.mark.asyncio
async def test_rename_updates_the_pinned_name_in_the_record(tmp_path):
    where = tmp_path / "scratch"
    where.mkdir()
    factory = _fake_factory("sid-rename")
    app = DoxaApp(cwd=str(where), engine_factory=factory, new_session_factory=factory)
    async with app.run_test() as pilot:
        assert await _wait(pilot, lambda: app.panes()[0]._session_id)
        app.active_pane.set_custom_name("my pinned tab")
        await pilot.pause()
    record = tabsets.load(str(where))
    assert record.tabs[0].pinned_name == "my pinned tab"


@pytest.mark.asyncio
async def test_ctrl_w_detach_keeps_the_session_in_the_record(tmp_path):
    """Item D #4: a session merely DETACHED (Ctrl+W) stays in the
    persisted set even though its tab leaves the strip -- only an
    explicit STOP drops it (see the next test)."""
    where = tmp_path / "scratch"
    where.mkdir()
    ids = iter(["sid-a", "sid-b"])

    def factory() -> FakeEngine:
        engine = FakeEngine([])
        engine.session_id = next(ids)
        return engine

    app = DoxaApp(cwd=str(where), engine_factory=factory, new_session_factory=factory)
    async with app.run_test() as pilot:
        assert await _wait(pilot, lambda: app.panes()[0]._session_id)
        await pilot.press("ctrl+t")
        assert await _wait(
            pilot, lambda: len(app.panes()) == 2 and app.panes()[1]._session_id
        )
        await pilot.press("ctrl+w")  # close-detach the active (second) tab
        assert await _wait(pilot, lambda: len(app.panes()) == 1)
        await pilot.pause()
    record = tabsets.load(str(where))
    assert {t.session_id for t in record.tabs} == {"sid-a", "sid-b"}


@pytest.mark.asyncio
async def test_ctrl_w_parks_it_ctrl_q_ends_it(tmp_path):
    """The symmetry the fix is built on, stated as one test: Ctrl+W on a
    non-last tab keeps its session running and IN the persisted record
    (still reachable by NAME, /attach or the peers chip, next launch or
    this one) -- Ctrl+Q on a non-last tab ends the session and takes it
    OUT of the record entirely. Same starting shape (three tabs, close
    the middle one), opposite key, opposite bookkeeping dict: sid-b lands
    in _detached_this_run, never _ended_this_run, and its pane is never
    marked _stopped -- the flag _persist_tabset's mounted-pane scan now
    reads to drop a Ctrl+Q'd session is the one thing detach never sets."""
    where = tmp_path / "scratch"
    where.mkdir()
    ids = iter(["sid-a", "sid-b", "sid-c"])

    def factory() -> FakeEngine:
        engine = FakeEngine([])
        engine.session_id = next(ids)
        return engine

    app = DoxaApp(cwd=str(where), engine_factory=factory, new_session_factory=factory)
    async with app.run_test() as pilot:
        assert await _wait(pilot, lambda: app.panes()[0]._session_id)
        await pilot.press("ctrl+t")
        assert await _wait(
            pilot, lambda: len(app.panes()) == 2 and app.panes()[1]._session_id
        )
        await pilot.press("ctrl+t")
        assert await _wait(
            pilot, lambda: len(app.panes()) == 3 and app.panes()[2]._session_id
        )
        middle = app.panes()[1]
        await pilot.press("ctrl+left")
        assert await _wait(
            pilot,
            lambda: app.active_pane is not None
            and app.active_pane._session_id == "sid-b",
        )
        await pilot.press("ctrl+w")  # PARK the middle tab
        assert await _wait(pilot, lambda: len(app.panes()) == 2)
        await pilot.pause()
        assert "sid-b" in app._detached_this_run
        assert "sid-b" not in app._ended_this_run
        assert middle._stopped is False
    record = tabsets.load(str(where))
    assert {t.session_id for t in record.tabs} == {"sid-a", "sid-b", "sid-c"}


@pytest.mark.asyncio
async def test_stop_drops_the_ended_session_from_the_record(tmp_path):
    """v0.99.1 reverses v0.60.0's own reversal of this exact test (it used
    to assert the session STAYED, under the name
    test_stop_keeps_the_session_in_the_record). v0.56.0's session-id
    pinning still means --resume CAN replay a session Ctrl+Q ended -- that
    part was never wrong -- but v0.60.0 read it as license to keep the
    ended session in the AUTO-RESTORE set too, and DOXA's own finalize
    never removes the conversation from the CLI's history store, so the
    next launch's resume_state check found it and came back LIVE, not
    read-only: reported live as "tabs...are resurrected on the next start
    of DOXA anyway" (and, pressed further, "there is no way to permanently
    close a tab"). The palette's "Quit: stop session" is Ctrl+Q under a
    different door; both now drop the record."""
    where = tmp_path / "scratch"
    where.mkdir()
    ids = iter(["sid-a", "sid-b"])

    def factory() -> FakeEngine:
        engine = FakeEngine([])
        engine.session_id = next(ids)
        return engine

    app = DoxaApp(cwd=str(where), engine_factory=factory, new_session_factory=factory)
    async with app.run_test() as pilot:
        assert await _wait(pilot, lambda: app.panes()[0]._session_id)
        await pilot.press("ctrl+t")
        assert await _wait(
            pilot, lambda: len(app.panes()) == 2 and app.panes()[1]._session_id
        )
        app._cmd_stop_active()  # palette "Quit: stop session" on tab two
        assert await _wait(pilot, lambda: len(app.panes()) == 1)
        await pilot.pause()
    record = tabsets.load(str(where))
    assert {t.session_id for t in record.tabs} == {"sid-a"}


@pytest.mark.asyncio
async def test_ctrl_q_drops_the_ended_session_from_the_record(tmp_path):
    """The exact defect reported from disk, fixed: Ctrl+Q on a NON-last
    tab used to leave the ended session in the persisted set anyway
    (v0.60.0's deliberate choice) -- and worse, it came back LIVE next
    launch rather than read-only, because finalize() never touches the
    CLI's own history store (see test_ctrl_q_ended_session_is_not_in_
    resolve below for that half). Three tabs, closing the MIDDLE one:
    v0.91.0's spec notes a two-tab version of this test passed even with
    the old bug still live in one narrow shape, because the saved tab
    happened to also be the last one in the strip -- ``is_last`` is a
    COUNT (``len(self.panes()) == 1``), not a position, so a 2-tab test
    proves nothing about a session that has siblings on BOTH sides."""
    where = tmp_path / "scratch"
    where.mkdir()
    ids = iter(["sid-a", "sid-b", "sid-c"])

    def factory() -> FakeEngine:
        engine = FakeEngine([])
        engine.session_id = next(ids)
        return engine

    app = DoxaApp(cwd=str(where), engine_factory=factory, new_session_factory=factory)
    async with app.run_test() as pilot:
        assert await _wait(pilot, lambda: app.panes()[0]._session_id)
        await pilot.press("ctrl+t")
        assert await _wait(
            pilot, lambda: len(app.panes()) == 2 and app.panes()[1]._session_id
        )
        await pilot.press("ctrl+t")
        assert await _wait(
            pilot, lambda: len(app.panes()) == 3 and app.panes()[2]._session_id
        )
        await pilot.press("ctrl+left")  # step back onto tab two (sid-b)
        assert await _wait(
            pilot,
            lambda: app.active_pane is not None
            and app.active_pane._session_id == "sid-b",
        )
        middle = app.active_pane
        await pilot.press("ctrl+q")  # end the MIDDLE tab's session
        assert await _wait(pilot, lambda: len(app.panes()) == 2)
        await pilot.pause()
        # _ended_this_run STILL does its (v1.0.0 sidebar) job within this
        # run -- what changed is that it no longer has any say over what
        # the NEXT launch restores (that is _persist_tabset reading
        # middle._stopped, checked directly here too).
        assert "sid-b" in app._ended_this_run
        assert middle is not None and middle._stopped is True
    record = tabsets.load(str(where))
    assert {t.session_id for t in record.tabs} == {"sid-a", "sid-c"}


# -- v0.85.0: closing the LAST tab starts the next launch fresh -----------
#
# Reported live, in two parts: "if the last remaining open tab is closed
# with CTRL+Q, the next time doxa is started should start with a fresh
# session. If last open tab was closed with CTRL+W, we also start with a
# fresh session, but the old session could be reattached." The four tests
# above cover the OTHER case on purpose -- a second tab closed while a
# first one is still open -- where the closed one SHOULD stay in the
# record (there is still a window to restore). These cover the one they
# do not: nothing left open at all.


@pytest.mark.asyncio
async def test_ending_the_only_tab_with_ctrl_q_leaves_no_restore_record(tmp_path):
    """Also asserts NO background error surfaced during the quit -- an
    earlier version of the is_last fix called remove_pane() before
    persisting, which unmounted the pane while its _peer_pump worker was
    still alive and occasionally raced App.action_quit into an
    AssertionError (SessionPane._peer_pump's own `assert self.engine is
    not None`), visible in-app as an error block. tests/conftest.py's
    autouse _errors_must_be_claimed would already fail this test on that
    (as a teardown error, not a clean assertion) -- claiming it here
    explicitly is what turns a regression back into a readable failure."""
    seen = _claim_failures(DoxaApp)
    where = tmp_path / "scratch"
    where.mkdir()
    factory = _fake_factory("sid-solo")
    app = DoxaApp(cwd=str(where), engine_factory=factory, new_session_factory=factory)
    async with app.run_test() as pilot:
        assert await _wait(pilot, lambda: app.panes()[0]._session_id)
        await pilot.press("ctrl+q")
        await pilot.pause()
    assert seen == [], [f.headline() for f in seen]
    record = tabsets.load(str(where))
    assert record is None or record.tabs == ()


@pytest.mark.asyncio
async def test_palette_stop_active_on_the_only_tab_leaves_no_restore_record(
    tmp_path,
):
    """The palette's "Quit: stop session" (DoxaApp._stop_active) used to
    be a SEPARATE, partial reimplementation of Ctrl+Q's own disposition --
    it never grew v0.85.0's is_last carve-out at all, so stopping the
    ONLY tab from the palette left the session in the persisted record
    even on a DOXA that got Ctrl+Q's own last-tab case right. v0.99.1
    makes it delegate to _close_pane instead of re-deriving any of this a
    second time, which is what closes that gap -- this is the regression
    test for it."""
    seen = _claim_failures(DoxaApp)
    where = tmp_path / "scratch"
    where.mkdir()
    factory = _fake_factory("sid-solo")
    app = DoxaApp(cwd=str(where), engine_factory=factory, new_session_factory=factory)
    async with app.run_test() as pilot:
        assert await _wait(pilot, lambda: app.panes()[0]._session_id)
        app._cmd_stop_active()  # palette "Quit: stop session" on the only tab
        await pilot.pause()
    assert seen == [], [f.headline() for f in seen]
    record = tabsets.load(str(where))
    assert record is None or record.tabs == ()


@pytest.mark.asyncio
async def test_detaching_the_only_tab_with_ctrl_w_also_leaves_no_restore_record(
    tmp_path,
):
    """The other half: Ctrl+W's session is still running (unlike Ctrl+Q's
    -- see the notify test in tests/test_tabs.py for that half), but the
    next launch does not auto-restore it either. Reattaching it is a
    deliberate act (/attach, the peers chip) against the live daemon
    registry, never an automatic tab -- that registry, not this record,
    is what keeps it findable. Also asserts no background error, same
    reasoning as the Ctrl+Q test above."""
    seen = _claim_failures(DoxaApp)
    where = tmp_path / "scratch"
    where.mkdir()
    factory = _fake_factory("sid-solo")
    app = DoxaApp(cwd=str(where), engine_factory=factory, new_session_factory=factory)
    async with app.run_test() as pilot:
        assert await _wait(pilot, lambda: app.panes()[0]._session_id)
        await pilot.press("ctrl+w")
        await pilot.pause()
    assert seen == [], [f.headline() for f in seen]
    record = tabsets.load(str(where))
    assert record is None or record.tabs == ()


@pytest.mark.asyncio
async def test_peer_pump_tolerates_the_engine_already_being_gone(tmp_path):
    """Direct, deterministic regression test for the race the two tests
    above catch only intermittently: SessionPane._peer_pump is created
    (run_worker, in on_mount) alongside _boot but is not guaranteed its
    first actual turn on the event loop before _boot finishes -- and
    _boot sets _session_id (what every close-path test here waits on)
    BEFORE it sets _engine_ready (a naming-cache lookup sits between the
    two). A close landing in that window clears self.engine
    (detach()/stop() both do, immediately) without cancelling this
    worker, so by the time _engine_ready releases it, the engine it was
    about to assert on is already gone. Called directly here (bypassing
    the timing entirely) so the regression is a fast, deterministic
    failure rather than a stress test that only sometimes reproduces."""
    where = tmp_path / "scratch"
    where.mkdir()
    factory = _fake_factory("sid-solo")
    app = DoxaApp(cwd=str(where), engine_factory=factory, new_session_factory=factory)
    async with app.run_test() as pilot:
        assert await _wait(pilot, lambda: app.panes()[0]._session_id)
        pane = app.panes()[0]
        # _engine_ready is already set (boot completed); this simulates
        # detach()/stop() clearing the handle in the race window above.
        pane.engine = None
        await pane._peer_pump()  # must return quietly, not raise


@pytest.mark.asyncio
async def test_ctrl_q_ended_session_is_not_in_resolve_at_all(tmp_path):
    """Closes the loop test_ctrl_q_drops_the_ended_session_from_the_record
    only opens, and is the direct "simulated next launch" check: since
    sid-b was never written into the persisted record, resolve() -- what
    the CLI's own triage reads on the next launch -- cannot hand it back
    EITHER way. Not live (no daemon, Ctrl+Q's whole point) -- unsurprising
    -- but also not ARCHIVED, even with its transcript sitting right there
    on disk (written below exactly as Ctrl+Q's own finalize path would for
    real, and doxa.transcript.exists confirms it). Pre-v0.99.1 that
    transcript is exactly what made resolve() hand sid-b back as an
    archived tab (doxa.tabsets.resolve's own "Restore is a cross-check"
    docstring), and doxa.cli.ended_tab_spec would then have called
    history_mod.resume_state on it and, finding the CLI's own history
    store still holding the conversation, handed it back LIVE instead --
    the exact defect reported. None of that runs for a session that was
    never in the record to begin with. The transcript itself is
    untouched: this fix changes the AUTO-RESTORE set, never history."""
    from doxa import transcript as transcript_mod

    where = tmp_path / "scratch"
    where.mkdir()
    ids = iter(["sid-a", "sid-b"])

    def factory() -> FakeEngine:
        engine = FakeEngine([])
        engine.session_id = next(ids)
        return engine

    app = DoxaApp(cwd=str(where), engine_factory=factory, new_session_factory=factory)
    async with app.run_test() as pilot:
        assert await _wait(pilot, lambda: app.panes()[0]._session_id)
        await pilot.press("ctrl+t")
        assert await _wait(
            pilot, lambda: len(app.panes()) == 2 and app.panes()[1]._session_id
        )
        await pilot.press("ctrl+q")
        assert await _wait(pilot, lambda: len(app.panes()) == 1)
        await pilot.pause()
    # A transcript for EACH session -- sid-a's because a FakeEngine never
    # registers a real peer-registry entry either, so without one it would
    # resolve as skipped for a reason that has nothing to do with this
    # test (a unit-test artifact, not a live daemon DOXA lost track of);
    # sid-b's is what Ctrl+Q's own finalize path would have written for
    # real, and is the whole point here: the transcript SURVIVES.
    for sid in ("sid-a", "sid-b"):
        path = transcript_mod.transcript_path(sid, str(where))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '{"type": "user", "message": {"content": "hi"}}\n', encoding="utf-8"
        )
    assert transcript_mod.exists("sid-b", str(where))

    resolved = tabsets.resolve(str(where))
    assert resolved is not None
    assert {t.session_id for t in resolved.archived} == {"sid-a"}
    assert {t.session_id for t, _ in resolved.tabs} == set()
    assert resolved.skipped == 0


@pytest.mark.asyncio
async def test_quit_stop_drops_the_stopped_tab_keeps_the_detached_one(tmp_path):
    """action_quit_stop (palette 'Quit: stop session', ALL tabs): the
    whole-window mirror of the single-tab tests above, and it needed no
    dedicated fix -- v0.99.1's exclusion lives in _persist_tabset's own
    mounted-pane scan (a _stopped pane is skipped), which this method's
    one _persist_tabset() call already goes through. A pane the user
    detached_on_purpose is never marked _stopped (detach() only clears
    the engine handle), so it survives exactly as item D #4 always
    promised; the pane this loop actually stopped does not."""
    where = tmp_path / "scratch"
    where.mkdir()
    ids = iter(["sid-a", "sid-b"])

    def factory() -> FakeEngine:
        engine = FakeEngine([])
        engine.session_id = next(ids)
        return engine

    app = DoxaApp(cwd=str(where), engine_factory=factory, new_session_factory=factory)
    async with app.run_test() as pilot:
        assert await _wait(pilot, lambda: app.panes()[0]._session_id)
        await pilot.press("ctrl+t")
        assert await _wait(
            pilot, lambda: len(app.panes()) == 2 and app.panes()[1]._session_id
        )
        first_pane = app.panes()[0]
        first_pane.detached_on_purpose = True
        await app.action_quit_stop()
    record = tabsets.load(str(where))
    assert {t.session_id for t in record.tabs} == {"sid-a"}


# -- an explicit kill is the one veto ------------------------------------


@pytest.mark.asyncio
async def test_kill_evicts_a_session_already_recorded_as_detached(tmp_path):
    """Ctrl+W THEN `/sessions kill <prefix>`: the record already carries
    sid-b (detached, item D #4) by the time the kill lands. Reaping it on
    purpose is the one gesture this app treats as "forget this
    conversation" -- if the veto did not exist, this session would
    resurrect at the next launch despite having been explicitly killed,
    since /sessions kill stops a daemon over its own socket and never
    goes anywhere near _detached_this_run."""
    where = tmp_path / "scratch"
    where.mkdir()
    ids = iter(["sid-a", "sid-b"])

    def factory() -> FakeEngine:
        engine = FakeEngine([])
        engine.session_id = next(ids)
        return engine

    app = DoxaApp(cwd=str(where), engine_factory=factory, new_session_factory=factory)
    async with app.run_test() as pilot:
        assert await _wait(pilot, lambda: app.panes()[0]._session_id)
        await pilot.press("ctrl+t")
        assert await _wait(
            pilot, lambda: len(app.panes()) == 2 and app.panes()[1]._session_id
        )
        await pilot.press("ctrl+w")  # detach tab two -- sid-b stays running
        assert await _wait(pilot, lambda: len(app.panes()) == 1)
        record = tabsets.load(str(where))
        assert {t.session_id for t in record.tabs} == {"sid-a", "sid-b"}

        sock = _listening_daemon_entry("sid-b", str(where))
        pane = app.active_pane
        try:
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr("doxa.app._stop_session", lambda entry: True)
                await pane._cmd_sessions("kill sid-b")
                await pilot.pause()
        finally:
            sock.close()
    record = tabsets.load(str(where))
    assert [t.session_id for t in record.tabs] == ["sid-a"]


@pytest.mark.asyncio
async def test_kill_evicts_a_session_still_attached_in_a_tab_of_this_window(
    tmp_path,
):
    """`/sessions kill <prefix>` never excludes sessions attached HERE --
    only kill-detached does (see _kill_sessions). Kill the ACTIVE pane's
    own session by prefix: its pane stays mounted (nothing tears the tab
    down), never marked _stopped, so without the veto _persist_tabset's
    per-pane scan would still read it as an ordinary LIVE tab and persist
    it as if nothing happened."""
    where = tmp_path / "scratch"
    where.mkdir()
    engine = FakeEngine([])
    engine.session_id = "sid-only"
    app = DoxaApp(
        cwd=str(where), engine_factory=lambda: engine,
        new_session_factory=lambda: engine,
    )
    async with app.run_test() as pilot:
        assert await _wait(pilot, lambda: app.panes()[0]._session_id)
        await pilot.pause()
        record = tabsets.load(str(where))
        assert [t.session_id for t in record.tabs] == ["sid-only"]

        sock = _listening_daemon_entry("sid-only", str(where))
        pane = app.active_pane
        try:
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr("doxa.app._stop_session", lambda entry: True)
                await pane._cmd_sessions("kill sid-only")
                await pilot.pause()
        finally:
            sock.close()
        # Confirms the pane really is still sitting in the strip, unaware
        # -- the veto is doing the work, not a teardown this command never
        # performs on the pane that happened to hold the killed session.
        assert len(app.panes()) == 1
    record = tabsets.load(str(where))
    assert record is None  # the only tab there ever was, killed and gone


@pytest.mark.asyncio
async def test_quit_detach_keeps_every_tab_in_the_record(tmp_path):
    where = tmp_path / "scratch"
    where.mkdir()
    ids = iter(["sid-a", "sid-b"])

    def factory() -> FakeEngine:
        engine = FakeEngine([])
        engine.session_id = next(ids)
        return engine

    app = DoxaApp(cwd=str(where), engine_factory=factory, new_session_factory=factory)
    async with app.run_test() as pilot:
        assert await _wait(pilot, lambda: app.panes()[0]._session_id)
        await pilot.press("ctrl+t")
        assert await _wait(
            pilot, lambda: len(app.panes()) == 2 and app.panes()[1]._session_id
        )
        await app.action_quit()
    record = tabsets.load(str(where))
    assert [t.session_id for t in record.tabs] == ["sid-a", "sid-b"]


# -- doxa.app wiring: restore on launch ----------------------------------


@pytest.mark.asyncio
async def test_restore_tabs_open_in_saved_order_with_names_and_active_tab(tmp_path):
    where = tmp_path / "scratch"
    where.mkdir()
    specs = [
        RestoreTabSpec("sid-1", _fake_factory("sid-1"), pinned_name="alpha"),
        RestoreTabSpec("sid-2", _fake_factory("sid-2"), pinned_name=None),
    ]
    app = DoxaApp(
        cwd=str(where),
        restore_tabs=specs,
        restore_active_id="sid-2",
        restore_report="tab restore: restored 2 tabs.",
    )
    async with app.run_test() as pilot:
        assert await _wait(
            pilot, lambda: len(app.panes()) == 2 and all(p._session_id for p in app.panes())
        )
        panes = app.panes()
        assert [p._session_id for p in panes] == ["sid-1", "sid-2"]
        assert panes[0].custom_name == "alpha"
        assert panes[1].custom_name is None
        assert app.active_pane is panes[1]

        def _report_blocks(pane):
            return [
                b.text for b in pane.query(SystemBlock) if b.id != "identity-block"
            ]

        assert await _wait(pilot, lambda: _report_blocks(panes[0]))
        assert any("restored 2 tabs" in text for text in _report_blocks(panes[0]))
        assert _report_blocks(panes[1]) == []  # the report lands ONCE
        await pilot.pause()
    # The restored set was persisted once it fully settled, complete --
    # never a truncated write reflecting only whichever tab booted first.
    record = tabsets.load(str(where))
    assert [t.session_id for t in record.tabs] == ["sid-1", "sid-2"]
    assert record.active_session_id == "sid-2"


@pytest.mark.asyncio
async def test_the_active_tab_survives_a_persist_that_beats_activation(tmp_path):
    """v0.38.0's write-ordering race, made deterministic.

    A restore's FIRST save is triggered by the last restored pane
    reporting its session id (_note_pane_booted), and Textual resolves
    which tab is active ASYNCHRONOUSLY: ``TabbedContent.active`` is the
    empty string it starts as until the inner ``Tabs`` widget's own mount
    picks a tab. A pane that boots inside that window persisted with
    ``active_session_id: null`` -- tabs complete, correctly ordered, and
    the memory of which one you were on simply gone. Measured as 1 failure
    in 80 runs of the test above with four suites in parallel; the
    signature was always null, never a wrong id.

    Reproduced here by putting the strip back into the unresolved state
    (observed directly before the fix) and driving the SAME trigger."""
    where = tmp_path / "scratch"
    where.mkdir()
    specs = [
        RestoreTabSpec("sid-1", _fake_factory("sid-1")),
        RestoreTabSpec("sid-2", _fake_factory("sid-2")),
        RestoreTabSpec("sid-3", _fake_factory("sid-3")),
    ]
    app = DoxaApp(cwd=str(where), restore_tabs=specs, restore_active_id="sid-2")
    async with app.run_test() as pilot:
        assert await _wait(
            pilot,
            lambda: len(app.panes()) == 3 and all(p._session_id for p in app.panes()),
        )
        tabbed = app.query_one("#session-tabs", TabbedContent)
        tabbed.active = ""
        assert tabbed.active_pane is None  # exactly what a booting pane sees

        app._restore_pending = 1
        app._note_pane_booted(app.panes()[-1])

        record = tabsets.load(str(where))
        assert [t.session_id for t in record.tabs] == ["sid-1", "sid-2", "sid-3"]
        assert record.active_session_id == "sid-2"


def test_initial_active_tab_id_is_decided_before_anything_mounts():
    """A SECOND write-ordering race, distinct from the one fixed above.

    ``test_the_active_tab_survives_a_persist_that_beats_activation`` pins
    v0.38.0's race: a restored pane boots and persists before Textual has
    resolved ANY active tab, so ``_persist_tabset`` has to fall back to
    the restored id. This test's failure was different -- ``assert
    'sid-1' == 'sid-2'``, a WRONG id, not a missing one -- which means
    something DID resolve an active tab, just the wrong one.

    Measured cause, reading Textual's own ``Tabs``/``TabbedContent``:
    ``Tabs`` defaults itself to its first child tab on ITS OWN mount
    whenever nothing else names one, and that default reaches
    ``TabbedContent.active`` as a QUEUED MESSAGE
    (``Tabs.TabActivated`` -> ``_on_tabs_tab_activated``), not a
    synchronous write. The old ``_activate_initial_tab`` set
    ``tabbed.active`` directly from ``App.on_mount``, which usually runs
    after that default -- but the queued message from the default does
    not stop existing just because something else wrote ``active`` in the
    meantime; whenever it is finally handled, it overwrites ``active``
    with whatever tab IT named, unconditionally. Two writers, and under
    load the wrong one can land last.

    The fix -- ``_initial_active_tab_id`` -- removes the SECOND writer
    instead of racing it: it is read by ``compose()`` and handed to
    ``TabbedContent`` as ``initial=``, so ``Tabs`` never defaults to the
    wrong tab in the first place and there is no stray message left to
    land late. This test pins the SELECTION alone -- a pure function of
    ``_restore_tabs``/``_restore_active_id``, decided before any widget
    exists, so it needs no pilot and cannot itself race."""
    def app_for(specs, active_id):
        return DoxaApp(cwd=".", restore_tabs=specs, restore_active_id=active_id)

    # No restore at all: a single pane, Tabs' own default is already
    # right, so there is nothing to name.
    assert app_for([], None)._initial_active_tab_id() == ""

    live1 = RestoreTabSpec("sid-1", _fake_factory("sid-1"))
    live2 = RestoreTabSpec("sid-2", _fake_factory("sid-2"))
    archived1 = RestoreTabSpec("sid-a", None, archived=True)

    # The saved active tab, when the record named one and it live.
    app = app_for([live1, live2], "sid-2")
    assert app._initial_active_tab_id() == "restore-sid-2"

    # The saved active tab even when IT is the archived one -- "live pane
    # or archived tab alike, it is where the user was".
    app = app_for([archived1, live1], "sid-a")
    assert app._initial_active_tab_id() == "restore-sid-a"

    # No saved active id: the first SESSION spec, archived ones skipped.
    app = app_for([archived1, live1, live2], None)
    assert app._initial_active_tab_id() == "restore-sid-1"

    # A saved active id that names a tab NOT in this restore: falls back
    # to the same "first session spec" rule as no id at all.
    app = app_for([live1, live2], "sid-does-not-exist")
    assert app._initial_active_tab_id() == "restore-sid-1"

    # Every resolved tab archived: the fixed fallback id, matching the one
    # compose() gives the fresh pane it adds in that exact case.
    app = app_for([archived1], None)
    assert app._initial_active_tab_id() == app._FALLBACK_PANE_ID


@pytest.mark.asyncio
async def test_a_later_tab_switch_still_wins_over_the_restored_active_id(tmp_path):
    """The fallback above must not outlive the window it exists for: once
    activation HAS resolved, the active tab is whatever the user is
    actually on, restored id or not."""
    where = tmp_path / "scratch"
    where.mkdir()
    specs = [
        RestoreTabSpec("sid-1", _fake_factory("sid-1")),
        RestoreTabSpec("sid-2", _fake_factory("sid-2")),
    ]
    app = DoxaApp(cwd=str(where), restore_tabs=specs, restore_active_id="sid-2")
    async with app.run_test() as pilot:
        assert await _wait(
            pilot,
            lambda: len(app.panes()) == 2 and all(p._session_id for p in app.panes()),
        )
        await pilot.press("ctrl+left")
        await pilot.pause()
        assert app.active_pane._session_id == "sid-1"

        app._persist_tabset()
        record = tabsets.load(str(where))
        assert record.active_session_id == "sid-1"


@pytest.mark.asyncio
async def test_restore_report_lands_on_the_single_fallback_tab(tmp_path):
    """doxa.cli's own fallback (every saved session dead) still passes a
    report through with no restore_tabs at all -- compose()'s non-restore
    branch has to honor _restore_report too."""
    where = tmp_path / "scratch"
    where.mkdir()
    factory = _fake_factory("sid-fresh")
    app = DoxaApp(
        cwd=str(where),
        engine_factory=factory,
        new_session_factory=factory,
        restore_report="tab restore: skipped 2 sessions no longer running.",
    )
    async with app.run_test() as pilot:
        pane = app.active_pane
        assert await _wait(pilot, lambda: pane._session_id)

        def _report_blocks():
            return [b.text for b in pane.query(SystemBlock) if b.id != "identity-block"]

        assert await _wait(pilot, _report_blocks)
        assert any("skipped 2 sessions" in text for text in _report_blocks())

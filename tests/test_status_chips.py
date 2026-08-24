"""Status-chips (item Y and the branch/model click affordance): clicking
the status bar was inert, despite looking like it should do something --
the operator's own report. This file covers the v0.22.0 fix (the model
chip and the git chip's branch span open the SAME shared dropdown,
:class:`doxa.app.ChipPicker`, keyboard nav + type-to-filter narrow it,
Enter/click-select invoke the EXACT SAME coroutine the matching slash
command uses -- assert the call, not just the UI -- Esc and click-away
close it, and the status bar reflects the new value immediately) AND
v0.24.0's revision of three actions the operator reported wrong or missing:
ctx% now CONFIRMS before compacting (a real defect fix -- compaction is
lossy and had no confirm at all), the session-handle chip opens a sessions
picker (live + detached, current marked) instead of just copying, and the
beliefs chip is clickable now (grouped by scope) instead of inert. Item 4
(same release) also overrides v0.22.0's "repo name is INERT" call -- the
repo chip is a SELECTOR too now, opening a directory-walking picker.

Same headless Pilot + FakeEngine pattern as tests/test_branch_command.py
and tests/test_subagent_tracker.py. Click coordinates are resolved from
the status bar's PLAIN (markup-stripped) text via
``textual.content.Content.from_markup(...).plain`` -- `.renderable` on a
Static is the raw string handed to `update()`, brackets and all, so a
naive string index would land on the wrong screen column once a chip
carries `[@click=...]` markup.
"""

from __future__ import annotations

import os
import subprocess

import pytest
from textual.content import Content

from doxa import peers as peers_mod
from doxa.app import ChipPicker, CompactConfirm, DoxaApp, StatusBar, SystemBlock
from tests.fakes import FakeEngine


def _repo(tmp_path, branch="trunk", name="myrepo"):
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", branch, str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    (repo / "f.txt").write_text("one", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "one"], check=True)
    return repo


def _system_texts(app) -> list[str]:
    return [b.text for b in app.query(SystemBlock) if b.id != "identity-block"]


async def _app(monkeypatch, cwd, fake=None, *, factory_at=None):
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(cwd) + "-rt")
    engines: list[FakeEngine] = []

    def make():
        engines.append(fake or FakeEngine([]))
        return engines[-1]

    def make_at(path):
        # item 4's repo picker: a safe FAKE stand-in for
        # DoxaApp's own default (a real SessionEngine) -- tests that
        # exercise open_tab_at for real (rather than spying on it) must
        # never spawn an actual claude CLI subprocess.
        engines.append(FakeEngine([]))
        return engines[-1]

    app = DoxaApp(
        cwd=str(cwd), engine_factory=make, new_session_factory=make,
        new_session_factory_at=factory_at or make_at,
    )
    return app, engines


async def _settled(pilot, pane, tries=200):
    for _ in range(tries):
        if pane._git is not None:
            return True
        await pilot.pause(0.02)
    return pane._git is not None


def _status_plain(app) -> str:
    return Content.from_markup(str(app.query_one("#status-bar").renderable)).plain


def _offset_of(app, needle: str) -> tuple[int, int]:
    """A click coordinate landing on `needle`'s first character --
    ``#status-bar``'s own `padding: 0 2` (theme.tcss) means content starts
    at x=2 within the widget's own region, same as SubagentLine's
    established click-offset convention."""
    idx = _status_plain(app).index(needle)
    return (2 + idx, 0)


async def _wait_status(pilot, app, needle: str, tries=200) -> bool:
    for _ in range(tries):
        if needle in _status_plain(app):
            return True
        await pilot.pause(0.02)
    return needle in _status_plain(app)


# -- clicking chips opens/does not open the picker -----------------------


@pytest.mark.asyncio
async def test_click_on_model_chip_opens_the_picker(monkeypatch, tmp_path):
    fake = FakeEngine([], model="claude-haiku-4-5")
    app, _engines = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test() as pilot:
        assert await _wait_status(pilot, app, "claude-haiku-4-5")
        picker = app.query_one("#chip-picker", ChipPicker)
        assert not picker.is_open
        await pilot.click("#status-bar", offset=_offset_of(app, "claude-haiku-4-5"))
        await pilot.pause()
        assert picker.is_open
        assert picker.border_title == "model"


@pytest.mark.asyncio
async def test_click_on_branch_chip_opens_the_picker(monkeypatch, tmp_path):
    repo = _repo(tmp_path)
    app, _engines = await _app(monkeypatch, repo)
    async with app.run_test() as pilot:
        pane = app.active_pane
        assert await _settled(pilot, pane)
        assert await _wait_status(pilot, app, "trunk")
        picker = app.query_one("#chip-picker", ChipPicker)
        await pilot.click("#status-bar", offset=_offset_of(app, "trunk"))
        await pilot.pause()
        assert picker.is_open
        assert picker.border_title == "branch"


@pytest.mark.asyncio
async def test_click_on_repo_name_opens_the_repo_picker(monkeypatch, tmp_path):
    """v0.24.0 item 4 overrides v0.22.0's "repo name is INERT" call -- the
    repo chip is a SELECTOR now (a directory-walking picker, see
    test_repo_picker.py-style coverage below); the sha stays inert, the
    one segment item 4 left untouched."""
    repo = _repo(tmp_path)
    app, _engines = await _app(monkeypatch, repo)
    async with app.run_test() as pilot:
        pane = app.active_pane
        assert await _settled(pilot, pane)
        assert await _wait_status(pilot, app, "myrepo")
        picker = app.query_one("#chip-picker", ChipPicker)

        await pilot.click("#status-bar", offset=_offset_of(app, "myrepo"))
        await pilot.pause()
        assert picker.is_open
        assert picker.border_title.startswith("repo · ")
        await pilot.press("escape")
        await pilot.pause()
        assert not picker.is_open

        plain = _status_plain(app)
        sha_idx = plain.index("@") + 1  # the '@'-prefixed short sha
        await pilot.click("#status-bar", offset=(2 + sha_idx, 0))
        await pilot.pause()
        assert not picker.is_open


# -- keyboard nav, type-to-filter, esc ------------------------------------


@pytest.mark.asyncio
async def test_keyboard_nav_and_type_to_filter_narrows(monkeypatch, tmp_path):
    fake = FakeEngine([], model="claude-haiku-4-5")
    app, _engines = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test() as pilot:
        assert await _wait_status(pilot, app, "claude-haiku-4-5")
        pane = app.active_pane
        await pane.open_model_picker()
        await pilot.pause()
        picker = app.query_one("#chip-picker", ChipPicker)
        assert picker.is_open
        full_count = len(picker._rows)
        assert full_count >= 4  # the fallback alias set: haiku/sonnet/opus/fable

        await pilot.press("s", "o", "n")
        await pilot.pause()
        assert picker._filter_text == "son"
        # rid == "" is the (fallback-tier) note heading -- always present,
        # never a filter candidate; every REAL row left after "son" must
        # match "sonnet".
        real_rows = [(rid, label) for rid, label in picker._rows if rid]
        assert real_rows and all("sonnet" in label.lower() for _rid, label in real_rows)
        assert len(real_rows) < full_count

        # Up/down still work over the narrowed set.
        await pilot.press("down")
        await pilot.pause()
        assert picker.highlighted is not None


@pytest.mark.asyncio
async def test_esc_closes_the_picker(monkeypatch, tmp_path):
    fake = FakeEngine([], model="claude-haiku-4-5")
    app, _engines = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test() as pilot:
        pane = app.active_pane
        await pane.open_model_picker()
        await pilot.pause()
        picker = app.query_one("#chip-picker", ChipPicker)
        assert picker.is_open
        await pilot.press("escape")
        await pilot.pause()
        assert not picker.is_open
        assert app.query_one("#prompt-input").has_focus


# -- selection invokes the SAME path the slash command uses --------------


@pytest.mark.asyncio
async def test_model_picker_selection_calls_the_same_set_model_path(
    monkeypatch, tmp_path,
):
    fake = FakeEngine([], model="claude-sonnet-4-5")
    app, engines = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test() as pilot:
        pane = app.active_pane
        await pane.open_model_picker()
        await pilot.pause()
        picker = app.query_one("#chip-picker", ChipPicker)
        # The fallback tier's rows are the alias tuple; "haiku" is one.
        index = next(i for i, (rid, _l) in enumerate(picker._rows) if rid == "haiku")
        picker.select_row(index)
        for _ in range(100):
            if engines[0].model_switches:
                break
            await pilot.pause(0.02)
        assert engines[0].model_switches == ["haiku"]  # the SAME call /model makes
        assert engines[0].model == "haiku"
        assert not picker.is_open
        assert await _wait_status(pilot, app, "haiku")  # status bar reflects it


@pytest.mark.asyncio
async def test_branch_picker_selection_calls_the_same_switch_branch_path(
    monkeypatch, tmp_path,
):
    repo = _repo(tmp_path)
    fake = FakeEngine([])
    fake.branch_list_result = {
        "branches": ["trunk", "develop"], "base": "trunk", "checked_out": "trunk",
    }
    fake.branch_switch_result = {
        "ok": True, "base": "develop", "message": "doxa/abc123 now based on develop",
    }
    app, engines = await _app(monkeypatch, repo, fake)
    async with app.run_test() as pilot:
        pane = app.active_pane
        assert await _settled(pilot, pane)
        await pane.open_branch_picker()
        await pilot.pause()
        picker = app.query_one("#chip-picker", ChipPicker)
        index = next(i for i, (rid, _l) in enumerate(picker._rows) if rid == "develop")
        picker.select_row(index)
        for _ in range(100):
            if "develop" in engines[0].branch_calls:
                break
            await pilot.pause(0.02)
        # branch_calls[0] is the LISTING call (None) open_branch_picker
        # itself made to populate the rows -- switch_branch(None), the
        # exact call /branch with no argument makes too; the SWITCH is
        # the same call /branch develop makes.
        assert engines[0].branch_calls == [None, "develop"]
        assert _system_texts(app)[-1] == "branch: doxa/abc123 now based on develop"


@pytest.mark.asyncio
async def test_branch_switch_refusal_surfaces_for_a_dirty_worktree(
    monkeypatch, tmp_path,
):
    repo = _repo(tmp_path)
    fake = FakeEngine([])
    fake.branch_list_result = {
        "branches": ["trunk", "develop"], "base": "trunk", "checked_out": "trunk",
    }
    fake.branch_switch_result = {
        "ok": False, "base": None,
        "message": "doxa/abc123 has uncommitted changes -- ...",
    }
    app, engines = await _app(monkeypatch, repo, fake)
    async with app.run_test() as pilot:
        pane = app.active_pane
        assert await _settled(pilot, pane)
        await pane.open_branch_picker()
        await pilot.pause()
        picker = app.query_one("#chip-picker", ChipPicker)
        index = next(i for i, (rid, _l) in enumerate(picker._rows) if rid == "develop")
        picker.select_row(index)
        for _ in range(100):
            if "develop" in engines[0].branch_calls:
                break
            await pilot.pause(0.02)
        assert _system_texts(app)[-1] == (
            "branch: doxa/abc123 has uncommitted changes -- ..."
        )


# -- effort: a SELECTOR, but honest that it cannot touch THIS session ----


@pytest.mark.asyncio
async def test_effort_picker_notes_it_cannot_change_this_session(monkeypatch, tmp_path):
    fake = FakeEngine([], effort="high")
    app, engines = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test() as pilot:
        assert await _wait_status(pilot, app, "effort:high")
        pane = app.active_pane
        await pane.open_effort_picker()
        await pilot.pause()
        picker = app.query_one("#chip-picker", ChipPicker)
        assert picker.is_open
        assert "NEW sessions" in picker._note
        assert "high" in picker._note


# -- ACTIONABLE tier: peers -> /sessions, ctx% -> /compact, handle -> clip --


@pytest.mark.asyncio
async def test_peers_chip_click_runs_the_sessions_command(monkeypatch, tmp_path):
    import os

    from doxa import peers as peers_mod

    now = peers_mod._iso_now()
    peer = peers_mod.PeerInfo(
        session_id="peer00001111", pid=os.getpid(), socket_path="/tmp/peer.sock",
        cwd=str(tmp_path), repo_root=str(tmp_path), title="other",
        started_at=now, heartbeat_at=now,
    )
    fake = FakeEngine([], peers=[peer])
    app, _engines = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test() as pilot:
        assert await _wait_status(pilot, app, "peers 1")
        before = len(_system_texts(app))
        await pilot.click("#status-bar", offset=_offset_of(app, "peers 1"))
        for _ in range(100):
            if len(_system_texts(app)) > before:
                break
            await pilot.pause(0.02)
        # /sessions reads the REAL on-disk registry (empty here, hence
        # "none live") -- a DIFFERENT data source than the chip's own
        # engine.peer_count(); what this test actually pins is that the
        # click ran /sessions (its output always starts with "sessions"),
        # not /peers or anything else.
        assert _system_texts(app)[-1].startswith("sessions")


@pytest.mark.asyncio
async def test_ctx_chip_click_opens_a_confirm_and_does_not_compact_yet(
    monkeypatch, tmp_path,
):
    """Item 1: the real defect. Through v0.22.0 a single click fired
    /compact immediately -- this pins that the CLICK ALONE now sends
    nothing, and a CompactConfirm modal is what's up instead."""
    fake = FakeEngine([])
    fake.last_ctx_percentage = 42.0
    app, engines = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test() as pilot:
        assert await _wait_status(pilot, app, "ctx")
        await pilot.click("#status-bar", offset=_offset_of(app, "ctx"))
        for _ in range(100):
            if isinstance(app.screen, CompactConfirm):
                break
            await pilot.pause(0.02)
        assert isinstance(app.screen, CompactConfirm)
        assert engines[0].received_prompts == []


@pytest.mark.asyncio
async def test_ctx_confirm_accept_sends_compact(monkeypatch, tmp_path):
    fake = FakeEngine([])
    fake.last_ctx_percentage = 91.0
    app, engines = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test() as pilot:
        assert await _wait_status(pilot, app, "ctx")
        await pilot.click("#status-bar", offset=_offset_of(app, "ctx"))
        for _ in range(100):
            if isinstance(app.screen, CompactConfirm):
                break
            await pilot.pause(0.02)
        assert isinstance(app.screen, CompactConfirm)
        body = app.screen.query_one("#compact-confirm-body")
        assert "91%" in str(body.renderable)
        assert "discard" in str(body.renderable).lower()
        await pilot.click("#compact-confirm-yes")
        for _ in range(100):
            if engines[0].received_prompts:
                break
            await pilot.pause(0.02)
        assert engines[0].received_prompts == ["/compact"]


@pytest.mark.asyncio
async def test_ctx_confirm_decline_sends_nothing(monkeypatch, tmp_path):
    fake = FakeEngine([])
    app, engines = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test() as pilot:
        assert await _wait_status(pilot, app, "ctx")
        await pilot.click("#status-bar", offset=_offset_of(app, "ctx"))
        for _ in range(100):
            if isinstance(app.screen, CompactConfirm):
                break
            await pilot.pause(0.02)
        assert isinstance(app.screen, CompactConfirm)
        await pilot.click("#compact-confirm-no")
        await pilot.pause()
        assert not isinstance(app.screen, CompactConfirm)
        await pilot.pause(0.1)
        assert engines[0].received_prompts == []


@pytest.mark.asyncio
async def test_ctx_confirm_esc_sends_nothing(monkeypatch, tmp_path):
    fake = FakeEngine([])
    app, engines = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test() as pilot:
        assert await _wait_status(pilot, app, "ctx")
        await pilot.click("#status-bar", offset=_offset_of(app, "ctx"))
        for _ in range(100):
            if isinstance(app.screen, CompactConfirm):
                break
            await pilot.pause(0.02)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, CompactConfirm)
        await pilot.pause(0.1)
        assert engines[0].received_prompts == []


# -- session-handle chip: a SESSIONS dropdown, item 2 ---------------------


def _daemon_entry(tmp_path, sid, title, clients=0, cwd=None):
    """A live daemon-hosted registry entry with a REAL listening socket
    (list_daemons has no probe=True the peers chip's own count does, but
    the liveness checks -- dead pid, stale heartbeat -- still apply), same
    technique tests/test_sessions.py's own _entry helper uses."""
    import socket as socket_mod

    peers_mod.registry_dir()
    socket_path = str(peers_mod.runtime_dir() / f"peer-{sid[:8]}.sock")
    sock = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
    sock.bind(socket_path)
    sock.listen(1)
    now = peers_mod._iso_now()
    entry = peers_mod.PeerInfo(
        session_id=sid, pid=os.getpid(), socket_path=socket_path,
        cwd=str(cwd or tmp_path), repo_root=str(tmp_path), title=title,
        started_at=now, heartbeat_at=now, daemon_socket=f"/tmp/daemon-{sid[:8]}.sock",
        clients=clients,
    )
    import json as json_mod
    path = peers_mod.registry_dir() / f"{sid}.json"
    data = {
        "session_id": entry.session_id, "pid": entry.pid,
        "socket_path": entry.socket_path, "cwd": entry.cwd,
        "repo_root": entry.repo_root, "title": entry.title,
        "started_at": entry.started_at, "heartbeat_at": entry.heartbeat_at,
        "daemon_socket": entry.daemon_socket, "clients": entry.clients,
    }
    path.write_text(json_mod.dumps(data), encoding="utf-8")
    return entry, sock


@pytest.mark.asyncio
async def test_session_handle_click_opens_sessions_picker_with_detached_marker(
    monkeypatch, tmp_path,
):
    class Detachable(FakeEngine):
        detachable = True
        session_id = "sess-abcdef01"
        cwd = str(tmp_path)

    fake = Detachable([])
    app, _engines = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test() as pilot:
        assert await _wait_status(pilot, app, "session sess-abc")
        entry, sock = _daemon_entry(tmp_path, "aaaa1111live", "other-live", clients=1)
        detached_entry, dsock = _daemon_entry(
            tmp_path, "bbbb2222dead", "other-detached", clients=0,
        )
        try:
            picker = app.query_one("#chip-picker", ChipPicker)
            await pilot.click(
                "#status-bar", offset=_offset_of(app, "session sess-abc"),
            )
            await pilot.pause()
            assert picker.is_open
            assert picker.border_title == "sessions"
            labels = {rid: label for rid, label in picker._rows}
            assert labels.get("__copy__") == "⧉ copy this session's handle"
            assert "⌁ detached" in labels.get("bbbb2222dead", "")
            assert "⌁ detached" not in labels.get("aaaa1111live", "")
        finally:
            sock.close()
            dsock.close()


@pytest.mark.asyncio
async def test_sessions_picker_copy_row_copies_handle(monkeypatch, tmp_path):
    class Detachable(FakeEngine):
        detachable = True
        session_id = "sess-abcdef01"
        cwd = str(tmp_path)

    fake = Detachable([])
    app, _engines = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test() as pilot:
        assert await _wait_status(pilot, app, "session sess-abc")
        copied = []
        monkeypatch.setattr(app, "copy_to_clipboard", lambda text: copied.append(text))
        pane = app.active_pane
        pane.open_sessions_picker()
        await pilot.pause()
        picker = app.query_one("#chip-picker", ChipPicker)
        index = next(i for i, (rid, _l) in enumerate(picker._rows) if rid == "__copy__")
        picker.select_row(index)
        for _ in range(100):
            if copied:
                break
            await pilot.pause(0.02)
        assert copied == ["sess-abcdef01"]


@pytest.mark.asyncio
async def test_sessions_picker_current_row_is_a_noop(monkeypatch, tmp_path):
    class Detachable(FakeEngine):
        detachable = True
        session_id = "sess-abcdef01"
        cwd = str(tmp_path)

    fake = Detachable([])
    app, _engines = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test() as pilot:
        assert await _wait_status(pilot, app, "session sess-abc")
        entry, sock = _daemon_entry(tmp_path, "sess-abcdef01", "this-one", clients=1)
        try:
            pane = app.active_pane
            pane.open_sessions_picker()
            await pilot.pause()
            picker = app.query_one("#chip-picker", ChipPicker)
            # ChipPicker's OWN current-id marker (the same ▸ every other
            # picker uses) -- pins that item 2's "mark the current session"
            # requirement rides the existing mechanism, not a new one.
            assert picker._current_id == "sess-abcdef01"
            index = next(
                i for i, (rid, _l) in enumerate(picker._rows) if rid == "sess-abcdef01"
            )
            picker.select_row(index)
            await pilot.pause(0.1)
            # No crash, no attach call, nothing switched -- still on the
            # same (only) tab.
            assert len(app.panes()) == 1
        finally:
            sock.close()


@pytest.mark.asyncio
async def test_sessions_picker_detached_row_calls_the_existing_attach_path(
    monkeypatch, tmp_path,
):
    class Detachable(FakeEngine):
        detachable = True
        session_id = "sess-abcdef01"
        cwd = str(tmp_path)

    fake = Detachable([])
    app, _engines = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test() as pilot:
        assert await _wait_status(pilot, app, "session sess-abc")
        entry, sock = _daemon_entry(
            tmp_path, "bbbb2222dead", "other-detached", clients=0,
        )
        try:
            calls = []
            monkeypatch.setattr(
                app, "_cmd_attach", lambda e: calls.append(e.session_id),
            )
            pane = app.active_pane
            pane.open_sessions_picker()
            await pilot.pause()
            picker = app.query_one("#chip-picker", ChipPicker)
            index = next(
                i for i, (rid, _l) in enumerate(picker._rows)
                if rid == "bbbb2222dead"
            )
            picker.select_row(index)
            await pilot.pause()
            # Asserts the CALL, not just the UI -- the same path `doxa
            # attach` / the palette's "Attach: ..." entries use, never a
            # second attach implementation.
            assert calls == ["bbbb2222dead"]
        finally:
            sock.close()


# -- beliefs chip: grouped, filterable, lazy -- item 3 --------------------


@pytest.mark.asyncio
async def test_beliefs_chip_click_opens_grouped_picker(monkeypatch, tmp_path):
    fake = FakeEngine([])
    fake.list_beliefs_result = [
        {"id": 1, "subject": "user", "claim": "prefers terse commits", "confidence": 0.9},
        {"id": 2, "subject": "project:doxa", "claim": "uses uv for deps", "confidence": 0.8},
        {"id": 3, "subject": "user-model", "claim": "answers in house voice", "confidence": 0.7},
    ]
    app, _engines = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test() as pilot:
        # FakeEngine.belief_count() is a fixed 3 (engine parity, see
        # tests/fakes.py) -- the chip's COUNT and its picker's BODIES are
        # deliberately two different calls (cost discipline, item 3).
        assert await _wait_status(pilot, app, "3 beliefs")
        await pilot.click("#status-bar", offset=_offset_of(app, "3 beliefs"))
        await pilot.pause()
        picker = app.query_one("#chip-picker", ChipPicker)
        assert picker.is_open
        assert picker.border_title == "beliefs"
        headers = [label for rid, label in picker._rows if not rid]
        assert any("project" in h for h in headers)
        assert any("user" in h for h in headers)
        assert any("user model" in h for h in headers)
        # Cost discipline: list_beliefs() is a CLICK-only call.
        assert fake.list_beliefs_calls == 1


@pytest.mark.asyncio
async def test_beliefs_chip_never_loads_bodies_on_status_refresh(monkeypatch, tmp_path):
    fake = FakeEngine([])
    fake.list_beliefs_result = [
        {"id": 1, "subject": "user", "claim": "x", "confidence": 0.5},
    ]
    app, _engines = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test() as pilot:
        assert await _wait_status(pilot, app, "3 beliefs")
        pane = app.active_pane
        for _ in range(5):
            pane._refresh_status()
            await pilot.pause(0.01)
        assert fake.list_beliefs_calls == 0
        await pane.open_beliefs_picker()
        await pilot.pause()
        assert fake.list_beliefs_calls == 1


@pytest.mark.asyncio
async def test_beliefs_picker_type_to_filter_matches_claim_text(monkeypatch, tmp_path):
    fake = FakeEngine([])
    fake.list_beliefs_result = [
        {"id": 1, "subject": "user", "claim": "prefers terse commits", "confidence": 0.9},
        {"id": 2, "subject": "project:doxa", "claim": "uses uv for deps", "confidence": 0.8},
    ]
    app, _engines = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test() as pilot:
        pane = app.active_pane
        await pane.open_beliefs_picker()
        await pilot.pause()
        picker = app.query_one("#chip-picker", ChipPicker)
        await pilot.press("u", "v")
        await pilot.pause()
        real_rows = [(rid, label) for rid, label in picker._rows if rid]
        assert real_rows and all("uv" in label for _rid, label in real_rows)


@pytest.mark.asyncio
async def test_beliefs_picker_selection_shows_detail_inline(monkeypatch, tmp_path):
    fake = FakeEngine([])
    fake.list_beliefs_result = [
        {"id": 7, "subject": "user", "claim": "loves caveman commits", "confidence": 0.42},
    ]
    app, _engines = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test() as pilot:
        pane = app.active_pane
        await pane.open_beliefs_picker()
        await pilot.pause()
        picker = app.query_one("#chip-picker", ChipPicker)
        index = next(i for i, (rid, _l) in enumerate(picker._rows) if rid == "belief:7")
        picker.select_row(index)
        for _ in range(100):
            if _system_texts(app):
                break
            await pilot.pause(0.02)
        assert "loves caveman commits" in _system_texts(app)[-1]
        assert "0.42" in _system_texts(app)[-1]


# -- repo chip: SELECTOR with path autocomplete, item 4 -------------------


@pytest.mark.asyncio
async def test_repo_picker_lists_dirs_and_marks_repos(monkeypatch, tmp_path):
    repo = _repo(tmp_path, name="myrepo")
    (tmp_path / "just_a_folder").mkdir()  # scenery: a plain, non-repo sibling
    app, _engines = await _app(monkeypatch, repo)
    async with app.run_test() as pilot:
        pane = app.active_pane
        assert await _settled(pilot, pane)
        pane.open_repo_picker()
        await pilot.pause()
        picker = app.query_one("#chip-picker", ChipPicker)
        # This directory IS the repo itself -- the "open here" pinned row.
        assert any(rid.startswith("repo:") and "open here" in label
                   for rid, label in picker._rows)


@pytest.mark.asyncio
async def test_repo_picker_marks_child_repos_and_plain_dirs(monkeypatch, tmp_path):
    parent = tmp_path / "workspace"
    parent.mkdir()
    child_repo = parent / "childrepo"
    child_repo.mkdir()
    subprocess.run(["git", "init", "-q", str(child_repo)], check=True)
    (parent / "not_a_repo").mkdir()
    app, _engines = await _app(monkeypatch, parent)
    async with app.run_test() as pilot:
        pane = app.active_pane
        pane.open_repo_picker()
        await pilot.pause()
        picker = app.query_one("#chip-picker", ChipPicker)
        repo_row = next(
            (rid, label) for rid, label in picker._rows
            if rid.startswith("repo:") and "childrepo" in label
        )
        dir_row = next(
            (rid, label) for rid, label in picker._rows
            if rid.startswith("dir:") and "not_a_repo" in label
        )
        assert repo_row[0] == f"repo:{child_repo}"
        assert dir_row[0] == f"dir:{parent / 'not_a_repo'}"


@pytest.mark.asyncio
async def test_repo_picker_type_to_filter(monkeypatch, tmp_path):
    parent = tmp_path / "workspace"
    parent.mkdir()
    (parent / "alpha").mkdir()
    (parent / "beta").mkdir()
    app, _engines = await _app(monkeypatch, parent)
    async with app.run_test() as pilot:
        pane = app.active_pane
        pane.open_repo_picker()
        await pilot.pause()
        picker = app.query_one("#chip-picker", ChipPicker)
        await pilot.press("a", "l", "p", "h")
        await pilot.pause()
        real_rows = [(rid, label) for rid, label in picker._rows if rid]
        assert real_rows and all("alph" in label for _rid, label in real_rows)


@pytest.mark.asyncio
async def test_repo_picker_descend_relists_the_child_directory(monkeypatch, tmp_path):
    parent = tmp_path / "workspace"
    parent.mkdir()
    child = parent / "alpha"
    child.mkdir()
    (child / "grandchild").mkdir()
    app, _engines = await _app(monkeypatch, parent)
    async with app.run_test() as pilot:
        pane = app.active_pane
        pane.open_repo_picker()
        await pilot.pause()
        picker = app.query_one("#chip-picker", ChipPicker)
        index = next(
            i for i, (rid, _l) in enumerate(picker._rows) if rid == f"dir:{child}"
        )
        picker.select_row(index)
        await pilot.pause()
        assert picker.is_open  # re-opened at the new listing, not closed
        assert str(child) in picker.border_title
        assert any(
            rid == f"dir:{child / 'grandchild'}" for rid, _l in picker._rows
        )


@pytest.mark.asyncio
async def test_repo_picker_selecting_a_repo_calls_the_existing_spawn_path(
    monkeypatch, tmp_path,
):
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    repo = _repo(workdir, name="target")  # a CHILD of workdir, so it shows
    # up in the picker's very first listing without needing to navigate.
    app, _engines = await _app(monkeypatch, workdir)
    async with app.run_test() as pilot:
        pane = app.active_pane
        calls = []

        async def fake_open_tab_at(path):
            calls.append(path)
            return None

        monkeypatch.setattr(app, "open_tab_at", fake_open_tab_at)
        pane.open_repo_picker()
        await pilot.pause()
        picker = app.query_one("#chip-picker", ChipPicker)
        index = next(
            i for i, (rid, _l) in enumerate(picker._rows) if rid == f"repo:{repo}"
        )
        picker.select_row(index)
        for _ in range(100):
            if calls:
                break
            await pilot.pause(0.02)
        assert calls == [str(repo)]


@pytest.mark.asyncio
async def test_repo_picker_invalid_path_reports_cleanly_not_a_crash(
    monkeypatch, tmp_path,
):
    app, _engines = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        pane = app.active_pane
        before = len(_system_texts(app))
        pane._open_repo_picker_at(str(tmp_path / "does_not_exist"))
        await pilot.pause()
        assert len(_system_texts(app)) > before
        assert "not a directory" in _system_texts(app)[-1]


@pytest.mark.asyncio
async def test_open_tab_at_new_session_factory_called_with_chosen_path(
    monkeypatch, tmp_path,
):
    """Item 4's spawn call, one level down: DoxaApp.open_tab_at drives
    ``_new_session_factory_at`` (parametrized, never re-derived) rather
    than a second spawn implementation."""
    repo = _repo(tmp_path, name="target")
    app, engines = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        calls = []
        original = app._new_session_factory_at

        def spy(path):
            calls.append(path)
            return original(path)

        app._new_session_factory_at = spy
        error = await app.open_tab_at(str(repo))
        await pilot.pause()
        assert error is None
        assert calls == [str(repo)]
        assert len(app.panes()) == 2


# -- tooltips: every chip explains itself, item 5 --------------------------


@pytest.mark.asyncio
async def test_every_chip_has_a_nonempty_tooltip(monkeypatch, tmp_path):
    repo = _repo(tmp_path)
    fake = FakeEngine([], model="claude-haiku-4-5")
    app, _engines = await _app(monkeypatch, repo, fake)
    async with app.run_test() as pilot:
        pane = app.active_pane
        assert await _settled(pilot, pane)
        assert await _wait_status(pilot, app, "myrepo")
        bar = app.query_one("#status-bar", StatusBar)
        assert bar._chip_hints  # something was built at all
        for text, hint in bar._chip_hints:
            assert text.strip()
            assert hint.strip()
        # Resolve a couple of chips by their known x-offset (the SAME
        # convention click tests use) and check the RIGHT tooltip comes
        # back, not just "some" tooltip.
        model_x, _y = _offset_of(app, "claude-haiku-4-5")
        assert "model" in (bar._tooltip_for_x(model_x) or "").lower()
        repo_x, _y = _offset_of(app, "myrepo")
        assert "repo" in (bar._tooltip_for_x(repo_x) or "").lower()


@pytest.mark.asyncio
async def test_chip_order_preserved_after_tooltip_wiring(monkeypatch, tmp_path):
    """v0.22.0's own click-order pin, still true after item 5's tooltip
    machinery: model first, git chip second when present."""
    repo = _repo(tmp_path)
    fake = FakeEngine([], model="claude-haiku-4-5")
    app, _engines = await _app(monkeypatch, repo, fake)
    async with app.run_test() as pilot:
        pane = app.active_pane
        assert await _settled(pilot, pane)
        assert await _wait_status(pilot, app, "myrepo")
        plain = _status_plain(app)
        assert plain.index("claude-haiku-4-5") < plain.index("myrepo")
        assert plain.index("myrepo") < plain.index("trunk")


# -- doxa.providers: the model-list resolution order ----------------------


@pytest.mark.asyncio
async def test_claude_provider_falls_back_without_an_api_key(monkeypatch):
    from doxa.providers import ClaudeProvider

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    provider = ClaudeProvider()
    models = await provider.list_models()
    assert [m.id for m in models] == ["haiku", "sonnet", "opus", "fable"]
    assert all(m.source == "fallback" for m in models)


@pytest.mark.asyncio
async def test_claude_provider_caches_across_calls(monkeypatch):
    from doxa.providers import ClaudeProvider

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    provider = ClaudeProvider()
    first = await provider.list_models()
    second = await provider.list_models()
    assert first is second  # same list object: no re-resolution


@pytest.mark.asyncio
async def test_claude_provider_skips_the_api_probe_without_a_key(monkeypatch):
    """No ANTHROPIC_API_KEY in env -> _try_api must not even attempt an
    import/call (DOXA's normal OAuth posture, see doxa/providers.py's own
    module docstring for the empirical finding this pins)."""
    from doxa.providers import ClaudeProvider

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    provider = ClaudeProvider()
    result = await provider._try_api()
    assert result is None

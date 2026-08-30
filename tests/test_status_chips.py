# SPDX-License-Identifier: AGPL-3.0-only
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
from tests.helpers import _chip_offset


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
    """A click coordinate landing on `needle` -- delegates to
    :func:`tests.helpers._chip_offset`, which finds `needle` one whole
    CHIP at a time (in paint order) rather than a bare
    ``_status_plain(app).index(needle)`` against the whole bar. Outside a
    git repo the bar also carries a `dir <cwd name>` chip (GitLine.
    folder_label), and under pytest `<cwd name>` IS the running test's own
    name -- a plain `.index()` can land INSIDE that chip's text instead of
    the real target (measured: `test_ctx_confirm_esc_sends_nothing`'s own
    name contains "ctx", so a bare "ctx" needle used to click the folder
    chip instead of the ctx chip and silently never opened the confirm
    dialog it meant to test)."""
    return _chip_offset(app, needle)


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
async def test_a_non_repo_directory_shows_a_folder_chip_not_nothing(
    monkeypatch, tmp_path,
):
    """Reported: "if i start in a non-repo dir, there is no folder/repo
    chip shown in the status line". Outside a repo the git chip used to
    just vanish (GitLine.render() returns None there), leaving nothing on
    the bar that says where this session even is. This is a DIFFERENT
    shape from the git chip (`dir NAME`, no branch symbol) -- the
    distinction between "a repo, on a branch" and "a plain directory"
    has to stay visible, not collapse into the same-looking chip with an
    empty branch half."""
    where = tmp_path / "loose-files"
    where.mkdir()
    app, _engines = await _app(monkeypatch, where)
    async with app.run_test() as pilot:
        pane = app.active_pane
        assert await _settled(pilot, pane)
        assert await _wait_status(pilot, app, "dir loose-files")
        assert "⎇" not in _status_plain(app)


@pytest.mark.asyncio
async def test_click_on_the_folder_chip_opens_the_repo_picker(monkeypatch, tmp_path):
    """The folder chip carries the SAME affordance the repo-name chip
    does -- open_repo_picker already walks any directory, repo or not
    (PaneChipsMixin._repo_picker_rows), so a non-repo session is not left
    with a chip that merely informs while every other identity chip on
    the bar is clickable."""
    where = tmp_path / "loose-files"
    where.mkdir()
    app, _engines = await _app(monkeypatch, where)
    async with app.run_test() as pilot:
        pane = app.active_pane
        assert await _settled(pilot, pane)
        assert await _wait_status(pilot, app, "dir loose-files")
        picker = app.query_one("#chip-picker", ChipPicker)
        await pilot.click("#status-bar", offset=_offset_of(app, "dir loose-files"))
        await pilot.pause()
        assert picker.is_open
        assert picker.border_title.startswith("repo · ")


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


# -- ACTIONABLE tier: peers -> a roster picker, ctx% -> /compact ---------
#
# v0.79.0: the peers chip stops shortcutting to `/sessions` (pinned
# through v0.78.0 by the test this block replaced) and opens a real
# roster in the shared ChipPicker instead -- each row the peer, the
# beginning of its transcript (PeerInfo.title, now actually derived from
# the first prompt -- see tests/test_peers.py's
# test_first_turn_sets_peer_title_from_first_prompt_only), and tokens
# consumed so far (PeerInfo.usage_tokens).


def _peer(tmp_path, sid, title, *, usage_tokens=None, daemon_socket=None,
          clients=None, cwd=None):
    now = peers_mod._iso_now()
    return peers_mod.PeerInfo(
        session_id=sid, pid=os.getpid(), socket_path=f"/tmp/{sid}.sock",
        cwd=str(cwd or tmp_path), repo_root=str(tmp_path), title=title,
        started_at=now, heartbeat_at=now, usage_tokens=usage_tokens,
        daemon_socket=daemon_socket, clients=clients,
    )


@pytest.mark.asyncio
async def test_peers_chip_click_opens_the_peers_picker(monkeypatch, tmp_path):
    peer = _peer(tmp_path, "peer00001111", "fix the flaky test", usage_tokens=12345)
    fake = FakeEngine([], peers=[peer])
    app, _engines = await _app(monkeypatch, tmp_path, fake)
    # Wide enough that the peers chip stays on-screen behind the `dir
    # <cwd name>` folder chip -- outside a git repo (as `tmp_path` is
    # here) that chip's own text is this test's own name, and the default
    # 80-column terminal is not wide enough to fit it plus everything
    # that paints before the peers chip.
    async with app.run_test(size=(200, 24)) as pilot:
        assert await _wait_status(pilot, app, "peers 1")
        picker = app.query_one("#chip-picker", ChipPicker)
        assert not picker.is_open
        await pilot.click("#status-bar", offset=_offset_of(app, "peers 1"))
        await pilot.pause()
        assert picker.is_open
        assert picker.border_title == "peers"
        labels = {rid: label for rid, label in picker._rows}
        row = labels.get("peer00001111", "")
        # Rendered row text, not a query match: which peer, what it's
        # working on, and tokens so far -- fmt_tokens(12345) == "12k".
        assert "fix the flaky test" in row
        assert "12k tok" in row


@pytest.mark.asyncio
async def test_peers_picker_unknown_tokens_render_as_dash_never_zero(
    monkeypatch, tmp_path,
):
    """PeerInfo.usage_tokens is None for a peer that has not completed a
    turn yet (or an older build's entry) -- the row must say so, not
    claim "0 tok", which would be a confident lie the same way an
    uncoerced ctx-tokens field would be (item X's own rule)."""
    peer = _peer(tmp_path, "peer00002222", "just started", usage_tokens=None)
    fake = FakeEngine([], peers=[peer])
    app, _engines = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test() as pilot:
        assert await _wait_status(pilot, app, "peers 1")
        pane = app.active_pane
        pane.open_peers_picker()
        await pilot.pause()
        picker = app.query_one("#chip-picker", ChipPicker)
        labels = {rid: label for rid, label in picker._rows}
        row = labels.get("peer00002222", "")
        assert "tok —" in row
        assert "0 tok" not in row


@pytest.mark.asyncio
async def test_peers_picker_row_attaches_via_the_existing_attach_path(
    monkeypatch, tmp_path,
):
    peer = _peer(
        tmp_path, "peer00003333", "working on it", usage_tokens=500,
        daemon_socket="/tmp/daemon-peer3.sock",
    )
    fake = FakeEngine([], peers=[peer])
    app, _engines = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test() as pilot:
        assert await _wait_status(pilot, app, "peers 1")
        calls = []
        monkeypatch.setattr(app, "_cmd_attach", lambda e: calls.append(e.session_id))
        pane = app.active_pane
        pane.open_peers_picker()
        await pilot.pause()
        picker = app.query_one("#chip-picker", ChipPicker)
        index = next(
            i for i, (rid, _l) in enumerate(picker._rows) if rid == "peer00003333"
        )
        picker.select_row(index)
        await pilot.pause()
        # Asserts the CALL, not just the UI -- the SAME path the sessions
        # picker's own detached-row test asserts, never a second
        # implementation.
        assert calls == ["peer00003333"]


@pytest.mark.asyncio
async def test_peers_picker_row_without_daemon_socket_is_not_attachable(
    monkeypatch, tmp_path,
):
    """A peer with no daemon_socket (an in-process engine) cannot be
    reached by `doxa attach` at all -- the sessions picker never has this
    case (list_daemons only returns daemon-hosted entries); the peers
    picker reads the full peer list, so it must say so rather than
    silently doing nothing on selection."""
    peer = _peer(tmp_path, "peer00004444", "in-process only", usage_tokens=500)
    fake = FakeEngine([], peers=[peer])
    app, _engines = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test() as pilot:
        assert await _wait_status(pilot, app, "peers 1")
        calls = []
        monkeypatch.setattr(app, "_cmd_attach", lambda e: calls.append(e.session_id))
        before = len(_system_texts(app))
        pane = app.active_pane
        pane.open_peers_picker()
        await pilot.pause()
        picker = app.query_one("#chip-picker", ChipPicker)
        index = next(
            i for i, (rid, _l) in enumerate(picker._rows) if rid == "peer00004444"
        )
        picker.select_row(index)
        for _ in range(100):
            if len(_system_texts(app)) > before:
                break
            await pilot.pause(0.02)
        assert calls == []
        assert "not attachable" in _system_texts(app)[-1]


@pytest.mark.asyncio
async def test_peers_picker_title_markup_is_escaped_not_interpreted(
    monkeypatch, tmp_path,
):
    """title is written by ANOTHER process (PEER_UNTRUSTED_INTRO's own
    trust boundary) -- this pins that a peer cannot smuggle Rich markup
    through the row this picker paints. ChipPicker escapes every row
    centrally (_escape_markup at render); this proves it still holds for
    THIS new caller by re-parsing the actual painted Option text the same
    way Textual itself would, and asserting the malicious text survives
    as literal, inert characters -- rendered text, not a query match."""
    peer = _peer(
        tmp_path, "peer00005555", "[bold red]hacked[/]", usage_tokens=10,
    )
    fake = FakeEngine([], peers=[peer])
    app, _engines = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test() as pilot:
        assert await _wait_status(pilot, app, "peers 1")
        pane = app.active_pane
        pane.open_peers_picker()
        await pilot.pause()
        picker = app.query_one("#chip-picker", ChipPicker)
        index = next(
            i for i, (rid, _l) in enumerate(picker._rows) if rid == "peer00005555"
        )
        prompt = str(picker.get_option_at_index(index).prompt)
        rendered = Content.from_markup(prompt).plain
        assert "hacked" in rendered
        assert "[bold red]" in rendered


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
    async with app.run_test(size=(200, 24)) as pilot:
        assert await _wait_status(pilot, app, "ctx 42%")
        await pilot.click("#status-bar", offset=_offset_of(app, "ctx 42%"))
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
    async with app.run_test(size=(200, 24)) as pilot:
        assert await _wait_status(pilot, app, "ctx 91%")
        await pilot.click("#status-bar", offset=_offset_of(app, "ctx 91%"))
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
    async with app.run_test(size=(200, 24)) as pilot:
        # No percentage is set on this fake -- ctx_chip(None) == "ctx —".
        assert await _wait_status(pilot, app, "ctx —")
        await pilot.click("#status-bar", offset=_offset_of(app, "ctx —"))
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
    async with app.run_test(size=(200, 24)) as pilot:
        # No percentage is set on this fake -- ctx_chip(None) == "ctx —".
        assert await _wait_status(pilot, app, "ctx —")
        await pilot.click("#status-bar", offset=_offset_of(app, "ctx —"))
        for _ in range(100):
            if isinstance(app.screen, CompactConfirm):
                break
            await pilot.pause(0.02)
        # The click must actually have opened it -- previously missing,
        # which let this test pass vacuously (Esc on a screen that was
        # never a CompactConfirm "cancels" it trivially) whenever the
        # click landed on the wrong chip instead.
        assert isinstance(app.screen, CompactConfirm)
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
    # Wide enough that the session-handle chip stays on-screen behind the
    # `dir <cwd name>` folder chip -- see the peers-chip test above for
    # why the default 80-column terminal is not enough here.
    async with app.run_test(size=(200, 24)) as pilot:
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


@pytest.mark.asyncio
async def test_sessions_picker_detached_row_opens_a_new_tab(monkeypatch, tmp_path):
    """v0.60.0: the real (un-stubbed) attach path, end to end. Through
    v0.56.0 _cmd_attach switched the pane the picker was opened FROM in
    place, so the tab the user was looking at went blank rather than a
    new one appearing -- measured against a real SessionDaemon before
    this changed: the socket connected, but switch_engine() never set
    _restore_transcript_wanted, so the reattached content depended
    entirely on the daemon's capped in-memory event ring. Now it opens a
    new tab through _attach_in_new_tab, the same door /resume already
    sends a running session through, and the ORIGINAL tab is left alone."""
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
            def _attached_engine(sock, **kw):
                client = FakeEngine([], cwd=str(tmp_path))
                client.session_id = "bbbb2222dead"
                return client

            monkeypatch.setattr("doxa.client.EngineClient", _attached_engine)
            before = len(app.panes())
            original = app.active_pane
            pane = app.active_pane
            pane.open_sessions_picker()
            await pilot.pause()
            picker = app.query_one("#chip-picker", ChipPicker)
            index = next(
                i for i, (rid, _l) in enumerate(picker._rows)
                if rid == "bbbb2222dead"
            )
            picker.select_row(index)
            for _ in range(200):
                if len(app.panes()) > before:
                    break
                await pilot.pause(0.02)
            assert len(app.panes()) == before + 1  # a NEW tab, not a swap
            assert app.panes()[0] is original
            assert original.engine is fake  # the original tab is untouched
            new_pane = app.panes()[-1]
            assert new_pane.engine.session_id == "bbbb2222dead"
        finally:
            sock.close()


async def _show_belief(pilot, app, picker, rid):
    """Drive the picker to one belief's full claim.

    v0.48.0 moved that off the belief row itself: selecting a belief opens
    THAT belief's actions (record an outcome, retract, show the claim,
    open the browser), because an OptionList row cannot carry a button and
    the actions ARE the row set. "Show the full claim" is the first of
    them, so the old one-selection behaviour is still one selection away
    -- this helper is what that costs a test."""
    index = next(i for i, (r, _l) in enumerate(picker._rows) if r == rid)
    picker.select_row(index)
    # The action menu reopens the picker one refresh cycle later (see
    # PaneChipsMixin._pick_belief_row on why it cannot be synchronous).
    for _ in range(100):
        if any(r == "act:show" for r, _l in picker._rows):
            break
        await pilot.pause(0.02)
    show = next(i for i, (r, _l) in enumerate(picker._rows) if r == "act:show")
    picker.select_row(show)
    for _ in range(100):
        if _system_texts(app):
            break
        await pilot.pause(0.02)
    return bool(_system_texts(app))


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
    # Wide enough that the beliefs chip stays on-screen behind the `dir
    # <cwd name>` folder chip -- see the peers-chip test above for why
    # the default 80-column terminal is not enough here.
    async with app.run_test(size=(200, 24)) as pilot:
        # FakeEngine.belief_count() is a fixed 3 (engine parity, see
        # tests/fakes.py) -- the chip's COUNT and its picker's BODIES are
        # deliberately two different calls (cost discipline, item 3).
        assert await _wait_status(pilot, app, "3 beliefs")
        await pilot.click("#status-bar", offset=_offset_of(app, "3 beliefs"))
        await pilot.pause()
        picker = app.query_one("#chip-picker", ChipPicker)
        assert picker.is_open
        assert picker.border_title == "beliefs"
        # v0.48.0: group headers are SELECTABLE (folding is their
        # affordance), so they carry a namespaced rid rather than the
        # empty one a disabled separator used to have. The grouping this
        # test exists for is unchanged -- and each header now says how
        # many beliefs it stands for.
        headers = [label for rid, label in picker._rows
                   if rid.startswith(ChipPicker.GROUP_ROW_PREFIX)]
        assert any("project (1 belief)" in h for h in headers), headers
        # v0.67.0: `· stated`/`· inferred` -- LORE's channel tag.
        assert any("user · stated (1 belief)" in h for h in headers), headers
        assert any("user-model · inferred (1 belief)" in h for h in headers), headers
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
        picker.flush_filter()  # v0.69.0: the filter itself now debounces
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
        assert await _show_belief(pilot, app, picker, "belief:7")
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


# =========================================================================
# v0.28.0 -- three operator-reported defects in the v0.27.0 chip work.
#
# The tests above proved a CompactConfirm was PUSHED and that a synthetic
# `pilot.click("#compact-confirm-yes")` reached its handler. Neither says
# anything about whether the button OCCUPIES SCREEN, and it did not: the
# operator saw "a modal message, but no button to continue, no OK, enter
# does nothing". Every test below therefore asserts a user-visible fact --
# rendered geometry, a real hit test, a key that actually resolves the
# dialog, a status line that actually moves -- rather than that a message
# was posted.
# =========================================================================


async def _wait_for(pilot, predicate, tries=200):
    for _ in range(tries):
        if predicate():
            return True
        await pilot.pause(0.02)
    return bool(predicate())


def _hit(app, widget):
    """The widget the SCREEN reports at this widget's own centre -- the
    hit test a mouse actually performs. A zero-height button passes every
    query_one() in the suite and fails this."""
    region = widget.region
    if not region.area:
        return None
    x = region.x + region.width // 2
    y = region.y + region.height // 2
    try:
        found, _region = app.screen.get_widget_at(x, y)
    except Exception:
        return None
    return found


# -- defect 1: the confirm dialogs had no visible buttons and no Enter ----


@pytest.mark.asyncio
async def test_compact_confirm_buttons_have_real_height_and_are_hittable(
    monkeypatch, tmp_path,
):
    """The defect, measured: `#compact-confirm-buttons { height: 1;
    padding-top: 1 }` under Textual's border-box model spent the whole
    declared row on padding, leaving a 0-row content box -- the buttons
    laid out at Size(width=58, height=0) / Size(width=0, height=0) and
    nothing was drawn. This asserts the geometry directly, which is the
    check the v0.27.0 tests were missing."""
    fake = FakeEngine([])
    fake.last_ctx_percentage = 42.0
    app, _engines = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test(size=(120, 40)) as pilot:
        assert await _wait_status(pilot, app, "ctx 42%")
        await pilot.click("#status-bar", offset=_offset_of(app, "ctx 42%"))
        assert await _wait_for(pilot, lambda: isinstance(app.screen, CompactConfirm))
        await pilot.pause()

        row = app.screen.query_one("#compact-confirm-buttons")
        yes = app.screen.query_one("#compact-confirm-yes")
        no = app.screen.query_one("#compact-confirm-no")
        assert row.size.height > 0, f"button row collapsed: {row.size}"
        for button in (yes, no):
            assert button.size.height > 0, f"{button.id} collapsed: {button.size}"
            assert button.size.width > 0, f"{button.id} collapsed: {button.size}"
            assert _hit(app, button) is button, f"{button.id} is not hittable"

        # Self-describing: each door names the key that opens it, because
        # the operator's report was "no OK" -- they could not tell what to
        # press even once something was on screen.
        assert "enter" in str(yes.renderable).lower()
        assert "esc" in str(no.renderable).lower()


@pytest.mark.asyncio
async def test_enter_confirms_the_compact_dialog(monkeypatch, tmp_path):
    """"enter does nothing" -- CompactConfirm bound only escape, and
    handled y/c/n in on_key. Enter now takes the action the click that
    opened the dialog already asked for."""
    fake = FakeEngine([])
    fake.last_ctx_percentage = 88.0
    app, engines = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test() as pilot:
        assert await _wait_status(pilot, app, "ctx 88%")
        await pilot.click("#status-bar", offset=_offset_of(app, "ctx 88%"))
        assert await _wait_for(pilot, lambda: isinstance(app.screen, CompactConfirm))
        await pilot.press("enter")
        assert await _wait_for(
            pilot, lambda: not isinstance(app.screen, CompactConfirm)
        )
        assert await _wait_for(pilot, lambda: engines[0].received_prompts)
        assert engines[0].received_prompts == ["/compact"]


@pytest.mark.asyncio
async def test_esc_still_cancels_the_compact_dialog(monkeypatch, tmp_path):
    """Enter taking the default must not have moved Esc off cancel."""
    fake = FakeEngine([])
    app, engines = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test() as pilot:
        # No percentage is set on this fake -- ctx_chip(None) == "ctx —".
        assert await _wait_status(pilot, app, "ctx —")
        await pilot.click("#status-bar", offset=_offset_of(app, "ctx —"))
        assert await _wait_for(pilot, lambda: isinstance(app.screen, CompactConfirm))
        await pilot.press("escape")
        assert await _wait_for(
            pilot, lambda: not isinstance(app.screen, CompactConfirm)
        )
        await pilot.pause(0.1)
        assert engines[0].received_prompts == []


@pytest.mark.asyncio
async def test_close_confirm_buttons_have_real_height_and_are_hittable(
    monkeypatch, tmp_path,
):
    """`#close-confirm-buttons` carried the IDENTICAL css and therefore the
    identical latent defect -- Ctrl+Q with a turn running showed three
    invisible doors. Only the ctx% twin was reported; both are fixed."""
    from doxa.app import CloseWithTurnRunning

    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.push_screen(CloseWithTurnRunning())
        assert await _wait_for(
            pilot, lambda: isinstance(app.screen, CloseWithTurnRunning)
        )
        await pilot.pause()
        row = app.screen.query_one("#close-confirm-buttons")
        assert row.size.height > 0, f"button row collapsed: {row.size}"
        for wid in ("#close-terminate", "#close-detach", "#close-cancel"):
            button = app.screen.query_one(wid)
            assert button.size.height > 0, f"{wid} collapsed: {button.size}"
            assert button.size.width > 0, f"{wid} collapsed: {button.size}"
            assert _hit(app, button) is button, f"{wid} is not hittable"
        # Same self-describing rule as the compact dialog.
        assert "t" in str(app.screen.query_one("#close-terminate").renderable)
        assert "enter" in str(app.screen.query_one("#close-detach").renderable)
        assert "esc" in str(app.screen.query_one("#close-cancel").renderable)


@pytest.mark.asyncio
async def test_enter_on_the_close_confirm_detaches(monkeypatch, tmp_path):
    """Consistency with CompactConfirm's Enter, argued the same way: Enter
    takes the action the gesture that OPENED the dialog asked for. Ctrl+Q
    means "close this tab", and the non-destructive reading of that is
    detach -- terminate stays a deliberate `t`, never a default."""
    from doxa.app import CloseWithTurnRunning

    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        chosen: list = []
        app.push_screen(CloseWithTurnRunning(), callback=chosen.append)
        assert await _wait_for(
            pilot, lambda: isinstance(app.screen, CloseWithTurnRunning)
        )
        await pilot.press("enter")
        assert await _wait_for(pilot, lambda: bool(chosen))
        assert chosen == ["detach"]


# -- defect 3: a chosen branch / directory did not visibly apply ----------


class _WorktreeBranchEngine(FakeEngine):
    """switch_branch delegated to the REAL doxa.worktrees -- exactly what
    both SessionEngine.switch_branch and the daemon's `branch` RPC do, so
    this exercises the actual git operation the picker triggers rather
    than a canned reply."""

    def __init__(self, cwd: str) -> None:
        super().__init__([])
        self._cwd = cwd

    async def switch_branch(self, target):
        from doxa import worktrees as worktrees_mod

        self.branch_calls.append(target)
        if not target:
            return worktrees_mod.branch_status(self._cwd)
        return worktrees_mod.switch_base(self._cwd, target)


async def _click_picker_row(pilot, app, rid: str) -> None:
    """A REAL mouse click on the picker row carrying `rid` -- not
    `select_row(i)`, which is what every v0.22.0 selection test called and
    which therefore never proved a click reaches the callback at all. The
    +1 skips the picker's own top border row."""
    picker = app.query_one("#chip-picker", ChipPicker)
    index = next(i for i, (r, _l) in enumerate(picker._rows) if r == rid)
    await pilot.click(ChipPicker, offset=(4, index + 1))


@pytest.mark.asyncio
async def test_picking_a_branch_moves_the_status_line(monkeypatch, tmp_path):
    """The defect, measured: clicking `develop` DID fire the callback, DID
    run switch_base, DID rewrite the sidecar's base_ref -- and left the
    status bar byte-identical, because the chip rendered branch_label()
    (the checked-out `doxa/<id>`, which a base switch never renames)
    while the picker changes the BASE. From the operator's seat that is
    "i chose a branch and it is not changed"."""
    from doxa import worktrees as worktrees_mod

    monkeypatch.setenv("DOXA_WORKTREE", "1")
    repo = _repo(tmp_path, branch="main")
    subprocess.run(["git", "-C", str(repo), "branch", "develop"], check=True)
    worktree = worktrees_mod.create(str(repo), "chipfix1")
    assert worktree is not None

    fake = _WorktreeBranchEngine(worktree)
    app, _engines = await _app(monkeypatch, worktree, fake)
    async with app.run_test(size=(140, 40)) as pilot:
        pane = app.active_pane
        assert await _settled(pilot, pane)
        assert await _wait_status(pilot, app, "main")
        before = _status_plain(app)
        assert "main" in before

        await pane.open_branch_picker()
        await pilot.pause()
        await _click_picker_row(pilot, app, "develop")

        assert await _wait_for(pilot, lambda: "develop" in fake.branch_calls)
        # The git side actually landed...
        assert await _wait_for(
            pilot,
            lambda: (worktrees_mod.read_meta(worktree) or {}).get("base_ref")
            == "develop",
        )
        # ...and -- the whole defect -- the status line says so.
        assert await _wait_status(pilot, app, "develop")
        after = _status_plain(app)
        assert after != before
        assert "main" not in after.split("·")[1]


@pytest.mark.asyncio
async def test_clicking_a_directory_row_descends_the_repo_picker(
    monkeypatch, tmp_path,
):
    """Selection by REAL CLICK, the gesture the operator used. Descending
    re-opens the same ChipPicker instance from a `call_after_refresh`
    callback that runs after close/blur/focus have settled -- this pins
    that the reopen actually survives that hand-off."""
    repo = _repo(tmp_path)
    (repo / "nested").mkdir()
    app, _engines = await _app(monkeypatch, repo)
    async with app.run_test(size=(140, 40)) as pilot:
        pane = app.active_pane
        assert await _settled(pilot, pane)
        pane.open_repo_picker()
        await pilot.pause()
        picker = app.query_one("#chip-picker", ChipPicker)
        assert picker.border_title == f"repo · {repo}"

        await _click_picker_row(pilot, app, f"dir:{repo / 'nested'}")
        assert await _wait_for(
            pilot, lambda: picker.border_title == f"repo · {repo / 'nested'}"
        )
        assert picker.is_open


@pytest.mark.asyncio
async def test_clicking_a_repo_row_opens_it_in_a_new_tab(monkeypatch, tmp_path):
    """The other half of the same gesture: a repo root is not a place to
    descend into, it is a place to OPEN -- and by real click, not
    select_row."""
    repo = _repo(tmp_path)
    other = _repo(tmp_path, name="otherrepo")
    app, _engines = await _app(monkeypatch, repo)
    async with app.run_test(size=(140, 40)) as pilot:
        pane = app.active_pane
        assert await _settled(pilot, pane)
        assert len(app.panes()) == 1
        pane._open_repo_picker_at(str(tmp_path))
        await pilot.pause()

        await _click_picker_row(pilot, app, f"repo:{other}")
        assert await _wait_for(pilot, lambda: len(app.panes()) == 2)
        assert any(p.cwd == str(other) for p in app.panes())


@pytest.mark.asyncio
async def test_a_plain_directory_can_be_opened_not_only_descended_into(
    monkeypatch, tmp_path,
):
    """Before v0.28.0 the "· open here" row existed only when the current
    directory WAS a git repo root, so descending into an ordinary
    directory left a listing where every row went up or went deeper and
    nothing opened anything -- a dead end, and part of "i chose a dir and
    it is not changed". open_tab_at takes any directory; only the ⎇
    marker was ever about repo-ness."""
    plain = tmp_path / "notarepo"
    plain.mkdir()
    repo = _repo(tmp_path)
    app, _engines = await _app(monkeypatch, repo)
    async with app.run_test(size=(140, 40)) as pilot:
        pane = app.active_pane
        assert await _settled(pilot, pane)
        pane._open_repo_picker_at(str(plain))
        await pilot.pause()
        picker = app.query_one("#chip-picker", ChipPicker)
        labels = {rid: label for rid, label in picker._rows}
        assert f"repo:{plain}" in labels
        assert "⎇" not in labels[f"repo:{plain}"]  # honest: not a repo

        await _click_picker_row(pilot, app, f"repo:{plain}")
        assert await _wait_for(pilot, lambda: len(app.panes()) == 2)
        assert any(p.cwd == str(plain) for p in app.panes())


# -- defect 2's honesty half: a capped list must say it is capped --------


class _CappedBeliefEngine(FakeEngine):
    """A store with more active beliefs than list_beliefs will return --
    what BELIEF_LIST_LIMIT does to a big store, in miniature."""

    def __init__(self, shown: int, total: int) -> None:
        super().__init__([])
        self._total = total
        self.list_beliefs_result = [
            {"id": i, "subject": "user", "claim": f"claim {i}", "confidence": 0.5}
            for i in range(shown)
        ]

    def belief_count(self) -> int:
        return self._total


@pytest.mark.asyncio
async def test_a_capped_beliefs_list_says_so_in_its_own_row(monkeypatch, tmp_path):
    """A short list shown as if it were the whole store is the one failure
    the frame-cap fix must not trade itself for. belief_count() is the
    SAME `status='active'` COUNT(*) list_beliefs selects over, so the
    mismatch is exact, and ChipPicker's note row (already the place an
    honesty caveat lives -- see the effort picker) carries it."""
    fake = _CappedBeliefEngine(shown=500, total=517)
    app, _engines = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test() as pilot:
        pane = app.active_pane
        await pane.open_beliefs_picker()
        await pilot.pause()
        picker = app.query_one("#chip-picker", ChipPicker)
        assert picker.is_open
        assert "500" in picker._note and "517" in picker._note
        # The caveat is row 0 and is not selectable (rid "").
        assert picker._rows[0][0] == ""
        assert "capped" in picker._rows[0][1]


@pytest.mark.asyncio
async def test_a_complete_beliefs_list_carries_no_caveat(monkeypatch, tmp_path):
    """...and equally, a list that IS complete must not grow a scary row
    it has not earned."""
    fake = _CappedBeliefEngine(shown=4, total=4)
    app, _engines = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test() as pilot:
        pane = app.active_pane
        await pane.open_beliefs_picker()
        await pilot.pause()
        picker = app.query_one("#chip-picker", ChipPicker)
        assert picker._note == ""


@pytest.mark.asyncio
async def test_a_truncated_claim_says_so_in_the_detail_view(monkeypatch, tmp_path):
    """The one belief too big for a single wire frame comes back cut and
    flagged (doxa.daemon._fit_belief_page); the detail view must not show
    the remnant as though it were the whole claim."""
    fake = FakeEngine([])
    fake.list_beliefs_result = [
        {"id": 9, "subject": "user", "claim": "the beginning of a huge claim",
         "confidence": 0.6, "claim_truncated": True},
    ]
    app, _engines = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test() as pilot:
        pane = app.active_pane
        await pane.open_beliefs_picker()
        await pilot.pause()
        picker = app.query_one("#chip-picker", ChipPicker)
        assert await _show_belief(pilot, app, picker, "belief:9")
        assert "the beginning of a huge claim" in _system_texts(app)[-1]
        assert "truncated" in _system_texts(app)[-1]

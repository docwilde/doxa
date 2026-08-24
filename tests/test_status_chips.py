"""Status-chips (item Y and the branch/model click affordance): clicking
the status bar was inert, despite looking like it should do something --
the operator's own report. This file covers the fix: the model chip and
the git chip's branch span open the SAME shared dropdown
(:class:`doxa.app.ChipPicker`), keyboard nav + type-to-filter narrow it,
Enter/click-select invoke the EXACT SAME coroutine the matching slash
command uses (assert the call, not just the UI), Esc and click-away close
it, and the status bar reflects the new value immediately. Also covers the
two other tiers the operator's three-tier answer specced: ACTIONABLE chips
(peers -> /sessions, ctx% -> /compact, the session handle -> clipboard)
run something that already exists with no picker, and the INERT tier
(repo name, sha) gets no click affordance at all.

Same headless Pilot + FakeEngine pattern as tests/test_branch_command.py
and tests/test_subagent_tracker.py. Click coordinates are resolved from
the status bar's PLAIN (markup-stripped) text via
``textual.content.Content.from_markup(...).plain`` -- `.renderable` on a
Static is the raw string handed to `update()`, brackets and all, so a
naive string index would land on the wrong screen column once a chip
carries `[@click=...]` markup.
"""

from __future__ import annotations

import subprocess

import pytest
from textual.content import Content

from doxa.app import ChipPicker, DoxaApp, SystemBlock
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


async def _app(monkeypatch, cwd, fake=None):
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(cwd) + "-rt")
    engines: list[FakeEngine] = []

    def make():
        engines.append(fake or FakeEngine([]))
        return engines[-1]

    app = DoxaApp(cwd=str(cwd), engine_factory=make, new_session_factory=make)
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
async def test_click_on_repo_name_or_sha_does_not_open_anything(monkeypatch, tmp_path):
    repo = _repo(tmp_path)
    app, _engines = await _app(monkeypatch, repo)
    async with app.run_test() as pilot:
        pane = app.active_pane
        assert await _settled(pilot, pane)
        assert await _wait_status(pilot, app, "myrepo")
        picker = app.query_one("#chip-picker", ChipPicker)

        await pilot.click("#status-bar", offset=_offset_of(app, "myrepo"))
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
async def test_ctx_chip_click_sends_compact_as_a_turn(monkeypatch, tmp_path):
    fake = FakeEngine([])
    app, engines = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test() as pilot:
        assert await _wait_status(pilot, app, "ctx")
        await pilot.click("#status-bar", offset=_offset_of(app, "ctx"))
        for _ in range(100):
            if engines[0].received_prompts:
                break
            await pilot.pause(0.02)
        assert engines[0].received_prompts == ["/compact"]


@pytest.mark.asyncio
async def test_session_handle_click_copies_to_clipboard(monkeypatch, tmp_path):
    class Detachable(FakeEngine):
        detachable = True
        session_id = "sess-abcdef01"

    fake = Detachable([])
    app, _engines = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test() as pilot:
        assert await _wait_status(pilot, app, "session sess-abc")
        copied = []
        monkeypatch.setattr(app, "copy_to_clipboard", lambda text: copied.append(text))
        await pilot.click("#status-bar", offset=_offset_of(app, "session sess-abc"))
        for _ in range(100):
            if copied:
                break
            await pilot.pause(0.02)
        assert copied == ["sess-abcdef01"]


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

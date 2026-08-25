# SPDX-License-Identifier: AGPL-3.0-only
"""/branch (item S #2/#3): the command surface itself -- dispatch,
formatting, and the non-repo message. The real git mechanics
(doxa.worktrees.branch_status/switch_base) are exercised with real
repos in tests/test_worktrees.py; the daemon RPC round-trip and its
cross-client broadcast are exercised in tests/test_daemon.py. This file
is about doxa.app.SessionPane._cmd_branch talking to a SCRIPTED engine
handle, the same division test_auth.py already draws for /login."""

from __future__ import annotations

import subprocess

import pytest

from doxa.app import DoxaApp, SystemBlock
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


async def _app(monkeypatch, cwd):
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(cwd) + "-rt")
    engines: list[FakeEngine] = []

    def make():
        engines.append(FakeEngine([]))
        return engines[-1]

    app = DoxaApp(cwd=str(cwd), engine_factory=make, new_session_factory=make)
    return app, engines


async def _settled(pilot, pane, tries=200):
    for _ in range(tries):
        if pane._git is not None:
            return True
        await pilot.pause(0.02)
    return pane._git is not None


@pytest.mark.asyncio
async def test_branch_outside_a_repo_says_so_in_the_house_phrasing(
    monkeypatch, tmp_path,
):
    where = tmp_path / "loose-files"
    where.mkdir()
    app, _engines = await _app(monkeypatch, where)
    async with app.run_test() as pilot:
        pane = app.active_pane
        assert await _settled(pilot, pane)
        await pane._cmd_branch("")
        await pilot.pause()
        assert _system_texts(app)[-1] == "branch: no repo here"


@pytest.mark.asyncio
async def test_branch_with_no_args_lists_branches_and_marks_the_base(
    monkeypatch, tmp_path,
):
    repo = _repo(tmp_path)
    app, engines = await _app(monkeypatch, repo)
    async with app.run_test() as pilot:
        pane = app.active_pane
        assert await _settled(pilot, pane)
        engines[0].branch_list_result = {
            "branches": ["trunk", "develop", "feature"],
            "base": "trunk",
            "checked_out": "trunk",
        }
        await pane._cmd_branch("")
        await pilot.pause()
        text = _system_texts(app)[-1]
        assert text.startswith("branch: trunk")
        assert "▸ trunk" in text
        assert "develop" in text and "feature" in text
        assert "usage: /branch <name>" in text
        assert engines[0].branch_calls == [None]


@pytest.mark.asyncio
async def test_branch_switch_success_shows_the_confirmation_and_refreshes(
    monkeypatch, tmp_path,
):
    repo = _repo(tmp_path)
    app, engines = await _app(monkeypatch, repo)
    async with app.run_test() as pilot:
        pane = app.active_pane
        assert await _settled(pilot, pane)
        engines[0].branch_switch_result = {
            "ok": True, "base": "develop",
            "message": "doxa/abc123 now based on develop",
        }
        await pane._cmd_branch("develop")
        await pilot.pause()
        assert _system_texts(app)[-1] == "branch: doxa/abc123 now based on develop"
        assert engines[0].branch_calls == ["develop"]


@pytest.mark.asyncio
async def test_branch_switch_refusal_shows_the_refusal_message(
    monkeypatch, tmp_path,
):
    repo = _repo(tmp_path)
    app, engines = await _app(monkeypatch, repo)
    async with app.run_test() as pilot:
        pane = app.active_pane
        assert await _settled(pilot, pane)
        engines[0].branch_switch_result = {
            "ok": False, "base": None,
            "message": "doxa/abc123 has uncommitted changes -- ...",
        }
        await pane._cmd_branch("develop")
        await pilot.pause()
        assert _system_texts(app)[-1] == (
            "branch: doxa/abc123 has uncommitted changes -- ..."
        )


@pytest.mark.asyncio
async def test_branch_is_registered_in_the_palette_and_registry(monkeypatch, tmp_path):
    from doxa import commands

    row = commands.find("/branch")
    assert row is not None
    assert row.usage == "/branch [name]"
    assert row.palette == "Branch: switch"

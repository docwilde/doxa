# SPDX-License-Identifier: AGPL-3.0-only
"""``/dir`` and ``/cd`` (owner-requested, v0.93.0): "we should also
provide a /cd and /dir command where /dir lists the cwd".

``/dir`` is the honest half and the easy one: this session's own working
directory, read straight off the engine. ``/cd`` is the one with a real
decision behind it -- the running ``claude`` CLI subprocess was spawned
with its OWN operating-system cwd, and nothing in the SDK can hand it a
new one mid-session. So ``/cd <path>`` opens the target in a NEW tab (the
same mechanism ``/resume`` and the repo-name chip's own directory picker
already use for "go somewhere else") and says, every time, that THIS
session was left exactly where it was -- never a status line that claims
a location none of this session's own tool calls are actually touching.

These tests pin: what ``/dir`` reports (plain cwd, and a worktree
session's own sidecar detail); that bare ``/cd`` explains itself instead
of doing nothing; that a successful ``/cd`` opens elsewhere and reports
this session unchanged; and that a failed ``/cd`` surfaces the same
refusal ``open_tab_at`` already gives the repo picker, rather than a
second, differently-worded one.
"""

from __future__ import annotations

import subprocess

import pytest

from doxa.app import DoxaApp, SystemBlock
from doxa import worktrees as worktrees_mod
from tests.fakes import FakeEngine


def _repo(tmp_path, name="repo", branch="trunk"):
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


async def _app(monkeypatch, cwd, engine_cwd: "str | None" = None):
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(cwd) + "-rt")
    engines: list[FakeEngine] = []

    def make():
        engines.append(FakeEngine([], cwd=engine_cwd or str(cwd)))
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
async def test_dir_reports_the_engine_cwd_plainly(monkeypatch, tmp_path):
    where = tmp_path / "loose-files"
    where.mkdir()
    app, _engines = await _app(monkeypatch, where)
    async with app.run_test() as pilot:
        pane = app.active_pane
        assert await _settled(pilot, pane)
        await pane._cmd_dir("")
        await pilot.pause()
        assert _system_texts(app)[-1] == f"dir: {where}"


@pytest.mark.asyncio
async def test_dir_names_the_worktree_and_its_base_for_a_worktree_session(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "doxa-home"))
    monkeypatch.setenv("DOXA_WORKTREE", "1")
    repo = _repo(tmp_path)
    target = worktrees_mod.create(str(repo), "abcdef12")
    assert target is not None  # sanity: the real worktree got made
    app, _engines = await _app(monkeypatch, repo, engine_cwd=target)
    async with app.run_test() as pilot:
        pane = app.active_pane
        assert await _settled(pilot, pane)
        await pane._cmd_dir("")
        await pilot.pause()
        text = _system_texts(app)[-1]
        assert text.startswith(f"dir: {target}")
        assert "DOXA worktree" in text
        assert str(repo) in text
        assert "trunk" in text


@pytest.mark.asyncio
async def test_bare_cd_explains_itself_instead_of_doing_nothing(
    monkeypatch, tmp_path,
):
    where = tmp_path / "loose-files"
    where.mkdir()
    app, _engines = await _app(monkeypatch, where)
    async with app.run_test() as pilot:
        pane = app.active_pane
        assert await _settled(pilot, pane)
        await pane._cmd_cd("")
        await pilot.pause()
        text = _system_texts(app)[-1]
        assert "usage: /cd <path>" in text
        assert "cannot be changed" in text
        assert str(where) in text


@pytest.mark.asyncio
async def test_cd_opens_a_new_tab_and_leaves_this_session_unchanged(
    monkeypatch, tmp_path,
):
    where = tmp_path / "loose-files"
    where.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    app, _engines = await _app(monkeypatch, where)
    async with app.run_test() as pilot:
        pane = app.active_pane
        assert await _settled(pilot, pane)
        calls: list[str] = []

        async def fake_open_tab_at(path):
            calls.append(path)
            return None

        monkeypatch.setattr(app, "open_tab_at", fake_open_tab_at)
        await pane._cmd_cd(str(elsewhere))
        await pilot.pause()
        assert calls == [str(elsewhere)]
        text = _system_texts(app)[-1]
        assert "opened a new tab" in text
        assert str(elsewhere) in text
        # THIS session's own directory, unmoved -- named explicitly so the
        # claim is checkable, not just asserted.
        assert str(where) in text
        assert "this session keeps running" in text


@pytest.mark.asyncio
async def test_cd_surfaces_open_tab_ats_own_refusal_not_a_second_one(
    monkeypatch, tmp_path,
):
    where = tmp_path / "loose-files"
    where.mkdir()
    app, _engines = await _app(monkeypatch, where)
    async with app.run_test() as pilot:
        pane = app.active_pane
        assert await _settled(pilot, pane)

        async def fake_open_tab_at(path):
            return f"not a directory: {path}"

        monkeypatch.setattr(app, "open_tab_at", fake_open_tab_at)
        await pane._cmd_cd("/does/not/exist")
        await pilot.pause()
        assert _system_texts(app)[-1] == "cd: not a directory: /does/not/exist"


def test_dir_and_cd_reach_help_the_palette_and_autocomplete():
    from doxa import commands as commands_mod
    from doxa.ui.labels import help_text

    for name in ("/dir", "/cd"):
        assert name in commands_mod.interactive_names()
        row = commands_mod.lookup(name)
        assert row is not None and row.palette
        assert name in help_text()
        assert any(c.name == name for c in commands_mod.matches(name[:3]))

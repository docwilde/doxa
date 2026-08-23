"""Tab labels: `<short model> · <repo> ⎇ <branch>`.

A tab has room for the two things that differ between tabs -- which model
is answering and where it is working -- and for nothing else. What is
pinned here: the short-model rule, the git half (including the linked
worktree case), the no-repo fallback, that a live /model switch moves the
label, and that the label is derived from the SAME event-driven GitLine
the status bar reads rather than from any polling of its own.
"""

from __future__ import annotations

import subprocess

import pytest

from doxa.app import (
    DoxaApp,
    GitLine,
    TAB_LABEL_MAX,
    ellipsize,
    short_model,
)
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


# -- the short-model rule -------------------------------------------------


def test_short_model_is_the_tier_word():
    assert short_model("claude-sonnet-4-5") == "sonnet"
    assert short_model("claude-3-5-haiku-20241022") == "haiku"
    assert short_model("claude-opus-4-1") == "opus"
    assert short_model("fable") == "fable"
    # Unknown vendors keep their first dash-segment rather than a truncated
    # word, and an unset model is the same "default" the status bar says.
    assert short_model("gpt-5-codex") == "gpt"
    assert short_model("") == "default"
    assert short_model(None) == "default"


def test_labels_are_ellipsis_truncated():
    long = "x" * (TAB_LABEL_MAX + 20)
    out = ellipsize(long)
    assert len(out) == TAB_LABEL_MAX
    assert out.endswith("…")
    assert ellipsize("short") == "short"


# -- the git half ---------------------------------------------------------


def test_branch_label_is_the_plain_branch_in_a_normal_checkout(tmp_path):
    repo = _repo(tmp_path)
    line = GitLine(str(repo))
    assert line.worktree is None
    assert line.branch_label() == "trunk"


def test_branch_label_names_a_linked_worktree(tmp_path):
    """`main@featureX` -- a worktree is a different place with the same
    repo name, and the branch alone does not say which one you are in."""
    repo = _repo(tmp_path)
    work = tmp_path / "elsewhere"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-q", "-b", "feature",
         str(work)],
        check=True,
    )
    line = GitLine(str(work))
    assert line.worktree == "elsewhere"     # git's own name for it
    assert line.repo == "elsewhere"
    # The worktree name here repeats the repo slot, so it is not appended:
    # `elsewhere ⎇ feature@elsewhere` says one fact three times.
    assert line.branch_label() == "feature"

    # A worktree directory whose name differs from both DOES get the
    # suffix -- that is the case where it carries information.
    line.worktree = "spike"
    assert line.branch_label() == "feature@spike"
    line.worktree = "feature"  # same as the branch: nothing to add
    assert line.branch_label() == "feature"


# -- the label on a live tab ----------------------------------------------


async def _app(monkeypatch, cwd, model="claude-sonnet-4-5"):
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(cwd) + "-rt")
    engines: list[FakeEngine] = []

    def make():
        engines.append(FakeEngine([], model=model))
        return engines[-1]

    app = DoxaApp(cwd=str(cwd), engine_factory=make, new_session_factory=make)
    return app, engines


def _tab_label(app, pane) -> str:
    from textual.widgets import TabbedContent

    return app.query_one("#session-tabs", TabbedContent).get_tab(pane.id).label.plain


async def _settled(pilot, app, pane, tries=200):
    for _ in range(tries):
        if pane._tab_label:
            return True
        await pilot.pause(0.02)
    return bool(pane._tab_label)


@pytest.mark.asyncio
async def test_tab_label_is_model_repo_branch(monkeypatch, tmp_path):
    repo = _repo(tmp_path)
    app, _engines = await _app(monkeypatch, repo)
    async with app.run_test() as pilot:
        pane = app.active_pane
        assert await _settled(pilot, app, pane)
        assert pane.auto_label() == "sonnet · myrepo ⎇ trunk"
        assert _tab_label(app, pane) == "sonnet · myrepo ⎇ trunk"


@pytest.mark.asyncio
async def test_tab_label_outside_a_repo_is_model_and_dirname(
    monkeypatch, tmp_path
):
    where = tmp_path / "loose-files"
    where.mkdir()
    app, _engines = await _app(monkeypatch, where, model="claude-haiku-4-5")
    async with app.run_test() as pilot:
        pane = app.active_pane
        assert await _settled(pilot, app, pane)
        assert pane.auto_label() == "haiku · loose-files"
        assert "⎇" not in _tab_label(app, pane)


@pytest.mark.asyncio
async def test_a_model_switch_moves_the_label(monkeypatch, tmp_path):
    repo = _repo(tmp_path)
    app, _engines = await _app(monkeypatch, repo)
    async with app.run_test() as pilot:
        pane = app.active_pane
        assert await _settled(pilot, app, pane)
        assert _tab_label(app, pane).startswith("sonnet")

        await pane._cmd_model("haiku")
        await pilot.pause()
        assert _tab_label(app, pane) == "haiku · myrepo ⎇ trunk"


@pytest.mark.asyncio
async def test_the_label_follows_a_branch_switch_without_polling(
    monkeypatch, tmp_path
):
    """Same discipline as the git chip: the branch is re-read when HEAD's
    mtime moves, on the next event-driven status refresh -- there is no
    timer anywhere in this path."""
    repo = _repo(tmp_path)
    app, _engines = await _app(monkeypatch, repo)
    async with app.run_test() as pilot:
        pane = app.active_pane
        assert await _settled(pilot, app, pane)
        assert pane.auto_label().endswith("⎇ trunk")

        subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "side"],
                       check=True)
        pane._git._mtime = None  # defeat same-second mtime granularity
        pane._refresh_status()   # what a finished turn or a peer event calls
        await pilot.pause()
        assert pane.auto_label() == "sonnet · myrepo ⎇ side"
        assert _tab_label(app, pane) == "sonnet · myrepo ⎇ side"


@pytest.mark.asyncio
async def test_each_tab_carries_its_own_label(monkeypatch, tmp_path):
    repo = _repo(tmp_path)
    app, _engines = await _app(monkeypatch, repo)
    async with app.run_test() as pilot:
        first = app.active_pane
        assert await _settled(pilot, app, first)

        await pilot.press("ctrl+t")
        for _ in range(200):
            if len(app.panes()) == 2:
                break
            await pilot.pause(0.02)
        second = app.panes()[1]
        assert await _settled(pilot, app, second)

        await second._cmd_model("opus")
        await pilot.pause()
        assert _tab_label(app, first) == "sonnet · myrepo ⎇ trunk"
        assert _tab_label(app, second) == "opus · myrepo ⎇ trunk"

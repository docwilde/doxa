# SPDX-License-Identifier: AGPL-3.0-only
"""The status bar's curated-memory chip (``mem u63% p39%``), reported:
the project half stayed absent past startup, and never moved when the
repo changed under a tab (a repo switch, or a resume).

The chip's user half and project half come from the exact same helper
(:func:`doxa.ui.labels.memory_fill`), read once per :meth:`_refresh_status`
through :meth:`PaneChipsMixin._lore_slug`. That method resolved the
project slug from ``self.cwd`` alone -- the pane's OWN cwd, frozen at
construction, the before-boot guess. Every other reader in this pane that
needs to know "where does this session actually live" (``_boot``'s own
``git_cwd``, ``open_repo_picker``) already prefers ``self.engine.cwd`` --
the ENGINE's report, set once it actually connects -- and falls back to
the pane's cwd only when there is no engine yet to ask. ``_lore_slug`` was
the one exception: whatever ``self.cwd`` said at construction is what it
used forever, even once an engine was live and had a different, truer
answer -- a daemon attach, a worktree-per-session substitution
(doxa.worktrees), or a resumed session landing somewhere the picker's own
stale record did not anticipate.

Each test below constructs exactly that gap (pane cwd says one place,
the connected engine says another, correct one) and asserts the chip
follows the engine -- present at first boot, and moving on the two paths
that hand a pane a NEW engine without a new pane: a repo switch
(``switch_engine``, the primitive ``/clear`` and ``/new session`` already
drive) and a resume (``DoxaApp.resume_session``).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from textual.content import Content

from doxa import cli_isolation as cli_isolation_mod
from doxa.app import DoxaApp
from doxa.ui.statusline import StatusBar
from tests.fakes import FakeEngine


@pytest.fixture(autouse=True)
def _own_doxa_home(monkeypatch, tmp_path):
    """Resume needs the isolated CLI store's own directory (see
    ``_cli_history`` below) -- a fresh one per test, same as
    tests/test_resume.py's own fixture."""
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "doxa-home"))


def _status_plain(scope) -> str:
    """``scope`` is the app (single-pane tests) or a specific pane (a
    multi-tab test, where every pane's StatusBar shares the same id and
    an app-wide query would silently pick up whichever mounted first)."""
    return Content.from_markup(str(scope.query_one(StatusBar).renderable)).plain


async def _wait_status(pilot, scope, needle: str, tries: int = 300) -> bool:
    for _ in range(tries):
        if needle in _status_plain(scope):
            return True
        await pilot.pause(0.02)
    return needle in _status_plain(scope)


def _repo(tmp_path, name: str) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    run = lambda *a, **k: subprocess.run(*a, check=True, capture_output=True, **k)
    run(["git", "init", "-q"], cwd=repo)
    run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
         "--allow-empty", "-m", "init"], cwd=repo)
    return repo


def _slug_for(path) -> str:
    from lore_core.config import project_slug

    return project_slug(str(path))


def _store(monkeypatch, tmp_path, key: str, *, user: str = "", project: str = "",
           slug: str = "") -> Path:
    """Point lore_core's memory ROOT at a throwaway tree and fill it --
    same shape as tests/test_lore_line.py's own helper, keyed so this
    file can seed more than one project's store per test (the repo-switch
    and resume tests each need two)."""
    import lore_core.memory as lore_memory

    root = tmp_path / f"lore-store-{key}"
    (root / "projects" / slug).mkdir(parents=True, exist_ok=True)
    if user:
        (root / "USER.md").write_text(user, encoding="utf-8")
    if project:
        (root / "projects" / slug / "MEMORY.md").write_text(project, encoding="utf-8")
    return root


def _cli_history(session_id: str) -> Path:
    """Give the isolated CLI store a transcript under ``session_id`` --
    the on-disk fact ``resume_state`` needs before ``resume_session`` will
    proceed at all. Same helper as tests/test_resume.py's own."""
    path = (
        cli_isolation_mod.cli_config_dir() / "projects" / "-work"
        / f"{session_id}.jsonl"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"type":"user"}\n', encoding="utf-8")
    return path


# -- present at startup ------------------------------------------------------


@pytest.mark.asyncio
async def test_project_memory_present_at_startup(monkeypatch, tmp_path):
    """The pane is born with a placeholder cwd (the launch directory, not
    yet where the session actually connects); the engine reports the real
    project once it starts. The chip must read the ENGINE's answer, not
    the pre-boot guess -- a launch directory that owns no MEMORY.md must
    not be the reason the project half never shows."""
    launch = tmp_path / "launch"
    launch.mkdir()  # exists, but is not a git repo and owns no MEMORY.md
    repo = _repo(tmp_path, "repo")
    slug = _slug_for(repo)
    store = _store(
        monkeypatch, tmp_path, "a", slug=slug,
        user="- uv, never pip\n",
        project="- doxa lives here\n- the daemon lingers 90s\n",
    )
    import lore_core.memory as lore_memory
    from doxa.ui import labels as labels_mod

    monkeypatch.setattr(lore_memory, "ROOT", store)
    labels_mod._MEM_FILL_CACHE.clear()

    fake = FakeEngine([], cwd=str(repo))
    app = DoxaApp(cwd=str(launch), engine_factory=lambda: fake)
    async with app.run_test(size=(200, 40)) as pilot:
        await pilot.pause()
        assert await _wait_status(pilot, app, "mem u"), _status_plain(app)
        text = _status_plain(app)
        assert " p" in text.split("mem u", 1)[1].split("  ", 1)[0], text


# -- updates on a repo switch -------------------------------------------------


@pytest.mark.asyncio
async def test_project_memory_updates_on_repo_switch(monkeypatch, tmp_path):
    """``switch_engine`` -- the primitive ``/clear`` and ``/new session``
    already run -- hands a pane a NEW engine without a new pane. The chip
    must follow it to the new project, not keep reporting the one the tab
    was born in."""
    repo_a = _repo(tmp_path, "repo_a")
    repo_b = _repo(tmp_path, "repo_b")
    slug_a, slug_b = _slug_for(repo_a), _slug_for(repo_b)
    store = tmp_path / "lore-store"
    (store / "projects" / slug_a).mkdir(parents=True)
    (store / "projects" / slug_b).mkdir(parents=True)
    (store / "USER.md").write_text("- uv, never pip\n", encoding="utf-8")
    (store / "projects" / slug_a / "MEMORY.md").write_text(
        "- repo a fact\n", encoding="utf-8",
    )
    (store / "projects" / slug_b / "MEMORY.md").write_text(
        "- repo b fact, this one longer so the fills clearly differ\n"
        "- a second entry\n",
        encoding="utf-8",
    )
    import lore_core.memory as lore_memory
    from doxa.ui import labels as labels_mod

    monkeypatch.setattr(lore_memory, "ROOT", store)
    labels_mod._MEM_FILL_CACHE.clear()

    fake_a = FakeEngine([], cwd=str(repo_a))
    fake_b = FakeEngine([], cwd=str(repo_b))
    calls = {"n": 0}

    def new_session_factory():
        calls["n"] += 1
        return fake_b

    app = DoxaApp(
        cwd=str(repo_a), engine_factory=lambda: fake_a,
        new_session_factory=new_session_factory,
    )
    async with app.run_test(size=(200, 40)) as pilot:
        await pilot.pause()
        assert await _wait_status(pilot, app, "mem u")
        before = _status_plain(app)
        assert "mem u" in before

        app.query_one("#prompt-input").value = "/clear"
        await pilot.press("enter")
        for _ in range(300):
            if calls["n"] == 1 and fake_b.started:
                break
            await pilot.pause(0.02)
        assert calls["n"] == 1 and fake_b.started

        # The chip must settle on repo B's fill -- not repo A's, and not
        # disappear because the pane's own cwd never moved off repo A.
        for _ in range(300):
            after = _status_plain(app)
            if "mem u" in after and after != before:
                break
            await pilot.pause(0.02)
        after = _status_plain(app)
        assert "mem u" in after
        mem_after = after.split("mem u", 1)[1].split("  ", 1)[0]
        assert " p" in mem_after, after
        mem_before = before.split("mem u", 1)[1].split("  ", 1)[0]
        assert mem_after != mem_before, (
            "the chip kept reporting repo A's project fill after "
            f"switch_engine moved this tab to repo B: {after!r}"
        )


# -- updates on resume --------------------------------------------------------


@pytest.mark.asyncio
async def test_project_memory_updates_on_resume(monkeypatch, tmp_path):
    """A resumed conversation opens a new tab whose pane is born with the
    cwd the resume PICKER recorded for it -- which is not necessarily
    where the reconnected engine actually ends up (the picker's record
    can predate a worktree-per-session substitution, or simply be stale).
    The chip on that new tab must read where the session landed, not the
    picker's guess."""
    session_id = "11111111-2222-3333-4444-555555555555"
    _cli_history(session_id)
    stale_cwd = tmp_path / "stale"
    stale_cwd.mkdir()  # exists (resume_state requires that), owns no MEMORY.md

    repo = _repo(tmp_path, "resumed_repo")
    slug = _slug_for(repo)
    store = tmp_path / "lore-store"
    (store / "projects" / slug).mkdir(parents=True)
    (store / "USER.md").write_text("- uv, never pip\n", encoding="utf-8")
    (store / "projects" / slug / "MEMORY.md").write_text(
        "- resumed project fact\n", encoding="utf-8",
    )
    import lore_core.memory as lore_memory
    from doxa.ui import labels as labels_mod

    monkeypatch.setattr(lore_memory, "ROOT", store)
    labels_mod._MEM_FILL_CACHE.clear()

    origin = FakeEngine([], cwd=str(stale_cwd))
    resumed_engine = FakeEngine([], cwd=str(repo))

    def resume_session_factory(path: str, sid: str) -> FakeEngine:
        # The picker's own stale cwd is handed in here (see the pane's
        # construction-time cwd below) -- the engine reports the real
        # location once it connects, decoupled from that guess.
        return resumed_engine

    app = DoxaApp(cwd=str(stale_cwd), engine_factory=lambda: origin)
    app._resume_session_factory = resume_session_factory
    async with app.run_test(size=(200, 40)) as pilot:
        await pilot.pause()
        before = len(app.panes())
        note = await app.resume_session({
            "session_id": session_id, "cwd": str(stale_cwd), "title": "resumed",
        })
        assert note is None, note
        for _ in range(300):
            if len(app.panes()) > before:
                break
            await pilot.pause(0.02)
        assert len(app.panes()) == before + 1
        pane = app.panes()[-1]
        for _ in range(300):
            if pane.engine is resumed_engine and getattr(pane.engine, "started", False):
                break
            await pilot.pause(0.02)
        assert pane.engine is resumed_engine

        app.query_one("#session-tabs").active = pane.tab_id
        for _ in range(300):
            text = _status_plain(pane)
            if "mem u" in text and " p" in text.split("mem u", 1)[1].split("  ", 1)[0]:
                break
            await pilot.pause(0.02)
        text = _status_plain(pane)
        assert "mem u" in text, text
        mem = text.split("mem u", 1)[1].split("  ", 1)[0]
        assert " p" in mem, (
            f"resumed tab's chip never showed the project half: {text!r}"
        )

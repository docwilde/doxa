# SPDX-License-Identifier: AGPL-3.0-only
"""v0.56.0: the opening block's `lore` line, widened.

Reported: "the 'lore' line in the status/welcome box on startup should
also show how many pending, how many in user/project context and how full
each one is." Before, it read

    lore     /home/…/.claude/lore · 518 beliefs

and everything else LORE holds for the session was somewhere else or
nowhere.

Nothing here is newly derived. The fill percentages are v0.44.0's
``labels.memory_fill`` -- the exact character count read from the file
lore_core itself writes, cached on mtime -- so this line and the status
bar's `mem u63% p39%` chip cannot quote different numbers at each other.
The entry counts are lore_core's own ``read_entries`` over that same
file. The staged count is v0.31.0's paged ``list_pending``, called once by
``_boot``. And the project slug resolves through ``_lore_slug``, i.e.
through ``peers.main_repo_root_of``, which is the v0.47.0 fix and the one
thing that must not be re-derived from a raw cwd: every session runs in a
worktree, and a worktree's own slug owns no MEMORY.md.

The assertions read the identity block's RENDERED text rather than the
helper's return value, for the v0.28.0 reason -- a line that is computed
and not drawn passes every structural test there is.
"""

from __future__ import annotations

import pathlib
import subprocess

import pytest
from textual.geometry import Region

from doxa.app import DoxaApp, SystemBlock
from tests.fakes import FakeEngine


def _lore_line(app) -> str:
    """The `lore` row of the opening identity block, as drawn."""
    block = app.active_pane.query_one("#identity-block", SystemBlock)
    block._styles_cache.clear()
    region = Region(0, 0, block.size.width, block.size.height)
    rows = [
        "".join(segment.text for segment in strip)
        for strip in block.render_lines(region)
    ]
    for row in rows:
        if row.strip().startswith("lore "):
            return row.strip()
    raise AssertionError(f"no lore line in the identity block: {rows}")


def _store(monkeypatch, tmp_path, *, user: str = "", project: str = "",
           slug: str = "") -> pathlib.Path:
    """Point lore_core's memory ROOT at a throwaway tree and fill it."""
    import lore_core.memory as lore_memory

    root = tmp_path / "lore-store"
    (root / "projects" / slug).mkdir(parents=True, exist_ok=True)
    if user:
        (root / "USER.md").write_text(user, encoding="utf-8")
    if project:
        (root / "projects" / slug / "MEMORY.md").write_text(project, encoding="utf-8")
    monkeypatch.setattr(lore_memory, "ROOT", root)
    # v0.44.0's mtime cache is process-global; a fresh tmp store per test
    # would otherwise be read through a previous test's entry.
    from doxa.ui import labels as labels_mod

    labels_mod._MEM_FILL_CACHE.clear()
    return root


def _slug_for(path) -> str:
    from lore_core.config import project_slug

    return project_slug(str(path))


def _engine(pending: int = 0) -> FakeEngine:
    fake = FakeEngine([])
    fake.lore_root = "/fake/.claude/lore"
    fake.list_pending_result = [
        {"pid": f"2026-{i:02d}", "kind": "memory", "action": "add",
         "scope": "user", "text": f"proposal {i}"}
        for i in range(pending)
    ]
    return fake


@pytest.mark.asyncio
async def test_the_lore_line_carries_pending_and_both_memory_scopes(
    monkeypatch, tmp_path,
):
    """All four new facts on one line, drawn: how many staged, how many
    entries each scope holds, and how full each scope is."""
    slug = _slug_for(tmp_path)
    _store(
        monkeypatch, tmp_path, slug=slug,
        user="- uv, never pip\n- the operator runs zsh\n",
        project="- doxa lives at ~/doxa\n",
    )
    fake = _engine(pending=3)
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: fake)
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test(size=(200, 40)) as pilot:
        await pilot.pause()
        for _ in range(100):
            try:
                line = _lore_line(app)
            except AssertionError:
                await pilot.pause(0.02)
                continue
            if "pending" in line:
                break
            await pilot.pause(0.02)
        line = _lore_line(app)

        assert "3 pending" in line, line
        assert "user 2 entries" in line, line
        assert "project 1 entry" in line, line  # singular, not "1 entrys"
        # ...and a fill percentage for each, which is the "how full" half.
        after_user = line.split("user 2 entries", 1)[1]
        assert after_user.lstrip().split()[0].endswith("%"), line
        after_project = line.split("project 1 entry", 1)[1]
        assert after_project.lstrip().split()[0].endswith("%"), line
        # The line it grew out of is still all there.
        assert "/fake/.claude/lore" in line and "beliefs" in line


@pytest.mark.asyncio
async def test_a_scope_with_no_file_is_omitted_rather_than_reported_as_zero(
    monkeypatch, tmp_path,
):
    """"Absent fields are omitted, not invented" is this block's own rule
    (see _identity_text). A session outside any project has no MEMORY.md,
    and `project 0 entries 0%` would be a measurement nobody took."""
    slug = _slug_for(tmp_path)
    _store(monkeypatch, tmp_path, slug=slug, user="- uv, never pip\n")
    fake = _engine(pending=0)
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: fake)
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test(size=(200, 40)) as pilot:
        await pilot.pause()
        for _ in range(100):
            if "user 1 entry" in _lore_line(app):
                break
            await pilot.pause(0.02)
        line = _lore_line(app)
        assert "user 1 entry" in line, line
        assert "project" not in line, line


@pytest.mark.asyncio
async def test_zero_pending_is_stated_not_hidden(monkeypatch, tmp_path):
    """Hide-at-zero is the STATUS BAR's convention, where a chip competes
    for a row of width. This is a boot report, and "0 pending" is the
    answer to a question the reader asked -- hiding it leaves them unable
    to tell a clear queue from a lookup that failed."""
    slug = _slug_for(tmp_path)
    _store(monkeypatch, tmp_path, slug=slug, user="- one\n")
    fake = _engine(pending=0)
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: fake)
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test(size=(200, 40)) as pilot:
        await pilot.pause()
        for _ in range(100):
            if "pending" in _lore_line(app):
                break
            await pilot.pause(0.02)
        assert "0 pending" in _lore_line(app)


@pytest.mark.asyncio
async def test_an_engine_that_cannot_answer_leaves_the_rest_of_the_line_intact(
    monkeypatch, tmp_path,
):
    """The staged count costs a socket round trip to the daemon. A daemon
    that refuses it must cost the fact, never the boot: the line loses
    `N pending` and keeps everything else."""
    slug = _slug_for(tmp_path)
    _store(monkeypatch, tmp_path, slug=slug, user="- one\n")
    fake = _engine(pending=2)
    fake.list_pending_error = RuntimeError("daemon is busy")
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: fake)
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test(size=(200, 40)) as pilot:
        await pilot.pause()
        for _ in range(100):
            if "user 1 entry" in _lore_line(app):
                break
            await pilot.pause(0.02)
        line = _lore_line(app)
        assert "pending" not in line, line
        assert "user 1 entry" in line, line
        assert "beliefs" in line, line


@pytest.mark.asyncio
async def test_a_worktree_session_still_finds_its_project_memory(
    monkeypatch, tmp_path,
):
    """The v0.47.0 defect, guarded on the new surface before it can be
    reproduced there.

    Every repo session runs in a worktree since v0.17.0. Resolving the
    LORE slug from the pane's raw cwd answers "which DIRECTORY" when the
    question is "which PROJECT" -- the worktree owns no MEMORY.md, so the
    project half silently vanishes for the normal case. Asserted against
    real git repos rather than a stubbed mapping: the mapping is git's own
    (`rev-parse --git-common-dir`), and a mocked one would pass on the
    broken code."""
    run = lambda *a, **k: subprocess.run(*a, check=True, capture_output=True, **k)
    main = tmp_path / "repo"
    main.mkdir()
    run(["git", "init", "-q"], cwd=main)
    run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
         "--allow-empty", "-m", "init"], cwd=main)
    worktree = tmp_path / "wt"
    run(["git", "worktree", "add", "-q", "-b", "doxa/x", str(worktree)], cwd=main)

    main_slug = _slug_for(main)
    worktree_slug = _slug_for(worktree)
    assert main_slug != worktree_slug, "the premise of the defect no longer holds"

    # The project memory exists ONLY under the main repo's slug, which is
    # exactly how it sits on a real machine.
    _store(
        monkeypatch, tmp_path, slug=main_slug,
        user="- uv, never pip\n",
        project="- doxa lives here\n- the daemon lingers 90s\n",
    )

    fake = _engine(pending=1)
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: fake)
    app = DoxaApp(cwd=str(worktree))
    async with app.run_test(size=(200, 40)) as pilot:
        await pilot.pause()
        for _ in range(200):
            if "project" in _lore_line(app):
                break
            await pilot.pause(0.02)
        line = _lore_line(app)
        assert "project 2 entries" in line, line


def test_memory_entries_reads_lore_s_own_parser(tmp_path, monkeypatch):
    """Counting `- ` lines here instead would be doxa reimplementing
    lore_core's storage format, which is how the two drift. The helper
    must go through read_entries -- including on the shapes a hand-rolled
    count gets wrong: headings, blank lines, and a wrapped continuation."""
    import lore_core.memory as lore_memory

    from doxa.ui.labels import memory_entries

    store = tmp_path / "USER.md"
    store.write_text(
        "# curated memory\n\n- uv, never pip\n\n- the operator runs zsh\n"
        "  continued on a second line\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(lore_memory, "memory_path", lambda scope, slug: store)
    assert memory_entries("user") == len(lore_memory.read_entries(store))


def test_memory_entries_on_a_missing_file_is_none_not_zero(tmp_path, monkeypatch):
    """None and 0 mean different things on this line: None omits the
    scope, 0 would claim an empty file was measured."""
    import lore_core.memory as lore_memory

    from doxa.ui.labels import memory_entries

    monkeypatch.setattr(
        lore_memory, "memory_path", lambda scope, slug: tmp_path / "nope.md"
    )
    assert memory_entries("user") is None

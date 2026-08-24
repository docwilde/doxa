"""The status line: containment signal, real headroom, and chip ORDER.

Three properties, each with a reason:

* the context chip escalates by COLOR and keeps its percentage in every
  tier -- a color that replaced the number would be decoration, and this
  chip exists to be a containment signal;
* the headroom chip shows only numbers that are real (the `claude` CLI's
  own cached utilization) and is recomputed at most once per turn-done,
  never on a timer;
* the git short sha sits immediately right of the branch it qualifies, and
  is read event-driven -- a commit moves the ref file, not HEAD, which is
  why the sha needs its own stat.
"""

from __future__ import annotations

import json
import subprocess
import time

import pytest

from doxa import identity
from doxa.app import (
    CLICKABLE_CHIP_ACCENT,
    CTX_AMBER,
    CTX_AMBER_PCT,
    CTX_RED,
    CTX_RED_PCT,
    DoxaApp,
    GitLine,
    compose_tab_label,
    ctx_chip,
)
from doxa.engine import EngineEvent
from tests.fakes import FakeEngine


# -- (a) context escalation ----------------------------------------------


def test_ctx_chip_escalates_and_always_shows_the_percentage():
    assert ctx_chip(12.0) == "ctx 12%"
    assert CTX_AMBER in ctx_chip(CTX_AMBER_PCT)
    assert CTX_AMBER in ctx_chip(80.0)
    assert CTX_RED in ctx_chip(CTX_RED_PCT)
    assert CTX_RED in ctx_chip(99.4)
    # The number survives every tier -- that is the whole rule.
    for value in (12.0, 74.0, 95.0):
        assert f"{value:.0f}%" in ctx_chip(value)
    assert ctx_chip(None) == "ctx —"


def test_ctx_thresholds_are_the_documented_ones():
    assert (CTX_AMBER_PCT, CTX_RED_PCT) == (70.0, 90.0)
    assert ctx_chip(CTX_AMBER_PCT - 0.1) == "ctx 70%"  # below amber: plain
    assert CTX_AMBER in ctx_chip(CTX_RED_PCT - 0.1)


@pytest.mark.asyncio
async def test_status_line_colors_the_chip_after_a_pressured_turn(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    script = [
        EngineEvent("turn_started", {}),
        EngineEvent("turn_done", {"cost_usd": 0.0, "ctx_percentage": 93.0}),
    ]
    monkeypatch.setattr(
        "doxa.app.SessionEngine", lambda cwd, model=None: FakeEngine(script)
    )
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#prompt-input").value = "go"
        await pilot.press("enter")
        for _ in range(200):
            status = str(app.query_one("#status-bar").renderable)
            if "93%" in status:
                break
            await pilot.pause(0.02)
        assert CTX_RED in status and "ctx 93%" in status


# -- (b) the headroom chip -----------------------------------------------


def _write_utilization(tmp_path, session=9, weekly=48):
    (tmp_path / ".claude.json").write_text(
        json.dumps({
            "cachedUsageUtilization": {
                "fetchedAtMs": int(time.time() * 1000),
                "utilization": {"limits": [
                    {"kind": "session", "percent": session, "severity": "normal",
                     "resets_at": ""},
                    {"kind": "weekly_all", "percent": weekly, "severity": "normal",
                     "resets_at": ""},
                ]},
            },
        }),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_headroom_chip_shows_real_cached_numbers(monkeypatch, tmp_path):
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    identity.invalidate()
    _write_utilization(tmp_path)
    monkeypatch.setattr(
        "doxa.app.SessionEngine", lambda cwd, model=None: FakeEngine([])
    )
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        for _ in range(200):
            status = str(app.query_one("#status-bar").renderable)
            if "s:9%" in status:
                break
            await pilot.pause(0.02)
        assert "s:9% w:48%" in status
    identity.invalidate()


@pytest.mark.asyncio
async def test_no_headroom_chip_when_nothing_is_cached(monkeypatch, tmp_path):
    """API-key auth, or a CLI that never fetched one: show NOTHING rather
    than a fabricated zero. The $ tally beside it is the honest figure."""
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "empty"))
    identity.invalidate()
    monkeypatch.setattr(
        "doxa.app.SessionEngine", lambda cwd, model=None: FakeEngine([])
    )
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(100):
            if app.active_pane is not None and app.active_pane._git is not None:
                break
            await pilot.pause(0.02)
        status = str(app.query_one("#status-bar").renderable)
        assert "s:" not in status and "w:" not in status
        assert "$0.0000" in status
    identity.invalidate()


@pytest.mark.asyncio
async def test_headroom_chip_refreshes_on_turn_done_and_never_on_a_timer(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    identity.invalidate()
    _write_utilization(tmp_path, session=9, weekly=48)

    calls: list[float] = []
    real = identity.usage

    def counted():
        calls.append(time.monotonic())
        return real()

    monkeypatch.setattr(identity, "usage", counted)
    script = [
        EngineEvent("turn_started", {}),
        EngineEvent("turn_done", {"cost_usd": 0.0, "ctx_percentage": 5.0}),
    ]
    monkeypatch.setattr(
        "doxa.app.SessionEngine", lambda cwd, model=None: FakeEngine(script)
    )
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        for _ in range(200):
            if "s:9%" in str(app.query_one("#status-bar").renderable):
                break
            await pilot.pause(0.02)
        assert len(calls) == 1  # boot only

        # Sitting idle must cost nothing: no timer refreshes this chip.
        await pilot.pause(0.4)
        assert len(calls) == 1

        # The CLI refreshes its cache; the next turn-done picks it up.
        _write_utilization(tmp_path, session=31, weekly=52)
        app.query_one("#prompt-input").value = "go"
        await pilot.press("enter")
        for _ in range(200):
            status = str(app.query_one("#status-bar").renderable)
            if "s:31%" in status:
                break
            await pilot.pause(0.02)
        assert "s:31% w:52%" in status
        assert len(calls) == 2  # exactly one more: per turn-done, not per event
    identity.invalidate()


# -- (c) the git chip's order --------------------------------------------


def _repo(tmp_path, branch="trunk"):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", branch, str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    (repo / "f.txt").write_text("one", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "one"], check=True)
    return repo


def _short_sha(repo) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--short=7", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def test_git_chip_puts_the_sha_immediately_right_of_the_branch(tmp_path):
    """...and marks it as a COMMIT with "@". The bar also carries the
    session handle, another short hex-ish id: two unlabelled hex strings a
    few chips apart read as one commit id printed twice."""
    repo = _repo(tmp_path)
    chip = GitLine(str(repo)).render()
    assert chip == f"myrepo ⎇ trunk @{_short_sha(repo)}"


def test_git_chip_follows_a_new_commit_without_polling(tmp_path):
    """A commit moves the REF file, not HEAD -- so the sha has its own
    stat, and the next event-driven render sees it."""
    repo = _repo(tmp_path)
    line = GitLine(str(repo))
    first = line.render()
    (repo / "f.txt").write_text("two", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "commit", "-qam", "two"], check=True)
    line._sha_mtime = None  # defeat same-second mtime granularity
    second = line.render()
    assert first != second
    assert second == f"myrepo ⎇ trunk @{_short_sha(repo)}"


def test_git_chip_reads_a_packed_ref(tmp_path):
    repo = _repo(tmp_path)
    subprocess.run(["git", "-C", str(repo), "pack-refs", "--all"], check=True)
    assert GitLine(str(repo)).render() == f"myrepo ⎇ trunk @{_short_sha(repo)}"


def test_git_chip_omits_a_sha_that_would_repeat_the_branch(tmp_path):
    """Detached HEAD already shows the sha as its 'branch' -- printing it
    twice is noise, not information."""
    repo = _repo(tmp_path)
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", sha], check=True)
    chip = GitLine(str(repo)).render()
    assert chip == f"myrepo ⎇ {sha[:8]}"


# -- the git chip inside a linked worktree ---------------------------------
#
# render() builds its branch half from branch_label() -- the SAME method a
# tab label uses -- so the worktree-suffix dedup rule (only append when the
# worktree's name differs from BOTH the branch and the repo slot beside it)
# is inherited here rather than re-implemented.


def test_git_chip_is_plain_in_a_normal_checkout(tmp_path):
    """No worktree at all: nothing for branch_label() to append, so the
    chip reads exactly as it always did."""
    repo = _repo(tmp_path)
    line = GitLine(str(repo))
    assert line.worktree is None
    assert line.render() == f"myrepo ⎇ trunk @{_short_sha(repo)}"


def test_git_chip_carries_the_worktree_suffix(tmp_path):
    """Inside a linked worktree whose directory name says something the
    branch does not: `repo ⎇ branch@worktree`, repo being the MAIN
    checkout's name (item S's fix: NOT this worktree directory's own name
    -- see test_git_chip_repo_slot_is_the_main_checkout_not_the_worktree
    below). `git worktree add` names the worktree after the directory it
    just created, so -- same construction
    test_branch_label_names_a_linked_worktree uses in test_tab_labels.py
    -- a worktree name that differs from BOTH the branch and the repo
    slot is simulated the same way branch_label()'s own tests do.

    The `@sha` DOES appear here (worktree-per-session, #3): a linked
    worktree's checked-out branch ref lives in the MAIN repo's
    `.git/refs/heads/`, not under this worktree's own private gitdir
    (`.git/worktrees/<name>/`) -- `_read_sha` used to stat the ref under
    the wrong gitdir and silently come back None for every worktree
    session. Fixed by resolving through the worktree's `commondir`
    pointer (see GitLine._resolve_commondir) before reading the ref."""
    repo = _repo(tmp_path)
    work = repo.parent / "elsewhere"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-q", "-b", "feature",
         str(work)],
        check=True,
    )
    line = GitLine(str(work))
    line.worktree = "spike"
    assert line.render() == f"myrepo ⎇ feature@spike @{_short_sha(repo)}"


def test_git_chip_repo_slot_is_the_main_checkout_not_the_worktree(tmp_path):
    """The reported regression (item S, folded in as a defect-then-fix):
    `GitLine.repo` used to read `Path(repo_root).name`, and since v0.17
    every session's cwd IS a linked worktree -- `git rev-parse
    --show-toplevel` from inside one names the WORKTREE, not the repo, so
    the chip read `doxa-f13526d4 ⎇ doxa/f13526d4`, the session id printed
    twice. Fixed by resolving the repo slot through the worktree's
    `commondir` pointer (already read for the sha above) instead."""
    repo = _repo(tmp_path)
    work = repo.parent / "elsewhere"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-q", "-b", "feature",
         str(work)],
        check=True,
    )
    line = GitLine(str(work))
    assert line.repo == "myrepo"
    assert line.render() == f"myrepo ⎇ feature@elsewhere @{_short_sha(repo)}"
    branch, isolated = line.tab_branch()
    label = compose_tab_label("Opus", line.repo, branch, isolated=isolated)
    # The session id (the worktree/branch's OWN name, "elsewhere"/
    # "feature") no longer appears twice in one label.
    assert label.count("elsewhere") <= 1


def test_git_chip_dedups_when_the_worktree_name_repeats_branch_or_repo(tmp_path):
    """The worktree name is withheld from branch_label() when it repeats
    either the branch or the (now MAIN-checkout) repo slot beside it --
    saying the same fact twice is worse than saying it once."""
    repo = _repo(tmp_path)
    work = repo.parent / "elsewhere"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-q", "-b", "feature",
         str(work)],
        check=True,
    )
    line = GitLine(str(work))
    assert line.worktree == "elsewhere"
    assert line.repo == "myrepo"
    sha = _short_sha(repo)

    # Matches the BRANCH: withheld.
    line.worktree = "feature"
    assert line.render() == f"myrepo ⎇ feature @{sha}"

    # Matches the REPO slot: withheld too.
    line.worktree = "myrepo"
    assert line.render() == f"myrepo ⎇ feature @{sha}"

    # Matches neither: carries real information, so it IS appended.
    line.worktree = "elsewhere"
    assert line.render() == f"myrepo ⎇ feature@elsewhere @{sha}"


@pytest.mark.asyncio
async def test_status_line_chip_order(monkeypatch, tmp_path):
    """Pinned ORDER: model · repo ⎇ branch sha · cost · headroom · ctx ·
    beliefs. The sha belongs next to the branch it qualifies."""
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    identity.invalidate()
    _write_utilization(tmp_path)
    repo = _repo(tmp_path)
    fake = FakeEngine([])
    fake.account = {"subscriptionType": "Claude Max"}
    fake.last_ctx_percentage = 74.0
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: fake)
    app = DoxaApp(cwd=str(repo))
    async with app.run_test() as pilot:
        for _ in range(200):
            status = str(app.query_one("#status-bar").renderable)
            if "myrepo" in status:
                break
            await pilot.pause(0.02)
        sha = _short_sha(repo)
        # Status-chips (item Y): the model chip and the git chip's branch
        # span are now click-action spans (`[@click=...][accent]...[/][/]`)
        # -- `.renderable` is the RAW string handed to `Static.update`,
        # markup and all, so the chunks below match that literal wire
        # format rather than the pre-chips plain text.
        accent = CLICKABLE_CHIP_ACCENT
        order = [
            f"[@click=open_model_picker][{accent}]{fake.model}[/][/]",
            f"myrepo ⎇ [@click=open_branch_picker][{accent}]trunk[/][/] @{sha}",
            "sub:max",
            "s:9% w:48%",
            f"[@click=compact_now][{accent}]{ctx_chip(74.0)}[/][/]",
            "3 beliefs",
        ]
        positions = [status.index(chunk) for chunk in order]
        assert positions == sorted(positions), status
    identity.invalidate()


@pytest.mark.asyncio
async def test_the_two_hex_ids_in_the_bar_are_told_apart(monkeypatch, tmp_path):
    """The reported "commit id appears doubled": nothing renders twice --
    the git sha and the detached-session handle are both short hex-ish
    strings a few chips apart, and unlabelled they read as one id printed
    twice. Both stay; both are labelled."""
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    repo = _repo(tmp_path)

    class Detachable(FakeEngine):
        detachable = True

        def __init__(self):
            super().__init__([])
            self.session_id = "4f8e2a91-77bc-4c1d-9a01-000000000000"

    monkeypatch.setattr(
        "doxa.app.SessionEngine", lambda cwd, model=None: Detachable()
    )
    app = DoxaApp(cwd=str(repo))
    async with app.run_test() as pilot:
        for _ in range(200):
            status = str(app.query_one("#status-bar").renderable)
            if "myrepo" in status:
                break
            await pilot.pause(0.02)
        sha = _short_sha(repo)
        # Each id appears exactly once, and each says what kind it is.
        assert status.count(sha) == 1
        assert status.count("4f8e2a91") == 1
        assert f"@{sha}" in status
        assert "⌁ session 4f8e2a91" in status
        # ...and no bare hex string sits in the bar unlabelled.
        assert f" {sha} " not in status

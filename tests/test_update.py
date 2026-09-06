# SPDX-License-Identifier: AGPL-3.0-only
"""Version resolution and `/update`.

The version has ONE source (pyproject.toml) and two readers, and a source
checkout must never report "unknown". `/update` is deliberately narrow --
fast-forward or refuse -- so what is pinned here is mostly the refusals,
plus the two things a successful pull owes the user: what came in, and
whether the dependencies moved under it.

Git is mocked at the single injected `run` seam, so every branch is
exercised without a network or a second repository.

DOXA_SKIP_UPDATE_CHECK is cleared for every test in this module. conftest.py
sets it suite-wide (v1.7.5) so that no DoxaApp mount anywhere opens a real
`git fetch`, but check_for_update honors it BEFORE it looks at anything --
including a scripted `run` -- so leaving it set here would turn every
"silent when ..." test below green for the wrong reason: they all assert
False, and a function that declines to look also returns False. The two
tests that are ABOUT the switch set it themselves.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from doxa import update as update_mod
from doxa import version as version_mod

SUITE_WIDE_SKIP = os.environ.get("DOXA_SKIP_UPDATE_CHECK", "")
"""Whatever conftest.py set at import time, captured before the fixture
below clears it -- so one test can still pin that the suite-wide default
is armed."""


@pytest.fixture(autouse=True)
def _boot_check_kill_switch_cleared(monkeypatch):
    monkeypatch.delenv("DOXA_SKIP_UPDATE_CHECK", raising=False)


class FakeGit:
    """Scripted `run`: maps a command's first two words to a result."""

    def __init__(self, replies: dict, ok: bool = True):
        self.replies = replies
        self.calls: list[list[str]] = []
        self.ok = ok

    def __call__(self, cmd, cwd, timeout):
        self.calls.append(list(cmd))
        key = " ".join(cmd[:2])
        stdout, code = self.replies.get(key, ("", 0))
        return subprocess.CompletedProcess(cmd, code, stdout=stdout, stderr="")


# -- the version ----------------------------------------------------------


def test_a_source_checkout_never_reports_unknown():
    root = version_mod.source_root()
    assert root is not None                      # we ARE a checkout here
    assert (root / "pyproject.toml").is_file()
    version = version_mod.resolve_version()
    assert version != "unknown"
    assert version[0].isdigit()
    import doxa

    assert doxa.__version__ == version


def test_the_checkout_wins_over_installed_metadata(monkeypatch):
    monkeypatch.setattr(version_mod, "_from_pyproject", lambda: "9.9.9")
    monkeypatch.setattr(version_mod, "_from_metadata", lambda: "0.0.1")
    assert version_mod.resolve_version() == "9.9.9"
    # ...and an installed copy (no checkout) falls through to metadata.
    monkeypatch.setattr(version_mod, "_from_pyproject", lambda: None)
    assert version_mod.resolve_version() == "0.0.1"


def test_the_version_line_shows_a_sha_only_when_it_adds_something(monkeypatch):
    monkeypatch.setattr(version_mod, "resolve_version", lambda: "0.4.0")
    monkeypatch.setattr(version_mod, "source_sha", lambda: "a1b2c3d")
    monkeypatch.setattr(version_mod, "source_dirty", lambda: False)

    # The git chip is already showing this very sha: do not print it twice.
    assert version_mod.version_line("a1b2c3d") == "DOXA 0.4.0"
    # A different repo on screen (or no chip at all): the sha is news.
    assert version_mod.version_line("9999999") == "DOXA 0.4.0 (a1b2c3d)"
    assert version_mod.version_line(None) == "DOXA 0.4.0 (a1b2c3d)"
    # A dirty checkout is news no chip carries, even at the same sha.
    monkeypatch.setattr(version_mod, "source_dirty", lambda: True)
    assert version_mod.version_line("a1b2c3d") == "DOXA 0.4.0 (a1b2c3d+)"


# -- /update refusals -----------------------------------------------------


def test_update_refuses_a_non_repo(tmp_path):
    report = update_mod.update(root=tmp_path, run=FakeGit({}))
    assert report.status == "refused"
    assert "not a git checkout" in report.message


def test_update_refuses_a_dirty_tree(tmp_path):
    (tmp_path / ".git").mkdir()
    git = FakeGit({"git status": (" M doxa/app.py\n?? scratch.py\n", 0)})
    report = update_mod.update(root=tmp_path, run=git)
    assert report.status == "refused"
    assert "uncommitted changes" in report.message
    assert "doxa/app.py" in report.message
    # Nothing was pulled -- the refusal came first.
    assert not any(c[:2] == ["git", "pull"] for c in git.calls)


def test_update_refuses_a_diverged_checkout(tmp_path):
    (tmp_path / ".git").mkdir()
    git = FakeGit({
        "git status": ("", 0),
        "git rev-parse": ("aaaaaaaaaa", 0),
        "git pull": ("fatal: Not possible to fast-forward, aborting.", 1),
    })
    report = update_mod.update(root=tmp_path, run=git)
    assert report.status == "refused"
    assert "fast-forward refused" in report.message
    assert "will not merge or rebase" in report.message


# -- /update success paths ------------------------------------------------


def test_update_is_a_one_liner_when_already_current(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(version_mod, "resolve_version", lambda: "0.4.0")
    git = FakeGit({
        "git status": ("", 0),
        "git rev-parse": ("samesha", 0),
        "git pull": ("Already up to date.", 0),
    })
    report = update_mod.update(root=tmp_path, run=git)
    assert report.status == "up-to-date"
    assert report.text() == "update: already up to date (0.4.0)"


def test_update_reports_what_came_in(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    versions = iter(["0.4.0", "0.5.0"])
    monkeypatch.setattr(version_mod, "resolve_version", lambda: next(versions))

    shas = iter(["aaaaaaa1111", "bbbbbbb2222"])
    def run(cmd, cwd, timeout):
        key = " ".join(cmd[:2])
        if key == "git rev-parse":
            return subprocess.CompletedProcess(cmd, 0, next(shas), "")
        replies = {
            "git status": "",
            "git pull": "Fast-forward",
            "git log": "bbbbbbb feat(x): a thing\nccccccc fix(y): another\n",
            "git diff": "doxa/app.py\nREADME.md\n",
        }
        return subprocess.CompletedProcess(cmd, 0, replies.get(key, ""), "")

    report = update_mod.update(root=tmp_path, run=run)
    assert report.status == "updated"
    assert len(report.commits) == 2
    text = report.text()
    assert "2 commits pulled" in text
    assert "feat(x): a thing" in text
    assert "0.4.0 → 0.5.0" in text
    assert report.synced is False           # no dependency file moved
    # It never restarts anything by itself; it says how.
    assert "/update --restart" in text


def test_a_dependency_change_runs_uv_sync(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(version_mod, "resolve_version", lambda: "0.4.0")
    shas = iter(["aaaaaaa", "bbbbbbb"])
    calls: list[list[str]] = []

    def run(cmd, cwd, timeout):
        calls.append(list(cmd))
        key = " ".join(cmd[:2])
        if key == "git rev-parse":
            return subprocess.CompletedProcess(cmd, 0, next(shas), "")
        replies = {
            "git status": "",
            "git pull": "Fast-forward",
            "git log": "bbbbbbb chore: bump textual\n",
            "git diff": "uv.lock\npyproject.toml\n",
            "uv sync": "Resolved 42 packages\nInstalled 2 packages",
        }
        return subprocess.CompletedProcess(cmd, 0, replies.get(key, ""), "")

    report = update_mod.update(root=tmp_path, run=run)
    assert report.synced is True
    assert ["uv", "sync"] in calls           # RUN, not merely suggested
    assert "Installed 2 packages" in report.text()


# -- the command surface --------------------------------------------------


@pytest.mark.asyncio
async def test_the_restart_flag_is_opt_in(monkeypatch, tmp_path):
    """`/update` alone never touches a running session; `--restart` asks
    the CLI (after the app exits) to relaunch."""
    from doxa.app import DoxaApp, SystemBlock
    from tests.fakes import FakeEngine

    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setattr(
        update_mod, "update",
        lambda *a, **k: update_mod.UpdateReport(
            status="updated", message="update: fast-forwarded aaa → bbb",
            commits=["bbb feat: thing"], version_before="0.4.0",
            version_after="0.5.0",
        ),
    )
    engine = FakeEngine([])
    app = DoxaApp(cwd=str(tmp_path), engine_factory=lambda: engine,
                  new_session_factory=lambda: engine)
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.active_pane
        await pane._cmd_update("")
        await pilot.pause()
        assert app.restart_requested is False
        blocks = [b.text for b in pane.query(SystemBlock) if "update:" in b.text]
        assert "0.4.0 → 0.5.0" in blocks[-1]

        await pane._cmd_update("--restart")
        for _ in range(100):
            if app.restart_requested:
                break
            await pilot.pause(0.02)
        assert app.restart_requested is True


# -- boot-time update check (git level) -------------------------------------


def test_check_for_update_true_when_upstream_is_ahead(tmp_path):
    (tmp_path / ".git").mkdir()
    git = FakeGit({"git fetch": ("", 0), "git rev-list": ("3\n", 0)})
    assert update_mod.check_for_update(root=tmp_path, run=git) is True


def test_check_for_update_false_when_already_current(tmp_path):
    (tmp_path / ".git").mkdir()
    git = FakeGit({"git fetch": ("", 0), "git rev-list": ("0\n", 0)})
    assert update_mod.check_for_update(root=tmp_path, run=git) is False


def test_check_for_update_silent_when_not_a_checkout(tmp_path):
    assert update_mod.check_for_update(root=tmp_path, run=FakeGit({})) is False


def test_check_for_update_silent_when_fetch_fails(tmp_path):
    (tmp_path / ".git").mkdir()
    git = FakeGit({"git fetch": ("fatal: unable to access", 1)})
    assert update_mod.check_for_update(root=tmp_path, run=git) is False
    # rev-list is never reached once fetch has already failed.
    assert not any(c[:2] == ["git", "rev-list"] for c in git.calls)


def test_check_for_update_silent_when_no_upstream_is_configured(tmp_path):
    (tmp_path / ".git").mkdir()
    git = FakeGit({
        "git fetch": ("", 0),
        "git rev-list": ("fatal: no upstream configured", 1),
    })
    assert update_mod.check_for_update(root=tmp_path, run=git) is False


def test_check_for_update_silent_when_git_itself_is_unavailable(tmp_path):
    (tmp_path / ".git").mkdir()

    def boom(cmd, cwd, timeout):
        raise FileNotFoundError("git: command not found")

    assert update_mod.check_for_update(root=tmp_path, run=boom) is False


# -- the boot check must not touch the network in the suite (v1.7.5) --------
#
# DoxaApp.on_mount runs check_for_update off a worker, and on a checkout
# -- which is what the suite runs from -- that opened a real `git fetch`
# against origin on EVERY mount. Measured before the fix on
# tests/test_app.py alone: 12 fetches for 13 tests, 17.5s of subprocess
# time inside a 24.4s module.
#
# What is asserted is that no git subprocess is REACHED, never that the
# mount was quick.


def test_check_for_update_declines_to_look_when_the_kill_switch_is_set(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("DOXA_SKIP_UPDATE_CHECK", "1")
    (tmp_path / ".git").mkdir()
    git = FakeGit({"git fetch": ("", 0), "git rev-list": ("3\n", 0)})
    # Upstream IS ahead; the point is that nothing goes and looks.
    assert update_mod.check_for_update(root=tmp_path, run=git) is False
    assert git.calls == []


def test_check_for_update_still_looks_when_the_kill_switch_is_clear(tmp_path):
    """The switch removes the suite's network traffic, not the feature."""
    (tmp_path / ".git").mkdir()
    git = FakeGit({"git fetch": ("", 0), "git rev-list": ("3\n", 0)})
    assert update_mod.check_for_update(root=tmp_path, run=git) is True
    assert [c[:2] for c in git.calls] == [["git", "fetch"], ["git", "rev-list"]]


def test_the_boot_check_runs_on_its_own_short_timeout(tmp_path):
    """An advisory probe nobody asked for may not hold a worker thread and
    a live git child for the two minutes /update's own pull is allowed."""
    (tmp_path / ".git").mkdir()
    timeouts: "list[float]" = []

    def record(cmd, cwd, timeout):
        timeouts.append(timeout)
        return subprocess.CompletedProcess(cmd, 0, stdout="0\n", stderr="")

    update_mod.check_for_update(root=tmp_path, run=record)
    assert timeouts == [
        update_mod.CHECK_TIMEOUT_SECS, update_mod.CHECK_TIMEOUT_SECS
    ]
    assert update_mod.CHECK_TIMEOUT_SECS < update_mod.GIT_TIMEOUT_SECS


@pytest.mark.asyncio
async def test_mounting_the_app_opens_no_git_subprocess(monkeypatch, tmp_path):
    """conftest.py sets DOXA_SKIP_UPDATE_CHECK suite-wide (re-set here,
    because this module's autouse fixture clears it); this pins that the
    app actually honors it end to end.

    The wait predicate is `update_available is not None` -- the very last
    thing _check_for_update writes -- rather than a pause of some length,
    so the assertion runs strictly AFTER the worker has finished deciding.
    A weaker predicate would pass before the check had run at all, which
    is the same green for the wrong reason."""
    from doxa.app import DoxaApp
    from tests.fakes import FakeEngine

    monkeypatch.setenv("DOXA_SKIP_UPDATE_CHECK", "1")
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    calls: "list[list[str]]" = []
    monkeypatch.setattr(
        update_mod, "_run",
        lambda cmd, cwd, timeout: calls.append(list(cmd))
        or subprocess.CompletedProcess(cmd, 0, stdout="0\n", stderr=""),
    )
    engine = FakeEngine([])
    app = DoxaApp(
        cwd=str(tmp_path), engine_factory=lambda: engine,
        new_session_factory=lambda: engine,
    )
    async with app.run_test() as pilot:
        for _ in range(200):
            if app.update_available is not None:
                break
            await pilot.pause(0.02)
        assert app.update_available is not None, "the boot check never ran"
    assert calls == []


def test_the_suite_arms_the_kill_switch_for_every_other_module(tmp_path):
    """Everything above clears the switch on purpose. Nothing else in the
    suite does, and this is the assertion that says so -- without it, a
    conftest.py line quietly deleted would cost several hundred live `git
    fetch` calls per run and nothing would go red."""
    assert SUITE_WIDE_SKIP.strip(), (
        "tests/conftest.py must set DOXA_SKIP_UPDATE_CHECK -- without it "
        "every DoxaApp mount in this suite opens a real network git fetch"
    )


# -- boot-time update check (app-worker level) -------------------------------


@pytest.mark.asyncio
async def test_boot_notifies_once_when_an_update_is_available(monkeypatch, tmp_path):
    from doxa.app import DoxaApp
    from doxa import notify as notify_mod
    from tests.fakes import FakeEngine

    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setattr(update_mod, "check_for_update", lambda *a, **k: True)
    calls: list[bool] = []
    monkeypatch.setattr(
        notify_mod, "notify_update_available", lambda focus: calls.append(focus)
    )
    engine = FakeEngine([])
    app = DoxaApp(
        cwd=str(tmp_path), engine_factory=lambda: engine,
        new_session_factory=lambda: engine,
    )
    async with app.run_test() as pilot:
        for _ in range(100):
            if calls:
                break
            await pilot.pause(0.02)
    assert calls == [True]  # app_has_focus starts True


@pytest.mark.asyncio
async def test_boot_stays_silent_when_nothing_is_available(monkeypatch, tmp_path):
    from doxa.app import DoxaApp
    from doxa import notify as notify_mod
    from tests.fakes import FakeEngine

    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setattr(update_mod, "check_for_update", lambda *a, **k: False)
    calls: list[bool] = []
    monkeypatch.setattr(
        notify_mod, "notify_update_available", lambda focus: calls.append(focus)
    )
    engine = FakeEngine([])
    app = DoxaApp(
        cwd=str(tmp_path), engine_factory=lambda: engine,
        new_session_factory=lambda: engine,
    )
    async with app.run_test() as pilot:
        await pilot.pause(0.3)
    assert calls == []


@pytest.mark.asyncio
async def test_boot_check_failure_is_silent_and_never_crashes_startup(
    monkeypatch, tmp_path
):
    from doxa.app import DoxaApp
    from doxa import notify as notify_mod
    from tests.fakes import FakeEngine

    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))

    def boom(*a, **k):
        raise OSError("offline")

    monkeypatch.setattr(update_mod, "check_for_update", boom)
    calls: list[bool] = []
    monkeypatch.setattr(
        notify_mod, "notify_update_available", lambda focus: calls.append(focus)
    )
    engine = FakeEngine([])
    app = DoxaApp(
        cwd=str(tmp_path), engine_factory=lambda: engine,
        new_session_factory=lambda: engine,
    )
    async with app.run_test() as pilot:
        await pilot.pause(0.3)
        assert app.active_pane is not None  # boot completed normally
    assert calls == []

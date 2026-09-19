# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.worktrees -- worktree-per-session (#3).

Real git repos throughout (tmp repos, real `git worktree` calls): this
feature IS git behavior, mocking it would test nothing. Each test gets its
own DOXA_HOME (worktrees_root() lives under it) via the autouse fixture
below, mirroring test_doctor.py's isolation discipline.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from doxa import config as config_mod
from doxa import peers as peers_mod
from doxa import worktrees as worktrees_mod


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "doxa-home"))
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "runtime"))
    config_mod.invalidate()
    yield
    config_mod.invalidate()


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


def _worktree_list(repo):
    return subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        capture_output=True, text=True, check=True,
    ).stdout


def _branches(repo):
    return subprocess.run(
        ["git", "-C", str(repo), "branch", "--list"],
        capture_output=True, text=True, check=True,
    ).stdout


# -- create --------------------------------------------------------------


def test_create_makes_a_worktree_on_its_own_branch(tmp_path):
    repo = _repo(tmp_path)
    path = worktrees_mod.create(str(repo), "abcdef1234567890")
    assert path is not None
    assert path.endswith("repo-abcdef12")
    assert path.startswith(str(worktrees_mod.worktrees_root()))
    listing = _worktree_list(repo)
    assert "branch refs/heads/doxa/abcdef12" in listing
    branch = subprocess.run(
        ["git", "-C", path, "branch", "--show-current"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert branch == "doxa/abcdef12"


def test_create_forks_from_the_branch_currently_checked_out(tmp_path):
    """A worktree started from a feature branch forks FROM that branch,
    not from whatever the repo's default happens to be."""
    repo = _repo(tmp_path)
    subprocess.run(["git", "-C", str(repo), "checkout", "-qb", "feature"], check=True)
    (repo / "g.txt").write_text("two", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "two"], check=True)
    path = worktrees_mod.create(str(repo), "11112222")
    assert path is not None
    assert (path and Path(path, "g.txt").exists())


def test_create_is_idempotent_reuse(tmp_path):
    """A second call for the SAME session id lands on the SAME worktree
    instead of erroring or trying to make a duplicate."""
    repo = _repo(tmp_path)
    first = worktrees_mod.create(str(repo), "sess0001")
    second = worktrees_mod.create(str(repo), "sess0001")
    assert first == second
    listing = _worktree_list(repo)
    assert listing.count("branch refs/heads/doxa/sess0001") == 1


def test_create_returns_none_outside_a_repo(tmp_path):
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    assert worktrees_mod.create(str(plain), "abcd1234") is None


def test_create_returns_none_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("DOXA_WORKTREE", "0")
    repo = _repo(tmp_path)
    assert worktrees_mod.create(str(repo), "abcd1234") is None
    # And current behavior is unchanged: no worktrees dir was even touched.
    assert not worktrees_mod.worktrees_root().exists()


def test_create_default_is_on(tmp_path):
    """An untouched DOXA_WORKTREE -- the user's stated default. No env,
    no config row written: create() still makes a worktree."""
    repo = _repo(tmp_path)
    assert worktrees_mod.create(str(repo), "abcd1234") is not None


def test_enabled_reaches_the_settings_row(monkeypatch):
    """worktree_per_session's real reader, same bool_on discipline as
    clock_show (doxa.clock._bool): "" reads as ON, only an explicit off
    (env or the config row) turns it off, and emptying the row returns to
    the default."""
    monkeypatch.delenv("DOXA_WORKTREE", raising=False)
    assert worktrees_mod.enabled() is True
    monkeypatch.setenv("DOXA_WORKTREE", "0")
    assert worktrees_mod.enabled() is False
    monkeypatch.delenv("DOXA_WORKTREE")
    config_mod.save({"worktree_per_session": "0"})
    assert worktrees_mod.enabled() is False
    config_mod.save({"worktree_per_session": ""})
    assert worktrees_mod.enabled() is True


# -- finalize: clean / dirty ----------------------------------------------


def test_finalize_removes_a_clean_worktree_with_no_trace(tmp_path):
    repo = _repo(tmp_path)
    path = worktrees_mod.create(str(repo), "cleanid1")
    assert path is not None
    note = worktrees_mod.finalize(path)
    assert note is None
    assert not Path(path).exists()
    assert "doxa/cleanid1" not in _branches(repo)
    assert "doxa/cleanid1" not in _worktree_list(repo)


def test_finalize_keeps_a_dirty_worktree(tmp_path):
    repo = _repo(tmp_path)
    path = worktrees_mod.create(str(repo), "dirtyid1")
    assert path is not None
    (Path(path) / "f.txt").write_text("edited", encoding="utf-8")
    note = worktrees_mod.finalize(path)
    assert note == "kept doxa/dirtyid1 — merge when ready"
    assert Path(path).exists()
    assert "doxa/dirtyid1" in _branches(repo)


def test_finalize_keeps_a_worktree_with_unmerged_commits(tmp_path):
    """Clean working tree, but the branch carries a real commit its base
    does not -- never auto-merged, never silently discarded."""
    repo = _repo(tmp_path)
    path = worktrees_mod.create(str(repo), "commitid")
    assert path is not None
    (Path(path) / "new.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "-C", path, "add", "-A"], check=True)
    subprocess.run(["git", "-C", path, "commit", "-qm", "work"], check=True)
    note = worktrees_mod.finalize(path)
    assert note == "kept doxa/commitid — merge when ready"
    assert Path(path).exists()


def test_finalize_rule_text_matches_finalize_behavior(tmp_path):
    """``FINALIZE_RULE`` is the single string doxa.engine's
    ``[SESSION WORKTREE]`` prompt block quotes for its removal warning
    (see test_engine.py's own worktree-notice tests) -- so it is the
    ONLY place that sentence is allowed to be written down. This test is
    the coupling: it exercises :func:`finalize` under all three outcomes
    the sentence claims and checks the outcome against the sentence's own
    wording, so a change to finalize's clean/ahead logic that is not
    matched by an edit to FINALIZE_RULE fails HERE, in the same module as
    the logic that changed -- not silently in a prompt nobody reread."""
    repo = _repo(tmp_path)

    # clean + zero commits ahead -> removed with no trace.
    clean_path = worktrees_mod.create(str(repo), "ruleclea")
    assert clean_path is not None
    assert worktrees_mod.finalize(clean_path) is None
    assert "removed with no trace" in worktrees_mod.FINALIZE_RULE
    assert "clean" in worktrees_mod.FINALIZE_RULE
    assert "zero commits ahead" in worktrees_mod.FINALIZE_RULE

    # dirty (uncommitted change) -> kept.
    dirty_path = worktrees_mod.create(str(repo), "ruledirt")
    assert dirty_path is not None
    (Path(dirty_path) / "f.txt").write_text("edited", encoding="utf-8")
    assert worktrees_mod.finalize(dirty_path) is not None
    assert "uncommitted change" in worktrees_mod.FINALIZE_RULE
    assert "kept for manual merge" in worktrees_mod.FINALIZE_RULE

    # clean, but committed-and-unmerged -> kept.
    ahead_path = worktrees_mod.create(str(repo), "ruleahea")
    assert ahead_path is not None
    (Path(ahead_path) / "new.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "-C", ahead_path, "add", "-A"], check=True)
    subprocess.run(["git", "-C", ahead_path, "commit", "-qm", "work"], check=True)
    assert worktrees_mod.finalize(ahead_path) is not None
    assert "committed-but-unmerged work" in worktrees_mod.FINALIZE_RULE


def test_finalize_is_a_noop_for_a_non_doxa_worktree(tmp_path):
    """No sidecar metadata (the setting was off, or this is just some
    other directory) -- finalize leaves it completely alone."""
    plain = tmp_path / "elsewhere"
    plain.mkdir()
    assert worktrees_mod.finalize(str(plain)) is None
    assert plain.exists()


def test_finalize_drops_metadata_for_an_already_gone_directory(tmp_path):
    repo = _repo(tmp_path)
    path = worktrees_mod.create(str(repo), "goneid01")
    assert path is not None
    # Simulate an out-of-band removal (not through finalize/git worktree
    # remove): the directory is just gone.
    shutil.rmtree(path)
    assert worktrees_mod.finalize(path) is None
    assert worktrees_mod.read_meta(path) is None  # sidecar cleaned up too


# -- scope-key grouping across worktrees -----------------------------------


def test_main_repo_root_of_matches_from_inside_a_linked_worktree(tmp_path):
    """The bug this feature would otherwise ship with: git rev-parse
    --show-toplevel from inside a linked worktree names the WORKTREE, not
    the main repo -- peers.repo_root_of would diverge per worktree.
    main_repo_root_of must agree from both places."""
    repo = _repo(tmp_path)
    from_main = peers_mod.main_repo_root_of(str(repo))
    path = worktrees_mod.create(str(repo), "scopeid1")
    assert path is not None
    from_worktree = peers_mod.main_repo_root_of(path)
    assert from_main == from_worktree == str(repo)
    # And the OLD function really does diverge -- proving the fix matters.
    assert peers_mod.repo_root_of(path) != peers_mod.repo_root_of(str(repo))


@pytest.mark.asyncio
async def test_peerhost_scope_key_matches_across_worktrees_of_one_repo(tmp_path):
    """Two sessions of the SAME repo -- one in the main checkout, one in a
    worktree-per-session worktree -- must be peers: same scope_key, so
    /peers and the spawn-or-attach reuse path (doxa.cli) find each other
    instead of reading as two separate projects."""
    repo = _repo(tmp_path)
    path = worktrees_mod.create(str(repo), "peerid01")
    assert path is not None

    main_host = peers_mod.PeerHost(session_id="main-1", cwd=str(repo), title="main")
    wt_host = peers_mod.PeerHost(session_id="wt-1", cwd=path, title="worktree")
    await main_host.start()
    try:
        await wt_host.start()
        try:
            assert main_host.scope_key == wt_host.scope_key == str(repo)
            assert [p.session_id for p in main_host.list_peers()] == ["wt-1"]
            assert [p.session_id for p in wt_host.list_peers()] == ["main-1"]
        finally:
            await wt_host.stop()
    finally:
        await main_host.stop()


# -- orphans (doctor's read-only survey) -----------------------------------


def test_list_orphans_finds_a_worktree_with_no_live_session(tmp_path):
    repo = _repo(tmp_path)
    path = worktrees_mod.create(str(repo), "orphanid")
    assert path is not None
    orphans = worktrees_mod.list_orphans()
    assert [o["path"] for o in orphans] == [path]
    assert orphans[0]["branch"] == "doxa/orphanid"


@pytest.mark.asyncio
async def test_list_orphans_excludes_a_live_session(tmp_path):
    repo = _repo(tmp_path)
    session_id = "livesession"
    path = worktrees_mod.create(str(repo), session_id)
    assert path is not None
    host = peers_mod.PeerHost(session_id=session_id, cwd=path, title="t")
    await host.start()
    try:
        assert worktrees_mod.list_orphans() == []
    finally:
        await host.stop()
    # Once the session is gone, it's an orphan again.
    assert [o["path"] for o in worktrees_mod.list_orphans()] == [path]


def test_list_orphans_empty_when_nothing_created():
    assert worktrees_mod.list_orphans() == []


# -- item S: branch switch --------------------------------------------------
#
# Real git throughout, same discipline as the rest of this file.


def test_create_forks_from_an_explicit_base_branch(tmp_path):
    """`doxa new --branch <name>`, item S #1: the worktree forks from the
    NAMED branch, not whatever cwd has checked out."""
    repo = _repo(tmp_path)
    subprocess.run(["git", "-C", str(repo), "checkout", "-qb", "alt"], check=True)
    (repo / "alt.txt").write_text("alt", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "alt"], check=True)
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "trunk"], check=True)
    path = worktrees_mod.create(str(repo), "brsess01", base_branch="alt")
    assert path is not None
    assert (Path(path) / "alt.txt").exists()  # forked from alt, not trunk
    meta = worktrees_mod.read_meta(path)
    assert meta is not None and meta.get("base_ref") == "alt"


def test_create_returns_none_for_an_invalid_base_branch(tmp_path):
    repo = _repo(tmp_path)
    assert worktrees_mod.create(str(repo), "brsess02", base_branch="nope") is None
    assert "doxa/brsess02" not in _worktree_list(repo)  # no partial worktree left


def test_resolve_ref_finds_a_local_branch(tmp_path):
    repo = _repo(tmp_path)
    subprocess.run(["git", "-C", str(repo), "branch", "feature"], check=True)
    assert worktrees_mod.resolve_ref(str(repo), "feature") == "feature"


def test_resolve_ref_is_none_for_nonsense(tmp_path):
    repo = _repo(tmp_path)
    assert worktrees_mod.resolve_ref(str(repo), "does-not-exist") is None


def test_resolve_ref_prefers_the_local_branch_over_its_remote_tracking_ref(tmp_path):
    """`origin/foo` names a remote-tracking ref; if a LOCAL `foo` already
    exists, that is the local-semantics answer -- `git worktree add`/
    `git rebase` want a branch to return to, not a detached point.
    `git clone` already leaves exactly this shape: a local `trunk`
    tracking `origin/trunk`."""
    upstream = _repo(tmp_path, name="upstream")
    repo = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(upstream), str(repo)], check=True)
    assert worktrees_mod.resolve_ref(str(repo), "origin/trunk") == "trunk"


def test_resolve_ref_falls_back_to_the_remote_tracking_ref_with_no_local_branch(tmp_path):
    upstream = _repo(tmp_path, name="upstream")
    repo = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(upstream), str(repo)], check=True)
    # No local `trunk` branch was ever created in the clone (bare clone
    # checks out `trunk` as HEAD but that IS the local branch already --
    # simulate the no-local case with a second remote branch instead).
    subprocess.run(
        ["git", "-C", str(upstream), "branch", "only-remote"], check=True,
    )
    subprocess.run(["git", "-C", str(repo), "fetch", "-q", "origin"], check=True)
    assert (
        worktrees_mod.resolve_ref(str(repo), "origin/only-remote")
        == "origin/only-remote"
    )


def test_list_local_branches(tmp_path):
    repo = _repo(tmp_path)
    subprocess.run(["git", "-C", str(repo), "branch", "feature"], check=True)
    subprocess.run(["git", "-C", str(repo), "branch", "develop"], check=True)
    assert worktrees_mod.list_local_branches(str(repo)) == [
        "develop", "feature", "trunk",
    ]


def test_branch_status_marks_the_recorded_base_inside_a_worktree(tmp_path):
    repo = _repo(tmp_path)
    subprocess.run(["git", "-C", str(repo), "branch", "feature"], check=True)
    path = worktrees_mod.create(str(repo), "statusid")
    assert path is not None
    status = worktrees_mod.branch_status(path)
    assert status["base"] == "trunk"
    assert set(status["branches"]) >= {"trunk", "feature"}


def test_branch_status_outside_a_worktree_marks_the_checked_out_branch(tmp_path):
    repo = _repo(tmp_path)
    status = worktrees_mod.branch_status(str(repo))
    assert status["base"] == "trunk"
    assert status["checked_out"] == "trunk"


def test_switch_base_rebases_a_clean_zero_ahead_worktree(tmp_path):
    """The free path (item S #2): clean, zero commits ahead of the CURRENT
    base -- the switch is a fast-forward, and the sidecar's base_ref
    follows it."""
    repo = _repo(tmp_path)
    subprocess.run(["git", "-C", str(repo), "checkout", "-qb", "develop"], check=True)
    (repo / "dev.txt").write_text("dev", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "dev"], check=True)
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "trunk"], check=True)
    path = worktrees_mod.create(str(repo), "switchid")
    assert path is not None
    result = worktrees_mod.switch_base(path, "develop")
    assert result == {
        "ok": True, "base": "develop",
        "message": "doxa/switchid now based on develop",
    }
    assert (Path(path) / "dev.txt").exists()  # rebase actually landed
    meta = worktrees_mod.read_meta(path)
    assert meta is not None and meta.get("base_ref") == "develop"


def test_switch_base_refuses_a_dirty_worktree(tmp_path):
    repo = _repo(tmp_path)
    subprocess.run(["git", "-C", str(repo), "branch", "develop"], check=True)
    path = worktrees_mod.create(str(repo), "dirtysw1")
    assert path is not None
    (Path(path) / "f.txt").write_text("wip", encoding="utf-8")
    result = worktrees_mod.switch_base(path, "develop")
    assert result["ok"] is False
    assert result["base"] is None
    assert "uncommitted changes" in result["message"]
    assert "merge when ready" in result["message"]
    # And the base never moved: refused, not silently carried.
    meta = worktrees_mod.read_meta(path)
    assert meta is not None and meta.get("base_ref") == "trunk"


def test_switch_base_refuses_a_worktree_ahead_of_its_base(tmp_path):
    repo = _repo(tmp_path)
    subprocess.run(["git", "-C", str(repo), "branch", "develop"], check=True)
    path = worktrees_mod.create(str(repo), "aheadsw1")
    assert path is not None
    (Path(path) / "new.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "-C", path, "add", "-A"], check=True)
    subprocess.run(["git", "-C", path, "commit", "-qm", "work"], check=True)
    result = worktrees_mod.switch_base(path, "develop")
    assert result["ok"] is False
    assert result["base"] is None
    assert "ahead of trunk" in result["message"]
    assert "merge when ready" in result["message"]


def test_switch_base_refuses_an_invalid_ref(tmp_path):
    repo = _repo(tmp_path)
    path = worktrees_mod.create(str(repo), "badrefsw")
    assert path is not None
    result = worktrees_mod.switch_base(path, "no-such-branch")
    assert result["ok"] is False
    assert "no such branch" in result["message"]


def test_switch_base_refuses_outside_a_worktree_session(tmp_path):
    """No sidecar metadata -- the checked-out branch is the user's ACTUAL
    real checkout, and this command must never move it silently."""
    repo = _repo(tmp_path)
    subprocess.run(["git", "-C", str(repo), "branch", "develop"], check=True)
    result = worktrees_mod.switch_base(str(repo), "develop")
    assert result["ok"] is False
    assert "worktree_per_session is off" in result["message"]
    # And nothing moved: still on trunk.
    branch = subprocess.run(
        ["git", "-C", str(repo), "branch", "--show-current"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert branch == "trunk"


# -- the session's own branch is never a base ----------------------------
#
# Regression, v0.33.0. `/branch` listed every local branch, which inside a
# worktree-per-session session includes the session's OWN `doxa/<id>`.
# Picking it "succeeded" ("doxa/x now based on doxa/x") and wrote a sidecar
# whose base_ref equals its branch -- and `rev-list branch..HEAD` is
# structurally zero there, so finalize's "clean and zero ahead" test read
# real unmerged commits as nothing to keep and `git branch -D`'d them.


def test_branch_status_never_offers_the_sessions_own_branch(tmp_path):
    """The picker's rows come from here, and a session's own throwaway
    branch is IDENTITY, not a base anyone can fork from."""
    repo = _repo(tmp_path)
    subprocess.run(["git", "-C", str(repo), "branch", "feature"], check=True)
    path = worktrees_mod.create(str(repo), "ownlist1")
    assert path is not None
    own = worktrees_mod.read_meta(path)["branch"]
    status = worktrees_mod.branch_status(path)
    assert own not in status["branches"]
    # and the real bases are all still there
    assert set(status["branches"]) == {"trunk", "feature"}


def test_branch_status_still_lists_the_checked_out_branch_with_no_sidecar(tmp_path):
    """The filter keys off the SIDECAR's branch, so a plain checkout (no
    worktree-per-session) keeps listing the branch it is on -- which is
    also the one it marks as the base."""
    repo = _repo(tmp_path)
    status = worktrees_mod.branch_status(str(repo))
    assert "trunk" in status["branches"]
    assert status["base"] == "trunk"


def test_switch_base_refuses_the_sessions_own_branch(tmp_path):
    repo = _repo(tmp_path)
    path = worktrees_mod.create(str(repo), "ownsw001")
    assert path is not None
    own = worktrees_mod.read_meta(path)["branch"]
    result = worktrees_mod.switch_base(path, own)
    assert result["ok"] is False
    assert "own branch" in result["message"]
    # names the base it is keeping, so the refusal is actionable
    assert "trunk" in result["message"]
    # and the sidecar is untouched: the base is still the real one
    assert worktrees_mod.read_meta(path)["base_ref"] == "trunk"


def test_unmerged_work_survives_a_switch_attempt_onto_the_own_branch(tmp_path):
    """The whole defect, at the outcome that matters: after the refused
    switch, a real commit is still KEPT at session end rather than
    force-deleted."""
    repo = _repo(tmp_path)
    path = worktrees_mod.create(str(repo), "ownloss1")
    assert path is not None
    own = worktrees_mod.read_meta(path)["branch"]
    worktrees_mod.switch_base(path, own)  # refused
    (Path(path) / "work.txt").write_text("unmerged", encoding="utf-8")
    subprocess.run(["git", "-C", path, "add", "-A"], check=True)
    subprocess.run(["git", "-C", path, "commit", "-qm", "real work"], check=True)
    sha = subprocess.run(
        ["git", "-C", path, "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert worktrees_mod.finalize(path) == f"kept {own} — merge when ready"
    assert own in _branches(repo)
    contains = subprocess.run(
        ["git", "-C", str(repo), "branch", "--contains", sha],
        capture_output=True, text=True, check=True,
    ).stdout
    assert own in contains


def test_finalize_keeps_a_sidecar_that_records_its_own_branch_as_base(tmp_path):
    """The safety net for sidecars the SHIPPED bug already wrote: a
    base_ref equal to the branch cannot measure 'ahead', and unmeasurable
    has always meant keep, never delete."""
    repo = _repo(tmp_path)
    path = worktrees_mod.create(str(repo), "ownold01")
    assert path is not None
    own = worktrees_mod.read_meta(path)["branch"]
    worktrees_mod.update_base(path, own)  # what the old build left on disk
    (Path(path) / "work.txt").write_text("unmerged", encoding="utf-8")
    subprocess.run(["git", "-C", path, "add", "-A"], check=True)
    subprocess.run(["git", "-C", path, "commit", "-qm", "real work"], check=True)
    assert worktrees_mod.finalize(path) == f"kept {own} — merge when ready"
    assert own in _branches(repo)


# -- machine identity (LORE/docs/plans/sync.md, "## DOXA" item 2) ----------
#
# Real git and real sidecars throughout, same discipline as the rest of
# this file: what lands on disk IS the feature. :mod:`doxa.lore_sync` is
# monkeypatched rather than a real op log stood up -- DOXA does not own the
# op log, and a test that needed one would pass or fail on which lore_core
# doxa._lore_bootstrap happened to resolve.
#
# The first test is the regression bar the whole feature is measured
# against, and it is deliberately first: with sync off, a sidecar is the
# same four fields it has been since v0.17.


def _sync_off_sidecar_fields():
    return {"main_root", "branch", "base_ref", "session_id"}


@pytest.fixture
def _sync_on(monkeypatch):
    """The worktrees class opted in, with a fixed machine id -- the two
    things doxa.worktrees asks before it stamps a sidecar."""
    from doxa import lore_sync as lore_sync_mod

    monkeypatch.setattr(lore_sync_mod, "worktrees_enabled", lambda: True)
    monkeypatch.setattr(lore_sync_mod, "machine_id", lambda **_kw: "machine-here")
    return "machine-here"


def _claim_machine(monkeypatch, value):
    """Make this machine's identity read as ``value`` (None = unreadable)."""
    from doxa import lore_sync as lore_sync_mod

    monkeypatch.setattr(lore_sync_mod, "machine_id", lambda **_kw: value)


def test_sidecar_carries_no_machine_id_with_sync_off(tmp_path):
    """THE regression bar for item 2: sync off writes exactly what 1.9.2
    wrote, no extra key, so nothing downstream can start behaving
    differently on a machine that never opted in."""
    repo = _repo(tmp_path)
    path = worktrees_mod.create(str(repo), "syncoff1")
    assert path is not None
    meta = worktrees_mod.read_meta(path)
    assert set(meta) == _sync_off_sidecar_fields()
    assert worktrees_mod.is_own_record(meta) is True


def test_sidecar_carries_this_machine_with_sync_on(tmp_path, _sync_on):
    repo = _repo(tmp_path)
    path = worktrees_mod.create(str(repo), "syncon01")
    assert path is not None
    meta = worktrees_mod.read_meta(path)
    assert meta["machine_id"] == _sync_on
    # The four original fields are untouched beside it -- the key is added,
    # never a rewrite of the record.
    assert _sync_off_sidecar_fields() <= set(meta)
    assert worktrees_mod.is_own_record(meta) is True


def test_finalize_leaves_another_machines_worktree_alone(tmp_path, monkeypatch, _sync_on):
    """sync.md item 2: "finalize ignores records that are not its own."

    The directory a foreign sidecar names is on the OTHER machine's disk,
    so acting on it here means running `git branch -D` against whatever
    local directory happens to share the name. Clean and zero-ahead --
    i.e. the one state finalize would otherwise remove without a trace --
    so a regression here deletes the branch rather than merely reporting
    oddly."""
    repo = _repo(tmp_path)
    path = worktrees_mod.create(str(repo), "elsewhr1")
    assert path is not None
    _claim_machine(monkeypatch, "some-other-machine")
    assert worktrees_mod.finalize(path) is None
    assert Path(path).exists()
    assert "doxa/elsewhr1" in _branches(repo)
    # And the sidecar is still there: finalize did not even clean up after
    # a record it does not own.
    assert worktrees_mod.read_meta(path) is not None


def test_finalize_still_removes_this_machines_clean_worktree_with_sync_on(
    tmp_path, _sync_on
):
    """The refusal above must not be vacuous: the SAME code path with a
    matching machine id still removes a clean, zero-ahead worktree."""
    repo = _repo(tmp_path)
    path = worktrees_mod.create(str(repo), "ownclean")
    assert path is not None
    assert worktrees_mod.finalize(path) is None
    assert not Path(path).exists()
    assert "doxa/ownclean" not in _branches(repo)


def test_list_orphans_never_offers_another_machines_worktree_as_a_path(
    tmp_path, monkeypatch, _sync_on
):
    """A foreign record is not an orphan HERE, and -- the half that
    matters -- it is never handed out as a path to open."""
    repo = _repo(tmp_path)
    path = worktrees_mod.create(str(repo), "farawy01")
    assert path is not None
    _claim_machine(monkeypatch, "some-other-machine")
    assert worktrees_mod.list_orphans() == []


def test_list_remote_names_another_machines_worktree_and_carries_no_path(
    tmp_path, monkeypatch, _sync_on
):
    """sync.md item 2: listed as REMOTE, never as a path to open. The
    guarantee is structural -- there is no path key to offer -- so this
    asserts the absence, not just that nobody currently reads it."""
    repo = _repo(tmp_path)
    path = worktrees_mod.create(str(repo), "farawy02")
    assert path is not None
    _claim_machine(monkeypatch, "workstation-id")
    remote = worktrees_mod.list_remote()
    assert len(remote) == 1
    assert remote[0]["branch"] == "doxa/farawy02"
    assert remote[0]["machine"] == "machine-here"
    assert "path" not in remote[0]


def test_list_remote_is_empty_for_this_machines_own_worktrees(tmp_path, _sync_on):
    """Not vacuous in the other direction either: our own records never
    show up as remote."""
    repo = _repo(tmp_path)
    assert worktrees_mod.create(str(repo), "mineonly") is not None
    assert worktrees_mod.list_remote() == []
    assert len(worktrees_mod.list_orphans()) == 1


def test_a_sidecar_is_kept_when_this_machines_identity_cannot_be_read(
    tmp_path, monkeypatch, _sync_on
):
    """Unprovable has always meant KEEP here (finalize's meta-is-None case
    is the same judgement). A lore_core that failed to import must not
    make DOXA disown -- and then delete -- the user's worktrees."""
    repo = _repo(tmp_path)
    path = worktrees_mod.create(str(repo), "noident1")
    assert path is not None
    _claim_machine(monkeypatch, None)
    meta = worktrees_mod.read_meta(path)
    assert worktrees_mod.is_own_record(meta) is True
    assert worktrees_mod.list_remote() == []


def test_update_base_preserves_the_machine_id(tmp_path, _sync_on):
    """/branch rewrites the sidecar through a whole-dict round trip; the
    machine id has to survive it, or one branch switch silently disowns
    the worktree."""
    repo = _repo(tmp_path)
    path = worktrees_mod.create(str(repo), "switch01")
    assert path is not None
    subprocess.run(["git", "-C", str(repo), "branch", "other"], check=True)
    assert worktrees_mod.update_base(path, "other") is True
    meta = worktrees_mod.read_meta(path)
    assert meta["base_ref"] == "other"
    assert meta["machine_id"] == _sync_on


# =======================================================================
# Unmerged is a property of the BRANCH, not of HEAD (panel finding 10)
# =======================================================================


def test_finalize_keeps_work_when_head_moved_off_the_recorded_branch(tmp_path):
    """Mirrors probe_worktree_finalize.py.

    ``commits_ahead`` counted ``base_ref..HEAD``. A ``git checkout`` inside
    the worktree -- detaching onto the base to read something, switching
    to another branch -- moves HEAD off ``doxa/<short>``, and that count
    then reads 0 while the session's commits sit on a branch nobody asked
    about. finalize read the 0 as "nothing unmerged", removed the worktree
    and DELETED the branch: the commit was gone."""
    repo = _repo(tmp_path)
    path = worktrees_mod.create(str(repo), "movedhead")
    assert path is not None
    branch = worktrees_mod.read_meta(path)["branch"]
    (Path(path) / "new.txt").write_text("important", encoding="utf-8")
    subprocess.run(["git", "-C", path, "add", "-A"], check=True)
    subprocess.run(["git", "-C", path, "commit", "-qm", "real work"], check=True)
    kept_sha = subprocess.run(
        ["git", "-C", path, "rev-parse", branch],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    # Somebody looks at the base inside the worktree and leaves it there.
    subprocess.run(["git", "-C", path, "checkout", "-q", "--detach", "trunk"],
                   check=True)

    note = worktrees_mod.finalize(path)

    assert note == f"kept {branch} — merge when ready"
    assert Path(path).exists()
    assert branch in _branches(repo)
    still = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", branch],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert still == kept_sha


def test_commits_ahead_counts_the_branch_it_is_given(tmp_path):
    """The unit underneath it, both ways round: the recorded branch is
    ahead, the detached HEAD beside it is not."""
    repo = _repo(tmp_path)
    path = worktrees_mod.create(str(repo), "countid1")
    assert path is not None
    branch = worktrees_mod.read_meta(path)["branch"]
    (Path(path) / "new.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "-C", path, "add", "-A"], check=True)
    subprocess.run(["git", "-C", path, "commit", "-qm", "work"], check=True)
    subprocess.run(["git", "-C", path, "checkout", "-q", "--detach", "trunk"],
                   check=True)

    assert worktrees_mod.commits_ahead(path, "trunk", branch) == 1
    assert worktrees_mod.commits_ahead(path, "trunk") == 0  # HEAD, as before


def test_finalize_still_removes_a_worktree_that_really_has_nothing(tmp_path):
    """The check must not turn every worktree into a kept one."""
    repo = _repo(tmp_path)
    path = worktrees_mod.create(str(repo), "emptyid1")
    assert path is not None
    branch = worktrees_mod.read_meta(path)["branch"]
    subprocess.run(["git", "-C", path, "checkout", "-q", "--detach", "trunk"],
                   check=True)
    assert worktrees_mod.finalize(path) is None
    assert not Path(path).exists()
    assert branch not in _branches(repo)

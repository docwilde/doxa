"""The curated-memory fill chip (v0.44.0).

The caps are the one LORE number that fails a WRITE when exceeded rather
than degrading quietly: past the cap an add is refused and the entries are
listed so they get consolidated. A user sitting at 88% wants to know that
before the refusal, not after -- which is the whole reason this sits in the
status bar next to the belief count rather than behind a command.

Two percentages rather than one merged figure, because the caps are
separate and fill at different rates: user memory holds facts that never
stop being true and creeps up forever, project memory rotates with the
repo. Merging them would hide whichever one is about to start refusing.
"""

from __future__ import annotations

import pytest

from doxa.ui.labels import memory_fill, memory_fill_chip


def test_chip_shows_both_scopes_as_percentages():
    text, hint = memory_fill_chip((2824, 4500), (3471, 8800))
    assert text == "mem u63% p39%"
    # The hint carries the raw numbers the chip cannot spend width on.
    assert "2824/4500" in hint and "3471/8800" in hint


def test_one_scope_alone_still_renders():
    """A session outside any project has no MEMORY.md; the user half must
    still show rather than the whole chip vanishing."""
    text, _ = memory_fill_chip((2824, 4500), None)
    assert text == "mem u63%"


def test_no_scopes_omits_the_chip_entirely():
    assert memory_fill_chip(None, None) is None


def test_percentages_are_rounded_not_truncated():
    # 50.6% -> 51, not 50: a cap creeping up should read high, not low.
    text, _ = memory_fill_chip((2277, 4500), None)
    assert text == "mem u51%"


def test_a_full_scope_reads_100_not_a_crash():
    text, _ = memory_fill_chip((4500, 4500), None)
    assert text == "mem u100%"


def test_unreadable_store_yields_none_rather_than_raising(monkeypatch):
    """A missing store, an older lore_core, a permissions problem: the chip
    disappears and the status bar keeps working. Memory fill is a
    convenience and must never be a reason to degrade the bar."""
    import lore_core.memory as lore_memory

    def boom(*a, **k):
        raise OSError("no store here")

    monkeypatch.setattr(lore_memory, "memory_path", boom)
    assert memory_fill("user") is None


def test_fill_matches_what_lore_itself_reports(tmp_path, monkeypatch):
    """The number must equal lore_core's own char count, not an st_size
    approximation -- those diverge on any multi-byte character, and a chip
    that disagrees with the cap the write path enforces is worse than no
    chip. The body below is deliberately non-ASCII."""
    import lore_core.memory as lore_memory

    store = tmp_path / "USER.md"
    body = "- über café naïve — measured 40/40\n"
    store.write_text(body, encoding="utf-8")
    assert len(body.encode("utf-8")) != len(body), "test body must be multi-byte"

    monkeypatch.setattr(lore_memory, "memory_path", lambda scope, slug: store)
    monkeypatch.setattr(lore_memory, "memory_cap", lambda scope: 4500)

    used, cap = memory_fill("user")
    assert used == len(body)  # chars, not bytes
    assert cap == 4500


def test_repeat_reads_are_cached_until_the_file_changes(tmp_path, monkeypatch):
    """_refresh_status already pays for a belief COUNT(*) on every
    event-driven refresh; this must not add a file read on top of each one.
    An unchanged file costs a stat, not a read."""
    import lore_core.memory as lore_memory

    store = tmp_path / "USER.md"
    store.write_text("- one\n", encoding="utf-8")
    monkeypatch.setattr(lore_memory, "memory_path", lambda scope, slug: store)
    monkeypatch.setattr(lore_memory, "memory_cap", lambda scope: 4500)

    reads = {"n": 0}
    real_read = type(store).read_text

    def counting_read(self, *a, **k):
        reads["n"] += 1
        return real_read(self, *a, **k)

    monkeypatch.setattr(type(store), "read_text", counting_read)

    assert memory_fill("user")[0] == len("- one\n")
    first = reads["n"]
    for _ in range(5):
        memory_fill("user")
    assert reads["n"] == first, "cached scope was re-read on an unchanged file"

    # A real edit must invalidate: bump mtime explicitly, since a fast
    # test can write twice inside one filesystem timestamp tick.
    store.write_text("- one\n- two\n", encoding="utf-8")
    import os

    stamp = store.stat().st_mtime + 10
    os.utime(store, (stamp, stamp))
    assert memory_fill("user")[0] == len("- one\n- two\n")
    assert reads["n"] > first


# -- the worktree case, which is the NORMAL case (v0.48.0) -----------------


def test_a_worktree_resolves_to_its_main_repo_s_project_memory(tmp_path, monkeypatch):
    """The defect this file shipped with, reported by the user: the chip
    showed `mem u63%` and silently dropped the project half on every
    worktree session.

    Since v0.17.0 every repo session runs in a worktree, so resolving the
    LORE slug from the pane's raw cwd answers "which DIRECTORY" when the
    question is "which PROJECT" -- a worktree owns no MEMORY.md, the fill
    came back None, and the chip rendered half of itself with no
    indication anything was missing. The fix routes through
    `peers.main_repo_root_of`, which already existed for exactly this
    scope-key fracture.

    Asserted against real git repos, not a stub: the mapping is git's own
    (`rev-parse --git-common-dir`), and a test that mocks it would pass on
    the broken code.
    """
    import pathlib
    import subprocess

    from doxa import peers as peers_mod

    main = tmp_path / "repo"
    main.mkdir()
    run = lambda *a, **k: subprocess.run(*a, check=True, capture_output=True, **k)
    run(["git", "init", "-q"], cwd=main)
    run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
         "--allow-empty", "-m", "init"], cwd=main)
    wt = tmp_path / "wt"
    run(["git", "worktree", "add", "-q", "-b", "doxa/x", str(wt)], cwd=main)

    # The premise: a worktree is its own toplevel, which is why the naive
    # reading goes wrong. If this stops being true the test is moot.
    top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=wt,
                         capture_output=True, text=True).stdout.strip()
    assert pathlib.Path(top).resolve() == wt.resolve()

    assert pathlib.Path(peers_mod.main_repo_root_of(str(wt))).resolve() == main.resolve()

# SPDX-License-Identifier: AGPL-3.0-only
"""A Codex session must be able to commit inside its own worktree (#57).

Codex's ``workspace-write`` sandbox makes the process cwd writable and
nothing else. A linked git worktree keeps its index in the MAIN repository
under ``.git/worktrees/<name>/`` and its objects, refs and reflogs one
level above that, so a Codex worker created its file and then died at::

    fatal: Unable to create '<main>/.git/worktrees/<n>/index.lock':
    Read-only file system

Measured on 1.14.0, supervisor run ``20260919T160458-539e``: both Codex
workers, both failing the same way, both reporting it back over
``peer_send``. Under supervisor mode committing IS the worker's delivery
mechanism, so this is the main path for a Codex slot rather than a corner.

Two halves, and the second is the one that keeps this honest. The
arithmetic is exercised against REAL git repositories and real ``git
worktree`` calls, the way ``tests/test_worktrees.py`` already does --
this feature IS git's directory layout, and mocking it would test
nothing. The argv half asserts what DOXA composes for ``codex exec``,
including the three cases where it must compose exactly what it composed
before, and the boundary the widening must not cross: the common ``.git``
itself, and therefore ``hooks`` (scripts the user's own next git command
runs outside any sandbox) and ``config`` (``core.editor``, credential
helpers -- an execution channel of its own).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from doxa import config as config_mod
from doxa import worktrees as worktrees_mod
from doxa.codex import (
    GIT_WRITE_ENV,
    WRITABLE_ROOTS_KEY,
    CodexEngine,
)


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "doxa-home"))
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "runtime"))
    config_mod.invalidate()
    yield
    config_mod.invalidate()


def _repo(tmp_path, name="repo", branch="trunk") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", branch, str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    (repo / "f.txt").write_text("one", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "one"], check=True)
    return repo


def _session_worktree(tmp_path) -> "tuple[Path, str]":
    """A repo and the worktree DOXA itself would hand a session in it --
    ``worktrees.create``, not a hand-rolled ``git worktree add``, so the
    layout under test is the one a real session actually gets."""
    repo = _repo(tmp_path)
    path = worktrees_mod.create(str(repo), "abcdef1234567890")
    assert path is not None
    return repo, path


def _overrides(argv: "list[str]") -> "dict[str, str]":
    """The ``-c key=value`` pairs in an argv, as a dict (the same reading
    ``tests/test_engines.py`` takes of the same argv)."""
    out: "dict[str, str]" = {}
    for flag, pair in zip(argv, argv[1:]):
        if flag == "-c" and "=" in pair:
            key, _, value = pair.partition("=")
            out[key] = value
    return out


# -- the path arithmetic -------------------------------------------------


def test_only_a_linked_worktree_widens_anything(tmp_path):
    """An ordinary checkout keeps its index at ``.git/index``, so the only
    grant that would help it is ``.git`` WHOLE -- hooks, config and all,
    which is the grant this feature exists to avoid. A directory that is no
    repository has nothing to widen either. Both answer with the empty
    list, and the worktree beside them is what proves that list means "no
    narrow answer exists here" rather than "this function does nothing"."""
    repo, path = _session_worktree(tmp_path)
    plain = tmp_path / "plain"
    plain.mkdir()
    assert worktrees_mod.external_git_roots(str(repo)) == []
    assert worktrees_mod.external_git_roots(str(plain)) == []
    assert worktrees_mod.external_git_roots(path)


def test_a_session_worktree_names_its_index_and_the_object_store(tmp_path):
    """The four directories a commit made in a linked worktree writes to,
    and the reason each one is on the list: the per-worktree admin
    directory takes ``index.lock``, ``objects`` takes the new commit,
    ``refs`` takes the branch update, ``logs`` takes its reflog entry."""
    repo, path = _session_worktree(tmp_path)
    roots = worktrees_mod.external_git_roots(path)
    common = Path(os.path.realpath(repo)) / ".git"
    assert roots == [
        str(common / "worktrees" / Path(path).name),
        str(common / "objects"),
        str(common / "refs"),
        str(common / "logs"),
    ]


def test_the_common_git_directory_itself_is_never_widened(tmp_path):
    """The security boundary, asserted by name. ``.git`` itself would
    carry ``hooks`` -- which the user's own next ``git commit`` runs
    OUTSIDE any sandbox -- and ``config``, whose ``core.editor`` and
    credential-helper rows are an execution channel too."""
    repo, path = _session_worktree(tmp_path)
    roots = worktrees_mod.external_git_roots(path)
    common = os.path.realpath(repo / ".git")
    assert roots, "nothing was widened, so this proves nothing"
    assert common not in roots
    for forbidden in ("hooks", "config", "info", "modules"):
        assert os.path.join(common, forbidden) not in roots


def test_the_roots_really_are_where_git_puts_the_index(tmp_path):
    """The arithmetic is checked against git's OWN answer rather than
    against the string this module built: a rename upstream that moved
    the per-worktree admin directory would break the fix silently."""
    _, path = _session_worktree(tmp_path)
    admin = subprocess.run(
        ["git", "-C", path, "rev-parse", "--absolute-git-dir"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert os.path.realpath(admin) in worktrees_mod.external_git_roots(path)
    assert (Path(admin) / "index").exists()


def test_every_returned_root_exists_and_is_outside_the_worktree(tmp_path):
    """A root that is not on disk is a rule a sandbox may refuse, and a
    root already inside the cwd is one the sandbox granted anyway."""
    _, path = _session_worktree(tmp_path)
    workspace = os.path.realpath(path)
    roots = worktrees_mod.external_git_roots(path)
    assert roots, "nothing was widened, so this proves nothing"
    for root in roots:
        assert Path(root).is_dir()
        assert os.path.commonpath([workspace, root]) != workspace


# -- what DOXA composes for `codex exec` ---------------------------------


def test_a_codex_turn_in_a_worktree_can_reach_its_own_index(tmp_path):
    """The fix, at the level it can be tested without a live model: the
    writable roots are on the argv, as a TOML array of exactly the
    directories the arithmetic named."""
    _, path = _session_worktree(tmp_path)
    engine = CodexEngine(cwd=path)
    value = _overrides(engine._argv(True)).get(WRITABLE_ROOTS_KEY)
    assert value is not None
    assert json.loads(value) == worktrees_mod.external_git_roots(path)


def test_both_turns_carry_the_same_roots(tmp_path):
    """One argv shape for the first turn and every ``codex exec resume``
    after it -- a resume that forgot them would be a session that could
    commit once and never again. ``-c`` is why this is possible at all:
    ``--add-dir`` exists on ``codex exec`` and NOT on ``codex exec
    resume``, the same asymmetry that keeps ``-C`` and ``-s`` off the
    argv."""
    _, path = _session_worktree(tmp_path)
    engine = CodexEngine(cwd=path)
    engine.thread_id = "th-x"
    first, resume = engine._argv(True), engine._argv(False)
    assert "--add-dir" not in first and "--add-dir" not in resume
    assert (
        _overrides(first)[WRITABLE_ROOTS_KEY]
        == _overrides(resume)[WRITABLE_ROOTS_KEY]
    )


def test_a_session_outside_a_worktree_gets_the_argv_it_always_had(tmp_path):
    """The fallback. An ordinary checkout, a bare directory, a machine
    with no git: nothing is added, and the sandbox is the one every Codex
    session before this fix already ran under."""
    repo, path = _session_worktree(tmp_path)
    plain = tmp_path / "plain"
    plain.mkdir()
    for cwd in (str(repo), str(plain)):
        over = _overrides(CodexEngine(cwd=cwd)._argv(True))
        assert WRITABLE_ROOTS_KEY not in over
        assert over["sandbox_mode"] == '"workspace-write"'
    # The worktree beside them: the difference is the cwd and nothing else.
    assert WRITABLE_ROOTS_KEY in _overrides(CodexEngine(cwd=path)._argv(True))


def test_a_read_only_session_is_never_widened(tmp_path):
    """``read-only`` is a session that writes nothing. Widening the write
    set of a mode that has none would answer a question nobody asked --
    and ``sandbox_workspace_write`` is not even the table in force."""
    _, path = _session_worktree(tmp_path)
    over = _overrides(CodexEngine(cwd=path, sandbox="read-only")._argv(True))
    assert over["sandbox_mode"] == '"read-only"'
    assert WRITABLE_ROOTS_KEY not in over
    # The same cwd in the mode that writes: the difference is the mode.
    assert WRITABLE_ROOTS_KEY in _overrides(CodexEngine(cwd=path)._argv(True))


def test_the_widening_can_be_switched_off(tmp_path, monkeypatch):
    """An operator who would rather have the failure than the write. Off
    is exactly the pre-fix argv, which is what makes this a switch rather
    than a third behaviour."""
    _, path = _session_worktree(tmp_path)
    monkeypatch.setenv(GIT_WRITE_ENV, "0")
    config_mod.invalidate()
    assert WRITABLE_ROOTS_KEY not in _overrides(CodexEngine(cwd=path)._argv(True))
    monkeypatch.setenv(GIT_WRITE_ENV, "1")
    config_mod.invalidate()
    assert WRITABLE_ROOTS_KEY in _overrides(CodexEngine(cwd=path)._argv(True))


def test_the_roots_are_toml_quoted_never_interpolated(tmp_path):
    """The same guard ``SANDBOX_MODES`` is an allow-list for: this value
    lands in the config table that decides what the agent may WRITE, so a
    path carrying a quote must escape rather than open a second key."""
    repo = _repo(tmp_path, name='re"po')
    path = worktrees_mod.create(str(repo), "abcdef1234567890")
    assert path is not None
    value = _overrides(CodexEngine(cwd=path)._argv(True))[WRITABLE_ROOTS_KEY]
    assert json.loads(value) == worktrees_mod.external_git_roots(path)
    assert '\\"' in value


def test_the_roots_are_measured_once_per_session(tmp_path, monkeypatch):
    """``self.cwd`` never changes and ``_argv`` runs once per turn, so a
    ``git rev-parse`` per turn would be a subprocess paid forever for an
    answer that cannot move."""
    _, path = _session_worktree(tmp_path)
    engine = CodexEngine(cwd=path)
    calls: "list[str]" = []
    real = worktrees_mod.external_git_roots

    def counting(cwd: str) -> "list[str]":
        calls.append(cwd)
        return real(cwd)

    monkeypatch.setattr(worktrees_mod, "external_git_roots", counting)
    engine._argv(True)
    engine._argv(False)
    engine._argv(True)
    assert calls == [path]

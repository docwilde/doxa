# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.cli -- item S #1: `doxa new --branch <name>` / `--checkout`.

Real git throughout (house pattern). ``spawn_daemon``/``_run_attached`` are
monkeypatched to recording stand-ins -- this file is about the CLI's own
validation and plumbing, not about actually booting a daemon or a Textual
app, which doxa/test_daemon.py and the app-level suites already cover."""

from __future__ import annotations

import subprocess

import pytest

from doxa import cli as cli_mod
from doxa import config as config_mod


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "doxa-home"))
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "runtime"))
    config_mod.invalidate()
    yield
    config_mod.invalidate()


def _repo(tmp_path, branch="trunk"):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", branch, str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    (repo / "f.txt").write_text("one", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "one"], check=True)
    return repo


def _spies(monkeypatch):
    spawn_calls = []
    attach_calls = []

    def fake_spawn(cwd, model=None, linger_secs=120.0, base_branch=None):
        spawn_calls.append({"cwd": cwd, "base_branch": base_branch})
        return "fake-session-id", "/tmp/fake.sock"

    def fake_run_attached(socket_path, cwd, model, linger):
        attach_calls.append(socket_path)

    monkeypatch.setattr(cli_mod, "spawn_daemon", fake_spawn)
    monkeypatch.setattr(cli_mod, "_run_attached", fake_run_attached)
    return spawn_calls, attach_calls


def test_branch_spawns_off_an_alternate_branch(monkeypatch, tmp_path, capsys):
    repo = _repo(tmp_path)
    subprocess.run(["git", "-C", str(repo), "branch", "alt"], check=True)
    monkeypatch.chdir(repo)
    spawn_calls, attach_calls = _spies(monkeypatch)

    rc = cli_mod.main(["new", "--branch", "alt"])
    assert rc == 0
    assert spawn_calls == [{"cwd": str(repo), "base_branch": "alt"}]
    assert attach_calls  # still attached normally


def test_branch_invalid_ref_is_a_clear_actionable_message(monkeypatch, tmp_path, capsys):
    repo = _repo(tmp_path)
    monkeypatch.chdir(repo)
    spawn_calls, _attach_calls = _spies(monkeypatch)

    rc = cli_mod.main(["new", "--branch", "no-such-branch"])
    assert rc == 2
    assert spawn_calls == []
    err = capsys.readouterr().err
    assert "no such branch" in err
    assert "no-such-branch" in err


def test_branch_needs_a_git_repo(monkeypatch, tmp_path, capsys):
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    monkeypatch.chdir(plain)
    spawn_calls, _ = _spies(monkeypatch)

    rc = cli_mod.main(["new", "--branch", "anything"])
    assert rc == 2
    assert spawn_calls == []
    assert "needs a git repo" in capsys.readouterr().err


def test_branch_with_worktree_off_refuses_by_default(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("DOXA_WORKTREE", "0")
    repo = _repo(tmp_path)
    subprocess.run(["git", "-C", str(repo), "branch", "alt"], check=True)
    monkeypatch.chdir(repo)
    spawn_calls, _ = _spies(monkeypatch)

    rc = cli_mod.main(["new", "--branch", "alt"])
    assert rc == 2
    assert spawn_calls == []
    err = capsys.readouterr().err
    assert "worktree_per_session is off" in err
    assert "--checkout" in err
    # And the real checkout never moved.
    branch = subprocess.run(
        ["git", "-C", str(repo), "branch", "--show-current"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert branch == "trunk"


def test_checkout_flag_switches_the_real_checkout_on_a_clean_tree(
    monkeypatch, tmp_path, capsys,
):
    monkeypatch.setenv("DOXA_WORKTREE", "0")
    repo = _repo(tmp_path)
    subprocess.run(["git", "-C", str(repo), "branch", "alt"], check=True)
    monkeypatch.chdir(repo)
    spawn_calls, attach_calls = _spies(monkeypatch)

    rc = cli_mod.main(["new", "--branch", "alt", "--checkout"])
    assert rc == 0
    branch = subprocess.run(
        ["git", "-C", str(repo), "branch", "--show-current"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert branch == "alt"
    # No worktree was forked -- the real checkout already IS the answer.
    assert spawn_calls == [{"cwd": str(repo), "base_branch": None}]
    assert attach_calls


def test_checkout_flag_refuses_a_dirty_tree(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("DOXA_WORKTREE", "0")
    repo = _repo(tmp_path)
    subprocess.run(["git", "-C", str(repo), "branch", "alt"], check=True)
    (repo / "f.txt").write_text("dirty", encoding="utf-8")
    monkeypatch.chdir(repo)
    spawn_calls, _ = _spies(monkeypatch)

    rc = cli_mod.main(["new", "--branch", "alt", "--checkout"])
    assert rc == 2
    assert spawn_calls == []
    assert "dirty tree" in capsys.readouterr().err
    branch = subprocess.run(
        ["git", "-C", str(repo), "branch", "--show-current"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert branch == "trunk"  # never moved

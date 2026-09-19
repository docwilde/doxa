# SPDX-License-Identifier: AGPL-3.0-only
"""Item D (cli wiring): plain `doxa` restores a saved tab set for its
scope instead of the single-most-recent-session attach, falls back to
that single-attach path when nothing was ever saved (or the setting is
off), and `doxa new` never consults a saved set at all.

Same house split test_cli_branch.py already established: this file is
about doxa.cli's OWN plumbing -- _run_attached/_run_restored/spawn_daemon
are monkeypatched to recording stand-ins, never a real daemon subprocess
or a real Textual app (tests/test_tabsets.py covers doxa.app's side of the
wiring; tests/test_daemon.py covers a real daemon end to end).
"""

from __future__ import annotations

import json
import os

import pytest

from doxa import cli as cli_mod
from doxa import config as config_mod
from doxa import peers as peers_mod
from doxa import tabsets


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "doxa-home"))
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "runtime"))
    config_mod.invalidate()
    yield
    config_mod.invalidate()


def _daemon_entry(session_id: str, scope: str, cwd: "str | None" = None) -> None:
    reg = peers_mod.registry_dir()
    entry = {
        "session_id": session_id,
        "pid": os.getpid(),
        "socket_path": str(reg / f"{session_id}.sock"),
        "cwd": cwd or scope,
        "repo_root": scope,
        "title": session_id[:8],
        "started_at": peers_mod._iso_now(),
        "heartbeat_at": peers_mod._iso_now(),
        "daemon_socket": str(reg / f"{session_id}-daemon.sock"),
    }
    (reg / f"{session_id}.json").write_text(json.dumps(entry), encoding="utf-8")


def _spies(monkeypatch):
    restored_calls = []
    attached_calls = []
    spawn_calls = []

    def fake_restored(resolved, cwd, model, linger, engine=None):
        restored_calls.append(resolved)

    def fake_attached(socket_path, cwd, model, linger, engine=None):
        attached_calls.append(socket_path)

    def fake_spawn(cwd, model=None, linger_secs=120.0, base_branch=None,
                   engine=None, **_rest):
        spawn_calls.append(cwd)
        return "spawned-session-id", "/tmp/spawned.sock"

    monkeypatch.setattr(cli_mod, "_run_restored", fake_restored)
    monkeypatch.setattr(cli_mod, "_run_attached", fake_attached)
    monkeypatch.setattr(cli_mod, "spawn_daemon", fake_spawn)
    return restored_calls, attached_calls, spawn_calls


def test_plain_doxa_restores_a_saved_set_over_single_attach(monkeypatch, tmp_path):
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    monkeypatch.chdir(plain)
    scope = str(plain)
    tabsets.save(scope, [tabsets.TabRecord("sid-1")], "sid-1")
    _daemon_entry("sid-1", scope, cwd=str(plain))
    restored, attached, spawned = _spies(monkeypatch)

    rc = cli_mod.main([])

    assert rc == 0
    assert len(restored) == 1
    assert restored[0].skipped == 0
    assert [t.session_id for t, _ in restored[0].tabs] == ["sid-1"]
    assert attached == []  # the single-most-recent path was never consulted
    assert spawned == []


def test_no_saved_record_falls_back_to_single_attach(monkeypatch, tmp_path):
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    monkeypatch.chdir(plain)
    scope = str(plain)
    _daemon_entry("sid-live", scope, cwd=str(plain))  # live, but never saved
    restored, attached, spawned = _spies(monkeypatch)

    rc = cli_mod.main([])

    assert rc == 0
    assert restored == []
    assert len(attached) == 1
    assert spawned == []


def test_restore_tabs_off_skips_the_restore_consult(monkeypatch, tmp_path):
    monkeypatch.setenv("DOXA_RESTORE_TABS", "0")
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    monkeypatch.chdir(plain)
    scope = str(plain)
    tabsets.save(scope, [tabsets.TabRecord("sid-1")], "sid-1")
    _daemon_entry("sid-1", scope, cwd=str(plain))
    restored, attached, spawned = _spies(monkeypatch)

    rc = cli_mod.main([])

    assert rc == 0
    assert restored == []  # never even asked doxa.tabsets.resolve
    assert len(attached) == 1  # falls back to today's single-attach exactly


def test_doxa_new_bypasses_restore_even_with_a_saved_set(monkeypatch, tmp_path):
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    monkeypatch.chdir(plain)
    scope = str(plain)
    tabsets.save(
        scope, [tabsets.TabRecord("sid-1"), tabsets.TabRecord("sid-2")], "sid-2"
    )
    _daemon_entry("sid-1", scope, cwd=str(plain))
    _daemon_entry("sid-2", scope, cwd=str(plain))
    restored, attached, spawned = _spies(monkeypatch)

    rc = cli_mod.main(["new"])

    assert rc == 0
    assert restored == []
    assert len(spawned) == 1
    assert len(attached) == 1  # spawn-new's own attach, exactly one fresh tab


def test_doxa_attach_bypasses_restore(monkeypatch, tmp_path):
    """`doxa attach <prefix>` stays the single-session path either way --
    the judgment call the item's spec flagged explicitly."""
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    monkeypatch.chdir(plain)
    scope = str(plain)
    tabsets.save(scope, [tabsets.TabRecord("sid-1")], "sid-1")
    _daemon_entry("sid-1", scope, cwd=str(plain))
    restored, attached, spawned = _spies(monkeypatch)

    rc = cli_mod.main(["attach"])

    assert rc == 0
    assert restored == []
    assert len(attached) == 1


# -- _run_restored's own plumbing (no real DoxaApp, no real daemon) -----


def test_restore_report_text_pluralizes_correctly():
    assert cli_mod._restore_report_text(0, 0) is None
    assert cli_mod._restore_report_text(1, 0) == "tab restore: restored 1 tab."
    assert cli_mod._restore_report_text(2, 0) == "tab restore: restored 2 tabs."
    assert (
        cli_mod._restore_report_text(0, 1)
        == "tab restore: skipped 1 session no longer running."
    )
    assert (
        cli_mod._restore_report_text(1, 2)
        == "tab restore: restored 1 tab, skipped 2 sessions no longer running."
    )


class _RecordingApp:
    """Stands in for doxa.app.DoxaApp: records its constructor kwargs,
    never actually boots Textual or touches a real terminal."""

    instances: list = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        _RecordingApp.instances.append(self)

    def run(self):
        pass


@pytest.fixture(autouse=True)
def _reset_recording_app():
    _RecordingApp.instances = []
    yield


def _spy_spawn_only(monkeypatch):
    """_run_restored is the function UNDER TEST here -- unlike _spies
    above, this must NOT monkeypatch it away, only its own two
    dependencies (spawn_daemon, DoxaApp)."""
    spawn_calls = []

    def fake_spawn(cwd, model=None, linger_secs=120.0, base_branch=None,
                   engine=None, **_rest):
        spawn_calls.append(cwd)
        return "spawned-session-id", "/tmp/spawned.sock"

    monkeypatch.setattr(cli_mod, "spawn_daemon", fake_spawn)
    monkeypatch.setattr(cli_mod, "DoxaApp", _RecordingApp)
    return spawn_calls


def test_run_restored_with_every_saved_session_dead_spawns_one_fresh_tab(
    monkeypatch, tmp_path
):
    spawned = _spy_spawn_only(monkeypatch)
    resolved = tabsets.ResolvedRestore(tabs=[], skipped=2, active_session_id=None)

    cli_mod._run_restored(resolved, str(tmp_path), None, 120.0)

    assert len(spawned) == 1  # one fresh daemon -- never a replacement per dead id
    kwargs = _RecordingApp.instances[0].kwargs
    assert "restore_tabs" not in kwargs  # fell back to the ordinary single-pane branch
    assert kwargs["restore_report"] == "tab restore: skipped 2 sessions no longer running."


def test_run_restored_with_live_tabs_builds_specs_in_saved_order(monkeypatch, tmp_path):
    spawned = _spy_spawn_only(monkeypatch)
    entry_a = peers_mod.PeerInfo(
        session_id="sid-a", pid=1, socket_path="s", cwd=str(tmp_path),
        repo_root=None, title="a", started_at="t", heartbeat_at="t",
        daemon_socket="/sock/a",
    )
    entry_b = peers_mod.PeerInfo(
        session_id="sid-b", pid=1, socket_path="s", cwd=str(tmp_path),
        repo_root=None, title="b", started_at="t", heartbeat_at="t",
        daemon_socket="/sock/b",
    )
    resolved = tabsets.ResolvedRestore(
        tabs=[
            (tabsets.TabRecord("sid-a", "alpha"), entry_a),
            (tabsets.TabRecord("sid-b", None), entry_b),
        ],
        skipped=1,
        active_session_id="sid-b",
    )

    cli_mod._run_restored(resolved, str(tmp_path), None, 120.0)

    assert spawned == []  # both tabs are live -- no fresh daemon needed
    kwargs = _RecordingApp.instances[0].kwargs
    specs = kwargs["restore_tabs"]
    assert [s.session_id for s in specs] == ["sid-a", "sid-b"]
    assert specs[0].pinned_name == "alpha"
    assert specs[1].pinned_name is None
    assert kwargs["restore_active_id"] == "sid-b"
    assert kwargs["restore_report"] == (
        "tab restore: restored 2 tabs, skipped 1 session no longer running."
    )


# -- a restored tab comes back on ITS OWN engine (issue #46) -------------
#
# The `engine` _run_restored closes over is what a FRESH tab spawns -- the
# --engine this process was launched with. Handing it to a RESUME would
# reopen a Codex conversation as a Claude one, under the resumed id and on
# top of the resumed transcript, which is the same defect the gate had one
# layer up. A saved tab records no engine (TabRecord is a session id, a
# pinned name and a cwd), so the spawn derives it the way the gate does:
# from the artefact the session left beside its transcript.

CODEX_SID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
CLAUDE_SID = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"


@pytest.fixture
def _projects_dir(monkeypatch, tmp_path):
    """This test file's own PROJECTS_DIR (see tests/test_resume.py's
    fixture of the same shape for why the attribute, not the env var)."""
    import lore_core.config as lore_config

    project = tmp_path / "projects" / "-work"
    project.mkdir(parents=True)
    monkeypatch.setattr(lore_config, "PROJECTS_DIR", tmp_path / "projects")
    return project


def _spy_spawn_kwargs(monkeypatch):
    """_spy_spawn_only's recording sibling: keeps every kwarg, because the
    thing under test here IS one of them."""
    calls = []

    def fake_spawn(cwd, **kwargs):
        calls.append({"cwd": cwd, **kwargs})
        return kwargs.get("resume") or "spawned-session-id", "/tmp/spawned.sock"

    monkeypatch.setattr(cli_mod, "spawn_daemon", fake_spawn)
    monkeypatch.setattr(cli_mod, "DoxaApp", _RecordingApp)
    return calls


def _one_ended_tab(session_id: str, cwd: str):
    return tabsets.ResolvedRestore(
        tabs=[], skipped=0, active_session_id=session_id,
        archived=[tabsets.TabRecord(session_id, None, cwd)],
    )


def test_a_restored_codex_tab_is_resumed_on_codex(
    monkeypatch, tmp_path, _projects_dir
):
    """Launched as a Claude window, restoring an ended CODEX conversation:
    the resume spawn carries engine="codex", not the window's own."""
    (_projects_dir / f"{CODEX_SID}.codex.json").write_text(
        json.dumps({"thread_id": "thr-0199", "session_id": CODEX_SID}),
        encoding="utf-8",
    )
    calls = _spy_spawn_kwargs(monkeypatch)

    cli_mod._run_restored(
        _one_ended_tab(CODEX_SID, str(tmp_path)), str(tmp_path), None, 120.0,
        engine=None,
    )

    specs = _RecordingApp.instances[0].kwargs["restore_tabs"]
    assert [s.session_id for s in specs] == [CODEX_SID]
    assert specs[0].resume is True  # the gate said yes -- it used to say no
    specs[0].engine_factory()  # the spawn itself, deferred until now

    resume_calls = [c for c in calls if c.get("resume") == CODEX_SID]
    assert len(resume_calls) == 1
    assert resume_calls[0]["engine"] == "codex"

    # The OTHER caller, same closure: DoxaApp._resume_session_factory is
    # what doxa.appwindow.tabs.resume_session (the /resume popup) calls,
    # and it is handed this exact function -- so the popup's spawn is the
    # boot restore's spawn, engine and all.
    _RecordingApp.instances[0].kwargs["resume_session_factory"](
        str(tmp_path), CODEX_SID,
    )
    resume_calls = [c for c in calls if c.get("resume") == CODEX_SID]
    assert len(resume_calls) == 2
    assert resume_calls[1]["engine"] == "codex"


def test_a_restored_claude_tab_is_resumed_on_claude_in_a_codex_window(
    monkeypatch, tmp_path, _projects_dir
):
    """The other direction, which is the same bug mirrored: a window
    launched with --engine codex must not reopen a Claude conversation on
    Codex. The derived engine WINS over the window's."""
    from doxa import cli_isolation as cli_isolation_mod

    cli_history = (
        cli_isolation_mod.cli_config_dir() / "projects" / "-work"
        / f"{CLAUDE_SID}.jsonl"
    )
    cli_history.parent.mkdir(parents=True, exist_ok=True)
    cli_history.write_text('{"type":"user"}\n', encoding="utf-8")
    calls = _spy_spawn_kwargs(monkeypatch)

    cli_mod._run_restored(
        _one_ended_tab(CLAUDE_SID, str(tmp_path)), str(tmp_path), None, 120.0,
        engine="codex",
    )

    specs = _RecordingApp.instances[0].kwargs["restore_tabs"]
    specs[0].engine_factory()

    resume_calls = [c for c in calls if c.get("resume") == CLAUDE_SID]
    assert len(resume_calls) == 1
    assert resume_calls[0]["engine"] == "claude"


def test_a_tab_no_engine_can_resume_still_comes_back_read_only(
    monkeypatch, tmp_path, _projects_dir
):
    """No artefact anywhere: the pre-existing outcome, unchanged, and no
    spawn at all."""
    calls = _spy_spawn_kwargs(monkeypatch)

    cli_mod._run_restored(
        _one_ended_tab(CODEX_SID, str(tmp_path)), str(tmp_path), None, 120.0,
    )

    specs = _RecordingApp.instances[0].kwargs["restore_tabs"]
    assert specs[0].archived is True
    assert specs[0].resume is False
    assert "v0.56.0" in specs[0].resume_note
    assert [c for c in calls if c.get("resume")] == []

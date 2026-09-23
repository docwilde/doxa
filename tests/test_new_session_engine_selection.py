# SPDX-License-Identifier: AGPL-3.0-only
"""A new tab obeys /engine after a window first attached to another engine."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from doxa import cli, config, tabsets
from doxa.app import DoxaApp, SystemBlock
from doxa.settings import field_id
from tests.fakes import FakeEngine


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch, tmp_path):
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "doxa-home"))
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.delenv("DOXA_ENGINE", raising=False)
    monkeypatch.delenv("DOXA_MODEL", raising=False)
    config.invalidate()
    yield
    config.invalidate()


async def _command(app, pilot, value):
    before = len(app.query(SystemBlock))
    app.query_one("#prompt-input").value = value
    await pilot.press("enter")
    for _ in range(100):
        if len(app.query(SystemBlock)) > before:
            return
        await pilot.pause(0.02)
    raise AssertionError(f"{value!r} produced no response")


@pytest.mark.parametrize("configured_engine", ["codex", "claude"])
async def test_engine_claude_changes_the_next_daemon_spawn_and_uses_claude_model(
    monkeypatch, tmp_path, configured_engine,
):
    """Drive the CLI's actual factory through /engine and Ctrl+T, without a daemon."""
    config.save({"engine": configured_engine})
    config.save_model("codex", "gpt-6-sol")
    config.save_model("claude", "opus")
    captured = []
    spawns = []
    sockets = {"initial-codex": ("codex", "gpt-6-sol")}

    def spawn(cwd, **kwargs):
        spawns.append({"cwd": cwd, **kwargs})
        socket = f"new-{len(spawns)}"
        sockets[socket] = (kwargs["engine"], kwargs["model"])
        return f"session-{len(spawns)}", socket

    def client(socket, **_kwargs):
        engine_id, model = sockets[socket]
        fake = FakeEngine([], model=model or "default", cwd=str(tmp_path))
        fake.engine_id = engine_id
        return fake

    monkeypatch.setattr(DoxaApp, "run", lambda self: captured.append(self))
    monkeypatch.setattr(cli, "spawn_daemon", spawn)
    monkeypatch.setattr("doxa.client.EngineClient", client)
    cli._run_attached("initial-codex", str(tmp_path), "gpt-6-sol", 120.0,
                      engine="codex")
    app = captured[0]

    async with app.run_test() as pilot:
        await pilot.pause()
        initial = app.engine
        assert initial.engine_id == "codex"
        await _command(app, pilot, "/engine claude")
        assert app.engine is initial  # the running conversation stays on Codex
        await app.action_new_tab()
        for _ in range(100):
            if app.engine is not initial and getattr(app.engine, "started", False):
                break
            await pilot.pause(0.02)
        assert app.engine is not initial
        assert app.engine.engine_id == "claude"
        assert app.engine.model == "opus"

    assert spawns == [{
        "cwd": str(tmp_path), "model": "opus", "linger_secs": 120.0,
        "engine": "claude",
    }]


def test_launch_flag_stays_in_effect_until_the_in_app_selection_changes(monkeypatch):
    """An explicit --engine remains the window default before /engine runs."""
    app = DoxaApp(cwd="/tmp")
    assert cli._fresh_selection(app, "codex", "gpt-6-sol") == (
        "codex", "gpt-6-sol",
    )
    app._new_session_engine_override = "claude"
    assert cli._fresh_selection(app, "codex", "gpt-6-sol") == (
        "claude", config.model("claude"),
    )


async def test_settings_engine_change_updates_the_same_next_session_selection(
    tmp_path,
):
    config.save({"engine": "codex"})
    fake = FakeEngine([], model="gpt-6-sol")
    fake.engine_id = "codex"
    app = DoxaApp(cwd=str(tmp_path), engine_factory=lambda: fake)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+comma")
        await pilot.pause()
        screen = app.screen
        screen.query_one(f"#{field_id('engine')}").value = "claude"
        screen.action_save()
        screen.action_cancel()  # the app callback runs when the modal closes
        for _ in range(100):
            if app._new_session_engine_override == "claude":
                break
            await pilot.pause(0.02)
        assert app._new_session_engine_override == "claude"
        assert app.engine is fake


@pytest.mark.parametrize("has_live_tab", [False, True])
def test_restored_window_uses_updated_engine_for_fresh_sessions(
    monkeypatch, tmp_path, has_live_tab,
):
    config.save_model("claude", "opus")
    calls = []
    apps = []

    class RecordingApp:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self._new_session_engine_override = None
            apps.append(self)

        def run(self):
            pass

    def spawn(cwd, **kwargs):
        calls.append({"cwd": cwd, **kwargs})
        return "new-session", "/tmp/fake.sock"

    monkeypatch.setattr(cli, "DoxaApp", RecordingApp)
    monkeypatch.setattr(cli, "spawn_daemon", spawn)
    monkeypatch.setattr("doxa.client.EngineClient", lambda socket, **_kw: socket)
    entry = SimpleNamespace(
        session_id="live-session", cwd=str(tmp_path),
        daemon_socket="/tmp/live.sock",
    )
    resolved = tabsets.ResolvedRestore(
        tabs=[(tabsets.TabRecord("live-session"), entry)] if has_live_tab else [],
        skipped=0, active_session_id="live-session" if has_live_tab else None,
    )

    cli._run_restored(resolved, str(tmp_path), "gpt-6-sol", 120.0,
                      engine="codex")
    app = apps[0]
    app._new_session_engine_override = "claude"
    app.kwargs["new_session_factory"]()
    app.kwargs["new_session_factory_at"](str(tmp_path / "other"))

    assert [call["engine"] for call in calls[-2:]] == ["claude", "claude"]
    assert [call["model"] for call in calls[-2:]] == ["opus", "opus"]


def test_in_process_window_can_select_claude_for_its_next_session(
    monkeypatch, tmp_path,
):
    config.save_model("claude", "opus")
    apps = []

    class RecordingApp:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self._new_session_engine_override = None
            apps.append(self)

        def run(self):
            pass

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "DoxaApp", RecordingApp)
    monkeypatch.setattr(
        "doxa.app.SessionEngine",
        lambda cwd, model: SimpleNamespace(engine_id="claude", model=model),
    )
    assert cli.main(["--engine", "codex", "--in-process"]) == 0
    app = apps[0]
    app._new_session_engine_override = "claude"

    fresh = app.kwargs["new_session_factory"]()
    assert (fresh.engine_id, fresh.model) == ("claude", "opus")

"""Command-palette tests (Ctrl+P): the DOXA provider is registered on the
app, the command surface carries the Phase 2 commands, the attach picker
lists live daemon sessions straight from the shared registry, and the
belief-inspector stub actually toggles. Headless via the Pilot harness,
FakeEngine underneath -- same pattern as tests/test_app.py.
"""

from __future__ import annotations

import json
import os

import pytest

from doxa import peers
from doxa.app import DoxaApp
from doxa.palette import DoxaCommandProvider
from tests.fakes import FakeEngine

EXPECTED_COMMANDS = {
    "New session",
    "Peers: list",
    "Peers: message",
    "Belief inspector: toggle",
    "Quit: detach",
    "Quit: stop session",
}


def _seed_daemon_entry(sid: str, title: str, cwd: str = "/work/repo") -> None:
    """A live registry entry with the daemon marker: our own pid (alive)
    and a fresh heartbeat, so read_registry treats it as live."""
    now = peers._iso_now()
    entry = {
        "session_id": sid, "pid": os.getpid(),
        "socket_path": f"/tmp/peer-{sid[:8]}.sock",
        "cwd": cwd, "repo_root": cwd, "title": title,
        "started_at": now, "heartbeat_at": now,
        "daemon_socket": f"/tmp/daemon-{sid[:8]}.sock",
    }
    path = peers.registry_dir() / f"{sid}.json"
    path.write_text(json.dumps(entry), encoding="utf-8")


@pytest.mark.asyncio
async def test_palette_provider_registered_and_commands_present(monkeypatch, tmp_path):
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: FakeEngine([]))

    assert DoxaCommandProvider in DoxaApp.COMMANDS

    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        names = {name for name, _help, _cb in app.doxa_commands()}
        assert EXPECTED_COMMANDS <= names


@pytest.mark.asyncio
async def test_attach_picker_lists_live_sessions_from_registry(monkeypatch, tmp_path):
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: FakeEngine([]))
    _seed_daemon_entry("aaaa1111-0000-0000", "alpha")
    _seed_daemon_entry("bbbb2222-0000-0000", "beta", cwd="/work/other")

    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        commands = app.doxa_commands()
        names = [name for name, _help, _cb in commands]
        assert "Attach: alpha (aaaa1111)" in names
        assert "Attach: beta (bbbb2222)" in names
        # The picker's help names where the session lives.
        helps = {name: help_text for name, help_text, _cb in commands}
        assert "/work/repo" in helps["Attach: alpha (aaaa1111)"]

        # And the provider surfaces them as palette hits.
        provider = DoxaCommandProvider(app.screen)
        hits = [hit async for hit in provider.search("attach alp")]
        assert any(h.text and "alpha" in h.text for h in hits)


@pytest.mark.asyncio
async def test_attach_picker_excludes_own_session(monkeypatch, tmp_path):
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    fake = FakeEngine([])
    fake.session_id = "aaaa1111-0000-0000"  # pretend we ARE alpha
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: fake)
    _seed_daemon_entry("aaaa1111-0000-0000", "alpha")
    _seed_daemon_entry("bbbb2222-0000-0000", "beta")

    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        names = [name for name, _help, _cb in app.doxa_commands()]
        assert "Attach: beta (bbbb2222)" in names
        assert not any("alpha" in n for n in names)


@pytest.mark.asyncio
async def test_belief_inspector_stub_toggles(monkeypatch, tmp_path):
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: FakeEngine([]))

    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        panel = app.query_one("#belief-inspector")
        assert panel.display is False
        app.action_toggle_inspector()
        await pilot.pause()
        assert panel.display is True
        assert "3 active beliefs" in str(panel.renderable)  # FakeEngine's count
        app.action_toggle_inspector()
        await pilot.pause()
        assert panel.display is False


@pytest.mark.asyncio
async def test_new_session_command_switches_engine(monkeypatch, tmp_path):
    """The palette's 'New session' swaps in a fresh engine from the
    new_session_factory and resets the block list."""
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    engines = []

    def make_engine():
        engines.append(FakeEngine([]))
        return engines[-1]

    app = DoxaApp(
        cwd=str(tmp_path),
        engine_factory=make_engine,
        new_session_factory=make_engine,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        first = app.engine
        assert first is engines[0] and first.started

        app._cmd_new_session()
        for _ in range(100):
            if len(engines) == 2 and app.engine is engines[1] and engines[1].started:
                break
            await pilot.pause(0.02)
        assert app.engine is engines[1]
        assert engines[1].started is True
        assert engines[0].finalized is True  # old handle released


@pytest.mark.asyncio
async def test_quit_stop_prefers_engine_stop(monkeypatch, tmp_path):
    """Quit-stop calls stop() (finalize-the-daemon-now) when the engine
    offers it -- the detach-vs-stop distinction the palette exists for."""
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))

    class StoppableEngine(FakeEngine):
        def __init__(self):
            super().__init__([])
            self.stopped = False

        async def stop(self):
            self.stopped = True

    engine = StoppableEngine()
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: engine)
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_quit_stop()
    assert engine.stopped is True
    assert engine.finalized is False  # stop, not detach-finalize

# SPDX-License-Identifier: AGPL-3.0-only
"""Command-palette tests (Ctrl+P): the DOXA provider is registered on the
app, the command surface carries the Phase 2 commands, the attach picker
lists live daemon sessions straight from the shared registry, and the
belief-inspector stub actually toggles. Headless via the Pilot harness,
FakeEngine underneath -- same pattern as tests/test_app.py.
"""

from __future__ import annotations

import contextlib
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


_OPEN_SOCKETS: list = []


def _listen(path: str):
    """A real AF_UNIX listener, held open for the test: since the launch
    sweep (and the peer count) check that a session's socket ACCEPTS a
    connection, a seeded entry needs a real one to count as live."""
    import socket as _socket

    with contextlib.suppress(OSError):
        os.unlink(path)
    sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    sock.bind(path)
    sock.listen(1)
    _OPEN_SOCKETS.append(sock)
    return sock


def _seed_daemon_entry(sid: str, title: str, cwd: str = "/work/repo") -> None:
    """A live registry entry with the daemon marker: our own pid (alive),
    a fresh heartbeat and a socket that answers, so every liveness check
    read_registry makes passes."""
    now = peers._iso_now()
    peers.registry_dir()  # ensures the runtime dir exists, clamped 0700
    socket_path = str(peers.runtime_dir() / f"peer-{sid[:8]}.sock")
    _listen(socket_path)
    entry = {
        "session_id": sid, "pid": os.getpid(),
        "socket_path": socket_path,
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
        names = {entry.label for entry in app.doxa_commands()}
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
        names = [entry.label for entry in commands]
        assert "Attach: alpha (aaaa1111)" in names
        assert "Attach: beta (bbbb2222)" in names
        # The picker's help names where the session lives.
        helps = {entry.label: entry.help for entry in commands}
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
        names = [entry.label for entry in app.doxa_commands()]
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


# -- palette ORDER (the whole list, not just the registry rows) -----------


async def _tabbed_app(monkeypatch, tmp_path, tabs: int = 3):
    """An app with `tabs` open tabs, each on its own FakeEngine."""
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    engines: list[FakeEngine] = []

    def make():
        engines.append(FakeEngine([]))
        return engines[-1]

    app = DoxaApp(cwd=str(tmp_path), engine_factory=make, new_session_factory=make)
    return app, engines


async def _open_tabs(app, pilot, count: int) -> None:
    for _ in range(count - 1):
        await pilot.press("ctrl+t")
        for _ in range(100):
            if len(app.panes()) == count:
                break
            await pilot.pause(0.02)
    await pilot.pause(0.05)


@pytest.mark.asyncio
async def test_unfiltered_palette_order(monkeypatch, tmp_path):
    """New tab, then the open tabs in TAB-BAR order, then the commands in
    the registry's groups, then the attachable sessions."""
    from doxa import commands as commands_mod
    from doxa.palette import (
        SECTION_ATTACH,
        SECTION_NEW,
        SECTION_TABS,
        SECTIONS,
    )

    app, _engines = await _tabbed_app(monkeypatch, tmp_path)
    _seed_daemon_entry("cccc3333-0000-0000", "elsewhere")  # after the env is set
    async with app.run_test() as pilot:
        await pilot.pause()
        await _open_tabs(app, pilot, 3)

        entries = app.doxa_commands()
        sections = [e.section for e in entries]
        # Sections appear in SECTIONS order and never interleave.
        seen = [s for i, s in enumerate(sections) if i == 0 or sections[i - 1] != s]
        assert seen == sorted(seen, key=SECTIONS.index)
        assert len(seen) == len(set(seen))

        assert entries[0].section == SECTION_NEW
        assert entries[0].label == "New tab"

        # Open tabs: left to right, exactly the tab bar's order.
        tabs = [e for e in entries if e.section == SECTION_TABS]
        assert [t.label.split("  ")[0] for t in tabs] == [
            # v0.91.0: a leaf is not the tab, so the palette names the
            # PANE (display_name) rather than the tab header's title --
            # which is also how two sessions sharing one tab are told
            # apart here.
            p.display_name() for p in app.panes()
        ]

        # Commands: the registry's grouping and its within-group order --
        # the SAME sequence the dropdown and /help use (item A's contract).
        labelled = [c.palette for c in commands_mod.ordered() if c.palette]
        labels = [e.label for e in entries]
        positions = [labels.index(label) for label in labelled]
        assert positions == sorted(positions)
        for entry in entries:
            if entry.section in commands_mod.GROUPS:
                continue
            assert entry.section in (SECTION_NEW, SECTION_TABS, SECTION_ATTACH)

        # Attach last.
        assert entries[-1].section == SECTION_ATTACH
        assert entries[-1].label.startswith("Attach: elsewhere")


@pytest.mark.asyncio
async def test_the_active_tab_is_marked(monkeypatch, tmp_path):
    from doxa.palette import SECTION_TABS

    app, _engines = await _tabbed_app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _open_tabs(app, pilot, 3)

        def marks():
            return [
                e.label.endswith("· active")
                for e in app.doxa_commands() if e.section == SECTION_TABS
            ]

        assert marks() == [False, False, True]  # ctrl+t focused the newest
        app._cycle_tab(1)  # wraps to the first
        await pilot.pause()
        assert marks() == [True, False, False]


@pytest.mark.asyncio
async def test_palette_headers_are_unselectable_and_skipped(monkeypatch, tmp_path):
    """The real Ctrl+P screen: section headers are disabled rows, the
    highlight starts below one, and arrowing the whole list never lands on
    one."""
    from doxa.palette import DoxaPalette

    app, _engines = await _tabbed_app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _open_tabs(app, pilot, 2)

        await pilot.press("ctrl+p")
        for _ in range(200):
            if isinstance(app.screen, DoxaPalette):
                break
            await pilot.pause(0.02)
        assert isinstance(app.screen, DoxaPalette)

        from textual.command import CommandList

        command_list = app.screen.query_one(CommandList)
        for _ in range(200):
            if command_list.option_count > 3:
                break
            await pilot.pause(0.02)

        rows = [
            command_list.get_option_at_index(i)
            for i in range(command_list.option_count)
        ]
        headers = [r for r in rows if r.disabled]
        assert [str(h.prompt) for h in headers][:2] == ["New", "Open tabs"]
        assert command_list.highlighted is not None
        assert not rows[command_list.highlighted].disabled

        for _ in range(command_list.option_count + 2):
            assert not rows[command_list.highlighted].disabled
            await pilot.press("down")
            await pilot.pause()


@pytest.mark.asyncio
async def test_filtering_keeps_tabs_above_commands_at_equal_score(
    monkeypatch, tmp_path
):
    """Equal match, tab first: someone who opens the palette mid-work is
    usually switching tabs. The tab here is renamed to the command's exact
    label, so the two scores are equal by construction."""
    from operator import attrgetter

    app, _engines = await _tabbed_app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _open_tabs(app, pilot, 2)
        # == the /usage row's palette label. Named the way a user names a
        # tab, which is what the palette reads since v0.91.0.
        app.panes()[0].set_custom_name("Usage")

        provider = DoxaCommandProvider(app.screen)
        hits = [hit async for hit in provider.search("usage")]
        texts = [h.text for h in hits]
        assert "Usage" in texts
        tab_at = texts.index("Usage")
        command_at = [
            i for i, h in enumerate(hits) if h.text == "Usage"
        ][-1]
        assert tab_at < command_at  # yielded tab-first...
        assert hits[tab_at].score == hits[command_at].score
        # ...and the palette's own sort (by score, stable) keeps it there.
        # (Hit.__eq__ compares scores, so positions are found by identity.)
        ranked = sorted(hits, key=attrgetter("score"), reverse=True)
        order = [id(h) for h in ranked]
        assert order.index(id(hits[tab_at])) < order.index(id(hits[command_at]))


@pytest.mark.asyncio
async def test_closing_a_tab_updates_the_next_palette_open(monkeypatch, tmp_path):
    """Live state, never a snapshot: the entries are rebuilt per open."""
    from doxa.palette import SECTION_TABS

    app, _engines = await _tabbed_app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _open_tabs(app, pilot, 3)
        assert len([e for e in app.doxa_commands() if e.section == SECTION_TABS]) == 3

        await pilot.press("ctrl+w")
        for _ in range(100):
            if len(app.panes()) == 2:
                break
            await pilot.pause(0.02)
        assert len([e for e in app.doxa_commands() if e.section == SECTION_TABS]) == 2

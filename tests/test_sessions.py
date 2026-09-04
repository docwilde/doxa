# SPDX-License-Identifier: AGPL-3.0-only
"""Ending a session vs detaching one, and telling them apart afterwards.

Ctrl+W detaches BY DESIGN -- the session keeps running and can be
reattached. What was missing was the other gesture: Ctrl+Q, which ends the
session for real. These tests pin the pair, the confirm that guards a
running turn, and the three things that keep the fleet honest once
detached sessions are expected to accumulate: a peer count that only counts
sessions which answer, a launch sweep for the files a crash leaves behind,
and /sessions as the place to see and reap them.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys

import pytest

from doxa import peers
from doxa.app import CloseWithTurnRunning, DoxaApp, SystemBlock
from doxa.engine import EngineEvent
from tests.fakes import FakeEngine

_OPEN: list = []


def _listen(path: str) -> None:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(path)
    sock.listen(1)
    _OPEN.append(sock)


def _entry(sid: str, title: str, clients: int | None = 0, alive: bool = True):
    """A registry entry with a REAL socket when `alive` -- the liveness
    check connects, so a fake path is exactly what a dead session looks
    like."""
    peers.registry_dir()
    socket_path = str(peers.runtime_dir() / f"peer-{sid[:8]}.sock")
    if alive:
        _listen(socket_path)
    now = peers._iso_now()
    entry = {
        "session_id": sid, "pid": os.getpid(), "socket_path": socket_path,
        "cwd": "/work/repo", "repo_root": "/work/repo", "title": title,
        "started_at": now, "heartbeat_at": now,
        "daemon_socket": f"/tmp/daemon-{sid[:8]}.sock",
    }
    if clients is not None:
        entry["clients"] = clients
    path = peers.registry_dir() / f"{sid}.json"
    path.write_text(json.dumps(entry), encoding="utf-8")
    return path


class StoppableEngine(FakeEngine):
    """Tells detach (finalize) and end (stop) apart, which the plain
    FakeEngine cannot -- and that difference IS this feature."""

    def __init__(self, script=None):
        super().__init__(list(script or []))
        self.stopped = False

    async def stop(self):
        self.stopped = True
        return EngineEvent("session_done", {"stopped": True})


async def _app(monkeypatch, tmp_path, script=None):
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    engines: list[StoppableEngine] = []

    def make():
        engines.append(StoppableEngine(script))
        return engines[-1]

    return DoxaApp(cwd=str(tmp_path), engine_factory=make,
                   new_session_factory=make), engines


async def _wait(pilot, cond, tries=200):
    for _ in range(tries):
        if cond():
            return True
        await pilot.pause(0.02)
    return cond()


# -- the two close keys ---------------------------------------------------


@pytest.mark.asyncio
async def test_ctrl_q_ends_the_session_and_closes_the_tab(monkeypatch, tmp_path):
    app, engines = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+t")
        assert await _wait(pilot, lambda: len(app.panes()) == 2)
        second = engines[1]

        await pilot.press("ctrl+q")
        assert await _wait(pilot, lambda: len(app.panes()) == 1)
        assert second.stopped is True          # ended, not merely released
        assert engines[0].stopped is False     # the other tab is untouched
        assert app.is_running                  # ...and the APP is still up


@pytest.mark.asyncio
async def test_ctrl_w_still_detaches(monkeypatch, tmp_path):
    app, engines = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+t")
        assert await _wait(pilot, lambda: len(app.panes()) == 2)
        second = engines[1]

        await pilot.press("ctrl+w")
        assert await _wait(pilot, lambda: len(app.panes()) == 1)
        assert second.stopped is False   # keeps running -- by design
        assert second.finalized is True  # the CLIENT let go, that is all


@pytest.mark.asyncio
async def test_ctrl_q_with_a_turn_running_asks_first(monkeypatch, tmp_path):
    """Killing work silently and keeping it silently are both wrong."""
    import asyncio

    class HangingEngine(StoppableEngine):
        """A turn that starts and never finishes -- what Ctrl+Q has to ask
        about."""

        async def send(self, prompt):
            yield EngineEvent("turn_started", {})
            await asyncio.Event().wait()  # never returns

    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    engines: list[StoppableEngine] = []

    def make():
        engines.append(HangingEngine())
        return engines[-1]

    app = DoxaApp(cwd=str(tmp_path), engine_factory=make,
                  new_session_factory=make)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+t")
        assert await _wait(pilot, lambda: len(app.panes()) == 2)
        pane = app.active_pane
        pane.query_one("#prompt-input").value = "a long one"
        await pilot.press("enter")
        assert await _wait(pilot, lambda: pane.turn_in_flight)

        await pilot.press("ctrl+q")
        assert await _wait(pilot, lambda: isinstance(app.screen, CloseWithTurnRunning))

        await pilot.press("c")  # cancel: nothing happens to anything
        await pilot.pause()
        assert len(app.panes()) == 2
        assert engines[1].stopped is False

        await pilot.press("ctrl+q")
        assert await _wait(pilot, lambda: isinstance(app.screen, CloseWithTurnRunning))
        await pilot.press("d")  # detach instead: tab closes, session lives
        assert await _wait(pilot, lambda: len(app.panes()) == 1)
        assert engines[1].stopped is False
        assert engines[1].finalized is True


# -- the confirm's DEFAULT door (reported twice from live use) ------------
#
# "CTRL+Q is still just sending a session to background and detaches it
# instead of finalising the session."
#
# The idle path above was never the defect: Ctrl+Q on an idle session
# finalizes, and did before this branch (measured against a real
# `python -m doxa.daemon` subprocess -- presence file gone, socket gone,
# process exited inside a second). The defect was one keystroke further
# in. A turn in flight opens CloseWithTurnRunning, and through v1.6.0 that
# dialog labelled DETACH as its Enter default -- a default chosen for
# Ctrl+W back when Ctrl+W was what opened it, and stale since v0.58.0,
# when action_close_tab stopped asking. Press Ctrl+Q, press the labelled
# default, and DOXA detached the session and toasted "still running in the
# background" -- the operator's own words, handed to them by the app.
#
# These two tests assert the OUTCOME, not the call. `stopped is True` on a
# mock is what let this ship twice: it proves a coroutine was awaited, not
# that anything died. LivingSessionEngine owns a REAL child process and
# models the daemon's two dispositions the way the socket does -- stop()
# ends the process, finalize() (detach) deliberately leaves it running --
# so the assertion at the end is `the process is gone`, made against the
# OS, and it fails on main.


class LivingSessionEngine(StoppableEngine):
    """A fake engine with a REAL process behind it, one per session.

    The child stands in for the daemon: ``stop()`` (Ctrl+Q's finalize-now)
    ends it, ``finalize()`` (a detach) closes this client's handle and
    deliberately leaves it running, exactly as ``EngineClient``'s two
    methods differ over a real socket. No `claude` CLI, no network, no
    quota -- just a process whose liveness is a fact about the OS rather
    than a boolean this class sets on itself."""

    def __init__(self, script=None):
        super().__init__(list(script or []))
        self.proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def alive(self) -> bool:
        return self.proc.poll() is None

    async def send(self, prompt):
        yield EngineEvent("turn_started", {})
        await asyncio.Event().wait()  # a turn that never finishes

    async def stop(self):
        self.proc.terminate()
        await asyncio.get_running_loop().run_in_executor(None, self.proc.wait)
        return await super().stop()

    async def finalize(self):
        return await super().finalize()   # the child keeps running

    def kill(self) -> None:
        if self.alive():
            self.proc.kill()
            self.proc.wait()


async def _living_app(monkeypatch, tmp_path):
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    engines: list[LivingSessionEngine] = []

    def make():
        engines.append(LivingSessionEngine())
        return engines[-1]

    return DoxaApp(cwd=str(tmp_path), engine_factory=make,
                   new_session_factory=make), engines


@pytest.mark.asyncio
async def test_ctrl_q_on_a_running_turn_ends_the_PROCESS_on_its_default_door(
    monkeypatch, tmp_path
):
    """Ctrl+Q, then the key the dialog labels as its default. The session's
    process must be GONE -- not detached, not lingering."""
    app, engines = await _living_app(monkeypatch, tmp_path)
    try:
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("ctrl+t")
            assert await _wait(pilot, lambda: len(app.panes()) == 2)
            pane = app.active_pane
            target = engines[1]
            assert target.alive()

            pane.query_one("#prompt-input").value = "something long"
            await pilot.press("enter")
            assert await _wait(pilot, lambda: pane.turn_in_flight)

            await pilot.press("ctrl+q")
            assert await _wait(
                pilot, lambda: isinstance(app.screen, CloseWithTurnRunning)
            )
            await pilot.press("enter")   # the labelled default door
            assert await _wait(pilot, lambda: len(app.panes()) == 1)

            # The OS is the witness, not the engine's own bookkeeping.
            assert await _wait(pilot, lambda: not target.alive()), (
                "Ctrl+Q's default door left the session's process running -- "
                "it detached instead of finalizing"
            )
            assert engines[0].alive()    # the other tab is untouched
    finally:
        for engine in engines:
            engine.kill()


@pytest.mark.asyncio
async def test_the_confirm_says_which_key_takes_which_door(monkeypatch, tmp_path):
    """The label and the keymap have to agree. They did not: the buttons
    read `[ detach · enter ]` on a dialog only Ctrl+Q can open."""
    app, _engines = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = CloseWithTurnRunning()
        app.push_screen(screen)
        assert await _wait(pilot, lambda: isinstance(app.screen, screen.__class__))
        assert await _wait(pilot, lambda: bool(app.screen.query("#close-terminate")))
        labels = {
            node_id: str(app.screen.query_one(node_id).renderable)
            for node_id in ("#close-terminate", "#close-detach", "#close-cancel")
        }
        assert "enter" in labels["#close-terminate"]
        assert "enter" not in labels["#close-detach"]
        assert "· d" in labels["#close-detach"]
        screen.dismiss("cancel")
        assert await _wait(pilot, lambda: not app.query(CloseWithTurnRunning))

    # ...and every key takes the door its label promises, driven through
    # the dialog itself rather than read off the dict it happens to use.
    for key, door in (("enter", "terminate"), ("t", "terminate"),
                      ("d", "detach"), ("c", "cancel"), ("escape", "cancel")):
        app2, _ = await _app(monkeypatch, tmp_path)
        async with app2.run_test() as pilot:
            await pilot.pause()
            picked: list[str] = []
            app2.push_screen(CloseWithTurnRunning(), callback=picked.append)
            assert await _wait(
                pilot, lambda: isinstance(app2.screen, CloseWithTurnRunning)
            )
            await pilot.press(key)
            assert await _wait(pilot, lambda: bool(picked))
            assert picked == [door], f"{key} -> {picked!r}, expected {door!r}"


@pytest.mark.asyncio
async def test_a_finalize_that_raises_is_reported_not_swallowed(
    monkeypatch, tmp_path
):
    """Suspect 1, and the reason this defect could hide in plain sight: a
    stop that RAISED was swallowed whole (contextlib.suppress), the pane
    marked stopped, the tab closed, nothing said -- which from the
    operator's chair is indistinguishable from a detach.

    The tab still closes (the handle is already gone and the socket with
    it; refusing would strand a dead window) -- what it may no longer do
    is stay quiet."""
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    engines: list[StoppableEngine] = []

    class RefusingEngine(StoppableEngine):
        async def stop(self):
            raise RuntimeError("socket went away mid-finalize")

    def make():
        engines.append(RefusingEngine())
        return engines[-1]

    app = DoxaApp(cwd=str(tmp_path), engine_factory=make,
                  new_session_factory=make)
    said: list[str] = []
    async with app.run_test() as pilot:
        await pilot.pause()
        monkeypatch.setattr(
            app, "notify",
            lambda message, **kw: said.append(str(message)),
        )
        await pilot.press("ctrl+t")
        assert await _wait(pilot, lambda: len(app.panes()) == 2)

        await pilot.press("ctrl+q")
        assert await _wait(pilot, lambda: len(app.panes()) == 1)

    told = " ".join(said)
    assert "socket went away mid-finalize" in told, (
        f"the failure was swallowed; toasts were {said!r}"
    )
    assert "doxa stop" in told   # ...and how to finish the job


@pytest.mark.asyncio
async def test_an_idle_session_ends_without_a_prompt(monkeypatch, tmp_path):
    app, engines = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+t")
        assert await _wait(pilot, lambda: len(app.panes()) == 2)
        await pilot.press("ctrl+q")
        assert await _wait(pilot, lambda: len(app.panes()) == 1)
        assert not isinstance(app.screen, CloseWithTurnRunning)


# -- counting only what is alive ------------------------------------------


def test_a_stale_presence_file_is_not_a_peer(monkeypatch, tmp_path):
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    _entry("aaaa1111-0000", "alive", clients=1)
    _entry("dddd4444-0000", "crashed", clients=0, alive=False)

    live = peers.list_peers("/work/repo")
    assert [p.session_id for p in live] == ["aaaa1111-0000"]
    # Unprobed, the crashed one still LOOKS live -- which is the whole
    # reason the counting paths probe.
    assert len(peers.read_registry(probe=False)) == 2


def test_the_launch_sweep_removes_what_cannot_answer(monkeypatch, tmp_path):
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    kept = _entry("aaaa1111-0000", "alive", clients=1)
    gone = _entry("dddd4444-0000", "crashed", clients=0, alive=False)

    assert peers.sweep_stale() == 1
    assert kept.exists() and not gone.exists()
    assert peers.sweep_stale() == 0  # idempotent


@pytest.mark.asyncio
async def test_the_peers_chip_says_how_many_are_detached(monkeypatch, tmp_path):
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))

    class Peered(FakeEngine):
        def __init__(self, peers_list):
            super().__init__([])
            self._peers = peers_list

    def peer(sid, clients):
        return peers.PeerInfo(
            session_id=sid, pid=os.getpid(), socket_path="/tmp/x.sock",
            cwd="/work/repo", repo_root="/work/repo", title=sid,
            started_at=peers._iso_now(), heartbeat_at=peers._iso_now(),
            daemon_socket="/tmp/d.sock", clients=clients,
        )

    watched = Peered([peer("one", 1), peer("two", 2)])
    app = DoxaApp(cwd=str(tmp_path), engine_factory=lambda: watched,
                  new_session_factory=lambda: watched)
    async with app.run_test() as pilot:
        assert await _wait(
            pilot,
            lambda: "peers" in str(app.active_pane.query_one("#status-bar").renderable),
        )
        status = str(app.active_pane.query_one("#status-bar").renderable)
        assert "peers 2" in status
        assert "⌁)" not in status  # nothing detached: no suffix at all

    mixed = Peered([peer("one", 1), peer("two", 0), peer("three", 0)])
    app = DoxaApp(cwd=str(tmp_path), engine_factory=lambda: mixed,
                  new_session_factory=lambda: mixed)
    async with app.run_test() as pilot:
        assert await _wait(
            pilot,
            lambda: "peers" in str(app.active_pane.query_one("#status-bar").renderable),
        )
        status = str(app.active_pane.query_one("#status-bar").renderable)
        assert "peers 3 (2⌁)" in status


# -- /sessions ------------------------------------------------------------


@pytest.mark.asyncio
async def test_sessions_lists_attached_and_detached_distinctly(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    _entry("aaaa1111-0000", "mine", clients=1)
    _entry("bbbb2222-0000", "left-running", clients=0)

    engine = FakeEngine([])
    engine.session_id = "aaaa1111-0000"
    app = DoxaApp(cwd=str(tmp_path), engine_factory=lambda: engine,
                  new_session_factory=lambda: engine)
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.active_pane
        await pane._cmd_sessions("")
        await pilot.pause()
        text = [b.text for b in pane.query(SystemBlock) if "sessions" in b.text][-1]
        assert "aaaa1111" in text and "attached here" in text
        assert "bbbb2222" in text and "detached" in text
        assert "/sessions kill" in text


@pytest.mark.asyncio
async def test_sessions_kill_stops_the_matching_session(monkeypatch, tmp_path):
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    _entry("bbbb2222-0000", "left-running", clients=0)
    stopped: list[str] = []
    monkeypatch.setattr(
        "doxa.app._stop_session",
        lambda entry: bool(stopped.append(entry.session_id)) or True,
    )

    engine = FakeEngine([])
    app = DoxaApp(cwd=str(tmp_path), engine_factory=lambda: engine,
                  new_session_factory=lambda: engine)
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.active_pane
        await pane._cmd_sessions("kill bbbb")
        await pilot.pause()
        assert stopped == ["bbbb2222-0000"]
        text = [b.text for b in pane.query(SystemBlock) if "stopped:" in b.text][-1]
        assert "bbbb2222" in text

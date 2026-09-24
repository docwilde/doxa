# SPDX-License-Identifier: AGPL-3.0-only
"""The startup message spans registry discovery and the first UI frame."""

from __future__ import annotations

import os
import asyncio
import pty
import select
import subprocess
import sys
import threading

import pytest
from textual.widgets import Static

from doxa.app import CloseWithTurnRunning, DoxaApp, RestoreTabSpec, SystemBlock
from tests.fakes import FakeEngine


@pytest.mark.skipif(sys.platform != "linux", reason="PTY test needs Linux")
def test_cli_prints_loading_while_restore_resolution_is_blocked(tmp_path):
    """A real terminal receives the first line before discovery finishes."""
    master, slave = pty.openpty()
    probe = """
from doxa import cli, tabsets
tabsets.enabled = lambda: True
def resolve(_scope):
    print('RESOLVE_WAITING', flush=True)
    input()
    return tabsets.ResolvedRestore([], 0, None)
tabsets.resolve = resolve
cli._run_restored = lambda *_a, **_kw: None
raise SystemExit(cli.main([]))
"""
    env = dict(os.environ, DOXA_HOME=str(tmp_path / "doxa-home"),
               DOXA_RUNTIME_DIR=str(tmp_path / "runtime"))
    try:
        child = subprocess.Popen(
            [sys.executable, "-c", probe], cwd=tmp_path, env=env,
            stdin=subprocess.PIPE, stdout=slave, stderr=slave,
        )
    finally:
        os.close(slave)
    try:
        observed = b""
        while b"RESOLVE_WAITING" not in observed:
            ready, _, _ = select.select([master], [], [], 5)
            assert ready, f"startup output stalled: {observed!r}"
            observed += os.read(master, 4096)
        assert observed.index(b"Loading") < observed.index(b"RESOLVE_WAITING")
        assert child.poll() is None  # still held in the restore stub
        assert child.stdin is not None
        child.stdin.write(b"continue\n")
        child.stdin.flush()
        assert child.wait(timeout=5) == 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        os.close(master)


@pytest.mark.asyncio
@pytest.mark.parametrize("restoring", [False, True])
async def test_startup_cover_waits_for_pane_boot(monkeypatch, tmp_path, restoring):
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "doxa-home"))
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "runtime"))
    gate = threading.Event()

    def engine():
        gate.wait(timeout=5)
        return FakeEngine([], cwd=str(tmp_path))

    options = (
        {"restore_tabs": [RestoreTabSpec("saved-id", engine, cwd=str(tmp_path))]}
        if restoring else {"engine_factory": engine}
    )
    app = DoxaApp(cwd=str(tmp_path), **options)
    try:
        async with app.run_test() as pilot:
            status = app.query_one("#startup-status", Static)
            assert status.display
            assert status.renderable == (
                "Restoring session…" if restoring else "Loading…"
            )
            gate.set()
            for _ in range(50):
                await pilot.pause(0.02)
                if not status.display:
                    break
            assert not status.display
    finally:
        gate.set()


@pytest.mark.asyncio
async def test_restore_cover_waits_for_every_opening_pane(monkeypatch, tmp_path):
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "doxa-home"))
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "runtime"))
    gates = [threading.Event(), threading.Event()]

    def engine(index):
        gates[index].wait(timeout=5)
        return FakeEngine([], cwd=str(tmp_path))

    app = DoxaApp(
        cwd=str(tmp_path),
        restore_tabs=[
            RestoreTabSpec(
                f"saved-{i}", lambda i=i: engine(i), cwd=str(tmp_path)
            )
            for i in range(2)
        ],
    )
    try:
        async with app.run_test() as pilot:
            status = app.query_one("#startup-status", Static)
            gates[0].set()
            for _ in range(50):
                await pilot.pause(0.02)
                if app._startup_pending == 1:
                    break
            assert app._startup_pending == 1
            assert status.display
            gates[1].set()
            for _ in range(50):
                await pilot.pause(0.02)
                if not status.display:
                    break
            assert not status.display
    finally:
        for gate in gates:
            gate.set()


@pytest.mark.asyncio
async def test_archived_restore_waits_for_its_fresh_fallback(monkeypatch, tmp_path):
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "doxa-home"))
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "runtime"))
    gate = threading.Event()

    def engine():
        gate.wait(timeout=5)
        return FakeEngine([], cwd=str(tmp_path))

    app = DoxaApp(
        cwd=str(tmp_path), engine_factory=engine,
        restore_tabs=[RestoreTabSpec("old-id", archived=True, cwd=str(tmp_path))],
    )
    try:
        async with app.run_test() as pilot:
            status = app.query_one("#startup-status", Static)
            assert status.display
            assert app._startup_pending == 1
            gate.set()
            for _ in range(50):
                await pilot.pause(0.02)
                if not status.display:
                    break
            assert not status.display
    finally:
        gate.set()


@pytest.mark.asyncio
async def test_cover_blocks_hidden_shell_input_and_later_pane_cannot_clear_it(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "doxa-home"))
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "runtime"))
    gate = threading.Event()

    def slow_engine():
        gate.wait(timeout=5)
        return FakeEngine([], cwd=str(tmp_path))

    app = DoxaApp(
        cwd=str(tmp_path), engine_factory=slow_engine,
        new_session_factory=lambda: FakeEngine([], cwd=str(tmp_path)),
    )
    marker = tmp_path / "hidden-command-ran"
    try:
        async with app.run_test() as pilot:
            status = app.query_one("#startup-status", Static)
            prompt = app.query_one("#prompt-input")
            assert status.display
            assert not await pilot.click("#prompt-input")
            await pilot.press("!", "t", "enter")
            assert prompt.value == ""
            # Even a prefilled prompt cannot be submitted through the cover.
            prompt.value = f"!touch {marker}"
            await pilot.press("enter")
            assert not marker.exists()
            assert status.display

            # Programmatic new tabs can still mount during startup. Their
            # faster boot must not dismiss the original pane's cover.
            await app.action_new_tab()
            for _ in range(50):
                await pilot.pause(0.02)
                if len(app.panes()) == 2 and app.panes()[-1].engine is not None:
                    break
            assert len(app.panes()) == 2
            assert status.display

            gate.set()
            for _ in range(50):
                await pilot.pause(0.02)
                if not status.display:
                    break
            assert not status.display
            assert not marker.exists()
    finally:
        gate.set()


@pytest.mark.asyncio
async def test_visible_modal_accepts_enter_while_pane_is_still_loading(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "doxa-home"))
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "runtime"))
    gate = threading.Event()

    def engine():
        gate.wait(timeout=5)
        return FakeEngine([], cwd=str(tmp_path))

    app = DoxaApp(cwd=str(tmp_path), engine_factory=engine)
    try:
        async with app.run_test() as pilot:
            status = app.query_one("#startup-status", Static)
            assert status.display
            chosen = []
            app.push_screen(CloseWithTurnRunning(), callback=chosen.append)
            for _ in range(50):
                await pilot.pause(0.02)
                if isinstance(app.screen, CloseWithTurnRunning):
                    break
            assert isinstance(app.screen, CloseWithTurnRunning)
            await pilot.press("enter")
            for _ in range(50):
                await pilot.pause(0.02)
                if chosen:
                    break
            assert chosen == ["terminate"]
            assert status.display  # the pane has not booted behind the modal
    finally:
        gate.set()


@pytest.mark.asyncio
async def test_failed_first_connection_reveals_error(monkeypatch, tmp_path):
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "doxa-home"))
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "runtime"))
    gate = threading.Event()

    def broken_engine():
        gate.wait(timeout=5)
        raise RuntimeError("connection refused")

    app = DoxaApp(cwd=str(tmp_path), engine_factory=broken_engine)
    try:
        async with app.run_test() as pilot:
            status = app.query_one("#startup-status", Static)
            assert status.display
            gate.set()
            for _ in range(50):
                await pilot.pause(0.02)
                if not status.display:
                    break
            assert not status.display
            assert any(
                "connection refused" in str(block.renderable)
                for block in app.query(SystemBlock)
            )
    finally:
        gate.set()


@pytest.mark.asyncio
async def test_failed_engine_start_reveals_error_after_cover(monkeypatch, tmp_path):
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "doxa-home"))
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "runtime"))
    gate = threading.Event()

    class FailingEngine(FakeEngine):
        async def start(self):
            await asyncio.to_thread(gate.wait, 5)
            raise RuntimeError("engine start refused")

    app = DoxaApp(
        cwd=str(tmp_path),
        engine_factory=lambda: FailingEngine([], cwd=str(tmp_path)),
    )
    try:
        async with app.run_test() as pilot:
            status = app.query_one("#startup-status", Static)
            assert status.display
            gate.set()
            for _ in range(50):
                await pilot.pause(0.02)
                if not status.display:
                    break
            assert not status.display
            assert any(
                "engine start refused" in str(block.renderable)
                for block in app.query(SystemBlock)
            )
    finally:
        gate.set()

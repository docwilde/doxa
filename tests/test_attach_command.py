# SPDX-License-Identifier: AGPL-3.0-only
"""``/attach`` (v0.60.0): the door back in that ``/detach`` never had a
counterpart for.

``/sessions`` could only list and kill; the one way to REATTACH a live
detached session from inside the app was the sessions status chip's own
picker (item 2), which switches the ACTIVE pane's engine -- correct for
that surface, but not what a v0.45.0-style command should do (see
``/resume``'s own settled rule: a pane holds a live conversation, and
``/clear`` is the verb for replacing one). ``/attach`` reuses
``DoxaApp._attach_in_new_tab`` instead -- the same door ``/resume`` already
sends a still-running session through -- so this is a second WAY to reach
the one attach primitive, never a second primitive.

Same monkeypatch pattern tests/test_resume.py's own running-session tests
use: ``peers.read_registry`` returns synthetic ``PeerInfo`` rows (no real
socket needed -- neither ``list_daemons`` nor ``_attach_in_new_tab`` probes)
and ``doxa.client.EngineClient`` is swapped for a plain ``FakeEngine`` so
attaching never dials a real daemon.
"""

from __future__ import annotations

import os

import pytest

from doxa import commands as commands_mod
from doxa import peers as peers_mod
from doxa.app import DoxaApp
from doxa.ui.dialogs import ChipPicker
from doxa.ui.labels import help_text
from tests.fakes import FakeEngine


def _peer(sid: str, title: str, cwd: str, daemon_socket: str = "d.sock") -> "peers_mod.PeerInfo":
    return peers_mod.PeerInfo(
        session_id=sid, pid=os.getpid(), socket_path="",
        cwd=cwd, repo_root=None, title=title,
        started_at="", heartbeat_at="", daemon_socket=daemon_socket,
    )


async def _app(monkeypatch, tmp_path):
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    engine = FakeEngine([])
    engine.session_id = "sid-here"
    app = DoxaApp(cwd=str(tmp_path), engine_factory=lambda: engine,
                  new_session_factory=lambda: engine)
    return app


def _attached_fake(monkeypatch):
    """Stand in for the real socket connect ``_attach_in_new_tab`` would
    otherwise make -- same substitution test_resume.py's own running-
    session tests use."""
    monkeypatch.setattr(
        "doxa.client.EngineClient", lambda sock, **kw: FakeEngine([]),
    )


async def _wait(pilot, cond, tries=200):
    for _ in range(tries):
        if cond():
            return True
        await pilot.pause(0.02)
    return cond()


# -- registry closure: one row, every surface -----------------------------


def test_attach_reaches_help_the_palette_and_autocomplete():
    assert "/attach" in commands_mod.interactive_names()
    row = commands_mod.lookup("/attach")
    assert row is not None and row.palette  # offered on Ctrl+P
    assert "/attach" in help_text()
    assert any(c.name == "/attach" for c in commands_mod.matches("/att"))


# -- with a prefix: always a NEW tab ---------------------------------------


@pytest.mark.asyncio
async def test_attach_with_a_prefix_opens_a_new_tab(monkeypatch, tmp_path):
    _attached_fake(monkeypatch)
    entry = _peer("bbbb2222dead", "left-running", str(tmp_path))
    monkeypatch.setattr(peers_mod, "read_registry", lambda *a, **k: [entry])
    app = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        before = len(app.panes())
        pane = app.active_pane
        await pane._cmd_attach("bbbb")
        assert await _wait(pilot, lambda: len(app.panes()) == before + 1)
        # The original tab is untouched -- attach never takes over the
        # pane it was typed in.
        assert app.panes()[0].engine.session_id == "sid-here"


@pytest.mark.asyncio
async def test_attach_no_prefix_single_detached_attaches_outright(
    monkeypatch, tmp_path
):
    _attached_fake(monkeypatch)
    entry = _peer("bbbb2222dead", "left-running", str(tmp_path))
    monkeypatch.setattr(peers_mod, "read_registry", lambda *a, **k: [entry])
    app = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        before = len(app.panes())
        pane = app.active_pane
        await pane._cmd_attach("")
        assert await _wait(pilot, lambda: len(app.panes()) == before + 1)


@pytest.mark.asyncio
async def test_attach_no_prefix_none_detached_refuses(monkeypatch, tmp_path):
    monkeypatch.setattr(peers_mod, "read_registry", lambda *a, **k: [])
    app = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        before = len(app.panes())
        pane = app.active_pane
        await pane._cmd_attach("")
        await pilot.pause()
        from doxa.app import SystemBlock

        text = [b.text for b in pane.query(SystemBlock) if "attach" in b.text][-1]
        assert "nothing detached" in text
        assert len(app.panes()) == before


# -- with several candidates: the SAME picker the sessions chip uses ------


@pytest.mark.asyncio
async def test_attach_no_prefix_several_opens_the_chip_picker(monkeypatch, tmp_path):
    entries = [
        _peer("bbbb2222dead", "one-detached", str(tmp_path)),
        _peer("cccc3333dead", "two-detached", str(tmp_path)),
    ]
    monkeypatch.setattr(peers_mod, "read_registry", lambda *a, **k: entries)
    app = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.active_pane
        await pane._cmd_attach("")
        await pilot.pause()
        picker = app.query_one("#chip-picker", ChipPicker)
        assert picker.is_open
        assert picker.border_title == "attach"
        rids = {rid for rid, _label in picker._rows if rid}
        assert rids == {"bbbb2222dead", "cccc3333dead"}


@pytest.mark.asyncio
async def test_attach_picker_selection_attaches_in_a_new_tab(monkeypatch, tmp_path):
    _attached_fake(monkeypatch)
    entries = [
        _peer("bbbb2222dead", "one-detached", str(tmp_path)),
        _peer("cccc3333dead", "two-detached", str(tmp_path)),
    ]
    monkeypatch.setattr(peers_mod, "read_registry", lambda *a, **k: entries)
    app = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        before = len(app.panes())
        pane = app.active_pane
        await pane._cmd_attach("")
        await pilot.pause()
        picker = app.query_one("#chip-picker", ChipPicker)
        index = next(
            i for i, (rid, _l) in enumerate(picker._rows)
            if rid == "cccc3333dead"
        )
        picker.select_row(index)
        assert await _wait(pilot, lambda: len(app.panes()) == before + 1)


# -- refusals name what they found -----------------------------------------


@pytest.mark.asyncio
async def test_attach_unknown_prefix_names_nothing_found(monkeypatch, tmp_path):
    entry = _peer("bbbb2222dead", "left-running", str(tmp_path))
    monkeypatch.setattr(peers_mod, "read_registry", lambda *a, **k: [entry])
    app = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        before = len(app.panes())
        pane = app.active_pane
        await pane._cmd_attach("zzzz")
        await pilot.pause()
        from doxa.app import SystemBlock

        text = [b.text for b in pane.query(SystemBlock) if "attach" in b.text][-1]
        assert "no live session matches" in text and "zzzz" in text
        assert len(app.panes()) == before


@pytest.mark.asyncio
async def test_attach_ambiguous_prefix_lists_the_candidates(monkeypatch, tmp_path):
    entries = [
        _peer("bbbb1111dead", "one", str(tmp_path)),
        _peer("bbbb2222dead", "two", str(tmp_path)),
    ]
    monkeypatch.setattr(peers_mod, "read_registry", lambda *a, **k: entries)
    app = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        before = len(app.panes())
        pane = app.active_pane
        await pane._cmd_attach("bbbb")
        await pilot.pause()
        from doxa.app import SystemBlock

        text = [b.text for b in pane.query(SystemBlock) if "attach" in b.text][-1]
        assert "matches 2 live sessions" in text
        assert "bbbb1111" in text and "bbbb2222" in text
        assert len(app.panes()) == before  # refused, never guessed


# -- already open HERE: switch, never a second attach ----------------------


@pytest.mark.asyncio
async def test_attach_a_session_already_open_here_switches_tabs(
    monkeypatch, tmp_path
):
    """One daemon, one client, per window -- the same exclusion the
    palette's Attach section and the sessions chip's own picker make."""
    entry = _peer("sid-here", "this one", str(tmp_path))
    monkeypatch.setattr(peers_mod, "read_registry", lambda *a, **k: [entry])
    app = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        before = len(app.panes())
        switched = []
        monkeypatch.setattr(
            app, "_switch_to_tab", lambda pane_id: switched.append(pane_id)
        )
        pane = app.active_pane
        await pane._cmd_attach("sid-here")
        await pilot.pause()
        assert switched == [pane.id]
        assert len(app.panes()) == before  # no new tab opened

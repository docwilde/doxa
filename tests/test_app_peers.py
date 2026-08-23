"""TUI peer-layer tests, headless via Textual's Pilot (same pattern as
tests/test_app.py): the peers status chip, /peers and /msg command flows
against a scripted FakeEngine, and one end-to-end test with a REAL
SessionEngine (fake SDK client, tmp registry) proving a socket-delivered
peer message renders scrubbed in its own PeerMessageBlock.
"""

from __future__ import annotations

import os

import pytest

from claude_agent_sdk import ResultMessage

from doxa import peers
from doxa.app import DoxaApp, PeerMessageBlock, SystemBlock
from doxa.engine import EngineEvent, SessionEngine
from tests.fakes import FakeEngine, factory_with_script

FAKE_AWS_KEY = "AKIAABCDEFGHIJKLMNOP"


def _peer(sid: str, title: str, cwd: str = "/work/repo") -> peers.PeerInfo:
    now = peers._iso_now()
    return peers.PeerInfo(
        session_id=sid, pid=os.getpid(), socket_path=f"/tmp/peer-{sid}.sock",
        cwd=cwd, repo_root="/work/repo", title=title,
        started_at=now, heartbeat_at=now,
    )


TWO_PEERS = [_peer("aaaa1111-0000", "alpha"), _peer("bbbb2222-0000", "beta")]


async def _wait_blocks(app, pilot, block_type, n=1):
    for _ in range(100):
        blocks = list(app.query(block_type))
        if len(blocks) >= n:
            return blocks
        await pilot.pause(0.02)
    return list(app.query(block_type))


@pytest.mark.asyncio
async def test_status_bar_peers_chip_counts_same_repo_peers(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "doxa.app.SessionEngine",
        lambda cwd, model=None: FakeEngine([], peers=TWO_PEERS),
    )
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "peers 2" in str(app.query_one("#status-bar").renderable)


@pytest.mark.asyncio
async def test_peers_chip_hidden_at_zero(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "doxa.app.SessionEngine",
        lambda cwd, model=None: FakeEngine([]),
    )
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "peers" not in str(app.query_one("#status-bar").renderable)


@pytest.mark.asyncio
async def test_peers_command_lists_live_peers(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "doxa.app.SessionEngine",
        lambda cwd, model=None: FakeEngine([], peers=TWO_PEERS),
    )
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#prompt-input").value = "/peers"
        await pilot.press("enter")
        blocks = await _wait_blocks(app, pilot, SystemBlock)
        assert len(blocks) == 1
        text = blocks[0].text
        assert "alpha" in text and "aaaa1111" in text
        assert "beta" in text and "bbbb2222" in text
        assert "/work/repo" in text
        # A slash command is not a turn: nothing was sent to the model.
        assert not app.query("TurnBlock")


@pytest.mark.asyncio
async def test_msg_command_sends_by_prefix(monkeypatch, tmp_path):
    engine = FakeEngine([], peers=TWO_PEERS)
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: engine)
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#prompt-input").value = "/msg alp hello over there"
        await pilot.press("enter")
        blocks = await _wait_blocks(app, pilot, SystemBlock)
        assert engine.sent_peer_messages == [("aaaa1111-0000", "hello over there")]
        assert "sent to alpha (aaaa1111)" in blocks[0].text


@pytest.mark.asyncio
async def test_msg_command_ambiguous_prefix_lists_matches(monkeypatch, tmp_path):
    ambiguous = [_peer("aaaa1111-0000", "alpha"), _peer("aabb3333-0000", "alps")]
    engine = FakeEngine([], peers=ambiguous)
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: engine)
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#prompt-input").value = "/msg al ping"
        await pilot.press("enter")
        blocks = await _wait_blocks(app, pilot, SystemBlock)
        text = blocks[0].text
        assert "msg error" in text and "ambiguous" in text
        assert "alpha (aaaa1111)" in text and "alps (aabb3333)" in text
        assert engine.sent_peer_messages == []

        # And bad usage gets usage help, not a crash.
        app.query_one("#prompt-input").value = "/msg onlyprefix"
        await pilot.press("enter")
        blocks = await _wait_blocks(app, pilot, SystemBlock, n=2)
        assert "usage: /msg" in blocks[1].text


@pytest.mark.asyncio
async def test_incoming_peer_message_renders_distinct_scrubbed_block(monkeypatch, tmp_path):
    """End to end with a REAL engine (fake SDK client only): a frame sent
    over the real tmp socket arrives scrubbed and renders as its own
    PeerMessageBlock, without starting a turn."""
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    factory, _created = factory_with_script([
        ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
            num_turns=1, session_id="s", total_cost_usd=0.0,
        ),
    ])
    monkeypatch.setattr(
        "doxa.app.SessionEngine",
        lambda cwd, model=None: SessionEngine(cwd=cwd, model=model, client_factory=factory),
    )
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        for _ in range(100):
            if app.engine is not None and getattr(app.engine, "peer_host", None):
                break
            await pilot.pause(0.02)
        assert app.engine.peer_host is not None

        await peers.send_message(
            app.engine.peer_host.socket_path,
            from_id="ffff9999-0000", from_title="scout",
            body=f"the token {FAKE_AWS_KEY} is in the log",
        )
        blocks = await _wait_blocks(app, pilot, PeerMessageBlock)
        assert len(blocks) == 1
        rendered = str(blocks[0].renderable)
        assert "scout" in rendered and "ffff9999" in rendered
        assert FAKE_AWS_KEY not in rendered
        assert "[REDACTED" in rendered
        # Display only -- no turn started, no model call made.
        assert not app.query("TurnBlock")
        await app.action_quit()


@pytest.mark.asyncio
async def test_peer_joined_event_updates_status_chip(monkeypatch, tmp_path):
    engine = FakeEngine([])
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: engine)
    app = DoxaApp(cwd=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "peers" not in str(app.query_one("#status-bar").renderable)
        engine._peers = [_peer("cccc4444-0000", "gamma")]
        engine.push_peer_event(EngineEvent("peer_joined", {
            "session_id": "cccc4444-0000", "title": "gamma", "cwd": "/work/repo",
        }))
        for _ in range(100):
            if "peers 1" in str(app.query_one("#status-bar").renderable):
                break
            await pilot.pause(0.02)
        assert "peers 1" in str(app.query_one("#status-bar").renderable)

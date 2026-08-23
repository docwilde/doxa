"""Daemon-split tests: a real SessionDaemon hosting a real SessionEngine
over a FakeClient (no subprocess, no network, no `claude` CLI), talked to by
real EngineClients over a real Unix socket in a tmp DOXA_RUNTIME_DIR.

Covers the Phase 2 headline contracts: the socket protocol round-trip (a
version-stamped hello, a prompt whose typed events stream back in order),
replay-from-cursor after a simulated detach, finalize-after-linger once the
LAST client detaches (and its cancellation on reattach), explicit stop, and
the shared-registry daemon marker `doxa attach` discovers sessions by.
"""

from __future__ import annotations

import asyncio
import contextlib
import json

import pytest

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from doxa import __version__, peers
from doxa.client import EngineClient, EngineClientError
from doxa.daemon import PROTOCOL_VERSION, EventRing, SessionDaemon
from doxa.engine import SessionEngine
from tests.fakes import factory_with_script

TURN_SCRIPT = [
    StreamEvent(
        uuid="stream-1", session_id="s",
        event={"type": "content_block_delta",
               "delta": {"type": "text_delta", "text": "Hello"}},
    ),
    AssistantMessage(content=[TextBlock(text="Hello")], model="claude-haiku-4-5"),
    AssistantMessage(
        content=[ToolUseBlock(id="tool-1", name="calculator_add",
                              input={"a": 1, "b": 2})],
        model="claude-haiku-4-5",
    ),
    UserMessage(content=[ToolResultBlock(tool_use_id="tool-1", content="3",
                                         is_error=False)]),
    ResultMessage(
        subtype="success", duration_ms=42, duration_api_ms=40, is_error=False,
        num_turns=1, session_id="s", total_cost_usd=0.001,
    ),
]

EXPECTED_TURN_TYPES = [
    "turn_started", "text_delta", "tool_call", "tool_result", "turn_done",
]


@contextlib.asynccontextmanager
async def running_daemon(tmp_path, monkeypatch, linger=30.0, script=None):
    """A served SessionDaemon over a FakeClient in an isolated runtime dir.
    Yields (daemon, created) where created[0] is the FakeClient."""
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    factory, created = factory_with_script(list(script or TURN_SCRIPT))
    daemon = SessionDaemon(
        cwd=str(tmp_path),
        linger_secs=linger,
        engine_factory=lambda cwd, sid, dsock: SessionEngine(
            cwd=cwd, session_id=sid, client_factory=factory,
            daemon_socket=dsock,
        ),
    )
    serve_task = asyncio.create_task(daemon.serve())
    await asyncio.wait_for(daemon.ready.wait(), 10)
    try:
        yield daemon, created, serve_task
    finally:
        if not serve_task.done():
            with contextlib.suppress(Exception):
                await daemon._shutdown("test teardown")
                await asyncio.wait_for(serve_task, 5)


async def _drain_oob(client: EngineClient, until_type: str, timeout=5.0):
    """Collect out-of-band events until (and including) `until_type`."""
    events = []
    agen = client.peer_events()
    async def collect():
        async for ev in agen:
            events.append(ev)
            if ev.type == until_type:
                return
    await asyncio.wait_for(collect(), timeout)
    await agen.aclose()
    return events


@pytest.mark.asyncio
async def test_protocol_round_trip(tmp_path, monkeypatch):
    """Hello is version-stamped; a prompt streams the same typed events in
    the same order the in-process engine yields them."""
    async with running_daemon(tmp_path, monkeypatch) as (daemon, created, _):
        client = EngineClient(str(daemon.socket_path))
        started = await client.start()
        assert started.type == "session_started"
        assert client.session_id == daemon.session_id

        events = [ev async for ev in client.send("what is 1+2?")]
        assert [e.type for e in events] == EXPECTED_TURN_TYPES
        tool_call = next(e for e in events if e.type == "tool_call")
        assert tool_call.data["name"] == "calculator_add"
        assert tool_call.data["input"] == {"a": 1, "b": 2}
        turn_done = next(e for e in events if e.type == "turn_done")
        assert turn_done.data["cost_usd"] == pytest.approx(0.001)
        # The prompt reached the engine's real SDK-client seam.
        assert created[0].queried == [("what is 1+2?", daemon.session_id)]
        # Status cache refreshed after the turn.
        assert client.total_cost_usd == pytest.approx(0.001)
        await client.finalize()


@pytest.mark.asyncio
async def test_hello_frame_is_version_stamped(tmp_path, monkeypatch):
    """Raw-socket check of the hello frame itself -- the one frame a client
    of ANY future version must be able to parse to know it should back off."""
    async with running_daemon(tmp_path, monkeypatch) as (daemon, _, _):
        reader, writer = await asyncio.open_unix_connection(str(daemon.socket_path))
        hello = json.loads(await asyncio.wait_for(reader.readline(), 5))
        assert hello["type"] == "hello"
        assert hello["proto"] == PROTOCOL_VERSION
        assert hello["doxa"] == __version__
        assert hello["session_id"] == daemon.session_id
        assert isinstance(hello["next_seq"], int)
        writer.close()


@pytest.mark.asyncio
async def test_replay_from_cursor_after_detach(tmp_path, monkeypatch):
    """Detach after a turn, reattach: cursor=None replays the whole ring;
    a mid-stream cursor replays only what that client has not yet seen."""
    async with running_daemon(tmp_path, monkeypatch) as (daemon, _, _):
        first = EngineClient(str(daemon.socket_path))
        await first.start()
        turn_events = [ev async for ev in first.send("run it")]
        assert [e.type for e in turn_events] == EXPECTED_TURN_TYPES
        cursor_after_turn = first.cursor
        await first.finalize()  # detach -- daemon keeps running

        # Fresh reattach, no cursor: the full ring replays as out-of-band
        # events (they belong to a finished turn, not to a live local one).
        again = EngineClient(str(daemon.socket_path))
        await again.start()
        replayed = await _drain_oob(again, "turn_done")
        assert [e.type for e in replayed] == EXPECTED_TURN_TYPES
        assert replayed[1].data["text"] == "Hello"
        assert again.cursor == cursor_after_turn
        await again.finalize()

        # Reattach from a mid-turn cursor: only the tail replays.
        partial = EngineClient(str(daemon.socket_path), cursor=cursor_after_turn - 2)
        await partial.start()
        tail = await _drain_oob(partial, "turn_done")
        assert [e.type for e in tail] == EXPECTED_TURN_TYPES[-2:]
        await partial.finalize()


@pytest.mark.asyncio
async def test_last_detach_finalizes_after_linger(tmp_path, monkeypatch):
    """The daemon finalizes (engine review+index path, SDK client exited)
    only after the LAST client detaches AND the linger window passes."""
    async with running_daemon(tmp_path, monkeypatch, linger=0.05) as (
        daemon, created, serve_task,
    ):
        client = EngineClient(str(daemon.socket_path))
        await client.start()
        # Attached: linger must not fire while a client is connected.
        await asyncio.sleep(0.15)
        assert not serve_task.done()
        assert created[0].exited is False

        await client.finalize()  # last client detaches
        await asyncio.wait_for(serve_task, 5)
        assert created[0].exited is True  # engine finalized: client closed
        assert not daemon.socket_path.exists()
        # Presence entry removed with the engine's PeerHost.
        assert peers.read_registry(reap=False) == []


@pytest.mark.asyncio
async def test_reattach_within_linger_cancels_finalize(tmp_path, monkeypatch):
    async with running_daemon(tmp_path, monkeypatch, linger=0.3) as (
        daemon, created, serve_task,
    ):
        first = EngineClient(str(daemon.socket_path))
        await first.start()
        await first.finalize()
        # Reattach well inside the linger window...
        second = EngineClient(str(daemon.socket_path))
        await second.start()
        await asyncio.sleep(0.5)
        # ...and the daemon must still be alive past the original deadline.
        assert not serve_task.done()
        assert created[0].exited is False
        await second.finalize()


@pytest.mark.asyncio
async def test_explicit_stop_finalizes_immediately(tmp_path, monkeypatch):
    async with running_daemon(tmp_path, monkeypatch, linger=600.0) as (
        daemon, created, serve_task,
    ):
        client = EngineClient(str(daemon.socket_path))
        await client.start()
        done = await client.stop()
        assert done.data.get("stopped") is True
        await asyncio.wait_for(serve_task, 5)
        assert created[0].exited is True


@pytest.mark.asyncio
async def test_registry_entry_carries_daemon_marker(tmp_path, monkeypatch):
    """One discovery surface: the peer registry entry doubles as the attach
    surface via the daemon_socket field, and list_daemons finds it."""
    async with running_daemon(tmp_path, monkeypatch) as (daemon, _, _):
        entries = peers.read_registry(reap=False)
        assert len(entries) == 1
        assert entries[0].session_id == daemon.session_id
        assert entries[0].daemon_socket == str(daemon.socket_path)

        found = peers.list_daemons()
        assert [p.session_id for p in found] == [daemon.session_id]
        # And the daemon's own peer view never lists itself.
        assert peers.list_daemons(self_id=daemon.session_id) == []


@pytest.mark.asyncio
async def test_second_prompt_while_turn_runs_is_refused(tmp_path, monkeypatch):
    """One turn at a time, daemon-enforced: the second client gets a
    graceful refusal (surfaced as an ordinary error), never interleaved
    events."""
    gate = asyncio.Event()

    class SlowScriptClient:
        def __init__(self, options):
            self.options = options
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def query(self, prompt, session_id="default"):
            pass
        async def receive_response(self):
            await gate.wait()
            yield ResultMessage(
                subtype="success", duration_ms=1, duration_api_ms=1,
                is_error=False, num_turns=1, session_id="s", total_cost_usd=0.0,
            )

    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    daemon = SessionDaemon(
        cwd=str(tmp_path), linger_secs=30.0,
        engine_factory=lambda cwd, sid, dsock: SessionEngine(
            cwd=cwd, session_id=sid, client_factory=SlowScriptClient,
            daemon_socket=dsock,
        ),
    )
    serve_task = asyncio.create_task(daemon.serve())
    await asyncio.wait_for(daemon.ready.wait(), 10)
    try:
        a = EngineClient(str(daemon.socket_path))
        b = EngineClient(str(daemon.socket_path))
        await a.start()
        await b.start()

        async def run_a():
            return [ev async for ev in a.send("slow one")]

        task_a = asyncio.create_task(run_a())
        # Wait until the slow turn is actually registered daemon-side.
        for _ in range(100):
            if daemon._turn_task is not None:
                break
            await asyncio.sleep(0.01)

        with pytest.raises(EngineClientError, match="already running"):
            async for _ in b.send("me too"):
                pass

        gate.set()
        events_a = await asyncio.wait_for(task_a, 5)
        assert events_a[-1].type == "turn_done"
        await a.finalize()
        await b.finalize()
    finally:
        if not serve_task.done():
            with contextlib.suppress(Exception):
                await daemon._shutdown("test teardown")
                await asyncio.wait_for(serve_task, 5)


@pytest.mark.asyncio
async def test_sigint_finalizes_gracefully(tmp_path, monkeypatch):
    """Ctrl+C aimed at the daemon process (SIGINT) runs the same graceful
    finalize as SIGTERM: engine finalized (SDK client exited), socket and
    presence entry gone -- the review gate is never skipped by impatience."""
    import os
    import signal as signal_mod

    from doxa.daemon import install_signal_handlers

    async with running_daemon(tmp_path, monkeypatch) as (daemon, created, serve_task):
        loop = asyncio.get_running_loop()
        install_signal_handlers(daemon, loop)
        try:
            os.kill(os.getpid(), signal_mod.SIGINT)
            await asyncio.wait_for(serve_task, 5)
        finally:
            for sig in (signal_mod.SIGTERM, signal_mod.SIGINT):
                loop.remove_signal_handler(sig)
        assert created[0].exited is True  # engine finalized
        assert not daemon.socket_path.exists()
        assert peers.read_registry(reap=False) == []


def test_event_ring_bounds_and_cursors():
    from doxa.engine import EngineEvent

    ring = EventRing(capacity=4)
    for i in range(6):
        ring.append(None, EngineEvent("text_delta", {"i": i}))
    assert ring.next_seq == 6
    # Bounded: the two oldest fell off; replay-all returns what remains.
    assert [f["seq"] for f in ring.since(None)] == [2, 3, 4, 5]
    assert [f["seq"] for f in ring.since(4)] == [4, 5]
    assert ring.since(99) == []

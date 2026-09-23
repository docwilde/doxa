# SPDX-License-Identifier: AGPL-3.0-only
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
import subprocess
import time
from pathlib import Path

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

from claude_agent_sdk import ToolPermissionContext

from doxa import __version__, peers
from doxa import config as config_mod
from doxa import daemon as daemon_mod
from doxa import worktrees as worktrees_mod
from doxa.client import EngineClient, EngineClientError
from doxa.daemon import PROTOCOL_VERSION, EventRing, SessionDaemon
from doxa.engine import EngineEvent, SessionEngine
from tests.fakes import FakeClient, factory_with_script

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
async def running_daemon(tmp_path, monkeypatch, linger=30.0, script=None,
                         server_info=None, ctx_usage=None):
    """A served SessionDaemon over a FakeClient in an isolated runtime dir.
    Yields (daemon, created) where created[0] is the FakeClient."""
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    factory, created = factory_with_script(
        list(script or TURN_SCRIPT), server_info=server_info,
        ctx_usage=ctx_usage,
    )
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
async def test_client_effort_is_daemon_sessions_connect_time_choice(tmp_path, monkeypatch):
    monkeypatch.setenv("DOXA_EFFORT", "high")
    async with running_daemon(tmp_path, monkeypatch) as (daemon, _, _):
        reader, writer = await asyncio.open_unix_connection(str(daemon.socket_path))
        hello = json.loads(await asyncio.wait_for(reader.readline(), 5))
        assert hello["effort"] == "high"
        writer.close()
        await writer.wait_closed()

        client = EngineClient(str(daemon.socket_path))
        await client.start()
        assert client.effort == "high"
        monkeypatch.setenv("DOXA_EFFORT", "low")
        status = await client.refresh_status()
        assert status["effort"] == "high"
        assert client.effort == "high"
        await client.finalize()


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
async def test_cancelling_the_linger_task_before_the_sleep_expires_still_cancels_cleanly(
    tmp_path, monkeypatch,
):
    """The ordinary case _cancel_linger exists for -- a reattach well
    before _linger_then_stop's sleep ever returns -- keeps working exactly
    as it did before the shutdown-stranding fix: the task ends on the
    plain CancelledError its own `except asyncio.CancelledError: return`
    already expects, _stopping is untouched, and the daemon keeps
    running."""
    async with running_daemon(tmp_path, monkeypatch, linger=30.0) as (
        daemon, created, serve_task,
    ):
        task = daemon._linger_task  # armed by serve() itself, nobody attached
        assert task is not None
        daemon._cancel_linger()
        assert daemon._linger_task is None
        # _linger_then_stop's own `except asyncio.CancelledError: return`
        # swallows the cancellation and returns normally -- the task ends
        # up done, not cancelled, and that is the existing contract this
        # test guards, not a side effect of the shutdown-stranding fix.
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert task.done()
        assert not task.cancelled()
        assert daemon._stopping is False
        assert not daemon._done.is_set()
        assert not serve_task.done()
        assert created[0].exited is False


@pytest.mark.asyncio
async def test_stopping_never_remains_true_with_done_unset_on_the_exception_path(
    tmp_path, monkeypatch,
):
    """Even a genuinely unexpected exception out of engine.finalize() --
    including a bare CancelledError, which the surrounding
    ``suppress(Exception)`` does NOT catch since it is a BaseException --
    must not leave ``_stopping`` True forever with ``_done`` unset. That
    exact combination is a stranded daemon: the re-entry guard refuses
    every later shutdown attempt, and serve()'s ``await
    self._done.wait()`` would never return."""
    async with running_daemon(tmp_path, monkeypatch, linger=30.0) as (
        daemon, created, serve_task,
    ):
        async def raising_finalize():
            raise asyncio.CancelledError("simulated external cancellation")

        daemon.engine.finalize = raising_finalize

        with pytest.raises(asyncio.CancelledError, match="simulated"):
            await daemon._shutdown("exception path test")

        assert daemon._stopping is True
        assert daemon._done.is_set()
        # _done is shared with serve()'s own wait -- it unwinds on its own.
        await asyncio.wait_for(serve_task, 5)


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
async def test_explicit_stop_waits_for_a_slow_but_clean_finalize(
    tmp_path, monkeypatch,
):
    """Issue #58's mechanism, isolated at the layer that owns it.
    ``engine.finalize()`` stands in for a LORE-enabled session's slow
    review/index -- SLOW, not wedged, so it still returns -- and
    ``EngineClient.stop()`` must not report done before it actually has.

    Before the fix, ``stop()`` closed the socket the instant the ack
    arrived, so this returned in milliseconds regardless of how long
    finalize took -- and ``doxa.fleet.FleetRun.teardown`` learned nothing
    from it about whether the daemon was actually gone. The fix makes
    ``stop()`` wait for the daemon's own close, which ``_handle_client``
    only does once ``_shutdown`` (finalize included) has returned -- so
    the elapsed time here has to be at least the finalize delay, not
    however long the ack took."""
    FINALIZE_DELAY = 0.3

    async with running_daemon(tmp_path, monkeypatch, linger=600.0) as (
        daemon, created, serve_task,
    ):
        async def slow_finalize():
            await asyncio.sleep(FINALIZE_DELAY)
            daemon.engine._finalized = True
            return EngineEvent("session_done", {"indexed": 0, "belief_count": 0})

        daemon.engine.finalize = slow_finalize

        client = EngineClient(str(daemon.socket_path))
        await client.start()

        started = time.monotonic()
        done = await asyncio.wait_for(client.stop(), timeout=5.0)
        elapsed = time.monotonic() - started

        assert done.data.get("stopped") is True
        assert elapsed >= FINALIZE_DELAY - 0.05, (
            f"stop() returned after {elapsed:.3f}s -- before the "
            f"{FINALIZE_DELAY}s finalize it was supposed to wait for"
        )
        await asyncio.wait_for(serve_task, 5)


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


def _slow_script_client_factory(gate: asyncio.Event):
    """A FakeClient-shaped stand-in whose receive_response() blocks on
    `gate` until the test releases it -- shared by every mid-turn-queue
    daemon test below, so a second prompt can be submitted while the
    first is provably still running."""

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

    return SlowScriptClient


@pytest.mark.asyncio
async def test_a_prompt_submitted_mid_turn_is_queued_not_refused(tmp_path, monkeypatch):
    """The bug this whole feature replaces: typing a prompt while a turn
    is running used to error (`"a turn is already running"`) AND leave
    the running turn's own generator undrained. Now it is acknowledged
    immediately as queued (never refused), the FIRST turn still runs to
    turn_done untouched, EVERY attached client (not just whichever one
    submitted the second prompt) learns it was queued, and it starts
    automatically -- its own turn_started/turn_done -- the instant the
    first turn ends."""
    gate = asyncio.Event()
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    daemon = SessionDaemon(
        cwd=str(tmp_path), linger_secs=30.0,
        engine_factory=lambda cwd, sid, dsock: SessionEngine(
            cwd=cwd, session_id=sid, client_factory=_slow_script_client_factory(gate),
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

        # Two attached clients both observe the queue events, by two
        # different routes: B (the one that submitted "me too") learns
        # it from its OWN send() yield -- the daemon deliberately does
        # NOT also broadcast this one to B (see
        # SessionDaemon._publish's own docstring) -- and A (the OTHER
        # attached client, who asked for nothing) learns it purely from
        # that broadcast, over its out-of-band stream.
        oob_a_queued = asyncio.ensure_future(_drain_oob(a, "prompt_queued"))
        events_b = [ev async for ev in b.send("me too")]
        # Queued, not refused: NO exception, and exactly the one
        # acknowledgement event -- no turn for "me too" yet.
        assert [ev.type for ev in events_b] == ["prompt_queued"]
        assert events_b[0].data["position"] == 1
        assert events_b[0].data["text"] == "me too"
        queued_a = await asyncio.wait_for(oob_a_queued, 5)
        assert queued_a[-1].type == "prompt_queued"
        assert queued_a[-1].data["position"] == 1
        assert queued_a[-1].data["text"] == "me too"

        # The FIRST turn is untouched: it still runs to its own
        # turn_done, exactly as if nothing else had been typed.
        gate.set()
        events_a = await asyncio.wait_for(task_a, 5)
        assert events_a[-1].type == "turn_done"

        # The queued prompt starts automatically once the first turn
        # ends -- nobody had to resubmit it. Collect A's out-of-band
        # stream, not B's: "slow one" is foreign to B too (B never sent
        # it), so B's oob ALSO carries slow one's own turn_done and would
        # stop there instead -- A's oob is clean of that (matched A's own
        # _active_turn, so it went to A's turn_queue) and, already
        # drained of its own prompt_queued above, holds only what
        # "me too" publishes once dequeued.
        oob_after = await asyncio.wait_for(_drain_oob(a, "turn_done"), 5)
        assert [ev.type for ev in oob_after][:2] == ["prompt_dequeued", "turn_started"]
        assert oob_after[-1].type == "turn_done"

        await a.finalize()
        await b.finalize()
    finally:
        if not serve_task.done():
            with contextlib.suppress(Exception):
                await daemon._shutdown("test teardown")
                await asyncio.wait_for(serve_task, 5)


@pytest.mark.asyncio
async def test_several_queued_prompts_start_in_fifo_order(tmp_path, monkeypatch):
    """Three prompts typed in a row while the first turn runs: all three
    are queued (never refused), and each starts -- and finishes -- in
    the order it was typed, one at a time, with no interleaving."""
    gate = asyncio.Event()
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    daemon = SessionDaemon(
        cwd=str(tmp_path), linger_secs=30.0,
        engine_factory=lambda cwd, sid, dsock: SessionEngine(
            cwd=cwd, session_id=sid, client_factory=_slow_script_client_factory(gate),
            daemon_socket=dsock,
        ),
    )
    serve_task = asyncio.create_task(daemon.serve())
    await asyncio.wait_for(daemon.ready.wait(), 10)
    try:
        client = EngineClient(str(daemon.socket_path))
        await client.start()

        async def run_first():
            return [ev async for ev in client.send("first")]

        task_first = asyncio.create_task(run_first())
        for _ in range(100):
            if daemon._turn_task is not None:
                break
            await asyncio.sleep(0.01)

        # One connection submitting all four: the daemon excludes THIS
        # writer from its prompt_queued broadcast every time (it is the
        # requester for "second"/"third"/"fourth" too, same as "me too"
        # in the mid-turn-queue test above), so each ack comes back on
        # the send() call itself, not over peer_events().
        positions = []
        for text in ("second", "third", "fourth"):
            events = [ev async for ev in client.send(text)]
            assert [ev.type for ev in events] == ["prompt_queued"]
            positions.append(events[0].data["position"])
        assert positions == [1, 2, 3]

        gate.set()  # release "first"; the daemon's slow client stays
        # released for every turn hereafter, so each queued prompt runs
        # to completion as soon as it starts.
        await asyncio.wait_for(task_first, 5)

        order: list[str] = []
        for _ in range(3):
            dequeued = await asyncio.wait_for(
                _drain_oob(client, "turn_done"), 5,
            )
            started = next(ev for ev in dequeued if ev.type == "prompt_dequeued")
            order.append(str(started.data["text"]))
        assert order == ["second", "third", "fourth"]

        await client.finalize()
    finally:
        if not serve_task.done():
            with contextlib.suppress(Exception):
                await daemon._shutdown("test teardown")
                await asyncio.wait_for(serve_task, 5)


@pytest.mark.asyncio
async def test_the_queue_bound_is_enforced_with_a_clear_reply(tmp_path, monkeypatch):
    """PROMPT_QUEUE_MAXLEN prompts queue cleanly; the next one is refused
    with a clear, specific reason -- never silently dropped."""
    from doxa.promptqueue import PROMPT_QUEUE_MAXLEN

    gate = asyncio.Event()
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    daemon = SessionDaemon(
        cwd=str(tmp_path), linger_secs=30.0,
        engine_factory=lambda cwd, sid, dsock: SessionEngine(
            cwd=cwd, session_id=sid, client_factory=_slow_script_client_factory(gate),
            daemon_socket=dsock,
        ),
    )
    serve_task = asyncio.create_task(daemon.serve())
    await asyncio.wait_for(daemon.ready.wait(), 10)
    try:
        client = EngineClient(str(daemon.socket_path))
        await client.start()

        task_first = asyncio.create_task(
            _collect(client.send("first"))
        )
        for _ in range(100):
            if daemon._turn_task is not None:
                break
            await asyncio.sleep(0.01)

        for i in range(PROMPT_QUEUE_MAXLEN):
            events = [ev async for ev in client.send(f"queued-{i}")]
            assert [ev.type for ev in events] == ["prompt_queued"]

        with pytest.raises(EngineClientError, match="queue is full"):
            async for _ in client.send("one too many"):
                pass

        gate.set()
        await asyncio.wait_for(task_first, 5)
        await client.finalize()
    finally:
        if not serve_task.done():
            with contextlib.suppress(Exception):
                await daemon._shutdown("test teardown")
                await asyncio.wait_for(serve_task, 5)


@pytest.mark.asyncio
async def test_cancelling_a_queued_prompt_is_visible_to_every_client(tmp_path, monkeypatch):
    """/queue's cancel: the SDK-facing daemon call removes the queued
    prompt (it never starts), and the cancellation is broadcast -- every
    attached client, not just whichever one asked, sees it."""
    gate = asyncio.Event()
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    daemon = SessionDaemon(
        cwd=str(tmp_path), linger_secs=30.0,
        engine_factory=lambda cwd, sid, dsock: SessionEngine(
            cwd=cwd, session_id=sid, client_factory=_slow_script_client_factory(gate),
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

        task_first = asyncio.create_task(_collect(a.send("first")))
        for _ in range(100):
            if daemon._turn_task is not None:
                break
            await asyncio.sleep(0.01)

        queued_events = [ev async for ev in a.send("cancel me")]
        assert [ev.type for ev in queued_events] == ["prompt_queued"]
        item_id = queued_events[0].data["id"]

        oob_b = asyncio.ensure_future(_drain_oob(b, "prompt_cancelled"))
        ok = await b.cancel_queued(item_id)
        assert ok
        cancelled = await asyncio.wait_for(oob_b, 5)
        assert cancelled[-1].data["id"] == item_id
        assert cancelled[-1].data["text"] == "cancel me"

        # It never starts: releasing the gate only lets "first" finish,
        # and the daemon returns to idle -- no second turn follows.
        gate.set()
        await asyncio.wait_for(task_first, 5)
        assert daemon._turn_task is None or daemon._turn_task.done()
        await asyncio.sleep(0.05)  # give a wrongly-started turn a chance
        listing = await a.list_queue()
        assert listing == []

        await a.finalize()
        await b.finalize()
    finally:
        if not serve_task.done():
            with contextlib.suppress(Exception):
                await daemon._shutdown("test teardown")
                await asyncio.wait_for(serve_task, 5)


async def _collect(agen):
    return [ev async for ev in agen]


@pytest.mark.asyncio
async def test_status_carries_identity_surface_to_the_client(tmp_path, monkeypatch):
    """The daemon's status reply relays the engine's connect-time account
    block and LORE store path; EngineClient caches them on refresh --
    engine-parity attributes the app reads synchronously mid-render."""
    account = {"email": "doc@example.org", "subscriptionType": "Claude Max",
               "apiProvider": "firstParty"}
    async with running_daemon(
        tmp_path, monkeypatch, server_info={"account": account}
    ) as (daemon, _, _):
        client = EngineClient(str(daemon.socket_path))
        await client.start()  # start() seeds the status cache
        assert client.account == account
        assert client.lore_root  # daemon-side LORE store path
        await client.finalize()


@pytest.mark.asyncio
async def test_beliefs_call_round_trips_active_belief_bodies(tmp_path, monkeypatch):
    """Item 3's beliefs picker, one layer down: the new "beliefs" call
    round-trips SessionEngine.list_beliefs()'s own result over the socket
    -- a SEPARATE call from "status" (which only ever carries the cheap
    belief_count(), see test_status_carries_identity_surface_to_the_client
    above for that same status-cache path)."""
    from lore_core import beliefs as beliefs_mod
    from lore_core import store as lore_store

    conn = lore_store.db_connect()
    beliefs_mod.belief_insert(
        conn, "project:doxa", "uses uv for deps", 0.8, None, None, None,
    )
    conn.commit()

    async with running_daemon(tmp_path, monkeypatch) as (daemon, _, _):
        client = EngineClient(str(daemon.socket_path))
        await client.start()
        result = await client.list_beliefs()
        claims = [b["claim"] for b in result]
        assert "uses uv for deps" in claims
        await client.finalize()


@pytest.mark.asyncio
async def test_context_call_round_trips_the_breakdown_over_the_socket(
    tmp_path, monkeypatch
):
    """Item K's `/context` in the mode DOXA actually ships in: the daemon
    owns the SDK client, so it is the only side that can issue the CLI's
    get_context_usage control request at all, and the client's engine-parity
    method has to bring the whole breakdown back across the socket.

    No pager, unlike `beliefs`/`pending`: doxa.engine.context_breakdown
    drops the SDK's pre-rendered gridRows and caps every list, so one reply
    fits MAX_FRAME_BYTES. That the reply SURVIVES the trip -- rather than
    being replaced by encode_frame's oversize-reply error -- is the half of
    this test that matters."""
    from tests.test_context import CTX_USAGE

    async with running_daemon(
        tmp_path, monkeypatch, ctx_usage=CTX_USAGE,
    ) as (daemon, _, _):
        client = EngineClient(str(daemon.socket_path))
        await client.start()
        breakdown = await client.context_usage()
        assert breakdown is not None
        assert breakdown["total_tokens"] == 60_650
        assert breakdown["percentage"] == pytest.approx(33.7)
        assert [c["name"] for c in breakdown["categories"]] == [
            c["name"] for c in CTX_USAGE["categories"]
        ]
        assert breakdown["mcp_tools"][0]["server"] == "doxa_lore"
        # The daemon-side engine measured its own injected snapshot.
        assert breakdown["lore_snapshot_chars"] > 0
        assert "gridRows" not in breakdown
        await client.finalize()


@pytest.mark.asyncio
async def test_context_call_reports_an_unmeasurable_session_as_absent(
    tmp_path, monkeypatch
):
    """No ctx_usage scripted -> the FakeClient's control request raises ->
    the daemon replies with a null breakdown and the client hands the pane
    None. The pane then prints "cannot report a breakdown" rather than an
    empty one, which is item K's whole rule."""
    async with running_daemon(tmp_path, monkeypatch) as (daemon, _, _):
        client = EngineClient(str(daemon.socket_path))
        await client.start()
        assert await client.context_usage() is None
        await client.finalize()


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


# -- worktree-per-session (#3) --------------------------------------------
#
# Real git repos throughout: this wires doxa.worktrees into the daemon's
# actual spawn/finalize path, which is exactly the git-behavior seam a
# mock would not exercise honestly.


def _git_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "trunk", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    (path / "f.txt").write_text("one", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "one"], check=True)
    return path


@contextlib.asynccontextmanager
async def running_daemon_at(cwd, tmp_path, monkeypatch, linger=30.0,
                             base_branch=None):
    """running_daemon's twin, hosting a real git repo cwd instead of a
    plain tmp dir -- DOXA_HOME is isolated too, since worktrees.create()
    makes worktrees_root() under it. ``base_branch`` (item S #1) threads
    straight to SessionDaemon's own parameter -- the same wire this
    module's ``spawn_daemon`` uses over a real subprocess, exercised
    in-process here."""
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "home"))
    config_mod.invalidate()
    factory, created = factory_with_script(list(TURN_SCRIPT))
    daemon = SessionDaemon(
        cwd=str(cwd), linger_secs=linger, base_branch=base_branch,
        engine_factory=lambda cwd, sid, dsock: SessionEngine(
            cwd=cwd, session_id=sid, client_factory=factory, daemon_socket=dsock,
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
        config_mod.invalidate()


@pytest.mark.asyncio
async def test_daemon_substitutes_cwd_for_a_worktree_by_default(tmp_path, monkeypatch):
    """The wire-in point: by the time the engine is built, self.cwd (and
    therefore engine.cwd, the hello frame, EngineClient.cwd, and
    SessionPane's GitLine) already points at the session's OWN worktree."""
    repo = _git_repo(tmp_path / "repo")
    async with running_daemon_at(repo, tmp_path, monkeypatch) as (daemon, _, _):
        assert daemon.cwd != str(repo)
        assert daemon.cwd.startswith(str(worktrees_mod.worktrees_root()))
        assert daemon.engine.cwd == daemon.cwd
        branch = subprocess.run(
            ["git", "-C", daemon.cwd, "branch", "--show-current"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert branch == f"doxa/{daemon.session_id[:8]}"


@pytest.mark.asyncio
async def test_daemon_worktree_toggle_off_keeps_original_cwd(tmp_path, monkeypatch):
    """DOXA_WORKTREE=0 -> current behavior exactly: the daemon (and engine)
    run directly in the launch directory."""
    monkeypatch.setenv("DOXA_WORKTREE", "0")
    repo = _git_repo(tmp_path / "repo")
    async with running_daemon_at(repo, tmp_path, monkeypatch) as (daemon, _, _):
        assert daemon.cwd == str(repo)
        assert daemon.engine.cwd == str(repo)
        assert not worktrees_mod.worktrees_root().exists()


@pytest.mark.asyncio
async def test_clean_stop_removes_the_worktree_with_no_trace(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path / "repo")
    async with running_daemon_at(repo, tmp_path, monkeypatch) as (
        daemon, _, serve_task,
    ):
        worktree_path = daemon.cwd
        client = EngineClient(str(daemon.socket_path))
        await client.start()
        done = await client.stop()
        assert done.data.get("stopped") is True
        assert done.data.get("note") is None
        await asyncio.wait_for(serve_task, 5)
        assert not Path(worktree_path).exists()


@pytest.mark.asyncio
async def test_dirty_stop_keeps_the_worktree_and_returns_a_note(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path / "repo")
    async with running_daemon_at(repo, tmp_path, monkeypatch) as (
        daemon, _, serve_task,
    ):
        worktree_path = daemon.cwd
        (Path(worktree_path) / "scratch.txt").write_text("wip", encoding="utf-8")
        client = EngineClient(str(daemon.socket_path))
        await client.start()
        done = await client.stop()
        assert done.data.get("stopped") is True
        note = done.data.get("note")
        assert note is not None
        assert note.startswith(f"kept doxa/{daemon.session_id[:8]}")
        assert "merge when ready" in note
        await asyncio.wait_for(serve_task, 5)
        assert Path(worktree_path).exists()  # kept, not destroyed


@pytest.mark.asyncio
async def test_detach_leaves_the_worktree_intact(tmp_path, monkeypatch):
    """A mere detach (client closes, daemon lingers) must never trigger
    the worktree cleanup that only real finalize runs."""
    repo = _git_repo(tmp_path / "repo")
    async with running_daemon_at(repo, tmp_path, monkeypatch, linger=30.0) as (
        daemon, _, serve_task,
    ):
        worktree_path = daemon.cwd
        client = EngineClient(str(daemon.socket_path))
        await client.start()
        await client.finalize()  # detach, not stop
        await asyncio.sleep(0.1)
        assert not serve_task.done()  # daemon still lingering
        assert Path(worktree_path).exists()


@pytest.mark.asyncio
async def test_attaching_while_shutdown_is_in_progress_does_not_abort_it(
    tmp_path, monkeypatch,
):
    """Regression for the daemon-stranding defect: a client that attaches
    the instant the linger sleep ends -- while _linger_then_stop is
    already inside _shutdown, blocked on engine.finalize() -- calls
    _cancel_linger() exactly as every attach does. That must not reach
    the shutdown in progress: finalize completes, the worktree finalizer
    runs, and _done is set, all despite the mid-shutdown attach."""
    repo = _git_repo(tmp_path / "repo")
    async with running_daemon_at(repo, tmp_path, monkeypatch, linger=0.05) as (
        daemon, created, serve_task,
    ):
        worktree_path = daemon.cwd
        real_finalize = daemon.engine.finalize
        finalize_entered = asyncio.Event()
        release_finalize = asyncio.Event()

        async def gated_finalize():
            finalize_entered.set()
            await release_finalize.wait()
            return await real_finalize()

        daemon.engine.finalize = gated_finalize

        # A client has to have attached at least once first: before that,
        # _arm_linger uses INITIAL_CLAIM_SECS (the generous unclaimed-spawn
        # window), not linger_secs, so an unattached daemon would never
        # reach _shutdown on this test's timescale. Attach and detach --
        # the SUBSEQUENT re-arm (on this drop) is the short linger_secs one.
        first = EngineClient(str(daemon.socket_path))
        await first.start()
        await first.finalize()

        # Nobody is attached now: the (short) linger expires and
        # _linger_then_stop moves past its sleep into _shutdown, now
        # parked inside the gated engine.finalize() -- exactly the window
        # the defect lived in.
        await asyncio.wait_for(finalize_entered.wait(), 5)
        assert daemon._stopping is True
        assert not daemon._done.is_set()

        # A client attaches NOW, mid-shutdown; its attach handler calls
        # _cancel_linger() same as always. Before the fix this would
        # cancel the very task blocked above, delivering CancelledError
        # into engine.finalize() and stranding the daemon.
        client = EngineClient(str(daemon.socket_path))
        await client.start()
        await client.finalize()

        # Shutdown is still exactly where it was -- not aborted.
        assert not daemon._done.is_set()
        assert created[0].exited is False

        release_finalize.set()
        await asyncio.wait_for(serve_task, 5)

        assert daemon._done.is_set()
        assert created[0].exited is True  # finalize ran to completion
        assert not Path(worktree_path).exists()  # worktree finalizer ran


# -- queue item 5: needs_input over the daemon split -----------------------


@pytest.mark.asyncio
async def test_needs_input_round_trips_over_the_socket_to_an_attached_client(
    tmp_path, monkeypatch,
):
    """An attached client sees the needs_input frame the moment the
    engine's can_use_tool callback queues one, answers it over the
    socket, and gets back the SAME PermissionResult an in-process caller
    would (protocol serialization proven both directions)."""
    async with running_daemon(tmp_path, monkeypatch, linger=30.0) as (daemon, _, _):
        client = EngineClient(str(daemon.socket_path))
        await client.start()

        task = asyncio.ensure_future(daemon.engine._on_can_use_tool(
            "AskUserQuestion",
            {"questions": [{
                "question": "which env?", "header": "Pick one",
                "options": [{"label": "staging"}, {"label": "prod"}],
            }]},
            ToolPermissionContext(),
        ))
        events = await _drain_oob(client, "needs_input")
        ev = events[-1]
        assert ev.type == "needs_input"
        assert ev.data["kind"] == "ask_user"
        req_id = ev.data["id"]

        ok = await client.answer_needs_input(
            req_id, {"answers": {"which env?": "staging"}}
        )
        assert ok is True

        from claude_agent_sdk import PermissionResultAllow

        result = await asyncio.wait_for(task, 5)
        assert isinstance(result, PermissionResultAllow)
        assert result.updated_input["answers"] == {"which env?": "staging"}
        await client.finalize()


@pytest.mark.asyncio
async def test_needs_input_parks_and_replays_on_reattach_with_no_client(
    tmp_path, monkeypatch,
):
    """The detached-session case queue item 5 calls out explicitly: a
    needs_input fired with NO client attached at all must not hang
    silently -- it parks in the ring (replayed to whoever attaches next)
    and fires the desktop notification (always the unfocused gate --
    there is no window to be focused) since nobody is here to see a
    blink."""
    notified = []
    monkeypatch.setattr(
        daemon_mod.notify_mod, "notify_needs_input",
        lambda focus, label, summary: notified.append((focus, label, summary)),
    )
    async with running_daemon(tmp_path, monkeypatch, linger=30.0) as (daemon, _, _):
        assert daemon._clients == set()  # nobody attached yet

        task = asyncio.ensure_future(daemon.engine._on_can_use_tool(
            "Bash", {"command": "rm -rf /tmp/x"},
            ToolPermissionContext(title="Claude wants to run rm -rf /tmp/x"),
        ))
        await asyncio.sleep(0.05)  # let the pump publish + notify

        assert len(notified) == 1
        focus, label, summary = notified[0]
        assert focus is False
        assert "rm -rf" in summary

        # A later attach replays the parked question from the ring.
        client = EngineClient(str(daemon.socket_path))
        await client.start()
        events = await _drain_oob(client, "needs_input")
        ev = next(e for e in events if e.type == "needs_input")
        assert ev.data["kind"] == "permission"

        # The browser restores its transcript and skips ring replay. It
        # needs the still-open request in status even on that attach path.
        browser = EngineClient(str(daemon.socket_path), skip_backlog=True)
        await browser.start()
        status = await browser.refresh_status()
        assert status["pending_inputs"] == [ev.data]

        await browser.answer_needs_input(ev.data["id"], {"decision": "deny"})
        from claude_agent_sdk import PermissionResultDeny

        result = await asyncio.wait_for(task, 5)
        assert isinstance(result, PermissionResultDeny)
        await _drain_oob(browser, "needs_input_resolved")
        assert (await browser.refresh_status())["pending_inputs"] == []
        await browser.finalize()
        await client.finalize()


@pytest.mark.asyncio
async def test_answer_needs_input_resolution_broadcasts_to_every_attached_client(
    tmp_path, monkeypatch,
):
    """Two clients attached to the same daemon (two windows on one
    session): one answers, and the OTHER also sees needs_input_resolved
    -- the same "everyone learns" convention model_changed already
    follows for /model."""
    async with running_daemon(tmp_path, monkeypatch, linger=30.0) as (daemon, _, _):
        first = EngineClient(str(daemon.socket_path))
        await first.start()
        second = EngineClient(str(daemon.socket_path))
        await second.start()

        task = asyncio.ensure_future(daemon.engine._on_can_use_tool(
            "AskUserQuestion",
            {"questions": [{"question": "q", "options": [{"label": "A"}]}]},
            ToolPermissionContext(),
        ))
        events = await _drain_oob(first, "needs_input")
        req_id = events[-1].data["id"]

        await second.answer_needs_input(req_id, {"answers": {"q": "A"}})
        await asyncio.wait_for(task, 5)

        resolved = await _drain_oob(first, "needs_input_resolved")
        assert resolved[-1].data["id"] == req_id
        await first.finalize()
        await second.finalize()


@pytest.mark.asyncio
async def test_answer_needs_input_unknown_id_is_a_graceful_rpc_failure(
    tmp_path, monkeypatch,
):
    async with running_daemon(tmp_path, monkeypatch, linger=30.0) as (daemon, _, _):
        client = EngineClient(str(daemon.socket_path))
        await client.start()
        ok = await client.answer_needs_input("no-such-id", {"decision": "allow"})
        assert ok is False
        await client.finalize()


# -- item S: branch switch, the daemon RPC and spawn-time wiring -----------


@pytest.mark.asyncio
async def test_daemon_forks_the_worktree_from_an_explicit_base_branch(
    tmp_path, monkeypatch,
):
    """Item S #1's daemon-side half: SessionDaemon(base_branch=...) reaches
    worktrees.create the same way cli.py's --branch does over a real
    subprocess -- exercised here in-process."""
    repo = _git_repo(tmp_path / "repo")
    subprocess.run(["git", "-C", str(repo), "branch", "alt"], check=True)
    async with running_daemon_at(
        repo, tmp_path, monkeypatch, base_branch="alt",
    ) as (daemon, _, _):
        meta = worktrees_mod.read_meta(daemon.cwd)
        assert meta is not None and meta.get("base_ref") == "alt"


@pytest.mark.asyncio
async def test_branch_rpc_lists_local_branches_with_the_base_marked(
    tmp_path, monkeypatch,
):
    repo = _git_repo(tmp_path / "repo")
    subprocess.run(["git", "-C", str(repo), "branch", "develop"], check=True)
    async with running_daemon_at(repo, tmp_path, monkeypatch) as (daemon, _, _):
        client = EngineClient(str(daemon.socket_path))
        await client.start()
        result = await client.switch_branch(None)
        assert result["base"] == "trunk"
        assert set(result["branches"]) >= {"trunk", "develop"}
        await client.finalize()


@pytest.mark.asyncio
async def test_branch_rpc_switch_round_trips_and_broadcasts_to_every_client(
    tmp_path, monkeypatch,
):
    """The switch itself: the daemon does the git op (worktrees.switch_base
    against its OWN cwd, the session's worktree), and every attached
    client -- not just whichever one asked -- gets the base_changed echo,
    same "everyone learns it" rule model_changed already follows."""
    repo = _git_repo(tmp_path / "repo")
    subprocess.run(["git", "-C", str(repo), "branch", "develop"], check=True)
    async with running_daemon_at(repo, tmp_path, monkeypatch) as (daemon, _, _):
        first = EngineClient(str(daemon.socket_path))
        await first.start()
        second = EngineClient(str(daemon.socket_path))
        await second.start()

        result = await second.switch_branch("develop")
        assert result["ok"] is True
        assert result["base"] == "develop"

        events = await _drain_oob(first, "base_changed")
        assert events[-1].data["base"] == "develop"

        meta = worktrees_mod.read_meta(daemon.cwd)
        assert meta is not None and meta.get("base_ref") == "develop"
        await first.finalize()
        await second.finalize()


@pytest.mark.asyncio
async def test_branch_rpc_switch_refusal_comes_back_without_raising(
    tmp_path, monkeypatch,
):
    """A dirty worktree: the RPC transport succeeds (this IS a normal,
    expected outcome, not a protocol failure), and the refusal rides in
    result["ok"]/result["message"] for the caller to show verbatim."""
    repo = _git_repo(tmp_path / "repo")
    subprocess.run(["git", "-C", str(repo), "branch", "develop"], check=True)
    async with running_daemon_at(repo, tmp_path, monkeypatch) as (daemon, _, _):
        (Path(daemon.cwd) / "scratch.txt").write_text("wip", encoding="utf-8")
        client = EngineClient(str(daemon.socket_path))
        await client.start()
        result = await client.switch_branch("develop")
        assert result["ok"] is False
        assert "uncommitted changes" in result["message"]
        await client.finalize()


# -- v0.28.0 defect 2: the beliefs reply outgrew the 64KB frame -----------
#
# Reported: "clicking on 'beliefs' chip leads to error message 'too much
# for a message'" / "it was supposed to be shown in an autocomplete
# dropdown". SessionEngine.list_beliefs returns beliefs WITH claim bodies;
# in a DETACHED session those crossed the socket in ONE reply, and
# encode_frame answers an oversize non-event reply by replacing it whole
# with {"ok": false, "error": "reply exceeded the frame cap"} -- which
# EngineClient raised and doxa.app printed as a system message instead of
# opening the picker. The operator has ~517 active beliefs; the fixtures
# below synthesize a set that genuinely exceeds MAX_FRAME_BYTES, because a
# test that stays under the cap proves nothing about this defect.


# Sized from a MEASUREMENT of the reporting operator's live LORE store, not
# from a guess: 500 active beliefs serialized to 235,839 bytes (230.3 KB) --
# 3.6x the 64KB frame cap -- at an average claim of 201 chars, max 300. A
# fixture that merely crossed 64KB would not have told paging apart from
# trim-the-claim-and-hope, and trimming was measured to STILL exceed the cap
# on that same store (115,105 bytes with claims cut to 120 chars, 1.75x
# over). So this fixture deliberately exceeds the real payload on both axes,
# rows and bytes, and asserts that it does.
REAL_STORE_PAYLOAD_BYTES = 235_839


def _staged_record(index, text):
    """One staged proposal as ``SessionEngine._pending_records`` returns it
    since item V -- a RECORD with the pending id the picker approves by
    and the fields its proposed verdict is computed from, not the bare
    string /pending carried in v0.31.0."""
    return {
        "pid": f"20260824120000-{index:02d}",
        "kind": "memory", "action": "add", "scope": "user",
        "created": "2026-08-24T12:00:00Z", "text": text,
    }


def _seed_big_belief_store(count=600, claim_chars=400):
    """`count` active beliefs whose serialized size exceeds the operator's
    real store. Returns (conn, subject) -- the caller deletes them again,
    since conftest.py's LORE_ROOT is shared by the whole session."""
    from lore_core import beliefs as beliefs_mod
    from lore_core import store as lore_store

    subject = "project:framecap"
    conn = lore_store.db_connect()
    for i in range(count):
        beliefs_mod.belief_insert(
            conn, subject, f"belief {i:04d} " + ("x" * claim_chars),
            0.7, None, None, None,
        )
    conn.commit()
    rows = conn.execute(
        "SELECT id, subject, claim, confidence FROM beliefs WHERE subject = ?",
        (subject,),
    ).fetchall()
    payload = len(json.dumps(
        [{"id": r[0], "subject": r[1], "claim": r[2], "confidence": r[3]}
         for r in rows],
        ensure_ascii=False,
    ).encode("utf-8"))
    assert payload > REAL_STORE_PAYLOAD_BYTES, (
        f"fixture ({payload} bytes) must exceed the real store's measured "
        f"{REAL_STORE_PAYLOAD_BYTES} bytes"
    )
    assert payload > peers.MAX_FRAME_BYTES * 3
    return conn, subject


def _drop_big_belief_store(conn, subject):
    conn.execute("DELETE FROM beliefs WHERE subject = ?", (subject,))
    conn.commit()


def test_fit_belief_page_splits_on_the_byte_budget():
    """Sizing by MEASUREMENT, not by a fixed row count: the page ends when
    the bytes run out, and reports where to resume."""
    beliefs = [
        {"id": i, "subject": "project:x", "claim": "y" * 4096, "confidence": 0.5}
        for i in range(200)
    ]
    page, next_offset = daemon_mod._fit_belief_page(beliefs, 0)
    assert 0 < len(page) < len(beliefs)
    assert next_offset == len(page)
    encoded = daemon_mod.encode_frame(
        {"type": "reply", "id": 1, "ok": True,
         "beliefs": page, "next_offset": next_offset}
    )
    # The whole point: what comes back is a REAL page, not encode_frame's
    # "reply exceeded the frame cap" substitute.
    assert len(encoded) <= peers.MAX_FRAME_BYTES
    assert b"exceeded the frame cap" not in encoded


def test_fit_belief_page_ends_cleanly_on_a_short_list():
    beliefs = [{"id": 1, "subject": "user", "claim": "short", "confidence": 0.5}]
    page, next_offset = daemon_mod._fit_belief_page(beliefs, 7)
    assert page == beliefs
    assert next_offset is None


def test_fit_belief_page_never_stalls_on_one_oversize_belief():
    """A single claim larger than the entire frame budget would otherwise
    page forever without emitting a row. It goes out alone, cut to fit,
    and MARKED -- the offset still advances."""
    huge = {"id": 1, "subject": "user", "claim": "z" * (peers.MAX_FRAME_BYTES * 2),
            "confidence": 0.9}
    page, next_offset = daemon_mod._fit_belief_page([huge, huge], 0)
    assert len(page) == 1
    assert next_offset == 1
    assert page[0]["claim_truncated"] is True
    assert len(page[0]["claim"]) < len(huge["claim"])
    assert len(daemon_mod.encode_frame(
        {"type": "reply", "id": 1, "ok": True, "beliefs": page}
    )) <= peers.MAX_FRAME_BYTES


def _seed_oversize_belief(claim_bytes=None):
    """ONE active belief whose claim ALONE exceeds the byte budget -- the
    shape defect 2's report described (as opposed to
    :func:`_seed_big_belief_store`'s many moderate rows summing past the
    cap). Returns (conn, subject, belief_id); the caller drops it with
    :func:`_drop_big_belief_store`, which deletes by subject regardless of
    how the rows were seeded."""
    from lore_core import beliefs as beliefs_mod
    from lore_core import store as lore_store

    subject = "project:oversize-belief"
    conn = lore_store.db_connect()
    beliefs_mod.belief_insert(
        conn, subject, "z" * (claim_bytes or peers.MAX_FRAME_BYTES * 2),
        0.9, None, None, None,
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM beliefs WHERE subject = ?", (subject,),
    ).fetchone()
    return conn, subject, row[0]


@pytest.mark.asyncio
async def test_a_belief_page_whose_first_row_exceeds_the_byte_budget_advances_past_it(
    tmp_path, monkeypatch,
):
    """End to end, over a real socket: the `beliefs` RPC's own guard on
    _fit_belief_page's result (``if next_offset is None and len(beliefs)
    == fetch: next_offset = offset + len(page)``) must never regress the
    advance _fit_belief_page already made -- and EngineClient.list_beliefs'
    paging loop, which would otherwise spin forever on a non-advancing
    offset, has to actually terminate with the oversize row present
    (marked ``claim_truncated``) rather than an empty result. The
    wait_for is the termination proof: a reintroduced stall times out
    the test instead of hanging the suite."""
    conn, subject, _belief_id = _seed_oversize_belief()
    try:
        async with running_daemon(tmp_path, monkeypatch) as (daemon, _, _):
            client = EngineClient(str(daemon.socket_path))
            await client.start()
            result = await asyncio.wait_for(client.list_beliefs(), 5)
            mine = [b for b in result if b["subject"] == subject]
            assert len(mine) == 1
            assert mine[0]["claim_truncated"] is True
            await client.finalize()
    finally:
        _drop_big_belief_store(conn, subject)


@pytest.mark.asyncio
async def test_beliefs_call_survives_a_store_bigger_than_one_frame(
    tmp_path, monkeypatch,
):
    """The defect end to end: over a real socket, with a belief store whose
    bodies exceed MAX_FRAME_BYTES, the client gets every belief back --
    not EngineClientError("reply exceeded the frame cap")."""
    conn, subject = _seed_big_belief_store()
    try:
        async with running_daemon(tmp_path, monkeypatch) as (daemon, _, _):
            client = EngineClient(str(daemon.socket_path))
            await client.start()
            result = await client.list_beliefs()
            mine = [b for b in result if b["subject"] == subject]
            assert len(mine) == 600
            # Whole claims, not ellipsized stand-ins -- the picker's rows
            # are ellipsized by _fmt_belief_row, the DATA is not.
            assert all(len(b["claim"]) > 200 for b in mine)
            assert not any(b.get("claim_truncated") for b in mine)
            await client.finalize()
    finally:
        _drop_big_belief_store(conn, subject)


@pytest.mark.asyncio
async def test_client_and_engine_list_beliefs_stay_in_parity(
    tmp_path, monkeypatch,
):
    """Paging is an implementation detail of the transport and must not be
    visible in the result: EngineClient.list_beliefs has to return exactly
    what SessionEngine.list_beliefs returns, because doxa.app reaches both
    through one `getattr(engine, "list_beliefs")` and cannot tell them
    apart."""
    conn, subject = _seed_big_belief_store()
    try:
        async with running_daemon(tmp_path, monkeypatch) as (daemon, _, _):
            client = EngineClient(str(daemon.socket_path))
            await client.start()
            over_socket = await client.list_beliefs()
            in_process = await SessionEngine(cwd=str(tmp_path)).list_beliefs()
            assert [b["id"] for b in over_socket] == [b["id"] for b in in_process]
            assert [b["claim"] for b in over_socket] == [
                b["claim"] for b in in_process
            ]
            await client.finalize()
    finally:
        _drop_big_belief_store(conn, subject)


@pytest.mark.asyncio
async def test_beliefs_paging_honours_an_explicit_limit(tmp_path, monkeypatch):
    """The limit is the caller's window, not a per-frame quota -- the loop
    stops at it rather than draining the store."""
    conn, subject = _seed_big_belief_store()
    try:
        async with running_daemon(tmp_path, monkeypatch) as (daemon, _, _):
            client = EngineClient(str(daemon.socket_path))
            await client.start()
            assert len(await client.list_beliefs(limit=37)) == 37
            await client.finalize()
    finally:
        _drop_big_belief_store(conn, subject)


@pytest.mark.asyncio
async def test_the_beliefs_picker_opens_complete_and_filterable_over_the_socket(
    tmp_path, monkeypatch,
):
    """The user-visible end of defect 2, over a REAL daemon socket with a
    belief payload larger than the operator's own store: the picker opens
    (it used to print "reply exceeded the frame cap" instead), and every
    belief is resident BEFORE the user can type.

    Residency is the whole reason paging stops at the transport and never
    reaches the scroll position. ChipPicker's type-to-filter matches across
    the entire row set; if only the first page were loaded, typing a term
    that matches a belief on a later page would show nothing -- the picker
    would actively assert that belief does not exist. A slow open beats a
    lying filter, so this asserts a LATE-page belief is findable by
    filtering immediately after open."""
    from doxa.app import ChipPicker, DoxaApp

    conn, subject = _seed_big_belief_store()
    try:
        async with running_daemon(tmp_path, monkeypatch) as (daemon, _, _):
            sock = str(daemon.socket_path)
            app = DoxaApp(cwd=str(tmp_path), engine_factory=lambda: EngineClient(sock))
            async with app.run_test(size=(140, 40)) as pilot:
                pane = app.active_pane
                for _ in range(400):
                    if pane.engine is not None:
                        break
                    await pilot.pause(0.02)
                assert isinstance(pane.engine, EngineClient)

                await pane.open_beliefs_picker()
                await pilot.pause()
                picker = app.query_one("#chip-picker", ChipPicker)
                assert picker.is_open, "the picker must OPEN, not raise"

                # Complete: every seeded belief is resident, not just the
                # first frame's worth.
                # Matched by row id (v0.69.0 removed the "open the belief
                # browser" door row this used to have to exclude by hand --
                # every row is a real belief now) AND by this fixture's own
                # claim text, which excludes any belief another test left
                # in the shared store.
                seeded = [label for rid, label in picker._all_rows
                          if rid.startswith("belief:") and "belief 0" in label]
                assert len(seeded) == 600
                # ...and no caveat row, because nothing was actually capped.
                assert picker._note == ""

                # Filterable: a belief from the LAST page, reachable by
                # typing, with no further round trip.
                await pilot.press("0", "5", "9", "9")
                await pilot.pause()
                picker.flush_filter()  # v0.69.0: the filter now debounces
                visible = [label for rid, label in picker._rows if rid]
                assert any("belief 0599" in l for l in visible), (
                    "a late-page belief must be findable by filtering"
                )
    finally:
        _drop_big_belief_store(conn, subject)


# -- /pending over the split (v0.31.0) ---------------------------------
#
# Same frame-cap discipline the beliefs RPC above had to learn the hard
# way, applied BEFORE a report this time: a staged proposal is free text of
# unbounded length, and encode_frame discards an oversize reply rather than
# shortening it. These pin that the `pending` RPC pages, that the paging is
# invisible to the caller (engine parity), and that the read-only shape is
# the whole shape -- there is deliberately no approve/reject RPC.


def test_fit_pending_page_splits_on_the_byte_budget():
    texts = ["y" * 4096 for _ in range(200)]
    page, next_offset = daemon_mod._fit_pending_page(texts, 0)
    assert 0 < len(page) < len(texts)
    assert next_offset == len(page)
    encoded = daemon_mod.encode_frame(
        {"type": "reply", "id": 1, "ok": True,
         "pending": page, "next_offset": next_offset}
    )
    assert len(encoded) <= peers.MAX_FRAME_BYTES
    assert b"exceeded the frame cap" not in encoded


def test_fit_pending_page_ends_cleanly_on_a_short_list():
    page, next_offset = daemon_mod._fit_pending_page(["short"], 7)
    assert page == ["short"]
    assert next_offset is None


def test_fit_pending_page_never_stalls_on_one_oversize_proposal():
    """A single proposal larger than the whole frame budget would
    otherwise page forever without emitting a row. It goes out alone, cut
    to fit and visibly ellipsized, and the offset still advances."""
    huge = "z" * (peers.MAX_FRAME_BYTES * 2)
    page, next_offset = daemon_mod._fit_pending_page([huge, huge], 0)
    assert len(page) == 1 and next_offset == 1
    assert len(page[0]) < len(huge) and page[0].endswith("…")
    assert len(daemon_mod.encode_frame(
        {"type": "reply", "id": 1, "ok": True, "pending": page}
    )) <= peers.MAX_FRAME_BYTES


@pytest.mark.asyncio
async def test_a_pending_page_whose_first_row_exceeds_the_byte_budget_advances_past_it(
    tmp_path, monkeypatch,
):
    """End to end twin of the beliefs test above, for the `pending` RPC --
    the identical outer guard, over the identical shared _fit_page rule
    (:func:`_fit_pending_page`), fed a real RECORD the way item V's staged
    proposals actually arrive (not the bare-string legacy shape the unit
    test above uses). EngineClient.list_pending's paging loop has to
    terminate with the row present (marked ``text_truncated``); the
    wait_for is what turns a reintroduced stall into a test failure
    instead of a hung suite."""
    huge = _staged_record(0, "z" * (peers.MAX_FRAME_BYTES * 2))
    async with running_daemon(tmp_path, monkeypatch) as (daemon, _, _):
        monkeypatch.setattr(daemon.engine, "_pending_records", lambda: [huge])
        client = EngineClient(str(daemon.socket_path))
        await client.start()
        result = await asyncio.wait_for(client.list_pending(), 5)
        assert len(result) == 1
        assert result[0]["text_truncated"] is True
        await client.finalize()


@pytest.mark.asyncio
async def test_pending_call_survives_a_queue_bigger_than_one_frame(
    tmp_path, monkeypatch,
):
    """End to end over a real socket: a staging area whose texts exceed
    MAX_FRAME_BYTES comes back WHOLE, not as
    EngineClientError("reply exceeded the frame cap")."""
    # Prose filler, not a long opaque token: lore_core's scrubber redacts
    # anything that reads like a base64 blob, and a redacted fixture would
    # be testing the scrubber rather than the paging.
    filler = "the operator prefers uv over pip and keeps doxa in home. " * 40
    staged = [_staged_record(i, f"proposal {i:04d} {filler}") for i in range(120)]
    assert len(json.dumps(staged).encode("utf-8")) > peers.MAX_FRAME_BYTES * 3
    async with running_daemon(tmp_path, monkeypatch) as (daemon, _, _):
        monkeypatch.setattr(
            daemon.engine, "_pending_records", lambda: [dict(r) for r in staged]
        )
        client = EngineClient(str(daemon.socket_path))
        await client.start()
        result = await client.list_pending()
        assert result == staged  # every row, whole, in order
        await client.finalize()


@pytest.mark.asyncio
async def test_client_and_engine_list_pending_stay_in_parity(
    tmp_path, monkeypatch,
):
    """Paging is a transport detail and must not be visible in the result
    -- doxa.app reaches both engines through one `getattr(engine,
    "list_pending")` and cannot tell them apart."""
    filler = "remember that the deriver stages proposals for review. " * 20
    staged = [_staged_record(i, f"proposal {i} {filler}") for i in range(80)]
    async with running_daemon(tmp_path, monkeypatch) as (daemon, _, _):
        monkeypatch.setattr(
            daemon.engine, "_pending_records", lambda: [dict(r) for r in staged]
        )
        client = EngineClient(str(daemon.socket_path))
        await client.start()
        over_socket = await client.list_pending()
        in_process = await daemon.engine.list_pending()
        assert over_socket == in_process
        await client.finalize()


@pytest.mark.asyncio
async def test_pending_paging_honours_an_explicit_limit(tmp_path, monkeypatch):
    filler = "remember that the deriver stages proposals for review. " * 20
    staged = [_staged_record(i, f"proposal {i} {filler}") for i in range(200)]
    async with running_daemon(tmp_path, monkeypatch) as (daemon, _, _):
        monkeypatch.setattr(
            daemon.engine, "_pending_records", lambda: [dict(r) for r in staged]
        )
        client = EngineClient(str(daemon.socket_path))
        await client.start()
        assert len(await client.list_pending(limit=37)) == 37
        await client.finalize()


@pytest.mark.asyncio
async def test_the_write_rpcs_take_one_id_and_there_is_no_bulk_form(
    tmp_path, monkeypatch,
):
    """SECURITY ASSERTION, pinned at the protocol.

    v0.31.0 crossed the socket with the read half of the review gate only.
    Item V adds the write half, and the property that makes it defensible
    is that nothing crosses without a per-item decision: `approve_pending`
    and `reject_pending` take ONE `pid` and there is no bulk spelling of
    either. If a future generic handler ever accepts "approve_all" or a
    list parameter, this test is what says so."""
    async with running_daemon(tmp_path, monkeypatch) as (daemon, _, _):
        client = EngineClient(str(daemon.socket_path))
        await client.start()

        # No bulk door, under any of the obvious spellings.
        for method in ("approve", "reject", "approve_all", "reject_all",
                       "approve_pending_all"):
            reply = await client._call(method)
            assert reply.get("ok") is False
            assert "unknown method" in str(reply.get("error")), method

        # The real methods exist, and refuse a call that names no ONE item.
        for method in ("approve_pending", "reject_pending"):
            reply = await client._call(method)
            assert reply.get("ok") is False
            assert "no proposal id" in str(reply.get("error")), method
            # A list where an id belongs is not a bulk form either: it is
            # stringified into an id that matches nothing, and applies
            # nothing.
            reply = await client._call(method, pid=["a", "b"])
            assert reply.get("ok") is False

        await client.finalize()


# -- item V: the beliefs picker's own RPCs, over a real socket -----------


@pytest.mark.asyncio
async def test_belief_evidence_crosses_the_socket_for_one_belief(
    tmp_path, monkeypatch,
):
    """Engine parity for item V's lazy trail. Fetched per belief, capped,
    and put through the shared byte budget -- the point being that a
    picker over hundreds of beliefs never asks for hundreds of trails."""
    trail = [{"session_id": "s1", "project": "p", "note": "derived here",
              "created": "2026-05-02T09:00:00Z"}]
    async with running_daemon(tmp_path, monkeypatch) as (daemon, _, _):
        async def fake_evidence(belief_id, limit=40):
            assert belief_id == 7
            return [dict(row) for row in trail]

        monkeypatch.setattr(daemon.engine, "belief_evidence", fake_evidence)
        client = EngineClient(str(daemon.socket_path))
        await client.start()
        assert await client.belief_evidence(7) == trail
        await client.finalize()


@pytest.mark.asyncio
async def test_lore_write_state_is_the_daemons_answer_not_the_clients(
    tmp_path, monkeypatch,
):
    """The daemon holds lore_core and the store, so it is the side that
    knows whether an approval can be recorded honestly. A client with a
    perfectly current wheel installed must still get the DAEMON's answer."""
    async with running_daemon(tmp_path, monkeypatch) as (daemon, _, _):
        monkeypatch.setattr(daemon.engine, "lore_write_state", lambda: {
            "capable": False, "version": "0.34.0", "source": "plugin",
            "reason": "no provenance ledger over here",
        })
        client = EngineClient(str(daemon.socket_path))
        await client.start()
        state = await client.lore_write_state()
        assert state["capable"] is False
        assert state["version"] == "0.34.0"
        assert "no provenance ledger over here" in state["reason"]
        await client.finalize()


@pytest.mark.asyncio
async def test_one_approve_crosses_with_exactly_one_id(tmp_path, monkeypatch):
    seen: list = []

    async with running_daemon(tmp_path, monkeypatch) as (daemon, _, _):
        async def fake_approve(pid):
            seen.append(pid)
            return None

        async def fake_reject(pid):
            seen.append(("reject", pid))
            return None

        monkeypatch.setattr(daemon.engine, "approve_pending", fake_approve)
        monkeypatch.setattr(daemon.engine, "reject_pending", fake_reject)
        client = EngineClient(str(daemon.socket_path))
        await client.start()
        assert await client.approve_pending("20260824-00") is None
        assert await client.reject_pending("20260824-01") is None
        assert seen == ["20260824-00", ("reject", "20260824-01")]
        await client.finalize()


@pytest.mark.asyncio
async def test_belief_actions_cross_the_socket_one_belief_at_a_time(
    tmp_path, monkeypatch,
):
    """v0.48.0: recording an outcome and retracting over the daemon split.
    One belief_id per call, no list parameter, and no bulk spelling of
    either -- the same protocol-level property approve/reject carry."""
    seen: list = []

    async def fake_outcome(belief_id, event, note=None):
        seen.append(("outcome", belief_id, event))
        return None

    async def fake_retract(belief_id, reason="x"):
        seen.append(("retract", belief_id))
        return None

    async with running_daemon(tmp_path, monkeypatch) as (daemon, _, _):
        monkeypatch.setattr(daemon.engine, "record_belief_outcome", fake_outcome)
        monkeypatch.setattr(daemon.engine, "retract_belief", fake_retract)
        monkeypatch.setattr(daemon.engine, "belief_action_state", lambda: {
            "capable": False, "version": "0.30.0",
            "reason": "no outcome ledger over here",
        })
        client = EngineClient(str(daemon.socket_path))
        await client.start()

        state = await client.belief_action_state()
        assert state["capable"] is False
        assert "no outcome ledger over here" in state["reason"]

        assert await client.record_belief_outcome(7, "confirmed") is None
        assert await client.retract_belief(9) is None
        assert seen == [("outcome", 7, "confirmed"), ("retract", 9)]

        for method in ("retract_all", "belief_outcome_all", "retract_beliefs"):
            reply = await client._call(method)
            assert reply.get("ok") is False
            assert "unknown method" in str(reply.get("error")), method
        await client.finalize()


# =======================================================================
# Issue #39 -- the daemon hosts any registered engine, not only Claude
# =======================================================================


class _StubHost:
    """Just enough peers.PeerHost for SessionDaemon.serve(): a presence
    entry it can count clients against and stop. The daemon treats a None
    peer_host as fatal (the registry entry IS the session's
    discoverability), so a stub engine has to have one."""

    def __init__(self) -> None:
        self.clients = 0
        self.stopped = False

    def set_client_count(self, n: int) -> None:
        self.clients = n

    async def stop(self) -> None:
        self.stopped = True

    def list_peers(self) -> list:
        return []


class _StubEngine:
    """A second engine, reduced to what doxa.daemon actually calls.

    Deliberately WITHOUT the nine members in
    ``daemon_mod.MEMORY_RPC_MEMBERS`` -- that absence is the thing under
    test, and it is the same absence doxa.vendors.ChatApiEngine and
    doxa.codex.CodexEngine were measured to have."""

    engine_id = "deepseek"

    def __init__(self, *, cwd, model=None, session_id=None, daemon_socket=None,
                 **_ignored):
        self.cwd = cwd
        self.model = model or "deepseek-chat"
        self.session_id = session_id
        self.daemon_socket = daemon_socket
        self.total_cost_usd = 0.0
        self.last_ctx_percentage = None
        self.peer_host = None
        self.peer_error = None
        self.started = False
        self._oob: asyncio.Queue = asyncio.Queue()

    async def start(self):
        self.started = True
        self.peer_host = _StubHost()
        return EngineEvent("session_started", {
            "session_id": self.session_id, "model": self.model, "cwd": self.cwd,
        })

    async def finalize(self):
        return EngineEvent("session_done", {"indexed": 0})

    async def send(self, prompt):
        yield EngineEvent("turn_started", {"prompt": prompt})
        yield EngineEvent("turn_done", {"is_error": False})

    async def peer_events(self):
        while True:
            yield await self._oob.get()

    async def set_model(self, model):
        self.model = model
        return self.model

    async def set_permission_mode(self, mode):
        raise RuntimeError("the deepseek engine has no permission modes")

    async def answer_needs_input(self, req_id, answer):
        return False

    async def context_usage(self):
        return None

    async def send_peer_message(self, target_prefix, text):
        raise RuntimeError("no peers here")

    def usage_summary(self):
        return {"input_tokens": 0, "output_tokens": 0}

    def belief_count(self):
        return 3

    def disabled_tools(self):
        return []

    def list_peers(self):
        return []


class _StubProvider:
    """An EngineProvider registered for the duration of one test, so the
    daemon really does resolve its engine through doxa.engines rather than
    through an injected factory."""

    def engine_id(self) -> str:
        return "deepseek"

    def engine_display_name(self) -> str:
        return "DeepSeek (stub)"

    def supports(self):
        from doxa.vendors import VENDOR_CAPABILITIES

        return VENDOR_CAPABILITIES

    def new_session(self, **kwargs):
        return _StubEngine(**kwargs)


@pytest.fixture
def stub_deepseek(monkeypatch):
    """Put a stub provider in the registry under the real `deepseek` id and
    put the real one back afterwards -- no network, no credential, and the
    daemon's own lookup path is what is exercised."""
    from doxa import engines as engines_mod

    engines_mod.available()  # force the lazy built-in registration first
    saved = dict(engines_mod._REGISTRY)
    engines_mod.register(_StubProvider())
    yield
    engines_mod._REGISTRY.clear()
    engines_mod._REGISTRY.update(saved)


@contextlib.asynccontextmanager
async def running_vendor_daemon(tmp_path, monkeypatch, engine_id="deepseek"):
    """A served SessionDaemon whose engine came from the REGISTRY -- no
    engine_factory injected, so _build_engine's non-Claude arm is the code
    under test."""
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    daemon = SessionDaemon(
        cwd=str(tmp_path), linger_secs=30.0, engine_id=engine_id,
    )
    serve_task = asyncio.create_task(daemon.serve())
    await asyncio.wait_for(daemon.ready.wait(), 10)
    try:
        yield daemon, serve_task
    finally:
        if not serve_task.done():
            with contextlib.suppress(Exception):
                await daemon._shutdown("test teardown")
                await asyncio.wait_for(serve_task, 5)


@pytest.mark.asyncio
async def test_a_daemon_started_on_a_second_engine_hosts_that_engine(
    tmp_path, monkeypatch, stub_deepseek,
):
    """The headline of issue #39: --engine deepseek really runs deepseek.

    Before this, doxa.daemon built a SessionEngine unconditionally and a
    fleet slot dealt `deepseek:...` ran Claude with a deepseek model name.
    """
    async with running_vendor_daemon(tmp_path, monkeypatch) as (daemon, _):
        assert isinstance(daemon.engine, _StubEngine)
        assert daemon.engine.started
        # The socket is threaded through, which is what puts the
        # daemon_socket marker on the registry entry `doxa attach` reads.
        assert daemon.engine.daemon_socket == str(daemon.socket_path)

        client = EngineClient(str(daemon.socket_path))
        await client.start()
        # The hello frame names the engine, so a client knows before it
        # paints anything.
        assert client.engine_id == "deepseek"
        assert client.engine_capabilities.lore_pickers is False
        assert client.engine_capabilities.detachable is True

        status = await client.refresh_status()
        assert status["engine"] == "deepseek"
        assert status["model"] == "deepseek-chat"
        assert status["running"] is False
        assert status["queued"] == 0
        assert status["lore"] is True
        assert status["belief_count"] == 3
        await client.finalize()


@pytest.mark.asyncio
async def test_no_lore_on_an_engine_without_a_memory_switch_says_so(
    tmp_path, monkeypatch, stub_deepseek, capsys,
):
    """--no-lore reaches an engine that has no memory switch and does
    nothing. Said out loud in the daemon's log rather than swallowed, and
    the status reply still reports what the session actually has."""
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    daemon = SessionDaemon(
        cwd=str(tmp_path), linger_secs=30.0, engine_id="deepseek", lore=False,
    )
    serve_task = asyncio.create_task(daemon.serve())
    await asyncio.wait_for(daemon.ready.wait(), 10)
    try:
        assert "--no-lore has no effect" in capsys.readouterr().err
        client = EngineClient(str(daemon.socket_path))
        await client.start()
        assert (await client.refresh_status())["lore"] is True
        await client.finalize()
    finally:
        if not serve_task.done():
            with contextlib.suppress(Exception):
                await daemon._shutdown("test teardown")
                await asyncio.wait_for(serve_task, 5)


@pytest.mark.asyncio
async def test_a_turn_runs_on_the_second_engine(tmp_path, monkeypatch, stub_deepseek):
    async with running_vendor_daemon(tmp_path, monkeypatch) as (daemon, _):
        client = EngineClient(str(daemon.socket_path))
        await client.start()
        types = [ev.type async for ev in client.send("hello")]
        assert types == ["turn_started", "turn_done"]
        await client.finalize()


@pytest.mark.asyncio
async def test_a_lore_rpc_an_engine_cannot_serve_answers_with_a_typed_error(
    tmp_path, monkeypatch, stub_deepseek,
):
    """Every one of the nine, as an ok=False reply naming the member --
    never an AttributeError out of a dispatch arm, and never an empty list
    that reads as "this session has no beliefs"."""
    async with running_vendor_daemon(tmp_path, monkeypatch) as (daemon, _):
        client = EngineClient(str(daemon.socket_path))
        await client.start()
        for method, member in daemon_mod.MEMORY_RPC_MEMBERS.items():
            reply = await client._call(method)
            assert reply.get("ok") is False, method
            assert daemon_mod.NO_MEMORY_SURFACE in str(reply.get("error")), method
            assert member in str(reply.get("error")), method
        # And the client wrappers turn that into an error the picker
        # prints, rather than an empty picker.
        with pytest.raises(EngineClientError, match=r"no memory surface"):
            await client.list_beliefs()
        with pytest.raises(EngineClientError, match=r"no memory surface"):
            await client.list_pending()
        assert "no memory surface" in str(await client.approve_pending("p1"))
        await client.finalize()


@pytest.mark.asyncio
async def test_status_still_answers_for_an_engine_without_the_pickers(
    tmp_path, monkeypatch, stub_deepseek,
):
    """The other half of the guard: `status` is NOT in the table, because
    every client refreshes it every few seconds and a session that could
    not report its model would be unusable."""
    async with running_vendor_daemon(tmp_path, monkeypatch) as (daemon, _):
        assert "status" not in daemon_mod.MEMORY_RPC_MEMBERS
        client = EngineClient(str(daemon.socket_path))
        await client.start()
        for _ in range(3):
            status = await client.refresh_status()
            assert status["session_id"] == daemon.session_id
        await client.finalize()


def test_an_unknown_engine_exits_two_with_the_registrys_listing(capsys):
    """One usage line, exit 2 -- the same shape every other bad flag gets,
    and the message is doxa.engines' own listing rather than a copy."""
    with pytest.raises(SystemExit) as excinfo:
        daemon_mod.main(["--engine", "nonsense"])
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "unknown engine 'nonsense'" in err
    assert "claude" in err and "deepseek" in err


def test_the_default_engine_is_claude_and_needs_no_flag():
    daemon = SessionDaemon(cwd="/tmp")
    assert daemon.engine_id == "claude"
    assert SessionDaemon(cwd="/tmp", engine_id="  ").engine_id == "claude"
    assert SessionDaemon(cwd="/tmp", engine_id="CODEX").engine_id == "codex"


# -- the argv, byte-identical for a session that does not use the flag --


def _spawn_argv(monkeypatch, tmp_path, **kwargs) -> list:
    """Run spawn_daemon far enough to capture the command line, without
    letting a real daemon start."""
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    captured: list = []

    class _Proc:
        returncode = 0

        def poll(self):
            # "exited during startup" is the fastest way back out of the
            # poll loop, and this test is about the argv, not the wait.
            return 0

    import subprocess as _subprocess

    def fake_popen(cmd, **_kw):
        captured.append(list(cmd))
        return _Proc()

    monkeypatch.setattr(_subprocess, "Popen", fake_popen)
    with contextlib.suppress(RuntimeError):
        daemon_mod.spawn_daemon(cwd=str(tmp_path), wait_secs=0.1, **kwargs)
    assert captured, "spawn_daemon never built a command line"
    argv = captured[0]
    # The session id is minted per call, so two runs of the same arguments
    # differ in exactly that one value and in nothing else. Normalised
    # here so "byte-identical" means what it says about the FLAGS.
    argv[argv.index("--session-id") + 1] = "<sid>"
    return argv


def test_spawn_daemon_appends_the_engine_only_when_it_says_something(
    monkeypatch, tmp_path,
):
    """The same discipline --no-lore, --task and --spawn-depth follow: a
    session that does not use the capability produces the argv this
    function built before the capability existed."""
    baseline = _spawn_argv(monkeypatch, tmp_path)
    assert "--engine" not in baseline

    assert _spawn_argv(monkeypatch, tmp_path, engine=None) == baseline
    assert _spawn_argv(monkeypatch, tmp_path, engine="claude") == baseline
    assert _spawn_argv(monkeypatch, tmp_path, engine="  CLAUDE  ") == baseline

    glm = _spawn_argv(monkeypatch, tmp_path, engine="glm")
    assert glm[len(baseline):] == ["--engine", "glm"]
    assert glm[:len(baseline)] == baseline


# -- running/queued truthfulness (the review defect) --------------------


class _GatedClient(FakeClient):
    """A FakeClient whose turn does not finish until the test says so, so
    "a turn is running right now" is a state the test can observe rather
    than a race it has to win."""

    gate: "asyncio.Event | None" = None

    async def receive_response(self):
        for message in self.script[:-1]:
            yield message
        if self.gate is not None:
            await self.gate.wait()
        yield self.script[-1]


@pytest.mark.asyncio
async def test_a_peer_started_turn_is_reported_running_and_its_queue_is_visible(
    tmp_path, monkeypatch,
):
    """The defect this fixes: doxa.daemon's `running` read only the turns
    the DAEMON started, and `queued` only the daemon's own FIFO. A turn
    started by an arriving peer message runs on the engine's own task, and
    a prompt submitted while it runs lands in the ENGINE's queue -- so a
    session busy answering another agent reported running=false, queued=0,
    and doxa.fleet's is_quiet called it idle."""
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setenv(peers.PEER_INBOUND_TURNS_ENV, "1")
    gate = asyncio.Event()
    created: "list[_GatedClient]" = []

    def factory(options):
        client = _GatedClient(options, script=list(TURN_SCRIPT))
        client.gate = gate
        created.append(client)
        return client

    daemon = SessionDaemon(
        cwd=str(tmp_path), linger_secs=30.0,
        engine_factory=lambda cwd, sid, dsock: SessionEngine(
            cwd=cwd, session_id=sid, client_factory=factory, daemon_socket=dsock,
        ),
    )
    serve_task = asyncio.create_task(daemon.serve())
    await asyncio.wait_for(daemon.ready.wait(), 10)
    try:
        client = EngineClient(str(daemon.socket_path))
        await client.start()
        assert (await client.refresh_status())["running"] is False

        # A DIRECT peer message, with inbound turn-starting armed: the
        # engine starts a turn of its own, and the daemon's _turn_task
        # knows nothing about it.
        daemon.engine._on_peer_frame({
            "from": "abcd1234", "from_title": "peer", "body": "ping",
            "kind": "direct",
        })
        assert daemon._turn_task is None
        for _ in range(50):
            if daemon.engine.turn_running:
                break
            await asyncio.sleep(0.01)
        status = await client.refresh_status()
        assert status["running"] is True, "a peer-started turn is a running turn"

        # A prompt submitted meanwhile lands in the ENGINE's queue.
        reply = await client._prompt("while you are up")
        assert reply.get("ok") is True
        for _ in range(50):
            if daemon.engine.queued_count():
                break
            await asyncio.sleep(0.01)
        status = await client.refresh_status()
        assert status["queued"] == 1
        queued = await client.list_queue()
        assert [row["text"] for row in queued] == ["while you are up"]

        # And it can be cancelled through the same /queue call.
        assert await client.cancel_queued(queued[0]["id"]) is True
        assert (await client.list_queue()) == []
        assert (await client.refresh_status())["queued"] == 0
    finally:
        gate.set()
        if not serve_task.done():
            with contextlib.suppress(Exception):
                await daemon._shutdown("test teardown")
                await asyncio.wait_for(serve_task, 5)


# =======================================================================
# Gated permission modes over the socket (audit finding 2)
# =======================================================================


@pytest.mark.asyncio
async def test_a_bare_socket_client_cannot_arm_a_gated_mode(tmp_path, monkeypatch):
    """Mirrors probe_bypass_mode.py: a connection that reads the hello and
    issues a `call` without ever sending an `attach` frame -- what a
    `python3 -c` one-liner under the model's own Bash tool can open --
    used to flip a bypass-armed session into bypassPermissions with no
    confirmation shown anywhere."""
    monkeypatch.setenv("DOXA_ALLOW_BYPASS", "1")
    config_mod.invalidate()
    async with running_daemon(tmp_path, monkeypatch) as (daemon, created, _):
        assert daemon.engine.bypass_armed
        reader, writer = await asyncio.open_unix_connection(str(daemon.socket_path))
        hello = json.loads(await asyncio.wait_for(reader.readline(), 5))
        assert hello["type"] == "hello"
        writer.write((json.dumps({
            "type": "call", "id": 1, "method": "set_permission_mode",
            "params": {"mode": "bypassPermissions"},
        }) + "\n").encode())
        await writer.drain()
        reply = json.loads(await asyncio.wait_for(reader.readline(), 5))

        assert reply["ok"] is False
        assert "attach" in reply["error"]
        assert daemon.engine.permission_mode == "default"
        assert created[0].permission_modes == []  # the SDK seam never saw it
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


@pytest.mark.asyncio
async def test_an_attached_client_can_still_arm_a_gated_mode_when_idle(
    tmp_path, monkeypatch,
):
    """The other half: shift+tab from the attached TUI reaches
    bypassPermissions on an idle session exactly as before. The gate is on
    who is asking and when, not on the mode existing."""
    monkeypatch.setenv("DOXA_ALLOW_BYPASS", "1")
    config_mod.invalidate()
    async with running_daemon(tmp_path, monkeypatch) as (daemon, created, _):
        client = EngineClient(str(daemon.socket_path))
        await client.start()
        assert await client.set_permission_mode("bypassPermissions") == (
            "bypassPermissions"
        )
        assert daemon.engine.permission_mode == "bypassPermissions"
        assert created[0].permission_modes == ["bypassPermissions"]
        await client.finalize()


@pytest.mark.asyncio
async def test_a_gated_mode_is_refused_mid_turn_and_allowed_once_idle(
    tmp_path, monkeypatch,
):
    """The condition that closes the MODEL as an attacker: a model acts
    only inside a turn, so an escalation request arriving while a turn is
    running did not come from the person at the keyboard. De-escalation is
    never refused, and the same request succeeds once the turn ends."""
    monkeypatch.setenv("DOXA_ALLOW_BYPASS", "1")
    config_mod.invalidate()
    gate = asyncio.Event()
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    slow = _slow_script_client_factory(gate)

    class SlowSwitchable(slow):
        """_slow_script_client_factory's client plus the one control
        request this test is about -- the shared helper has no mode
        setter, and SessionEngine refuses rather than pretending."""

        async def set_permission_mode(self, mode):
            return None

    daemon = SessionDaemon(
        cwd=str(tmp_path), linger_secs=30.0,
        engine_factory=lambda cwd, sid, dsock: SessionEngine(
            cwd=cwd, session_id=sid, client_factory=SlowSwitchable,
            daemon_socket=dsock,
        ),
    )
    serve_task = asyncio.create_task(daemon.serve())
    await asyncio.wait_for(daemon.ready.wait(), 10)
    try:
        client = EngineClient(str(daemon.socket_path))
        await client.start()
        runner = asyncio.create_task(
            _collect(client.send("slow one"))
        )
        for _ in range(200):
            if daemon._running():
                break
            await asyncio.sleep(0.01)
        assert daemon._running()

        with pytest.raises(EngineClientError, match="turn is running or queued"):
            await client.set_permission_mode("bypassPermissions")
        assert daemon.engine.permission_mode == "default"

        # Narrowing is never blocked, even mid-turn.
        assert await client.set_permission_mode("plan") == "plan"

        gate.set()
        await asyncio.wait_for(runner, 5)
        for _ in range(200):
            if not daemon._running():
                break
            await asyncio.sleep(0.01)

        assert await client.set_permission_mode("bypassPermissions") == (
            "bypassPermissions"
        )
        await client.finalize()
    finally:
        if not serve_task.done():
            with contextlib.suppress(Exception):
                await daemon._shutdown("test teardown")
                await asyncio.wait_for(serve_task, 5)


async def _collect(agen):
    return [ev async for ev in agen]


@pytest.mark.asyncio
async def test_the_daemon_refuses_a_session_id_that_is_a_path(tmp_path, monkeypatch):
    """The argv half of verify_transcript_traversal.py's finding: every
    downstream use of this id is a filename -- the transcript, the
    registry entry, the peer socket, the daemon log -- so an id that is
    not a name is refused at the door rather than scattering a session's
    files wherever it pointed."""
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    with pytest.raises(ValueError, match=r"invalid session id"):
        SessionDaemon(cwd=str(tmp_path), session_id="../../../../etc/passwd")
    with pytest.raises(ValueError, match=r"invalid resume id"):
        SessionDaemon(cwd=str(tmp_path), resume="../../elsewhere/leak")
    # A well-formed id is untouched, and an absent one is still minted.
    assert SessionDaemon(
        cwd=str(tmp_path), session_id="4f8e2a91-77bc-4c1d-9a01-000000000000"
    ).session_id == "4f8e2a91-77bc-4c1d-9a01-000000000000"
    assert SessionDaemon(cwd=str(tmp_path)).session_id


# =======================================================================
# Closing during a turn, and a client that stops reading (panel finding 9)
# =======================================================================


@pytest.mark.asyncio
async def test_closing_during_a_turn_ends_send_instead_of_hanging_it(
    tmp_path, monkeypatch,
):
    """`_close()` failed the pending RPCs and ended `peer_events`, and left
    `_turn_queue` alone -- so a `send()` parked on it mid-turn waited for a
    `turn_done` that a closed socket can never deliver. The one caller it
    hung is the one a user is watching."""
    gate = asyncio.Event()
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    daemon = SessionDaemon(
        cwd=str(tmp_path), linger_secs=30.0,
        engine_factory=lambda cwd, sid, dsock: SessionEngine(
            cwd=cwd, session_id=sid, client_factory=_slow_script_client_factory(gate),
            daemon_socket=dsock,
        ),
    )
    serve_task = asyncio.create_task(daemon.serve())
    await asyncio.wait_for(daemon.ready.wait(), 10)
    try:
        client = EngineClient(str(daemon.socket_path))
        await client.start()

        async def run():
            return [ev async for ev in client.send("slow one")]

        task = asyncio.create_task(run())
        for _ in range(200):
            if daemon._running():
                break
            await asyncio.sleep(0.01)
        assert daemon._running()

        client._close()  # the socket goes while the turn is still running

        with pytest.raises(EngineClientError, match=r"closed mid-turn"):
            await asyncio.wait_for(task, 5)
    finally:
        gate.set()
        if not serve_task.done():
            with contextlib.suppress(Exception):
                await daemon._shutdown("test teardown")
                await asyncio.wait_for(serve_task, 5)


@pytest.mark.asyncio
async def test_next_turn_event_returns_none_once_the_client_closes(
    tmp_path, monkeypatch,
):
    """The other consumer of that queue -- doxa.fleet's dispatch/drain
    half -- must be woken too, and its own contract is None, not a raise."""
    async with running_daemon(tmp_path, monkeypatch) as (daemon, _created, _):
        client = EngineClient(str(daemon.socket_path))
        await client.start()
        waiter = asyncio.create_task(client.next_turn_event())
        await asyncio.sleep(0.05)
        client._close()
        assert await asyncio.wait_for(waiter, 5) is None


@pytest.mark.asyncio
async def test_a_client_that_stops_reading_is_dropped_not_buffered_forever(
    tmp_path, monkeypatch,
):
    """`_publish` writes without `drain()` on purpose -- awaiting one slow
    reader would stall the turn for everyone. The cost was that a client
    which stops reading had its frames buffered in the daemon's memory
    without limit: an attached TUI that was SIGSTOPped, a detached client
    on a dead network. Past the bound it is dropped."""
    async with running_daemon(tmp_path, monkeypatch) as (daemon, _created, _):
        client = EngineClient(str(daemon.socket_path))
        await client.start()
        for _ in range(200):
            if daemon._clients:
                break
            await asyncio.sleep(0.01)
        writer = next(iter(daemon._clients))

        # Under the bound: still a client, and it still gets the frame.
        daemon._publish(None, EngineEvent("peer_message", {"body": "one"}))
        assert writer in daemon._clients

        class _Stuffed:
            """A transport whose write buffer is over the cap."""

            def get_write_buffer_size(self):
                return daemon_mod.CLIENT_WRITE_BUFFER_MAX + 1

        monkeypatch.setattr(type(writer), "transport", property(lambda _s: _Stuffed()))
        daemon._publish(None, EngineEvent("peer_message", {"body": "two"}))

        assert writer not in daemon._clients
        # The ring still carries what it missed, which is how it catches
        # up if it ever reattaches.
        assert any(
            f["event"]["data"].get("body") == "two" for f in daemon.ring.since(None)
        )
        with contextlib.suppress(Exception):
            await client.finalize()

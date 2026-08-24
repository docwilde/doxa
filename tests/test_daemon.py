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
async def running_daemon(tmp_path, monkeypatch, linger=30.0, script=None,
                         server_info=None):
    """A served SessionDaemon over a FakeClient in an isolated runtime dir.
    Yields (daemon, created) where created[0] is the FakeClient."""
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    factory, created = factory_with_script(
        list(script or TURN_SCRIPT), server_info=server_info
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

        await client.answer_needs_input(ev.data["id"], {"decision": "deny"})
        from claude_agent_sdk import PermissionResultDeny

        result = await asyncio.wait_for(task, 5)
        assert isinstance(result, PermissionResultDeny)
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
                labels = [label for _rid, label in picker._all_rows]
                seeded = [l for l in labels if l.startswith("belief ")]
                assert len(seeded) == 600
                # ...and no caveat row, because nothing was actually capped.
                assert picker._note == ""

                # Filterable: a belief from the LAST page, reachable by
                # typing, with no further round trip.
                await pilot.press("0", "5", "9", "9")
                await pilot.pause()
                visible = [label for rid, label in picker._rows if rid]
                assert any(l.startswith("belief 0599") for l in visible), (
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
    staged = [f"proposal {i:04d} {filler}" for i in range(120)]
    assert len(json.dumps(staged).encode("utf-8")) > peers.MAX_FRAME_BYTES * 3
    async with running_daemon(tmp_path, monkeypatch) as (daemon, _, _):
        monkeypatch.setattr(
            daemon.engine, "_pending_texts", lambda: list(staged)
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
    staged = [f"proposal {i} {filler}" for i in range(80)]
    async with running_daemon(tmp_path, monkeypatch) as (daemon, _, _):
        monkeypatch.setattr(
            daemon.engine, "_pending_texts", lambda: list(staged)
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
    staged = [f"proposal {i} {filler}" for i in range(200)]
    async with running_daemon(tmp_path, monkeypatch) as (daemon, _, _):
        monkeypatch.setattr(
            daemon.engine, "_pending_texts", lambda: list(staged)
        )
        client = EngineClient(str(daemon.socket_path))
        await client.start()
        assert len(await client.list_pending(limit=37)) == 37
        await client.finalize()


@pytest.mark.asyncio
async def test_the_daemon_exposes_no_approve_or_reject_rpc(tmp_path, monkeypatch):
    """Scope boundary, pinned at the protocol: the read half of the review
    gate crossed the socket in v0.31.0 and the write half deliberately did
    not (docs/plugin-api.md §6). An unknown method must be refused, never
    silently accepted by some future generic handler."""
    async with running_daemon(tmp_path, monkeypatch) as (daemon, _, _):
        client = EngineClient(str(daemon.socket_path))
        await client.start()
        for method in ("approve", "reject", "approve_pending"):
            reply = await client._call(method)
            assert reply.get("ok") is False
            assert "unknown method" in str(reply.get("error"))
        await client.finalize()

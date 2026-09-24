# SPDX-License-Identifier: AGPL-3.0-only
"""The ONE outbound peer path, exercised on every engine that has one.

``doxa.peerdelivery.PeerDelivery`` is where a peer message is charged
against the send limit, written to the peer ledger and flashed on the
status bar -- and until docwilde/doxa#39 only ``doxa.engine``'s Claude
session ran it. ``doxa.vendors`` and ``doxa.codex`` called
``peers.send_message`` directly, so the identical ``/msg`` keystroke was
bounded and recorded in one pane and neither in another, and a DeepSeek
or GLM model had no send tool at all.

What the tests below fix in place is not that the code was moved but that
the three engines cannot DRIFT: a vendor send's ledger row is compared
field by field against a Claude send's, so a future change that gives one
of them a different sender block, a different turn attribution or a
different body has to fail here.

No network and no credentials: the vendor engine is driven through its
stub transport, the peer targets are real AF_UNIX listeners in tmp_path,
and the ledger is a file under a tmp DOXA_HOME.
"""

from __future__ import annotations

import asyncio
import json
import stat
import time

import pytest

from claude_agent_sdk import ResultMessage

from doxa import mcpserver as mcpserver_mod
from doxa import peerledger as pl
from doxa import peers
from doxa.codex import CodexEngine
from doxa.engine import SessionEngine
from doxa.vendors import DEEPSEEK, ChatApiEngine

from tests.fakes import factory_with_script
from tests.test_engines import _FakeProc, _overrides, _script
from tests.test_peer_tools import _peer_entry
from tests.test_vendors import StubTransport, deepseek_tool_script, prose_script

FAKE_KEYS = {"DEEPSEEK_API_KEY": "ds-test-key-0001", "ZAI_API_KEY": "zai-test-key-0002"}


def _isolate(tmp_path, monkeypatch, *, peer_send=True, inbound=False):
    """A private runtime dir, a private DOXA_HOME (and therefore a private
    ledger file), and both peer switches set explicitly rather than
    inherited -- a suite that read the developer's own environment for
    these would pass or fail by machine."""
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DOXA_AGENT_PEER_SEND", "1" if peer_send else "")
    monkeypatch.setenv("DOXA_PEER_INBOUND_TURNS", "1" if inbound else "")
    for name, value in FAKE_KEYS.items():
        monkeypatch.setenv(name, value)


async def _vendor(tmp_path, monkeypatch, *scripts, **kwargs):
    """A started DeepSeek session with a live PeerHost and a stub
    transport. Returns the engine and the transport, because what the
    model was OFFERED is read off the request body the transport saw."""
    _isolate(tmp_path, monkeypatch, **kwargs)
    transport = StubTransport(*(scripts or (prose_script(),)))
    eng = ChatApiEngine(cwd=str(tmp_path), spec=DEEPSEEK, transport=transport)
    await eng.start()
    assert eng.peer_host is not None, (
        "the peer layer must be up or none of these prove anything"
    )
    return eng, transport


async def _claude(tmp_path, monkeypatch, **kwargs):
    """A started Claude session over the same isolated runtime, for the
    side-by-side comparison."""
    _isolate(tmp_path, monkeypatch, **kwargs)
    factory, _created = factory_with_script([
        ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
            num_turns=1, session_id="s", total_cost_usd=0.0,
        ),
    ])
    eng = SessionEngine(cwd=str(tmp_path), client_factory=factory)
    await eng.start()
    assert eng.peer_host is not None
    return eng


async def _run_turn(eng, prompt="hi") -> list:
    return [event async for event in eng.send(prompt)]


def _offered(transport) -> "set[str]":
    return {t["function"]["name"] for t in transport.last_body["tools"]}


def _drain(queue) -> list:
    """Every event sitting on an engine's out-of-band queue right now."""
    out = []
    while not queue.empty():
        out.append(queue.get_nowait())
    return out


# -- the tool surface --------------------------------------------------


async def test_a_vendor_session_is_offered_peer_send_when_the_switch_is_on(
    tmp_path, monkeypatch,
):
    """The claim this release retires. A DeepSeek session hosts a
    PeerHost, receives frames and publishes presence, and until now had no
    outbound path at all -- so operators._peer_send_configured's seam gate
    correctly kept the tool away from it. The seam exists now, so the
    user's own switch is the only thing left between a vendor model and a
    send."""
    eng, transport = await _vendor(tmp_path, monkeypatch, peer_send=True)
    try:
        await _run_turn(eng)
        offered = _offered(transport)
        assert "peer_send" in offered
        assert {"peer_list", "peer_history"} <= offered, (
            "discovery was never gated on the seam -- if it vanished, this "
            "test is passing for the wrong reason"
        )
    finally:
        await eng.finalize()


async def test_a_vendor_session_is_not_offered_peer_send_when_the_switch_is_off(
    tmp_path, monkeypatch,
):
    """Off is the default and off means NOT OFFERED, not refused: a tool
    the model cannot see is a tool the model cannot call. The seam being
    wired must not turn the user's switch into decoration."""
    eng, transport = await _vendor(tmp_path, monkeypatch, peer_send=False)
    try:
        await _run_turn(eng)
        offered = _offered(transport)
        assert "peer_send" not in offered
        assert "lore_remember" in offered, (
            "the write registry must still be projected, or the absence "
            "above says nothing about peer_send's own gate"
        )
    finally:
        await eng.finalize()


# -- a model send, end to end -----------------------------------------


async def test_a_model_peer_send_from_a_vendor_session_is_charged_and_ledgered(
    tmp_path, monkeypatch,
):
    """The whole path in one test: the model names peer_send in a
    tool_calls delta, ToolGate executes it, the operator hands the
    validated request to this engine's seam, and PeerDelivery charges the
    limit and writes ONE record naming who was reached.

    Driven through the transport rather than by calling the seam, because
    the defect being fixed was that the tool never reached the model at
    all."""
    eng, transport = await _vendor(
        tmp_path, monkeypatch,
        deepseek_tool_script(
            name="peer_send",
            args=json.dumps({"to": "target01", "body": "ready"}),
        ),
        prose_script(),
    )
    try:
        _peer_entry(tmp_path / "rt", "target01", scope="/repo/t")

        events = await _run_turn(eng, "say hello to target01")

        results = [e for e in events if e.type == "tool_result"]
        assert results and results[0].data["name"] == "peer_send"
        assert results[0].data["is_error"] is False, results[0].data

        assert eng._peer_delivery.limiter.used_in_window() == 1, (
            "a model send that charged nothing is the unbounded path this "
            "whole change exists to close"
        )
        record = eng._peer_delivery.ledger.recent(limit=1)[0]
        assert record.body == "ready"
        assert list(record.to) == ["target01"]
        assert record.sender.session == eng.session_id
        assert record.sender.engine == "deepseek", (
            "the ledger has to say WHICH engine sent it -- the mixed-vendor "
            "experiment reads exactly this field"
        )
        assert record.turn.state == "running", (
            "sent from inside a turn, and the ledger says so"
        )
        started = [e for e in events if e.type == "turn_started"][0]
        assert record.turn.id == started.data["turn_id"], (
            "the transcript's turn id and the ledger's must be one string, "
            "or a reader cannot join a send to the turn that made it"
        )
    finally:
        await eng.finalize()


async def test_a_vendor_send_and_a_claude_send_write_the_same_shaped_row(
    tmp_path, monkeypatch,
):
    """Field by field, because the experiment's whole method is comparing
    arms: a mesh graph whose DeepSeek rows carried a different sender
    block, a different turn attribution or a different kind from its
    Claude rows would be measuring the engine rather than the agents."""
    claude = await _claude(tmp_path, monkeypatch)
    vendor, _transport = await _vendor(tmp_path, monkeypatch)
    try:
        _peer_entry(tmp_path / "rt", "target01", scope="/repo/t")

        await claude._tool_peer_send({"to": "target01", "body": "same words"})
        await vendor._peer_delivery.tool_send({"to": "target01", "body": "same words"})

        rows = vendor._peer_delivery.ledger.recent(limit=2)
        assert len(rows) == 2, "both sends must have been recorded"
        by_engine = {row.sender.engine: row for row in rows}
        assert set(by_engine) == {"doxa", "deepseek"}
        one, two = by_engine["doxa"], by_engine["deepseek"]

        # Everything that is a property of the MESSAGE must match; only
        # the three fields that name the sender may differ.
        for field in ("body", "body_sha256", "kind", "in_reply_to"):
            assert getattr(one, field) == getattr(two, field), field
        assert list(one.to) == list(two.to) == ["target01"]
        assert one.turn.state == two.turn.state == "idle"
        assert one.sender.repo == two.sender.repo, (
            "both sessions are in the same cwd, so the sender's repo -- "
            "which is what a reader uses to tell which project a message "
            "is about -- must agree"
        )
        assert one.sender.session != two.sender.session
    finally:
        await vendor.finalize()
        await claude.finalize()


async def test_a_vendor_msg_is_ledgered_like_a_human_typed_claude_one(
    tmp_path, monkeypatch,
):
    """/msg from a DeepSeek pane. It used to call peers.send_message
    directly: delivered, unlimited, and absent from the mesh graph, which
    made the graph a picture of some panes rather than of the fleet."""
    eng, _transport = await _vendor(tmp_path, monkeypatch, peer_send=False)
    try:
        _peer_entry(tmp_path / "rt", "target01", scope="/repo/t")

        peer = await eng.send_peer_message("target01", "from a human")

        assert peer.session_id == "target01"
        assert eng._peer_delivery.limiter.used_in_window() == 1
        record = eng._peer_delivery.ledger.recent(limit=1)[0]
        assert record.body == "from a human"
        assert record.sender.session == eng.session_id
        assert record.turn.state == "idle", (
            "typed outside a turn, and that is a measurement: an unprompted "
            "message is the shape a coordinator has"
        )
        lit = [ev for ev in _drain(eng._peer_queue) if ev.type == "peer_sent"]
        assert lit, "the status bar's send light never flashed"
        assert lit[0].data["to"] == ["target01"]
    finally:
        await eng.finalize()


async def test_a_send_keeps_the_turn_it_started_in_across_transport_awaits(
    tmp_path, monkeypatch,
):
    """The ledger must not read a later engine turn after delivery waits."""
    eng, _transport = await _vendor(tmp_path, monkeypatch, peer_send=True)
    try:
        _peer_entry(tmp_path / "rt", "target01", scope="/repo/t")
        eng._turn_id = "turn-before-send"

        async def delayed_send(*_args, **_kwargs):
            eng._turn_id = "turn-after-send"

        monkeypatch.setattr("doxa.peerdelivery.peers_mod.send_message", delayed_send)
        await eng.send_peer_message("target01", "keep attribution")

        record = eng._peer_delivery.ledger.recent(limit=1)[0]
        assert record.turn.id == "turn-before-send"
    finally:
        await eng.finalize()


async def test_a_vendor_msg_that_the_limit_refuses_raises_the_error_msg_handles(
    tmp_path, monkeypatch,
):
    """/msg's caller (doxa.session.commands) catches PeerSendError and
    nothing else. A SendRefused escaping as itself would reach the pane as
    an unhandled exception for a limit doing its job."""
    eng, _transport = await _vendor(tmp_path, monkeypatch, peer_send=False)
    try:
        _peer_entry(tmp_path / "rt", "target01", scope="/repo/t")
        eng._peer_delivery.limiter.limits = pl.SendLimits(
            per_turn=64, per_window=1, window_secs=60.0,
        )
        await eng.send_peer_message("target01", "one")

        with pytest.raises(peers.PeerSendError, match=r"peer send refused"):
            await eng.send_peer_message("target01", "two")

        assert eng._peer_delivery.ledger.count() == 1, (
            "a refused send writes no record -- the ledger counts deliveries "
            "that happened"
        )
    finally:
        await eng.finalize()



# -- inbound: a message that starts a turn -----------------------------


async def test_an_arriving_message_starts_a_turn_on_a_vendor_session(
    tmp_path, monkeypatch,
):
    """The other half of the mesh. A DeepSeek session used to append every
    frame to a pending list and wait for a human to type something; with
    the switch armed it now wakes, exactly as a Claude session does, and
    the model is asked in the same words."""
    eng, transport = await _vendor(tmp_path, monkeypatch, inbound=True)
    try:
        eng._on_peer_frame({
            "from_id": "waker", "from_title": "waker", "sent_at": "now",
            "body": "can you take the parser?", "from_repo": "/repo/waker",
            "kind": "direct",
        })
        assert eng._turn_running is True, (
            "set synchronously, before the task's first step -- otherwise a "
            "prompt submitted in that window starts a second turn"
        )
        assert eng._queued_turn_task is not None
        await eng._queued_turn_task

        sent = transport.last_body["messages"][-1]["content"]
        assert sent.startswith(peers.PEER_TURN_INTRO)
        assert "can you take the parser?" in sent
        assert peers.PEER_UNTRUSTED_INTRO in sent, (
            "a peer-started turn's body still crosses the untrusted marker"
        )
        assert eng._pending_peer_frames == [], (
            "a frame that started a turn must not ALSO ride the next one -- "
            "that is the same message delivered twice"
        )
    finally:
        await eng.finalize()


async def test_a_peer_started_vendor_turn_is_attributable(tmp_path, monkeypatch):
    """Spend needs a traceable cause on every engine, not only the one
    that can price it. The turn says so in the same three places a Claude
    peer-started turn does, all three derived from one string: the prompt
    the model read, the event the transcript renders, and the turn id
    every ledger record written during that turn carries."""
    eng, _transport = await _vendor(tmp_path, monkeypatch, inbound=True)
    try:
        eng._on_peer_frame({
            "from_id": "cause01", "from_title": "the cause", "sent_at": "t0",
            "body": "please look at the parser", "from_repo": "/repo/cause",
            "kind": "direct",
        })
        await eng._queued_turn_task

        started = [ev for ev in _drain(eng._peer_queue) if ev.type == "turn_started"]
        assert started, "no turn_started reached the transcript"
        data = started[0].data
        assert data["peer_started"] is True
        assert "the cause" in str(data["peer_origin"])
        assert "cause01" in str(data["peer_origin"])
        assert str(data["turn_id"]).startswith("peer-"), (
            "the ledger's half of the attribution: every record written in "
            "this turn carries this id, so an inbound cause is readable "
            "without a second record type"
        )
    finally:
        await eng.finalize()


async def test_an_arriving_message_queues_when_a_vendor_turn_is_running(
    tmp_path, monkeypatch,
):
    """Into the EXISTING bounded FIFO, as a second producer. A parallel
    queue would disagree with this one about order the first time both had
    something waiting, and the bound would stop being a bound."""
    eng, transport = await _vendor(tmp_path, monkeypatch, inbound=True)
    try:
        eng._turn_running = True
        eng._on_peer_frame({
            "from_id": "patient", "from_title": "patient", "sent_at": "now",
            "body": "when you get a moment", "from_repo": "/repo/patient",
            "kind": "direct",
        })

        assert len(eng._prompt_queue) == 1
        queued = eng._prompt_queue.snapshot()[0]["text"]
        assert queued.startswith(peers.PEER_TURN_INTRO), (
            "the attribution rides the TEXT, which is what makes it survive "
            "the queue -- PromptQueue carries an id and a string, nothing else"
        )
        assert transport.requests == [], "the running turn was not interrupted"
        assert eng._pending_peer_frames == []
        queued_event = [
            ev for ev in _drain(eng._peer_queue) if ev.type == "prompt_queued"
        ]
        assert queued_event, "nothing told the user a peer message is waiting"
        assert queued_event[0].data["peer_started"] is True
        assert "patient" in str(queued_event[0].data["peer_origin"]), (
            "the queue line is the only thing shown between arrival and the "
            "turn starting, and a queue line shows the prompt's first 120 "
            "characters -- boilerplate identical on every one of these"
        )
    finally:
        eng._turn_running = False
        await eng.finalize()


async def test_an_arriving_message_starts_no_vendor_turn_while_the_switch_is_off(
    tmp_path, monkeypatch,
):
    """The default, and the behaviour this engine has always had.
    Receiving and being woken are different grants."""
    eng, transport = await _vendor(tmp_path, monkeypatch, inbound=False)
    try:
        eng._on_peer_frame({
            "from_id": "quiet", "from_title": "quiet", "sent_at": "now",
            "body": "no rush", "from_repo": "/repo/quiet", "kind": "direct",
        })

        assert eng._turn_running is False
        assert len(eng._prompt_queue) == 0
        assert eng._queued_turn_task is None
        assert transport.requests == []
        assert len(eng._pending_peer_frames) == 1, (
            "it is not dropped either -- it rides the next turn the user "
            "starts"
        )
    finally:
        await eng.finalize()


async def test_a_broadcast_never_wakes_a_vendor_session(tmp_path, monkeypatch):
    """The one rule docs/plans/emergent-organization.md states before it
    states anything else, and it has to hold on every engine: at N=32 a
    broadcast that started turns would wake the entire fleet in a single
    step, and a reply-broadcast round is 992 messages."""
    eng, transport = await _vendor(tmp_path, monkeypatch, inbound=True)
    try:
        eng._on_peer_frame({
            "from_id": "loud", "from_title": "loud", "sent_at": "now",
            "body": "everyone please respond", "from_repo": "/repo/loud",
            "kind": "broadcast",
        })

        assert eng._turn_running is False, "a broadcast must never wake a session"
        assert len(eng._prompt_queue) == 0, "not even into the queue"
        assert transport.requests == [], "nothing was sent to the model"
        assert len(eng._pending_peer_frames) == 1, (
            "it is not dropped either -- it rides the next turn the user "
            "starts, which is exactly what it did before any of this"
        )
    finally:
        await eng.finalize()


async def test_a_typed_prompt_queues_behind_a_peer_started_vendor_turn(
    tmp_path, monkeypatch,
):
    """The consequence of a peer being able to start a turn: "a turn is
    already running" is no longer a state only the human could have
    created, so send() has to queue rather than race. Before this the
    vendor engine had no such check at all."""
    eng, _transport = await _vendor(tmp_path, monkeypatch, inbound=True)
    try:
        eng._turn_running = True

        events = [ev async for ev in eng.send("meanwhile, from the human")]

        assert [ev.type for ev in events] == ["prompt_queued"]
        assert events[0].data["position"] == 1
        assert await eng.list_queue() == [
            {"id": events[0].data["id"], "text": "meanwhile, from the human"},
        ]
        assert await eng.cancel_queued(events[0].data["id"]) is True
        assert await eng.cancel_queued(events[0].data["id"]) is False, (
            "a stale id is not an error"
        )
    finally:
        eng._turn_running = False
        await eng.finalize()


# -- Codex, whose model sends through the engine's control socket -------


async def _codex(tmp_path, monkeypatch, *, lines=None, **kwargs):
    """A started Codex session, and the list its spawned fake processes
    land in -- what the model was asked is the bytes written to a child's
    stdin, so the child has to be reachable.

    ``shutil.which`` is faked because start() refuses without the CLI on
    PATH, and no test here may depend on one being installed."""
    _isolate(tmp_path, monkeypatch, **kwargs)
    monkeypatch.setattr("doxa.codex.shutil.which", lambda _name: "/usr/bin/codex")
    scripts = list(lines if lines is not None else [])
    procs: list = []

    async def exec_factory(*_argv, **_kwargs):
        proc = _FakeProc(scripts[len(procs)] if len(procs) < len(scripts) else [])
        procs.append(proc)
        return proc

    eng = CodexEngine(cwd=str(tmp_path), exec_factory=exec_factory)
    await eng.start()
    assert eng.peer_host is not None
    return eng, procs


async def test_a_codex_msg_is_charged_and_ledgered(tmp_path, monkeypatch):
    """``/msg`` on the shared path: charged, recorded, lit. The model's
    own ``peer_send`` lands on the same object through the control socket
    (below), which is what makes "one limiter, one ledger, one status
    bar" true of the session rather than of one keystroke."""
    eng, _procs = await _codex(tmp_path, monkeypatch)
    try:
        _peer_entry(tmp_path / "rt", "target01", scope="/repo/t")

        peer = await eng.send_peer_message("target01", "from a codex pane")

        assert peer.session_id == "target01"
        assert eng._peer_delivery.limiter.used_in_window() == 1
        record = eng._peer_delivery.ledger.recent(limit=1)[0]
        assert record.body == "from a codex pane"
        assert record.sender.session == eng.session_id
        assert record.sender.engine == "codex"
        assert record.turn.state == "idle"
        lit = [ev for ev in _drain(eng._peer_queue) if ev.type == "peer_sent"]
        assert lit and lit[0].data["to"] == ["target01"], (
            "the status bar's send light never flashed"
        )
    finally:
        await eng.finalize()


async def _control(path, payload, timeout=10.0) -> dict:
    """One request on an engine control socket, exactly as a sidecar
    makes it: one JSON line in, one JSON line out, connection closed."""
    reader, writer = await asyncio.open_unix_connection(str(path))
    try:
        writer.write(json.dumps(payload).encode("utf-8") + b"\n")
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout)
    finally:
        writer.close()
        await writer.wait_closed()
    return json.loads(line)


async def _receiver(tmp_path, session_id="receiver-01"):
    """A real second session's PeerHost, so a delivery below crosses a
    real peer socket rather than a stand-in. Its inbox is the list its
    on_message callback appends to -- already scrubbed by PeerHost's own
    receive path, like every other frame."""
    inbox: "list[dict]" = []
    cwd = tmp_path / "other-repo"
    cwd.mkdir(exist_ok=True)
    host = peers.PeerHost(
        session_id=session_id, cwd=str(cwd), title="receiver",
        on_message=inbox.append, heartbeat_secs=3600.0,
    )
    await host.start()
    return host, inbox


async def _until(predicate, timeout=5.0):
    """Wait for something another task does. The peer protocol is
    fire-and-forget -- the sender's send_message returns once the frame is
    written, and the receiver fires on_message after reading to EOF -- so
    a delivery is observed, never awaited."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


async def test_a_sidecar_peer_send_is_performed_by_the_engine(
    tmp_path, monkeypatch,
):
    """THE claim this change makes. A request arriving on the control
    socket is performed by the ENGINE, through the same PeerDelivery
    ``/msg`` uses: the frame crosses a real peer socket, the limiter is
    charged once, ONE ledger row is appended naming engine="codex" and the
    SIDECAR's turn, and peer_sent lights the status bar.

    The turn assertion is the sharp one. The engine is idle -- no turn is
    running here -- so a row reading ``idle`` would mean the sidecar's
    turn id was dropped and every Codex model send would be recorded as
    unprompted."""
    eng, _procs = await _codex(tmp_path, monkeypatch, peer_send=True)
    host, inbox = await _receiver(tmp_path)
    try:
        assert eng._turn_id is None, "the engine must be idle for this to bite"

        reply = await _control(eng._engine_control.path, {
            "op": "peer_send",
            "turn_id": "t-sidecar",
            "request": {"body": "ready", "to": "receiver-01",
                        "broadcast": False, "in_reply_to": None},
        })

        assert reply["ok"] is True, reply
        result = reply["result"]
        assert [d["session_id"] for d in result["delivered_to"]] == ["receiver-01"]

        assert await _until(lambda: inbox), "no frame reached the peer socket"
        assert inbox[0]["body"] == "ready"
        assert inbox[0]["from_id"] == eng.session_id

        assert eng._peer_delivery.limiter.used_in_window() == 1
        rows = eng._peer_delivery.ledger.recent(limit=5)
        assert len(rows) == 1, "one send, one row"
        assert rows[0].sender.engine == "codex"
        assert rows[0].sender.session == eng.session_id
        assert list(rows[0].to) == ["receiver-01"]
        assert rows[0].turn.id == "t-sidecar"
        assert rows[0].turn.state == "running"

        lit = [ev for ev in _drain(eng._peer_queue) if ev.type == "peer_sent"]
        assert lit and lit[0].data["to"] == ["receiver-01"], (
            "the status bar's send light never flashed"
        )
    finally:
        await host.stop()
        await eng.finalize()


async def test_the_control_socket_serves_one_op_and_refuses_every_other(
    tmp_path, monkeypatch,
):
    """The surface is one operation. Anything else is refused BY NAME --
    a socket that quietly ignored an unknown op would be a socket whose
    surface nobody can state -- and a refusal spends nothing."""
    eng, _procs = await _codex(tmp_path, monkeypatch, peer_send=True)
    path = eng._engine_control.path
    try:
        unknown = await _control(path, {"op": "peer_history", "request": {}})
        assert unknown["ok"] is False
        assert "unsupported op" in unknown["error"]
        assert "'peer_history'" in unknown["error"]

        shapeless = await _control(path, {"op": "peer_send", "request": "ready"})
        assert shapeless["ok"] is False
        assert "operator's request object" in shapeless["error"]

        not_json = await _control(path, [1, 2, 3])
        assert not_json["ok"] is False
        assert "not a JSON object" in not_json["error"]

        assert eng._peer_delivery.limiter.used_in_window() == 0
        assert eng._peer_delivery.ledger.recent(limit=5) == []
    finally:
        await eng.finalize()


async def test_the_switch_off_is_told_to_the_sidecar_and_enforced_at_the_socket(
    tmp_path, monkeypatch,
):
    """Two halves of one switch. The sidecar is told ``0`` and therefore
    offers no tool at all (tests/test_mcpserver.py proves the absence);
    the socket refuses anyway, because the setting lives in the user's
    environment or config file and can be turned off DURING a session --
    a socket that had captured the answer at start() would keep sending
    after the user said stop."""
    eng, _procs = await _codex(tmp_path, monkeypatch, peer_send=False)
    try:
        overrides = " ".join(eng._mcp_overrides())
        assert f'{mcpserver_mod.ENV_PEER_SEND}="0"' in overrides, overrides
        assert mcpserver_mod.ENV_ENGINE_SOCKET not in overrides, (
            "a sidecar that may not send is told nothing about where to"
        )
        assert mcpserver_mod.ENV_TURN_ID not in overrides

        reply = await _control(eng._engine_control.path, {
            "op": "peer_send", "turn_id": "t-1",
            "request": {"body": "ready", "to": "receiver-01"},
        })
        assert reply["ok"] is False
        assert "off on this DOXA install" in reply["error"]
    finally:
        await eng.finalize()


async def test_the_engine_socket_is_0600_and_finalize_unlinks_it(
    tmp_path, monkeypatch,
):
    """Same-user, enforced by the filesystem -- the boundary the peer
    sockets already rest on, and the reason there is no token to put on
    argv. Gone at finalize: a session that ended must leave nothing
    behind for anything to connect to."""
    eng, _procs = await _codex(tmp_path, monkeypatch, peer_send=True)
    path = eng._engine_control.path
    assert path.exists() and eng._engine_control.running is True
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.parent == peers.runtime_dir(), "beside the peer socket"

    await eng.finalize()

    assert not path.exists()
    assert eng._engine_control.running is False


async def test_the_engine_socket_does_not_cost_the_one_argv_shape(
    tmp_path, monkeypatch,
):
    """The two new overrides are env entries like every other one, so the
    resume argv still differs from the first turn's only by
    ``resume <id>``. A second turn whose command line had drifted would
    be a session whose tools changed shape after the first."""
    eng, _procs = await _codex(tmp_path, monkeypatch, peer_send=True)
    try:
        eng.thread_id = "th-x"
        eng._turn_id = "t-1"
        first, resume = eng._argv(True), eng._argv(False)
        assert resume[:2] == first[:2] == ["codex", "exec"]
        assert resume[2:4] == ["resume", "th-x"]
        assert resume[4:] == first[2:]

        over = _overrides(first)
        prefix = f"mcp_servers.{mcpserver_mod.SERVER_NAME}.env"
        assert over[f"{prefix}.{mcpserver_mod.ENV_PEER_SEND}"] == '"1"'
        assert over[f"{prefix}.{mcpserver_mod.ENV_ENGINE_SOCKET}"] == json.dumps(
            str(eng._engine_control.path)
        )
        assert over[f"{prefix}.{mcpserver_mod.ENV_TURN_ID}"] == '"t-1"'
    finally:
        await eng.finalize()


async def test_a_codex_session_is_offered_the_send_tool(tmp_path, monkeypatch):
    """The gap this release closes. ``peer_send_tool`` is True because the
    sidecar has somewhere to forward to, not because ``/msg`` works --
    those are different grants and the capability map says so with
    different fields."""
    from doxa.codex import CODEX_CAPABILITIES, _peer_delivery_available

    eng, _procs = await _codex(tmp_path, monkeypatch, peer_send=True)
    try:
        assert CODEX_CAPABILITIES.mcp_tools is True
        assert CODEX_CAPABILITIES.peer_send_tool is True
        assert CODEX_CAPABILITIES.peer_messaging is True
        assert _peer_delivery_available() is True
        assert eng._peer_send_armed() is True
        overrides = " ".join(eng._mcp_overrides())
        assert f'{mcpserver_mod.ENV_PEER_SEND}="1"' in overrides, overrides
    finally:
        await eng.finalize()


async def test_an_arriving_message_starts_a_codex_turn(tmp_path, monkeypatch):
    """A Codex turn is one `codex exec resume` process, which is what
    makes waking one possible at all: it costs a spawn, not an injection
    into a conversation DOXA does not hold."""
    eng, procs = await _codex(
        tmp_path, monkeypatch, inbound=True,
        lines=[_script(
            {"type": "item.completed",
             "item": {"id": "a", "type": "agent_message", "text": "on it"}},
            {"type": "turn.completed", "usage": {}},
        )],
    )
    try:
        eng._on_peer_frame({
            "from_id": "cause01", "from_title": "the cause", "sent_at": "t0",
            "body": "please look at the parser", "from_repo": "/repo/cause",
            "kind": "direct",
        })
        assert eng._turn_running is True
        await eng._queued_turn_task

        assert len(procs) == 1, "no codex process was spawned for the turn"
        sent = procs[0].stdin.written.decode()
        # A first turn on this engine opens with the LORE snapshot (the
        # only channel Codex has for it), so the peer intro follows the
        # memory block rather than the prompt's first byte; it still
        # precedes the message it explains.
        assert peers.PEER_TURN_INTRO in sent
        assert sent.index(peers.PEER_TURN_INTRO) < sent.index("please look at the parser")
        assert peers.PEER_UNTRUSTED_INTRO in sent, (
            "a peer-started turn's body still crosses the untrusted marker"
        )

        started = [ev for ev in _drain(eng._peer_queue) if ev.type == "turn_started"]
        assert started, "no turn_started reached the transcript"
        data = started[0].data
        assert data["peer_started"] is True
        assert "the cause" in str(data["peer_origin"])
        assert str(data["turn_id"]).startswith("peer-"), (
            "every ledger record written in this turn carries this id"
        )
        assert eng._pending_peer_frames == []
    finally:
        await eng.finalize()


async def test_a_broadcast_never_wakes_a_codex_session(tmp_path, monkeypatch):
    eng, procs = await _codex(tmp_path, monkeypatch, inbound=True)
    try:
        eng._on_peer_frame({
            "from_id": "loud", "from_title": "loud", "sent_at": "now",
            "body": "everyone please respond", "from_repo": "/repo/loud",
            "kind": "broadcast",
        })

        assert eng._turn_running is False
        assert procs == [], "no codex process may be spawned by a broadcast"
        assert len(eng._pending_peer_frames) == 1
    finally:
        await eng.finalize()


async def test_an_arriving_message_starts_no_codex_turn_while_the_switch_is_off(
    tmp_path, monkeypatch,
):
    eng, procs = await _codex(tmp_path, monkeypatch, inbound=False)
    try:
        eng._on_peer_frame({
            "from_id": "quiet", "from_title": "quiet", "sent_at": "now",
            "body": "no rush", "from_repo": "/repo/quiet", "kind": "direct",
        })

        assert eng._turn_running is False
        assert procs == []
        assert len(eng._pending_peer_frames) == 1
    finally:
        await eng.finalize()


# -- the capability field that says which of these is true --------------


def test_peer_send_tool_says_which_engines_can_offer_the_model_a_send():
    """``peer_messaging`` means ``/msg`` works and is True on all four.
    ``peer_send_tool`` means the MODEL can send, which is a different
    grant -- a map with one field for both would have to lie about one of
    them. All four answer True to both now; the field stays because the
    two questions stay different, and because a vendor session whose tool
    surface failed to import still answers False to the second (below).

    Read off the registry rather than the modules, because the registry is
    what ``/engine`` and the README table read."""
    from doxa import engines as engines_mod

    can_send = {
        engine_id: engines_mod.get(engine_id).supports().peer_send_tool
        for engine_id in engines_mod.available()
    }
    assert can_send == {
        "claude": True, "deepseek": True, "glm": True, "codex": True,
    }
    for engine_id in can_send:
        assert engines_mod.get(engine_id).supports().peer_messaging is True, (
            f"{engine_id}: /msg works everywhere -- if this flips, the new "
            "field is measuring the wrong thing"
        )


async def test_a_vendor_session_whose_tool_surface_failed_stops_claiming_the_send_tool(
    tmp_path, monkeypatch,
):
    """The narrowed posture has to narrow this field too. The seam
    survives an import failure -- PeerDelivery is built in __init__ and
    /msg still works -- but there is no tool surface left to offer the
    operator ON, and a handle still claiming peer_send_tool would be
    describing a tool this session cannot project."""
    _isolate(tmp_path, monkeypatch, peer_send=True)

    def _boom(*_args, **_kwargs):
        raise ImportError("no tool surface here")

    monkeypatch.setattr("doxa.vendors.operator_tools", _boom)
    eng = ChatApiEngine(cwd=str(tmp_path), spec=DEEPSEEK, transport=StubTransport())
    await eng.start()
    try:
        assert eng._tools == []
        assert eng.engine_capabilities.mcp_tools is False
        assert eng.engine_capabilities.peer_send_tool is False
        assert eng.engine_capabilities.peer_messaging is True, (
            "/msg is DOXA's own layer and does not need the tool surface"
        )
        assert eng._peer_delivery is not None, (
            "the outbound path is built in __init__ and is not what failed"
        )
    finally:
        await eng.finalize()

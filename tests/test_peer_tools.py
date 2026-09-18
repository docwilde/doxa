# SPDX-License-Identifier: AGPL-3.0-only
"""The peer TOOLS -- peer_list, peer_send, peer_history -- and the two
directions of traffic they create.

Every test here is named for the failure it catches, because this is the
release that retires DOXA's oldest claim about itself: until now the model
had no send tool, and ``docs/manual.md`` and ``README.md`` both stated
that as a property of the system. Giving a model ``peer_send`` means it
can reach another live session's context on its own initiative, possibly
in a different repository. That was accepted on ONE condition -- that it
is never silent -- so the tests below are not coverage, they are the
conditions themselves:

* the send tool is absent from the default projection, and absent again
  when its switch is off (twice, because they are two different gates and
  either one alone would be a single point of failure);
* an ambiguous addressee is refused NAMING the candidates, never
  delivered to a guess;
* a broadcast costs one delivery per recipient and starts no turn
  anywhere;
* an arriving message starts a turn when idle and queues when busy;
* a turn a peer started says so;
* a refusal says when it resets, because an agent told why can reason
  about it and one silently throttled just retries;
* peer_history answers about THIS session and nothing else;
* a credential in a body is scrubbed on disk while its hash still
  identifies the message.

Everything runs against a tmp_path DOXA_RUNTIME_DIR and DOXA_HOME, so no
test here touches the machine's real registry, sockets or ledger.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket as socket_mod

import pytest

from claude_agent_sdk import ResultMessage

from doxa import operators as ops
from doxa import peerledger as pl
from doxa import peers
from doxa.engine import SessionEngine
from tests.fakes import factory_with_script

# The same credential shape doxa's other trust tests use.
FAKE_AWS_KEY = "AKIAABCDEFGHIJKLMNOP"

_OPEN_SOCKETS: list = []


def _listening(path):
    """A real AF_UNIX listener, so a probed registry read sees a
    connectable socket. Kept in a module-level list so it outlives the
    call -- a closed socket is what a dead session looks like."""
    sock = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
    sock.bind(str(path))
    sock.listen(128)
    _OPEN_SOCKETS.append(sock)
    return sock


def _peer_entry(runtime, session_id, *, scope="/some/other/repo", title="t",
                model="sonnet", listening=True):
    """One live registry entry. ``scope`` defaults to a repo that is NOT
    the caller's, because cross-repo addressing is the point now."""
    entry = {
        "session_id": session_id,
        "pid": os.getpid(),
        "socket_path": str(runtime / f"peer-{session_id}.sock"),
        "cwd": scope,
        "repo_root": scope,
        "title": title,
        "model": model,
        "engine": "doxa",
        "started_at": peers._iso_now(),
        "heartbeat_at": peers._iso_now(),
    }
    path = runtime / "registry" / f"{session_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entry), encoding="utf-8")
    if listening:
        _listening(entry["socket_path"])
    return entry


async def _engine(tmp_path, monkeypatch, *, peer_send=True, inbound=False):
    """A started SessionEngine with its own runtime dir and ledger."""
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DOXA_AGENT_PEER_SEND", "1" if peer_send else "")
    monkeypatch.setenv("DOXA_PEER_INBOUND_TURNS", "1" if inbound else "")
    factory, created = factory_with_script([
        ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
            num_turns=1, session_id="s", total_cost_usd=0.0,
        ),
    ])
    engine = SessionEngine(cwd=str(tmp_path), client_factory=factory)
    await engine.start()
    assert engine.peer_host is not None, "the peer layer must be up or these prove nothing"
    return engine, created


def _drain(queue):
    """Every event sitting on the engine's out-of-band queue right now."""
    out = []
    while not queue.empty():
        out.append(queue.get_nowait())
    return out


# -- the two gates on peer_send ---------------------------------------


def test_peer_send_is_absent_from_the_default_tool_projection():
    """The default projection is what a caller gets by asking for tools
    with no arguments. A write-capable tool that appeared there would be
    offered to anything that ever forgot to say include_write=False --
    which is the whole reason WRITE_OPERATORS is a separate registry."""
    names = {tool.name for tool in ops.to_sdk_tools(lambda name, args: {})}
    assert "peer_send" not in names
    assert "lore_remember" not in names, (
        "if this fails the default projection stopped excluding writes at "
        "all, and peer_send's absence above proves nothing"
    )
    assert {"peer_list", "peer_history"} <= names, (
        "read-only discovery may default on -- if it does not, the "
        "exclusion above is not telling us anything about peer_send"
    )


def test_peer_send_is_absent_again_when_its_switch_is_off(monkeypatch):
    """The second, independent gate. include_write=True is what the ENGINE
    actually passes (lore_remember has to be reachable), so the registry
    split alone does not protect anything in production -- this predicate
    does. Off is the default, and off means not OFFERED, not refused: a
    tool the model cannot see is a tool the model cannot call."""
    ctx = {
        "belief_store": object(), "lore_root": "/tmp/lore",
        # The seam a real SessionEngine names. See the seam test below for
        # why its absence is also disqualifying.
        "peer_send": lambda request: {},
    }

    monkeypatch.setenv("DOXA_AGENT_PEER_SEND", "")
    off = {t.name for t in ops.to_sdk_tools(
        lambda n, a: {}, include_write=True, ctx=ctx)}
    assert "peer_send" not in off
    assert "lore_remember" in off, (
        "include_write must really be on, or this test passes for the "
        "wrong reason"
    )

    monkeypatch.setenv("DOXA_AGENT_PEER_SEND", "1")
    on = {t.name for t in ops.to_sdk_tools(
        lambda n, a: {}, include_write=True, ctx=ctx)}
    assert "peer_send" in on, "the switch must actually arm it, or it is dead code"


def test_an_engine_with_no_outbound_path_is_not_offered_the_send_tool(monkeypatch):
    """The setting alone is not enough, and the case is real rather than
    hypothetical: doxa.vendors' engine (DeepSeek, GLM) hosts a PeerHost
    and RECEIVES peer messages, but has no outbound path and names no
    peer_send seam. With the switch on it would otherwise be offered a
    tool whose only possible answer is "this session has no outbound peer
    channel" -- a soft, safe refusal, and still exactly the defect the
    configuredness filter exists to prevent."""
    monkeypatch.setenv("DOXA_AGENT_PEER_SEND", "1")
    no_seam = {"belief_store": object(), "lore_root": "/tmp/lore"}

    names = {t.name for t in ops.to_sdk_tools(
        lambda n, a: {}, include_write=True, ctx=no_seam)}

    assert "peer_send" not in names
    assert "peer_list" in names, (
        "discovery needs no seam -- it reads two files this process can "
        "open -- so an engine that cannot send keeps it"
    )


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "  "])
def test_a_word_that_means_no_does_not_arm_the_send_tool(monkeypatch, value):
    """``DOXA_AGENT_PEER_SEND=0`` meaning "on" because the string is
    non-empty is the classic version of this bug."""
    monkeypatch.setenv("DOXA_AGENT_PEER_SEND", value)
    assert peers.peer_send_enabled() is False


def test_peer_send_refuses_at_the_function_when_the_switch_is_off(monkeypatch):
    """Defence in depth behind the projection filter: if a future refactor
    drops is_configured, the fn itself still says no. The refusal must NOT
    contain the phrase "not configured", which gate.is_hard_failure counts
    as a strike -- a tool that disabled itself by correctly refusing twice
    would be a bug."""
    monkeypatch.setenv("DOXA_AGENT_PEER_SEND", "")
    from doxa.gate import OperatorContext

    ctx = OperatorContext(session_id="s", cwd="/tmp", repo_root="/tmp",
                          peer_send=lambda request: {"sent": True})
    out = ops.WRITE_OPERATORS["peer_send"].fn(body="hello", to="abc", op_ctx=ctx)
    assert "error" in out
    assert "not configured" not in out["error"]
    assert "peer_send failed" not in out["error"]


# -- addressing -------------------------------------------------------


@pytest.mark.asyncio
async def test_an_ambiguous_prefix_is_refused_naming_the_candidates(tmp_path, monkeypatch):
    """Cross-repo addressing is what kills prefix matching: two sessions
    that would never have shared a scope can now share a prefix. A guess
    here delivers a message to the wrong agent, in the wrong repository,
    with no way to tell afterwards -- so the answer is a refusal that
    names every contender, and NOTHING is sent."""
    engine, _ = await _engine(tmp_path, monkeypatch)
    try:
        rt = tmp_path / "rt"
        _peer_entry(rt, "shared-prefix-alpha", scope="/repo/a", title="alpha")
        _peer_entry(rt, "shared-prefix-beta", scope="/repo/b", title="beta")

        out = await engine._tool_peer_send({"to": "shared-prefix", "body": "hello"})

        assert "error" in out, f"an ambiguous prefix must not deliver: {out}"
        assert "ambiguous" in out["error"]
        assert "shared-p" in out["error"], "the candidates must be named"
        assert out["error"].count("shared-p") >= 2, (
            "both contenders, not just the first -- a listing of one is a "
            "guess with extra words"
        )
        assert engine._peer_ledger.count() == 0, (
            "a refused send must leave no record: a ledger line is a claim "
            "that a delivery happened"
        )
    finally:
        await engine.finalize()


@pytest.mark.asyncio
async def test_a_full_session_id_resolves_even_when_a_title_shares_its_prefix(
    tmp_path, monkeypatch,
):
    """resolve_peer matches titles too, which is a convenience across a
    small fleet and a hazard across a large one. An exact session id is
    the only name guaranteed unique and must win outright."""
    engine, _ = await _engine(tmp_path, monkeypatch)
    try:
        rt = tmp_path / "rt"
        _peer_entry(rt, "abc123", scope="/repo/a", title="unrelated")
        _peer_entry(rt, "zzz999", scope="/repo/b", title="abc123-and-more")

        out = await engine._tool_peer_send({"to": "abc123", "body": "hello"})

        assert "error" not in out, out
        assert [row["session_id"] for row in out["delivered_to"]] == ["abc123"]
    finally:
        await engine.finalize()


@pytest.mark.asyncio
async def test_peer_list_sees_other_repos_and_never_itself(tmp_path, monkeypatch):
    """Two properties in one place because they are the same decision.
    Addressing crosses repositories now, so discovery has to as well --
    and a session that listed itself would invite a message to nowhere."""
    engine, _ = await _engine(tmp_path, monkeypatch)
    try:
        rt = tmp_path / "rt"
        _peer_entry(rt, "elsewhere", scope="/a/totally/different/repo")

        out = ops.OPERATORS["peer_list"].fn(op_ctx=engine.tool_gate.op_ctx)

        ids = [row["session_id"] for row in out["peers"]]
        assert "elsewhere" in ids
        assert engine.session_id not in ids
        assert out["peers"][0]["repo"] == "/a/totally/different/repo"
        assert out["trust"] == peers.PEER_UNTRUSTED_INTRO, (
            "a roster row is self-description another process wrote -- a "
            "claimed model id is a capability claim, and there is no "
            "'structured, therefore safer' exception"
        )
    finally:
        await engine.finalize()


# -- broadcast --------------------------------------------------------


@pytest.mark.asyncio
async def test_a_broadcast_costs_one_delivery_per_recipient(tmp_path, monkeypatch):
    """Counting calls instead of deliveries would make one call an
    unbounded amplifier: at N=32 that is the difference between a limit
    and a decoration."""
    engine, _ = await _engine(tmp_path, monkeypatch)
    try:
        rt = tmp_path / "rt"
        for name in ("bc-one", "bc-two", "bc-three"):
            _peer_entry(rt, name, scope=f"/repo/{name}")

        out = await engine._tool_peer_send({"body": "all hands", "broadcast": True})

        assert "error" not in out, out
        assert out["deliveries_charged"] == 3
        assert engine._peer_limiter.used_in_window() == 3, (
            "the limiter must have been charged three, not one"
        )
        record = engine._peer_ledger.recent(limit=1)[0]
        assert record.kind == "broadcast"
        assert set(record.to) == {"bc-one", "bc-two", "bc-three"}, (
            "one record per send, whose 'to' is the whole fan-out"
        )
    finally:
        await engine.finalize()


@pytest.mark.asyncio
async def test_a_broadcast_starts_no_turn_even_with_inbound_turns_armed(
    tmp_path, monkeypatch,
):
    """The one rule docs/plans/emergent-organization.md states before it
    states anything else. At N=32 a broadcast that started turns would
    wake the entire fleet in a single step, and a reply-broadcast round is
    992 messages."""
    engine, created = await _engine(tmp_path, monkeypatch, inbound=True)
    try:
        engine._on_peer_frame({
            "from_id": "loud", "from_title": "loud", "sent_at": "now",
            "body": "everyone please respond", "from_repo": "/repo/loud",
            "kind": "broadcast",
        })
        await asyncio.sleep(0)

        assert engine._turn_running is False, "a broadcast must never wake a session"
        assert len(engine._prompt_queue) == 0, "not even into the queue"
        assert created[0].queried == [], "nothing was sent to the model"
        assert len(engine._pending_peer_frames) == 1, (
            "it is not dropped either -- it rides the next turn the user "
            "starts, which is exactly what it did before any of this"
        )
    finally:
        await engine.finalize()


# -- inbound delivery -------------------------------------------------


@pytest.mark.asyncio
async def test_an_incoming_message_starts_a_turn_when_the_session_is_idle(
    tmp_path, monkeypatch,
):
    engine, created = await _engine(tmp_path, monkeypatch, inbound=True)
    try:
        engine._on_peer_frame({
            "from_id": "waker", "from_title": "waker", "sent_at": "now",
            "body": "can you take the parser?", "from_repo": "/repo/waker",
            "kind": "direct",
        })
        assert engine._turn_running is True, (
            "set synchronously, before the task's first step -- otherwise a "
            "prompt submitted in that window starts a second turn"
        )
        assert engine._queued_turn_task is not None
        await engine._queued_turn_task

        assert created[0].queried, "the model was never asked anything"
        prompt = created[0].queried[0][0]
        assert prompt.startswith(peers.PEER_TURN_INTRO)
        assert "can you take the parser?" in prompt
        assert peers.PEER_UNTRUSTED_INTRO in prompt, (
            "a peer-started turn's body still crosses the untrusted marker"
        )
    finally:
        await engine.finalize()


@pytest.mark.asyncio
async def test_an_incoming_message_queues_when_a_turn_is_already_running(
    tmp_path, monkeypatch,
):
    """Into the EXISTING bounded FIFO, as a second producer. A parallel
    queue would disagree with this one about order the first time both had
    something waiting, and the bound would stop being a bound."""
    engine, created = await _engine(tmp_path, monkeypatch, inbound=True)
    try:
        engine._turn_running = True
        engine._on_peer_frame({
            "from_id": "patient", "from_title": "patient", "sent_at": "now",
            "body": "when you get a moment", "from_repo": "/repo/patient",
            "kind": "direct",
        })
        await asyncio.sleep(0)

        assert len(engine._prompt_queue) == 1
        queued = engine._prompt_queue.snapshot()[0]["text"]
        assert queued.startswith(peers.PEER_TURN_INTRO), (
            "the attribution rides the TEXT, which is what makes it survive "
            "the queue -- PromptQueue carries an id and a string, nothing else"
        )
        assert created[0].queried == [], "the running turn was not interrupted"
        assert engine._pending_peer_frames == [], (
            "a queued message must not ALSO ride the next turn -- that is "
            "the same message delivered twice"
        )
        queued_event = [
            ev for ev in _drain(engine._peer_queue) if ev.type == "prompt_queued"
        ]
        assert queued_event, "nothing told the user a peer message is waiting"
        assert queued_event[0].data["peer_started"] is True
        assert "patient" in str(queued_event[0].data["peer_origin"]), (
            "the queue line is the only thing shown between arrival and the "
            "turn starting, and the prompt's first 120 characters are "
            "boilerplate identical on every one of these -- so the sender "
            "has to be on the event"
        )
    finally:
        engine._turn_running = False
        await engine.finalize()


@pytest.mark.asyncio
async def test_an_incoming_message_starts_no_turn_while_the_switch_is_off(
    tmp_path, monkeypatch,
):
    """The default, and the behaviour DOXA has always had. Receiving and
    being woken are different grants."""
    engine, created = await _engine(tmp_path, monkeypatch, inbound=False)
    try:
        engine._on_peer_frame({
            "from_id": "quiet", "from_title": "quiet", "sent_at": "now",
            "body": "no rush", "from_repo": "/repo/quiet", "kind": "direct",
        })
        await asyncio.sleep(0)

        assert engine._turn_running is False
        assert len(engine._prompt_queue) == 0
        assert len(engine._pending_peer_frames) == 1
    finally:
        await engine.finalize()


@pytest.mark.asyncio
async def test_a_peer_started_turn_is_attributable(tmp_path, monkeypatch):
    """Spend needs a traceable cause. The turn says so in three places
    that cannot drift, because all three are derived from one string: the
    prompt the model read, the event the transcript renders from, and the
    turn id every ledger record written during that turn carries."""
    engine, created = await _engine(tmp_path, monkeypatch, inbound=True)
    try:
        engine._on_peer_frame({
            "from_id": "cause01", "from_title": "the cause", "sent_at": "t0",
            "body": "please look at the parser", "from_repo": "/repo/cause",
            "kind": "direct",
        })
        await engine._queued_turn_task

        started = [ev for ev in _drain(engine._peer_queue) if ev.type == "turn_started"]
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
        assert created[0].queried[0][0].startswith(peers.PEER_TURN_INTRO), (
            "and the model was told, in the same words the reader sees"
        )
    finally:
        await engine.finalize()


@pytest.mark.asyncio
async def test_a_send_inside_a_peer_started_turn_carries_that_turn_into_the_ledger(
    tmp_path, monkeypatch,
):
    """The attribution has to reach the FILE, not only the screen -- the
    mesh graph and the experiment read the file."""
    engine, _ = await _engine(tmp_path, monkeypatch)
    try:
        rt = tmp_path / "rt"
        _peer_entry(rt, "target01", scope="/repo/t")

        engine._turn_id = "peer-deadbeef0001"
        await engine._tool_peer_send({"to": "target01", "body": "on it"})

        record = engine._peer_ledger.recent(limit=1)[0]
        assert record.turn.id == "peer-deadbeef0001"
        assert record.turn.state == "running"
    finally:
        await engine.finalize()


# -- the limit --------------------------------------------------------


@pytest.mark.asyncio
async def test_a_refusal_names_its_reason_and_its_reset(tmp_path, monkeypatch):
    """An agent told WHY it was refused, and WHEN the budget frees up, can
    reason about it -- send to fewer peers, wait, stop. One that is
    silently throttled just retries, which is the behaviour the limit
    exists to prevent."""
    engine, _ = await _engine(tmp_path, monkeypatch)
    try:
        rt = tmp_path / "rt"
        _peer_entry(rt, "target01", scope="/repo/t")
        engine._peer_limiter.limits = pl.SendLimits(
            per_turn=64, per_window=1, window_secs=60.0,
        )

        first = await engine._tool_peer_send({"to": "target01", "body": "one"})
        assert "error" not in first, first

        refused = await engine._tool_peer_send({"to": "target01", "body": "two"})

        assert "error" in refused, "the second send must be refused"
        assert "peer send refused" in refused["error"]
        assert "1" in refused["error"], "the numbers that refused must be in it"
        assert refused["refused_by"] == "window"
        assert refused["resets"] not in ("", None, "not refused"), (
            "a refusal with no reset is the silent throttle this avoids"
        )
        assert "at 20" in refused["resets"] or "in " in refused["resets"], (
            f"the reset must be a time, not a shrug: {refused['resets']!r}"
        )
        assert engine._peer_ledger.count() == 1, (
            "a refused send writes no record -- the ledger counts deliveries "
            "that happened"
        )
    finally:
        await engine.finalize()


# -- history ----------------------------------------------------------


@pytest.mark.asyncio
async def test_peer_history_returns_only_this_sessions_traffic(tmp_path, monkeypatch):
    """One ledger file holds the whole fleet's traffic, so "my history" is
    a filter and not a file. Scoped deliberately: an agent needs to notice
    that a peer has sent it the same thing four times, and the fleet-wide
    picture is a graph a human looks at."""
    engine, _ = await _engine(tmp_path, monkeypatch)
    try:
        own = engine.session_id
        book = engine._peer_ledger
        book.append(sender=pl.Sender(session=own, title="me"), to=["other01"],
                    body="mine, outbound")
        book.append(sender=pl.Sender(session="other01", title="them"), to=[own],
                    body="mine, inbound")
        book.append(sender=pl.Sender(session="other01", title="them"),
                    to=["other02"], body="none of my business")

        out = ops.OPERATORS["peer_history"].fn(op_ctx=engine.tool_gate.op_ctx)

        bodies = [row["body"] for row in out["sent"] + out["received"]]
        assert "mine, outbound" in bodies
        assert "mine, inbound" in bodies
        assert "none of my business" not in bodies, (
            "a session must not read traffic it was neither sender nor "
            "recipient of"
        )
        assert out["sent_count"] == 1 and out["received_count"] == 1
        assert out["trust"] == peers.PEER_UNTRUSTED_INTRO, (
            "received bodies were written by another process"
        )
        assert all(row.get("id") for row in out["received"]), (
            "ids are what in_reply_to needs -- without them a thread cannot "
            "be reconstructed at all"
        )
    finally:
        await engine.finalize()


# -- the record -------------------------------------------------------


@pytest.mark.asyncio
async def test_a_credential_in_a_sent_body_is_scrubbed_but_still_hashes(
    tmp_path, monkeypatch,
):
    """The worst outcome available: an agent quotes a key at a peer and
    the ledger keeps it forever. The hash is taken BEFORE the scrub, so
    two identical messages still hash identically -- which is how a loop
    is recognised at all."""
    engine, _ = await _engine(tmp_path, monkeypatch)
    try:
        rt = tmp_path / "rt"
        _peer_entry(rt, "target01", scope="/repo/t")
        body = f"the key is {FAKE_AWS_KEY} -- use it"

        out = await engine._tool_peer_send({"to": "target01", "body": body})
        assert "error" not in out, out

        line = engine._peer_ledger.path.read_text(encoding="utf-8").strip()
        assert FAKE_AWS_KEY not in line, "a credential reached the ledger file"
        record = pl.Message.parse_line(line)
        assert FAKE_AWS_KEY not in record.body
        assert record.body_sha256 == hashlib.sha256(body.encode("utf-8")).hexdigest(), (
            "hashed pre-scrub, or identical messages stop matching and loop "
            "detection stops working"
        )
    finally:
        await engine.finalize()


@pytest.mark.asyncio
async def test_the_tool_hands_the_ledger_a_body_it_has_not_already_scrubbed(
    tmp_path, monkeypatch,
):
    """A quiet bug, caught by reading peerledger's contract rather than by
    anything failing: the ledger hashes the body BEFORE it scrubs it, so a
    caller that scrubbed first would store a hash of the redaction. Two
    identical messages would then hash differently, and identical-message
    detection is how a looping exchange is recognised at all.

    Driven through the OPERATOR, not the engine seam, because the operator
    is where the extra scrub was."""
    engine, _ = await _engine(tmp_path, monkeypatch)
    try:
        rt = tmp_path / "rt"
        _peer_entry(rt, "target01", scope="/repo/t")
        body = f"the key is {FAKE_AWS_KEY} -- use it"

        result = ops.WRITE_OPERATORS["peer_send"].fn(
            body=body, to="target01", op_ctx=engine.tool_gate.op_ctx,
        )
        assert asyncio.iscoroutine(result) or hasattr(result, "__await__"), result
        out = await result
        assert "error" not in out, out

        record = engine._peer_ledger.recent(limit=1)[0]
        assert FAKE_AWS_KEY not in record.body
        assert record.body_sha256 == hashlib.sha256(body.encode("utf-8")).hexdigest(), (
            "the operator scrubbed before the ledger did, so the hash now "
            "describes the redaction instead of the message"
        )
    finally:
        await engine.finalize()


@pytest.mark.asyncio
async def test_a_human_typed_message_is_recorded_too(tmp_path, monkeypatch):
    """/msg goes through the same one outbound path. A ledger that held
    only the model's traffic would make the mesh graph a picture of the
    model rather than of the fleet."""
    engine, _ = await _engine(tmp_path, monkeypatch, peer_send=False)
    try:
        rt = tmp_path / "rt"
        _peer_entry(rt, "target01", scope="/repo/t")

        await engine.send_peer_message("target01", "from a human")

        record = engine._peer_ledger.recent(limit=1)[0]
        assert record.body == "from a human"
        assert record.sender.session == engine.session_id
        assert record.turn.state == "idle", (
            "typed outside a turn, and that is a measurement: an unprompted "
            "message is the shape a coordinator has"
        )
    finally:
        await engine.finalize()


@pytest.mark.asyncio
async def test_the_senders_repo_travels_and_is_displayed_on_arrival(
    tmp_path, monkeypatch,
):
    """Addressing crosses repositories, so "which project is this about"
    is the first thing a reader needs, and it has to survive the wire."""
    engine, _ = await _engine(tmp_path, monkeypatch)
    try:
        received: list = []
        host = engine.peer_host
        assert host is not None
        # Straight onto the host's own callback: _handle_conn scrubs every
        # field before it fires, so what lands in this list is exactly what
        # the engine would have been handed.
        host._on_message = received.append

        await peers.send_message(
            host.socket_path,
            from_id="faraway",
            from_title="a session elsewhere",
            body="hello from another project",
            from_repo="/some/entirely/other/repo",
        )
        for _ in range(200):
            if received:
                break
            await asyncio.sleep(0.01)

        assert received, "the frame never arrived"
        assert received[0]["from_repo"] == "/some/entirely/other/repo"
        rendered = peers.frame_for_model([received[0]])
        assert "/some/entirely/other/repo" in rendered
    finally:
        await engine.finalize()


def test_a_frame_without_a_repo_says_unknown_rather_than_guessing():
    """An older sender omits the key. The one thing the display may not do
    is substitute the reader's own repo, which is the most misleading
    answer available now that the two can genuinely differ."""
    rendered = peers.frame_for_model([{
        "from_id": "old", "from_title": "old build", "sent_at": "t", "body": "hi",
    }])
    assert "repo unknown" in rendered


# -- the lights -------------------------------------------------------


def test_a_modem_light_goes_dark_on_its_own():
    """The lamp is a RENDERING of a timestamp, not state someone has to
    remember to turn off. A lamp that stayed lit because nothing cleared
    it would say "traffic now" hours after the traffic."""
    from doxa.session.chips import PEER_LAMP_DARK, PEER_LAMP_LIT, PEER_LAMP_SECS, peer_lamp

    never = peer_lamp("tx", None, 0, now=1000.0)
    assert PEER_LAMP_DARK in never.key
    assert "nothing yet" in never.hints[0][1]

    just_now = peer_lamp("tx", 1000.0, 3, now=1000.5)
    assert PEER_LAMP_LIT in just_now.key
    assert "3 messages" in just_now.hints[0][1]

    stale = peer_lamp("tx", 1000.0, 3, now=1000.0 + PEER_LAMP_SECS + 0.1)
    assert PEER_LAMP_DARK in stale.key, "the lamp must decay with no help"
    assert "3 messages" in stale.hints[0][1], (
        "the count survives the decay -- the light says 'traffic', the "
        "tooltip says how much"
    )


def test_both_lights_are_actually_wired_to_traffic():
    """A source check, for the same reason ``test_import_cost``'s bare-name
    check is one: the alternative is a test that drives a real pane's
    out-of-band pump, which either patches the wiring it is meant to be
    proving or mounts a whole app to assert on two characters.

    What it catches is the failure that would otherwise be invisible --
    somebody restructures ``_peer_pump`` and one lamp silently stops
    lighting. The peer tools work fine with a dead lamp, so nothing else
    in this suite would notice, and a light that only works in one
    direction is worse than none: it teaches you to trust it."""
    from pathlib import Path

    from doxa.session import runtime as runtime_mod

    source = Path(runtime_mod.__file__).read_text(encoding="utf-8")
    assert '_note_peer_traffic("rx")' in source, (
        "nothing lights the receive lamp when a peer message arrives"
    )
    assert '_note_peer_traffic("tx")' in source, (
        "nothing lights the send lamp on the engine's peer_sent event"
    )
    assert 'ev.type == "peer_sent"' in source, (
        "the out-of-band pump does not handle peer_sent at all, so a send "
        "made by the model or by a broadcast is invisible on the status bar"
    )


def test_the_two_lights_are_told_apart_without_reading_them():
    """Peripheral vision is the whole design: one glance has to say WHICH
    direction, so the two lamps cannot paint the same glyph."""
    from doxa.session.chips import peer_lamp

    tx = peer_lamp("tx", 1000.0, 1, now=1000.1)
    rx = peer_lamp("rx", 1000.0, 1, now=1000.1)
    assert tx.key != rx.key
    assert "sent" in tx.hints[0][1] and "received" in rx.hints[0][1]

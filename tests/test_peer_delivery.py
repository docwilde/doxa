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

import json

import pytest

from claude_agent_sdk import ResultMessage

from doxa import peerledger as pl
from doxa import peers
from doxa.engine import SessionEngine
from doxa.vendors import DEEPSEEK, ChatApiEngine

from tests.fakes import factory_with_script
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


# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.peernet (R2, docs/plans/remote.md): peers on another machine.

The testing bar is that spec's own, and it is stricter than "the refusal
fires" because it has been burned by that before:

    - a remote listener is **absent** unless explicitly enabled -- assert
      on the default, since a default is what almost everyone runs
    - a request from a non-allow-listed identity is refused, and the
      refusal is visible rather than silent
    - ... written as security assertions the way v0.36.0's "the model
      cannot reach the shell" test is written -- including its lesson that
      such a test passes *vacuously* until the capability exists, so it
      must be verified against a deliberately unsafe build

THE PERMISSIVE BUILD, concretely. Every refusal test below has a companion
that stands the SAME server up with remote listening on, DOXA's allow-list
naming the login, a loopback connection and a handler set that would
cheerfully answer -- and proves the request goes through. Without that
companion, every assertion here would pass on a module that refused
everything, on a module whose handlers were never wired, and on a module
that did not exist: ``_dispatch`` raising ``AttributeError`` would read as
"not granted" to a test that only checked for absence.

The third bar item -- the reduced surface refusing ``!`` shell and
``bypassPermissions`` -- is tested in tests/test_remote_policy.py against
the policy directly. It is not re-tested here, and the reason is worth
writing down: this module has no code path that could reach either. It
maps its three ops onto ``read_status`` / ``read_transcript`` /
``send_prompt`` and refuses every other string before policy is consulted,
so the shell case is unreachable by construction rather than by a check.
``test_no_op_maps_to_a_gated_request_kind`` asserts that construction.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from doxa import peernet as peernet_mod
from doxa import peers as peers_mod
from doxa import remote_policy as policy_mod
from doxa.ui.labels import peer_origin


LOGIN = "operator@example.com"


@pytest.fixture(autouse=True)
def _remote_is_off_unless_a_test_says_otherwise(monkeypatch):
    """Start every test from the state almost every machine is in."""
    for name in (
        "DOXA_REMOTE_ENABLED",
        "DOXA_REMOTE_ALLOWED_LOGINS",
        "DOXA_REMOTE_PEERS",
        "DOXA_REMOTE_BIND",
        "DOXA_REMOTE_PORT",
        "DOXA_REMOTE_ALLOW_SHELL",
        "DOXA_REMOTE_ALLOW_BYPASS",
    ):
        monkeypatch.delenv(name, raising=False)


def _permissive(monkeypatch) -> None:
    """The deliberately unsafe build every refusal below is checked
    against: listening on, this login allow-listed, both gated
    capabilities opted in. If a refusal still fires here, it is a real
    branch; if the companion request still fails here, the refusal above
    proved nothing."""
    monkeypatch.setenv("DOXA_REMOTE_ENABLED", "1")
    monkeypatch.setenv("DOXA_REMOTE_ALLOWED_LOGINS", LOGIN)
    monkeypatch.setenv("DOXA_REMOTE_ALLOW_SHELL", "1")
    monkeypatch.setenv("DOXA_REMOTE_ALLOW_BYPASS", "1")


def _server(**kw) -> peernet_mod.PeerNetServer:
    """A bridge whose handlers answer EVERYTHING -- so a refusal in a test
    below can only have come from the policy path."""
    handlers = {
        peernet_mod.OP_ROSTER: lambda body: {"peers": [], "count": 0},
        peernet_mod.OP_DELIVER: lambda body: {"delivered_to": "whoever"},
        peernet_mod.OP_HISTORY: lambda body: {"messages": []},
    }
    return peernet_mod.PeerNetServer(handlers, host="127.0.0.1", port=0, **kw)


# =======================================================================
# (a) a remote listener is ABSENT unless explicitly enabled
# =======================================================================


async def test_no_listener_exists_unless_remote_is_explicitly_enabled():
    """The default is what almost everyone runs, so the default is what is
    asserted. Not "a listener that rejects" -- no listener: nothing is
    bound, no port is open, and there is nothing on the network to find."""
    server = _server()
    decision = await server.start()

    assert decision.allowed is False
    assert server.listening is False
    assert server.port_in_use is None
    assert decision.reason, "a refusal with no reason is a silent refusal"
    assert "OFF" in decision.reason


async def test_a_listener_does_exist_once_remote_is_explicitly_enabled(monkeypatch):
    """The companion. Without it the assertion above would pass on a
    ``start()`` that always refused, and the absence it proves would be
    the absence of the feature rather than the absence of a listener."""
    _permissive(monkeypatch)
    server = _server()
    try:
        decision = await server.start()
        assert decision.allowed is True
        assert server.listening is True
        assert isinstance(server.port_in_use, int) and server.port_in_use > 0
    finally:
        await server.stop()
    assert server.listening is False


def test_the_bind_address_is_loopback_with_no_configuration():
    assert peernet_mod.bind_host() == "127.0.0.1"
    assert peernet_mod.loopback(peernet_mod.bind_host()) is True


def test_no_remote_endpoint_is_configured_with_no_configuration():
    """DOXA looks on this machine only until somebody names another one."""
    assert peernet_mod.endpoints() == ()


def test_moving_the_bind_off_loopback_is_said_out_loud(monkeypatch):
    """Not refused -- an operator may have a reason -- but never silent.
    The reason names the consequence, which is that the identity header is
    then believed on no path at all."""
    _permissive(monkeypatch)
    monkeypatch.setenv("DOXA_REMOTE_BIND", "0.0.0.0")
    decision = peernet_mod.listen_decision()
    assert decision.allowed is True
    assert "NOT loopback" in decision.reason


@pytest.mark.parametrize(
    "host,expected",
    [("127.0.0.1", True), ("127.0.0.2", True), ("::1", True),
     ("10.0.0.4", False), ("0.0.0.0", False), ("", False), ("nonsense", False)],
)
def test_loopback_is_decided_by_parsing_not_by_string_comparison(host, expected):
    """The failure this catches: a check that knows only ``127.0.0.1`` and
    therefore calls a genuinely local connection remote (127.0.0.2), or --
    the dangerous direction -- a check confused by a hostname."""
    assert peernet_mod.loopback(host) is expected


# =======================================================================
# (b) a non-allow-listed identity is refused, visibly
# =======================================================================


async def test_a_peer_from_a_non_allow_listed_identity_is_refused(monkeypatch):
    monkeypatch.setenv("DOXA_REMOTE_ENABLED", "1")
    monkeypatch.setenv("DOXA_REMOTE_ALLOWED_LOGINS", "someone.else@example.com")
    server = _server()

    status, payload = await server._dispatch(
        {"op": peernet_mod.OP_ROSTER}, login=LOGIN, from_loopback=True
    )

    assert status == 403
    assert payload["ok"] is False
    assert LOGIN in payload["reason"]
    assert "allow-list" in payload["reason"]
    # Visible on BOTH sides: the asker is told, and the machine that
    # refused can say what it refused.
    assert server.refusals and LOGIN in server.refusals[-1]


async def test_the_same_request_is_granted_once_the_login_is_allow_listed(monkeypatch):
    """The permissive build. Same server, same op, one input flipped."""
    _permissive(monkeypatch)
    server = _server()

    status, payload = await server._dispatch(
        {"op": peernet_mod.OP_ROSTER}, login=LOGIN, from_loopback=True
    )

    assert status == 200 and payload["ok"] is True
    assert payload["reason"], "a grant carries its reason too"


async def test_an_empty_allow_list_refuses_everyone_rather_than_everyone(monkeypatch):
    """The direction every allow-list bug in security software fails in.
    Listening is ON here -- the only thing missing is the list."""
    monkeypatch.setenv("DOXA_REMOTE_ENABLED", "1")
    server = _server()

    status, payload = await server._dispatch(
        {"op": peernet_mod.OP_ROSTER}, login=LOGIN, from_loopback=True
    )

    assert status == 403
    assert "empty" in payload["reason"]


async def test_the_identity_header_is_refused_when_it_did_not_arrive_on_loopback(
    monkeypatch,
):
    """THE load-bearing one. ``tailscale serve`` terminates TLS and
    forwards to loopback, and that forwarding is what makes the header mean
    anything. A header on any other path was written by whoever connected,
    so it is refused BEFORE the login is even looked at -- note that the
    login here is the allow-listed one and it still fails."""
    _permissive(monkeypatch)
    server = _server()

    status, payload = await server._dispatch(
        {"op": peernet_mod.OP_ROSTER}, login=LOGIN, from_loopback=False
    )

    assert status == 403
    assert "loopback" in payload["reason"]


async def test_the_listener_computes_loopback_from_the_socket_not_the_request(
    monkeypatch,
):
    """The failure this catches: believing a client that says it is local.
    Nothing in the body can move ``from_loopback`` -- it comes from the
    kernel's idea of who connected."""
    _permissive(monkeypatch)
    server = _server()
    await server.start()
    try:
        answer = await peernet_mod.request(
            peernet_mod.Endpoint("self", "127.0.0.1", server.port_in_use),
            {"op": peernet_mod.OP_ROSTER, "from_loopback": True, "login": "root"},
            login=LOGIN,
        )
        assert answer["ok"] is True
    finally:
        await server.stop()


async def test_a_refusal_crosses_the_wire_as_a_refusal_with_its_reason(monkeypatch):
    """End to end over a real socket: the client raises
    :class:`RemoteRefused` carrying the sentence, never a bare failure and
    never a silently empty roster."""
    monkeypatch.setenv("DOXA_REMOTE_ENABLED", "1")
    monkeypatch.setenv("DOXA_REMOTE_ALLOWED_LOGINS", "nobody@example.com")
    server = _server()
    await server.start()
    endpoint = peernet_mod.Endpoint("self", "127.0.0.1", server.port_in_use)
    try:
        with pytest.raises(peernet_mod.RemoteRefused, match=r"allow-list"):
            await peernet_mod.fetch_roster(endpoint, login=LOGIN)
    finally:
        await server.stop()


# =======================================================================
# One policy, not two
# =======================================================================


def test_every_op_maps_onto_a_request_kind_remote_policy_already_knows():
    """The temptation was to invent ``REQUEST_PEER_ROSTER``. A second
    vocabulary is a second policy, and a second policy is the one that does
    not get reviewed."""
    assert set(peernet_mod.OP_KINDS.values()) <= set(policy_mod.REQUEST_KINDS)


def test_no_op_maps_to_a_gated_request_kind():
    """The stronger statement about ``!`` shell and ``bypassPermissions``:
    not "refused" but unreachable. No op on this bridge maps to either
    gated kind, so there is no request a caller could form that would even
    ask."""
    assert set(peernet_mod.OP_KINDS.values()) & set(policy_mod.GATED_KINDS) == set()


async def test_an_unrecognised_op_is_refused_before_policy_is_consulted(monkeypatch):
    """Recognition is not permission -- but non-recognition is certainly
    refusal, and it happens without troubling the policy at all."""
    _permissive(monkeypatch)
    asked: "list[str]" = []
    server = _server(evaluate=lambda kind, **kw: asked.append(kind) or policy_mod.Decision.allow("yes"))

    status, payload = await server._dispatch(
        {"op": "rm_minus_rf"}, login=LOGIN, from_loopback=True
    )

    assert status == 400
    assert "not an operation" in payload["reason"]
    assert asked == [], "policy was consulted about an op that does not exist"


async def test_this_module_answers_no_authorization_question_itself(monkeypatch):
    """A guard against the drift that matters most: if this bridge ever
    grew its own opinion, a policy stub that refuses everything would stop
    being able to refuse it."""
    _permissive(monkeypatch)
    server = _server(
        evaluate=lambda kind, **kw: policy_mod.Decision.refuse("the policy said no")
    )

    for op in peernet_mod.OP_KINDS:
        status, payload = await server._dispatch(
            {"op": op}, login=LOGIN, from_loopback=True
        )
        assert status == 403, op
        assert payload["reason"] == "the policy said no"


# =======================================================================
# A remote peer is MARKED remote, everywhere a local one appears
# =======================================================================


def _peer(session_id: str, **kw) -> peers_mod.PeerInfo:
    return peers_mod.PeerInfo(
        session_id=session_id, pid=1, socket_path="/tmp/x.sock", cwd="/repo",
        repo_root="/repo", title="worker", started_at="2026-09-18T00:00:00.000000Z",
        heartbeat_at="2026-09-18T00:00:00.000000Z", **kw,
    )


def test_a_local_peer_reads_as_local_and_a_remote_one_names_its_machine():
    assert peer_origin(None) == "local"
    assert peer_origin("") == "local"
    assert "remote:workstation" in peer_origin("workstation")
    assert _peer("a").is_remote is False
    assert _peer("b", origin="workstation").is_remote is True


async def test_a_fetched_peer_is_marked_remote_and_cannot_claim_otherwise(monkeypatch):
    """THE assertion for "say who is connected", and the hostile case with
    it: a machine returns a roster row claiming ``origin: null`` -- i.e.
    claiming to be local -- and the row still comes back marked with the
    endpoint DOXA actually dialled.

    Which machine a peer is on is the one fact the READER can establish for
    itself. Everything else in a registry row is a claim; this is not."""
    _permissive(monkeypatch)
    liar = {
        **vars(_peer("remote-1")),
        "origin": None,          # "I am local"
        "title": "looks-local",
    }
    handlers = {peernet_mod.OP_ROSTER: lambda body: {"peers": [liar]}}
    server = peernet_mod.PeerNetServer(handlers, host="127.0.0.1", port=0)
    await server.start()
    endpoint = peernet_mod.Endpoint("workstation", "127.0.0.1", server.port_in_use)
    try:
        fetched = await peernet_mod.fetch_roster(endpoint, login=LOGIN)
    finally:
        await server.stop()

    assert len(fetched) == 1
    assert fetched[0].origin == "workstation"
    assert fetched[0].is_remote is True
    assert peer_origin(fetched[0].origin) != "local"


async def test_the_combined_roster_marks_local_and_remote_rows_differently(monkeypatch):
    """The surface a human and a model both read. A cluster's roster mixes
    machines, and a row that does not say which one it is on is worse than
    no row."""
    _permissive(monkeypatch)
    monkeypatch.setattr(
        peers_mod, "read_registry", lambda **kw: [_peer("local-1")]
    )
    handlers = {peernet_mod.OP_ROSTER: lambda body: {"peers": [vars(_peer("remote-1"))]}}
    server = peernet_mod.PeerNetServer(handlers, host="127.0.0.1", port=0)
    await server.start()
    endpoint = peernet_mod.Endpoint("workstation", "127.0.0.1", server.port_in_use)
    try:
        rows, problems = await peernet_mod.combined_roster(
            remote=(endpoint,), login=LOGIN
        )
    finally:
        await server.stop()

    assert problems == []
    by_id = {p.session_id: p for p in rows}
    assert by_id["local-1"].origin is None
    assert by_id["remote-1"].origin == "workstation"
    assert peer_origin(by_id["local-1"].origin) == "local"
    assert "workstation" in peer_origin(by_id["remote-1"].origin)


async def test_a_machine_that_cannot_be_reached_is_reported_not_silently_dropped(
    monkeypatch,
):
    """A cluster where one node quietly vanished looks exactly like a
    cluster where one node has no sessions. Those are different facts and
    the roster has to be able to tell them apart."""
    _permissive(monkeypatch)
    monkeypatch.setattr(peers_mod, "read_registry", lambda **kw: [])
    dead = peernet_mod.Endpoint("workstation", "127.0.0.1", 9)

    rows, problems = await peernet_mod.combined_roster(remote=(dead,), login=LOGIN)

    assert rows == []
    assert len(problems) == 1 and "workstation" in problems[0]


def test_the_peer_list_tool_tells_the_model_which_machine_a_peer_is_on(monkeypatch):
    """The model's copy of the roster, not just the human's. A model
    weighing a peer's self-reported capability needs to know the row came
    off a wire -- and ``origin`` is the one key in that row DOXA
    established rather than received."""
    from doxa import operators as operators_mod

    monkeypatch.setattr(
        peers_mod,
        "read_registry",
        lambda **kw: [_peer("local-1"), _peer("remote-1", origin="workstation")],
    )
    result = operators_mod._peer_list(limit=10)
    rows = {r["session_id"]: r for r in result["peers"]}

    assert rows["local-1"]["origin"] is None
    assert rows["local-1"]["is_remote"] is False
    assert rows["remote-1"]["origin"] == "workstation"
    assert rows["remote-1"]["is_remote"] is True


# =======================================================================
# Endpoints carry no credential, and never can
# =======================================================================


def test_an_endpoint_is_a_hostname_and_a_name_to_show(monkeypatch):
    monkeypatch.setenv(
        "DOXA_REMOTE_PEERS",
        "workstation=ws.tail1234.ts.net:47600, laptop=lp.tail1234.ts.net",
    )
    found = peernet_mod.endpoints()

    assert [e.label for e in found] == ["workstation", "laptop"]
    assert found[0].port == 47600
    assert found[1].port == peernet_mod.DEFAULT_PORT
    # There is nowhere to put a secret: the dataclass has three fields and
    # none of them is one. Asserted rather than assumed, because "no new
    # credential store" is a design promise that a later field could break
    # quietly.
    assert set(peernet_mod.Endpoint.__dataclass_fields__) == {"label", "host", "port"}


def test_one_malformed_endpoint_does_not_empty_the_whole_roster(monkeypatch):
    monkeypatch.setenv("DOXA_REMOTE_PEERS", "good=host, rubbish, =nohost, bad=h:xx")
    assert [e.label for e in peernet_mod.endpoints()] == ["good"]


def test_the_remote_rows_exist_and_default_to_loopback_and_off():
    from doxa import config as config_mod

    rows = {s.key: s for s in config_mod.SETTINGS}
    assert rows["remote_peers"].default == ""
    assert rows["remote_bind"].default == "127.0.0.1"
    assert rows["remote_port"].default == str(peernet_mod.DEFAULT_PORT)
    assert rows["remote_enabled"].default == ""


async def test_an_oversize_request_is_refused_before_a_byte_moves():
    endpoint = peernet_mod.Endpoint("nowhere", "127.0.0.1", 9)
    with pytest.raises(peernet_mod.PeerNetError, match=r"too large"):
        await peernet_mod.request(
            endpoint, {"op": "roster", "pad": "x" * (peernet_mod.MAX_BODY_BYTES + 1)}
        )


# =======================================================================
# Scope and transport-local fields at the machine boundary (finding 7)
# =======================================================================


def test_every_local_handler_honours_the_scope_key(monkeypatch, tmp_path):
    """No probe: nothing wires a bridge yet, so this is latent. ``_roster``
    applied ``scope_key`` and ``_deliver``/``_history`` ignored it -- a
    roster that hides the sessions in another repo while delivery still
    reaches them, and history still quotes them, is not a scope."""
    from doxa import peerledger as peerledger_mod

    from dataclasses import replace as _replace

    mine = _replace(_peer("mine"), cwd="/repo-a", repo_root="/repo-a")
    theirs = _replace(_peer("theirs"), cwd="/repo-b", repo_root="/repo-b")
    monkeypatch.setattr(peers_mod, "read_registry", lambda **kw: [mine, theirs])

    handlers = peernet_mod.local_handlers(scope_key="/repo-a")

    # roster: only the in-scope session.
    assert [r["session_id"] for r in handlers[peernet_mod.OP_ROSTER]({})["peers"]] == [
        "mine"
    ]

    # deliver: an out-of-scope target cannot be resolved at all.
    sent: list = []

    async def _send(socket_path, **kw):
        sent.append((socket_path, kw))

    monkeypatch.setattr(peers_mod, "send_message", _send)
    with pytest.raises(peers_mod.PeerSendError, match=r"theirs"):
        asyncio.run(handlers[peernet_mod.OP_DELIVER]({
            "target": "theirs", "body": "hello",
        }))
    assert sent == []

    # history: scoped by the sender's repo, which is the same scope key.
    asked: list = []

    class _Ledger:
        def recent(self, limit):
            asked.append(("recent", limit))
            return []

        def in_repo(self, repo, limit):
            asked.append(("in_repo", repo, limit))
            return []

    monkeypatch.setattr(peerledger_mod, "ledger", lambda: _Ledger())
    handlers[peernet_mod.OP_HISTORY]({"limit": 5})
    assert asked == [("in_repo", "/repo-a", 5)]


def test_a_roster_row_never_carries_a_socket_path_across_the_boundary(monkeypatch):
    """A Unix socket path and a pid are coordinates in one kernel. Sent
    across a machine boundary they are at best meaningless, and a reader
    that ADOPTED one would hold a PeerInfo naming a path in its own
    filesystem that some unrelated process may own. Blanked on the way out
    AND on the way in, by the same rule ``origin`` already follows."""
    monkeypatch.setattr(
        peers_mod, "read_registry",
        lambda **kw: [_peer("srv-1", daemon_socket="/run/doxa/daemon.sock")],
    )
    rows = peernet_mod.local_handlers()[peernet_mod.OP_ROSTER]({})["peers"]
    assert rows[0]["session_id"] == "srv-1"
    assert rows[0]["socket_path"] == ""
    assert rows[0]["pid"] == 0
    assert rows[0]["daemon_socket"] is None


async def test_fetch_roster_refuses_a_socket_path_the_reply_supplied(monkeypatch):
    """The receiving half, asserted against a server that sends them
    anyway -- a bridge on an older build, or one that is not DOXA at all."""
    _permissive(monkeypatch)
    monkeypatch.setattr(peers_mod, "read_registry", lambda **kw: [])
    leaky = dict(vars(_peer("remote-1", daemon_socket="/run/theirs.sock")))
    handlers = {peernet_mod.OP_ROSTER: lambda body: {"peers": [leaky]}}
    server = peernet_mod.PeerNetServer(handlers, host="127.0.0.1", port=0)
    await server.start()
    endpoint = peernet_mod.Endpoint("workstation", "127.0.0.1", server.port_in_use)
    try:
        fetched = await peernet_mod.fetch_roster(endpoint, login=LOGIN)
    finally:
        await server.stop()

    assert [p.session_id for p in fetched] == ["remote-1"]
    assert fetched[0].origin == "workstation"
    assert fetched[0].socket_path == ""
    assert fetched[0].pid == 0
    assert fetched[0].daemon_socket is None

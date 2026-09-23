# SPDX-License-Identifier: AGPL-3.0-only
"""The stdio MCP server that carries DOXA's operators to Codex.

TWO LEVELS, deliberately. The tests that assert PROTOCOL facts -- what
``initialize`` answers, what ``tools/list`` contains, what a ``tools/call``
returns -- drive a REAL ``python -m doxa.mcpserver`` subprocess through the
``mcp`` client, because a fake would only prove that this file and that one
agree. The tests that assert CONTAINMENT facts -- two strikes, the disable,
write staging -- drive :class:`doxa.mcpserver.OperatorSurface` in-process
and check it against a bare :class:`doxa.gate.ToolGate` doing the same
thing, because the claim being made is "the server adds no gating of its
own", and the only way to show that is to run both and compare.

Every subprocess here gets its own LORE_ROOT under tmp_path: the server
reads the store for real, and a test that seeded the suite's shared one
would be a test that passes only in the order it happens to run in.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from doxa import mcpserver as mcpserver_mod
from doxa.mcpserver import (
    ENV_CWD,
    ENV_ENGINE,
    ENV_ENGINE_SOCKET,
    ENV_LORE,
    ENV_PEER_SEND,
    ENV_SESSION_ID,
    ENV_SPAWN_DEPTH,
    ENV_TURN_ID,
    Identity,
    OperatorSurface,
    identity_from_env,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

SEEDED = "The probe's codename is QUARTZ-LANTERN-8812."


def _store(tmp_path: Path, *, seed: bool = True) -> Path:
    """A throwaway LORE store, optionally with one user-memory line in it.

    ``USER.md`` written directly rather than through ``lore memory add``:
    the file IS the format (``lore_core.memory.read_entries`` takes any
    ``"- "`` line), and shelling out to the CLI would put a second
    installed tool in the way of a test about this one."""
    root = tmp_path / "lore"
    root.mkdir(parents=True, exist_ok=True)
    if seed:
        (root / "USER.md").write_text(f"- {SEEDED}\n", encoding="utf-8")
    return root


def _env(tmp_path: Path, **overrides: str) -> "dict[str, str]":
    """The environment contract doxa.mcpserver documents, plus the paths
    that keep this subprocess out of the developer's real state."""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "PYTHONPATH": str(REPO_ROOT),
        "LORE_ROOT": str(_store(tmp_path)),
        "LORE_PROJECTS_DIR": str(tmp_path / "projects"),
        "DOXA_RUNTIME_DIR": str(tmp_path / "runtime"),
        "DOXA_HOME": str(tmp_path / "doxa-home"),
        ENV_SESSION_ID: "s-under-test",
        ENV_CWD: str(tmp_path),
    }
    env.update(overrides)
    return env


@asynccontextmanager
async def _client(tmp_path: Path, *, args: "tuple[str, ...]" = (), **env: str):
    """An initialized MCP client talking to a real server subprocess."""
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "doxa.mcpserver", *args],
        env=_env(tmp_path, **env),
        cwd=str(REPO_ROOT),
    )
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            yield session, await session.initialize()


def _text(result) -> str:
    return result.content[0].text


def _surface(tmp_path: Path, **kwargs) -> OperatorSurface:
    """An in-process surface with the same identity the subprocess gets."""
    return OperatorSurface(
        Identity(session_id="s-under-test", cwd=str(tmp_path), **kwargs)
    )


# -- the environment contract ------------------------------------------


def test_the_environment_contract_is_read_exactly_as_documented(tmp_path):
    identity = identity_from_env({
        ENV_SESSION_ID: "s-7", ENV_CWD: str(tmp_path),
        ENV_ENGINE: "codex",
        ENV_SPAWN_DEPTH: "2", ENV_LORE: "0", ENV_PEER_SEND: "1",
    })
    assert identity == Identity(
        session_id="s-7", cwd=str(tmp_path), source_engine="codex",
        spawn_depth=2, lore=False, peer_send=True,
    )


def test_the_two_engine_variables_are_spelled_once_for_both_ends(tmp_path):
    """``DOXA_MCP_ENGINE_SOCKET`` and ``DOXA_MCP_TURN_ID`` are set by
    doxa.codex and read by doxa.peerdelivery; this module names them so
    the documented contract and the code cannot say different things.
    Imported rather than repeated, which is what this asserts."""
    from doxa import peerdelivery as peerdelivery_mod

    assert ENV_ENGINE_SOCKET == peerdelivery_mod.ENGINE_SOCKET_ENV
    assert ENV_TURN_ID == peerdelivery_mod.ENGINE_TURN_ENV
    assert ENV_ENGINE_SOCKET in mcpserver_mod.IDENTITY_ENV
    assert ENV_TURN_ID in mcpserver_mod.IDENTITY_ENV


def test_an_empty_environment_is_a_running_server_not_a_crash():
    """The server is spawned by a CLI DOXA does not control. A missing
    variable has to read as "no session", never as an exception that takes
    the whole tool surface down before the first request."""
    identity = identity_from_env({})
    assert identity.session_id == ""
    assert identity.lore is True          # ON unless explicitly turned off
    assert identity.peer_send is False    # OFF unless explicitly asked for
    assert identity.spawn_depth == 0


def test_a_nonsense_spawn_depth_reads_as_zero_not_as_a_traceback():
    assert identity_from_env({ENV_SPAWN_DEPTH: "deep"}).spawn_depth == 0
    assert identity_from_env({ENV_SPAWN_DEPTH: "-3"}).spawn_depth == 0


# -- the protocol, against a real subprocess ---------------------------


@pytest.mark.asyncio
async def test_initialize_and_tools_list_match_configured_names(tmp_path):
    """The whole projection claim, in one test: what the server offers over
    the wire is exactly ``operators.configured_names`` for the ctx the
    engine's identity produces -- no extra tool, no missing one."""
    from doxa.operators import configured_names

    surface = _surface(tmp_path)
    expected = configured_names(surface.ctx)

    async with _client(tmp_path) as (session, init):
        assert init.server_info.name == mcpserver_mod.SERVER_NAME
        listed = await session.list_tools()

    assert {tool.name for tool in listed.tools} == expected
    # ...and the same set the in-process surface computes, so a future
    # change cannot make the two halves agree by both being wrong.
    assert {t["name"] for t in surface.tools()} == expected
    # The five lore_* tools plus the two read-only peer ones. peer_send is
    # absent: this environment names no engine control socket, so there is
    # nothing to forward a send to (see _resolve_delivery).
    assert "lore_memory_list" in expected and "peer_list" in expected
    assert "peer_send" not in expected


@pytest.mark.asyncio
async def test_a_lore_read_returns_the_operators_own_result(tmp_path):
    """The seeded line comes back through the wire verbatim -- this is the
    end-to-end claim ``mcp_tools=True`` makes for Codex."""
    async with _client(tmp_path) as (session, _init):
        result = await session.call_tool("lore_memory_list", {"scope": "all"})

    assert result.is_error is False
    payload = json.loads(_text(result))
    assert payload["user"]["entries"] == [SEEDED]


@pytest.mark.asyncio
async def test_an_unknown_tool_is_a_result_the_model_reads_not_a_crash(tmp_path):
    """ToolGate.execute's never-raises contract, preserved across the
    transport: a bad call must come back as an ordinary is_error result,
    because a server that died here would take the turn with it."""
    async with _client(tmp_path) as (session, _init):
        result = await session.call_tool("lore_nonesuch", {})

    assert result.is_error is True
    assert "unknown tool" in json.loads(_text(result))["error"]


@pytest.mark.asyncio
async def test_memory_off_removes_the_lore_tools_rather_than_refusing_them(
    tmp_path,
):
    """README's promise, and the reason it is an ABSENCE: a tool the model
    cannot see is a tool the model cannot call, which is a stronger
    position than a refusal it can retry. Both spellings of the switch."""
    async with _client(tmp_path, args=("--no-lore",)) as (session, _init):
        by_flag = {t.name for t in (await session.list_tools()).tools}
    async with _client(tmp_path, **{ENV_LORE: "0"}) as (session, _init):
        by_env = {t.name for t in (await session.list_tools()).tools}

    assert by_flag == by_env
    assert not [name for name in by_flag if name.startswith("lore_")]
    # ...and the peer tools are untouched: memory off is not tools off.
    assert by_flag == {"peer_list", "peer_history"}


@pytest.mark.asyncio
async def test_a_write_is_staged_and_is_the_same_result_the_gate_gives(tmp_path):
    """``lore_remember`` reaches the model (it is a WRITE_OPERATOR and the
    server projects those, as doxa.vendors does) and it STAGES -- nothing
    enters curated memory. Checked against ToolGate.execute directly, so
    the claim "the server adds no behaviour of its own" is measured rather
    than asserted."""
    from doxa.gate import OperatorContext, ToolGate, repo_root_of
    from lore_core import store as lore_store
    from lore_core.config import ROOT as SUITE_LORE_ROOT

    async with _client(tmp_path) as (session, _init):
        wire = json.loads(_text(await session.call_tool(
            "lore_remember", {"text": "probe fact one", "scope": "project"},
        )))

    direct = ToolGate(
        allowed=None,
        op_ctx=OperatorContext(
            session_id="s-under-test", cwd=str(tmp_path),
            repo_root=repo_root_of(str(tmp_path)),
            belief_store=lore_store.db_connect,
        ),
    ).execute("lore_remember", {"text": "probe fact one", "scope": "project"})

    # Same keys, same scope, same note. The id differs (it is a timestamp
    # in each process's own store) and so does the store -- the point is
    # the SHAPE, and that neither one wrote memory.
    assert set(wire) == set(direct) == {"staged", "scope", "text", "note"}
    assert wire["scope"] == direct["scope"] == "project"
    assert wire["note"] == direct["note"]
    assert "human approves" in wire["note"]

    # STAGED means staged: a pending file exists in the subprocess's store
    # and no curated MEMORY.md was written.
    server_root = Path(_env(tmp_path)["LORE_ROOT"])
    assert list((server_root / "pending").glob("*.json"))
    assert not list(server_root.rglob("MEMORY.md"))
    # ...and that store is not the suite's, which is the isolation this
    # file's docstring promises.
    assert str(SUITE_LORE_ROOT) != str(server_root)


# -- containment, in-process, against a bare gate ----------------------


def _boom(**_kwargs) -> dict:
    raise RuntimeError("backend is down")


@pytest.mark.asyncio
async def test_two_hard_failures_disable_the_tool_and_drop_it_from_the_list(
    tmp_path, monkeypatch,
):
    """The two-strikes tracker, and the one thing this transport adds to
    it: ``tools/list`` is recomputed per request, so the SECOND failure
    both refuses the tool and removes it from the offered surface.

    A bare ToolGate is run beside it on the same operator, so the strike
    counting itself is shown to be the gate's and not a reimplementation.
    The disable is per TURN here -- Codex spawns one server per ``codex
    exec`` run -- which is what the module docstring says and is why no
    test asserts it survives a restart."""
    from doxa import operators as operators_mod
    from doxa.gate import ToolGate

    broken = replace(operators_mod.OPERATORS["lore_memory_list"], fn=_boom)
    monkeypatch.setitem(operators_mod.OPERATORS, "lore_memory_list", broken)

    surface = _surface(tmp_path)
    bare = ToolGate(allowed=None)

    first = await surface.call("lore_memory_list", {})
    assert first["error"].startswith("lore_memory_list failed:")
    assert "lore_memory_list" in {t["name"] for t in surface.tools()}

    second = await surface.call("lore_memory_list", {})
    assert second["error"].startswith("lore_memory_list failed:")
    assert surface.gate.disabled_tools() == ["lore_memory_list"]
    assert surface.disabled_seen == ["lore_memory_list"]
    # Gone from the offered surface, not merely refused.
    assert "lore_memory_list" not in {t["name"] for t in surface.tools()}

    third = await surface.call("lore_memory_list", {})
    assert "disabled for the rest of this session" in third["error"]

    # The bare gate, given the same two calls, reaches the same state --
    # so nothing above is this module's own counting.
    bare.execute("lore_memory_list", {})
    bare.execute("lore_memory_list", {})
    assert bare.disabled_tools() == ["lore_memory_list"]


def test_the_op_ctx_carries_only_host_resolved_values(tmp_path):
    """Contract 4 of doxa.gate, unchanged by the extra process hop: the
    sidecar is built from the engine's identity, and the model writes only
    into ``args``."""
    surface = _surface(tmp_path, spawn_depth=3)
    ctx = surface.gate.op_ctx
    assert ctx.session_id == "s-under-test"
    assert ctx.cwd == str(tmp_path)
    assert ctx.spawn_depth == 3
    # No human to ask and no outbound peer path from this surface.
    assert ctx.spawn_confirm is None
    assert ctx.peer_send is None


@pytest.mark.asyncio
async def test_a_model_supplied_op_ctx_is_stripped(tmp_path):
    """The one argument the model must never be able to set. Asserted here
    too (and not only in test_gate.py) because this transport hands the
    arguments dict straight off the wire."""
    surface = _surface(tmp_path)
    result = await surface.call(
        "lore_memory_list", {"scope": "all", "op_ctx": {"cwd": "/etc"}},
    )
    from lore_core.config import project_slug

    # No TypeError from a duplicated kwarg, and the slug came from the
    # TRUSTED cwd on the sidecar rather than the one the model wrote.
    assert "error" not in result
    assert result["project_slug"] == project_slug(str(tmp_path))


# -- the peer_send seam ------------------------------------------------


def test_peer_send_is_not_offered_without_an_engine_socket(tmp_path, monkeypatch):
    """A sidecar with no engine behind it offers NOTHING rather than a
    tool that refuses: absence is what the model cannot call, and a
    refusal is something it retries. Never wired to peers.send_message,
    which would bypass the send-side rate limiter and the ledger."""
    monkeypatch.delenv(ENV_ENGINE_SOCKET, raising=False)
    assert mcpserver_mod._resolve_delivery(
        Identity(session_id="s", cwd=str(tmp_path), peer_send=True)
    ) is None
    assert "peer_send" not in {t["name"] for t in _surface(tmp_path).tools()}


def test_the_resolved_seam_forwards_and_never_sends_here(tmp_path, monkeypatch):
    """What ``delivery_for`` hands back is the FORWARDING seam. This
    process holds no limiter, no ledger and no PeerHost, and the object
    the gate ends up carrying has to be the one that knows that."""
    from doxa.peerdelivery import SidecarDelivery

    monkeypatch.setenv(ENV_ENGINE_SOCKET, str(tmp_path / "engine.sock"))
    monkeypatch.setenv(ENV_TURN_ID, "t-42")
    seam = mcpserver_mod._resolve_delivery(
        Identity(session_id="s", cwd=str(tmp_path), peer_send=True)
    )
    assert seam is not None
    assert isinstance(seam.__self__, SidecarDelivery)
    assert seam.__func__ is SidecarDelivery.tool_send
    assert seam.__self__.turn_id == "t-42"


def test_the_switch_off_beats_a_named_engine_socket(tmp_path, monkeypatch):
    """``DOXA_MCP_PEER_SEND=0`` is the engine reporting the USER's switch.
    It is checked before anything else, so a socket that happens to be
    named cannot arm the tool behind the user's back."""
    monkeypatch.setenv(ENV_ENGINE_SOCKET, str(tmp_path / "engine.sock"))
    assert mcpserver_mod._resolve_delivery(
        Identity(session_id="s", cwd=str(tmp_path), peer_send=False)
    ) is None


@asynccontextmanager
async def _fake_engine(tmp_path: Path, *, reply: "dict | None" = None):
    """A stand-in for :class:`doxa.peerdelivery.EngineControl`: it speaks
    the protocol's engine half and records what the sidecar forwarded.

    Deliberately not the real one. What this file tests is that the
    SIDECAR forwards -- the request, whole, with the turn id -- and a fake
    listener is the only way to read that off the wire rather than off a
    result. The real engine performing the send is asserted in
    tests/test_peer_delivery.py, against a real second session."""
    seen: "list[dict]" = []
    path = tmp_path / "engine.sock"
    answer = reply if reply is not None else {
        "ok": True,
        "result": {"delivered_to": [{"session_id": "peer-01", "title": "t",
                                     "repo": "/repo"}], "kind": "direct"},
    }

    async def handle(reader, writer):
        line = await reader.readline()
        try:
            seen.append(json.loads(line))
        except ValueError:
            seen.append({"unparseable": line.decode("utf-8", "replace")})
        writer.write(json.dumps(answer).encode("utf-8") + b"\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_unix_server(handle, path=str(path))
    try:
        yield path, seen
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_a_real_sidecar_forwards_peer_send_to_its_engine(tmp_path):
    """The whole seam, over a real stdio MCP server subprocess: the tool
    is offered, a ``tools/call`` reaches the ENGINE with the operator's
    request and the turn id the engine spawned this server for, and what
    comes back to the model is the engine's own result dict."""
    async with _fake_engine(tmp_path) as (sock, seen):
        async with _client(tmp_path, **{
            ENV_PEER_SEND: "1",
            ENV_ENGINE_SOCKET: str(sock),
            ENV_TURN_ID: "t-77",
            "DOXA_AGENT_PEER_SEND": "1",
        }) as (session, _init):
            listed = {t.name for t in (await session.list_tools()).tools}
            assert "peer_send" in listed
            result = await session.call_tool(
                "peer_send", {"body": "ready", "to": "peer-01"},
            )

    assert len(seen) == 1, "the send was not forwarded exactly once"
    assert seen[0]["op"] == "peer_send"
    assert seen[0]["turn_id"] == "t-77", "the ledger row would name no turn"
    assert seen[0]["request"]["body"] == "ready"
    assert seen[0]["request"]["to"] == "peer-01"
    assert seen[0]["request"]["broadcast"] is False

    assert result.is_error is False
    payload = json.loads(_text(result))
    assert payload["delivered_to"][0]["session_id"] == "peer-01"


@pytest.mark.asyncio
async def test_an_engine_that_refused_is_a_result_the_model_reads(tmp_path):
    """The engine's refusal comes back in the operator's own single-colon
    shape. It must NOT read ``peer_send failed: ...`` -- that is the
    string doxa.gate.is_hard_failure counts as a strike, and a limiter
    doing its job twice would disable the tool."""
    async with _fake_engine(
        tmp_path, reply={"ok": False, "error": "over the per-turn limit"},
    ) as (sock, _seen):
        async with _client(tmp_path, **{
            ENV_PEER_SEND: "1",
            ENV_ENGINE_SOCKET: str(sock),
            "DOXA_AGENT_PEER_SEND": "1",
        }) as (session, _init):
            result = await session.call_tool(
                "peer_send", {"body": "ready", "to": "peer-01"},
            )

    error = json.loads(_text(result))["error"]
    assert error == "peer_send: over the per-turn limit"
    from doxa.gate import is_hard_failure

    assert is_hard_failure("peer_send", {"error": error}) is False


@pytest.mark.asyncio
async def test_a_missing_engine_is_a_refusal_not_a_local_send(tmp_path):
    """The failure mode this whole design exists to prevent: with the
    engine gone the sidecar refuses. It has no PeerHost, no limiter and
    no ledger, so anything it "delivered" would be off the record."""
    async with _client(tmp_path, **{
        ENV_PEER_SEND: "1",
        ENV_ENGINE_SOCKET: str(tmp_path / "nothing-listens-here.sock"),
        "DOXA_AGENT_PEER_SEND": "1",
    }) as (session, _init):
        result = await session.call_tool(
            "peer_send", {"body": "ready", "to": "peer-01"},
        )

    error = json.loads(_text(result))["error"]
    assert error.startswith("peer_send: ")
    assert "nothing was sent" in error


def test_peer_send_appears_the_moment_a_seam_is_passed(tmp_path, monkeypatch):
    """The other half, so the absence above is shown to be the SEAM's
    doing and not a tool that can never be reached. This is exactly the
    shape doxa.peerdelivery has to hand back."""
    monkeypatch.setenv("DOXA_AGENT_PEER_SEND", "1")

    async def _deliver(_payload: dict) -> dict:
        return {"sent": True}

    surface = OperatorSurface(
        Identity(session_id="s", cwd=str(tmp_path)), delivery=_deliver,
    )
    assert "peer_send" in {t["name"] for t in surface.tools()}
    assert surface.gate.op_ctx.peer_send is _deliver


# -- the tool definitions ----------------------------------------------


def test_every_tool_carries_its_schema_and_its_cost_tier(tmp_path):
    """What the model actually reads about a tool. The description suffix
    is the same one doxa.vendors.operator_tools builds, so a Codex session
    weighs tool choice on the same text a DeepSeek one does."""
    from doxa.operators import OPERATORS

    tools = {t["name"]: t for t in _surface(tmp_path).tools()}
    listing = tools["lore_memory_list"]
    assert listing["inputSchema"] is OPERATORS["lore_memory_list"].parameters
    assert listing["inputSchema"]["type"] == "object"
    assert f"[cost: {OPERATORS['lore_memory_list'].cost}]" in listing["description"]
    # A write operator says so, in the one text the model reads about it.
    assert "[write:" in tools["lore_remember"]["description"]
    assert "[write:" not in listing["description"]

"""Engine wiring for the native-tool registry + gate: the in-process SDK
MCP server lands in ClaudeAgentOptions.mcp_servers, the PreToolUse hook
routes every tool through the gate, and a two-strikes disable surfaces as a
tool_disabled event on the out-of-band stream. Fake client throughout -- no
subprocess, no spend."""

from __future__ import annotations

import dataclasses
import json

import pytest

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    ServerToolResultBlock,
    ServerToolUseBlock,
)

from doxa import operators as ops
from doxa import transcript as transcript_mod
from doxa.engine import SessionEngine
from tests.fakes import factory_with_script


@pytest.mark.asyncio
async def test_engine_registers_the_native_tool_server(tmp_path):
    factory, created = factory_with_script([])
    engine = SessionEngine(cwd=str(tmp_path), client_factory=factory)
    await engine.start()

    servers = created[0].options.mcp_servers
    assert ops.SDK_SERVER_NAME in servers
    config = servers[ops.SDK_SERVER_NAME]
    # create_sdk_mcp_server's in-process shape: type "sdk" + a live
    # mcp.server.Server instance, no subprocess/IPC per call (PHASE0 SS6).
    assert config["type"] == "sdk"
    assert config["name"] == ops.SDK_SERVER_NAME
    assert config["instance"] is not None
    await engine.finalize()


@pytest.mark.asyncio
async def test_pre_tool_use_hook_routes_through_the_gate(tmp_path):
    factory, _created = factory_with_script([])
    engine = SessionEngine(
        cwd=str(tmp_path), client_factory=factory,
        allowed_tools={"lore_memory_list", "Read"},
    )
    await engine.start()

    # Offered tools and SDK built-ins inside the policy pass untouched.
    assert await engine._on_pre_tool_use(
        {"tool_name": "mcp__doxa__lore_memory_list", "tool_input": {}}, None, None) == {}
    assert await engine._on_pre_tool_use(
        {"tool_name": "Read", "tool_input": {"file_path": "/x"}}, None, None) == {}

    # Anything outside the allowed set -- native or built-in -- gets a
    # graceful hook deny, never an exception.
    denied = await engine._on_pre_tool_use(
        {"tool_name": "mcp__doxa__lore_belief_search", "tool_input": {"query": "x"}},
        None, None)
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    denied_builtin = await engine._on_pre_tool_use(
        {"tool_name": "Bash", "tool_input": {"command": "ls"}}, None, None)
    assert denied_builtin["hookSpecificOutput"]["permissionDecision"] == "deny"
    await engine.finalize()


@pytest.mark.asyncio
async def test_default_engine_allows_everything_at_the_hook(tmp_path):
    factory, _created = factory_with_script([])
    engine = SessionEngine(cwd=str(tmp_path), client_factory=factory)
    await engine.start()
    assert await engine._on_pre_tool_use(
        {"tool_name": "Bash", "tool_input": {"command": "ls"}}, None, None) == {}
    assert await engine._on_pre_tool_use(
        {"tool_name": "mcp__doxa__lore_belief_search", "tool_input": {"query": "x"}},
        None, None) == {}
    await engine.finalize()


@pytest.mark.asyncio
async def test_two_strikes_emits_tool_disabled_on_the_out_of_band_stream(
        tmp_path, monkeypatch):
    factory, _created = factory_with_script([])
    engine = SessionEngine(cwd=str(tmp_path), client_factory=factory)
    await engine.start()

    def boom(**kwargs):
        raise RuntimeError("belief db unavailable")

    real = ops.OPERATORS["lore_belief_search"]
    monkeypatch.setitem(ops.OPERATORS, "lore_belief_search",
                        dataclasses.replace(real, fn=boom))

    # The gate's executor is exactly what the projected SDK handlers call.
    engine.tool_gate.execute("mcp__doxa__lore_belief_search", {"query": "a"})
    assert engine.disabled_tools() == []
    engine.tool_gate.execute("mcp__doxa__lore_belief_search", {"query": "b"})

    assert engine.disabled_tools() == ["lore_belief_search"]
    ev = engine._peer_queue.get_nowait()
    assert ev.type == "tool_disabled"
    assert ev.data["name"] == "lore_belief_search"
    assert "failed" in ev.data["reason"]
    await engine.finalize()


# -- server-side tools (v0.43.0) ---------------------------------------
#
# Tools the API runs on the model's behalf ("advisor", and whatever else
# joins claude_agent_sdk.ServerToolName) do not arrive as the
# ToolUseBlock/ToolResultBlock pair a client-side call does. The installed
# SDK parses them into ServerToolUseBlock and ServerToolResultBlock, and
# puts BOTH on the assistant message -- so the result never passed under
# the UserMessage branch that renders ordinary tool results, and until
# v0.43.0 nothing else looked at it either: the call and its answer both
# vanished, with no error to tell the user the tool had run at all.


@pytest.mark.asyncio
async def test_server_tool_call_and_result_both_reach_the_ui(tmp_path):
    script = [
        AssistantMessage(
            content=[ServerToolUseBlock(
                id="srvtoolu-1", name="advisor", input={"query": "who owns billing?"},
            )],
            model="claude-haiku-4-5",
        ),
        AssistantMessage(
            content=[ServerToolResultBlock(
                tool_use_id="srvtoolu-1",
                content={"type": "advisor_tool_result", "content": [
                    {"type": "text", "text": "billing is owned by the payments team"},
                ]},
            )],
            model="claude-haiku-4-5",
        ),
        ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
            num_turns=1, session_id="s", total_cost_usd=0.0,
        ),
    ]
    factory, _created = factory_with_script(script)
    engine = SessionEngine(cwd=str(tmp_path), client_factory=factory)
    await engine.start()
    events = [ev async for ev in engine.send("who owns billing?")]

    calls = [e for e in events if e.type == "tool_call"]
    results = [e for e in events if e.type == "tool_result"]
    assert [e.data["name"] for e in calls] == ["advisor"]
    assert [e.data["id"] for e in results] == ["srvtoolu-1"]
    # The user-visible part: the result reads as the answer it is, and is
    # attributed to the tool that produced it.
    assert results[0].data["name"] == "advisor"
    assert results[0].data["is_error"] is False
    assert "payments team" in results[0].data["result_summary"]
    await engine.finalize()


@pytest.mark.asyncio
async def test_server_tool_result_of_an_unknown_shape_still_renders(tmp_path):
    """The SDK types a server tool's result as an opaque dict on purpose --
    every server tool has its own schema. A shape this app has never seen
    must still show the reader SOMETHING; silently dropping it is the bug
    this whole section exists for."""
    script = [
        AssistantMessage(
            content=[ServerToolUseBlock(
                id="srvtoolu-2", name="web_search", input={"query": "textual release"},
            )],
            model="claude-haiku-4-5",
        ),
        AssistantMessage(
            content=[ServerToolResultBlock(
                tool_use_id="srvtoolu-2",
                content={"type": "web_search_tool_result", "results": [
                    {"title": "textual on PyPI", "url": "https://pypi.org/project/textual/"},
                ]},
            )],
            model="claude-haiku-4-5",
        ),
        ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
            num_turns=1, session_id="s", total_cost_usd=0.0,
        ),
    ]
    factory, _created = factory_with_script(script)
    engine = SessionEngine(cwd=str(tmp_path), client_factory=factory)
    await engine.start()
    events = [ev async for ev in engine.send("find it")]

    results = [e for e in events if e.type == "tool_result"]
    assert len(results) == 1
    assert "textual on PyPI" in results[0].data["result_summary"]
    await engine.finalize()


@pytest.mark.asyncio
async def test_failed_server_tool_result_reads_as_an_error(tmp_path):
    script = [
        AssistantMessage(
            content=[ServerToolUseBlock(
                id="srvtoolu-3", name="advisor", input={"query": "x"},
            )],
            model="claude-haiku-4-5",
        ),
        AssistantMessage(
            content=[ServerToolResultBlock(
                tool_use_id="srvtoolu-3",
                content={"type": "advisor_tool_result_error", "error_code": "unavailable"},
            )],
            model="claude-haiku-4-5",
        ),
        ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
            num_turns=1, session_id="s", total_cost_usd=0.0,
        ),
    ]
    factory, _created = factory_with_script(script)
    engine = SessionEngine(cwd=str(tmp_path), client_factory=factory)
    await engine.start()
    events = [ev async for ev in engine.send("x")]

    results = [e for e in events if e.type == "tool_result"]
    assert results[0].data["is_error"] is True
    assert "unavailable" in results[0].data["result_summary"]
    await engine.finalize()


@pytest.mark.asyncio
async def test_server_tool_result_survives_a_transcript_restore(tmp_path):
    """A server tool's result is persisted where doxa.transcript's replay
    actually reads results from -- otherwise the answer comes back for one
    session and is gone from the restored scrollback, which is the same
    vanished-result bug one launch later."""
    script = [
        AssistantMessage(
            content=[ServerToolUseBlock(
                id="srvtoolu-4", name="advisor", input={"query": "x"},
            )],
            model="claude-haiku-4-5",
        ),
        AssistantMessage(
            content=[ServerToolResultBlock(
                tool_use_id="srvtoolu-4",
                content={"type": "advisor_tool_result", "content": [
                    {"type": "text", "text": "the payments team"},
                ]},
            )],
            model="claude-haiku-4-5",
        ),
        ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
            num_turns=1, session_id="s", total_cost_usd=0.0,
        ),
    ]
    factory, _created = factory_with_script(script)
    engine = SessionEngine(cwd=str(tmp_path), client_factory=factory)
    await engine.start()
    async for _ev in engine.send("who owns billing?"):
        pass
    path = engine.transcript_path
    await engine.finalize()

    records = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    turns = transcript_mod.parse(records)
    tools = [t for turn in turns for t in turn.tools]
    assert [t.name for t in tools] == ["advisor"]
    assert tools[0].result == "the payments team"

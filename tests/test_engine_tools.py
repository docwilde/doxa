"""Engine wiring for the native-tool registry + gate: the in-process SDK
MCP server lands in ClaudeAgentOptions.mcp_servers, the PreToolUse hook
routes every tool through the gate, and a two-strikes disable surfaces as a
tool_disabled event on the out-of-band stream. Fake client throughout -- no
subprocess, no spend."""

from __future__ import annotations

import dataclasses

import pytest

from doxa import operators as ops
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

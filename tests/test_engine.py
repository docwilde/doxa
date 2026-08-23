"""Engine event-stream unit tests: a fake claude_agent_sdk client, no
subprocess, no network. Covers: typed events yielded in order, the LORE
snapshot landing in system_prompt at start(), the secret-scrub choke point
applied before anything touches disk, and finalize() running exactly once.
"""

from __future__ import annotations

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

from doxa.engine import SessionEngine
from tests.fakes import factory_with_script


def _script_one_turn_with_tool_call() -> list:
    return [
        StreamEvent(
            uuid="stream-1", session_id="s",
            event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hello"}},
        ),
        AssistantMessage(content=[TextBlock(text="Hello")], model="claude-haiku-4-5"),
        AssistantMessage(
            content=[ToolUseBlock(id="tool-1", name="calculator_add", input={"a": 1, "b": 2})],
            model="claude-haiku-4-5",
        ),
        UserMessage(content=[ToolResultBlock(tool_use_id="tool-1", content="3", is_error=False)]),
        ResultMessage(
            subtype="success", duration_ms=42, duration_api_ms=40, is_error=False,
            num_turns=1, session_id="s", total_cost_usd=0.001,
        ),
    ]


@pytest.mark.asyncio
async def test_start_captures_account_and_init_names_the_model(tmp_path):
    """Identity surface: start() captures the CLI's connect-time account
    block via get_server_info (only the fields it actually reports), and the
    first turn's init SystemMessage names the ACTUAL session model when the
    engine rode the CLI default (model=None)."""
    from claude_agent_sdk import SystemMessage

    script = [
        SystemMessage(subtype="init", data={"model": "claude-haiku-4-5"}),
        ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
            num_turns=1, session_id="s", total_cost_usd=0.0,
        ),
    ]
    factory, created = factory_with_script(script, server_info={
        "account": {
            "email": "doc@example.org", "organization": "Doc's Org",
            "subscriptionType": "Claude Max", "apiProvider": "firstParty",
        },
        "output_style": "default",
    })
    engine = SessionEngine(cwd=str(tmp_path), client_factory=factory)
    await engine.start()
    assert engine.account["email"] == "doc@example.org"
    assert engine.account["subscriptionType"] == "Claude Max"
    assert engine.server_info["output_style"] == "default"
    assert engine.lore_root  # LORE store path, for the identity block
    assert engine.model is None  # CLI default until init says otherwise

    events = [ev async for ev in engine.send("hi")]
    assert events[-1].type == "turn_done"
    assert engine.model == "claude-haiku-4-5"
    await engine.finalize()


@pytest.mark.asyncio
async def test_start_without_server_info_leaves_identity_empty(tmp_path):
    """No initialize payload (fakes, older SDKs, API-key non-streaming):
    the identity surface stays empty -- never guessed."""
    factory, _created = factory_with_script([])
    engine = SessionEngine(cwd=str(tmp_path), client_factory=factory)
    await engine.start()
    assert engine.account == {}
    assert engine.server_info is None
    await engine.finalize()


@pytest.mark.asyncio
async def test_start_injects_lore_snapshot_into_system_prompt(tmp_path):
    factory, created = factory_with_script([])
    engine = SessionEngine(cwd=str(tmp_path), model="claude-haiku-4-5", client_factory=factory)

    event = await engine.start()

    assert event.type == "session_started"
    assert created[0].entered is True
    system_prompt = created[0].options.system_prompt
    assert system_prompt["type"] == "preset"
    assert system_prompt["preset"] == "claude_code"
    # PHASE0 redesign item 2: snapshot injection is the system_prompt
    # append, not a SessionStart hook -- this is the assertion that matters.
    assert "LORE SNAPSHOT" in system_prompt["append"]
    assert "hooks" in vars(created[0].options) or created[0].options.hooks
    assert set(created[0].options.hooks) == {"UserPromptSubmit", "PreCompact", "PreToolUse"}


@pytest.mark.asyncio
async def test_send_yields_typed_events_in_order(tmp_path):
    factory, created = factory_with_script(
        _script_one_turn_with_tool_call(), ctx_usage={"percentage": 12.5}
    )
    engine = SessionEngine(cwd=str(tmp_path), client_factory=factory)
    await engine.start()

    events = [ev async for ev in engine.send("what is 1+2?")]

    assert [e.type for e in events] == [
        "turn_started", "text_delta", "tool_call", "tool_result", "turn_done",
    ]
    assert created[0].queried == [("what is 1+2?", engine.session_id)]

    tool_call = next(e for e in events if e.type == "tool_call")
    assert tool_call.data["name"] == "calculator_add"
    assert tool_call.data["input"] == {"a": 1, "b": 2}

    tool_result = next(e for e in events if e.type == "tool_result")
    assert tool_result.data["result_summary"] == "3"
    assert tool_result.data["is_error"] is False
    assert tool_result.data["duration_ms"] is not None

    turn_done = next(e for e in events if e.type == "turn_done")
    assert turn_done.data["cost_usd"] == pytest.approx(0.001)
    assert turn_done.data["ctx_percentage"] == pytest.approx(12.5)
    assert engine.total_cost_usd == pytest.approx(0.001)


@pytest.mark.asyncio
async def test_secret_scrub_applied_before_persistence(tmp_path):
    factory, created = factory_with_script([
        ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
            num_turns=1, session_id="s", total_cost_usd=0.0,
        ),
    ])
    engine = SessionEngine(cwd=str(tmp_path), client_factory=factory)
    await engine.start()

    secret_prompt = "my key is AKIAABCDEFGHIJKLMNOP, do not leak it"
    async for _ in engine.send(secret_prompt):
        pass

    transcript = engine.transcript_path.read_text(encoding="utf-8")
    assert "AKIAABCDEFGHIJKLMNOP" not in transcript
    assert "[REDACTED:aws]" in transcript


@pytest.mark.asyncio
async def test_secret_scrub_applied_to_tool_input_and_result(tmp_path):
    script = [
        AssistantMessage(
            content=[ToolUseBlock(
                id="tool-1", name="Bash",
                input={"command": "curl -H 'Authorization: Bearer AKIAABCDEFGHIJKLMNOP' https://x"},
            )],
            model="claude-haiku-4-5",
        ),
        UserMessage(content=[ToolResultBlock(
            tool_use_id="tool-1", content="token AKIAABCDEFGHIJKLMNOP accepted", is_error=False,
        )]),
        ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
            num_turns=1, session_id="s", total_cost_usd=0.0,
        ),
    ]
    factory, created = factory_with_script(script)
    engine = SessionEngine(cwd=str(tmp_path), client_factory=factory)
    await engine.start()

    events = [ev async for ev in engine.send("run the curl")]

    tool_call = next(e for e in events if e.type == "tool_call")
    assert "AKIAABCDEFGHIJKLMNOP" not in tool_call.data["input"]["command"]

    tool_result = next(e for e in events if e.type == "tool_result")
    assert "AKIAABCDEFGHIJKLMNOP" not in tool_result.data["result_summary"]

    transcript = engine.transcript_path.read_text(encoding="utf-8")
    assert "AKIAABCDEFGHIJKLMNOP" not in transcript


@pytest.mark.asyncio
async def test_finalize_runs_once_and_disconnects_client(tmp_path):
    factory, created = factory_with_script([])
    engine = SessionEngine(cwd=str(tmp_path), client_factory=factory)
    await engine.start()

    first = await engine.finalize()
    assert first.type == "session_done"
    assert "already_finalized" not in first.data
    assert created[0].exited is True

    second = await engine.finalize()
    assert second.data.get("already_finalized") is True

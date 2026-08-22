"""Phase 0 spike 1/3 -- minimal working agent session.

Proves:
  1. The claude-agent-sdk package runs a real query end to end.
  2. A custom in-process tool (SDK MCP tool) is invoked by the model.
  3. The response streams, with per-chunk boundaries visible (not just one
     final blob) via `include_partial_messages=True`.
  4. Auth: this process sets NO ANTHROPIC_API_KEY. If the SDK requires one,
     it will fail loudly here -- that failure IS the finding.

Run: uv run python spike/01_loop.py
"""

import asyncio
import os

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    tool,
)


@tool("calculator_add", "Add two numbers and return the sum.", {"a": float, "b": float})
async def calculator_add(args: dict) -> dict:
    total = args["a"] + args["b"]
    return {"content": [{"type": "text", "text": f"{total}"}]}


async def main() -> None:
    assert "ANTHROPIC_API_KEY" not in os.environ, (
        "ANTHROPIC_API_KEY is set in this process -- spike is supposed to "
        "prove subscription-OAuth-only auth. Unset it and re-run."
    )
    print(f"[auth] ANTHROPIC_API_KEY present in env: {'ANTHROPIC_API_KEY' in os.environ}")

    from claude_agent_sdk import create_sdk_mcp_server

    calc_server = create_sdk_mcp_server(name="calc", tools=[calculator_add])

    options = ClaudeAgentOptions(
        model="claude-haiku-4-5",  # cheapest available model, per spike budget constraint
        system_prompt="You are a terse calculator assistant. Use the calculator_add tool for any addition. Answer in one short sentence.",
        mcp_servers={"calc": calc_server},
        allowed_tools=["mcp__calc__calculator_add"],
        include_partial_messages=True,  # per-chunk streaming boundaries
        max_turns=3,
    )

    chunk_count = 0
    tool_call_seen = False

    async with ClaudeSDKClient(options=options) as client:
        await client.query("What is 17.5 plus 24.25? Use the calculator tool.")

        async for message in client.receive_response():
            if isinstance(message, StreamEvent):
                chunk_count += 1
                ev = message.event
                ev_type = ev.get("type")
                if ev_type == "content_block_delta":
                    delta = ev.get("delta", {})
                    text = delta.get("text") or delta.get("partial_json") or ""
                    if text:
                        print(text, end="", flush=True)
                elif ev_type in ("message_start", "content_block_start", "content_block_stop", "message_stop"):
                    print(f"\n[stream-boundary] {ev_type}", flush=True)
            elif isinstance(message, SystemMessage):
                print(f"\n[system] subtype={message.subtype}")
            elif isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, ToolUseBlock):
                        tool_call_seen = True
                        print(f"\n[tool_use] {block.name}({block.input})")
                    elif isinstance(block, TextBlock) and not options.include_partial_messages:
                        print(f"\n[assistant text] {block.text}")
            elif isinstance(message, UserMessage):
                for block in message.content if isinstance(message.content, list) else []:
                    if isinstance(block, ToolResultBlock):
                        print(f"\n[tool_result] {block.content}")
            elif isinstance(message, ResultMessage):
                print(f"\n[result] duration_ms={message.duration_ms} "
                      f"total_cost_usd={message.total_cost_usd} "
                      f"num_turns={message.num_turns} "
                      f"is_error={message.is_error}")

    print(f"\n\n[spike-1 summary] stream_chunks_seen={chunk_count} tool_call_seen={tool_call_seen}")
    assert chunk_count > 0, "No StreamEvent chunks observed -- streaming granularity finding is negative"
    assert tool_call_seen, "Custom tool was never invoked"
    print("[spike-1] PASS")


if __name__ == "__main__":
    asyncio.run(main())

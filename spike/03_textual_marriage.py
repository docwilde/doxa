"""Phase 0 spike 3/3 -- Textual UI + claude-agent-sdk asyncio coexistence.

Proves:
  1. A minimal Textual app (one scrolling block list) can run a real
     claude-agent-sdk session concurrently, inside the SAME asyncio event
     loop that drives Textual's own rendering -- via `App.run_worker()`,
     which by default schedules the worker as a plain asyncio Task on the
     running loop (not a separate thread).
  2. Each SDK message / tool-call streams into a live, foldable
     `Collapsible` widget as it arrives -- not batched after the fact.
  3. The app is driven headlessly via Textual's own `run_test()` Pilot
     harness, so this script is non-interactive and verifiable without a
     human watching a terminal.

Run: uv run python spike/03_textual_marriage.py
"""

import asyncio
import os

from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Collapsible, Static

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
    create_sdk_mcp_server,
    tool,
)


@tool("word_count", "Count words in a string.", {"text": str})
async def word_count(args: dict) -> dict:
    n = len(args["text"].split())
    return {"content": [{"type": "text", "text": f"{n} words"}]}


class LoreBlockList(App):
    """Minimal vertical block-list TUI: each agent event becomes one
    foldable Collapsible block, mounted live as it streams in."""

    CSS = """
    Screen { align: center top; }
    VerticalScroll { width: 100%; height: 100%; }
    """

    def __init__(self) -> None:
        super().__init__()
        self.blocks_mounted = 0
        self.agent_done = False
        self.stream_chunk_count = 0
        self.tool_call_seen = False

    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="block-list")

    async def on_mount(self) -> None:
        # This is the crux of the spike: run_worker schedules run_agent()
        # as an asyncio Task on Textual's own running event loop (the
        # default `thread=False`). The SDK's subprocess-based transport
        # (also asyncio-native, using asyncio.create_subprocess_exec under
        # the hood) shares that same loop with Textual's own render/input
        # loop with no bridging code required.
        self.run_worker(self.run_agent(), exclusive=True)

    async def mount_block(self, title: str, body: str) -> None:
        container = self.query_one("#block-list", VerticalScroll)
        block = Collapsible(Static(body), title=title, collapsed=True)
        await container.mount(block)
        container.scroll_end(animate=False)
        self.blocks_mounted += 1

    async def run_agent(self) -> None:
        assert "ANTHROPIC_API_KEY" not in os.environ

        server = create_sdk_mcp_server(name="util", tools=[word_count])
        options = ClaudeAgentOptions(
            model="claude-haiku-4-5",
            system_prompt="You are terse. Use the word_count tool once, then answer in one short sentence.",
            mcp_servers={"util": server},
            allowed_tools=["mcp__util__word_count"],
            include_partial_messages=True,
            max_turns=3,
        )

        async with ClaudeSDKClient(options=options) as client:
            await client.query(
                "Count the words in 'the quick brown fox jumps over the lazy dog' "
                "using the tool, then tell me the count in one short sentence."
            )
            async for message in client.receive_response():
                if isinstance(message, StreamEvent):
                    self.stream_chunk_count += 1
                    ev_type = message.event.get("type")
                    if ev_type in ("content_block_start", "message_start"):
                        await self.mount_block(
                            f"stream: {ev_type}", str(message.event)[:300]
                        )
                elif isinstance(message, SystemMessage):
                    await self.mount_block(
                        f"system: {message.subtype}", str(message.data)[:300]
                    )
                elif isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, ToolUseBlock):
                            self.tool_call_seen = True
                            await self.mount_block(
                                f"tool_use: {block.name}", str(block.input)
                            )
                        elif isinstance(block, TextBlock):
                            await self.mount_block("assistant text", block.text)
                elif isinstance(message, UserMessage):
                    content = message.content if isinstance(message.content, list) else []
                    for block in content:
                        if isinstance(block, ToolResultBlock):
                            await self.mount_block("tool_result", str(block.content))
                elif isinstance(message, ResultMessage):
                    await self.mount_block(
                        "result",
                        f"duration_ms={message.duration_ms} "
                        f"cost_usd={message.total_cost_usd} "
                        f"is_error={message.is_error}",
                    )

        self.agent_done = True
        self.exit()


async def main() -> None:
    app = LoreBlockList()
    # Headless harness: drives the full Textual event loop (same loop the
    # SDK worker runs on) without needing a real terminal, and lets us
    # assert on final state afterward.
    async with app.run_test(size=(100, 40)) as pilot:
        # Give the worker time to run the full agent turn. Poll instead of
        # a fixed sleep so this finishes as soon as the agent is done.
        for _ in range(600):  # up to ~60s
            if app.agent_done:
                break
            await pilot.pause(0.1)

    print("\n[spike-3 summary]")
    print(f"  blocks_mounted={app.blocks_mounted}")
    print(f"  stream_chunk_count={app.stream_chunk_count}")
    print(f"  tool_call_seen={app.tool_call_seen}")
    print(f"  agent_done={app.agent_done}")

    assert app.agent_done, "Agent worker never completed inside the Textual loop"
    assert app.blocks_mounted > 0, "No Collapsible blocks were mounted live"
    assert app.tool_call_seen, "Tool call was never observed"
    print("[spike-3] PASS -- Textual event loop and claude-agent-sdk asyncio loop coexisted cleanly")


if __name__ == "__main__":
    asyncio.run(main())

"""doxa.app -- the single-pane Textual shell.

Phase 1 slice 2 scope only (see /README.md's status table): one pane, one
session, one prompt input at the bottom, a scrolling list of turn blocks
above it. Each turn is a foldable Collapsible; tool calls inside a turn
render as compact chips (name + one-line arg summary + duration + a check
or cross) that lazily expand into full args/result on first click -- the
expensive JSON pretty-printing only happens once, on demand, not for every
tool call that streams past.

Asyncio/Textual coexistence follows PHASE0_FINDINGS.md §4 exactly:
``run_worker`` schedules the SDK-driving coroutine on Textual's own running
event loop (default ``thread=False``), same as ``spike/03_textual_marriage.py``
proved out.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from textual import on
from textual.app import App, ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Collapsible, Input, LoadingIndicator, Static

from .engine import EngineEvent, SessionEngine
from .peers import PeerSendError, age_secs


def _one_line(text: str, limit: int = 70) -> str:
    return " ".join(text.split())[:limit]


def _fmt_age(secs: float) -> str:
    if secs < 60:
        return f"{int(secs)}s"
    if secs < 3600:
        return f"{int(secs // 60)}m"
    return f"{int(secs // 3600)}h{int((secs % 3600) // 60)}m"


class SystemBlock(Static):
    """One block of doxa-generated (not model-generated) output -- slash
    command results, peer-layer errors. Same ▎ accent as turns, secondary
    color via .system-block in the theme."""

    def __init__(self, text: str) -> None:
        self.text = text
        super().__init__(f"▎ doxa\n{text}", classes="system-block")


class PeerMessageBlock(Static):
    """One incoming peer message. Visually distinct from turns (dim border,
    peer title in the header -- see PeerMessageBlock rules in theme.tcss);
    the body was scrubbed on receive in peers.PeerHost, the one choke point
    for peer input."""

    def __init__(self, frame: dict) -> None:
        self.frame = frame
        title = frame.get("from_title", "?")
        short_id = str(frame.get("from_id", ""))[:8]
        sent_at = frame.get("sent_at", "")
        body = str(frame.get("body", ""))
        super().__init__(
            f"✉ peer · {title} ({short_id}) · {sent_at}\n{body}",
            classes="peer-block",
        )


class ToolChip(Collapsible):
    """One tool call, collapsed by default. Body content (full args + full
    result) is formatted lazily -- only on first expand, per the "lazy-
    formatted" requirement -- so a turn with a dozen tool calls doesn't pay
    for a dozen JSON pretty-prints it may never look at."""

    def __init__(self, call_id: str, name: str, input_data: dict) -> None:
        self.call_id = call_id
        self.tool_name = name
        self.tool_input = input_data
        self.tool_result: str | None = None
        self.is_error = False
        self.duration_ms: int | None = None
        self._formatted = False
        self._body = Static("", id=f"chip-body-{call_id}", classes="chip-body")
        super().__init__(self._body, title=self._chip_title(), collapsed=True)

    def _chip_title(self) -> str:
        arg_summary = _one_line(json.dumps(self.tool_input, ensure_ascii=False), 60)
        if self.tool_result is None:
            status, dur = "…", "…"
        else:
            status = "✗" if self.is_error else "✓"
            dur = f"{self.duration_ms}ms" if self.duration_ms is not None else "?"
        return f"⚒ {self.tool_name}({arg_summary})  ·  {dur}  {status}"

    def update_result(self, result_summary: str, is_error: bool, duration_ms: int | None) -> None:
        self.tool_result = result_summary
        self.is_error = is_error
        self.duration_ms = duration_ms
        self.title = self._chip_title()
        if not self.collapsed:
            self.format_body()

    def format_body(self) -> None:
        if self._formatted:
            return
        self._formatted = True
        text = "ARGS:\n" + json.dumps(self.tool_input, indent=2, ensure_ascii=False)
        if self.tool_result is not None:
            text += "\n\nRESULT:\n" + self.tool_result
        self._body.update(text)


class TurnBlock(Collapsible):
    """One user turn + the assistant's response, foldable. Streaming text
    updates the body live; tool chips mount into `self.tools` as tool_call
    events arrive."""

    def __init__(self, prompt: str) -> None:
        self.prompt_text = prompt
        self.assistant_text = ""
        self.thinking = LoadingIndicator(id="thinking", classes="thinking")
        self.body = Static("", id="turn-body", classes="turn-body")
        self.tools = Vertical(id="turn-tools", classes="turn-tools")
        super().__init__(self.thinking, self.body, self.tools, title=self._render_title(), collapsed=False)

    def _render_title(self, suffix: str = "") -> str:
        return f"▎ {_one_line(self.prompt_text)}{suffix}"

    def hide_thinking(self) -> None:
        if self.thinking.display:
            self.thinking.display = False

    def append_text(self, chunk: str) -> None:
        self.hide_thinking()
        self.assistant_text += chunk
        self.body.update(self.assistant_text)

    def mark_done(self, cost_usd: float | None, duration_ms: int | None, is_error: bool) -> None:
        self.hide_thinking()
        bits = []
        if duration_ms is not None:
            bits.append(f"{duration_ms}ms")
        if cost_usd is not None:
            bits.append(f"${cost_usd:.4f}")
        if is_error:
            bits.append("✗ error")
        suffix = f"  ·  {'  ·  '.join(bits)}" if bits else ""
        self.title = self._render_title(suffix)


class DoxaApp(App):
    """The DOXA terminal -- single pane, Phase 1 slice 2."""

    CSS_PATH = "theme.tcss"
    TITLE = "DOXA"

    def __init__(self, cwd: str | None = None, model: str | None = None) -> None:
        super().__init__()
        self.cwd = cwd or os.getcwd()
        self.model = model
        self.engine: SessionEngine | None = None
        self._engine_ready = asyncio.Event()

    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="block-list")
        yield Static("doxa · connecting…", id="status-bar")
        yield Input(placeholder="Ask DOXA…", id="prompt-input")

    async def on_mount(self) -> None:
        self.engine = SessionEngine(cwd=self.cwd, model=self.model)
        self.query_one("#prompt-input", Input).focus()
        self.run_worker(self._boot(), exclusive=True, group="engine")
        self.run_worker(self._peer_pump(), group="peers")

    async def _boot(self) -> None:
        assert self.engine is not None
        await self.engine.start()
        self._engine_ready.set()
        self._refresh_status()

    async def _peer_pump(self) -> None:
        """Consume the engine's out-of-band stream for the life of the app:
        peer_message mounts a block immediately (display path only -- the
        model sees it on the next user turn, engine-side); joins/leaves just
        move the status-bar chip; tool_disabled (the gate's two-strikes
        containment) mounts a system block and adds the status-bar
        `⊘ toolname` note."""
        await self._engine_ready.wait()
        assert self.engine is not None
        async for ev in self.engine.peer_events():
            if ev.type == "peer_message":
                block_list = self.query_one("#block-list", VerticalScroll)
                await block_list.mount(PeerMessageBlock(ev.data))
                block_list.scroll_end(animate=False)
            elif ev.type == "tool_disabled":
                block_list = self.query_one("#block-list", VerticalScroll)
                await block_list.mount(SystemBlock(
                    f"⊘ tool disabled for this session: {ev.data.get('name')}"
                    f" — {ev.data.get('reason')}"
                ))
                block_list.scroll_end(animate=False)
            self._refresh_status()

    def _refresh_status(self) -> None:
        if self.engine is None:
            return
        model = self.engine.model or "default"
        cost = f"${self.engine.total_cost_usd:.4f}"
        ctx = (
            f"{self.engine.last_ctx_percentage:.0f}%"
            if self.engine.last_ctx_percentage is not None
            else "—"
        )
        beliefs = self.engine.belief_count()
        parts = [model, cost, f"ctx {ctx}", f"{beliefs} beliefs"]
        peer_count = self.engine.peer_count()
        if peer_count:  # hidden at 0 -- a solo session has no peers chip
            parts.append(f"peers {peer_count}")
        disabled = self.engine.disabled_tools()
        if disabled:  # two-strikes containment note -- hidden when empty
            parts.append(" ".join(f"⊘ {name}" for name in disabled))
        bar = self.query_one("#status-bar", Static)
        bar.update("  ·  ".join(parts))

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        prompt = event.value.strip()
        if not prompt:
            return
        event.input.value = ""
        # Only doxa's own two peer commands are intercepted; anything else
        # starting with "/" (e.g. the literal "/compact" convention) still
        # goes to the model untouched.
        if prompt == "/peers" or prompt.startswith(("/peers ", "/msg ")) or prompt == "/msg":
            self.run_worker(self._run_command(prompt), group="command")
            return
        self.run_worker(self._run_turn(prompt), exclusive=True, group="turn")

    async def _run_command(self, prompt: str) -> None:
        await self._engine_ready.wait()
        assert self.engine is not None
        if prompt.split()[0] == "/peers":
            peers = self.engine.list_peers()
            if not peers:
                text = "peers: none in this project right now"
            else:
                lines = [
                    f"{p.title}  {p.session_id[:8]}  {p.cwd}  ·  up {_fmt_age(age_secs(p.started_at))}"
                    for p in peers
                ]
                text = "peers:\n" + "\n".join(lines)
        else:  # /msg
            parts = prompt.split(maxsplit=2)
            if len(parts) < 3:
                text = "usage: /msg <session_prefix> <text>"
            else:
                try:
                    peer = await self.engine.send_peer_message(parts[1], parts[2])
                    text = f"sent to {peer.title} ({peer.session_id[:8]})"
                except PeerSendError as exc:
                    text = f"msg error: {exc}"
        block_list = self.query_one("#block-list", VerticalScroll)
        await block_list.mount(SystemBlock(text))
        block_list.scroll_end(animate=False)

    async def _run_turn(self, prompt: str) -> None:
        assert self.engine is not None
        await self._engine_ready.wait()
        block = TurnBlock(prompt)
        block_list = self.query_one("#block-list", VerticalScroll)
        await block_list.mount(block)
        block_list.scroll_end(animate=False)

        chips: dict[str, ToolChip] = {}

        async for ev in self.engine.send(prompt):
            await self._handle_event(ev, block, chips)
            block_list.scroll_end(animate=False)

        self._refresh_status()

    async def _handle_event(self, ev: EngineEvent, block: TurnBlock, chips: dict[str, ToolChip]) -> None:
        if ev.type == "turn_started":
            return
        if ev.type == "text_delta":
            block.append_text(ev.data["text"])
        elif ev.type == "tool_call":
            block.hide_thinking()
            chip = ToolChip(ev.data["id"], ev.data["name"], ev.data["input"])
            chips[ev.data["id"]] = chip
            await block.tools.mount(chip)
        elif ev.type == "tool_result":
            chip = chips.get(ev.data["id"])
            if chip is not None:
                chip.update_result(ev.data["result_summary"], ev.data["is_error"], ev.data["duration_ms"])
        elif ev.type == "turn_done":
            block.mark_done(ev.data.get("cost_usd"), ev.data.get("duration_ms"), ev.data.get("is_error", False))
            self._refresh_status()

    @on(Collapsible.Expanded)
    def _on_chip_expanded(self, event: Collapsible.Expanded) -> None:
        if isinstance(event.collapsible, ToolChip):
            event.collapsible.format_body()

    async def action_quit(self) -> None:
        """Host-driven finalization (PHASE0 redesign item 1: no SessionEnd
        hook exists, so the app's own teardown path is where the session-end
        review + index deterministically runs -- see SessionEngine.finalize)."""
        if self.engine is not None:
            await self.engine.finalize()
        await super().action_quit()


def main() -> None:
    DoxaApp().run()


if __name__ == "__main__":
    main()

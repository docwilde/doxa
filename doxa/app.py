"""doxa.app -- the Textual shell: N session tabs over N engine handles.

Phase 1 built this as one pane over an in-process SessionEngine; Phase 2's
daemon split swapped what sits behind the engine handle (an in-process
``SessionEngine`` or a ``doxa.client.EngineClient`` attached to a session
daemon -- the app consumes the same async-iterator surface either way).
Phase 3's tab step is exactly the README sketch: the single-session surface
became :class:`SessionPane` (a pure extraction -- block list, status bar,
prompt input, boot/pump workers, out-of-band rendering), and a
``TabbedContent`` hosts N of them, one engine handle EACH. Tabs are N
clients in one TUI, not N engines in one process: Ctrl+T spawns a fresh
daemon in the same repo scope (``new_session_factory``) and attaches it in
a new tab; Ctrl+W close-detaches just that tab's client. Worker groups are
scoped per pane node (Textual cancels by (node, group)), so an exclusive
pump dies with its tab, not with its neighbor. The peer layer needed zero
changes -- each daemon registers its own presence, so two tabs of the same
repo correctly see each other as peers.

Ctrl+C stays APP-level, deliberately: one press arms the double-press
window and then detaches ALL tabs (every daemon keeps running -- the
cheapest outcome to recover from is always chosen on a reflex
keystroke); a second press inside the window stops EVERY tab's session
(finalize NOW). Per-tab stopping remains available where deliberation
lives: the palette's quit-stop and Ctrl+W.

Each turn is a foldable Collapsible; tool calls inside a turn render as
compact chips (name + one-line arg summary + duration + a check or cross)
that lazily expand into full args/result on first click -- the expensive
JSON pretty-printing only happens once, on demand, not for every tool call
that streams past.

Asyncio/Textual coexistence follows PHASE0_FINDINGS.md §4 exactly:
``run_worker`` schedules the SDK-driving coroutine on Textual's own running
event loop (default ``thread=False``), same as ``spike/03_textual_marriage.py``
proved out.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path
from typing import Any, Callable

from functools import partial

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.widgets import (
    Collapsible,
    Input,
    LoadingIndicator,
    Static,
    TabbedContent,
    TabPane,
)

from . import images as images_mod
from . import peers as peers_mod
from .engine import EngineEvent, SessionEngine
from .palette import DoxaCommandProvider
from .peers import PeerSendError, age_secs


# Ctrl+C quit semantics: the first press arms this window and then detaches;
# a second press inside it upgrades to quit-stop (finalize NOW).
CTRL_C_DOUBLE_SECS = 2.0


def _one_line(text: str, limit: int = 70) -> str:
    return " ".join(text.split())[:limit]


def _fmt_age(secs: float) -> str:
    if secs < 60:
        return f"{int(secs)}s"
    if secs < 3600:
        return f"{int(secs // 60)}m"
    return f"{int(secs // 3600)}h{int((secs % 3600) // 60)}m"


def tier_short(subscription_type: "str | None") -> "str | None":
    """Compact status-line form of the CLI-reported subscription tier:
    'Claude Max' -> 'max', 'Claude Pro' -> 'pro'; any other non-empty tier
    lowercased as-is. None/empty (API-key auth) stays None -- the caller
    then shows the plain $ figure."""
    if not subscription_type or not str(subscription_type).strip():
        return None
    tier = str(subscription_type).strip().lower()
    return tier.removeprefix("claude").strip() or tier


def git_branch_symbol() -> str:
    """The nerd-font branch glyph (U+E0A0) when the user opted in via
    DOXA_NERD_FONT (a TUI cannot detect font glyph coverage itself);
    the universally-rendering ⎇ otherwise."""
    return "" if os.environ.get("DOXA_NERD_FONT", "").strip() else "⎇"


class GitLine:
    """The `repo ⎇ branch` chip for the status line.

    Cost discipline (this sits next to the idle-CPU fix for a reason): the
    repo root is resolved ONCE at construction (the only subprocess); after
    that a branch read is one stat + at most one small file read of
    .git/HEAD, re-parsed only when HEAD's mtime moves (checkout/switch touch
    it). render() is called from event-driven sites only (_refresh_status:
    boot, turn done, peer events) -- NEVER from a timer or per-frame hook,
    which would recreate the busy-idle bug this app just shed."""

    def __init__(self, cwd: str) -> None:
        self.repo_root = peers_mod.repo_root_of(cwd)
        self.repo = Path(self.repo_root).name if self.repo_root else None
        self._head: Path | None = None
        self._mtime: float | None = None
        self._branch: str | None = None
        if self.repo_root:
            git = Path(self.repo_root) / ".git"
            if git.is_file():
                # Worktree/submodule: .git is a one-line pointer file.
                try:
                    for line in git.read_text(
                        encoding="utf-8", errors="replace"
                    ).splitlines():
                        if line.startswith("gitdir:"):
                            gitdir = Path(line.split(":", 1)[1].strip())
                            if not gitdir.is_absolute():
                                gitdir = (Path(self.repo_root) / gitdir).resolve()
                            git = gitdir
                            break
                except OSError:
                    return
            self._head = git / "HEAD"

    def render(self) -> str | None:
        """` repo ⎇ branch`, or None outside a repo (no chip at all)."""
        if not self.repo:
            return None
        branch = self._read_branch()
        if not branch:
            return self.repo
        return f"{self.repo} {git_branch_symbol()} {branch}"

    def _read_branch(self) -> str | None:
        if self._head is None:
            return None
        try:
            mtime = self._head.stat().st_mtime
        except OSError:
            return self._branch  # HEAD briefly gone (rebase): keep last known
        if mtime == self._mtime:
            return self._branch
        self._mtime = mtime
        try:
            head = self._head.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return self._branch
        if head.startswith("ref:"):
            ref = head.split(":", 1)[1].strip()
            self._branch = ref.removeprefix("refs/heads/")
        else:
            self._branch = head[:8] or None  # detached HEAD: short sha
        return self._branch


class SystemBlock(Static):
    """One block of doxa-generated (not model-generated) output -- slash
    command results, peer-layer errors. Same ▎ accent as turns, secondary
    color via .system-block in the theme."""

    def __init__(self, text: str) -> None:
        self.text = text
        super().__init__(f"▎ doxa\n{text}", classes="system-block")


class ImageBlock(Vertical):
    """The `/img <path>` debug block: a caption line plus whatever
    doxa.images.widget_for yields for this terminal -- a real image widget
    on a KGP/sixel/half-block tier, the "[image: ...]" Static otherwise.
    Exists so image support can be eyeballed without needing a tool call
    that happens to return a picture."""

    def __init__(self, path: str) -> None:
        self.path = path
        super().__init__(classes="image-block")

    def compose(self) -> ComposeResult:
        yield Static(f"▎ img · {self.path}", classes="image-caption")
        yield images_mod.widget_for(self.path, self.path)


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
    for a dozen JSON pretty-prints it may never look at. A tool result that
    carries the engine's ``image_path`` convention gets an image widget (or
    its guaranteed text fallback -- doxa/images.py) mounted into the media
    area, equally lazily: on first expand only.

    Trace tree: a Task-spawned subagent's own activity arrives tagged with
    ``parent_id`` = this chip's call id (the engine's subagent trace
    convention), and nests HERE -- child tool calls mount as further
    ToolChips into ``self.subcalls`` (foldable all the way down, each level
    as lazily formatted as the top), and the subagent's streamed text
    accumulates in a buffer that only renders on expand. Everything shown
    was scrubbed engine-side before it ever reached an event."""

    def __init__(self, call_id: str, name: str, input_data: dict) -> None:
        self.call_id = call_id
        self.tool_name = name
        self.tool_input = input_data
        self.tool_result: str | None = None
        self.tool_image_path: str | None = None
        self.is_error = False
        self.duration_ms: int | None = None
        self._formatted = False
        self._image_mounted = False
        self._sub_text = ""  # subagent streamed text, rendered lazily
        self._body = Static("", id=f"chip-body-{call_id}", classes="chip-body")
        self._subout = Static("", id=f"chip-subout-{call_id}", classes="chip-subout")
        self.subcalls = Vertical(
            id=f"chip-subcalls-{call_id}", classes="chip-subcalls"
        )
        self._media = Vertical(id=f"chip-media-{call_id}", classes="chip-media")
        super().__init__(
            self._body, self._subout, self.subcalls, self._media,
            title=self._chip_title(), collapsed=True,
        )

    def _chip_title(self) -> str:
        arg_summary = _one_line(json.dumps(self.tool_input, ensure_ascii=False), 60)
        if self.tool_result is None:
            status, dur = "…", "…"
        else:
            status = "✗" if self.is_error else "✓"
            dur = f"{self.duration_ms}ms" if self.duration_ms is not None else "?"
        return f"⚒ {self.tool_name}({arg_summary})  ·  {dur}  {status}"

    def update_result(
        self,
        result_summary: str,
        is_error: bool,
        duration_ms: int | None,
        image_path: str | None = None,
    ) -> None:
        self.tool_result = result_summary
        self.is_error = is_error
        self.duration_ms = duration_ms
        self.tool_image_path = image_path
        self.title = self._chip_title()
        if not self.collapsed:
            self.format_body()

    def append_subagent_text(self, chunk: str) -> None:
        """Streamed text from the subagent behind this Task call. Buffered
        always; RENDERED only while expanded (or on the next expand) --
        the same lazy discipline as the body, so a subagent that narrates
        for pages costs nothing until someone looks."""
        self._sub_text += chunk
        if not self.collapsed:
            self._render_subout()

    def _render_subout(self) -> None:
        if self._sub_text:
            self._subout.update("SUBAGENT:\n" + self._sub_text)

    def format_body(self) -> None:
        if not self._formatted:
            self._formatted = True
            text = "ARGS:\n" + json.dumps(self.tool_input, indent=2, ensure_ascii=False)
            if self.tool_result is not None:
                text += "\n\nRESULT:\n" + self.tool_result
            self._body.update(text)
        self._render_subout()
        if self.tool_image_path and not self._image_mounted:
            self._image_mounted = True
            # widget_for NEVER raises and never returns None: an unsupported
            # terminal or a bad file yields the "[image: ...]" Static.
            self._media.mount(
                images_mod.widget_for(self.tool_image_path, self.tool_image_path)
            )


class TurnBlock(Collapsible):
    """One user turn + the assistant's response, foldable. Streaming text
    updates the body live; tool chips mount into `self.tools` as tool_call
    events arrive."""

    def __init__(self, prompt: str) -> None:
        self.prompt_text = prompt
        self.assistant_text = ""
        self.thinking = LoadingIndicator(classes="thinking")
        self.body = Static("", classes="turn-body")
        self.tools = Vertical(classes="turn-tools")
        super().__init__(self.thinking, self.body, self.tools, title=self._render_title(), collapsed=False)

    def _render_title(self, suffix: str = "") -> str:
        return f"▎ {_one_line(self.prompt_text)}{suffix}"

    def hide_thinking(self) -> None:
        if self.thinking.display:
            self.thinking.display = False
            # display=False only HIDES the indicator: LoadingIndicator arms a
            # 16Hz auto-refresh animation timer on mount and nothing else ever
            # stops it, so every finished turn would leave one live timer
            # behind and idle CPU grew linearly with scrollback (measured
            # ~0.2%/turn headless; the fix takes 40 idle turn blocks from
            # ~8.7% CPU to baseline). The animation dies with the display.
            self.thinking.auto_refresh = None

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


class SessionPane(TabPane):
    """One session's whole surface: engine handle, block list, status bar,
    prompt input, and the boot/pump workers that drive them.

    This is the README sketch's extraction step: exactly the widget subtree
    (and exactly the per-session state) the single-pane app owned before,
    now owned per tab. Every worker this pane starts runs on the PANE node
    (``self.run_worker``), so exclusivity groups are scoped per tab and a
    removed tab takes its workers down with it (Textual cancels a node's
    workers on removal)."""

    def __init__(
        self,
        title: str,
        cwd: str,
        model: str | None,
        engine_factory: "Callable[[], Any]",
        *,
        id: str | None = None,
    ) -> None:
        super().__init__(title, id=id)
        self.cwd = cwd
        self.model = model
        self.engine: Any | None = None
        self._engine_factory = engine_factory
        self._engine_ready = asyncio.Event()
        # Out-of-band turn rendering state (replayed history after reattach,
        # or a turn another attached client drives) -- see _peer_pump.
        self._oob_turn: TurnBlock | None = None
        self._oob_chips: dict[str, ToolChip] = {}
        # Status-line git chip -- built in _boot (per engine, since attach
        # can land in another project's cwd), refreshed event-driven only.
        self._git: GitLine | None = None

    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="block-list")
        yield Static("doxa · connecting…", id="status-bar")
        yield Input(placeholder="Ask DOXA…", id="prompt-input")

    async def on_mount(self) -> None:
        self.engine = self._engine_factory()
        self.query_one("#prompt-input", Input).focus()
        self.run_worker(self._boot(), exclusive=True, group="engine")
        self.run_worker(self._peer_pump(), exclusive=True, group="peers")

    # -- lifecycle ---------------------------------------------------

    async def detach(self) -> None:
        """Close-detach this pane's engine handle: over a daemon client
        finalize() only detaches (the daemon lingers); in-process it
        finalizes for real. Never raises -- teardown paths call this."""
        engine, self.engine = self.engine, None
        if engine is not None:
            with contextlib.suppress(Exception):
                await engine.finalize()

    async def stop(self) -> None:
        """Finalize this pane's session NOW (daemon included)."""
        engine, self.engine = self.engine, None
        if engine is not None:
            stop = getattr(engine, "stop", None)
            with contextlib.suppress(Exception):
                if stop is not None:
                    await stop()
                else:
                    await engine.finalize()

    async def _boot(self) -> None:
        assert self.engine is not None
        await self.engine.start()
        # Engine cwd wins over the pane's own (attach may cross projects);
        # GitLine's constructor runs one git subprocess -- off the loop.
        git_cwd = str(getattr(self.engine, "cwd", None) or self.cwd)
        self._git = await asyncio.to_thread(GitLine, git_cwd)
        self._engine_ready.set()
        self._refresh_status()
        # Initial identity block: who/where this session actually is --
        # only fields the CLI/config really reported, never guesses.
        block_list = self.query_one("#block-list", VerticalScroll)
        identity = SystemBlock(self._identity_text(git_cwd))
        identity.id = "identity-block"
        await block_list.mount(identity)
        block_list.scroll_end(animate=False)

    def _identity_text(self, cwd: str) -> str:
        """The session-start identity summary. Every line renders a REAL
        field (the SDK's connect-time account block, the engine handle, the
        git chip, LORE's store) -- absent fields are omitted, not invented."""
        engine = self.engine
        account = getattr(engine, "account", None) or {}
        lines: list[str] = []
        who = " · ".join(
            str(account[k]) for k in ("email", "organization") if account.get(k)
        )
        if who:
            lines.append(f"account  {who}")
        if account.get("subscriptionType"):
            plan = str(account["subscriptionType"])
            if account.get("apiProvider"):
                plan += f" ({account['apiProvider']})"
            lines.append(f"plan     {plan}")
        lines.append(f"model    {getattr(engine, 'model', None) or 'default'}")
        lines.append(f"cwd      {cwd}")
        git_chip = self._git.render() if self._git is not None else None
        if git_chip:
            lines.append(f"repo     {git_chip}")
        lore_bits = []
        if getattr(engine, "lore_root", None):
            lore_bits.append(str(engine.lore_root))
        if engine is not None:
            lore_bits.append(f"{engine.belief_count()} beliefs")
        if lore_bits:
            lines.append(f"lore     {' · '.join(lore_bits)}")
        return "\n".join(lines)

    async def _peer_pump(self) -> None:
        """Consume the engine's out-of-band stream for the life of the pane:
        peer_message mounts a block immediately (display path only -- the
        model sees it on the next user turn, engine-side); joins/leaves just
        move the status-bar chip; tool_disabled (the gate's two-strikes
        containment) mounts a system block and adds the status-bar
        `⊘ toolname` note. Since the daemon split, TURN events can arrive
        here too -- replayed history right after a reattach, or a turn that
        another attached client of the same daemon is driving -- and render
        into the same TurnBlock/ToolChip widgets a local turn uses."""
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
            elif ev.type == "derive_done":
                # Streaming deriver (engine-side, DOXA_DERIVE_SECS): newly
                # staged proposals await the SAME human review gate as ever
                # -- this is a notification, never an auto-apply.
                staged = int(ev.data.get("staged") or 0)
                if staged > 0:
                    block_list = self.query_one("#block-list", VerticalScroll)
                    noun = "proposal" if staged == 1 else "proposals"
                    await block_list.mount(SystemBlock(
                        f"{staged} {noun} staged — /lore:pending"
                    ))
                    block_list.scroll_end(animate=False)
            elif ev.type == "turn_started":
                block_list = self.query_one("#block-list", VerticalScroll)
                self._oob_turn = TurnBlock(str(ev.data.get("prompt") or ""))
                self._oob_chips = {}
                await block_list.mount(self._oob_turn)
                block_list.scroll_end(animate=False)
            elif ev.type in ("text_delta", "tool_call", "tool_result", "turn_done"):
                if self._oob_turn is not None:
                    await self._handle_event(ev, self._oob_turn, self._oob_chips)
                    self.query_one("#block-list", VerticalScroll).scroll_end(
                        animate=False
                    )
                    if ev.type == "turn_done":
                        self._oob_turn = None
                        self._oob_chips = {}
            self._refresh_status()

    def _refresh_status(self) -> None:
        if self.engine is None:
            return
        model = self.engine.model or "default"
        # Subscription-aware cost: on subscription auth the session costs
        # no dollars, so a bare $ figure is misleading -- show the tier,
        # with the (already-computed) list-price figure demoted to an
        # explicit what-if. API-key auth keeps the real $ estimate.
        account = getattr(self.engine, "account", None) or {}
        tier = tier_short(account.get("subscriptionType"))
        if tier:
            cost = f"sub:{tier} (≈${self.engine.total_cost_usd:.4f} if API)"
        else:
            cost = f"${self.engine.total_cost_usd:.4f}"
        ctx = (
            f"{self.engine.last_ctx_percentage:.0f}%"
            if self.engine.last_ctx_percentage is not None
            else "—"
        )
        beliefs = self.engine.belief_count()
        parts = [model]
        git_chip = self._git.render() if self._git is not None else None
        if git_chip:  # hidden entirely outside a repo
            parts.append(git_chip)
        parts += [cost, f"ctx {ctx}", f"{beliefs} beliefs"]
        if getattr(self.engine, "detachable", False):
            sid = str(getattr(self.engine, "session_id", "") or "")
            if sid:  # attached to a daemon: show the reattach handle
                parts.append(f"⌁ {sid[:8]}")
        peer_count = self.engine.peer_count()
        if peer_count:  # hidden at 0 -- a solo session has no peers chip
            parts.append(f"peers {peer_count}")
        disabled = self.engine.disabled_tools()
        if disabled:  # two-strikes containment note -- hidden when empty
            parts.append(" ".join(f"⊘ {name}" for name in disabled))
        bar = self.query_one("#status-bar", Static)
        bar.update("  ·  ".join(parts))

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "prompt-input":
            return  # a modal overlay's input is never a prompt
        event.stop()  # this pane's prompt is nobody else's business
        prompt = event.value.strip()
        if not prompt:
            return
        event.input.value = ""
        # Only doxa's own commands (peers, msg, img) are intercepted;
        # anything else starting with "/" (e.g. the literal "/compact"
        # convention) still goes to the model untouched.
        if (
            prompt in ("/peers", "/msg", "/img")
            or prompt.startswith(("/peers ", "/msg ", "/img "))
        ):
            self.run_worker(self._run_command(prompt), group="command")
            return
        self.run_worker(self._run_turn(prompt), exclusive=True, group="turn")

    async def _run_command(self, prompt: str) -> None:
        await self._engine_ready.wait()
        assert self.engine is not None
        if prompt.split()[0] == "/img":
            # Debug render site for image support -- see ImageBlock.
            parts = prompt.split(maxsplit=1)
            path = os.path.expanduser(parts[1].strip()) if len(parts) > 1 else ""
            block_list = self.query_one("#block-list", VerticalScroll)
            if not path:
                await block_list.mount(SystemBlock("usage: /img <path>"))
            elif not os.path.isfile(path):
                await block_list.mount(SystemBlock(f"img: no such file: {path}"))
            else:
                await block_list.mount(ImageBlock(path))
            block_list.scroll_end(animate=False)
            return
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

        try:
            async for ev in self.engine.send(prompt):
                await self._handle_event(ev, block, chips)
                block_list.scroll_end(animate=False)
        except Exception as exc:  # noqa: BLE001 -- a refused/broken turn must
            # not take the shell down (e.g. the daemon is busy with another
            # client's turn, or the connection dropped mid-stream).
            block.mark_done(None, None, True)
            await block_list.mount(SystemBlock(f"turn failed: {exc}"))
            block_list.scroll_end(animate=False)

        self._refresh_status()

    async def _handle_event(self, ev: EngineEvent, block: TurnBlock, chips: dict[str, ToolChip]) -> None:
        if ev.type == "turn_started":
            return
        if ev.type == "text_delta":
            parent = chips.get(ev.data.get("parent_id") or "")
            if parent is not None:
                # A subagent narrating: trace material, nested under its
                # Task chip -- never mixed into the turn's own prose.
                parent.append_subagent_text(ev.data["text"])
            else:
                block.append_text(ev.data["text"])
        elif ev.type == "tool_call":
            block.hide_thinking()
            chip = ToolChip(ev.data["id"], ev.data["name"], ev.data["input"])
            chips[ev.data["id"]] = chip
            parent = chips.get(ev.data.get("parent_id") or "")
            if parent is not None:
                # Trace tree: a subagent's call nests under the Task chip
                # that spawned it, foldable at every level. An unknown
                # parent (ring truncation on replay) degrades to top level
                # -- the call is never dropped.
                await parent.subcalls.mount(chip)
            else:
                await block.tools.mount(chip)
        elif ev.type == "tool_result":
            chip = chips.get(ev.data["id"])
            if chip is not None:
                chip.update_result(
                    ev.data["result_summary"], ev.data["is_error"],
                    ev.data["duration_ms"], image_path=ev.data.get("image_path"),
                )
        elif ev.type == "turn_done":
            block.mark_done(ev.data.get("cost_usd"), ev.data.get("duration_ms"), ev.data.get("is_error", False))
            self._refresh_status()

    async def switch_engine(self, make_engine: "Callable[[], Any]") -> None:
        """Swap this pane's live engine handle: detach/finalize the old one,
        build the new one (off-loop -- a daemon spawn blocks on
        subprocess+registry polling), reset the block list, and restart the
        boot + pump workers (both exclusive in their pane-scoped groups, so
        the old pump dies with its engine)."""
        old, self.engine = self.engine, None
        self._engine_ready = asyncio.Event()
        self._oob_turn = None
        self._oob_chips = {}
        if old is not None:
            with contextlib.suppress(Exception):
                await old.finalize()
        try:
            self.engine = await asyncio.to_thread(make_engine)
        except Exception as exc:  # noqa: BLE001 -- spawn/attach failure must surface, not crash
            block_list = self.query_one("#block-list", VerticalScroll)
            await block_list.mount(SystemBlock(f"session switch failed: {exc}"))
            return
        await self.query_one("#block-list", VerticalScroll).remove_children()
        self.query_one("#status-bar", Static).update("doxa · connecting…")
        self.run_worker(self._boot(), exclusive=True, group="engine")
        self.run_worker(self._peer_pump(), exclusive=True, group="peers")


class DoxaApp(App):
    """The DOXA terminal."""

    CSS_PATH = "theme.tcss"
    TITLE = "DOXA"
    # Ctrl+P (App.COMMAND_PALETTE_BINDING's default) opens the built-in
    # CommandPalette; DoxaCommandProvider feeds it doxa_commands() below.
    COMMANDS = App.COMMANDS | {DoxaCommandProvider}
    # Ctrl+R: history search over LORE's session FTS (doxa/history.py) --
    # instant BM25 over every indexed session, not a scrollback scan.
    # Ctrl+T/Ctrl+W: tab lifecycle (new same-repo session / close-detach).
    # Ctrl+C: quit. Textual 5 binds ctrl+c to a "press ctrl+q to quit"
    # notification app-side and to "copy" on a focused Input -- with the
    # prompt input permanently focused, Ctrl+C therefore did NOTHING
    # quit-shaped (the dogfooding bug). priority=True beats both: one press
    # = quit-detach ALL tabs (daemons keep running), double press within
    # CTRL_C_DOUBLE_SECS = quit-stop ALL -- see action_ctrl_c_quit.
    BINDINGS = [
        ("ctrl+r", "history_search", "History"),
        Binding("ctrl+t", "new_tab", "New tab", show=False, priority=True),
        Binding("ctrl+w", "close_tab", "Close tab", show=False, priority=True),
        Binding("ctrl+c", "ctrl_c_quit", "Quit (detach)", show=False, priority=True),
    ]

    def __init__(
        self,
        cwd: str | None = None,
        model: str | None = None,
        engine_factory: "Callable[[], Any] | None" = None,
        new_session_factory: "Callable[[], Any] | None" = None,
    ) -> None:
        super().__init__()
        self.cwd = cwd or os.getcwd()
        self.model = model
        # The daemon-split seam: engine_factory builds whatever the first
        # tab drives (in-process SessionEngine by default; an EngineClient
        # when doxa.cli attached us to a daemon). new_session_factory builds
        # a FRESH session -- the palette's "new session", and every Ctrl+T
        # tab -- distinct because an attach-flavored engine_factory must not
        # be re-invoked to mean "new".
        self._engine_factory = engine_factory or (
            lambda: SessionEngine(cwd=self.cwd, model=self.model)
        )
        self._new_session_factory = new_session_factory or self._engine_factory
        self._tab_serial = 0
        # Ctrl+C double-press window (see action_ctrl_c_quit): the armed
        # timer that will quit-detach when it fires; a second Ctrl+C while
        # it is armed cancels it and quit-stops instead.
        self._ctrl_c_timer: Any = None
        # Settle the image-mode probe NOW, while this process still owns the
        # terminal: textual-image's TGP/sixel queries read their answer from
        # stdin, which Textual's own reader thread will grab the moment
        # App.run() starts (doxa/images.py's detection discipline note).
        images_mod.detect_mode()

    # -- pane plumbing -----------------------------------------------

    def _tab_title(self) -> str:
        self._tab_serial += 1
        name = Path(self.cwd).name or "session"
        return name if self._tab_serial == 1 else f"{name} ·{self._tab_serial}"

    def _make_pane(self, engine_factory: "Callable[[], Any]") -> SessionPane:
        return SessionPane(
            self._tab_title(), self.cwd, self.model, engine_factory,
        )

    @property
    def active_pane(self) -> SessionPane | None:
        try:
            pane = self.query_one("#session-tabs", TabbedContent).active_pane
        except Exception:
            return None
        return pane if isinstance(pane, SessionPane) else None

    def panes(self) -> list[SessionPane]:
        return list(self.query(SessionPane))

    @property
    def engine(self) -> Any | None:
        """The ACTIVE tab's engine handle -- the single-session accessors
        (palette callbacks, history insertion, tests) read the app the way
        they always did; multi-tab awareness lives in panes()."""
        pane = self.active_pane
        return pane.engine if pane is not None else None

    @property
    def _git(self) -> GitLine | None:
        pane = self.active_pane
        return pane._git if pane is not None else None

    def _refresh_status(self) -> None:
        pane = self.active_pane
        if pane is not None:
            pane._refresh_status()

    def compose(self) -> ComposeResult:
        yield Static("", id="belief-inspector")  # hidden stub, palette-toggled
        with TabbedContent(id="session-tabs"):
            yield self._make_pane(self._engine_factory)

    @on(TabbedContent.TabActivated)
    def _on_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        pane = self.active_pane
        if pane is not None:
            with contextlib.suppress(Exception):
                pane.query_one("#prompt-input", Input).focus()

    # -- tab lifecycle -----------------------------------------------

    async def action_new_tab(self) -> None:
        """Ctrl+T: a fresh session in the same repo scope (exactly
        new_session_factory -- a new daemon under the CLI, a new in-process
        engine otherwise), attached in a new tab and focused."""
        tabbed = self.query_one("#session-tabs", TabbedContent)
        pane = self._make_pane(self._new_session_factory)
        await tabbed.add_pane(pane)
        tabbed.active = pane.id or tabbed.active

    async def action_close_tab(self) -> None:
        """Ctrl+W: close-DETACH the active tab -- its daemon keeps running
        (reattach later via the palette's attach picker or `doxa attach`).
        Closing the last tab closes the app, on the same detach semantics."""
        pane = self.active_pane
        if pane is None:
            return
        if len(self.panes()) == 1:
            await self.action_quit()
            return
        await pane.detach()
        await self.query_one("#session-tabs", TabbedContent).remove_pane(
            pane.id or ""
        )

    def _switch_to_tab(self, pane_id: str) -> None:
        with contextlib.suppress(Exception):
            self.query_one("#session-tabs", TabbedContent).active = pane_id

    # -- palette (Ctrl+P) --------------------------------------------

    def doxa_commands(self) -> "list[tuple[str, str, Callable[[], Any]]]":
        """The DOXA command surface, as (name, help, callback) tuples --
        consumed by palette.DoxaCommandProvider on every palette open, so
        the attach picker's and tab picker's entries reflect the live state
        each time."""
        commands: list[tuple[str, str, Any]] = [
            (
                "New tab",
                "Open a fresh DOXA session in this repo scope in a new tab (ctrl+t)",
                self._cmd_new_tab,
            ),
            (
                "Close tab",
                "Close-detach the current tab; its session keeps running (ctrl+w)",
                self._cmd_close_tab,
            ),
            (
                "New session",
                "Start a fresh DOXA session and switch THIS tab to it",
                self._cmd_new_session,
            ),
            (
                "Peers: list",
                "Same-project live sessions (the /peers command)",
                self._cmd_list_peers,
            ),
            (
                "Peers: message",
                "Prefill /msg <session> <text> in the prompt",
                self._cmd_message_peer,
            ),
            (
                "Belief inspector: toggle",
                "Show/hide the belief inspector pane (stub until Phase 3)",
                self.action_toggle_inspector,
            ),
            (
                "History: search past sessions",
                "BM25 search over LORE's session index (ctrl+r)",
                self.action_history_search,
            ),
            (
                "Quit: detach",
                "Close this TUI; every session daemon keeps running "
                "(reattach with `doxa attach`)",
                self.action_quit,
            ),
            (
                "Quit: stop session",
                "Finalize the current tab's session now (LORE review + index) "
                "and close its tab",
                self._cmd_stop_active,
            ),
        ]
        # Tab picker: one entry per OTHER live tab, in tab order.
        active = self.active_pane
        for pane in self.panes():
            if pane is active or not pane.id:
                continue
            sid = str(getattr(pane.engine, "session_id", "") or "")[:8]
            commands.append((
                f"Tab: {pane._title}" + (f" ({sid})" if sid else ""),
                "Switch to this tab",
                partial(self._switch_to_tab, pane.id),
            ))
        # Attach picker: live daemon-hosted sessions from the shared
        # peer/daemon registry, newest first, never any session already
        # open in one of our tabs.
        open_ids = {
            str(getattr(p.engine, "session_id", "") or "") for p in self.panes()
        }
        for entry in peers_mod.list_daemons():
            if entry.session_id in open_ids:
                continue
            commands.append((
                f"Attach: {entry.title} ({entry.session_id[:8]})",
                f"Reattach to the live session in {entry.cwd} (in this tab)",
                partial(self._cmd_attach, entry),
            ))
        return commands

    def _cmd_new_tab(self) -> None:
        self.run_worker(self.action_new_tab(), group="tabs")

    def _cmd_close_tab(self) -> None:
        self.run_worker(self.action_close_tab(), group="tabs")

    def _cmd_new_session(self) -> None:
        pane = self.active_pane
        if pane is not None:
            pane.run_worker(
                pane.switch_engine(self._new_session_factory),
                exclusive=True, group="switch",
            )

    def _cmd_attach(self, entry: peers_mod.PeerInfo) -> None:
        from .client import EngineClient  # deferred: tests without a daemon never import it

        socket_path = entry.daemon_socket
        if not socket_path:
            return
        pane = self.active_pane
        if pane is not None:
            pane.run_worker(
                pane.switch_engine(lambda: EngineClient(socket_path)),
                exclusive=True, group="switch",
            )

    def _cmd_stop_active(self) -> None:
        self.run_worker(self._stop_active(), group="tabs")

    async def _stop_active(self) -> None:
        """Palette 'Quit: stop session', tab-scoped: finalize the ACTIVE
        tab's session NOW; the tab closes with it. Stopping the only tab
        closes the app (the Phase 2 behavior, per-app == per-tab then)."""
        pane = self.active_pane
        if pane is None:
            return
        await pane.stop()
        if len(self.panes()) == 1:
            await App.action_quit(self)
            return
        await self.query_one("#session-tabs", TabbedContent).remove_pane(
            pane.id or ""
        )

    def _cmd_list_peers(self) -> None:
        pane = self.active_pane
        if pane is not None:
            pane.run_worker(pane._run_command("/peers"), group="command")

    def _cmd_message_peer(self) -> None:
        pane = self.active_pane
        if pane is None:
            return
        prompt = pane.query_one("#prompt-input", Input)
        prompt.value = "/msg "
        prompt.cursor_position = len(prompt.value)
        prompt.focus()

    def action_history_search(self) -> None:
        """Ctrl+R: modal FTS search over LORE's session index. A chosen hit
        inserts its text reference (full session id + timestamp + snippet)
        into the ACTIVE tab's prompt input -- material for the next turn,
        never an auto-sent prompt."""
        from .history import HistorySearchScreen, hit_reference

        def _insert(hit: "dict | None") -> None:
            if not hit:
                return
            pane = self.active_pane
            if pane is None:
                return
            prompt = pane.query_one("#prompt-input", Input)
            ref = hit_reference(hit)
            prompt.value = f"{prompt.value.rstrip()} {ref}".strip()
            prompt.cursor_position = len(prompt.value)
            prompt.focus()

        self.push_screen(HistorySearchScreen(self.cwd), callback=_insert)

    def action_toggle_inspector(self) -> None:
        """Belief-inspector stub: Phase 3 owns the real pane (live STEER/
        CITE split, evidence trails); Phase 2 reserves the toggle, the dock
        and the count so the palette command and the muscle memory exist."""
        panel = self.query_one("#belief-inspector", Static)
        if panel.display:
            panel.display = False
            return
        beliefs = self.engine.belief_count() if self.engine is not None else 0
        panel.update(
            "▎ belief inspector — stub\n\n"
            f"{beliefs} active beliefs in the store.\n\n"
            "Phase 3 renders them here: STEER/CITE split,\n"
            "evidence trails, calibration. Until then use\n"
            "the lore_belief_search / lore_belief_show tools."
        )
        panel.display = True

    @on(Collapsible.Expanded)
    def _on_chip_expanded(self, event: Collapsible.Expanded) -> None:
        if isinstance(event.collapsible, ToolChip):
            event.collapsible.format_body()

    # -- quit semantics (app-level, all tabs) ------------------------

    async def action_ctrl_c_quit(self) -> None:
        """Ctrl+C (priority binding), APP-level by design -- a reflex
        keystroke gets the cheapest-to-recover outcome across every tab.
        First press: arm the double-press window, then quit-DETACH when it
        expires -- every daemon-hosted session keeps running (same as
        ctrl+q); in-process engines finalize right there, so Ctrl+C always
        exits cleanly. Second press inside the window: quit-STOP EVERY
        tab's session (finalize NOW, daemons included). Per-tab stop stays
        on the palette's 'Quit: stop session' and per-tab detach on
        Ctrl+W, where the choice is deliberate rather than reflexive."""
        if self._ctrl_c_timer is not None:
            self._ctrl_c_timer.stop()
            self._ctrl_c_timer = None
            await self.action_quit_stop()
            return
        self.notify(
            "detaching all tabs — Ctrl+C again to STOP the sessions (finalize now)",
            severity="warning",
            timeout=CTRL_C_DOUBLE_SECS,
        )
        self._ctrl_c_timer = self.set_timer(CTRL_C_DOUBLE_SECS, self.action_quit)

    async def action_quit_stop(self) -> None:
        """Quit-stop, ALL tabs -- finalize every session NOW. Over a daemon
        client this stops the daemon itself (LORE review + index run
        there); in-process it is plain finalize-and-quit."""
        for pane in self.panes():
            await pane.stop()
        await super().action_quit()

    async def action_quit(self) -> None:
        """ctrl+q / palette 'Quit: detach' -- ALL tabs. Over a daemon
        client, finalize() only DETACHES -- the daemon lingers and runs the
        session-end review + index itself once the last client is gone
        (or on `doxa stop`). In-process (Phase 1 shape), finalize() still
        runs the review + index right here, host-driven (PHASE0 redesign
        item 1: no SessionEnd hook exists)."""
        for pane in self.panes():
            await pane.detach()
        await super().action_quit()


def main() -> None:
    DoxaApp().run()


if __name__ == "__main__":
    main()

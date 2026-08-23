"""doxa.app -- the single-pane Textual shell.

Phase 1 built this as one pane over an in-process SessionEngine; Phase 2's
daemon split keeps the shell almost unchanged and swaps what sits behind
``self.engine``: a factory now supplies EITHER an in-process
``SessionEngine`` (tests, ``--in-process``) or a ``doxa.client.EngineClient``
attached to a session daemon over its Unix socket (the default --
see doxa/daemon.py). The app consumes the same async-iterator surface either
way. One addition the split forces: turn events can now arrive OUT-OF-BAND
-- replayed history right after a reattach, or a turn another attached
client is driving -- so the peer pump renders those into turn blocks too,
not just peer messages.

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
from textual.widgets import Collapsible, Input, LoadingIndicator, Static

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


def git_branch_symbol() -> str:
    """The nerd-font branch glyph (U+E0A0) when the user opted in via
    DOXA_NERD_FONT (a TUI cannot detect font glyph coverage itself);
    the universally-rendering ⎇ otherwise."""
    return "" if os.environ.get("DOXA_NERD_FONT", "").strip() else "⎇"


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


class DoxaApp(App):
    """The DOXA terminal."""

    CSS_PATH = "theme.tcss"
    TITLE = "DOXA"
    # Ctrl+P (App.COMMAND_PALETTE_BINDING's default) opens the built-in
    # CommandPalette; DoxaCommandProvider feeds it doxa_commands() below.
    COMMANDS = App.COMMANDS | {DoxaCommandProvider}
    # Ctrl+R: history search over LORE's session FTS (doxa/history.py) --
    # instant BM25 over every indexed session, not a scrollback scan.
    # Ctrl+C: quit. Textual 5 binds ctrl+c to a "press ctrl+q to quit"
    # notification app-side and to "copy" on a focused Input -- with the
    # prompt input permanently focused, Ctrl+C therefore did NOTHING
    # quit-shaped (the dogfooding bug). priority=True beats both: one press
    # = quit-detach (daemon keeps running), double press within
    # CTRL_C_DOUBLE_SECS = quit-stop -- see action_ctrl_c_quit.
    BINDINGS = [
        ("ctrl+r", "history_search", "History"),
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
        self.engine: Any | None = None
        # The daemon-split seam: engine_factory builds whatever this shell
        # drives (in-process SessionEngine by default; an EngineClient when
        # doxa.cli attached us to a daemon). new_session_factory builds a
        # FRESH session for the palette's "new session" command -- distinct
        # because an attach-flavored engine_factory must not be re-invoked
        # to mean "new".
        self._engine_factory = engine_factory or (
            lambda: SessionEngine(cwd=self.cwd, model=self.model)
        )
        self._new_session_factory = new_session_factory or self._engine_factory
        self._engine_ready = asyncio.Event()
        # Out-of-band turn rendering state (replayed history after reattach,
        # or a turn another attached client drives) -- see _peer_pump.
        self._oob_turn: TurnBlock | None = None
        self._oob_chips: dict[str, ToolChip] = {}
        # Ctrl+C double-press window (see action_ctrl_c_quit): the armed
        # timer that will quit-detach when it fires; a second Ctrl+C while
        # it is armed cancels it and quit-stops instead.
        self._ctrl_c_timer: Any = None
        # Status-line git chip -- built in _boot (per engine, since attach
        # can land in another project's cwd), refreshed event-driven only.
        self._git: GitLine | None = None

    def compose(self) -> ComposeResult:
        yield Static("", id="belief-inspector")  # hidden stub, palette-toggled
        yield VerticalScroll(id="block-list")
        yield Static("doxa · connecting…", id="status-bar")
        yield Input(placeholder="Ask DOXA…", id="prompt-input")

    async def on_mount(self) -> None:
        self.engine = self._engine_factory()
        self.query_one("#prompt-input", Input).focus()
        self.run_worker(self._boot(), exclusive=True, group="engine")
        self.run_worker(self._peer_pump(), exclusive=True, group="peers")

    async def _boot(self) -> None:
        assert self.engine is not None
        await self.engine.start()
        # Engine cwd wins over the app's own (attach may cross projects);
        # GitLine's constructor runs one git subprocess -- off the loop.
        git_cwd = str(getattr(self.engine, "cwd", None) or self.cwd)
        self._git = await asyncio.to_thread(GitLine, git_cwd)
        self._engine_ready.set()
        self._refresh_status()

    async def _peer_pump(self) -> None:
        """Consume the engine's out-of-band stream for the life of the app:
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

    # -- palette (Ctrl+P) --------------------------------------------

    def doxa_commands(self) -> "list[tuple[str, str, Callable[[], Any]]]":
        """The DOXA command surface, as (name, help, callback) tuples --
        consumed by palette.DoxaCommandProvider on every palette open, so
        the attach picker's entries reflect the live registry each time."""
        commands: list[tuple[str, str, Any]] = [
            (
                "New session",
                "Start a fresh DOXA session in this project and switch to it",
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
                "Close this TUI; the session daemon keeps running "
                "(reattach with `doxa attach`)",
                self.action_quit,
            ),
            (
                "Quit: stop session",
                "Finalize now (LORE review + index) and stop the daemon",
                self.action_quit_stop,
            ),
        ]
        # Attach picker: live daemon-hosted sessions from the shared
        # peer/daemon registry, newest first, never this session itself.
        self_id = str(getattr(self.engine, "session_id", "") or "") or None
        for entry in peers_mod.list_daemons(self_id=self_id):
            commands.append((
                f"Attach: {entry.title} ({entry.session_id[:8]})",
                f"Reattach to the live session in {entry.cwd}",
                partial(self._cmd_attach, entry),
            ))
        return commands

    def _cmd_new_session(self) -> None:
        self.run_worker(
            self._switch_engine(self._new_session_factory),
            exclusive=True, group="switch",
        )

    def _cmd_attach(self, entry: peers_mod.PeerInfo) -> None:
        from .client import EngineClient  # deferred: tests without a daemon never import it

        socket_path = entry.daemon_socket
        if not socket_path:
            return
        self.run_worker(
            self._switch_engine(lambda: EngineClient(socket_path)),
            exclusive=True, group="switch",
        )

    def _cmd_list_peers(self) -> None:
        self.run_worker(self._run_command("/peers"), group="command")

    def _cmd_message_peer(self) -> None:
        prompt = self.query_one("#prompt-input", Input)
        prompt.value = "/msg "
        prompt.cursor_position = len(prompt.value)
        prompt.focus()

    async def _switch_engine(self, make_engine: "Callable[[], Any]") -> None:
        """Swap the live engine handle: detach/finalize the old one, build
        the new one (off-loop -- a daemon spawn blocks on subprocess+registry
        polling), reset the block list, and restart the boot + pump workers
        (both exclusive in their groups, so the old pump dies with its
        engine)."""
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

    def action_history_search(self) -> None:
        """Ctrl+R: modal FTS search over LORE's session index. A chosen hit
        inserts its text reference (full session id + timestamp + snippet)
        into the prompt input -- material for the next turn, never an
        auto-sent prompt."""
        from .history import HistorySearchScreen, hit_reference

        def _insert(hit: "dict | None") -> None:
            if not hit:
                return
            prompt = self.query_one("#prompt-input", Input)
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

    async def action_ctrl_c_quit(self) -> None:
        """Ctrl+C (priority binding). First press: arm the double-press
        window, then quit-DETACH when it expires -- over a daemon client the
        session keeps running (same as ctrl+q / palette 'Quit: detach');
        in-process, finalize runs right here, so Ctrl+C always exits
        cleanly. Second press inside the window: quit-STOP (finalize NOW,
        daemon included -- the palette's 'Quit: stop session')."""
        if self._ctrl_c_timer is not None:
            self._ctrl_c_timer.stop()
            self._ctrl_c_timer = None
            await self.action_quit_stop()
            return
        self.notify(
            "detaching — Ctrl+C again to STOP the session (finalize now)",
            severity="warning",
            timeout=CTRL_C_DOUBLE_SECS,
        )
        self._ctrl_c_timer = self.set_timer(CTRL_C_DOUBLE_SECS, self.action_quit)

    async def action_quit_stop(self) -> None:
        """Palette 'Quit: stop session' -- finalize NOW. Over a daemon
        client this stops the daemon itself (LORE review + index run
        there); in-process it is plain finalize-and-quit."""
        engine, self.engine = self.engine, None
        if engine is not None:
            stop = getattr(engine, "stop", None)
            with contextlib.suppress(Exception):
                if stop is not None:
                    await stop()
                else:
                    await engine.finalize()
        await super().action_quit()

    async def action_quit(self) -> None:
        """ctrl+q / palette 'Quit: detach'. Over a daemon client,
        finalize() only DETACHES -- the daemon lingers and runs the
        session-end review + index itself once the last client is gone
        (or on `doxa stop`). In-process (Phase 1 shape), finalize() still
        runs the review + index right here, host-driven (PHASE0 redesign
        item 1: no SessionEnd hook exists)."""
        if self.engine is not None:
            await self.engine.finalize()
        await super().action_quit()


def main() -> None:
    DoxaApp().run()


if __name__ == "__main__":
    main()

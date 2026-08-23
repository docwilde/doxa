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

from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.fuzzy import Matcher
from textual.widgets import (
    Collapsible,
    Input,
    LoadingIndicator,
    OptionList,
    Static,
    TabbedContent,
    TabPane,
)
from textual.widgets.option_list import Option

from . import auth as auth_mod
from . import commands as commands_mod
from . import config as config_mod
from . import identity as identity_mod
from . import images as images_mod
from . import peers as peers_mod
from .engine import EngineEvent, SessionEngine
from .identity import tier_short  # noqa: F401 -- re-exported: the status
# line's plan label lives in doxa.identity now (precise local tier first,
# SDK subscriptionType second); app.py keeps the name callers already use.
from .palette import DoxaCommandProvider
from .peers import PeerSendError, age_secs


# Ctrl+C quit semantics: the first press arms this window and then detaches;
# a second press inside it upgrades to quit-stop (finalize NOW).
CTRL_C_DOUBLE_SECS = 2.0

# Model aliases the installed CLI documents for --model ("provide an alias
# for the latest model (e.g. 'fable', 'opus', or 'sonnet') or a model's
# full name"). /model accepts any string -- these are what it OFFERS, and
# the list is short because a stale menu of full model ids is worse than
# an alias that always resolves to the current one.
MODEL_ALIASES = ("haiku", "sonnet", "opus", "fable")


def help_text() -> str:
    """``/help``, generated from the command registry -- never a
    hand-maintained list, because a hand-maintained list is a list that is
    wrong by the second command anyone adds."""
    lines = ["commands", ""]
    width = max(len(cmd.call_form()) for cmd in commands_mod.REGISTRY)
    for command in commands_mod.REGISTRY:
        suffix = "  (sent to the CLI, not intercepted)" if command.passthrough else ""
        lines.append(f"  {command.call_form():<{width}}  {command.summary}{suffix}")
    return "\n".join(lines)


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
    the universally-rendering ⎇ otherwise. Read through doxa.config, so
    the settings modal's stored value works exactly like the env var --
    env first, file second (doxa/config.py's one precedence rule)."""
    return "\ue0a0" if config_mod.raw("DOXA_NERD_FONT").strip() else "⎇"


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


class SlashComplete(OptionList):
    """The slash-command dropdown above the prompt input.

    Textual has no built-in input autocomplete, so this is an OptionList
    overlay driven entirely by the prompt's key protocol (see
    :class:`PromptInput`) -- it never takes focus, because a dropdown that
    steals the caret from the line you are typing is worse than no
    dropdown.

    It reads :data:`doxa.commands.REGISTRY` and scores with the SAME
    ``textual.fuzzy.Matcher`` the Ctrl+P palette uses: one registry, one
    matcher, two surfaces. "/" alone lists everything (registration order,
    stable-sorted under equal scores); "/pe" narrows to /peers.

    Dismissal latches: Esc, or completing an entry, closes the dropdown and
    keeps it closed for the rest of this "/"-prefixed line. Deleting the
    leading "/" clears the latch, which is what makes the next "/" open a
    fresh dropdown."""

    can_focus = False

    def __init__(self) -> None:
        super().__init__(id="slash-complete")
        self.display = False
        self.matches: list[commands_mod.SlashCommand] = []
        self._dismissed = False

    @property
    def is_open(self) -> bool:
        return bool(self.display)

    def sync(self, value: str) -> None:
        """React to the prompt's current text: open, filter, or close."""
        if not value.startswith("/"):
            self._dismissed = False  # the leading "/" is gone: latch clears
            self.close()
            return
        if self._dismissed:
            return
        if " " in value:
            # Past the command token (a space means arguments are being
            # typed): the dropdown has nothing left to say about this line.
            self.close()
            return
        matcher = Matcher(value)
        scored = [
            (matcher.match(cmd.name), index, cmd)
            for index, cmd in enumerate(commands_mod.REGISTRY)
        ]
        # Score first, registration order as the tie-break -- so "/" lists
        # the registry in the order it declares itself.
        scored = sorted(
            (item for item in scored if item[0] > 0),
            key=lambda item: (-item[0], item[1]),
        )
        self.matches = [cmd for _score, _index, cmd in scored]
        if not self.matches:
            self.close()
            return
        self.clear_options()
        for cmd in self.matches:
            self.add_option(Option(f"{cmd.call_form():<28} {cmd.summary}"))
        self.highlighted = 0
        self.display = True

    def close(self) -> None:
        if self.display:
            self.display = False
        self.matches = []

    def dismiss_for_this_line(self) -> None:
        """Esc / a completed entry: stay shut until the "/" line is gone."""
        self._dismissed = True
        self.close()

    def move(self, delta: int) -> None:
        if not self.matches:
            return
        current = self.highlighted or 0
        self.highlighted = (current + delta) % len(self.matches)

    def chosen(self) -> "commands_mod.SlashCommand | None":
        if not self.matches:
            return None
        index = self.highlighted if self.highlighted is not None else 0
        return self.matches[index] if 0 <= index < len(self.matches) else None


class PromptInput(Input):
    """The prompt line, plus the slash-autocomplete key protocol.

    While the dropdown is open, up/down/tab/enter/escape belong to IT.
    ``on_key`` on the focused widget runs BEFORE that widget's own bindings,
    so consuming the key here (stop + prevent_default) is what keeps Enter
    from submitting a half-typed command and Tab from moving focus out of
    the prompt. With the dropdown closed, every one of those keys behaves
    exactly as it always did -- the protocol is additive, never a
    reinterpretation of the prompt line."""

    def __init__(self, dropdown: SlashComplete, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.dropdown = dropdown

    def complete(self) -> bool:
        """Insert the highlighted command. Commands that take arguments
        complete with a trailing space (the caret lands where the argument
        goes); commands that don't are left ready to submit."""
        command = self.dropdown.chosen()
        if command is None:
            return False
        self.dropdown.dismiss_for_this_line()
        self.value = command.name + (" " if command.usage else "")
        self.cursor_position = len(self.value)
        return True

    def on_key(self, event: events.Key) -> None:
        if not self.dropdown.is_open:
            return
        if event.key == "escape":
            self.dropdown.dismiss_for_this_line()
        elif event.key == "down":
            self.dropdown.move(1)
        elif event.key == "up":
            self.dropdown.move(-1)
        elif event.key in ("tab", "enter"):
            command = self.dropdown.chosen()
            if event.key == "enter" and command is not None and self.value == command.name:
                # Already typed in full: there is nothing to complete, so
                # Enter means SEND. (Otherwise typing a whole command would
                # cost two Enters -- one to "complete" it into itself.)
                self.dropdown.dismiss_for_this_line()
                return  # not consumed: Input's own submit binding runs
            if not self.complete():
                return
        else:
            return
        event.stop()
        event.prevent_default()


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
        # The dropdown sits directly ABOVE the prompt (last two children):
        # in a terminal the block list simply gives up the rows while it is
        # open, which reads as an overlay without the layer bookkeeping a
        # floating panel would need over a TabbedContent.
        dropdown = SlashComplete()
        yield dropdown
        yield PromptInput(dropdown, placeholder="Ask DOXA…", id="prompt-input")

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
        field (the SDK's connect-time account block, the CLI's own local
        config for the precise plan tier, the engine handle, the git chip,
        LORE's store) -- absent fields are omitted, not invented.

        plan and org are SEPARATE lines on purpose: an organization name is
        informative, never the plan. Conflating the two is how a Max
        subscription can end up reading as somebody's "team subscription"."""
        engine = self.engine
        account = getattr(engine, "account", None) or {}
        local = identity_mod.local_account()
        lines: list[str] = []
        if account.get("email"):
            lines.append(f"account  {account['email']}")
        elif local.get("emailAddress"):
            lines.append(f"account  {local['emailAddress']}")
        plan_line = self._plan_line(account, local)
        if plan_line:
            lines.append(f"plan     {plan_line}")
        org = identity_mod.organization(account, local)
        if org:
            role = local.get("organizationRole")
            lines.append(f"org      {org}" + (f" ({role})" if role else ""))
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

    @staticmethod
    def _plan_line(account: dict, local: dict) -> str | None:
        """`max 20x (Claude Max · firstParty)` -- the precise local tier
        leading, with the coarse SDK string kept visible as its provenance.
        Falls back to the SDK string alone, then to nothing at all."""
        tier = identity_mod.account_tier(account, local)
        if not tier:
            return None
        detail = [
            str(account[k]) for k in ("subscriptionType", "apiProvider")
            if account.get(k)
        ]
        # The SDK string stays visible as provenance -- it is what the
        # session actually reported -- unless it IS the label verbatim.
        if detail and detail[0].strip().lower() == tier:
            detail = detail[1:]
        return tier + (f" ({' · '.join(detail)})" if detail else "")

    def _refresh_identity(self) -> None:
        """Re-render the identity block in place -- after an auth flow the
        account, the plan tier and the organization may all have changed,
        and a stale identity block is worse than none."""
        try:
            block = self.query_one("#identity-block", SystemBlock)
        except Exception:
            return
        engine = self.engine
        cwd = str(getattr(engine, "cwd", None) or self.cwd)
        block.text = self._identity_text(cwd)
        block.update(f"▎ doxa\n{block.text}")

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
        tier = identity_mod.account_tier(account)
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

    @on(Input.Changed, "#prompt-input")
    def _on_prompt_changed(self, event: Input.Changed) -> None:
        """The autocomplete's only trigger: what the prompt currently says.
        Cheap by construction -- a registry scan of a handful of rows, no
        I/O, no timer (this app does not poll)."""
        event.stop()
        self.query_one("#slash-complete", SlashComplete).sync(event.value)

    @on(OptionList.OptionSelected, "#slash-complete")
    def _on_slash_selected(self, event: OptionList.OptionSelected) -> None:
        """Clicking an entry completes it, same as Tab/Enter would."""
        event.stop()
        prompt = self.query_one("#prompt-input", PromptInput)
        prompt.dropdown.highlighted = event.option_index
        prompt.complete()
        prompt.focus()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "prompt-input":
            return  # a modal overlay's input is never a prompt
        event.stop()  # this pane's prompt is nobody else's business
        self.query_one("#slash-complete", SlashComplete).close()
        prompt = event.value.strip()
        if not prompt:
            return
        event.input.value = ""
        # Only rows of the slash registry (doxa/commands.py) are
        # intercepted, and passthrough rows deliberately are not: the
        # literal "/compact" convention has to REACH the CLI to do anything.
        command = commands_mod.lookup(prompt)
        if command is not None and not command.passthrough:
            self.run_worker(self._run_command(prompt), group="command")
            return
        self.run_worker(self._run_turn(prompt), exclusive=True, group="turn")

    # -- slash commands ----------------------------------------------

    def _command_handlers(self) -> "dict[str, Callable[[str], Any]]":
        """name -> coroutine handler, each taking the argument string.

        The keys of this dict and ``commands.interactive_names()`` are
        asserted equal by the test suite: the registry describes, the pane
        executes, and neither may grow a command the other doesn't have."""
        return {
            "/peers": self._cmd_peers,
            "/msg": self._cmd_msg,
            "/img": self._cmd_img,
            "/login": partial(self._cmd_auth, "login"),
            "/logout": partial(self._cmd_auth, "logout"),
            "/settings": self._cmd_settings,
            "/model": self._cmd_model,
            "/effort": self._cmd_effort,
            "/usage": self._cmd_usage,
            "/clear": self._cmd_clear,
            "/help": self._cmd_help,
        }

    async def _cmd_settings(self, args: str) -> None:
        self.app.action_settings()

    async def _cmd_model(self, args: str) -> None:
        """/model -- switch the model for subsequent turns, in place.

        The SDK's set_model is a control request, so this is genuinely a
        switch and not a restart: the transcript, the daemon, the replay
        ring and every hook survive it untouched. The chosen model is also
        written to the settings file, because the settings modal's `model`
        row and this command are the SAME state -- one source of truth."""
        engine = self.engine
        current = str(getattr(engine, "model", None) or "default")
        if not args:
            lines = [f"model: {current}", ""]
            for alias in MODEL_ALIASES:
                mark = "▸" if alias in current.lower() else " "
                lines.append(f" {mark} {alias}")
            lines.append("")
            lines.append("usage: /model <alias or full model id>")
            await self._system("\n".join(lines))
            return
        wanted = args.split()[0]
        setter = getattr(engine, "set_model", None)
        if setter is None:
            await self._system(
                "model: this session's handle cannot switch models"
            )
            return
        try:
            resolved = await setter(wanted)
        except Exception as exc:  # noqa: BLE001 -- a refusal is information
            await self._system(f"model: {type(exc).__name__}: {exc}")
            return
        config_mod.save({"model": wanted})
        self._refresh_status()
        self._refresh_identity()
        await self._system(
            f"model: {current} → {resolved}  ·  transcript and session kept "
            "(SDK control request, no reconnect)"
        )

    async def _cmd_effort(self, args: str) -> None:
        """/effort -- honest about a real SDK limit.

        ``ClaudeAgentOptions.effort`` (the CLI's --effort) is a CONNECT-TIME
        option; there is no control request for it the way there is for the
        model. So this sets the level for NEW sessions and says plainly
        that the running one keeps its own, rather than pretending to
        change something it cannot."""
        from . import engine as engine_mod

        current = engine_mod.effort_level()
        if not args:
            lines = [f"effort: {current or '(CLI default)'}", ""]
            for level in engine_mod.EFFORT_LEVELS:
                lines.append(f" {'▸' if level == current else ' '} {level}")
            lines.append("")
            lines.append("usage: /effort <level>   ·   empty value clears it")
            lines.append(
                "the SDK sets effort at connect only — a change applies to "
                "NEW sessions (/clear, a new tab), never to this one"
            )
            await self._system("\n".join(lines))
            return
        level = args.split()[0].lower()
        if level not in engine_mod.EFFORT_LEVELS and level != "default":
            await self._system(
                f"effort: unknown level {level!r} — "
                + ", ".join(engine_mod.EFFORT_LEVELS)
            )
            return
        config_mod.save({"effort": "" if level == "default" else level})
        await self._system(
            f"effort: new sessions will use {level} — this session keeps "
            f"{current or 'the CLI default'} (the SDK has no live setter)"
        )

    async def _cmd_usage(self, args: str) -> None:
        await self._system(self._usage_text())

    def _usage_text(self) -> str:
        """/usage: the session's REAL numbers, and the account's real
        headroom. Both sides are measured, neither is modelled -- the
        token counts are the CLI's own per-result usage block, and the
        percentages are the utilization snapshot the CLI itself fetched
        and cached (doxa.identity.usage). Anything absent is omitted."""
        engine = self.engine
        summary = {}
        if engine is not None and hasattr(engine, "usage_summary"):
            summary = engine.usage_summary() or {}
        rows: list[tuple[str, str]] = []
        session_id = str(summary.get("session_id") or "")
        if session_id:
            rows.append(("session", session_id[:8]))
        rows.append(("model", str(summary.get("model") or "default")))
        rows.append(("turns", f"{int(summary.get('num_turns') or 0):,}"))
        for key, label in (
            ("input_tokens", "tokens in"),
            ("output_tokens", "tokens out"),
            ("cache_read_input_tokens", "cache read"),
            ("cache_creation_input_tokens", "cache write"),
        ):
            if key in summary:
                rows.append((label, f"{int(summary.get(key) or 0):,}"))
        ctx = summary.get("ctx_percentage")
        if ctx is not None:
            rows.append(("context", f"{float(ctx):.0f}%"))
        account = getattr(engine, "account", None) or {}
        tier = identity_mod.account_tier(account)
        cost = float(summary.get("total_cost_usd") or 0.0)
        if tier:
            rows.append(("plan", f"{tier}  (≈${cost:.4f} if API)"))
        else:
            rows.append(("cost", f"${cost:.4f}"))
        lines = [f"{label:<12} {value}" for label, value in rows]

        usage = identity_mod.usage()
        if usage is None:
            lines.append("")
            lines.append(
                "no subscription utilization cached by the claude CLI "
                "(API-key auth, or it has not fetched one yet)"
            )
            return "usage\n" + "\n".join(lines)
        lines.append("")
        for limit, label in (
            (usage.session, "session (5h)"),
            (usage.weekly, "weekly"),
            (usage.scoped, f"weekly ({usage.scope_label or 'model'})"),
        ):
            if limit is None:
                continue
            note = f"  ⚠ {limit.severity}" if limit.severity != "normal" else ""
            resets = f"  · resets {limit.resets_at[:16]}" if limit.resets_at else ""
            lines.append(f"{label:<12} {limit.percent}%{resets}{note}")
        age = usage.age_secs()
        if age is not None:
            lines.append("")
            lines.append(
                f"utilization cached by the claude CLI {_fmt_age(age)} ago"
                + (" — stale" if usage.is_stale() else "")
            )
        return "usage\n" + "\n".join(lines)

    async def _cmd_clear(self, args: str) -> None:
        """/clear -- a FRESH session in this tab, not a cleared screen.

        Distinct from Ctrl+T: the tab stays, its engine handle is
        finalized (LORE review + index, transcript rotated to the new
        session's file) and replaced. Distinct from scrolling away: the
        model's context is genuinely gone, because the session is."""
        factory = getattr(self.app, "_new_session_factory", None)
        if factory is None:
            await self._system("clear: no session factory on this app")
            return
        self.run_worker(
            self.switch_engine(factory), exclusive=True, group="switch"
        )

    async def _cmd_help(self, args: str) -> None:
        await self._system(help_text())

    async def _system(self, text: str) -> None:
        """Mount one doxa-generated block and stay scrolled to it."""
        block_list = self.query_one("#block-list", VerticalScroll)
        await block_list.mount(SystemBlock(text))
        block_list.scroll_end(animate=False)

    async def _run_command(self, prompt: str) -> None:
        await self._engine_ready.wait()
        name, _, args = prompt.strip().partition(" ")
        handler = self._command_handlers().get(name)
        if handler is None:  # registry/handler drift -- the closure test's job
            await self._system(f"unknown command: {name}")
            return
        await handler(args.strip())

    async def _cmd_img(self, args: str) -> None:
        # Debug render site for image support -- see ImageBlock.
        path = os.path.expanduser(args) if args else ""
        if not path:
            await self._system("usage: /img <path>")
            return
        if not os.path.isfile(path):
            await self._system(f"img: no such file: {path}")
            return
        block_list = self.query_one("#block-list", VerticalScroll)
        await block_list.mount(ImageBlock(path))
        block_list.scroll_end(animate=False)

    async def _cmd_peers(self, args: str) -> None:
        assert self.engine is not None
        peers = self.engine.list_peers()
        if not peers:
            await self._system("peers: none in this project right now")
            return
        lines = [
            f"{p.title}  {p.session_id[:8]}  {p.cwd}"
            f"  ·  up {_fmt_age(age_secs(p.started_at))}"
            for p in peers
        ]
        await self._system("peers:\n" + "\n".join(lines))

    async def _cmd_msg(self, args: str) -> None:
        assert self.engine is not None
        parts = args.split(maxsplit=1)
        if len(parts) < 2:
            await self._system("usage: /msg <session_prefix> <text>")
            return
        try:
            peer = await self.engine.send_peer_message(parts[0], parts[1])
        except PeerSendError as exc:
            await self._system(f"msg error: {exc}")
            return
        await self._system(f"sent to {peer.title} ({peer.session_id[:8]})")

    async def _cmd_auth(self, verb: str, args: str) -> None:
        """/login [provider] and /logout [provider].

        DOXA holds no credential and runs no auth logic: it suspends the
        TUI (App.suspend -- the supported way to hand the terminal over),
        execs the provider's OWN interactive auth CLI from the data table
        in doxa/auth.py, and on return re-reads identity so the block and
        the status chips reflect whoever is signed in NOW."""
        try:
            provider = auth_mod.resolve(args.split()[0] if args.split() else None)
        except auth_mod.AuthError as exc:
            await self._system(f"{verb}: {exc}")
            return
        cmd = provider.command_for(verb)
        try:
            with self.app.suspend():
                code = auth_mod.run_auth_command(cmd)
        except Exception as exc:  # noqa: BLE001 -- SuspendNotSupported and
            # friends must surface as an ordinary block, not a crashed TUI.
            await self._system(
                f"{verb}: cannot hand the terminal to {provider.binary} here "
                f"({exc}) — run `{' '.join(cmd)}` in another terminal instead"
            )
            return
        # The CLI may have rewritten its config within one mtime tick.
        identity_mod.invalidate()
        self._refresh_identity()
        self._refresh_status()
        shown = " ".join(cmd)
        if code == 0:
            await self._system(f"{shown} — done; identity re-read")
        else:
            await self._system(f"{shown} — exited {code}; identity re-read")

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
        Binding("ctrl+comma", "settings", "Settings", show=False, priority=True),
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
        # Slash registry, second surface: every row that declares a palette
        # label appears here too (doxa/commands.py is the single list --
        # the prompt's autocomplete reads the same rows). Rows that need
        # arguments PREFILL the prompt instead of running blind.
        for command in commands_mod.REGISTRY:
            if not command.palette:
                continue
            callback = (
                partial(self._cmd_prefill, command.name + " ")
                if command.palette_prefill
                else partial(self._cmd_run_slash, command.name)
            )
            commands.append((command.palette, command.summary, callback))
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

    def _cmd_run_slash(self, name: str) -> None:
        """Palette -> the ACTIVE pane's slash handler. One dispatch path for
        both surfaces: the palette never reimplements a command."""
        pane = self.active_pane
        if pane is not None:
            pane.run_worker(pane._run_command(name), group="command")

    def _cmd_prefill(self, text: str) -> None:
        pane = self.active_pane
        if pane is None:
            return
        prompt = pane.query_one("#prompt-input", Input)
        prompt.value = text
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

    def action_settings(self) -> None:
        """Ctrl+, / /settings / the palette's Settings entry -- one modal,
        three doors. Saving re-reads the affected surfaces immediately
        (the status line's branch glyph and the plan chip are the two that
        show without a new session); knobs the ENGINE reads take effect on
        its next read, which is per turn by construction."""
        from .settings import SettingsScreen

        def _saved(saved: "bool | None") -> None:
            if not saved:
                return
            config_mod.invalidate()
            for pane in self.panes():
                pane._refresh_status()

        self.push_screen(SettingsScreen(), callback=_saved)

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

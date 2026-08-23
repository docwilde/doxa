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
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.fuzzy import Matcher
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import (
    Collapsible,
    Input,
    OptionList,
    Static,
    TabbedContent,
    TabPane,
)
from textual.widgets.option_list import Option

from . import auth as auth_mod
from . import clock as clock_mod
from . import commands as commands_mod
from . import config as config_mod
from . import identity as identity_mod
from . import images as images_mod
from . import naming as naming_mod
from . import peers as peers_mod
from . import version as version_mod
from .engine import EngineEvent, SessionEngine
from .history import SEARCH_PREFIX, SessionSearch, hit_reference
from .identity import tier_short  # noqa: F401 -- re-exported: the status
# line's plan label lives in doxa.identity now (precise local tier first,
# SDK subscriptionType second); app.py keeps the name callers already use.
from . import palette as palette_mod
from .palette import DoxaCommandProvider, PaletteEntry
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

# Tab labels: `Model@repo:branch`, e.g. `Opus@doxa:main`. No provider
# segment: model names are distinct across providers anyway (`Opus` vs
# `deepseek-chat`), so the model already says who is answering, and a
# prefix repeated on every tab of the strip is width spent saying nothing.
#
# Widths: 34 columns total, which fits `Sonnet@re_ab_harness:kg-stats` and
# keeps four tabs on an 80-column terminal. When it does not fit, the MODEL
# is trimmed first (down to 4 columns: `Son…`), then the repo (down to 6),
# and the BRANCH last of all -- across several open tabs the model is
# usually identical and the branch is usually what differs, so trimming the
# branch would destroy exactly the information the label exists to carry.
TAB_LABEL_MAX = 34
TAB_MODEL_MIN = 4
TAB_REPO_MIN = 6


def short_model(model: "str | None") -> str:
    """The tier word out of a model name: claude-sonnet-4-5 -> `Sonnet`.

    A tab label has room for the thing that actually varies between tabs,
    and that is the tier, not the vendor prefix or the point release. An
    unrecognised name keeps its first dash-segment verbatim (deepseek-chat
    -> deepseek) rather than being truncated mid-word or title-cased into
    something that looks like a tier it is not; an unset model is
    "default", the same word the status bar and identity block use."""
    name = (model or "").strip().lower()
    if not name:
        return "default"
    for tier in MODEL_ALIASES:
        if tier in name:
            return tier.capitalize()
    return name.split("-", 1)[0] or name


def _shrink(text: str, width: int) -> str:
    """`Sonnet` -> `Son…` at width 4. Never returns more than ``width``."""
    if width <= 0:
        return ""
    return text if len(text) <= width else text[: width - 1] + "…"


def compose_tab_label(
    model: str,
    repo: str,
    branch: "str | None" = None,
    limit: int = TAB_LABEL_MAX,
) -> str:
    """`Model@repo:branch`, trimmed model-first when it must be.

    Outside a repo there is no branch and therefore NO colon: a dangling
    separator is a label saying "something is missing here", which is worse
    than the shorter label it replaced."""

    def build(m: str, r: str, b: "str | None") -> str:
        return f"{m}@{r}" + (f":{b}" if b else "")

    model_s, repo_s = model, repo
    for segment, floor in (("model", TAB_MODEL_MIN), ("repo", TAB_REPO_MIN)):
        text = build(model_s, repo_s, branch)
        overflow = len(text) - limit
        if overflow <= 0:
            return text
        current = model_s if segment == "model" else repo_s
        room = len(current) - floor
        if room <= 0:
            continue
        shrunk = _shrink(current, len(current) - min(room, overflow))
        if segment == "model":
            model_s = shrunk
        else:
            repo_s = shrunk
    # Only now, with the model and the repo already at their floors, does
    # the branch give ground.
    return ellipsize(build(model_s, repo_s, branch), limit)


def ellipsize(text: str, limit: int = TAB_LABEL_MAX) -> str:
    """Truncate with a real ellipsis. A tab that grows without bound pushes
    its neighbours off the bar, which costs more than the tail of a branch
    name is worth."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


# Context-pressure escalation. The README calls this out as "a containment
# signal, not decoration": the chip changes COLOR as the window fills, and
# keeps showing the percentage throughout -- a color that replaced the
# number would be decoration. Amber is "start thinking about /compact",
# red is "the next long tool result may not fit".
CTX_AMBER_PCT = 70.0
CTX_RED_PCT = 90.0
CTX_AMBER = "#E8A33D"
CTX_RED = "#D9534F"


def ctx_chip(percentage: "float | None") -> str:
    """The context chip, escalating normal -> amber -> red. Markup only:
    the percentage is always present, in every tier."""
    if percentage is None:
        return "ctx —"
    text = f"ctx {percentage:.0f}%"
    if percentage >= CTX_RED_PCT:
        return f"[{CTX_RED}]{text}[/]"
    if percentage >= CTX_AMBER_PCT:
        return f"[{CTX_AMBER}]{text}[/]"
    return text


def app_bindings() -> "list[tuple[str, str]]":
    """``(key, description)`` for every app-level binding, read off
    ``DoxaApp.BINDINGS`` itself -- the thing Textual actually dispatches.
    /help renders this, so a binding cannot exist without being documented
    and cannot be documented without existing."""
    rows: list[tuple[str, str]] = []
    for binding in DoxaApp.BINDINGS:
        if isinstance(binding, tuple):
            key, _action, description = (list(binding) + ["", "", ""])[:3]
        else:
            key, description = binding.key, binding.description
        if key and description:
            rows.append((key, description))
    return rows


def _pretty_key(key: str) -> str:
    """`ctrl+comma` is what Textual dispatches; `Ctrl+,` is what a keyboard
    has written on it."""
    parts = key.split("+")
    names = {"comma": ",", "left": "←", "right": "→", "ctrl": "Ctrl", "shift": "Shift"}
    return "+".join(names.get(p, p.upper() if len(p) == 1 else p.capitalize())
                    for p in parts)


def help_text() -> str:
    """``/help``, generated from the command registry AND the live binding
    list -- never a hand-maintained list, because a hand-maintained list is
    wrong by the second command anyone adds.

    Two columns for commands (call form + what it does, with its key
    binding where one reaches the same place), then a hotkeys section for
    the bindings that have no slash form at all."""
    lines = ["commands", ""]
    width = max(len(cmd.call_form()) for cmd in commands_mod.REGISTRY)
    bound: set[str] = set()
    # Same grouping and the same order the dropdown and the palette use --
    # commands.grouped() is the single sequence (see doxa/commands.py).
    for group, group_commands in commands_mod.grouped():
        lines.append(f"  {group}")
        for command in group_commands:
            note = ""
            if command.binding:
                bound.add(command.binding)
                note = f"   [{_pretty_key(command.binding)}]"
            if command.passthrough:
                note += "  (sent to the CLI, not intercepted)"
            lines.append(
                f"    {command.call_form():<{width}}  {command.summary}{note}"
            )
        lines.append("")
    while lines and lines[-1] == "":
        lines.pop()

    hotkeys = [(k, d) for k, d in app_bindings() if k not in bound]
    if hotkeys:
        lines += ["", "hotkeys (no slash form)", ""]
        key_width = max(len(_pretty_key(k)) for k, _d in hotkeys)
        for key, description in hotkeys:
            lines.append(f"  {_pretty_key(key):<{key_width}}  {description}")
    return "\n".join(lines)


def _one_line(text: str, limit: int = 70) -> str:
    return " ".join(text.split())[:limit]


def _fmt_age(secs: float) -> str:
    if secs < 60:
        return f"{int(secs)}s"
    if secs < 3600:
        return f"{int(secs // 60)}m"
    return f"{int(secs // 3600)}h{int((secs % 3600) // 60)}m"


def _stop_session(entry: "peers_mod.PeerInfo") -> bool:
    """End one live session by its registry entry -- the same path `doxa
    stop` takes: attach to its daemon socket, ask it to finalize (LORE
    review + index run there), let it exit. Returns whether it confirmed.

    Blocking, and deliberately so: callers hand it to a thread. A session
    without a daemon socket is in-process somewhere else and cannot be
    reached this way, which is reported as a failure rather than pretended
    away."""
    if not entry.daemon_socket:
        return False

    async def _stop() -> None:
        from .client import EngineClient

        client = EngineClient(entry.daemon_socket)
        await client.start()
        await client.stop()

    try:
        asyncio.run(_stop())
    except Exception:  # noqa: BLE001 -- a refusal is information, not a crash
        return False
    return True


def git_branch_symbol() -> str:
    """The nerd-font branch glyph (U+E0A0) when the user opted in via
    DOXA_NERD_FONT (a TUI cannot detect font glyph coverage itself);
    the universally-rendering ⎇ otherwise. Read through doxa.config, so
    the settings modal's stored value works exactly like the env var --
    env first, file second (doxa/config.py's one precedence rule)."""
    return "\ue0a0" if config_mod.raw("DOXA_NERD_FONT").strip() else "⎇"


class GitLine:
    """The `repo ⎇ branch sha` chip for the status line.

    Cost discipline (this sits next to the idle-CPU fix for a reason): the
    repo root is resolved ONCE at construction (the only subprocess); after
    that a read is a couple of stats and at most two small file reads --
    ``.git/HEAD`` re-parsed only when its mtime moves (checkout/switch touch
    it), and the branch's ref file re-read only when ITS mtime moves (a
    commit touches the ref, not HEAD -- which is exactly why the sha needs
    its own stat rather than riding HEAD's). ``packed-refs`` is the
    fallback for a branch with no loose ref, cached the same way.
    render() is called from event-driven sites only (_refresh_status: boot,
    turn done, peer events) -- NEVER from a timer or per-frame hook, which
    would recreate the busy-idle bug this app just shed."""

    def __init__(self, cwd: str) -> None:
        self.repo_root = peers_mod.repo_root_of(cwd)
        self.repo = Path(self.repo_root).name if self.repo_root else None
        self._head: Path | None = None
        self._gitdir: Path | None = None
        self._mtime: float | None = None
        self._branch: str | None = None
        self._ref: str | None = None      # refs/heads/<branch>, when attached
        self._sha: str | None = None
        self._sha_mtime: float | None = None
        self.worktree: str | None = None
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
                # A linked worktree's gitdir is <main>/.git/worktrees/<name>
                # -- the last component IS the worktree's name, which is
                # what `git worktree list` calls it. A submodule's gitdir
                # sits under modules/ instead and leaves this None.
                if git.parent.name == "worktrees":
                    self.worktree = git.name
            self._gitdir = git
            self._head = git / "HEAD"

    def render(self) -> str | None:
        """`repo ⎇ branch sha`, or None outside a repo (no chip at all).

        The short sha sits immediately right of the branch, because that is
        where "which commit am I actually on" belongs -- next to the branch
        it qualifies, not at the far end of the bar. Omitted when it would
        merely repeat the branch label (detached HEAD)."""
        if not self.repo:
            return None
        branch = self._read_branch()
        if not branch:
            return self.repo
        chip = f"{self.repo} {git_branch_symbol()} {branch}"
        sha = self._read_sha()
        if sha and not branch.startswith(sha):
            # "@" marks this hex string as a COMMIT. The status bar also
            # carries the detached-session handle, another short hex-ish
            # id a few chips away, and two unlabelled hex strings in one
            # bar read as one commit id printed twice (reported as exactly
            # that). Neither is dropped -- both are labelled instead.
            chip += f" @{sha}"
        return chip

    def branch_label(self) -> str | None:
        """The branch as a TAB says it: `main`, or `main@featureX` inside a
        linked worktree.

        The worktree name is only added when it says something the label
        does not already carry -- `git worktree add ../foo -b foo` makes
        the worktree, the branch and the directory all "foo", and a label
        reading `foo ⎇ foo@foo` is three copies of one fact. So the suffix
        appears only when the worktree name differs from BOTH the branch
        and the repo slot beside it."""
        branch = self._read_branch()
        if not branch:
            return None
        if self.worktree and self.worktree not in (branch, self.repo):
            return f"{branch}@{self.worktree}"
        return branch

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
        self._sha_mtime = None  # HEAD moved: the sha must be re-read too
        if head.startswith("ref:"):
            self._ref = head.split(":", 1)[1].strip()
            self._branch = self._ref.removeprefix("refs/heads/")
        else:
            self._ref = None
            self._sha = head[:7] or None
            self._branch = head[:8] or None  # detached HEAD: short sha
        return self._branch

    def _read_sha(self) -> str | None:
        """The short sha of the branch tip. A COMMIT moves the ref file,
        not HEAD, so this stats the ref in its own right -- still event-
        driven (a stat per status refresh), still never polled."""
        if self._gitdir is None or self._ref is None:
            return self._sha
        ref_path = self._gitdir / self._ref
        try:
            mtime = ref_path.stat().st_mtime
        except OSError:
            return self._read_packed_sha()
        if mtime == self._sha_mtime:
            return self._sha
        self._sha_mtime = mtime
        try:
            self._sha = ref_path.read_text(
                encoding="utf-8", errors="replace"
            ).strip()[:7] or None
        except OSError:
            pass
        return self._sha

    def _read_packed_sha(self) -> str | None:
        """A freshly cloned or gc'd repo keeps its branch tips in
        packed-refs and has no loose ref file. Same mtime discipline."""
        if self._gitdir is None or self._ref is None:
            return self._sha
        packed = self._gitdir / "packed-refs"
        try:
            mtime = packed.stat().st_mtime
        except OSError:
            return self._sha
        if mtime == self._sha_mtime:
            return self._sha
        self._sha_mtime = mtime
        try:
            for line in packed.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines():
                if line.endswith(f" {self._ref}"):
                    self._sha = line.split(" ", 1)[0].strip()[:7] or None
                    break
        except OSError:
            pass
        return self._sha


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


class ThinkingMarker(Static):
    """The in-flight marker on a running turn -- STATIC, deliberately.

    This replaced a ``LoadingIndicator``, whose 16 Hz auto-refresh
    animation was the same class of cost as the leaked-timer bug this app
    already fixed once: every in-flight turn armed a repaint tick, and a
    terminal that repaints sixteen times a second to say "working" is
    spending the user's CPU on reassurance. The marker says the same thing
    with zero timers. Nothing in DOXA's own chrome animates; the only
    interval left in the app is Textual's caret blink on the focused
    prompt, which is load-bearing (it is how you find the caret) and runs
    at 2 Hz on exactly one widget."""

    def __init__(self) -> None:
        super().__init__("⋯ thinking", classes="thinking")


class TurnBlock(Collapsible):
    """One user turn + the assistant's response, foldable. Streaming text
    updates the body live; tool chips mount into `self.tools` as tool_call
    events arrive."""

    def __init__(self, prompt: str) -> None:
        self.prompt_text = prompt
        self.assistant_text = ""
        self.thinking = ThinkingMarker()
        self.body = Static("", classes="turn-body")
        self.tools = Vertical(classes="turn-tools")
        super().__init__(self.thinking, self.body, self.tools, title=self._render_title(), collapsed=False)

    def _render_title(self, suffix: str = "") -> str:
        return f"▎ {_one_line(self.prompt_text)}{suffix}"

    def hide_thinking(self) -> None:
        if self.thinking.display:
            self.thinking.display = False
            # Belt and braces after the LoadingIndicator removal: the old
            # indicator armed a 16 Hz auto-refresh on mount that nothing
            # else stopped, so every finished turn leaked a live timer and
            # idle CPU grew linearly with scrollback (measured ~0.2%/turn
            # headless; clearing it took 40 idle turn blocks from ~8.7% CPU
            # to baseline). ThinkingMarker arms nothing at all -- this line
            # now guarantees the invariant rather than repairing a widget.
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


class BeliefInspector(Vertical):
    """The docked belief pane (palette-toggled, hidden by default).

    Carries the house panel convention: a title row with a ✕ in its
    upper-right corner, clickable, because a panel that can only be closed
    by the same command that opened it is a panel mouse users get stuck
    in. Same treatment as the settings modal's header -- one convention,
    every panel."""

    def __init__(self) -> None:
        super().__init__(id="belief-inspector")
        self.text = ""
        self._body = Static("", id="inspector-body")

    def compose(self) -> ComposeResult:
        with Horizontal(id="inspector-header"):
            yield Static("▎ belief inspector — stub", id="inspector-title")
            yield Static("✕", id="inspector-close", classes="panel-close")
        yield self._body

    def set_text(self, text: str) -> None:
        self.text = text
        self._body.update(text)

    @property
    def renderable(self) -> str:
        """Kept so every caller that treated this as a Static still reads
        its content the same way."""
        return self.text


class ClockChip(Static):
    """The upper-right clock (item M): fixed width, dock:right on its own
    layer (see ``#doxa-clock`` in theme.tcss for why that -- not a flow
    sibling -- is what keeps the tab bar from ever being displaced).

    ONE timer for its whole life, and only while enabled: it rides
    Textual's own ``auto_refresh`` -- the exact ``_auto_refresh_timer``
    slot the no-idle-timer guard tests already watch (see
    ``tests/test_chrome.py``'s ``_armed`` and the matching helper in
    ``tests/test_app.py``) -- but re-armed to a freshly computed,
    BOUNDARY-ALIGNED delay on every tick (:func:`doxa.clock.
    seconds_until_boundary`) rather than left at a fixed period. That is
    what makes it minute-aligned when seconds are hidden instead of a 1Hz
    timer silently redrawing an identical string sixty times for one
    visible change, and second-aligned when they are shown. Disabled
    config never sets ``auto_refresh`` at all: no config, no timer, full
    stop -- the same contract :meth:`reconfigure` restores on every
    settings save, so toggling the clock off leaves nothing armed."""

    def __init__(self) -> None:
        super().__init__("", id="doxa-clock")
        self.cfg = clock_mod.ClockConfig.load()

    def on_mount(self) -> None:
        self.reconfigure()

    def reconfigure(self) -> None:
        """Re-read settings and restart clean. Called at mount, and again
        after the settings modal saves -- the settings screen's `_saved`
        callback owns that second call, the same way it already refreshes
        every pane's status bar."""
        self.auto_refresh = None  # stop whatever the OLD config armed
        self.cfg = clock_mod.ClockConfig.load()
        self.display = self.cfg.show
        if self.cfg.show:
            self._tick()

    def _tick(self) -> None:
        now = clock_mod.now_utc()
        text, warning = clock_mod.render(now, self.cfg)
        self.update(text)
        self.tooltip = warning  # the "visible-error fallback": a bad
        # custom format or an unresolvable timezone still renders (the
        # built-in format, system-local time) -- the tooltip is where the
        # degradation is disclosed rather than swallowed.
        self.auto_refresh = clock_mod.seconds_until_boundary(
            now, self.cfg.show_seconds
        )

    def automatic_refresh(self) -> None:
        """Textual calls this when ``_auto_refresh_timer`` fires. The
        default implementation just repaints; this one repaints AND
        re-arms the next boundary -- that re-arm, from inside the very
        callback the old timer is finishing, is what makes this ONE
        self-rescheduling timer rather than a periodic one this class
        would otherwise need to stop and restart from outside."""
        if self.cfg.show:
            self._tick()


class SlashComplete(OptionList):
    """The slash-command dropdown above the prompt input.

    Textual has no built-in input autocomplete, so this is an OptionList
    overlay driven entirely by the prompt's key protocol (see
    :class:`PromptInput`) -- it never takes focus, because a dropdown that
    steals the caret from the line you are typing is worse than no
    dropdown.

    It reads :data:`doxa.commands.REGISTRY` and scores with the SAME
    ``textual.fuzzy.Matcher`` the Ctrl+P palette uses: one registry, one
    matcher, two surfaces.

    Two modes, because browsing and filtering want different orders:

    * **"/" alone -- browsing.** Everything, in the registry's display
      order (functional group, then alphabetical inside it), with a dim
      header row per group. Insertion order is meaningless to a reader
      looking for a command they cannot name yet.
    * **"/pe" -- filtering.** Headers collapse and rows rank by match
      quality, then alphabetically. Someone who is typing has already told
      us what they want; grouping would only push the best match down.

    Dismissal latches: Esc, or completing an entry, closes the dropdown and
    keeps it closed for the rest of this "/"-prefixed line. Deleting the
    leading "/" clears the latch, which is what makes the next "/" open a
    fresh dropdown."""

    can_focus = False

    def __init__(self) -> None:
        super().__init__(id="slash-complete")
        self.display = False
        self.matches: list[commands_mod.SlashCommand] = []
        # Row-by-row map onto what the OptionList shows: None marks a group
        # header (a disabled Option), so highlight movement and selection
        # can both address rows by the SAME index the widget uses.
        self._rows: list["commands_mod.SlashCommand | None"] = []
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
        if value == "/":
            self._show_grouped()
        else:
            self._show_ranked(value)

    def _show_grouped(self) -> None:
        """Browsing: group headers, alphabetical inside each group."""
        self._rows = []
        self.matches = []
        self.clear_options()
        for group, group_commands in commands_mod.grouped():
            self._rows.append(None)
            self.add_option(Option(group, disabled=True))
            for cmd in group_commands:
                self._rows.append(cmd)
                self.matches.append(cmd)
                self.add_option(Option(self._row_text(cmd)))
        if not self.matches:
            self.close()
            return
        self.highlighted = self._first_command_row()
        self.display = True

    def _show_ranked(self, value: str) -> None:
        """Filtering: no headers, best match first, alphabetical to break
        ties -- a deterministic order, never insertion order."""
        matcher = Matcher(value)
        scored = [
            (matcher.match(cmd.name), cmd)
            for cmd in commands_mod.ordered()
        ]
        ranked = sorted(
            (item for item in scored if item[0] > 0),
            key=lambda item: (-item[0], item[1].name),
        )
        self.matches = [cmd for _score, cmd in ranked]
        self._rows = list(self.matches)
        if not self.matches:
            self.close()
            return
        self.clear_options()
        for cmd in self.matches:
            self.add_option(Option(self._row_text(cmd)))
        self.highlighted = 0
        self.display = True

    @staticmethod
    def _row_text(cmd: "commands_mod.SlashCommand") -> str:
        return f"  {cmd.call_form():<28} {cmd.summary}"

    def _first_command_row(self) -> int:
        for index, row in enumerate(self._rows):
            if row is not None:
                return index
        return 0

    def close(self) -> None:
        if self.display:
            self.display = False
        self.matches = []
        self._rows = []

    def dismiss_for_this_line(self) -> None:
        """Esc / a completed entry: stay shut until the "/" line is gone."""
        self._dismissed = True
        self.close()

    def move(self, delta: int) -> None:
        """Move the highlight, SKIPPING group headers -- a header is a
        label, never a destination, and arrowing onto one would offer a
        selection that cannot be completed."""
        if not self.matches or not self._rows:
            return
        index = self.highlighted if self.highlighted is not None else 0
        for _ in range(len(self._rows)):
            index = (index + delta) % len(self._rows)
            if self._rows[index] is not None:
                self.highlighted = index
                return

    def chosen(self) -> "commands_mod.SlashCommand | None":
        if not self._rows:
            return None
        index = self.highlighted if self.highlighted is not None else 0
        if 0 <= index < len(self._rows):
            return self._rows[index]
        return None


class PromptInput(Input):
    """The prompt line, plus the key protocols of the two popups above it.

    While a popup is open, up/down/tab/enter/escape belong to IT.
    ``on_key`` on the focused widget runs BEFORE that widget's own bindings,
    so consuming the key here (stop + prevent_default) is what keeps Enter
    from submitting a half-typed command and Tab from moving focus out of
    the prompt. With both popups closed, every one of those keys behaves
    exactly as it always did -- the protocol is additive, never a
    reinterpretation of the prompt line.

    The search popup is checked FIRST because it is the one that can be
    open while a command name is fully typed (``/search ...``); the two are
    mutually exclusive in practice, and this settles the order anyway."""

    def __init__(
        self, dropdown: SlashComplete, search: SessionSearch, **kwargs: Any
    ) -> None:
        super().__init__(**kwargs)
        self.dropdown = dropdown
        self.search = search

    def take_hit(self) -> bool:
        """Enter inside the search popup: the chosen session's reference
        REPLACES the ``/search …`` line that found it. The query was the
        prompt; the answer takes its place, ready to send (or to type
        around) -- the same insertion the Ctrl+R overlay used to do, minus
        the overlay."""
        hit = self.search.chosen()
        if hit is None:
            return False
        self.search.dismiss_for_this_line()
        self.value = hit_reference(hit)
        self.cursor_position = len(self.value)
        return True

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
        if self.search.is_open:
            if event.key == "escape":
                # Close, but KEEP what was typed: the query is prompt text
                # like any other, and deleting it would be a second,
                # unasked-for action on one keystroke.
                self.search.dismiss_for_this_line()
            elif event.key == "down":
                self.search.move(1)
            elif event.key == "up":
                self.search.move(-1)
            elif event.key == "enter":
                if not self.take_hit():
                    return  # no hits: Enter submits, and /search answers
            else:
                return
            event.stop()
            event.prevent_default()
            return
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


class CloseWithTurnRunning(ModalScreen[str]):
    """Ctrl+W with a turn still running: terminate, detach, or neither.

    The three-way choice is the point. Silently killing a running turn
    throws away work the user is waiting for; silently keeping it alive is
    the leak this whole change exists to end. So the one case where both
    defaults are wrong asks, and every other close stays instant."""

    BINDINGS = [("escape", "pick_cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="close-confirm"):
            yield Static("▎ a turn is still running", id="close-confirm-title")
            yield Static(
                "terminate  — stop the session now, losing the running turn\n"
                "detach     — close this tab and leave the turn running\n"
                "cancel     — keep the tab open",
                id="close-confirm-body",
            )
            with Horizontal(id="close-confirm-buttons"):
                yield Static("[ terminate ]", id="close-terminate")
                yield Static("[ detach ]", id="close-detach")
                yield Static("[ cancel ]", id="close-cancel")

    def action_pick_cancel(self) -> None:
        self.dismiss("cancel")

    def on_key(self, event: events.Key) -> None:
        choice = {"t": "terminate", "d": "detach", "c": "cancel"}.get(event.key)
        if choice:
            event.stop()
            self.dismiss(choice)

    @on(events.Click, "#close-terminate")
    def _click_terminate(self, event: events.Click) -> None:
        event.stop()
        self.dismiss("terminate")

    @on(events.Click, "#close-detach")
    def _click_detach(self, event: events.Click) -> None:
        event.stop()
        self.dismiss("detach")

    @on(events.Click, "#close-cancel")
    def _click_cancel(self, event: events.Click) -> None:
        event.stop()
        self.dismiss("cancel")


class TabRename(Input):
    """The inline editor a double-clicked tab header turns into.

    It is mounted INTO the tab strip, in the tab's own slot, with the tab
    hidden behind it -- so the field appears exactly where the label was
    rather than as a dialog about the label. Enter commits (Input.Submitted,
    handled by the app), Esc cancels, and losing focus cancels too: a stray
    click elsewhere must not silently rename a tab.

    An EMPTY value is not a name -- it is the instruction to go back to the
    automatic label, which is also the only way to un-pin a renamed tab."""

    def __init__(self, pane_id: str, value: str) -> None:
        super().__init__(value=value, id="tab-rename")
        self.pane_id = pane_id
        self.cursor_position = len(value)

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            event.stop()
            event.prevent_default()
            self.post_message(TabRenameCancelled(self.pane_id))

    def on_blur(self, event: events.Blur) -> None:
        self.post_message(TabRenameCancelled(self.pane_id))


class TabRenameCancelled(Message):
    """Esc (or a lost focus) inside the inline tab editor."""

    def __init__(self, pane_id: str) -> None:
        super().__init__()
        self.pane_id = pane_id


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
        # Tab label: `<short model> · <repo> ⎇ <branch>`, recomputed from
        # the tracked model and the (event-driven) GitLine wherever the
        # status bar is refreshed -- never on a timer, and only WRITTEN
        # when it actually changed, since writing it repaints the tab.
        self._tab_label: str | None = None
        # A tab the user NAMED. Set, it pins the label: model switches and
        # branch changes stop rewriting it, because a name the user typed
        # outranks anything DOXA can derive. Cleared (an empty rename) the
        # automatic label takes over again -- that is the only un-pin.
        self.custom_name: str | None = None
        # Outside a repo there is no repo:branch to label with, so the tab
        # is named from the session's first turn (doxa/naming.py, one Haiku
        # call, cached). None until that lands -- the dirname stands in
        # meanwhile, and a failure leaves it standing for good.
        self.generated_name: str | None = None
        self._naming_done = False
        # Is a turn running right now? Ctrl+W asks before killing one.
        self.turn_in_flight = False
        # Did the USER detach this session on purpose? Then it is no longer
        # this window's to terminate -- quit-stop leaves it alone, and
        # /sessions' kill-all-detached is the only thing that comes for it.
        self.detached_on_purpose = False
        # Subscription-headroom chip, recomputed at most once per turn-done
        # (see _refresh_usage_chip). Cached as a plain string because
        # _refresh_status runs on every peer event and must stay free.
        self._usage_chip: str | None = None

    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="block-list")
        yield Static("doxa · connecting…", id="status-bar")
        # Both popups sit directly ABOVE the prompt (the last children):
        # in a terminal the block list simply gives up the rows while one
        # is open, which reads as an overlay without the layer bookkeeping
        # a floating panel would need over a TabbedContent. They are never
        # open at once -- the slash dropdown closes at the first space,
        # which is exactly the keystroke that opens the search popup.
        search = SessionSearch(self.cwd)
        yield search
        dropdown = SlashComplete()
        yield dropdown
        yield PromptInput(
            dropdown, search, placeholder="Ask DOXA…", id="prompt-input"
        )

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
        # /search scopes "this project first" by cwd, and attach can land
        # this pane in another project: the engine's cwd wins here too.
        with contextlib.suppress(Exception):
            self.query_one("#session-search", SessionSearch).cwd = git_cwd
        # A session named on an earlier run keeps that name across restarts
        # -- the cache IS the persistence, and reusing it is what stops a
        # restore from re-spending a call per restored tab.
        session_id = str(getattr(self.engine, "session_id", "") or "")
        if session_id and not self.generated_name:
            cached = await asyncio.to_thread(naming_mod.cached_name, session_id)
            if cached:
                self.generated_name = cached
                self._naming_done = True
        self._refresh_usage_chip()
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
        # Version first: the one line that says WHICH DOXA this is. Its sha
        # is shown only when it differs from the sha the git chip below
        # already carries (or when the checkout is dirty, which the chip
        # never says) -- two identical hex strings in one block is the
        # confusion the @sha labelling exists to prevent.
        head_sha = self._git._read_sha() if self._git is not None else None
        lines: list[str] = [version_mod.version_line(head_sha)]
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
        swept = int(getattr(self.app, "swept_at_boot", 0) or 0)
        if swept:
            lines.append(
                f"swept    {swept} stale session presence file(s) — /sessions"
            )
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

    def _refresh_usage_chip(self) -> None:
        """Recompute the subscription-headroom chip (``s:9% w:48%``).

        The numbers are REAL and local: the `claude` CLI fetches its own
        utilization and caches the answer verbatim in its config
        (``cachedUsageUtilization``), so DOXA reads a file rather than
        calling an endpoint -- no new credential handling, and nothing to
        rate-limit. It is a cache, so a stale one is marked (`~`) instead
        of being presented as live.

        Called from boot and turn-done ONLY. Never a timer: this app pays
        for what events tell it, and a status chip is not worth a tick.
        API-key auth has no such cache and shows nothing here -- the plain
        $ tally next to it is already the honest figure for that case."""
        try:
            snapshot = identity_mod.usage()
        except Exception:  # noqa: BLE001 -- a chip must degrade to silence
            snapshot = None
        self._usage_chip = snapshot.chip() if snapshot is not None else None

    # -- the tab's own label -----------------------------------------

    def auto_label(self) -> str:
        """`Opus@doxa:main` -- which model is answering, and where.

        Both halves are tracked state already: the model is the engine's
        (so a live /model switch moves it), and the repo/branch come from
        the pane's GitLine, whose reads are event-driven stats -- this adds
        no polling and no subprocess. OUTSIDE a repo there is nothing after
        the `@` that would mean anything, so the session names itself from
        its first turn (doxa/naming.py) and the directory name stands in
        until it does."""
        engine = self.engine
        model = short_model(getattr(engine, "model", None) or self.model)
        cwd = str(getattr(engine, "cwd", None) or self.cwd)
        git = self._git
        if git is not None and git.repo:
            return compose_tab_label(model, git.repo, git.branch_label())
        return compose_tab_label(
            model, self.generated_name or Path(cwd).name or cwd
        )

    def display_name(self) -> str:
        """What this tab currently says -- the user's name for it if it has
        one, the automatic label otherwise."""
        return self.custom_name or self._tab_label or self.auto_label()

    def set_custom_name(self, name: "str | None") -> None:
        """Name this tab (pinning it), or pass None/"" to un-pin it and
        hand the label back to :meth:`auto_label`."""
        name = (name or "").strip()
        self.custom_name = name or None
        if self.custom_name:
            self.set_tab_label(ellipsize(self.custom_name))
        else:
            self._tab_label = None
            self.refresh_tab_label()

    def _maybe_name_tab(self, first_message: str) -> None:
        """After the FIRST completed turn of a repo-less session: ask Haiku
        for a name, once. Never on a tab the user named, never inside a
        repo (repo and branch already say where you are), and never twice
        -- a namer that failed must not retry in a loop."""
        if self._naming_done or self.custom_name:
            return
        git = self._git
        if git is not None and git.repo:
            return
        self._naming_done = True
        session_id = str(getattr(self.engine, "session_id", "") or "")
        self.run_worker(
            self._name_tab(session_id, first_message), group="naming"
        )

    async def _name_tab(self, session_id: str, first_message: str) -> None:
        name = await asyncio.to_thread(
            naming_mod.name_for, session_id, first_message
        )
        if not name or self.custom_name:
            return  # keep the dirname; the failure is final for this session
        self.generated_name = name
        self._tab_label = None
        self.refresh_tab_label()

    def refresh_tab_label(self) -> None:
        """Re-render the tab's label if it changed. Cheap, idempotent, and
        called from exactly where the status bar is refreshed. A NAMED tab
        keeps its name through every model switch and branch change --
        that is what pinning means."""
        if self.custom_name:
            return
        label = self.auto_label()
        if label == self._tab_label:
            return
        self.set_tab_label(label)

    def set_tab_label(self, text: str) -> None:
        """Write one label onto the tab header AND onto the pane's own
        title, which is what the palette's tab section and any later
        re-add of the pane read."""
        self._tab_label = text
        self._title = self.render_str(text)
        with contextlib.suppress(Exception):
            tabbed = self.app.query_one("#session-tabs", TabbedContent)
            tabbed.get_tab(self.id or "").label = text

    def _refresh_status(self) -> None:
        if self.engine is None:
            return
        self.refresh_tab_label()
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
        beliefs = self.engine.belief_count()
        parts = [model]
        git_chip = self._git.render() if self._git is not None else None
        if git_chip:  # hidden entirely outside a repo
            parts.append(git_chip)
        parts.append(cost)
        if self._usage_chip:  # only when real numbers exist -- see below
            parts.append(self._usage_chip)
        parts += [ctx_chip(self.engine.last_ctx_percentage), f"{beliefs} beliefs"]
        if getattr(self.engine, "detachable", False):
            sid = str(getattr(self.engine, "session_id", "") or "")
            if sid:  # attached to a daemon: show the reattach handle
                # Labelled and dimmed for the same reason the git sha wears
                # an "@": this is a SESSION id, not a commit id.
                parts.append(f"[#8A8073]⌁ session {sid[:8]}[/]")
        peer_count = self.engine.peer_count()
        if peer_count:  # hidden at 0 -- a solo session has no peers chip
            # Under detach-by-default a bare count is ambiguous: four live
            # peers could be four colleagues or two sessions the user left
            # running an hour ago. The ⌁ suffix (the same glyph the attach
            # handle wears) says how many are running with nobody watching
            # -- and is omitted entirely at zero, because "(0⌁)" is noise
            # on the common case. /sessions is where the number leads.
            detached = sum(
                1 for peer in self.engine.list_peers() if peer.clients == 0
            )
            parts.append(
                f"peers {peer_count}" + (f" ({detached}⌁)" if detached else "")
            )
        disabled = self.engine.disabled_tools()
        if disabled:  # two-strikes containment note -- hidden when empty
            parts.append(" ".join(f"⊘ {name}" for name in disabled))
        bar = self.query_one("#status-bar", Static)
        bar.update("  ·  ".join(parts))

    @on(Input.Changed, "#prompt-input")
    def _on_prompt_changed(self, event: Input.Changed) -> None:
        """The two popups' only trigger: what the prompt currently says.
        Cheap by construction -- a registry scan of a handful of rows for
        the dropdown, and for the search popup a debounce timer rather than
        a query (this app does not poll, and it does not hit SQLite on a
        keystroke either)."""
        event.stop()
        self.query_one("#slash-complete", SlashComplete).sync(event.value)
        self.query_one("#session-search", SessionSearch).sync(event.value)

    @on(OptionList.OptionSelected, "#slash-complete")
    def _on_slash_selected(self, event: OptionList.OptionSelected) -> None:
        """Clicking an entry completes it, same as Tab/Enter would."""
        event.stop()
        prompt = self.query_one("#prompt-input", PromptInput)
        prompt.dropdown.highlighted = event.option_index
        prompt.complete()
        prompt.focus()

    @on(OptionList.OptionSelected, "#session-search")
    def _on_search_selected(self, event: OptionList.OptionSelected) -> None:
        """Clicking a result takes it, same as Enter would."""
        event.stop()
        prompt = self.query_one("#prompt-input", PromptInput)
        prompt.search.highlighted = event.option_index
        prompt.take_hit()
        prompt.focus()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "prompt-input":
            return  # a modal overlay's input is never a prompt
        event.stop()  # this pane's prompt is nobody else's business
        self.query_one("#slash-complete", SlashComplete).close()
        self.query_one("#session-search", SessionSearch).close()
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
            "/setup": self._cmd_setup,
            "/doctor": self._cmd_doctor,
            "/model": self._cmd_model,
            "/effort": self._cmd_effort,
            "/usage": self._cmd_usage,
            "/clear": self._cmd_clear,
            "/detach": self._cmd_detach,
            "/sessions": self._cmd_sessions,
            "/rename": self._cmd_rename,
            "/search": self._cmd_search,
            "/update": self._cmd_update,
            "/help": self._cmd_help,
        }

    async def _cmd_settings(self, args: str) -> None:
        self.app.action_settings()

    async def _cmd_setup(self, args: str) -> None:
        self.app.action_setup()

    async def _cmd_doctor(self, args: str) -> None:
        """/doctor -- read-only, so this is the whole handler: run every
        check off the event loop (the claude CLI probes shell out) and
        print the report as an ordinary SystemBlock."""
        from . import doctor as doctor_mod

        checks = await asyncio.to_thread(doctor_mod.run_checks)
        await self._system(doctor_mod.report(checks))

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

    async def _cmd_detach(self, args: str) -> None:
        """/detach -- the deliberate opposite of Ctrl+W: this tab closes,
        its session keeps running, and quitting will not come back for
        it."""
        await self.app.action_detach_tab()

    async def _cmd_sessions(self, args: str) -> None:
        """/sessions -- what is actually alive, and the way to end it.

        Live means all three checks pass: presence file, live pid, and a
        socket that accepts a connection. A file left behind by a crash is
        not a session, and this is the surface where that has to be true,
        because it is where the user comes to find out what is running."""
        parts = args.split()
        verb = parts[0].lower() if parts else ""
        if verb == "kill":
            if len(parts) < 2:
                await self._system("usage: /sessions kill <session prefix>")
                return
            await self._kill_sessions(prefix=parts[1])
            return
        if verb in ("kill-detached", "kill-all-detached"):
            await self._kill_sessions(detached_only=True)
            return
        if verb:
            await self._system(
                f"sessions: unknown action {verb!r} — "
                "usage: /sessions [kill <prefix> | kill-detached]"
            )
            return
        await self._system(self._sessions_text())

    def _sessions_text(self) -> str:
        entries = peers_mod.read_registry(probe=True)
        if not entries:
            return "sessions: none live"
        attached = {
            str(getattr(p.engine, "session_id", "") or "")
            for p in self.app.panes()
        }
        names = {
            str(getattr(p.engine, "session_id", "") or ""): p.display_name()
            for p in self.app.panes()
        }
        rows = []
        for entry in sorted(entries, key=lambda e: e.started_at):
            here = entry.session_id in attached
            label = names.get(entry.session_id) or entry.title
            rows.append(
                f"{entry.session_id[:8]}  {label[:28]:<28} "
                f"up {_fmt_age(age_secs(entry.started_at)):<5} "
                f"{'attached here' if here else 'detached'}"
            )
        return (
            "sessions\n" + "\n".join(rows)
            + "\n\nkill one: /sessions kill <prefix>   ·   "
            "kill every detached one: /sessions kill-detached"
        )

    async def _kill_sessions(
        self, prefix: str = "", detached_only: bool = False
    ) -> None:
        """Terminate live sessions by prefix, or every session no tab of
        this window is attached to. Same stop path as Ctrl+W and `doxa
        stop`: the daemon finalizes (LORE review + index) and exits."""
        entries = peers_mod.read_registry(probe=True)
        attached = {
            str(getattr(p.engine, "session_id", "") or "")
            for p in self.app.panes()
        }
        if detached_only:
            targets = [e for e in entries if e.session_id not in attached]
        else:
            targets = [
                e for e in entries
                if e.session_id.startswith(prefix) or e.title.startswith(prefix)
            ]
        if not targets:
            await self._system(
                "sessions: nothing matched" if prefix
                else "sessions: nothing detached to kill"
            )
            return
        killed, failed = [], []
        for entry in targets:
            ok = await asyncio.to_thread(_stop_session, entry)
            (killed if ok else failed).append(entry.session_id[:8])
        swept = await asyncio.to_thread(peers_mod.sweep_stale)
        lines = []
        if killed:
            lines.append(f"stopped: {', '.join(killed)}")
        if failed:
            lines.append(f"could not stop: {', '.join(failed)}")
        if swept:
            lines.append(f"swept {swept} stale presence file(s)")
        await self._system("sessions\n" + "\n".join(lines))

    async def _cmd_rename(self, args: str) -> None:
        """/rename -- the keyboard door to what double-clicking a tab
        header does. An empty argument returns the tab to its automatic
        label, exactly like an emptied inline editor."""
        before = self.display_name()
        self.set_custom_name(args)
        if self.custom_name:
            await self._system(
                f"tab renamed: {before} → {self.custom_name}  ·  pinned "
                "(model and branch changes no longer rewrite it)"
            )
        else:
            await self._system(
                f"tab name cleared → {self.display_name()}  ·  back to the "
                "automatic label"
            )

    async def _cmd_search(self, args: str) -> None:
        """/search as a SUBMITTED command -- the fallback path.

        The command's real surface is the live popup, which opens the
        moment the separating space is typed and answers as you go. This
        runs when someone submits the line anyway (no hits highlighted, or
        Enter on an empty result): it prints the same hits as a block, so
        the command is never a no-op."""
        from . import history as history_mod

        term = args.strip()
        if not term:
            await self._system(
                "search: type `/search ` and keep typing — results appear "
                "above the prompt as you type (↑/↓ to pick, enter to insert "
                "the reference, esc to close)"
            )
            return
        cwd = str(getattr(self.engine, "cwd", None) or self.cwd)
        hits = await asyncio.to_thread(history_mod.search_sessions, term, cwd)
        if not hits:
            await self._system(f"search: no matches for {term!r}")
            return
        lines = [
            f"{str(h.get('title') or h.get('session_id', '?'))[:30]:<30} "
            f"{str(h.get('ts', ''))[:16]}  {str(h.get('snippet', ''))}"
            for h in hits
        ]
        await self._system(f"search: {len(hits)} hit(s)\n" + "\n".join(lines))

    async def _cmd_update(self, args: str) -> None:
        """/update -- fast-forward the checkout DOXA runs from, and say what
        moved. `--restart` is the explicit opt-in that stops THIS window's
        sessions afterwards and relaunches; without it nothing running is
        touched, because a terminal that restarts your work to update
        itself has its priorities backwards."""
        from . import update as update_mod

        restart = "--restart" in args.split()
        report = await asyncio.to_thread(update_mod.update)
        await self._system(report.text())
        if restart and report.status == "updated":
            await self._system(
                "update: stopping this window's sessions and relaunching…"
            )
            self.app.restart_requested = True
            self.app.run_worker(self.app.action_quit_stop(), group="tabs")
        elif restart:
            await self._system("update: nothing to restart for")

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
        self.turn_in_flight = True
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

        self.turn_in_flight = False
        self._refresh_status()
        # First completed turn of a repo-less session: name the tab from it.
        self._maybe_name_tab(prompt)

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
            # The one place the headroom chip is recomputed: a turn just
            # spent budget, and the CLI may have refreshed its own cache.
            self._refresh_usage_chip()
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
    # Ctrl+R: prefills "/search " -- the live session-search popup
    # (doxa/history.py) is the one search surface; the key is a shortcut to
    # it, not a second door.
    # instant BM25 over every indexed session, not a scrollback scan.
    # Ctrl+T/Ctrl+W: tab lifecycle (new same-repo session / close-detach).
    # Ctrl+C: quit. Textual 5 binds ctrl+c to a "press ctrl+q to quit"
    # notification app-side and to "copy" on a focused Input -- with the
    # prompt input permanently focused, Ctrl+C therefore did NOTHING
    # quit-shaped (the dogfooding bug). priority=True beats both: one press
    # = quit-detach ALL tabs (daemons keep running), double press within
    # CTRL_C_DOUBLE_SECS = quit-stop ALL -- see action_ctrl_c_quit.
    BINDINGS = [
        Binding("ctrl+p", "command_palette", "Command palette", show=False),
        Binding("ctrl+r", "history_search", "Search past sessions (/search)"),
        Binding("ctrl+comma", "settings", "Settings", show=False, priority=True),
        Binding("ctrl+t", "new_tab", "New tab", show=False, priority=True),
        Binding(
            "ctrl+w", "close_tab",
            "Close tab — DETACHES: the session keeps running",
            show=False, priority=True,
        ),
        # Ctrl+Q is Textual's own quit-the-app binding; this overrides it
        # deliberately and scopes it to the TAB. Quitting the window is
        # Ctrl+C (twice) here, and a key that ends one session must not be
        # the same key that ends all of them. priority=True for the same
        # reason Ctrl+C needs it: the focused Input would otherwise eat it.
        # (Terminal flow control does not: Textual's Linux driver clears
        # IXON/IXOFF, i.e. `stty -ixon`, so Ctrl+Q reaches the app.)
        Binding(
            "ctrl+q", "end_session",
            "End this session (finalize now) and close its tab",
            show=False, priority=True,
        ),
        Binding("ctrl+left", "prev_tab", "Previous tab", show=False, priority=True),
        Binding("ctrl+right", "next_tab", "Next tab", show=False, priority=True),
        Binding(
            "ctrl+c", "ctrl_c_quit",
            "Quit: detach all tabs (twice = stop the sessions)",
            show=False, priority=True,
        ),
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
        # Set by `/update --restart`: doxa.cli re-execs after the app exits,
        # which is the only place that can -- exec'ing out from under a
        # running Textual app would leave the terminal in raw mode.
        self.restart_requested = False
        # One sweep of the registry per launch: a crash can always leave a
        # presence file behind, so the fleet needs a sweeper that does not
        # depend on anything shutting down cleanly. Here rather than in a
        # worker because it must be done before the first status line reads
        # the registry -- it is a handful of stats and local connects, the
        # same class of startup cost as the image-mode probe below. Silently
        # cleaning is fine; silently IGNORING is not, so the count shows up
        # in the session's identity block when it is nonzero.
        self.swept_at_boot = peers_mod.sweep_stale()
        # Nothing in DOXA's chrome animates -- and that has to include the
        # animations DOXA did not write. Textual's own tab underline slides
        # to the newly-activated tab over 0.3 s (textual.widgets._tabs:
        # _highlight_active -> underline.animate), which is felt as lag when
        # arrowing through tabs and measured as ~290-345 ms of extra wall
        # time PER SWITCH. This one attribute is the supported off switch
        # for every Textual animation (App.animation_level, the same value
        # TEXTUAL_ANIMATIONS sets), and it is off for the same reason the
        # thinking marker stopped spinning: motion the user did not ask for
        # is paid for in their latency.
        self.animation_level = "none"
        # Settle the image-mode probe NOW, while this process still owns the
        # terminal: textual-image's TGP/sixel queries read their answer from
        # stdin, which Textual's own reader thread will grab the moment
        # App.run() starts (doxa/images.py's detection discipline note).
        images_mod.detect_mode()

    # -- pane plumbing -----------------------------------------------

    def _tab_title(self) -> str:
        """The label a pane is BORN with -- model plus directory, no git.

        The pane replaces it with its own ``auto_label`` the moment its
        engine and GitLine exist (one boot later); this exists so a tab
        never flashes a differently-shaped label on the way there. Two tabs
        on the same repo, branch and model do read alike, deliberately:
        they ARE alike, and the palette's tab section carries the session
        id that tells them apart."""
        self._tab_serial += 1
        name = Path(self.cwd).name or "session"
        return ellipsize(f"{short_model(self.model)} · {name}")

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
        yield BeliefInspector()  # hidden stub, palette-toggled
        yield ClockChip()  # upper-right, own layer -- see theme.tcss
        with TabbedContent(id="session-tabs"):
            yield self._make_pane(self._engine_factory)

    @on(TabbedContent.TabActivated)
    def _on_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        self._jump_tab_marker()
        pane = self.active_pane
        if pane is not None:
            with contextlib.suppress(Exception):
                pane.query_one("#prompt-input", Input).focus()

    # -- renaming a tab in place -------------------------------------

    @on(events.Click)
    def _on_click_maybe_rename(self, event: events.Click) -> None:
        """Double-clicking a tab header turns it into a field.

        Textual counts click chains for us (``event.chain``), so this needs
        no timing of its own -- and a SINGLE click keeps meaning "switch to
        this tab", untouched."""
        if event.chain != 2:
            return
        from textual.widgets import Tab

        widget = event.widget
        while widget is not None and not isinstance(widget, Tab):
            widget = widget.parent
        if widget is None:
            return
        pane = self._pane_for_tab(widget)
        if pane is None:
            return
        event.stop()
        self.run_worker(self._start_rename(pane), group="rename")

    def _pane_for_tab(self, tab: Any) -> "SessionPane | None":
        from textual.widgets._tabbed_content import ContentTab

        pane_id = ContentTab.sans_prefix(tab.id or "")
        for pane in self.panes():
            if pane.id == pane_id:
                return pane
        return None

    async def _start_rename(self, pane: "SessionPane") -> None:
        """Mount the editor in the tab's own slot and hide the tab behind
        it, so the label is edited where the label IS."""
        if self.query("#tab-rename"):
            return  # one rename at a time
        with contextlib.suppress(Exception):
            tabbed = self.query_one("#session-tabs", TabbedContent)
            tab = tabbed.get_tab(pane.id or "")
            editor = TabRename(pane.id or "", pane.display_name())
            editor.styles.width = max(len(editor.value) + 4, 14)
            await tab.parent.mount(editor, before=tab)
            tab.display = False
            editor.focus()

    @on(Input.Submitted, "#tab-rename")
    def _on_rename_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        pane_id = getattr(event.input, "pane_id", "")
        pane = next((p for p in self.panes() if p.id == pane_id), None)
        if pane is not None:
            # Empty means "no name", which is how a pinned tab is un-pinned.
            pane.set_custom_name(event.value)
        self._end_rename(pane_id)

    @on(TabRenameCancelled)
    def _on_rename_cancelled(self, event: TabRenameCancelled) -> None:
        event.stop()
        self._end_rename(event.pane_id)

    def _end_rename(self, pane_id: str) -> None:
        with contextlib.suppress(Exception):
            tabbed = self.query_one("#session-tabs", TabbedContent)
            tabbed.get_tab(pane_id).display = True
        for editor in list(self.query(TabRename)):
            editor.remove()
        pane = next((p for p in self.panes() if p.id == pane_id), None)
        if pane is not None:
            with contextlib.suppress(Exception):
                pane.query_one("#prompt-input", Input).focus()

    def _jump_tab_marker(self) -> None:
        """Put the active-tab underline at its destination on THIS frame.

        Textual's ``Tabs`` slides the marker: ``watch_active`` calls
        ``_highlight_active(animate=True)``, which arms a 0.02 s timer and
        then animates ``highlight_start``/``highlight_end`` over 0.3 s.
        ``animation_level = "none"`` (set in __init__) already takes the
        no-animate branch, but that branch still defers the move to
        ``call_after_refresh`` -- one frame late. Measured: the slide cost
        ~290-345 ms of WALL time per switch on top of the switch itself,
        constant regardless of scrollback, which is exactly the "tab
        switching is laggy" report.

        So the marker is placed directly, from the same geometry Textual's
        own mover reads. Failure is not an error: if Textual's internals
        move, this degrades to the built-in (still un-animated) path rather
        than breaking tab switching."""
        with contextlib.suppress(Exception):
            from textual.widgets import Tabs
            from textual.widgets._tabs import Underline

            tabs = self.query_one("#session-tabs", TabbedContent).query_one(Tabs)
            active = tabs.query_one("#tabs-list > Tab.-active")
            start, end = active.virtual_region.shrink(
                active.styles.gutter
            ).column_span
            if end <= start:
                return  # geometry not laid out yet: leave the marker alone
            underline = tabs.query_one(Underline)
            underline.highlight_start = start
            underline.highlight_end = end

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
        """Ctrl+W: close-DETACH the active tab -- its daemon keeps running,
        by design (reattach via the palette's attach picker or `doxa
        attach`). The cheapest outcome to recover from is what a close key
        does; ENDING a session is Ctrl+Q, which says so.

        Closing the last tab closes the app, on the same detach semantics."""
        pane = self.active_pane
        if pane is not None:
            await self._close_pane(pane, terminate=False)

    def action_end_session(self) -> None:
        """Ctrl+Q: END this tab's session -- finalize NOW (LORE review +
        index run daemon-side), socket closed, presence file removed, the
        daemon child reaped -- and close the tab. Nothing survives but the
        transcript.

        Tab-scoped, never app-scoped: quitting the whole window is Ctrl+C
        (twice). A turn IN FLIGHT is the one case this refuses to decide by
        itself -- killing work silently is not a thing a keystroke should
        do -- so it asks; an idle session ends without a prompt.

        Dispatched into a worker because awaiting a modal's answer
        (push_screen_wait) is only legal from one."""
        self.run_worker(self._end_session(), group="close")

    async def _end_session(self) -> None:
        pane = self.active_pane
        if pane is None:
            return
        if pane.turn_in_flight:
            choice = await self.push_screen_wait(CloseWithTurnRunning())
            if choice == "cancel":
                return
            if choice == "detach":
                await self._close_pane(pane, terminate=False)
                return
        await self._close_pane(pane, terminate=True)

    async def action_detach_tab(self) -> None:
        """`/detach` -- the named form of what Ctrl+W does."""
        await self.action_close_tab()

    async def _close_pane(self, pane: "SessionPane", terminate: bool) -> None:
        """One close path, two dispositions. Closing the LAST tab closes the
        app on the same disposition -- a window with no tabs is not a
        window, and the session's fate must not depend on tab arithmetic."""
        if terminate:
            await pane.stop()
        else:
            # Detached ON PURPOSE: it is no longer this window's to end, so
            # a later quit-stop leaves it running.
            pane.detached_on_purpose = True
            await pane.detach()
        if len(self.panes()) == 1:
            await App.action_quit(self)
            return
        await self.query_one("#session-tabs", TabbedContent).remove_pane(
            pane.id or ""
        )

    def _cycle_tab(self, delta: int) -> None:
        """Ctrl+← / Ctrl+→ -- move to the neighbouring tab, wrapping. One
        tab wraps to itself, which is the correct no-op."""
        panes = self.panes()
        if len(panes) < 2:
            return
        tabbed = self.query_one("#session-tabs", TabbedContent)
        ids = [p.id for p in panes if p.id]
        try:
            index = ids.index(tabbed.active)
        except ValueError:
            index = 0
        tabbed.active = ids[(index + delta) % len(ids)]
        # Textual's reactive watcher has already moved the `-active` class
        # by the time that assignment returns, so the marker can be placed
        # NOW -- one message-pump turn earlier than TabActivated arrives.
        # Held Ctrl+←/→ is the case this exists for.
        self._jump_tab_marker()

    def action_prev_tab(self) -> None:
        self._cycle_tab(-1)

    def action_next_tab(self) -> None:
        self._cycle_tab(1)

    def _switch_to_tab(self, pane_id: str) -> None:
        with contextlib.suppress(Exception):
            self.query_one("#session-tabs", TabbedContent).active = pane_id
        self._jump_tab_marker()

    # -- palette (Ctrl+P) --------------------------------------------

    def doxa_commands(self) -> "list[PaletteEntry]":
        """The DOXA palette surface, as :class:`~doxa.palette.PaletteEntry`
        rows in display order.

        Rebuilt from live state on EVERY palette open (that is what the
        provider calls), so a tab opened or closed while the palette is up
        cannot leave a stale row behind.

        The order is the one doxa/palette.py documents: New tab, then the
        open tabs in tab-bar order, then the commands in the registry's own
        groups, then the attachable sessions. App-level entries that have
        no registry row (Close tab, the quits, the inspector) declare a
        registry GROUP like everything else -- there is one grouping in
        this app, not one per surface."""
        entries: list[PaletteEntry] = [
            PaletteEntry(
                palette_mod.SECTION_NEW,
                "New tab",
                "Open a fresh DOXA session in this repo scope in a new tab (ctrl+t)",
                self._cmd_new_tab,
            ),
        ]
        # Open tabs, LEFT TO RIGHT -- the palette mirrors the tab bar, so
        # the order the user sees along the top is the order they get here.
        # The active tab is marked rather than hidden: "where am I" is as
        # much a question as "where do I want to go".
        active = self.active_pane
        for position, pane in enumerate(self.panes()):
            if not pane.id:
                continue
            sid = str(getattr(pane.engine, "session_id", "") or "")[:8]
            is_active = pane is active
            entries.append(PaletteEntry(
                palette_mod.SECTION_TABS,
                f"{pane._title}" + (f"  ({sid})" if sid else "")
                + ("  · active" if is_active else ""),
                "This tab (already active)" if is_active
                else "Switch to this tab",
                partial(self._switch_to_tab, pane.id),
                sort_key=(position, ""),
            ))
        # App-level entries: no slash row of their own, but the SAME
        # registry groups -- they sort after the registry's rows inside a
        # group (sort_key (1, label) vs the registry's (0, name)).
        for group, label, help_text, callback in (
            ("Panes & tabs", "Close tab",
             "Close-detach the current tab; its session keeps running (ctrl+w)",
             self._cmd_close_tab),
            ("Session", "New session",
             "Start a fresh DOXA session and switch THIS tab to it",
             self._cmd_new_session),
            ("Panes & tabs", "Belief inspector: toggle",
             "Show/hide the belief inspector pane (stub until Phase 3)",
             self.action_toggle_inspector),
            ("Session", "Quit: detach",
             "Close this TUI; every session daemon keeps running "
             "(reattach with `doxa attach`)",
             self.action_quit),
            ("Session", "Quit: stop session",
             "Finalize the current tab's session now (LORE review + index) "
             "and close its tab",
             self._cmd_stop_active),
        ):
            entries.append(PaletteEntry(group, label, help_text, callback))
        # Slash registry, second surface: every row that declares a palette
        # label appears here too (doxa/commands.py is the single list --
        # the prompt's autocomplete reads the same rows), keeping
        # commands.ordered()'s sequence inside its group. Rows that need
        # arguments PREFILL the prompt instead of running blind.
        for index, command in enumerate(commands_mod.ordered()):
            if not command.palette:
                continue
            callback = (
                partial(self._cmd_prefill, command.name + " ")
                if command.palette_prefill
                else partial(self._cmd_run_slash, command.name)
            )
            entries.append(PaletteEntry(
                command.group, command.palette, command.summary, callback,
                sort_key=(0, f"{index:03d}"),
            ))
        # Attach: live daemon-hosted sessions from the shared peer/daemon
        # registry, newest first, never any session already open in a tab.
        open_ids = {
            str(getattr(p.engine, "session_id", "") or "") for p in self.panes()
        }
        for position, entry in enumerate(peers_mod.list_daemons()):
            if entry.session_id in open_ids:
                continue
            entries.append(PaletteEntry(
                palette_mod.SECTION_ATTACH,
                f"Attach: {entry.title} ({entry.session_id[:8]})",
                f"Reattach to the live session in {entry.cwd} (in this tab)",
                partial(self._cmd_attach, entry),
                sort_key=(position, ""),
            ))
        return palette_mod.ordered_entries(entries)

    def action_command_palette(self) -> None:
        """Ctrl+P -- DOXA's palette screen, which is Textual's plus the
        section headers (doxa/palette.py). Overridden rather than
        configured because Textual's App pushes its own CommandPalette
        class by name."""
        from textual.command import CommandPalette

        if self.use_command_palette and not CommandPalette.is_open(self):
            self.push_screen(palette_mod.DoxaPalette(id="--command-palette"))

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
        """Ctrl+R: prefill ``/search `` in the active tab's prompt, which
        IS the search surface (doxa/history.py's popup opens on that exact
        prefix). The modal overlay this used to push is gone: one key, one
        slash command and one palette entry now land on the same place, so
        there is nothing left for two search paths to disagree about."""
        self._cmd_prefill(SEARCH_PREFIX)

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
            with contextlib.suppress(Exception):
                self.query_one(ClockChip).reconfigure()

        engine = self.engine
        self.push_screen(
            SettingsScreen(
                session_model=getattr(engine, "model", None),
                account=getattr(engine, "account", None) or {},
            ),
            callback=_saved,
        )

    def action_setup(self) -> None:
        """/setup / the palette's Setup entry -- check state, fix findings
        one at a time. Also what a genuine first launch auto-triggers (see
        on_mount): the marker that stops it recurring is consumed there,
        not here, so this method itself is identical whether it was
        summoned on demand or by the app."""
        from .setup import ACTION_OPEN_SETTINGS, SetupScreen

        def _done(result: "str | None") -> None:
            config_mod.invalidate()
            for pane in self.panes():
                pane._refresh_status()
            if result == ACTION_OPEN_SETTINGS:
                self.action_settings()

        self.push_screen(SetupScreen(), callback=_done)

    async def on_mount(self) -> None:
        """Auto-run /setup exactly once: a genuine first launch on this
        machine (doxa.setup.needs_first_run), never again after. The
        marker is written the moment this fires -- declining or Esc-ing
        out of the wizard must not make it reappear at the next launch;
        /setup still runs on demand any time."""
        from . import setup as setup_mod

        if setup_mod.needs_first_run():
            setup_mod.mark_seen()
            self.call_after_refresh(self.action_setup)

    def action_toggle_inspector(self) -> None:
        """Belief-inspector stub: Phase 3 owns the real pane (live STEER/
        CITE split, evidence trails); Phase 2 reserves the toggle, the dock
        and the count so the palette command and the muscle memory exist."""
        panel = self.query_one("#belief-inspector", BeliefInspector)
        if panel.display:
            panel.display = False
            return
        beliefs = self.engine.belief_count() if self.engine is not None else 0
        panel.set_text(
            f"{beliefs} active beliefs in the store.\n\n"
            "Phase 3 renders them here: STEER/CITE split,\n"
            "evidence trails, calibration. Until then use\n"
            "the lore_belief_search / lore_belief_show tools."
        )
        panel.display = True

    @on(events.Click, "#inspector-close")
    def _on_inspector_close(self, event: events.Click) -> None:
        """The ✕ is a real target for the mouse the key toggle leaves out."""
        event.stop()
        self.query_one("#belief-inspector", BeliefInspector).display = False

    @on(Collapsible.Expanded)
    def _on_chip_expanded(self, event: Collapsible.Expanded) -> None:
        if isinstance(event.collapsible, ToolChip):
            event.collapsible.format_body()

    # -- quit semantics (app-level, all tabs) ------------------------

    async def action_ctrl_c_quit(self) -> None:
        """Ctrl+C (priority binding), APP-level by design -- a reflex
        keystroke gets the cheapest-to-recover outcome across every tab.
        First press: arm the double-press window, then quit-DETACH when it
        expires -- every daemon-hosted session keeps running; in-process
        engines finalize right there, so Ctrl+C always exits cleanly.
        Second press inside the window: quit-STOP every tab's session
        (finalize NOW, daemons included). Per-tab ending lives on Ctrl+Q
        and the palette's 'Quit: stop session', where the choice is
        deliberate rather than reflexive."""
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
        there); in-process it is plain finalize-and-quit.

        A pane the user DETACHED on purpose is not stopped: detaching is
        the explicit "keep this running" gesture, and a later quit must not
        quietly undo it. Those sessions outlive the window, which is what
        /sessions exists to show and reap."""
        for pane in self.panes():
            if pane.detached_on_purpose:
                await pane.detach()
            else:
                await pane.stop()
        await App.action_quit(self)

    async def action_quit(self) -> None:
        """palette 'Quit: detach' (and the Ctrl+C window's expiry) -- ALL
        tabs. Over a daemon client, finalize() only DETACHES: the daemon
        lingers and runs the session-end review + index itself once the
        last client has been gone for the linger window (or on `doxa
        stop`). In-process (Phase 1 shape), finalize() still runs the
        review + index right here, host-driven (PHASE0 redesign item 1: no
        SessionEnd hook exists)."""
        for pane in self.panes():
            await pane.detach()
        await App.action_quit(self)


def main() -> None:
    DoxaApp().run()


if __name__ == "__main__":
    main()

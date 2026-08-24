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

Each turn is a foldable Collapsible; its response streams as markdown
(Markdown.get_stream -- textual 5's append-only path for LLM deltas, no
full re-parse per chunk). Tool calls inside a turn render as compact
chips (name + one-line arg summary + duration + a check or cross) that
lazily expand into full args/result on first click -- the expensive JSON
pretty-printing only happens once, on demand, not for every tool call
that streams past -- and compact further behind ONE per-turn "Tool calls
(N)" fold (ToolCallsSection), created lazily on the first call.

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
    Markdown,
    OptionList,
    Static,
    TabbedContent,
    TabPane,
    TextArea,
)
from textual.widgets.markdown import MarkdownStream
from textual.widgets.option_list import Option

from . import auth as auth_mod
from . import clock as clock_mod
from . import commands as commands_mod
from . import config as config_mod
from . import identity as identity_mod
from . import images as images_mod
from . import naming as naming_mod
from . import notify as notify_mod
from . import paste as paste_mod
from . import peers as peers_mod
from . import providers as providers_mod
from . import version as version_mod
from . import worktrees as worktrees_mod
from .engine import EngineEvent, SessionEngine
from . import history as history_mod
from .history import SEARCH_PREFIX, SessionSearch
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
# an alias that always resolves to the current one. Status-chips (item Y):
# this now POINTS AT doxa.providers.FALLBACK_MODEL_ALIASES rather than
# keeping its own copy -- the model picker's static-fallback tier reads
# the same tuple, so there is one list, not two that happen to agree today
# (see that module's docstring for the full resolution order and the
# empirical finding on why the live Models-API tier is unreachable here).
MODEL_ALIASES = providers_mod.FALLBACK_MODEL_ALIASES

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
#
# The 34 is the budget for THIS text -- what compose_tab_label returns and
# what auto_label/display_name carry around as the tab's identity (seeding
# the rename field, sorting the palette, and so on). What actually paints
# onto the tab header adds a 2-cell prefix on top (the provider glyph plus
# its separating space, set in SessionPane.set_tab_label) that is NOT part
# of that identity string, so TAB_LABEL_MAX is trimmed to 32 here -- glyph
# + space + 32 lands back on the original 34-column, four-tabs-at-80
# budget instead of quietly blowing past it.
TAB_LABEL_MAX = 32
TAB_MODEL_MIN = 4
TAB_REPO_MIN = 6

# Item S: a worktree-isolated session earns one more character saying so --
# the SAME glyph render() already uses between repo and branch, trailing
# the tab's branch half instead. It is appended AFTER the trim algorithm
# above has already run, and ONLY when there is a free character left at
# TAB_LABEL_MAX -- the base branch itself never gives up a character to
# make room for it, and a label already at the limit just goes without.
TAB_ISOLATION_MARKER = "⎇"

# Provider identity: one glyph, prepended to every tab label ahead of the
# model tier. Multi-provider engines (a second SessionPane driving a
# non-Claude CLI) are planned but not shipped -- every model DOXA drives
# today is Claude/Anthropic's (see MODEL_ALIASES) -- so this table has
# exactly one row. A future provider is a second row here, not a branch in
# the display logic.
PROVIDER_GLYPHS: dict[str, str] = {"claude": "✳"}
PROVIDER_GLYPH_COLOR = "#D97757"  # Claude/Anthropic orange -- theme.tcss's own

# Status-chips (item Y): the SAME accent, under its own name -- not a new
# color, the one this house already uses for every interactive/highlighted
# span (the active tab, palette matches, #slash-complete's highlighted
# row -- see theme.tcss). Chips that OPEN something (the model chip, the
# git chip's branch span, the new picker's rows) wear it; chips that are
# just information (cost, repo name, sha, usage headroom) do not.
CLICKABLE_CHIP_ACCENT = PROVIDER_GLYPH_COLOR


def provider_glyph(provider: str = "claude", *, colored: bool = True) -> str:
    """The provider glyph for `provider`, Claude-orange via Textual markup
    when `colored`. Defaults to "claude" because that is the only engine
    DOXA drives today; an unrecognised provider (should never happen while
    there is one row) degrades to no glyph rather than a broken label.

    Textual 5's Tab renders its label through ``Content.from_markup`` by
    default (confirmed empirically: ``Tab("[#D97757]x[/] y").label.spans``
    carries the color span, and ``.plain`` strips the markup cleanly) --
    so the color tag below is real color, not a literal bracket leaking
    into the tab bar."""
    glyph = PROVIDER_GLYPHS.get(provider, "")
    if not glyph:
        return ""
    return f"[{PROVIDER_GLYPH_COLOR}]{glyph}[/]" if colored else glyph


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
    *,
    isolated: bool = False,
) -> str:
    """`Model@repo:branch`, trimmed model-first when it must be.

    Outside a repo there is no branch and therefore NO colon: a dangling
    separator is a label saying "something is missing here", which is worse
    than the shorter label it replaced.

    ``isolated=True`` (item S: a worktree-per-session session, ``branch``
    then being the BASE the worktree forked from -- see GitLine.tab_branch)
    appends :data:`TAB_ISOLATION_MARKER` when -- and only when -- there is
    a free character left at ``limit`` after everything else has already
    been fit; never at the cost of shrinking the branch further to make
    room for it."""

    def build(m: str, r: str, b: "str | None") -> str:
        return f"{m}@{r}" + (f":{b}" if b else "")

    model_s, repo_s = model, repo
    text = build(model_s, repo_s, branch)
    for segment, floor in (("model", TAB_MODEL_MIN), ("repo", TAB_REPO_MIN)):
        overflow = len(text) - limit
        if overflow <= 0:
            break
        current = model_s if segment == "model" else repo_s
        room = len(current) - floor
        if room > 0:
            shrunk = _shrink(current, len(current) - min(room, overflow))
            if segment == "model":
                model_s = shrunk
            else:
                repo_s = shrunk
        text = build(model_s, repo_s, branch)
    # Only now, with the model and the repo already at their floors, does
    # the branch give ground.
    text = ellipsize(text, limit)
    if isolated and branch and len(text) < limit:
        text += TAB_ISOLATION_MARKER
    return text


def ellipsize(text: str, limit: int = TAB_LABEL_MAX) -> str:
    """Truncate with a real ellipsis. A tab that grows without bound pushes
    its neighbours off the bar, which costs more than the tail of a branch
    name is worth."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


# Subagent tracker (queue item 4): the second status row and the transcript
# tabs it opens both need to write onto a #session-tabs Tab header that is
# NOT their own widget's tab (SubagentLine lives inside a SessionPane but
# targets whichever Tab a click named; SubagentTranscriptTab is a plain
# TabPane, no engine, that still carries -done-unseen like any other tab).
# These two module functions are the shared write path: SessionPane's own
# set_tab_label/_set_tab_class keep their existing names (set_tab_label
# does a little more -- it also prepends the provider glyph and keeps
# self._title in sync) but _set_tab_class calls straight through to
# _write_tab_class below, and SubagentTranscriptTab uses both directly.
def _write_tab_label(app: Any, tab_id: str, text: str) -> None:
    """Write `text` straight onto one Tab's label -- no provider glyph.
    SessionPane.set_tab_label prepends one deliberately (every SESSION is
    Claude/Anthropic's); a subagent transcript tab is not a session, so it
    never goes through that path at all -- this is its own, glyph-free,
    door onto the same tab strip."""
    with contextlib.suppress(Exception):
        tabbed = app.query_one("#session-tabs", TabbedContent)
        tabbed.get_tab(tab_id).label = text


def _write_tab_class(app: Any, tab_id: str, class_name: str, value: bool) -> None:
    """Toggle one status class on one #session-tabs Tab header. Shared by
    SessionPane (-working/-done-unseen/-attention) and
    SubagentTranscriptTab (-done-unseen only) -- same contextlib.suppress
    discipline either caller needs: the tab may not exist yet this early,
    or may already be mid-teardown."""
    with contextlib.suppress(Exception):
        tabbed = app.query_one("#session-tabs", TabbedContent)
        tabbed.get_tab(tab_id).set_class(value, class_name)


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


def _subagent_label(chip: "ToolChip") -> str:
    """The running label for one Task-spawned subagent: its own
    `description` input field (the model's own name for the subtask),
    collapsed to one line and ellipsized to ~24 cells -- short enough that
    a handful of concurrently running subagents still fit one status row.
    Falls back to the tool name in the (should-never-happen) case of a
    Task call with no description at all."""
    description = str(chip.tool_input.get("description") or "").strip()
    return ellipsize(_one_line(description, 200) or chip.tool_name, 24)


def _needs_input_summary(data: dict) -> str:
    """The notification body for one needs_input event: the first
    question's own text for an ask_user request (already scrubbed --
    doxa.engine's own choke point, see _scrub_json in _ask_user_question),
    the tool's input summary for a permission request (same, via
    _permission_summary). Never the full payload -- a desktop banner is a
    headline, the popup itself is where the detail lives."""
    if data.get("kind") == "ask_user":
        questions = data.get("questions") or []
        if questions and isinstance(questions[0], dict):
            return str(questions[0].get("question") or "question")
        return "question"
    return str(data.get("input_summary") or data.get("tool_name") or "")


def _escape_markup(text: str) -> str:
    """Rich markup escape for text interpolated INTO a markup string this
    app builds itself (as opposed to the display-only chip/tab titles
    elsewhere, which are cosmetic and already tolerate a stray bracket) --
    the subagent line embeds a `[@click=...]` action span per label, so an
    unescaped `[` in a model-chosen description must not be able to swallow
    or corrupt the click target that follows it."""
    return text.replace("[", "\\[")


def _chip_span(text: str, action: str) -> str:
    """A whole status-bar chip as a clickable, accent-colored span --
    `[@click=<action>][accent]text[/][/]` -- for the two tiers that get an
    affordance at all (SELECTORS: model, branch, effort; ACTIONABLE: peers,
    ctx%, the session handle). `action` names a zero-argument action
    method on the clicked widget (StatusBar) -- see that class's own
    docstring for why a dedicated action per chip, rather than one
    generic dispatcher taking an argument, was the simpler choice here."""
    return f"[@click={action}][{CLICKABLE_CHIP_ACCENT}]{_escape_markup(text)}[/][/]"


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
        self._cwd = cwd
        self.repo_root = peers_mod.repo_root_of(cwd)
        self.repo: str | None = None
        self._head: Path | None = None
        self._gitdir: Path | None = None
        self._mtime: float | None = None
        self._branch: str | None = None
        self._ref: str | None = None      # refs/heads/<branch>, when attached
        self._sha: str | None = None
        self._sha_mtime: float | None = None
        self.worktree: str | None = None
        # The gitdir that holds refs/heads/<branch> and packed-refs -- see
        # _read_sha's docstring for why this is NOT always self._gitdir.
        self._commondir: Path | None = None
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
            self._commondir = self._resolve_commondir(git)
        # The repo NAME is always the MAIN checkout's, never a linked
        # worktree's own directory -- since v0.17 (worktree-per-session)
        # every session's cwd IS such a worktree, and `Path(repo_root).name`
        # there reads `doxa-<shortid>`, printing the session id twice
        # alongside the `doxa/<shortid>` branch chip beside it (reported).
        # self._commondir already resolves THROUGH the worktree's commondir
        # pointer for the sha read above -- its parent is the main repo root
        # in every case, worktree or not, and reusing it costs no extra
        # subprocess (pure filesystem reads, same "one subprocess total"
        # discipline this class already documents). Anything where that
        # resolution doesn't land on a plain ".git" (a submodule, a bare
        # repo) falls back to the worktree-root name, same as before.
        if self._commondir is not None and self._commondir.name == ".git":
            self.repo = self._commondir.parent.name
        elif self.repo_root:
            self.repo = Path(self.repo_root).name
        # Item S / the tab-label regression it surfaced: the worktree
        # sidecar's own base_ref (see doxa.worktrees), mtime-guarded the
        # SAME way HEAD/the ref file above are -- a live `/branch` switch
        # rewrites this file (worktrees.update_base), and the next event-
        # driven render sees it with no polling and no reconstructing this
        # GitLine. None outside a worktree-per-session session (no
        # sidecar): callers fall back to branch_label().
        self._base_meta_path = worktrees_mod.meta_file_path(cwd)
        self._base_mtime: float | None = None
        self._base_ref_cached: str | None = None

    @staticmethod
    def _resolve_commondir(gitdir: Path) -> Path:
        """A linked worktree's private gitdir (``<main>/.git/worktrees/
        <name>``) holds only its own HEAD, index and logs -- refs/heads/*
        and packed-refs are SHARED, and live under the ``commondir`` file's
        target (ordinarily ``../..``, i.e. the main repo's ``.git``). A
        normal (non-worktree) repo has no ``commondir`` file and its
        gitdir already IS the common one, so this returns it unchanged."""
        commondir_file = gitdir / "commondir"
        try:
            text = commondir_file.read_text(
                encoding="utf-8", errors="replace"
            ).strip()
        except OSError:
            return gitdir
        if not text:
            return gitdir
        common = Path(text)
        if not common.is_absolute():
            common = (gitdir / common).resolve()
        return common

    def render(self, *, clickable: bool = False) -> str | None:
        """`repo ⎇ branch sha`, or None outside a repo (no chip at all).

        The branch half is :meth:`branch_label` -- the SAME string a tab
        shows -- so a linked worktree reads `repo ⎇ main@featureX @sha`
        here too: one source of truth for "how does a worktree spell its
        branch", inherited rather than re-derived.

        The short sha sits immediately right of the branch, because that is
        where "which commit am I actually on" belongs -- next to the branch
        it qualifies, not at the far end of the bar. Omitted when it would
        merely repeat the branch label (detached HEAD).

        `clickable` (status-chips, item Y): wraps ONLY the branch segment
        in the click-action span that opens the branch picker -- the repo
        name and the sha are information, not selectors, and stay plain
        even here (see the operator's three-tier clickability answer in
        the release notes). Default False keeps every other caller (the
        identity block's `/about`-style dump, every pre-chips test that
        asserts this string verbatim) exactly as it was; only
        `SessionPane._refresh_status` passes True."""
        if not self.repo:
            return None
        branch = self.branch_label()
        if not branch:
            return self.repo
        branch_text = _chip_span(branch, "open_branch_picker") if clickable else branch
        chip = f"{self.repo} {git_branch_symbol()} {branch_text}"
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
        """The branch actually checked out HERE: `main`, or `main@featureX`
        inside a linked worktree. SESSION identity -- what render() (the
        status bar chip) and /about show, exactly what git has HEAD
        pointed at right now. NOT what a tab shows; see :meth:`tab_branch`
        for that (item S's fix for the v0.17 regression where the tab
        label started showing this SAME string -- `doxa/f13526d4` -- which
        is the session's own throwaway branch, not the base the operator
        is orienting by, and which repeats the session id already visible
        elsewhere).

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

    def base_branch(self) -> str | None:
        """The worktree sidecar's recorded ``base_ref`` (doxa.worktrees),
        re-read on the SAME mtime-guard discipline as HEAD/the ref file
        above -- ``None`` outside a worktree-per-session session (no
        sidecar at all), in which case :meth:`tab_branch` falls back to
        :meth:`branch_label`, the checked-out branch, exactly as it always
        was before v0.17."""
        try:
            mtime = self._base_meta_path.stat().st_mtime
        except OSError:
            return self._base_ref_cached
        if mtime == self._base_mtime:
            return self._base_ref_cached
        self._base_mtime = mtime
        meta = worktrees_mod.read_meta(self._cwd)
        self._base_ref_cached = str(meta.get("base_ref") or "") or None if meta else None
        return self._base_ref_cached

    def tab_branch(self) -> "tuple[str | None, bool]":
        """What the TAB label's branch half actually shows, and whether
        this is a worktree-isolated session (the caller uses the second
        value to decide whether compose_tab_label's isolation marker
        earns its keep) -- item S / the v0.17 tab-label regression.

        Orientation, not identity: a worktree session's tab says `main`
        (what it is WORKING OFF), not `doxa/f13526d4` (branch_label's
        session handle, which the status bar keeps -- see that method's
        docstring). Falls back to branch_label() with no worktree sidecar,
        so worktree_per_session OFF (or a cwd that was never a doxa
        worktree) reads exactly as it did before this feature: the
        checked-out branch IS the base there."""
        base = self.base_branch()
        if base:
            return base, True
        return self.branch_label(), False

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
        driven (a stat per status refresh), still never polled.

        Reads from ``self._commondir``, NOT ``self._gitdir``: inside a
        linked worktree the checked-out branch's ref file lives in the
        MAIN repo's ``refs/heads/``, shared via the worktree's ``commondir``
        pointer (see ``_resolve_commondir``) -- the worktree's own private
        gitdir never has it, which is why this used to come back None for
        every worktree session (pinned, then fixed, in
        tests/test_statusline.py)."""
        if self._commondir is None or self._ref is None:
            return self._sha
        ref_path = self._commondir / self._ref
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
        packed-refs and has no loose ref file. Same mtime discipline, and
        the same commondir redirection ``_read_sha`` needs -- packed-refs
        is shared across a repo's worktrees exactly like refs/heads/*."""
        if self._commondir is None or self._ref is None:
            return self._sha
        packed = self._commondir / "packed-refs"
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
    command results, peer-layer errors. Same ▎ accent as turns; v0.13.0's
    restyle carries the role in the background tint instead of a border --
    the dimmer step on the surface ramp, one below the screen, with muted
    text (.system-block in the theme)."""

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


async def _clone_chip(chip: "ToolChip") -> "ToolChip":
    """A read-only COPY of one ToolChip's current state -- name, input,
    result, error, duration, image, and (recursively) its own already-
    mounted subcalls -- for a subagent transcript tab's mirror tree
    (SubagentTranscriptTab, below). Never the live widget itself: a
    widget has exactly one parent, and the original stays exactly where
    the trace tree put it (inside its Task chip's ``subcalls``) whether or
    not anyone ever opens a transcript tab to look at a copy of it."""
    mirror = ToolChip(chip.call_id, chip.tool_name, chip.tool_input)
    if chip.tool_result is not None:
        mirror.update_result(
            chip.tool_result, chip.is_error, chip.duration_ms,
            image_path=chip.tool_image_path,
        )
    for sub in list(chip.subcalls.children):
        if isinstance(sub, ToolChip):
            await mirror.subcalls.mount(await _clone_chip(sub))
    return mirror


class ToolCallsSection(Collapsible):
    """The turn's own top-level tool chips, compacted behind ONE fold:
    "Tool calls (N)", collapsed by default. Created lazily -- on the
    FIRST top-level tool_call event of a turn (see TurnBlock.add_chip) --
    so a turn with no tool calls grows no section at all (hide-at-zero,
    same convention as the git/usage/peers/disabled-tools status chips).

    N updates live as chips mount mid-turn: a title rewrite only, as
    cheap as ToolChip's own title update on every result -- no repaint
    storm (see this app's idle-CPU comments elsewhere: ThinkingMarker,
    the leaked-timer fix in TurnBlock.hide_thinking). If the user expands
    this section mid-turn it STAYS expanded as further chips arrive --
    nothing here ever writes ``self.collapsed`` itself, so only a click
    (or a test poking the reactive directly) can change it; it never
    auto-collapses out from under the cursor.

    Chrome, not model content: this wraps chips, it does not replace
    them -- each chip inside keeps its own ToolChip fold, args/result
    formatting, and (for a Task call) its own nested subcalls tree,
    unchanged by living inside this wrapper."""

    def __init__(self) -> None:
        self.count = 0
        self.chips = Vertical(classes="tool-calls-list")
        super().__init__(self.chips, title=self._render_title(), collapsed=True)

    def _render_title(self) -> str:
        return f"⚒ Tool calls ({self.count})"

    async def add_chip(self, chip: "ToolChip") -> None:
        self.count += 1
        self.title = self._render_title()
        await self.chips.mount(chip)


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
    """One user turn + the assistant's response, foldable. The user's
    prompt lives in the fold header (title) only -- it is never re-set
    after construction, so it stays literal plain text no matter what the
    response below does (typed text must not reflow). The response body
    streams as MARKDOWN: ``self.body`` is a ``Markdown`` widget fed
    through ``Markdown.get_stream`` (textual 5's append-only streaming
    path, built for exactly this -- LLM deltas arriving chunk by chunk),
    so tables/bold/fences/inline code render as they complete without a
    full-document re-parse on every ``text_delta``. Top-level tool chips
    compact into ``self.tool_section`` (a ``ToolCallsSection``, created
    lazily on the first one -- see its own docstring); a Task call's
    subagent chips still nest inside THAT chip's own ``subcalls``,
    unaffected by any of this."""

    def __init__(self, prompt: str) -> None:
        self.prompt_text = prompt
        self.assistant_text = ""
        self.thinking = ThinkingMarker()
        self.body = Markdown("", classes="turn-body")
        self._stream: MarkdownStream | None = None
        self.tools = Vertical(classes="turn-tools")
        self.tool_section: ToolCallsSection | None = None
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

    async def append_text(self, chunk: str) -> None:
        self.hide_thinking()
        self.assistant_text += chunk
        # Lazy, like everything else in this pane: the stream (and its one
        # background asyncio task -- an event-driven coroutine, NOT a
        # Textual auto-refresh timer) is only created once a turn actually
        # has text to show, and mark_done() below stops it the moment the
        # turn finishes, so nothing outlives the turn it belongs to.
        if self._stream is None:
            self._stream = Markdown.get_stream(self.body)
        await self._stream.write(chunk)

    async def add_tool_chip(self, chip: "ToolChip") -> None:
        """Mount one top-level tool chip (no ``parent_id`` -- a subagent's
        own calls nest inside its Task chip instead, see ToolChip's
        docstring) into this turn's ONE ``ToolCallsSection``, created on
        first use so a turn with no tool calls grows no section at all."""
        if self.tool_section is None:
            self.tool_section = ToolCallsSection()
            await self.tools.mount(self.tool_section)
        await self.tool_section.add_chip(chip)

    async def mark_done(
        self,
        cost_usd: float | None,
        duration_ms: int | None,
        is_error: bool,
        tier: str | None = None,
    ) -> None:
        """``tier`` (item T): the SAME subscription-vs-API rule the status
        bar and /usage already apply to the session TOTAL applies here to
        the per-turn figure too -- a bare ``$`` on subscription auth reads
        as a real per-turn bill next to a status bar that just said this
        account pays no dollars. ``None`` (API-key auth, or no account info
        yet) keeps the plain ``$`` figure unchanged."""
        self.hide_thinking()
        if self._stream is not None:
            # Stops the stream's one background task and flushes anything
            # still buffered -- a finished turn must not leave a live
            # asyncio task behind any more than it may leave a timer.
            await self._stream.stop()
        bits = []
        if duration_ms is not None:
            bits.append(f"{duration_ms}ms")
        if cost_usd is not None:
            bits.append(f"≈${cost_usd:.4f} if API" if tier else f"${cost_usd:.4f}")
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


# Number-key selection for the needs-input popup, 1-9 (Textual reports a
# bare digit keystroke's own key name as the digit itself).
_NEEDS_INPUT_DIGIT_KEYS = frozenset("123456789")


class NeedsInputPopup(OptionList):
    """The needs-input dialog (queue item 5): same mount position, same
    "never takes focus" discipline as SlashComplete/SessionSearch -- above
    the prompt, driven entirely through :class:`PromptInput`'s key
    protocol. It serves BOTH interactive cases ``doxa.engine``'s
    ``needs_input`` event carries: an ``AskUserQuestion`` (one or more
    questions, answered one at a time -- multi-select collapses to the
    single highlighted/numbered choice; a model asking for more than one
    pick per question is rare enough that the SDK's own comma-joined-
    answer convention degrades gracefully to "just that one") and a plain
    permission request (tool name + input summary, Allow/Deny).

    Row 0 is always a disabled heading (the question text, or the
    tool+summary) -- same "label, never a destination" convention
    :meth:`SlashComplete._first_command_row` established; real choices
    start at row 1, numbered from 1 in the label text itself so the
    number keys this widget answers to are visible, not just implied.

    Esc always DECLINES the WHOLE request -- politely, a graceful
    ``PermissionResultDeny``, never a silent hang -- per the SDK's own
    contract for a tool call nobody answered. There is no plain "cancel
    and leave the agent waiting" gesture: the agent really is waiting."""

    can_focus = False

    def __init__(self) -> None:
        super().__init__(id="needs-input-popup")
        self.display = False
        self.request_id: str | None = None
        self.kind: str | None = None
        # ask_user: the remaining questions to walk through, and the
        # answers collected so far (question text -> chosen label).
        # permission: neither is used -- one step, Allow/Deny.
        self._questions: list[dict] = []
        self._answers: dict[str, str] = {}
        self._decision: str | None = None
        self._rows: list[dict] = []  # rows for the CURRENT step

    @property
    def is_open(self) -> bool:
        return bool(self.display)

    def ask(self, data: dict) -> None:
        """Open on a fresh needs_input event's data (see doxa/engine.py's
        _ask_user_question/_request_permission for the exact shape)."""
        self.request_id = data.get("id")
        self.kind = data.get("kind")
        self._answers = {}
        self._decision = None
        if self.kind == "ask_user":
            self._questions = list(data.get("questions") or [])
            self._show_next_question()
        else:
            self._questions = []
            heading = str(data.get("title") or data.get("tool_name") or "permission request")
            summary = str(data.get("input_summary") or "")
            if summary and summary != heading:
                heading = f"{heading} — {summary}"
            self._rows = [{"label": "Allow"}, {"label": "Deny"}]
            self._render(heading, self._rows)

    def _show_next_question(self) -> None:
        if not self._questions:
            self.close()
            return
        question = self._questions[0]
        self._rows = list(question.get("options") or [])
        heading = str(question.get("header") or question.get("question") or "")
        self._render(heading, self._rows)

    def _render(self, heading: str, options: list[dict]) -> None:
        self.clear_options()
        self.add_option(Option(heading, disabled=True))
        for index, opt in enumerate(options, start=1):
            label = str(opt.get("label") or "")
            description = str(opt.get("description") or "")
            text = f"  {index}. {label}" + (f" — {description}" if description else "")
            self.add_option(Option(text))
        self.highlighted = 1 if options else 0
        self.display = True

    def move(self, delta: int) -> None:
        """Arrow navigation, skipping row 0 (the heading) -- same
        convention SlashComplete.move follows for its own group headers."""
        if not self._rows:
            return
        index = (self.highlighted if self.highlighted is not None else 1) - 1
        index = (index + delta) % len(self._rows)
        self.highlighted = index + 1

    def choose_index(self, index: int) -> bool:
        """0-based option index (the heading row never reaches here).
        Returns True once THIS request is fully answered -- the caller
        (SessionPane) then reads :meth:`answer_payload` and calls
        :meth:`close`. False means an ask_user request with more
        questions still to go; this re-rendered itself in place and stays
        open."""
        if not (0 <= index < len(self._rows)):
            return False
        label = str(self._rows[index].get("label") or "")
        if self.kind == "ask_user":
            question = self._questions.pop(0)
            self._answers[str(question.get("question") or "")] = label
            if self._questions:
                self._show_next_question()
                return False
            return True
        self._decision = "allow" if index == 0 else "deny"
        return True

    def choose_highlighted(self) -> bool:
        return self.choose_index((self.highlighted if self.highlighted is not None else 1) - 1)

    def answer_payload(self) -> dict:
        """What ``engine.answer_needs_input`` wants -- call once, right
        after :meth:`choose_index`/:meth:`choose_highlighted` returns
        True, before :meth:`close` (which does not touch these -- only
        :meth:`ask` resets them, for exactly this ordering)."""
        if self.kind == "ask_user":
            return {"answers": dict(self._answers)}
        return {"decision": self._decision or "deny"}

    def close(self) -> None:
        if self.display:
            self.display = False
        self.request_id = None
        self.kind = None
        self._questions = []
        self._rows = []


class ChipPicker(OptionList):
    """The ONE dropdown every clickable status-bar chip opens (status-
    chips, item Y) -- model, branch, effort -- reused rather than one
    widget per chip, since the "list candidates, mark the current one,
    navigate, filter, select" shape is identical across all three.

    Unlike the three PROMPT-driven popups above (SlashComplete,
    NeedsInputPopup, SessionSearch -- all `can_focus = False`, driven
    entirely through PromptInput's own key protocol because typing owns
    focus at that point), this one takes REAL focus the instant it opens:
    nothing else needs the caret while a chip menu is up, and taking focus
    is what lets OptionList's OWN bindings (up/down/home/end/enter, and
    its built-in mouse-click-to-select -- confirmed in
    `_option_list.py`: `action_cursor_up`/`action_cursor_down` already
    skip disabled rows via `find_next_enabled`) work completely unchanged
    -- this widget adds only what OptionList does NOT already have: Esc to
    close, and type-to-filter. Type-to-filter follows the exact pattern
    `textual.widgets.Input._on_key` uses for printable characters
    (`event.is_printable` / `event.character`, `event.stop()`) layered on
    top of the inherited bindings rather than replacing them.

    Rows are `(id, label)` pairs; `id` is what actually gets handed to the
    same `_cmd_*` coroutine the matching slash command uses (`open()`'s
    `on_select` callback) -- the picker is UI only, never a second
    implementation of a switch. An optional `note` occupies row 0 as a
    disabled heading (same "label, never a destination" convention
    NeedsInputPopup's own row 0 follows) for the one caller that needs an
    honesty caveat: the effort picker, whose selection cannot take effect
    on the CURRENT session (connect-time only, same as `/effort` itself)."""

    can_focus = True
    BINDINGS = [Binding("escape", "close_picker", "Close", show=False)]

    def __init__(self, pane: "SessionPane") -> None:
        super().__init__(id="chip-picker")
        self.pane = pane
        self.display = False
        self._all_rows: list[tuple[str, str]] = []
        # Row-by-row map onto what the OptionList shows -- SAME
        # convention SlashComplete._rows follows, including the note
        # heading occupying index 0 as `("", note_text)` when present.
        self._rows: list[tuple[str, str]] = []
        self._note = ""
        self._filter_text = ""
        self._current_id: "str | None" = None
        self._on_select: "Callable[[str], Any] | None" = None

    @property
    def is_open(self) -> bool:
        return bool(self.display)

    def open(
        self,
        rows: "list[tuple[str, str]]",
        current_id: "str | None",
        on_select: "Callable[[str], Any]",
        *,
        note: str = "",
        title: str = "",
    ) -> None:
        """Configure and show. Reopening (a click on a DIFFERENT chip
        while this one is already up) just reconfigures the same instance
        -- there is only ever one picker, so no prior-close bookkeeping is
        needed here."""
        self._all_rows = list(rows)
        self._current_id = current_id
        self._on_select = on_select
        self._note = note
        self._filter_text = ""
        self.border_title = title
        self._render_rows()
        self.display = True
        self.focus()

    def _render_rows(self) -> None:
        self.clear_options()
        self._rows = []
        if self._note:
            self.add_option(Option(self._note, disabled=True))
            self._rows.append(("", self._note))
        candidates = self._all_rows
        if self._filter_text:
            matcher = Matcher(self._filter_text)
            scored = [
                (matcher.match(label), rid, label) for rid, label in self._all_rows
            ]
            candidates = [
                (rid, label) for score, rid, label in
                sorted((s for s in scored if s[0] > 0), key=lambda s: (-s[0], s[2]))
            ]
        if not candidates:
            self.add_option(Option("  (no match)", disabled=True))
        else:
            for rid, label in candidates:
                mark = "▸" if rid == self._current_id else " "
                self.add_option(Option(f" {mark} {_escape_markup(label)}"))
                self._rows.append((rid, label))
        first = 1 if self._note else 0
        if len(self._rows) > first and self._rows[first][0]:
            self.highlighted = first
        else:
            self.highlighted = None
        self.border_subtitle = f"/{self._filter_text}" if self._filter_text else ""

    def _on_key(self, event: events.Key) -> None:
        """Type-to-filter -- the "autocomplete" a status-bar dropdown is
        expected to have. Backspace/printable only; every other key
        (arrows, enter, home/end, escape) is left untouched so OptionList's
        own bindings (and this class's own Esc binding) keep handling it."""
        if event.is_printable:
            event.stop()
            event.prevent_default()
            assert event.character is not None
            self._filter_text += event.character
            self._render_rows()
        elif event.key == "backspace" and self._filter_text:
            event.stop()
            event.prevent_default()
            self._filter_text = self._filter_text[:-1]
            self._render_rows()

    def _on_blur(self, event: events.Blur) -> None:
        """Focus genuinely moving to another widget (a tab switch, a click
        on something else focusable) closes the picker too -- click-away
        onto something NON-focusable is handled separately, at the pane
        level (see SessionPane._on_click_away_closes_chip_picker), since a
        click there never generates a Blur in the first place."""
        if self.display:
            self.close()

    def action_close_picker(self) -> None:
        self.close()
        self.pane.query_one("#prompt-input").focus()

    def select_row(self, index: int) -> None:
        """OptionSelected's `option_index` -- fired by Enter (OptionList's
        own `action_select`) or a mouse click on a row (OptionList's own
        `_on_click`), indistinguishably; both already skip disabled rows,
        so `index` here is always a real, selectable candidate."""
        if not (0 <= index < len(self._rows)):
            return
        rid, _label = self._rows[index]
        if not rid:
            return
        callback = self._on_select
        self.close()
        self.pane.query_one("#prompt-input").focus()
        if callback is not None:
            callback(rid)

    def close(self) -> None:
        if self.display:
            self.display = False
        self._on_select = None
        self._all_rows = []
        self._rows = []
        self._filter_text = ""
        self.border_title = ""
        self.border_subtitle = ""


class PromptInput(TextArea):
    """The prompt: a multi-line editor, plus the key protocols of the two
    popups above it (item N -- clipboard paste).

    Was a single-line ``Input`` through 0.8.0; a bracketed multi-line paste
    landing in a widget that can only show ONE row was the immediate
    forcing function (``Input._on_paste`` keeps only ``splitlines()[0]`` --
    every line after the first was silently dropped, no error, nothing).
    ``TextArea`` is Textual's only multi-line text widget; it comes with a
    gutter, undo history and a real ``ctrl+v`` action none of which this
    prompt wants, so most of this class exists to strip those back down to
    "one growing line" rather than to add anything TextArea lacks.

    Height policy: :data:`MIN_ROWS` (1) up to :data:`MAX_ROWS` (10) content
    rows, recomputed after every edit from ``wrapped_document.height`` (the
    SOFT-WRAPPED row count, so a long single line grows the box the same
    way embedded newlines do) -- past the cap the box stops growing and
    TextArea's own scrolling takes over, never displacing the block list
    above it by more than the cap allows.

    ``value``/``value=`` stay as a thin alias over ``.text`` -- every test
    and script written against the old ``Input``-backed prompt keeps
    working; :meth:`clear` is the new spelling of ``self.value = ""`` (it
    also forgets pending paste placeholders, which a bare text reset should
    not leave dangling).

    ``ctrl+v`` is deliberately UNBOUND (mapped to a no-op): TextArea's own
    binding pastes from Textual's OWN in-process clipboard variable --
    whatever this app last copied -- not the live OS clipboard, which is
    silently wrong on any terminal that hasn't echoed an OSC52 write back
    in. The real paste path is bracketed paste (:meth:`_on_paste`): the
    terminal delivers the actual, current clipboard content as one
    ``events.Paste``, and a stray physical Ctrl+V that a terminal does NOT
    special-case reaches us as an ordinary (now harmless) keystroke.

    While a popup is open, up/down/tab/enter/escape belong to IT. With
    all three popups closed, bare Enter submits (see :class:`Submitted`)
    and Shift+Enter/Alt+Enter insert a literal newline -- whichever a
    given terminal actually distinguishes from bare Enter; item O's
    keyboard-protocol detection is what will one day tell the operator
    which of the two their terminal grants, but both are bound here
    regardless so neither terminal family is left without a
    deliberate-newline key.

    The needs-input dialog (queue item 5) is checked FIRST, ahead of
    search and the slash dropdown: a pending AskUserQuestion/permission
    request represents something ELSE actually waiting on you, not a UI
    convenience you opened yourself, so it wins any (in practice
    vanishingly rare) contention for the same keystroke. The search popup
    is checked next because it is the one that can be open while a
    command name is fully typed (``/search ...``); the two ordinary
    popups are mutually exclusive in practice, and this settles the order
    anyway."""

    MIN_ROWS = 1
    MAX_ROWS = 10

    BINDINGS = [
        Binding("ctrl+v", "noop", show=False),
    ]

    class Submitted(Message):
        """Bare Enter, both popups closed: this pane's turn to run --
        TextArea has no ``Input.Submitted`` equivalent, so the prompt
        defines its own rather than repurposing a message class tied to a
        different widget type."""

        def __init__(self, prompt_input: "PromptInput", value: str) -> None:
            self.prompt_input = prompt_input
            self.value = value
            super().__init__()

        @property
        def control(self) -> "PromptInput":
            return self.prompt_input

    class ClipboardImageNotice(Message):
        """Posted when a paste event arrives with NO text and the system
        clipboard turns out to be holding an image right then. A terminal
        cannot forward binary clipboard content through bracketed paste --
        there is no escape sequence for it -- so an empty paste is the only
        signal DOXA ever gets that something was pasted at all. This is the
        "stub, and report" half of item N.4: there is no image-attachment
        plumbing in the engine to hand the bytes to yet, so the honest
        thing is to say what was noticed, not to pretend to attach it."""

        def __init__(self, mime: str) -> None:
            self.mime = mime
            super().__init__()

    class NeedsInputChoice(Message):
        """Enter, or a number key 1-9, while the needs-input popup is
        open: which option (0-based -- the heading row never reaches
        here). SessionPane runs the actual (async) engine round-trip;
        this widget only knows keys, not how to await one."""

        def __init__(self, popup: "NeedsInputPopup", index: int) -> None:
            self.popup = popup
            self.index = index
            super().__init__()

    class NeedsInputDecline(Message):
        """Esc while the needs-input popup is open -- a graceful decline,
        see :class:`NeedsInputPopup`'s own docstring."""

        def __init__(self, popup: "NeedsInputPopup") -> None:
            self.popup = popup
            super().__init__()

    def __init__(
        self,
        dropdown: SlashComplete,
        search: SessionSearch,
        needs_input: "NeedsInputPopup",
        **kwargs: Any,
    ) -> None:
        kwargs.setdefault("tab_behavior", "focus")
        kwargs.setdefault("soft_wrap", True)
        kwargs.setdefault("show_line_numbers", False)
        kwargs.setdefault("highlight_cursor_line", False)
        super().__init__(**kwargs)
        self.dropdown = dropdown
        self.search = search
        self.needs_input_popup = needs_input
        # (placeholder text, original text) for every paste collapsed in
        # THIS message -- resolved back into the real content at submit
        # time regardless of whether the operator ever expanded it to
        # look. See doxa/paste.py for the collapse threshold and format.
        self._pending_pastes: list[tuple[str, str]] = []
        self.styles.height = self.MIN_ROWS + 2  # +2: the round border

    def action_noop(self) -> None:
        """Where ``ctrl+v`` lands now -- see the class docstring."""

    @property
    def value(self) -> str:
        """Back-compat alias for the ``Input.value`` this widget
        replaced."""
        return self.text

    @value.setter
    def value(self, text: str) -> None:
        self.text = text
        self.move_cursor(self.document.end)

    @property
    def cursor_position(self) -> int:
        """Back-compat alias for ``Input.cursor_position``: the cursor's
        flat character offset into :attr:`text`, counting embedded
        newlines as one character each -- unambiguous for the single-line
        content every existing caller of this property still uses it on."""
        return self.document.get_index_from_location(self.cursor_location)

    def clear(self) -> None:
        """Blank the prompt and forget its pending paste placeholders --
        the pane calls this once it has taken the value, in place of the
        old ``self.value = ""``."""
        self.text = ""
        self._pending_pastes = []

    def take_hit(self) -> bool:
        """Enter on a matching SNIPPET row (never a session header -- see
        PromptInput.on_key, which only calls this for a "hit" row): the
        chosen excerpt REPLACES the ``/search …`` line that found it,
        exactly like the pre-item-J session reference used to. What
        replaces it is now item J's excerpt -- a provenance line plus the
        de-marked snippet -- staged through the SAME collapse machinery a
        real clipboard paste uses (:meth:`_stage_pasteable`), so a long
        excerpt collapses to a placeholder, Ctrl+G expands it, and submit
        sends the full text either way."""
        hit = self.search.chosen()
        if hit is None:
            return False
        self.search.dismiss_for_this_line()
        self.value = self._stage_pasteable(history_mod.excerpt_text(hit))
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
        return True

    def _resolved_text(self) -> str:
        """The text to actually send: every collapsed-paste placeholder
        swapped back for what it stood for."""
        text = self.text
        for placeholder, original in self._pending_pastes:
            text = text.replace(placeholder, original)
        return text

    def _submit(self) -> None:
        self.post_message(self.Submitted(self, self._resolved_text()))

    def _expand_pending_paste(self) -> bool:
        """Ctrl+G: if the cursor sits on a line that is EXACTLY a collapsed
        placeholder, swap it back for the text it stands for -- the
        "expandable" half of "collapse to a placeholder, expandable" (item
        N.3). Submitting without ever expanding still sends the real
        content (:meth:`_resolved_text`); this is only for looking first."""
        if not self._pending_pastes:
            return False
        row, _col = self.cursor_location
        line = self.document.get_line(row)
        for index, (placeholder, original) in enumerate(self._pending_pastes):
            if line == placeholder:
                start = (row, 0)
                end = (row, len(line))
                self.replace(original, start, end)
                del self._pending_pastes[index]
                return True
        return False

    def _resize_to_content(self) -> None:
        """Grow the box to fit :attr:`MIN_ROWS`..:attr:`MAX_ROWS` content
        rows -- past the cap TextArea's own vertical scrolling takes over
        instead. Uses the WRAPPED row count (``wrapped_document.height``),
        not the raw newline count, so a long single line (soft-wrapped)
        grows the box the same way embedded newlines would."""
        rows = max(self.MIN_ROWS, min(self.MAX_ROWS, self.wrapped_document.height))
        self.styles.height = rows + 2  # +2: the round border, top and bottom

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        # Deliberately NOT stopped: SessionPane's own ``@on(TextArea.Changed,
        # "#prompt-input")`` handler still needs this to drive the two
        # popups -- this instance-level handler only owns the box's height.
        self._resize_to_content()

    async def _on_paste(self, event: events.Paste) -> None:
        """Bracketed paste, handled explicitly rather than left to
        TextArea's own ``_on_paste`` (which inserts the raw text verbatim,
        no collapse) -- see doxa/paste.py. Always exactly ONE edit no
        matter how many lines the clipboard held: nothing here can ever
        submit a turn, spurious or otherwise -- only :meth:`_submit` posts
        ``Submitted``, and paste handling never calls it.

        ``prevent_default()`` (not just ``stop()``) is required here:
        Textual calls EVERY class's own ``_on_paste`` up the MRO, most-
        derived first, and only ``prevent_default()`` short-circuits that
        walk before it reaches ``TextArea._on_paste`` -- without it the
        paste would land TWICE, once collapsed here and once verbatim
        from TextArea's own default handling."""
        event.stop()
        event.prevent_default()
        if not event.text:
            # No text at all: possibly a genuine no-op paste, possibly a
            # clipboard holding something a terminal can't forward (an
            # image). Worth a cheap, off-loop check -- never worth
            # blocking the keystroke on.
            self.run_worker(self._check_clipboard_image(), group="clipboard-probe")
            return
        text = paste_mod.normalize_newlines(event.text)
        insert = self._stage_pasteable(text)
        if result := self._replace_via_keyboard(insert, *self.selection):
            self.move_cursor(result.end_location)

    def _stage_pasteable(self, text: str) -> str:
        """Collapse ``text`` to a paste.py placeholder if it is large
        enough to warrant one, bookkeeping it in :attr:`_pending_pastes`
        so Ctrl+G (:meth:`_expand_pending_paste`) and submit-time
        resolution (:meth:`_resolved_text`) pick it up exactly like a real
        clipboard paste; otherwise returns it untouched. The one seam a
        real paste (item N) and an inserted search excerpt (item J) share
        -- see doxa/paste.py's own module docstring, written expecting
        this second caller before item J existed to be it."""
        if paste_mod.should_collapse(text):
            placeholder = paste_mod.placeholder_for(text)
            self._pending_pastes.append((placeholder, text))
            return placeholder
        return text

    async def _check_clipboard_image(self) -> None:
        mime = await asyncio.to_thread(paste_mod.detect_clipboard_image_mime)
        if mime:
            self.post_message(self.ClipboardImageNotice(mime))

    def on_key(self, event: events.Key) -> None:
        if self.needs_input_popup.is_open:
            popup = self.needs_input_popup
            if event.key == "escape":
                self.post_message(self.NeedsInputDecline(popup))
            elif event.key == "down":
                popup.move(1)
            elif event.key == "up":
                popup.move(-1)
            elif event.key == "enter":
                index = (popup.highlighted if popup.highlighted is not None else 1) - 1
                self.post_message(self.NeedsInputChoice(popup, index))
            elif event.key in _NEEDS_INPUT_DIGIT_KEYS:
                self.post_message(self.NeedsInputChoice(popup, int(event.key) - 1))
            else:
                return
            event.stop()
            event.prevent_default()
            return
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
            elif event.key == "right":
                self.search.expand_current()  # item I: open a session fold
            elif event.key == "left":
                self.search.collapse_current()  # item I: close it (or its parent)
            elif event.key == "enter":
                if self.search.current_kind() == "header":
                    # Match the trace tree's own convention: Enter toggles
                    # a fold. A header row is never itself an excerpt, so
                    # this is the ONLY thing Enter can mean here.
                    self.search.toggle_current()
                elif not self.take_hit():
                    self._submit()  # no hits: Enter submits, /search answers
            else:
                return
            event.stop()
            event.prevent_default()
            return
        if self.dropdown.is_open:
            if event.key == "escape":
                self.dropdown.dismiss_for_this_line()
            elif event.key == "down":
                self.dropdown.move(1)
            elif event.key == "up":
                self.dropdown.move(-1)
            elif event.key in ("tab", "enter"):
                command = self.dropdown.chosen()
                if event.key == "enter" and command is not None and self.text == command.name:
                    # Already typed in full: there is nothing to complete,
                    # so Enter means SEND. (Otherwise typing a whole
                    # command would cost two Enters -- one to "complete"
                    # it into itself.)
                    self.dropdown.dismiss_for_this_line()
                    self._submit()
                else:
                    if not self.complete():
                        return
            else:
                return
            event.stop()
            event.prevent_default()
            return
        # Neither popup open: this widget's own submit/newline protocol.
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            self._submit()
            return
        if event.key in ("shift+enter", "alt+enter"):
            event.stop()
            event.prevent_default()
            self._replace_via_keyboard("\n", *self.selection)
            return
        if event.key == "ctrl+g":
            if self._expand_pending_paste():
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


class StatusBar(Static):
    """The top status row -- SAME click-action-span pattern SubagentLine
    (below) already established for its own row: an unprefixed
    `[@click=...]` markup span resolves against the CLICKED widget itself
    (`Widget.broker_event`, confirmed empirically there), so each action
    method just needs to live on this class and delegate to the owning
    pane.

    Three tiers of chip live in one status-bar string (`_refresh_status`
    builds it) but only two carry a click action at all -- the operator's
    own "for every chip?" question, answered explicitly in the release
    notes: SELECTORS open the shared :class:`ChipPicker` (model, branch,
    effort), ACTIONABLE chips run something that already exists with no
    picker (peers -> /sessions, ctx% -> /compact, the session handle ->
    clipboard), and everything else (cost, repo name, sha, usage headroom)
    stays plain -- giving every chip the same affordance would make the
    affordance stop meaning anything, the same defect class as the
    original "the whole bar looks interactive and isn't" report. One
    action per chip rather than a single dispatcher taking an argument:
    simpler markup (no `json.dumps`-escaped action params to get wrong),
    and every action here is a fixed, known operation anyway."""

    def __init__(self, pane: "SessionPane") -> None:
        super().__init__("doxa · connecting…", id="status-bar")
        self.pane = pane

    async def action_open_model_picker(self) -> None:
        await self.pane.open_model_picker()

    async def action_open_branch_picker(self) -> None:
        await self.pane.open_branch_picker()

    async def action_open_effort_picker(self) -> None:
        await self.pane.open_effort_picker()

    def action_open_sessions(self) -> None:
        self.pane.run_status_command("/sessions")

    def action_compact_now(self) -> None:
        self.pane.run_compact_now()

    def action_copy_session_handle(self) -> None:
        self.pane.copy_session_handle()


class SubagentLine(Static):
    """Second status row: one clickable ``⧉ <label>`` per RUNNING Task-
    spawned subagent, mounted directly below ``#status-bar`` -- and ONLY
    while at least one is running (SessionPane._sync_subagent_line mounts
    it on the first, unmounts it on the last, mirroring the house hide-at-
    zero convention the peers/git/usage chips already use in the status
    bar itself, just one level up: this is a whole ROW that costs nothing
    at idle rather than a chip that reads empty).

    Each span is Textual click-action markup, ``[@click=open_transcript
    ('id')]⧉ label[/]`` -- ``Widget.broker_event`` resolves an unprefixed
    action against the clicked widget itself (confirmed empirically, see
    tests/test_subagent_tracker.py), so ``action_open_transcript`` lives
    right here rather than needing an ``app.`` / ``screen.`` namespace
    prefix on every span."""

    def __init__(self, pane: "SessionPane") -> None:
        super().__init__("", id="subagent-line")
        self.pane = pane

    def refresh_labels(self, entries: "list[tuple[str, str]]") -> None:
        """`entries`: (call_id, label) pairs in arrival order -- the
        registry (a plain dict) already keeps them in the order they
        started, and that is the only ordering this row promises."""
        spans = [
            f"[@click=open_transcript({json.dumps(call_id)})]"
            f"⧉ {_escape_markup(label)}[/]"
            for call_id, label in entries
        ]
        self.update("  ·  ".join(spans))

    async def action_open_transcript(self, call_id: str) -> None:
        await self.pane.open_transcript(call_id)


class SubagentTranscriptTab(TabPane):
    """One running (or finished) subagent's OWN activity, read-only: a
    plain ``TabPane`` -- deliberately NOT a ``SessionPane`` -- with no
    engine and no prompt, living alongside the session tabs in the SAME
    ``#session-tabs`` strip. Opened by clicking its row on a
    :class:`SubagentLine`; titled from the subagent's own label.

    Two content sources, both routed through ``SessionPane``: at OPEN time
    :meth:`replay` mirrors whatever the live Task chip already buffered
    (narration text + its direct subcall chips, via :func:`_clone_chip`)
    without touching the original trace-tree widgets; from then on,
    ``SessionPane._handle_event`` forwards further parent_id-matching
    events here AS WELL AS to the live chip, one level of nesting deep --
    a grandchild subagent (a Task spawned BY this one) gets its own
    second-line row and its own transcript tab instead of recursing into
    this one, exactly like a top-level Task would."""

    def __init__(
        self, call_id: str, label: str, owner: "SessionPane", *, id: str | None = None,
    ) -> None:
        self.call_id = call_id
        self.base_label = label
        self.owner = owner
        self.done = False
        self._narration_text = ""
        self._narration = Static("", classes="transcript-narration")
        self.chips_area = Vertical(classes="transcript-chips")
        self.scroll = VerticalScroll(
            self._narration, self.chips_area, classes="transcript-scroll",
        )
        # id, not call_id, keyed: mirror chips are fresh ToolChip instances
        # (see _clone_chip) that happen to reuse the ORIGINAL call's id, so
        # a tool_result event (matched by id) can find the right one here.
        self.mirror_chips: dict[str, ToolChip] = {}
        super().__init__(label, id=id)

    def compose(self) -> ComposeResult:
        yield self.scroll

    def append_narration(self, text: str) -> None:
        self._narration_text += text
        self._narration.update(self._narration_text)

    async def mirror_chip(self, chip: "ToolChip") -> "ToolChip":
        """Add ONE cloned chip for a direct child call that just arrived
        live (see SessionPane._route_transcript_chip) -- fresh, with
        whatever the source chip already knows (usually nothing yet but
        its name/input; a same-event tool_result lands right after)."""
        mirror = await _clone_chip(chip)
        self.mirror_chips[chip.call_id] = mirror
        await self.chips_area.mount(mirror)
        return mirror

    async def replay(self, chip: "ToolChip") -> None:
        """Open-time snapshot: the Task chip's buffered narration plus its
        current direct subcalls (each cloned recursively, so nesting that
        already happened by the time someone clicks is not lost) -- called
        once, right after this tab mounts."""
        if chip._sub_text:
            self.append_narration(chip._sub_text)
        for sub in list(chip.subcalls.children):
            if isinstance(sub, ToolChip):
                await self.mirror_chip(sub)

    def _set_title(self, text: str) -> None:
        self._title = self.render_str(text)
        _write_tab_label(self.app, self.id or "", text)

    def mark_done(self) -> None:
        """The tracked subagent's own tool_result just landed: title gets
        the ✓ suffix, and -- the same convention every other tab-status
        signal in this app follows -- a -done-unseen dot if this tab is
        not the one currently active (cleared on activation, see
        DoxaApp._on_tab_activated)."""
        if self.done:
            return
        self.done = True
        self._set_title(f"{self.base_label} ✓")
        with contextlib.suppress(Exception):
            tabbed = self.app.query_one("#session-tabs", TabbedContent)
            if tabbed.active != (self.id or ""):
                self._set_tab_class("-done-unseen", True)

    def _set_tab_class(self, class_name: str, value: bool) -> None:
        _write_tab_class(self.app, self.id or "", class_name, value)


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
        # Attention-blink infra (tab status, item: per-status tab colors).
        # Nothing sets this True yet -- the engine-side event that should
        # (can_use_tool / AskUserQuestion plumbing, a session waiting on the
        # user mid-turn) is phase 2. What exists here is the mechanism: a
        # timer that blinks the -attention class on the tab, alive ONLY
        # while needs_input is True (see set_needs_input) -- this app
        # measures idle CPU and a timer nothing ever stops is exactly the
        # busy-idle bug GitLine's docstring warns about, reintroduced.
        self.needs_input = False
        self._attention_timer: Any = None
        self._attention_on = False
        # Subagent tracker (queue item 4): running Task-spawned subagents
        # for THIS pane, tool_use_id -> the ToolChip already mounted in the
        # trace tree -- a second INDEX into that same widget, not a copy of
        # its state. Entries exist ONLY while running (added on a top-level
        # or nested tool_call named "Task", popped on that same id's own
        # tool_result), so len() IS the live count the status chip and the
        # second line both read, arrival order (plain dict insertion order)
        # is all either needs, and no wall clock is kept anywhere.
        self._subagents: dict[str, ToolChip] = {}
        # Open transcript tabs for THIS pane's subagents, call_id -> tab.
        # Outlives the matching _subagents entry (a finished subagent's tab
        # stays open, marked done, until the user closes it) but never
        # outlives the tab itself -- popped in _close_transcript_tab.
        self._transcript_tabs: dict[str, SubagentTranscriptTab] = {}
        # The second status row -- mounted the moment _subagents stops
        # being empty, unmounted the moment it is empty again (see
        # _sync_subagent_line); None at every other time, deliberately, so
        # an idle pane carries neither the widget nor its layout cost.
        self._subagent_line: "SubagentLine | None" = None
        # Status-chips (item Y): one provider seam instance per pane,
        # cached for the pane's whole life -- list_models() itself caches
        # its result too (see doxa.providers), but this is what makes THAT
        # cache actually persist across picker opens instead of being
        # rebuilt (and re-probing the network) every time.
        self._model_provider = providers_mod.ClaudeProvider()

    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="block-list")
        yield StatusBar(self)
        # All four popups sit directly ABOVE the prompt (the last
        # children): in a terminal the block list simply gives up the rows
        # while one is open, which reads as an overlay without the layer
        # bookkeeping a floating panel would need over a TabbedContent. The
        # two ordinary ones are never open at once -- the slash dropdown
        # closes at the first space, which is exactly the keystroke that
        # opens the search popup. The needs-input dialog (queue item 5) is
        # independent of both -- see PromptInput.on_key's priority order.
        # The chip picker (status-chips, item Y) is independent of all
        # three too -- it opens from a status-bar click, never from
        # anything typed in the prompt.
        search = SessionSearch(self.cwd)
        yield search
        dropdown = SlashComplete()
        yield dropdown
        needs_input = NeedsInputPopup()
        yield needs_input
        yield ChipPicker(self)
        # No ``placeholder=`` -- TextArea has no built-in placeholder text
        # (Input did); a deliberate drop, not an oversight, see item N.
        yield PromptInput(dropdown, search, needs_input, id="prompt-input")

    async def on_mount(self) -> None:
        self.engine = self._engine_factory()
        self.query_one("#prompt-input", PromptInput).focus()
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

    async def stop(self) -> "str | None":
        """Finalize this pane's session NOW (daemon included). Returns the
        worktree-per-session (#3) closing note -- `kept doxa/<id> — merge
        when ready` -- when the daemon kept an unfinished worktree instead
        of removing it; None otherwise (in-process engines never have
        one; a cleanly-removed or non-worktree session doesn't either)."""
        engine, self.engine = self.engine, None
        if engine is None:
            return None
        stop = getattr(engine, "stop", None)
        note: "str | None" = None
        with contextlib.suppress(Exception):
            if stop is not None:
                event = await stop()
                data = getattr(event, "data", None) or {}
                value = data.get("note") if isinstance(data, dict) else None
                note = str(value) if value else None
            else:
                await engine.finalize()
        return note

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
            elif ev.type == "needs_input":
                self._open_needs_input(ev.data)
            elif ev.type == "needs_input_resolved":
                # Some attached client (possibly a DIFFERENT one -- the
                # daemon fans this to everyone, see doxa/client.py) just
                # answered this pane's own pending request. If the popup
                # here is still showing that SAME id, drop it -- it is no
                # longer this pane's to answer.
                popup = self.query_one("#needs-input-popup", NeedsInputPopup)
                if popup.request_id and popup.request_id == ev.data.get("id"):
                    popup.close()
                    self.set_needs_input(False)
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
                # A new turn starting (even one another client is driving)
                # is itself "seen" -- the same stale-dot clear _run_turn
                # does for a locally-driven turn.
                self._set_tab_class("-done-unseen", False)
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
        """`Opus@doxa:main` -- which model is answering, and WHAT IT IS
        WORKING OFF.

        The branch half is GitLine.tab_branch(): the worktree-per-session
        BASE (`main`) inside an isolated session, not the session's own
        throwaway branch (`doxa/f13526d4`, branch_label()'s answer, kept
        for the status bar/`/about` -- see that method's docstring for the
        v0.17 regression this un-does). Both halves are tracked state
        already: the model is the engine's (so a live /model switch moves
        it), and the repo/branch come from the pane's GitLine, whose reads
        are event-driven stats -- this adds no polling and no subprocess.
        OUTSIDE a repo there is nothing after the `@` that would mean
        anything, so the session names itself from its first turn
        (doxa/naming.py) and the directory name stands in until it does."""
        engine = self.engine
        model = short_model(getattr(engine, "model", None) or self.model)
        cwd = str(getattr(engine, "cwd", None) or self.cwd)
        git = self._git
        if git is not None and git.repo:
            branch, isolated = git.tab_branch()
            return compose_tab_label(model, git.repo, branch, isolated=isolated)
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
        re-add of the pane read.

        `text` is the tab's plain IDENTITY -- what the rename field seeds
        from, what a later call compares against to skip a no-op render,
        what the user typed if they pinned the tab. The provider glyph is
        a display-only prefix layered on top HERE, never folded into that
        identity string: a pinned (user-renamed) tab still gets the glyph
        -- provider identity is orthogonal to the user's name for the tab
        -- but renaming it back to itself must not hand back
        "✳ my old name" as the seed."""
        self._tab_label = text
        displayed = f"{provider_glyph()} {text}"
        self._title = self.render_str(displayed)
        with contextlib.suppress(Exception):
            tabbed = self.app.query_one("#session-tabs", TabbedContent)
            tabbed.get_tab(self.id or "").label = displayed

    def _set_tab_class(self, class_name: str, value: bool) -> None:
        """Toggle one status class (``-working`` / ``-done-unseen`` /
        ``-attention``) on this pane's own Tab header -- same
        contextlib.suppress discipline as :meth:`set_tab_label`, and for
        the same reasons: the tab may not exist yet this early in boot, or
        this pane may already be mid-teardown (a closed tab's last event
        landing after the Tab widget is gone). Delegates to the module-level
        ``_write_tab_class``, the same door ``SubagentTranscriptTab`` uses
        for its own (``-done-unseen``-only) status class."""
        _write_tab_class(self.app, self.id or "", class_name, value)

    def set_needs_input(self, value: bool) -> None:
        """The attention-blink mechanism. Nothing calls this with True yet
        -- see the ``needs_input`` note in ``__init__`` -- but the timer
        discipline is real: a ``set_interval`` lives on this pane ONLY
        between a True call and the next False (or tab activation, which
        also clears it), never longer. That is what keeps an idle DOXA at
        zero timers even after this feature is wired up in phase 2."""
        if value == self.needs_input:
            return
        self.needs_input = value
        if value:
            self._attention_on = False
            self._attention_timer = self.set_interval(0.5, self._blink_attention)
        else:
            if self._attention_timer is not None:
                self._attention_timer.stop()
                self._attention_timer = None
            self._attention_on = False
            self._set_tab_class("-attention", False)

    def _blink_attention(self) -> None:
        self._attention_on = not self._attention_on
        self._set_tab_class("-attention", self._attention_on)

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
        # Three tiers of chip, per the operator's own "for every chip?"
        # question (release notes): SELECTOR chips (model, branch, effort)
        # open the shared ChipPicker; ACTIONABLE chips (peers, ctx%, the
        # session handle) run something that already exists with no
        # picker; everything else (cost, repo name, sha, usage headroom,
        # beliefs -- no `/beliefs`-ish surface exists to route to, see the
        # release notes) stays plain. Only the first two tiers get
        # _chip_span's click markup / accent color.
        parts = [_chip_span(model, "open_model_picker")]
        if self.needs_input:  # hide-at-zero, same convention as every
            # other chip below -- visible only while a question or
            # permission request is actually pending on THIS pane.
            parts.append("⚑ needs input")
        effort = getattr(self.engine, "effort", None)
        if effort:  # hide-at-zero: omitted when the CLI default is in
            # force (no level asserted at connect) -- same convention as
            # the git/usage/peers/disabled-tools chips below. A SELECTOR
            # too, but its picker can only ever affect a FUTURE session
            # (connect-time only, same as /effort) -- the picker itself
            # says so rather than silently no-opping.
            parts.append(_chip_span(f"effort:{effort}", "open_effort_picker"))
        git_chip = self._git.render(clickable=True) if self._git is not None else None
        if git_chip:  # hidden entirely outside a repo
            parts.append(git_chip)
        parts.append(cost)
        if self._usage_chip:  # only when real numbers exist -- see below
            parts.append(self._usage_chip)
        # ctx% is ACTIONABLE (click -> /compact) but its own markup is
        # already trusted, code-generated pressure coloring (ctx_chip's
        # amber/red escalation) -- wrapping it through _chip_span would
        # escape THOSE brackets as if they were arbitrary text, same
        # defect a literal `[` in a model-chosen label would risk the
        # other way. So the click span is built directly here, no
        # _escape_markup: the accent shows through at the "normal" tier
        # (no inner color) and yields to the pressure color once one
        # applies -- the pressure signal outranks the click affordance.
        ctx_text = ctx_chip(self.engine.last_ctx_percentage)
        parts += [
            f"[@click=compact_now][{CLICKABLE_CHIP_ACCENT}]{ctx_text}[/][/]",
            f"{beliefs} beliefs",
        ]
        subagent_count = len(self._subagents)
        if subagent_count:  # hidden at 0 -- same convention as peers below
            noun = "agent" if subagent_count == 1 else "agents"
            parts.append(f"⧉ {subagent_count} {noun}")
        if getattr(self.engine, "detachable", False):
            sid = str(getattr(self.engine, "session_id", "") or "")
            if sid:  # attached to a daemon: show the reattach handle --
                # ACTIONABLE (copies it to the clipboard); the accent
                # color replaces the old #8A8073 dim treatment, since a
                # clickable chip wears the SAME affordance every other one
                # does rather than staying visually "quiet".
                parts.append(_chip_span(f"⌁ session {sid[:8]}", "copy_session_handle"))
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
            peers_text = (
                f"peers {peer_count}" + (f" ({detached}⌁)" if detached else "")
            )
            parts.append(_chip_span(peers_text, "open_sessions"))
        disabled = self.engine.disabled_tools()
        if disabled:  # two-strikes containment note -- hidden when empty
            parts.append(" ".join(f"⊘ {name}" for name in disabled))
        bar = self.query_one("#status-bar", StatusBar)
        bar.update("  ·  ".join(parts))

    @on(TextArea.Changed, "#prompt-input")
    def _on_prompt_changed(self, event: TextArea.Changed) -> None:
        """The two popups' only trigger: what the prompt currently says.
        Cheap by construction -- a registry scan of a handful of rows for
        the dropdown, and for the search popup a debounce timer rather than
        a query (this app does not poll, and it does not hit SQLite on a
        keystroke either). PromptInput's OWN ``on_text_area_changed``
        (box-height resize) has already run by the time this bubbles here
        -- it deliberately does not stop the event."""
        event.stop()
        if self.query_one("#needs-input-popup", NeedsInputPopup).is_open:
            # A pending question owns this row while it is up -- typing
            # still works (composing a note is fine), but the two ordinary
            # popups must not pop up underneath/instead of it.
            return
        text = event.text_area.text
        self.query_one("#slash-complete", SlashComplete).sync(text)
        self.query_one("#session-search", SessionSearch).sync(text)

    @on(OptionList.OptionSelected, "#slash-complete")
    def _on_slash_selected(self, event: OptionList.OptionSelected) -> None:
        """Clicking an entry completes it, same as Tab/Enter would."""
        event.stop()
        prompt = self.query_one("#prompt-input", PromptInput)
        prompt.dropdown.highlighted = event.option_index
        prompt.complete()
        prompt.focus()

    @on(OptionList.OptionSelected, "#chip-picker")
    def _on_chip_picker_selected(self, event: OptionList.OptionSelected) -> None:
        """Enter (OptionList's own ``action_select``) and a mouse click on
        a row (OptionList's own ``_on_click``) both post this SAME
        message -- one handler covers keyboard and mouse selection."""
        event.stop()
        self.query_one("#chip-picker", ChipPicker).select_row(event.option_index)

    @on(events.Click)
    def _on_click_away_closes_chip_picker(self, event: events.Click) -> None:
        """A click ANYWHERE in this pane other than the picker itself
        closes it -- clicking one of the status-bar's own `[@click=...]`
        spans never reaches here (``Widget.broker_event`` calls
        ``event.stop()`` the moment it resolves an action, before the
        event would bubble up to this pane-level handler), and a click on
        the picker's own rows is handled by ``_on_chip_picker_selected``
        above (OptionList's ``_on_click`` does not itself stop the event,
        so it still bubbles here too -- the ``event.widget is picker``
        check below is what keeps that harmless). Focus genuinely moving
        elsewhere (a tab switch, clicking another focusable widget) is
        handled separately by ChipPicker's own ``_on_blur``, since that
        case never fires a Click that bubbles through this pane at all."""
        picker = self.query_one("#chip-picker", ChipPicker)
        if picker.is_open and event.widget is not picker:
            picker.close()

    @on(OptionList.OptionSelected, "#needs-input-popup")
    def _on_needs_input_selected(self, event: OptionList.OptionSelected) -> None:
        """Clicking a row answers it, same as a number key or Enter would.
        ``event.option_index`` is offset by the disabled heading row at 0
        -- see :class:`NeedsInputPopup`'s own row convention."""
        event.stop()
        popup = self.query_one("#needs-input-popup", NeedsInputPopup)
        index = event.option_index - 1
        if index < 0:
            return
        self.run_worker(
            self._resolve_needs_input(popup, index, False),
            exclusive=True, group="needs-input",
        )

    @on(PromptInput.NeedsInputChoice)
    def _on_needs_input_key_choice(self, event: "PromptInput.NeedsInputChoice") -> None:
        event.stop()
        self.run_worker(
            self._resolve_needs_input(event.popup, event.index, False),
            exclusive=True, group="needs-input",
        )

    @on(PromptInput.NeedsInputDecline)
    def _on_needs_input_key_decline(self, event: "PromptInput.NeedsInputDecline") -> None:
        event.stop()
        self.run_worker(
            self._resolve_needs_input(event.popup, None, True),
            exclusive=True, group="needs-input",
        )

    @on(OptionList.OptionSelected, "#session-search")
    def _on_search_selected(self, event: OptionList.OptionSelected) -> None:
        """Clicking a row does what Enter would: toggle a session header,
        or take a snippet's excerpt."""
        event.stop()
        prompt = self.query_one("#prompt-input", PromptInput)
        prompt.search.highlighted = event.option_index
        if prompt.search.current_kind() == "header":
            prompt.search.toggle_current()
        else:
            prompt.search.take_hit()
        prompt.focus()

    @on(PromptInput.Submitted)
    def on_prompt_submitted(self, event: "PromptInput.Submitted") -> None:
        event.stop()  # this pane's prompt is nobody else's business
        self.query_one("#slash-complete", SlashComplete).close()
        self.query_one("#session-search", SessionSearch).close()
        prompt = event.value.strip()
        if not prompt:
            return
        event.control.clear()
        # Only rows of the slash registry (doxa/commands.py) are
        # intercepted, and passthrough rows deliberately are not: the
        # literal "/compact" convention has to REACH the CLI to do anything.
        command = commands_mod.lookup(prompt)
        if command is not None and not command.passthrough:
            self.run_worker(self._run_command(prompt), group="command")
            return
        self.run_worker(self._run_turn(prompt), exclusive=True, group="turn")

    @on(PromptInput.ClipboardImageNotice)
    async def on_clipboard_image_notice(
        self, event: "PromptInput.ClipboardImageNotice"
    ) -> None:
        event.stop()
        await self._system(
            f"clipboard holds an image ({event.mime}) — image attachments "
            "aren't wired into turns yet; save it to a file and use "
            "/img <path>, or paste it somewhere that turns it into a "
            "file DOXA can point at"
        )

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
            "/branch": self._cmd_branch,
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

    # -- status-chips: the shared picker, and what each chip opens it with
    # (item Y) -------------------------------------------------------

    def _open_chip_picker(
        self,
        rows: "list[tuple[str, str]]",
        current_id: "str | None",
        on_select: "Callable[[str], Any]",
        *,
        note: str = "",
        title: str = "",
    ) -> None:
        """Shared entry point for all three SELECTOR chips -- guards
        against opening UNDER a pending needs-input request (same "the
        question owns this row" rule ``_on_prompt_changed`` already
        applies to the two prompt-driven popups) and closes those two
        popups first, so at most one of the four ever shows at once."""
        if self.query_one("#needs-input-popup", NeedsInputPopup).is_open:
            return
        self.query_one("#slash-complete", SlashComplete).close()
        self.query_one("#session-search", SessionSearch).close()
        self.query_one("#chip-picker", ChipPicker).open(
            rows, current_id, on_select, note=note, title=title,
        )

    @staticmethod
    def _match_current_model(current: str, rows: "list[tuple[str, str]]") -> "str | None":
        """Which row is "the current one" for the ``▸`` marker.
        ``engine.model`` holds whatever raw string the last switch used --
        an alias (``sonnet``) OR a resolved full id
        (``claude-sonnet-4-5``) -- so an exact match is tried first, then
        a substring match either direction (the same looseness
        ``short_model`` already uses for the tab label's tier word)."""
        current_l = current.lower()
        for rid, _label in rows:
            if rid.lower() == current_l:
                return rid
        for rid, _label in rows:
            if rid.lower() in current_l or current_l in rid.lower():
                return rid
        return None

    async def open_model_picker(self) -> None:
        """The model chip's click target -- lists whatever
        ``doxa.providers.ClaudeProvider.list_models()`` resolves (see that
        module for the full tier order and the empirical finding on why
        tier 1, the live Models API, is unreachable under DOXA's normal
        OAuth auth), marks the current one, and on selection calls the
        SAME ``_cmd_model`` coroutine ``/model <name>`` uses -- one switch
        path, two ways to reach it."""
        if self.engine is None:
            return
        models = await self._model_provider.list_models()
        note = ""
        if models and models[0].source == "fallback":
            note = (
                "model catalog: static fallback -- the Anthropic Models "
                "API is not reachable under this session's OAuth auth"
            )
        rows = [(m.id, m.display_name) for m in models]
        current = str(getattr(self.engine, "model", None) or "")
        current_id = self._match_current_model(current, rows)
        self._open_chip_picker(
            rows, current_id,
            lambda chosen: self.run_worker(self._cmd_model(chosen), group="command"),
            note=note, title="model",
        )

    async def open_branch_picker(self) -> None:
        """The git chip's branch span -- lists local branches (the SAME
        no-argument listing ``/branch`` itself uses, ``engine.
        switch_branch(None)``), marks the current base, and on selection
        calls the SAME ``_cmd_branch`` coroutine ``/branch <name>`` uses
        (identical refusal messages: dirty tree, commits ahead, no
        worktree here -- none of that is reimplemented here)."""
        engine = self.engine
        git = self._git
        if engine is None or git is None or not git.repo:
            await self._system("branch: no repo here")
            return
        switcher = getattr(engine, "switch_branch", None)
        if switcher is None:
            await self._system("branch: this session's handle cannot switch branches")
            return
        try:
            result = await switcher(None)
        except Exception as exc:  # noqa: BLE001 -- same refusal-is-information
            # posture _cmd_branch itself follows.
            await self._system(f"branch: {type(exc).__name__}: {exc}")
            return
        rows = [(name, name) for name in (result.get("branches") or [])]
        self._open_chip_picker(
            rows, result.get("base"),
            lambda chosen: self.run_worker(self._cmd_branch(chosen), group="command"),
            title="branch",
        )

    async def open_effort_picker(self) -> None:
        """The effort chip -- only ever reachable when the chip itself is
        showing (hide-at-zero, same as the status bar's own convention),
        i.e. a connect-time effort was actually asserted on THIS session.
        Selecting a level here does exactly what ``/effort <level>`` does:
        saves it for NEW sessions and says, honestly, that this one keeps
        its own -- the note row says so BEFORE a choice is even made,
        rather than only after."""
        from . import engine as engine_mod

        if self.engine is None:
            return
        current = getattr(self.engine, "effort", None)
        rows = [(level, level) for level in engine_mod.EFFORT_LEVELS]
        note = (
            "applies to NEW sessions only (connect-time) -- this one "
            f"keeps {current or 'its own'}"
        )
        self._open_chip_picker(
            rows, current,
            lambda chosen: self.run_worker(self._cmd_effort(chosen), group="command"),
            note=note, title="effort",
        )

    def run_status_command(self, name: str) -> None:
        """The peers chip's click target -- runs a slash command exactly
        as if it had been typed and submitted (``_run_command`` is the
        SAME dispatch ``on_prompt_submitted`` uses for a non-passthrough
        row)."""
        self.run_worker(self._run_command(name), group="command")

    def run_compact_now(self) -> None:
        """The ctx% chip's click target -- ``/compact`` is a PASSTHROUGH
        row (doxa/commands.py: the literal prompt text is what triggers
        compaction and fires the PreCompact hook), so its dispatch is a
        turn, not a command -- the same ``run_worker(self._run_turn(...))``
        call ``on_prompt_submitted`` would make for that same text."""
        if self.engine is None:
            return
        self.run_worker(self._run_turn("/compact"), exclusive=True, group="turn")

    def copy_session_handle(self) -> None:
        """The session-handle chip's click target -- only ever visible
        (hide-at-zero) on an attached, detachable session, so ``sid`` here
        is never empty in practice; the guard is defensive only."""
        sid = str(getattr(self.engine, "session_id", "") or "")
        if not sid:
            return
        self.app.copy_to_clipboard(sid)
        self.run_worker(self._system(f"copied session handle: {sid[:8]}…"), group="command")

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

    async def _cmd_branch(self, args: str) -> None:
        """/branch -- no argument lists local branches (current base
        marked); an argument switches this session's base (item S #2).

        Only meaningful with worktree-per-session: a switch rebases the
        session's OWN worktree branch onto the new base -- free (a fast-
        forward, no history to replay) only when clean and zero commits
        ahead of the CURRENT base, the same rule
        doxa.worktrees.finalize's "kept doxa/<id> — merge when ready"
        convention already applies at session end; a dirty or committed-
        ahead worktree is refused in that same voice rather than silently
        carrying the diff across (doxa.worktrees.switch_base owns the
        exact wording). Without a session worktree at all (toggle off, or
        this handle just cannot switch), this refuses too: switching the
        ACTUAL checkout out from under a running session is exactly what
        worktree-per-session exists to prevent, so there is no `git
        checkout` fallback here."""
        engine = self.engine
        git = self._git
        if git is None or not git.repo:
            await self._system("branch: no repo here")
            return
        switcher = getattr(engine, "switch_branch", None)
        if switcher is None:
            await self._system(
                "branch: this session's handle cannot switch branches"
            )
            return
        target = args.split()[0] if args else None
        try:
            result = await switcher(target)
        except Exception as exc:  # noqa: BLE001 -- a refusal is information
            await self._system(f"branch: {type(exc).__name__}: {exc}")
            return
        if target is None:
            base = result.get("base")
            lines = [f"branch: {base or '(none)'}", ""]
            for name in result.get("branches") or []:
                mark = "▸" if name == base else " "
                lines.append(f" {mark} {name}")
            lines.append("")
            lines.append("usage: /branch <name>")
            await self._system("\n".join(lines))
            return
        if not result.get("ok"):
            await self._system(f"branch: {result.get('message') or 'switch refused'}")
            return
        self._refresh_status()
        await self._system(f"branch: {result.get('message') or 'switched'}")

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
        term = args.strip()
        if not term:
            await self._system(
                "search: type `/search ` and keep typing — results appear "
                "above the prompt as you type (↑/↓ to move, →/← to expand/"
                "collapse a session, enter to insert the excerpt, esc to "
                "close)"
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
        self._set_tab_class("-working", True)
        # A fresh turn starting is itself "seen" -- clear any stale
        # done-unseen dot from a PREVIOUS turn the user has not looked at
        # yet, rather than letting it sit there through a whole new one.
        self._set_tab_class("-done-unseen", False)
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
            await block.mark_done(None, None, True)
            await block_list.mount(SystemBlock(f"turn failed: {exc}"))
            block_list.scroll_end(animate=False)

        self.turn_in_flight = False
        self._set_tab_class("-working", False)
        self._refresh_status()
        # First completed turn of a repo-less session: name the tab from it.
        self._maybe_name_tab(prompt)

    async def _handle_event(self, ev: EngineEvent, block: TurnBlock, chips: dict[str, ToolChip]) -> None:
        if ev.type == "turn_started":
            return
        if ev.type == "text_delta":
            parent_id = ev.data.get("parent_id") or ""
            parent = chips.get(parent_id)
            if parent is not None:
                # A subagent narrating: trace material, nested under its
                # Task chip -- never mixed into the turn's own prose.
                parent.append_subagent_text(ev.data["text"])
                # Live routing: an open transcript tab for THIS parent gets
                # the same narration, alongside (not instead of) the chip.
                self._route_transcript_text(parent_id, ev.data["text"])
            else:
                await block.append_text(ev.data["text"])
        elif ev.type == "tool_call":
            block.hide_thinking()
            chip = ToolChip(ev.data["id"], ev.data["name"], ev.data["input"])
            chips[ev.data["id"]] = chip
            parent_id = ev.data.get("parent_id") or ""
            parent = chips.get(parent_id)
            if parent is not None:
                # Trace tree: a subagent's call nests under the Task chip
                # that spawned it, foldable at every level. An unknown
                # parent (ring truncation on replay) degrades to top level
                # -- the call is never dropped.
                await parent.subcalls.mount(chip)
                await self._route_transcript_chip(parent_id, chip)
            else:
                # Top-level chip: compacted behind the turn's ONE "Tool
                # calls (N)" section (see ToolCallsSection/add_tool_chip).
                await block.add_tool_chip(chip)
            if ev.data["name"] == "Task":
                # Subagent tracker: a Task call (top-level or nested -- a
                # subagent's own Task is tracked exactly like a top-level
                # one) starts a new entry in the running registry.
                await self._register_subagent(chip)
        elif ev.type == "tool_result":
            chip = chips.get(ev.data["id"])
            if chip is not None:
                chip.update_result(
                    ev.data["result_summary"], ev.data["is_error"],
                    ev.data["duration_ms"], image_path=ev.data.get("image_path"),
                )
                self._route_transcript_result(chip)
            if ev.data["id"] in self._subagents:
                await self._unregister_subagent(ev.data["id"])
        elif ev.type == "turn_done":
            # Same tier lookup _refresh_status/_usage_text already do --
            # keeps the per-turn figure consistent with both (item T).
            account = getattr(self.engine, "account", None) or {}
            tier = identity_mod.account_tier(account)
            await block.mark_done(
                ev.data.get("cost_usd"), ev.data.get("duration_ms"),
                ev.data.get("is_error", False), tier,
            )
            # The one place the headroom chip is recomputed: a turn just
            # spent budget, and the CLI may have refreshed its own cache.
            self._refresh_usage_chip()
            self._refresh_status()
            self._on_turn_done_status(ev.data.get("duration_ms"))

    # -- subagent tracker (queue item 4) ------------------------------

    async def _register_subagent(self, chip: "ToolChip") -> None:
        """One Task call started: add it to the running registry, then
        sync the second line and the status chip -- both read len() of
        the same dict, so this one write keeps them both correct."""
        self._subagents[chip.call_id] = chip
        await self._sync_subagent_line()
        self._refresh_status()

    async def _unregister_subagent(self, call_id: str) -> None:
        """That Task call's own tool_result just landed: it drops out of
        the running registry (the second line and the status chip shrink
        by one, possibly to zero) -- but an OPEN transcript tab for it
        stays open, just marked done."""
        self._subagents.pop(call_id, None)
        await self._sync_subagent_line()
        self._refresh_status()
        tab = self._transcript_tabs.get(call_id)
        if tab is not None:
            tab.mark_done()

    async def _sync_subagent_line(self) -> None:
        """Mount the second line on the first running subagent, unmount it
        on the last one finishing -- mount/unmount, never a display toggle,
        so an idle pane (the common case) carries zero cost for a feature
        it isn't using right now. While mounted its content is rewritten
        on every registry change (cheap: one markup string, no repaint
        storm any worse than a status-bar update already is)."""
        if self._subagents and self._subagent_line is None:
            self._subagent_line = SubagentLine(self)
            with contextlib.suppress(Exception):
                status_bar = self.query_one("#status-bar", Static)
                await self.mount(self._subagent_line, after=status_bar)
        elif not self._subagents and self._subagent_line is not None:
            line, self._subagent_line = self._subagent_line, None
            with contextlib.suppress(Exception):
                await line.remove()
        if self._subagent_line is not None:
            self._subagent_line.refresh_labels([
                (call_id, _subagent_label(chip))
                for call_id, chip in self._subagents.items()
            ])

    async def open_transcript(self, call_id: str) -> None:
        """Open (or, if it is already open, just focus) the read-only
        transcript tab for one RUNNING subagent -- the only way in here is
        a click on the second line, which only ever offers ids currently
        in ``self._subagents``, so a miss (finished and dropped between
        the click and this running) degrades to a silent no-op rather than
        a crash.

        Focus moves to the new tab's own scroll container (it is
        focusable, so the arrow keys/PageUp/PageDown a reader would reach
        for just work) -- load-bearing, not just nicety: TabbedContent's
        own ``_on_tab_pane_focused`` snaps ``.active`` back to whichever
        pane holds the CURRENTLY focused widget, and this pane's own
        ``#prompt-input`` stays focused (its tab merely hides, focus does
        not move on its own) unless something claims focus in the pane
        being switched to -- exactly what SessionPane's own boot already
        does for itself by focusing its prompt input on mount."""
        try:
            tabbed = self.app.query_one("#session-tabs", TabbedContent)
        except Exception:  # noqa: BLE001 -- app mid-teardown; nothing to open
            return
        existing = self._transcript_tabs.get(call_id)
        if existing is not None:
            tabbed.active = existing.id or tabbed.active
            existing.scroll.focus()
            return
        chip = self._subagents.get(call_id)
        if chip is None:
            return
        label = _subagent_label(chip)
        tab = SubagentTranscriptTab(
            call_id, label, self, id=f"trace-{self.id}-{call_id}",
        )
        self._transcript_tabs[call_id] = tab
        await tabbed.add_pane(tab)
        await tab.replay(chip)
        tabbed.active = tab.id or tabbed.active
        tab.scroll.focus()

    def _route_transcript_text(self, parent_id: str, text: str) -> None:
        tab = self._transcript_tabs.get(parent_id)
        if tab is not None:
            tab.append_narration(text)

    async def _route_transcript_chip(self, parent_id: str, chip: "ToolChip") -> None:
        tab = self._transcript_tabs.get(parent_id)
        if tab is not None:
            await tab.mirror_chip(chip)

    def _route_transcript_result(self, chip: "ToolChip") -> None:
        """A tool_result may belong to a chip mirrored inside some open
        transcript tab (a direct child of the tab's own subagent) -- find
        it by call id and bring the mirror's own result up to date too.
        At most one tab can hold a mirror for a given id in practice (ids
        are the SDK's own tool_use ids), so the first match wins."""
        for tab in self._transcript_tabs.values():
            mirror = tab.mirror_chips.get(chip.call_id)
            if mirror is not None:
                mirror.update_result(
                    chip.tool_result, chip.is_error, chip.duration_ms,
                    image_path=chip.tool_image_path,
                )
                break

    # -- needs-input dialog (queue item 5) -----------------------------

    def _open_needs_input(self, data: dict) -> None:
        """A fresh needs_input event: open the dialog, blink the tab
        (cleared on answer or on activating this tab -- set_needs_input's
        own, already-tested convention, unchanged here), and notify --
        gated exactly like notify_turn_done, by THIS pane's real
        app_has_focus (the detached-daemon case, no client at all
        attached, is handled separately, daemon-side -- see
        doxa/daemon.py's _peer_pump)."""
        popup = self.query_one("#needs-input-popup", NeedsInputPopup)
        popup.ask(data)
        self.set_needs_input(True)
        notify_mod.notify_needs_input(
            getattr(self.app, "app_has_focus", True),
            self.display_name(),
            _needs_input_summary(data),
        )

    async def _resolve_needs_input(
        self, popup: "NeedsInputPopup", index: "int | None", decline: bool,
    ) -> None:
        """Answer (or decline) whatever the popup currently holds, and
        tell the engine -- SessionEngine and EngineClient both expose
        ``answer_needs_input`` (see doxa/client.py's engine-parity note),
        so this reads the same regardless of the daemon split. A stale
        popup (already closed -- e.g. a needs_input_resolved from another
        client beat this keystroke) is a silent no-op, same discipline
        every other "the widget might already be gone" call site in this
        pane follows."""
        if not popup.is_open:
            return
        request_id = popup.request_id
        if decline:
            answer = (
                {"declined": True} if popup.kind == "ask_user"
                else {"decision": "deny"}
            )
            popup.close()
        else:
            assert index is not None
            if not popup.choose_index(index):
                return  # ask_user: more questions to go -- stays open
            answer = popup.answer_payload()
            popup.close()
        self.set_needs_input(False)
        # Refresh NOW: this path runs off a key/click worker, never
        # through _peer_pump's own trailing _refresh_status() call -- the
        # engine's matching needs_input_resolved event will ALSO loop back
        # through that pump shortly (in-process, or fanned out by the
        # daemon), but the status-bar hint and tab class must not wait on
        # that round-trip to catch up.
        self._refresh_status()
        engine = self.engine
        if engine is not None and request_id:
            answerer = getattr(engine, "answer_needs_input", None)
            if answerer is not None:
                with contextlib.suppress(Exception):
                    await answerer(request_id, answer)

    def _on_turn_done_status(self, duration_ms: "float | None") -> None:
        """Tab-status + desktop-notification side effects of ONE finished
        turn. Reached for a turn THIS client drove (_run_turn) and for one
        replayed in from another attached client of the same daemon
        (_peer_pump's turn_started/turn_done forwarding) -- both funnel
        through _handle_event's turn_done branch, and both are equally "a
        turn just finished on a session you might not be looking at"."""
        active = self.app.active_pane is self
        if not active:
            self._set_tab_class("-done-unseen", True)
        notify_mod.notify_turn_done(
            getattr(self.app, "app_has_focus", True),
            self.display_name(),
            duration_ms,
        )

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
        # Terminal-window focus, for "auto" desktop notifications (only
        # notify while you are NOT looking at the terminal). Init True: a
        # window is assumed focused until an AppBlur says otherwise, which
        # matters on a terminal with no focus-reporting -- see the
        # AppFocus/AppBlur handlers below and doxa/notify.py's "auto"
        # docstring for what that degrades to there.
        self.app_has_focus = True
        # One-shot "has this run already told you about an update" latch --
        # the background checker in on_mount fires at most once per launch.
        self._update_notified = False
        # Bring lore_core's own in-process notification (staged-proposal
        # review, fired synchronously from doxa.engine's review path) in
        # line with the notify_lore toggle. Also re-run whenever the
        # settings modal saves (action_settings) -- the knob is live, not
        # boot-only.
        notify_mod.sync_lore_notify_env()
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
            # Looking at this tab now clears both "you missed something"
            # signals: the done-unseen dot from a turn that finished while
            # you were elsewhere, and any attention blink (and its timer --
            # set_needs_input(False) is what stops that).
            pane._set_tab_class("-done-unseen", False)
            pane.set_needs_input(False)
            with contextlib.suppress(Exception):
                pane.query_one("#prompt-input", PromptInput).focus()
        elif isinstance(event.pane, SubagentTranscriptTab):
            # Same "you're looking at it now" clear, for a transcript tab
            # that finished (and picked up -done-unseen) while it sat in
            # the background -- it carries no -working/-attention, so
            # -done-unseen is the only class it ever needs cleared.
            event.pane._set_tab_class("-done-unseen", False)

    # -- window focus, for "auto" desktop notifications ---------------

    @on(events.AppFocus)
    def _on_app_focus(self, event: events.AppFocus) -> None:
        self.app_has_focus = True

    @on(events.AppBlur)
    def _on_app_blur(self, event: events.AppBlur) -> None:
        self.app_has_focus = False

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
                pane.query_one("#prompt-input", PromptInput).focus()

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

        A subagent transcript tab (SubagentTranscriptTab) takes the SAME
        key to a much simpler path: it is not a session (self.active_pane
        -- SessionPane-only -- comes back None for one), so there is no
        daemon to detach and no turn-in-flight question to ask; it just
        closes. There is always at least one SessionPane, so a transcript
        tab is never "the last tab" and never reaches the close-the-app
        branch _close_pane below falls back to.

        Closing the last SESSION tab closes the app, on the same detach
        semantics."""
        pane = self.active_pane
        if pane is not None:
            await self._close_pane(pane, terminate=False)
            return
        with contextlib.suppress(Exception):
            active = self.query_one("#session-tabs", TabbedContent).active_pane
            if isinstance(active, SubagentTranscriptTab):
                await self._close_transcript_tab(active)

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

    async def _close_transcript_tab(self, tab: "SubagentTranscriptTab") -> None:
        """Ctrl+W (or the palette's Close tab) on a subagent transcript
        tab: no engine to stop, no daemon to detach -- just remove it and
        drop the owning pane's own reference to it."""
        tab.owner._transcript_tabs.pop(tab.call_id, None)
        with contextlib.suppress(Exception):
            await self.query_one("#session-tabs", TabbedContent).remove_pane(
                tab.id or ""
            )

    async def _close_pane(self, pane: "SessionPane", terminate: bool) -> None:
        """One close path, two dispositions. Closing the LAST tab closes the
        app on the same disposition -- a window with no tabs is not a
        window, and the session's fate must not depend on tab arithmetic.

        A closing session takes its OWN open transcript tabs down with it
        first -- they have no engine and nothing left to route events into
        once the session that spawned their subagents is gone."""
        for tab in list(pane._transcript_tabs.values()):
            await self._close_transcript_tab(tab)
        if terminate:
            note = await pane.stop()
            if note:
                # The pane itself is about to be removed (or the whole app
                # quits, below) -- a toast is screen-level, not pane-level,
                # so it survives the tab it was about -- unlike a SystemBlock
                # mounted in the closing pane's own block list, which the
                # user would never get a chance to see.
                self.notify(note, severity="information", timeout=10)
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
        note = await pane.stop()
        if note:
            self.notify(note, severity="information", timeout=10)
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
        prompt = pane.query_one("#prompt-input", PromptInput)
        prompt.value = text  # the setter also moves the cursor to the end
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
            notify_mod.sync_lore_notify_env()
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
            notify_mod.sync_lore_notify_env()
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
        # Non-blocking: a `git fetch` (even a quiet, local one) must never
        # be on boot's critical path. Exclusive group of its own so a
        # pathological double-mount cannot stack two of these.
        self.run_worker(
            self._check_for_update(), exclusive=True, group="update-check"
        )

    async def _check_for_update(self) -> None:
        """Boot-time "is there something to pull" check -- see
        doxa.update.check_for_update for the git-level detail and its
        all-failures-are-silent posture. Notifies at most once per app run
        (the latch, not the checker, owns "once": the checker itself is
        stateless and could in principle be called again)."""
        from . import update as update_mod

        try:
            available = await asyncio.to_thread(update_mod.check_for_update)
        except Exception:  # noqa: BLE001 -- advisory only, never surfaces
            return
        if available and not self._update_notified:
            self._update_notified = True
            notify_mod.notify_update_available(self.app_has_focus)

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
                note = await pane.stop()
                if note:
                    # Best-effort: the app quits right after this loop, so
                    # this toast may not get a paint frame -- the daemon's
                    # own log line (doxa.daemon._finalize_worktree) is the
                    # channel actually guaranteed to survive quitting the
                    # TUI, exactly the "headless" case worktrees.finalize's
                    # docstring calls out.
                    self.notify(note, severity="information", timeout=10)
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

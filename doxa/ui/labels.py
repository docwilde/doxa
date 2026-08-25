"""doxa.ui.labels -- the small formatters, and the constants they read.

Extracted from ``doxa/app.py`` unchanged. Everything in this module is a
pure function of its arguments (or a module constant): tab labels, the
context chip's escalation, the belief/pending row text, ``/help``. The rest
of :mod:`doxa.ui` depends on this module; this module depends on no other
part of it, which is what keeps the import graph a tree.

The exceptions each carry their reason in their own docstring:
:func:`_write_tab_label` / :func:`_write_tab_class` take an app and write
onto a Tab header (they are shared by two widgets that are not each
other's parent), :func:`app_bindings` reads ``DoxaApp.BINDINGS``
through a deferred import, because /help documents the bindings Textual
actually dispatches and there is no second place to read them from, and
(item O) :func:`_binding_mark` reads :mod:`doxa.keyboard`'s settled probe
result, because a binding /help advertises that this terminal cannot
physically send has to be marked where it is advertised, and the only
place that knows is the module that asked the terminal.
"""

from __future__ import annotations

import contextlib
from typing import Any

from textual.widgets import TabbedContent

from .. import commands as commands_mod
from .. import config as config_mod
from .. import providers as providers_mod


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


# Item X (ctx absolute): the inline `24k/200k` half of the ctx chip is
# spent width, and the status bar is the most contended row in the app
# (TAB_MODEL_MIN/TAB_REPO_MIN exist for the same reason one column over).
# So it appears only when the terminal is at least this wide -- below it
# the chip falls back to the percentage alone and the numbers stay in the
# tooltip, which is where they live for every user who never turns the
# setting on. 100 columns is measured against the widest ordinary chip
# set (model · effort · git · sub · s/w · ctx · beliefs), which already
# fills ~95 columns before this segment is added.
CTX_ABSOLUTE_MIN_COLS = 100


def fmt_tokens(count: "int | None") -> str:
    """A token count for a status chip: `812`, `24k`, `1.2M`, and `—` for
    a count nobody has reported. Rounded, because the chip answers "how
    much room is left", not "how many tokens exactly" -- /usage and the
    tooltip carry the exact figure, in full, with separators."""
    if count is None:
        return "—"
    if count < 1000:
        return str(int(count))
    if count < 1_000_000:
        return f"{count / 1000:.0f}k"
    return f"{count / 1_000_000:.1f}M"


def ctx_absolute_text(
    used: "int | None", total: "int | None"
) -> "str | None":
    """`24k/200k` -- the inline absolute segment, or None when there is
    nothing measured to print.

    An unknown LIMIT is `?`, never a substituted 200000: DOXA drives
    several models with very different windows, and a prior measurement in
    this project found the Models API unreachable under OAuth-only auth,
    so there is no second source to fall back on. Saying "unknown" is the
    honest degradation; printing somebody else's window size is not."""
    if used is None and total is None:
        return None
    return f"{fmt_tokens(used)}/{'?' if total is None else fmt_tokens(total)}"


def ctx_text(
    percentage: "float | None",
    used: "int | None" = None,
    total: "int | None" = None,
    *,
    absolute: bool = False,
) -> str:
    """The context chip's PLAIN text -- what a reader sees once the markup
    is stripped, and therefore what the tooltip machinery has to match on.

    Split out of :func:`ctx_chip` for item X because ``StatusBar``'s
    per-chip tooltip resolves by finding the chip's text inside the bar's
    markup-STRIPPED string (``StatusBar._tooltip_for_x``): handing it a
    string with `[#D9534F]…[/]` still in it can never match, which is why
    the ctx chip's hint used to go missing at exactly the amber and red
    tiers where it matters most. One function builds the words, the other
    colors them, and the two cannot say different things."""
    tail = ctx_absolute_text(used, total) if absolute else None
    base = "ctx —" if percentage is None else f"ctx {percentage:.0f}%"
    return f"{base} {tail}" if tail else base


def ctx_chip(
    percentage: "float | None",
    used: "int | None" = None,
    total: "int | None" = None,
    *,
    absolute: bool = False,
) -> str:
    """The context chip, escalating normal -> amber -> red. Markup only:
    the percentage is always present, in every tier.

    Item X: ``absolute=True`` appends the `used/total` segment INSIDE the
    same pressure-colored span, so the whole chip escalates as one thing
    rather than half of it turning red. It is opt-in (``DOXA_CTX_ABSOLUTE``
    / the settings modal) because the percentage alone is what this chip
    has always cost the status bar in width, and the absolute numbers stay
    reachable without it -- they are in this chip's tooltip
    unconditionally (see ``PaneChipsMixin._status_chips``) and in
    ``/usage``."""
    text = ctx_text(percentage, used, total, absolute=absolute)
    if percentage is None:
        return text
    if percentage >= CTX_RED_PCT:
        return f"[{CTX_RED}]{text}[/]"
    if percentage >= CTX_AMBER_PCT:
        return f"[{CTX_AMBER}]{text}[/]"
    return text


def _belief_scope_label(subject: str) -> str:
    """Which GROUP a belief's row falls under in the beliefs chip's picker
    (item 3) -- derived from lore_core's own subject vocabulary
    (``lore_core.beliefs.belief_subject``: ``"user"``, ``"user-model"``, or
    ``"project:<slug>"`` -- verified against the installed lore_core, there
    is no separate ``scope`` column), data-driven rather than a hardcoded
    two-way branch so a future subject prefix (LORE issue #41's proposed
    ``machine:<id>``, still an open, unimplemented proposal -- NOT built
    here) slots into its own group the moment lore_core starts writing one,
    with no change to this function. ``"user-model"`` stays its own group
    (interaction-model beliefs, never folded into plain "user") -- the same
    distinction belief_subject's own docstring draws."""
    if subject == "user-model":
        return "user model"
    if ":" in subject:
        return subject.split(":", 1)[0]  # "project:<slug>" -> "project"
    return subject or "user"


def _fmt_belief_row(belief: dict) -> str:
    """One beliefs-picker row: the claim text, ellipsized -- filtering
    (ChipPicker's type-to-filter) matches against exactly this string, so
    the claim has to be IN it, not just referenced by an id."""
    claim = _one_line(str(belief.get("claim") or ""), 200)
    return ellipsize(claim, 72)


def _fmt_pending_row(text: str) -> str:
    """One ``/pending`` row: the staged proposal's own text, ellipsized --
    same rule :func:`_fmt_belief_row` follows and for the same reason,
    ChipPicker's type-to-filter matches this string. A staged proposal has
    no id, title or subject to fall back on (``lore_deriver.pending_texts``
    returns text and nothing else), so the text is not merely the best row
    label available, it is the only one."""
    return ellipsize(_one_line(str(text or ""), 200), 72)


def app_bindings() -> "list[tuple[str, str]]":
    """``(key, description)`` for every app-level binding, read off
    ``DoxaApp.BINDINGS`` itself -- the thing Textual actually dispatches.
    /help renders this, so a binding cannot exist without being documented
    and cannot be documented without existing."""
    # Deferred: doxa.app imports this package, so the arrow only points
    # back at call time. /help is the only caller and it runs long after
    # the app class exists, so the cycle never has to be resolved at
    # import -- and reading BINDINGS off any COPY of it would be the
    # hand-maintained list this function exists to avoid.
    from ..app import DoxaApp

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


UNREACHABLE_MARK = "✗"
"""Appended to a key in /help when THIS terminal cannot send it (item O).

A glyph rather than prose because it has to fit inside a padded key
column, and this one rather than a warning triangle because the claim is
"the key does not arrive", not "careful"."""


def _binding_mark(key: str) -> str:
    """:data:`UNREACHABLE_MARK` when `key` is unreachable in the terminal
    we are measurably in, empty otherwise.

    Empty covers BOTH "the terminal can send it" and "we could not
    measure the terminal", and those two collapsing into the same output
    is the design, not a gap in it: :func:`doxa.keyboard.is_unreachable`
    only ever answers True off a real measurement, because a /help that
    wrongly labels a working key as dead sends the user to their terminal
    settings for a bug that is ours."""
    from .. import keyboard as keyboard_mod

    return UNREACHABLE_MARK if keyboard_mod.is_unreachable(key) else ""


def unreachable_bindings() -> "list[str]":
    """Every advertised binding this terminal cannot deliver, in pretty
    form (``["Ctrl+,"]``) -- the command bindings and the bare hotkeys
    both, read off the same two sources /help renders. Empty on a terminal
    that grants the kitty protocol AND on one we never got to measure.

    ``/doctor`` names these; /help marks them in place."""
    keys = [cmd.binding for cmd in commands_mod.REGISTRY if cmd.binding]
    keys += [key for key, _description in app_bindings()]
    seen: set[str] = set()
    out: list[str] = []
    for key in keys:
        if key in seen:
            continue
        seen.add(key)
        if _binding_mark(key):
            out.append(_pretty_key(key))
    return out


def help_text() -> str:
    """``/help``, generated from the command registry AND the live binding
    list -- never a hand-maintained list, because a hand-maintained list is
    wrong by the second command anyone adds.

    Two columns for commands (call form + what it does, with its key
    binding where one reaches the same place), then a hotkeys section for
    the bindings that have no slash form at all, and finally the ONE
    prompt convention that is not a registry row at all (``!``, item Q).

    That last section is hand-written for the reason ``!`` is not a
    registry row in the first place (see :mod:`doxa.shell`): the registry
    is a dispatch surface, and the shell executor must not be reachable
    from one. A user still has to be able to find out the feature exists,
    so /help says so in prose rather than the registry saying it in
    data.

    Item O adds one thing to the rendering: a binding THIS terminal cannot
    physically send is marked (:data:`UNREACHABLE_MARK`) and explained in
    a footnote that only appears when something was marked. A documented
    key that does nothing is the failure this fixes -- and on a terminal
    whose protocol we could not measure, nothing is marked and /help is
    byte-identical to what it always was."""
    lines = ["commands", ""]
    width = max(len(cmd.call_form()) for cmd in commands_mod.REGISTRY)
    bound: set[str] = set()
    marked = False
    # Same grouping and the same order the dropdown and the palette use --
    # commands.grouped() is the single sequence (see doxa/commands.py).
    for group, group_commands in commands_mod.grouped():
        lines.append(f"  {group}")
        for command in group_commands:
            note = ""
            if command.binding:
                bound.add(command.binding)
                mark = _binding_mark(command.binding)
                marked = marked or bool(mark)
                note = f"   [{_pretty_key(command.binding)}]{mark}"
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
        # The mark is part of the key column, not something trailing the
        # description: it belongs to the key it disqualifies, and padding
        # the column to the marked width keeps the descriptions aligned.
        rendered = [(_pretty_key(k) + _binding_mark(k), d) for k, d in hotkeys]
        marked = marked or any(k.endswith(UNREACHABLE_MARK) for k, _d in rendered)
        key_width = max(len(k) for k, _d in rendered)
        for key, description in rendered:
            lines.append(f"  {key:<{key_width}}  {description}")
    if marked:
        lines += [
            "",
            f"  {UNREACHABLE_MARK} This terminal cannot send that combination: it "
            "speaks the legacy key",
            "    encoding, which has no way to express it. Where the same place "
            "has a slash",
            "    command, that command is the way in. A terminal that supports "
            "the",
            "    kitty keyboard protocol (kitty, Ghostty, WezTerm, foot, recent",
            "    Alacritty/iTerm2) would send it; /about reports what was "
            "measured here.",
        ]
    lines += [
        "",
        "shell (no slash form, deliberately)",
        "",
        "  !<command>   Run it in this session's directory and show the "
        "output here",
        "",
        "  It runs with YOUR full privileges and there is no confirmation "
        "step.",
        "  Only a line you type at this prompt can reach it: it is not a "
        "command,",
        "  not a tool, and not something the model or a peer session can "
        "trigger.",
        "  Neither the command nor its output ever enters the model's "
        "context.",
    ]
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


CONTEXT_UNAVAILABLE = (
    "context: this session cannot report a breakdown — its handle has no "
    "get_context_usage (an older SDK, or a session that has not connected "
    "yet). Nothing is estimated here in its place."
)
"""What ``/context`` prints when there is no measurement. Item K's whole
rule in one string: a diagnostic surface that cannot measure something says
so, rather than showing a plausible number in its place."""


def context_breakdown_text(breakdown: "dict | None") -> str:
    """``/context`` (item K), rendered from
    :func:`doxa.engine.context_breakdown`.

    EVERY token figure below was counted by the claude CLI itself, for this
    session, against the request it actually sends -- DOXA runs no
    tokenizer and holds no per-component estimate. The only arithmetic done
    here is a share of the window (``tokens / max_tokens``), which is
    division of two measured integers, and it is only shown when the CLI
    reported the window size. A row whose number is absent is OMITTED; a
    breakdown that is absent entirely prints
    :data:`CONTEXT_UNAVAILABLE`."""
    if not breakdown:
        return CONTEXT_UNAVAILABLE
    total = breakdown.get("total_tokens")
    window = breakdown.get("max_tokens")
    lines: list[str] = ["context"]
    if breakdown.get("model"):
        lines.append(f"model      {breakdown['model']}")
    percent = breakdown.get("percentage")
    if isinstance(total, (int, float)) and isinstance(window, (int, float)):
        share = (
            f"  ·  {float(percent):.1f}%" if isinstance(percent, (int, float)) else ""
        )
        lines.append(f"in use     {int(total):,} / {int(window):,} tokens{share}")
    elif isinstance(total, (int, float)):
        lines.append(f"in use     {int(total):,} tokens")
    raw_window = breakdown.get("raw_max_tokens")
    if (
        isinstance(raw_window, (int, float))
        and isinstance(window, (int, float))
        and int(raw_window) != int(window)
    ):
        lines.append(
            f"window     {int(window):,} usable of {int(raw_window):,} — the "
            "difference is the autocompact buffer"
        )
    threshold = breakdown.get("autocompact_threshold")
    if isinstance(threshold, (int, float)):
        lines.append(
            f"autocompact  at {int(threshold):,} tokens"
            + ("" if breakdown.get("autocompact_enabled") else "  (disabled)")
        )
    lines += _context_section(
        "", breakdown.get("categories"), breakdown.get("categories_dropped"),
        lambda row: str(row.get("name") or "?"), window,
    )
    lines += _context_section(
        "memory files", breakdown.get("memory_files"),
        breakdown.get("memory_files_dropped"),
        lambda row: str(row.get("path") or "?"), window,
    )
    lines += _context_section(
        "mcp tools", breakdown.get("mcp_tools"), breakdown.get("mcp_tools_dropped"),
        lambda row: (
            f"{row.get('server')}: {row.get('name')}" if row.get("server")
            else str(row.get("name") or "?")
        ),
        window,
    )
    lines.append("")
    lines.append(
        "every token count above is the claude CLI's own measurement of "
        "THIS session's window — the same one the ctx% chip reads. Nothing "
        "on this screen is estimated."
    )
    chars = breakdown.get("lore_snapshot_chars")
    if isinstance(chars, int):
        lines.append(
            f"lore snapshot: {chars:,} characters appended to the system "
            "prompt at connect. Its tokens are counted INSIDE the system-"
            "prompt row above — the CLI cannot tell DOXA's appendix from the "
            "preset, so no separate token figure is claimed for it."
        )
    return "\n".join(lines)


def _context_section(
    title: str, rows: Any, dropped: Any, label: Any, window: Any
) -> list[str]:
    """One indented block of ``label   tokens   share-of-window`` rows.
    Empty list in, empty list out -- a heading with nothing under it is the
    placeholder row doxa/commands.py already refuses to ship."""
    if not isinstance(rows, list) or not rows:
        return []
    out = [""]
    if title:
        out.append(title)
    width = min(max(max(len(label(row)) for row in rows), 12), 40)
    for row in rows:
        tokens = row.get("tokens")
        if not isinstance(tokens, (int, float)):
            continue  # no number, no row -- see this module's rule above
        share = ""
        if isinstance(window, (int, float)) and window:
            share = f"  {float(tokens) / float(window) * 100:5.1f}% of window"
        out.append(f"  {label(row)[:width]:<{width}}  {int(tokens):>9,}{share}")
    if isinstance(dropped, int) and dropped > 0:
        out.append(f"  … and {dropped:,} more not shown")
    return out


def git_branch_symbol() -> str:
    """The nerd-font branch glyph (U+E0A0) when the user opted in via
    DOXA_NERD_FONT (a TUI cannot detect font glyph coverage itself);
    the universally-rendering ⎇ otherwise. Read through doxa.config, so
    the settings modal's stored value works exactly like the env var --
    env first, file second (doxa/config.py's one precedence rule)."""
    return "\ue0a0" if config_mod.raw("DOXA_NERD_FONT").strip() else "⎇"

# -- curated-memory fill (v0.44.0) ------------------------------------------

_MEM_FILL_CACHE: "dict[str, tuple[float, int]]" = {}


def memory_fill(scope: str, project: "str | None" = None) -> "tuple[int, int] | None":
    """(chars used, cap) for a curated-memory scope, or None if unknown.

    Read from the file lore_core itself writes, so the number matches what
    `lore status` and the injected snapshot report exactly -- an
    approximation from st_size would drift on any multi-byte character,
    and the whole point of the chip is that it agrees with the cap the
    write path enforces.

    Cached on mtime: `_refresh_status` already pays for a belief COUNT(*)
    on every event-driven refresh (see PaneChipsMixin), and this must not
    add two file reads on top of it. An unchanged file costs one stat.
    """
    try:
        from lore_core import memory as lore_memory

        # memory_path(scope, slug) -- slug is ignored for the user scope,
        # required positionally regardless.
        path = lore_memory.memory_path(scope, project or "")
        cap = lore_memory.memory_cap(scope)
        stamp = path.stat().st_mtime
        key = str(path)
        hit = _MEM_FILL_CACHE.get(key)
        if hit is not None and hit[0] == stamp:
            return hit[1], cap
        used = len(path.read_text(encoding="utf-8"))
        _MEM_FILL_CACHE[key] = (stamp, used)
        return used, cap
    except Exception:
        # A missing store, an older lore_core, an unreadable file: the chip
        # simply does not appear. Memory fill is a convenience, never a
        # reason to degrade the status bar.
        return None


def memory_fill_chip(user: "tuple[int, int] | None",
                     project: "tuple[int, int] | None") -> "tuple[str, str] | None":
    """(chip text, hint) for the curated-memory fill, or None to omit.

    Renders as `mem u63% p39%` -- two percentages, because the caps are
    separate and fill at different rates: user memory holds facts that
    never stop being true and creeps up forever, project memory rotates
    with the repo. One merged number would hide the one that is about to
    start refusing writes.
    """
    parts, hints = [], []
    for tag, pair, name in (("u", user, "user"), ("p", project, "project")):
        if pair is None:
            continue
        used, cap = pair
        pct = round(100 * used / cap) if cap else 0
        parts.append(f"{tag}{pct}%")
        hints.append(f"{name} memory {used}/{cap} chars ({pct}%)")
    if not parts:
        return None
    return (
        "mem " + " ".join(parts),
        " · ".join(hints) + " -- curated memory injected at session start; "
        "a write past the cap fails and lists the entries so they get "
        "consolidated rather than silently dropped",
    )
